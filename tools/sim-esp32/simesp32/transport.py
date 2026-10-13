"""Transports: a real MQTT/TLS one (paho) and an in-memory one for tests and --mock.

Both expose the same small surface the Node uses:
  start(), publish(topic, payload, qos, retain) -> bool, subscribe(topic, qos), drop(down_s), close()
and call node.on_connect() / node.on_disconnect() / node.on_message(topic, payload)."""
import logging
import socket
import ssl
import threading

log = logging.getLogger("sim.transport")


class PahoTransport:
    def __init__(self, node_id, host, port, ca_file, username, password, will_topic, keepalive=30):
        import paho.mqtt.client as mqtt

        self.node = None
        self.node_id = node_id
        self.host, self.port, self.keepalive = host, port, keepalive
        self._mqtt = mqtt
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=node_id, protocol=mqtt.MQTTv311,
                        clean_session=True)
        ctx = ssl.create_default_context(cafile=str(ca_file))   # verifies chain, dates and hostname
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        c.tls_set_context(ctx)
        c.username_pw_set(username, password)
        c.will_set(will_topic, payload="offline", qos=1, retain=True)
        c.reconnect_delay_set(min_delay=1, max_delay=30)
        c.on_connect = self._on_connect
        c.on_disconnect = self._on_disconnect
        c.on_message = self._on_message
        self.client = c
        self._connected = threading.Event()

    def start(self):
        self.client.connect_async(self.host, self.port, keepalive=self.keepalive)
        self.client.loop_start()

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            log.warning("%s: broker refused connection: %s", self.node_id, reason_code)
            return
        self._connected.set()
        self.client.reconnect_delay_set(min_delay=1, max_delay=30)
        self.node.on_connect()

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        self._connected.clear()
        if self.node:
            self.node.on_disconnect()

    def _on_message(self, client, userdata, msg):
        self.node.on_message(msg.topic, bytes(msg.payload))

    def publish(self, topic, payload, qos=1, retain=False) -> bool:
        if not self._connected.is_set():
            return False
        info = self.client.publish(topic, payload, qos=qos, retain=retain)
        return info.rc == self._mqtt.MQTT_ERR_SUCCESS

    def subscribe(self, topic, qos=1):
        self.client.subscribe(topic, qos=qos)

    def drop(self, down_s: float):
        """Kill the TCP connection without a DISCONNECT so the broker fires the LWT; paho reconnects after down_s."""
        self.client.reconnect_delay_set(min_delay=max(1, int(down_s)), max_delay=max(1, int(down_s)))
        sock = getattr(self.client, "_sock", None)
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def close(self):
        try:
            self.client.disconnect()
        finally:
            self.client.loop_stop()


class MemoryBus:
    """Records everything nodes publish and lets a test or --mock stdin inject commands."""

    def __init__(self, echo=None):
        self.published = []          # (node_id, topic, payload_bytes, qos, retain)
        self.retained = {}           # topic -> payload bytes
        self.echo = echo             # optional callable(line)
        self.transports = {}

    def record(self, node_id, topic, payload, qos, retain):
        self.published.append((node_id, topic, payload, qos, retain))
        if retain:
            self.retained[topic] = payload
        if self.echo:
            self.echo(f"{topic} {'R' if retain else '-'} {payload.decode('utf-8', 'replace')}")

    def inject(self, node_id, payload):
        data = payload if isinstance(payload, bytes) else (
            payload.encode() if isinstance(payload, str) else __import__("json").dumps(payload).encode())
        self.transports[node_id].deliver(f"home/v1/nodes/{node_id}/cmd", data)

    def on_topic(self, suffix, node_id=None):
        import json
        out = []
        for nid, topic, payload, _q, _r in self.published:
            if topic.endswith("/" + suffix) and (node_id is None or nid == node_id):
                try:
                    out.append(json.loads(payload))
                except ValueError:
                    out.append(payload)
        return out

    def raw(self, suffix, node_id=None):
        return [p for nid, t, p, _q, _r in self.published
                if t.endswith("/" + suffix) and (node_id is None or nid == node_id)]


class MemoryTransport:
    def __init__(self, node_id, bus: MemoryBus, clock, will_topic):
        self.node = None
        self.node_id = node_id
        self.bus = bus
        self.clock = clock
        self.will_topic = will_topic
        self.connected = False
        self.subscriptions = []
        self._reconnect_at = None
        bus.transports[node_id] = self

    def start(self):
        self._connect()

    def _connect(self):
        self.connected = True
        self._reconnect_at = None
        self.node.on_connect()

    def poll(self):
        if self._reconnect_at is not None and self.clock.monotonic() >= self._reconnect_at:
            self._connect()

    def publish(self, topic, payload, qos=1, retain=False) -> bool:
        if not self.connected:
            return False
        if isinstance(payload, str):
            payload = payload.encode()
        self.bus.record(self.node_id, topic, payload, qos, retain)
        return True

    def subscribe(self, topic, qos=1):
        self.subscriptions.append(topic)

    def deliver(self, topic, payload):
        if self.connected:
            self.node.on_message(topic, payload)

    def drop(self, down_s: float):
        was = self.connected
        self.connected = False
        self._reconnect_at = self.clock.monotonic() + down_s
        if was:
            self.bus.record(self.node_id, self.will_topic, b"offline", 1, True)   # broker fires the LWT
            self.node.on_disconnect()

    def close(self):
        self.connected = False
        self._reconnect_at = None
