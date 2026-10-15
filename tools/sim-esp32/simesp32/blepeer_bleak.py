"""OPTIONAL: drive a REAL ESP32 from a PC with Bluetooth (`pip install bleak` into the sim .venv; not in requirements.txt).
Same peer interface as ble.LocalPeer/TcpPeer, so `simesp32.provclient --bleak "Setup-E5F6"` runs the exact same client
code against hardware. UNVERIFIED: written for the first hardware test, never run in this repo (no radio here)."""
import asyncio
import threading

from .ble import DATA, INFO, RESULT, SESSION

SVC = "4dd57997-6f7e-4f0a-b1d5-e01ef6674efe"
UUIDS = {INFO: "9809777c-4f37-44e6-b054-b3fb7ee9f6db", SESSION: "8583a1b2-4f51-48cc-8270-35f88e7b7260",
         DATA: "c5a63a5d-8766-4e3b-b2b2-eab7c732361b", RESULT: "43f00218-77e0-4e21-9eb2-56da36658cee"}


class BleakPeer:
    def __init__(self, name: str, scan_s: float = 10.0):
        from bleak import BleakClient, BleakScanner
        self._loop = asyncio.new_event_loop()
        threading.Thread(target=self._loop.run_forever, daemon=True).start()
        dev = self._run(BleakScanner.find_device_by_name(name, timeout=scan_s))
        if dev is None:
            raise ConnectionError("board not found (is the provisioning window open?)")
        self._c = BleakClient(dev)
        self._run(self._c.connect())
        self._q = {SESSION: asyncio.Queue(), DATA: asyncio.Queue()}
        for ch in (SESSION, DATA):
            self._run(self._c.start_notify(UUIDS[ch], self._cb(ch)))

    def _run(self, coro, timeout=20):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)

    def _cb(self, ch):
        def cb(_s, data):
            self._loop.call_soon_threadsafe(self._q[ch].put_nowait, bytes(data))
        return cb

    def read(self, char):
        return bytes(self._run(self._c.read_gatt_char(UUIDS[char])))

    def write_session(self, data):
        self._run(self._c.write_gatt_char(UUIDS[SESSION], data, response=True))
        try:
            return self._run(asyncio.wait_for(self._q[SESSION].get(), 5))
        except Exception:  # noqa: BLE001 - no indication: the board refused (result says why)
            return None

    def write_data(self, data):
        self._run(self._c.write_gatt_char(UUIDS[DATA], data, response=False))
        return self._run(asyncio.wait_for(self._q[DATA].get(), 5))

    def disconnect(self):
        try:
            self._run(self._c.disconnect())
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
