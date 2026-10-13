"""MQTT transport abstraction. PahoTransport talks to the real broker over TLS; mock.FakeClient is the in-memory twin.

Safety rule baked in: publishing while disconnected raises instead of queueing, so a command can never be delivered
late (the hub never queues commands, see mqtt.md).
"""
from __future__ import annotations

import logging
import ssl
import threading
from typing import Callable, Protocol

from .util import SECRETS

log = logging.getLogger("device_hub.transport")

MessageHandler = Callable[[str, bytes, bool], None]


class TransportDown(Exception):
    pass


class Transport(Protocol):
    connected: bool
    connect_error: str | None

    def start(self) -> None: ...
    def stop(self) -> None: ...
    def publish(self, topic: str, payload: bytes | str, qos: int = 1, retain: bool = False) -> None: ...
    def subscribe(self, topic: str, qos: int = 1) -> None: ...
    def add_message_handler(self, fn: MessageHandler) -> None: ...
    def wait_connected(self, timeout: float) -> bool: ...


class PahoTransport:
    def __init__(self, host: str, port: int, username: str, password: str, client_id: str,
                 ca_file: str | None = None, tls: bool = True, keepalive: int = 30,
                 will: tuple[str, bytes | str, int, bool] | None = None) -> None:
        import paho.mqtt.client as mqtt

        self._mqtt = mqtt
        SECRETS.add(password)
        self.host, self.port, self.keepalive = host, port, keepalive
        self.connected = False
        self.connect_error: str | None = None
        self._ev = threading.Event()
        self._handlers: list[MessageHandler] = []
        self._subs: dict[str, int] = {}
        self._lock = threading.Lock()
        # NOTE: never call enable_logger(): paho's debug log would print dynsec/provisioning payloads.
        self._c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id, protocol=mqtt.MQTTv311)
        self._c.username_pw_set(username, password)
        if tls:
            ctx = ssl.create_default_context(cafile=ca_file)
            self._c.tls_set_context(ctx)
        if will:
            t, p, q, r = will
            self._c.will_set(t, p, q, r)
        self._c.reconnect_delay_set(min_delay=1, max_delay=30)
        self._c.on_connect = self._on_connect
        self._c.on_disconnect = self._on_disconnect
        self._c.on_message = self._on_message

    # -- paho callbacks (network thread)
    def _on_connect(self, client, userdata, flags, reason_code, properties=None):  # noqa: ANN001
        if getattr(reason_code, "is_failure", False) or reason_code != 0:
            self.connected = False
            self.connect_error = str(reason_code)
            log.warning("mqtt connect refused: %s", self.connect_error)
            return
        self.connect_error = None
        with self._lock:
            subs = dict(self._subs)
        for topic, qos in subs.items():
            client.subscribe(topic, qos)
        self.connected = True
        self._ev.set()
        log.info("mqtt connected to %s:%s", self.host, self.port)

    def _on_disconnect(self, client, userdata, *args):  # noqa: ANN001, ANN002
        self.connected = False
        self._ev.clear()
        log.warning("mqtt disconnected")

    def _on_message(self, client, userdata, msg):  # noqa: ANN001
        for fn in list(self._handlers):
            try:
                fn(msg.topic, bytes(msg.payload), bool(msg.retain))
            except Exception:  # noqa: BLE001
                log.exception("mqtt message handler failed (topic=%s)", msg.topic)

    # -- Transport API
    def add_message_handler(self, fn: MessageHandler) -> None:
        self._handlers.append(fn)

    def start(self) -> None:
        self._c.connect_async(self.host, self.port, self.keepalive)
        self._c.loop_start()

    def stop(self) -> None:
        try:
            self._c.disconnect()
        finally:
            self._c.loop_stop()
        self.connected = False

    def publish(self, topic: str, payload: bytes | str, qos: int = 1, retain: bool = False) -> None:
        if not self.connected:
            raise TransportDown("mqtt not connected")
        info = self._c.publish(topic, payload, qos=qos, retain=retain)
        if info.rc != self._mqtt.MQTT_ERR_SUCCESS:
            raise TransportDown(f"publish failed rc={info.rc}")

    def subscribe(self, topic: str, qos: int = 1) -> None:
        with self._lock:
            self._subs[topic] = qos
        if self.connected:
            self._c.subscribe(topic, qos)

    def wait_connected(self, timeout: float) -> bool:
        return self._ev.wait(timeout) and self.connected
