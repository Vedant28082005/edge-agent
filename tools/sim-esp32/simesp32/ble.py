"""BLE provisioning in the simulator (S2b): the board side of contracts/ble-provisioning.md (proto 1) as a fake GATT
peer, and the client side that the dashboard (Web Bluetooth) and the Flutter app implement.

The board mirrors firmware/esp32-core/src/edge/BleProv.cpp rule for rule (window, PoP in the HKDF info, lockout,
chunking, AES-256-GCM with AAD = node_id). The radio is replaced by `LocalPeer` (in-process) or a loopback-only TCP
JSON-lines server (`serve_board`) so a separate process (the e2e script) can play the app. No secret is ever logged.

SECURITY NOTE: with a PoP this is authenticated against an active MITM (the PoP is mixed into the key derivation and
never travels over the air). Without a PoP (`require_pop=False`) it is the contract's v1 unauthenticated ECDH:
confidentiality against passive sniffing only.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import socket
import socketserver
import struct
import threading
from dataclasses import dataclass, field

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from .clock import Clock

log = logging.getLogger("sim.ble")

KDF_INFO = b"edge-agent-ble-v1"
MAX_BUNDLE = 4096
MAX_BLOB = MAX_BUNDLE + 28

# characteristic names used on the fake peer
INFO, SESSION, DATA, RESULT = "info", "session", "data", "result"


def b64e(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def b64d(s: str) -> bytes:
    return base64.b64decode(s, validate=True)


def new_pop() -> str:
    """128-bit per-board PoP, Crockford-style base32 without padding (26 chars). High entropy on purpose: a
    captured transcript allows an offline guess of a short code, so a 6-digit code would not be acceptable."""
    return base64.b32encode(os.urandom(16)).decode("ascii").rstrip("=")


@dataclass
class Policy:
    window_s: float = 300.0
    max_failures: int = 5
    lock_s: float = 300.0
    idle_s: float = 30.0
    require_pop: bool = True


class BleBoard:
    """States: closed | open | session | receiving | stored | locked."""

    def __init__(self, node_id: str, pop: str, clock: Clock | None = None, policy: Policy | None = None,
                 hw: str = "esp32-sim", fw: str = "0.1.0-sim"):
        self.node_id, self.hw, self.fw = node_id, hw, fw
        self._pop = pop or ""
        self.clock = clock or Clock()
        self.p = policy or Policy()
        self.lock = threading.RLock()
        self.state = "closed"
        self.code = "ok"
        self.failures = 0
        self.has_creds = False
        self.config: dict | None = None          # what NVS would hold; only read by the node after `stored`
        self._key: bytes | None = None
        self._blob = bytearray()
        self._next_seq = 0
        self._window_end = 0.0
        self._lock_end = 0.0
        self._last_chunk = 0.0
        self._stored_pending = False
        self.stats = {"sessions": 0, "bad_bundle": 0, "locks": 0, "stored": 0}

    # ---------------------------------------------------------------- lifecycle
    def _now(self):
        return self.clock.monotonic()

    def _open(self):
        self._drop()
        self.failures = 0
        self.state = "open"
        self._window_end = self._now() + self.p.window_s
        self.code = "ok"

    def _drop(self):
        self._key = None
        self._blob = bytearray()
        self._next_seq = 0

    @property
    def advertising(self):
        return self.state in ("open", "session", "receiving")

    def _pop_missing(self):
        return self.p.require_pop and not self._pop

    def boot(self, has_creds: bool) -> bool:
        with self.lock:
            self.has_creds = has_creds
            if has_creds or self._pop_missing():
                self.state = "closed"
                return False
            self._open()
            return True

    def button_hold(self) -> bool:
        with self.lock:
            if self.state in ("locked", "stored") or self._pop_missing():
                return False
            self._open()
            return True

    def factory_reset(self) -> bool:
        """BOOT held 10 s / cmd unprovision: wipe config (the PoP stays), open a fresh window."""
        with self.lock:
            self.config = None
            self.has_creds = False
            if self.state != "locked" and not self._pop_missing():
                self._open()
            return True

    def tick(self):
        with self.lock:
            now = self._now()
            if self.state == "locked":
                if now >= self._lock_end:
                    self.failures = 0
                    if not self.has_creds and not self._pop_missing():
                        self._open()
                    else:
                        self.state = "closed"
                return
            if self.state in ("closed", "stored"):
                return
            if now >= self._window_end:
                self._drop()
                self.state, self.code = "closed", "window_closed"
                return
            if self.state == "receiving" and now >= self._last_chunk + self.p.idle_s:
                self._drop()
                self.state, self.code = "open", "bad_chunk"

    def _fail(self, code: str, count: bool):
        self.code = code
        self._drop()
        if self.state != "locked":
            self.state = "open"
        if count:
            self.failures += 1
            if self.failures >= self.p.max_failures:
                self.state = "locked"
                self._lock_end = self._now() + self.p.lock_s
                self.code = "window_closed"      # contract has no `locked` code (gap)
                self.stats["locks"] += 1
                log.warning("%s: BLE locked for %ss after %d failed attempts", self.node_id, self.p.lock_s, self.failures)

    def take_stored(self) -> dict | None:
        """The node calls this once: the stored config (then BLE is off and the node 'reboots' and connects)."""
        with self.lock:
            if not self._stored_pending:
                return None
            self._stored_pending = False
            return self.config

    # ---------------------------------------------------------------- GATT surface
    def read(self, char: str) -> bytes:
        with self.lock:
            self.tick()
            if char == INFO:
                left = max(0, int(self._window_end - self._now())) if self.advertising else 0
                return json.dumps({"proto": 1, "node_id": self.node_id, "hw": self.hw, "fw": self.fw,
                                   "window_left_s": left, "pop_required": bool(self._pop)},
                                  separators=(",", ":")).encode()
            if char == RESULT:
                st = {"session": "session", "receiving": "receiving", "stored": "stored"}.get(
                    self.state, "idle" if self.code == "ok" else "error")
                return json.dumps({"state": st, "code": self.code}, separators=(",", ":")).encode()
            raise KeyError(char)

    def write_session(self, data: bytes) -> bytes | None:
        with self.lock:
            self.tick()
            if not self.advertising:
                self.code = "window_closed"
                return None
            try:
                req = json.loads(data.decode("utf-8")) if 0 < len(data) <= 256 else None
                ok = (isinstance(req, dict) and req.get("proto") == 1 and isinstance(req.get("client_pub"), str)
                      and req.get("pop") == "" and not self._pop_missing())
                pub = b64d(req["client_pub"]) if ok else b""
                ok = ok and len(pub) == 32
            except Exception:  # noqa: BLE001 - any malformed input is just bad_session
                ok = False
            if not ok:
                self._fail("bad_session", False)
                return None
            self._drop()
            priv = X25519PrivateKey.generate()
            shared = priv.exchange(X25519PublicKey.from_public_bytes(pub))
            salt = os.urandom(16)
            self._key = HKDF(hashes.SHA256(), 32, salt, KDF_INFO + self._pop.encode()).derive(shared)
            self.state, self.code = "session", "ok"
            self.stats["sessions"] += 1
            board_pub = priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
            return json.dumps({"board_pub": b64e(board_pub), "salt": b64e(salt)}, separators=(",", ":")).encode()

    def write_data(self, d: bytes) -> bytes:
        """Returns the 3-byte chunk ack [seq u16 BE][status u8]; 0 ok, 1 out-of-order/bad, 2 too large."""
        with self.lock:
            self.tick()
            seq = struct.unpack(">H", d[:2])[0] if len(d) >= 2 else 0
            def ack(status): return struct.pack(">HB", seq, status)
            if not self.advertising:
                self.code = "window_closed"
                return ack(1)
            if self._key is None:
                self.code = "bad_session"
                return ack(1)
            if len(d) < 4 or d[3] != len(d) - 4:
                self._fail("bad_chunk", False)
                return ack(1)
            flags, payload = d[2], d[4:]
            if flags & 1:
                if seq != 0:
                    self.code = "bad_chunk"
                    return ack(1)
                self._blob, self._next_seq, self.state = bytearray(), 0, "receiving"
            elif self.state != "receiving":
                self.code = "bad_chunk"
                return ack(1)
            if seq != self._next_seq:
                self.code = "bad_chunk"
                return ack(1)
            if len(self._blob) + len(payload) > MAX_BLOB:
                self._fail("bad_chunk", False)
                return ack(2)
            self._blob += payload
            self._next_seq += 1
            self._last_chunk = self._now()
            self.code = "ok"
            if flags & 2:
                self._finish()
            return ack(0)

    def _finish(self):
        blob, self._blob = bytes(self._blob), bytearray()
        key = self._key
        if len(blob) < 12 + 16 + 2:
            return self._fail("bad_bundle", True)
        try:
            plain = AESGCM(key).decrypt(blob[:12], blob[12:], self.node_id.encode())
        except InvalidTag:
            self.stats["bad_bundle"] += 1
            return self._fail("bad_bundle", True)          # wrong PoP / tampering: counts as a guess
        try:
            cfg = parse_bundle(plain, self.node_id)
        except ValueError as e:                              # authenticated sender, bad content: not a guess
            log.warning("%s: bundle rejected (%s)", self.node_id, e)
            return self._fail("bad_bundle", False)
        self.config = cfg
        self.has_creds = True
        self._drop()
        self.state, self.code = "stored", "ok"
        self._stored_pending = True
        self.stats["stored"] += 1
        log.info("%s: BLE provisioning stored a config; BLE off", self.node_id)

    def client_disconnected(self):
        with self.lock:
            self._drop()
            if self.state in ("session", "receiving"):
                self.state = "open"


def parse_bundle(plain: bytes, node_id: str) -> dict:
    """Same checks as parseBundle() in firmware: raises ValueError naming a field, never a value."""
    if len(plain) > MAX_BUNDLE:
        raise ValueError("size")
    try:
        d = json.loads(plain.decode("utf-8"))
        assert d["v"] == 1
        wifi, mq = d["wifi"], d["mqtt"]
        out = {"wifi_ssid": wifi["ssid"], "wifi_pass": wifi["password"], "mqtt_host": mq["host"],
               "mqtt_port": int(mq.get("port", 8883)), "mqtt_user": mq["username"], "mqtt_pass": mq["password"],
               "ntp_host": d.get("ntp_host", ""), "ca_pem": d["ca_pem"]}
    except Exception as e:  # noqa: BLE001
        raise ValueError("format") from e
    if not all(isinstance(out[k], str) for k in ("wifi_ssid", "wifi_pass", "mqtt_host", "mqtt_user", "mqtt_pass", "ntp_host", "ca_pem")):
        raise ValueError("type")
    if out["mqtt_user"] != node_id:
        raise ValueError("mqtt_user")
    if not 1 <= len(out["wifi_ssid"]) <= 32:
        raise ValueError("wifi_ssid")
    if not 1 <= out["mqtt_port"] <= 65535:
        raise ValueError("mqtt_port")
    if not out["mqtt_host"] or not out["mqtt_pass"]:
        raise ValueError("mqtt")
    ca = out["ca_pem"]
    if not ca.startswith("-----BEGIN CERTIFICATE-----") or "PRIVATE KEY" in ca:
        raise ValueError("ca")
    return out


# ------------------------------------------------------------------------------------------------ client (the app)
class ProvisionError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


class LocalPeer:
    """In-process 'radio' to a BleBoard."""

    def __init__(self, board: BleBoard):
        self.board = board

    def read(self, char):
        return self.board.read(char)

    def write_session(self, data):
        return self.board.write_session(data)

    def write_data(self, data):
        return self.board.write_data(data)

    def disconnect(self):
        self.board.client_disconnected()


class TcpPeer:
    """Client end of `serve_board` (loopback JSON lines): stands in for flutter_blue_plus / Web Bluetooth."""

    def __init__(self, host: str, port: int, timeout=10):
        self.s = socket.create_connection((host, port), timeout=timeout)
        self.f = self.s.makefile("rwb")

    def _rpc(self, obj):
        self.f.write(json.dumps(obj).encode() + b"\n")
        self.f.flush()
        line = self.f.readline()
        if not line:
            raise ConnectionError("peer closed")
        return json.loads(line)

    def read(self, char):
        return b64d(self._rpc({"op": "read", "char": char})["value"])

    def write_session(self, data):
        r = self._rpc({"op": "write", "char": SESSION, "value": b64e(data)})
        return b64d(r["indicate"]) if r.get("indicate") else None

    def write_data(self, data):
        return b64d(self._rpc({"op": "write", "char": DATA, "value": b64e(data)})["notify"])

    def disconnect(self):
        try:
            self._rpc({"op": "disconnect"})
        except Exception:  # noqa: BLE001
            pass
        self.s.close()


class BleClient:
    """What the provisioning app does (contracts/ble-provisioning.md, Sequence 2-4)."""

    def __init__(self, peer, pop: str):
        self.peer, self.pop = peer, pop or ""

    def info(self) -> dict:
        return json.loads(self.peer.read(INFO))

    def result(self) -> dict:
        return json.loads(self.peer.read(RESULT))

    def provision(self, bundle: dict, node_id: str, chunk: int = 180) -> dict:
        """Handshake, encrypt, send. Raises ProvisionError(code) on any failure. The PoP stays local."""
        priv = X25519PrivateKey.generate()
        pub = priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        reply = self.peer.write_session(json.dumps({"proto": 1, "client_pub": b64e(pub), "pop": ""}).encode())
        if reply is None:
            raise ProvisionError(self.result().get("code", "bad_session"))
        r = json.loads(reply)
        shared = priv.exchange(X25519PublicKey.from_public_bytes(b64d(r["board_pub"])))
        key = HKDF(hashes.SHA256(), 32, b64d(r["salt"]), KDF_INFO + self.pop.encode()).derive(shared)
        nonce = os.urandom(12)
        plain = json.dumps(bundle, separators=(",", ":")).encode()
        if len(plain) > MAX_BUNDLE:
            raise ProvisionError("bad_bundle")
        blob = nonce + AESGCM(key).encrypt(nonce, plain, node_id.encode())
        n_chunks = (len(blob) + chunk - 1) // chunk
        for i in range(n_chunks):
            part = blob[i * chunk:(i + 1) * chunk]
            flags = (1 if i == 0 else 0) | (2 if i == n_chunks - 1 else 0)
            ack = self.peer.write_data(struct.pack(">HBB", i, flags, len(part)) + part)
            if len(ack) != 3 or ack[2] != 0:
                raise ProvisionError(self.result().get("code", "bad_chunk"))
        res = self.result()
        if res.get("state") != "stored":
            raise ProvisionError(res.get("code", "bad_bundle"))
        return res


# ------------------------------------------------------------------------------------------------ fake GATT server
def serve_board(board: BleBoard, host: str = "127.0.0.1", port: int = 0):
    """Loopback-only fake GATT server. Returns (server, port). One client at a time is enough for the tests."""
    if host not in ("127.0.0.1", "::1", "localhost"):
        raise ValueError("the fake GATT peer only listens on loopback")

    class H(socketserver.StreamRequestHandler):
        def handle(self):
            try:
                for line in self.rfile:
                    req = json.loads(line)
                    op = req.get("op")
                    if op == "read":
                        out = {"value": b64e(board.read(req["char"]))}
                    elif op == "write" and req.get("char") == SESSION:
                        r = board.write_session(b64d(req["value"]))
                        out = {"indicate": b64e(r) if r is not None else None}
                    elif op == "write" and req.get("char") == DATA:
                        out = {"notify": b64e(board.write_data(b64d(req["value"])))}
                    elif op == "disconnect":
                        board.client_disconnected()
                        out = {}
                    else:
                        out = {"error": "bad_op"}
                    self.wfile.write(json.dumps(out).encode() + b"\n")
                    self.wfile.flush()
            except (ConnectionError, ValueError, KeyError):
                pass
            finally:
                board.client_disconnected()

    class S(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    srv = S((host, port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]
