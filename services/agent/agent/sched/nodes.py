"""Real vision-node / yolo-node clients behind `PerceptionBackend` (contracts/openapi/vision-node-internal.yaml 1.0.0 and
yolo-node-internal.yaml 1.0.0).

Rules this module enforces (CLAUDE.md 4, 6, 10, 11):
* One secret per node, read from a file (never env/argv), sent only in `X-Service-Secret`, never logged (log lines carry the
  node name, the source and a fixed reason code, nothing from the request or the response). Redirects are not followed.
* Images go out as a multipart upload and live in RAM only; nothing here writes, caches or logs bytes, the question or an answer.
* EVERYTHING a node returns is untrusted data. Responses are re-validated by hand against the closed contract schemas (unknown
  field, wrong type, out-of-range number, label or text outside the allowlist: the whole response is dropped, the source counts
  as failed). YOLO answer text is built HERE from validated detections, using only labels from the fixed COCO class table;
  node free text is never copied into it. Short VLM text is shown to the user as a quoted, labelled data block, and the
  vlm_yesno answer is the closed form yes|no|unsure.
* 429 `queue_full` (and an unusable snapshot source) is "busy": the ladder moves up, the circuit breaker is NOT tripped.
* Snapshot calls (no image on the wire, the node grabs a camera frame) need an admin or member, a configured source and a room
  inside the caller's scope; a guest never gets one and any doubt means no snapshot (fail closed).
Nothing a node returns can name a device, scene or tool: the five runtime tools are untouched."""
from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
from collections import Counter
from dataclasses import dataclass

import httpx

from ..scope import Scope
from ..util import clean_label, read_secret_file, register_secret
from .images import NodeImage, SnapshotRef
from .router import BackendAnswer
from .text import normalise

log = logging.getLogger("agent.sched.nodes")

MAX_RESPONSE_BYTES = 64 * 1024
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MEDIA_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})

_SAFE_TEXT = re.compile(r"[A-Za-z0-9 .,;:!?'()%/_+-]*")
_LABEL = re.compile(r"[A-Za-z0-9 _-]{1,32}")
_MODEL = re.compile(r"[A-Za-z0-9._-]{1,48}")
_REF = re.compile(r"[A-Za-z0-9_-]{1,64}")
_SOURCE_ID = re.compile(r"[A-Za-z0-9_-]{1,32}")
_ENUM_VALUE = re.compile(r"[a-z0-9][a-z0-9 _-]{0,31}")
_CTRL = re.compile(r"[\x00-\x1f\x7f]+")
ERROR_CODES = frozenset({"bad_request", "unauthorized", "payload_too_large", "unsupported_media_type", "bad_image", "bad_audio",
                         "queue_full", "model_error", "schema_violation", "vision_unavailable", "timeout", "unknown_source",
                         "source_offline"})

# The 80 COCO classes (YOLOv8n's fixed class table). Only these labels are ever echoed into an answer.
COCO = ("person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck", "boat", "traffic light", "fire hydrant",
        "stop sign", "parking meter", "bench", "bird", "cat", "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra", "giraffe",
        "backpack", "umbrella", "handbag", "tie", "suitcase", "frisbee", "skis", "snowboard", "sports ball", "kite",
        "baseball bat", "baseball glove", "skateboard", "surfboard", "tennis racket", "bottle", "wine glass", "cup", "fork", "knife",
        "spoon", "bowl", "banana", "apple", "sandwich", "orange", "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair",
        "couch", "potted plant", "bed", "dining table", "toilet", "tv", "laptop", "mouse", "remote", "keyboard", "cell phone",
        "microwave", "oven", "toaster", "sink", "refrigerator", "book", "clock", "vase", "scissors", "teddy bear", "hair drier",
        "toothbrush")
_COCO_SET = frozenset(COCO)
# question word -> COCO class
_TARGETS = {"person": "person", "people": "person", "someone": "person", "anyone": "person", "anybody": "person",
            "somebody": "person", "man": "person", "woman": "person", "kid": "person", "child": "person",
            "intruder": "person", "dog": "dog", "cat": "cat", "car": "car", "bicycle": "bicycle", "bike": "bicycle",
            "bird": "bird"}
_PLURAL = {"person": "people", "mouse": "mice", "sheep": "sheep", "skis": "skis", "scissors": "scissors", "broccoli": "broccoli"}

# vlm_json: one fixed structural schema (BoundedSchema), no free-text slot that could carry an instruction
_OBJECT_ENUM = ["person", "dog", "cat", "car", "package", "bicycle", "bird", "none"]
JSON_SCHEMA = {"type": "object", "required": ["person_present", "people_count", "objects", "scene"], "properties": {
    "person_present": {"type": "boolean"},
    "people_count": {"type": "integer", "minimum": 0, "maximum": 20},
    "objects": {"type": "array", "maxItems": 8, "items": {"type": "string", "enum": _OBJECT_ENUM}},
    "scene": {"type": "string", "maxLength": 48}}}


class Invalid(ValueError):
    """A node response that does not satisfy the contract. The message is fixed text, never node content."""


# ------------------------------------------------------------------------------------------------ response validation
def _closed(obj: object, required: tuple, optional: tuple = ()) -> dict:
    if not isinstance(obj, dict):
        raise Invalid("not an object")
    keys = set(obj)
    if not set(required) <= keys or not keys <= set(required) | set(optional):
        raise Invalid("unexpected or missing fields")
    return obj


def _num(v: object, lo: float, hi: float, integer: bool = False) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or (integer and not isinstance(v, int)):
        raise Invalid("bad number")
    if not math.isfinite(v) or not lo <= v <= hi:
        raise Invalid("number out of range")
    return v


def _str(v: object, pattern: re.Pattern, maxlen: int, minlen: int = 0) -> str:
    if not isinstance(v, str) or not minlen <= len(v) <= maxlen or not pattern.fullmatch(v):
        raise Invalid("bad string")
    return v


@dataclass(frozen=True)
class Meta:
    latency_ms: float
    queue_ms: float
    cached: bool = False


def _meta(obj: dict, sent_ref: str | None, cached: bool = False) -> Meta:
    lat, q = _num(obj["latency_ms"], 0, 120000), _num(obj["queue_ms"], 0, 120000)
    if "image_ref" in obj and (_str(obj["image_ref"], _REF, 64) != sent_ref):       # echoed back unchanged, or it is not our answer
        raise Invalid("image_ref mismatch")
    if cached and not isinstance(obj["cached"], bool):
        raise Invalid("bad cached")
    return Meta(float(lat), float(q), bool(obj.get("cached", False)) if cached else False)


def _bool(v: object) -> bool:
    if not isinstance(v, bool):
        raise Invalid("bad boolean")
    return v


def validate_yesno(obj: object, ref: str | None) -> tuple[str, Meta]:
    o = _closed(obj, ("answer", "latency_ms", "queue_ms", "cached"), ("image_ref",))
    if o["answer"] not in ("yes", "no", "unsure"):
        raise Invalid("bad answer")
    _bool(o["cached"])
    return o["answer"], _meta(o, ref, cached=True)


def validate_short(obj: object, ref: str | None) -> tuple[str, Meta]:
    o = _closed(obj, ("text", "truncated", "latency_ms", "queue_ms", "cached"), ("image_ref",))
    text = _str(o["text"], _SAFE_TEXT, 240)
    _bool(o["truncated"]), _bool(o["cached"])
    return text, _meta(o, ref, cached=True)


def validate_json(obj: object, ref: str | None) -> tuple[dict, Meta]:
    """Validated against the generic JsonValue rules AND the exact schema we sent (JSON_SCHEMA)."""
    o = _closed(obj, ("data", "latency_ms", "queue_ms", "cached"), ("image_ref",))
    data = o["data"]
    if not isinstance(data, dict) or len(data) > 16:
        raise Invalid("bad data")
    props = JSON_SCHEMA["properties"]
    if set(data) != set(props):
        raise Invalid("data does not match the requested schema")
    out: dict = {}
    for k, v in data.items():
        _str(k, re.compile(r"[a-z][a-z0-9_]{0,31}"), 32, 1)
        if k == "person_present":
            out[k] = _bool(v)
        elif k == "people_count":
            out[k] = int(_num(v, 0, 20, integer=True))
        elif k == "objects":
            if not isinstance(v, list) or len(v) > 8 or any(x not in _OBJECT_ENUM for x in v):
                raise Invalid("bad objects")
            out[k] = list(v)
        else:
            out[k] = _str(v, _SAFE_TEXT, 48)
    _bool(o["cached"])
    return out, _meta(o, ref, cached=True)


def validate_detections(items: object) -> list[tuple[str, float]]:
    if not isinstance(items, list) or len(items) > 50:
        raise Invalid("bad detections")
    out = []
    for d in items:
        d = _closed(d, ("label", "conf", "bbox"))
        label = _str(d["label"], _LABEL, 32, 1)
        conf = float(_num(d["conf"], 0, 1))
        bb = d["bbox"]
        if not isinstance(bb, list) or len(bb) != 4:
            raise Invalid("bad bbox")
        for x in bb:
            _num(x, 0, 1e9)
        out.append((label, conf))
    return out


_DETECT_KEYS = ("detections", "image_width", "image_height", "model", "latency_ms", "queue_ms")


def validate_detect(obj: object, ref: str | None) -> tuple[list[tuple[str, float]], Meta]:
    o = _closed(obj, _DETECT_KEYS, ("image_ref",))
    _num(o["image_width"], 1, 4096, True), _num(o["image_height"], 1, 4096, True)
    _str(o["model"], _MODEL, 48, 1)
    return validate_detections(o["detections"]), _meta(o, ref)


def validate_snapshot(obj: object, source_id: str) -> tuple[list[tuple[str, float]], Meta]:
    o = _closed(obj, ("source_id",) + _DETECT_KEYS + ("frame_age_ms",))
    if _str(o["source_id"], _SOURCE_ID, 32, 1) != source_id:
        raise Invalid("source mismatch")
    _num(o["image_width"], 1, 4096, True), _num(o["image_height"], 1, 4096, True), _num(o["frame_age_ms"], 0, 60000)
    _str(o["model"], _MODEL, 48, 1)
    return validate_detections(o["detections"]), _meta(o, None)


def validate_error(obj: object) -> str | None:
    """The error code of a contract-conforming error body, else None. Only the code is ever used (never `message`)."""
    try:
        o = _closed(obj, ("error", "message"))
        _str(o["message"], re.compile(r".*", re.S), 120)
        return o["error"] if o["error"] in ERROR_CODES else None
    except Invalid:
        return None


# ------------------------------------------------------------------------------------------------ answer text (ours)
def _plural(label: str, n: int) -> str:
    if n == 1:
        return label
    if label in _PLURAL:
        return _PLURAL[label]
    return label + ("es" if label.endswith(("s", "sh", "ch", "x")) else "s")


def _count_phrase(label: str, n: int) -> str:
    return f"{n} {_plural(label, n)}"


def question_targets(question: str) -> list[str]:
    toks = normalise(question).split()
    hits = (_TARGETS.get(t) or _TARGETS.get(t.rstrip("s")) for t in toks)      # "dogs" -> dog
    return list(dict.fromkeys(h for h in hits if h))


def detection_answer(question: str, dets: list[tuple[str, float]], min_conf: float = 0.25) -> str:
    """Answer text from VALIDATED detections only. Labels outside the COCO table are counted as 'other objects', never echoed."""
    known = Counter(l for l, c in dets if c >= min_conf and l in _COCO_SET)
    other = sum(1 for l, c in dets if c >= min_conf and l not in _COCO_SET)
    targets = question_targets(question)
    if targets:
        found = [(t, known[t]) for t in targets if known[t]]
        if found:
            return "Yes, I see " + " and ".join(_count_phrase(t, n) for t, n in found) + "."
        return "No, I don't see " + " or ".join(("a " + t) if t != "person" else "a person" for t in targets) + "."
    parts = [_count_phrase(l, n) for l, n in known.most_common(6)]
    if other:
        parts.append(_count_phrase("other object", other))
    if not parts:
        return "I don't see any objects."
    return "I see " + (", ".join(parts[:-1]) + " and " + parts[-1] if len(parts) > 1 else parts[0]) + "."


def _yesno_text(answer: str) -> str:
    return {"yes": "Yes.", "no": "No.", "unsure": "I'm not sure."}[answer]


def _short_text(text: str) -> str:
    return f'The camera view (unverified description): "{text.strip()}"'


def _json_text(d: dict) -> str:
    n = d["people_count"]
    parts = ["Someone is in view" if d["person_present"] else "No one is in view"]
    if n:
        parts.append(f"people counted: {n}")
    objs = [o for o in d["objects"] if o != "none"]
    if objs:
        parts.append("objects: " + ", ".join(dict.fromkeys(objs)))
    scene = d["scene"].strip()
    if scene:
        parts.append(f'scene (unverified): "{scene}"')
    return "; ".join(parts) + "."


def _render(validated: tuple, text_of) -> tuple[str, Meta]:
    value, meta = validated
    return text_of(value), meta


def _question(text: str) -> str:
    return _CTRL.sub(" ", text or "").strip()[:300].strip()


# ------------------------------------------------------------------------------------------------ transport
class _NodeClient:
    name = "node"

    def __init__(self, base_url: str, secret: str, timeout_s: float = 30.0, http: httpx.AsyncClient | None = None,
                 ca_file: str | None = None, min_conf: float = 0.25) -> None:
        if not secret:
            raise ValueError("a node secret is required")
        register_secret(secret)                                 # so the log redactor knows it even if it never came from a file
        self._base, self._secret, self.timeout_s, self.min_conf = base_url.rstrip("/"), secret, float(timeout_s), min_conf
        self._own = http is None
        self._http = http or httpx.AsyncClient(timeout=httpx.Timeout(timeout_s), verify=ca_file or True, follow_redirects=False)

    def __repr__(self) -> str:                                  # never the secret
        return f"<{type(self).__name__} {self._base}>"

    @property
    def node_timeout_ms(self) -> int:
        return int(min(25000, max(1000, self.timeout_s * 1000 - 1000)))

    async def aclose(self) -> None:
        if self._own:
            await self._http.aclose()

    def supports(self, source: str) -> bool:
        return False

    async def _send(self, path: str, params: dict, image: NodeImage | None) -> tuple[int, bytes]:
        headers = {"X-Service-Secret": self._secret, "Accept": "application/json"}
        url, to = self._base + path, httpx.Timeout(self.timeout_s)
        if image is None:
            req = self._http.build_request("POST", url, json=params, headers=headers, timeout=to)
        else:
            files = {"params": (None, json.dumps(params, separators=(",", ":")).encode(), "application/json"),
                     "image": ("image", image.data, image.media_type)}        # a generic filename: nothing of the caller's
            req = self._http.build_request("POST", url, files=files, headers=headers, timeout=to)
        resp = await self._http.send(req, stream=True)
        try:
            buf = bytearray()
            async for chunk in resp.aiter_bytes():
                buf += chunk
                if len(buf) > MAX_RESPONSE_BYTES:
                    raise Invalid("response too large")
            return resp.status_code, bytes(buf)
        finally:
            await resp.aclose()

    def _fail(self, source: str, reason: str, t0: float, busy: bool = False) -> BackendAnswer:
        # fixed strings only: node, source, reason code. No request or response content, no URL, no header.
        log.warning("node %s %s %s: %s", self.name, source, "busy" if busy else "failed", reason)
        return BackendAnswer(False, "", 0.0, busy=busy, reason=reason)

    async def _call(self, source: str, path: str, params: dict, image: NodeImage | None, parse, snapshot: bool = False) -> BackendAnswer:
        """POST, then `parse(obj) -> (text, Meta)`. Every failure mode is a BackendAnswer(ok=False), never an exception."""
        t0 = time.monotonic()
        try:
            status, raw = await asyncio.wait_for(self._send(path, params, image), self.timeout_s + 1)
        except (asyncio.TimeoutError, httpx.TimeoutException):
            return self._fail(source, "timeout", t0)
        except Invalid:
            return self._fail(source, "response_too_large", t0)
        except httpx.HTTPError:
            return self._fail(source, "unreachable", t0)
        if status == 429:
            return self._fail(source, "queue_full", t0, busy=True)
        if status != 200:
            try:
                code = validate_error(json.loads(raw))
            except (ValueError, RecursionError):
                code = None
            if snapshot and status in (404, 409) and code in ("unknown_source", "source_offline"):
                return self._fail(source, code, t0, busy=True)           # the camera, not the node, is the problem
            return self._fail(source, code or f"http_{status}", t0)
        try:
            text, meta = parse(json.loads(raw))
        except (ValueError, RecursionError):                              # Invalid is a ValueError
            return self._fail(source, "invalid_response", t0)
        # Cost table input: the node's own processing time (latency_ms minus the queue wait, per the contract), or, for a
        # cached answer (which says nothing about inference cost), our measured round trip.
        ms = (time.monotonic() - t0) * 1000 if meta.cached else max(0.0, meta.latency_ms - meta.queue_ms)
        return BackendAnswer(True, text, ms)

    @staticmethod
    def _first_image(images: list) -> NodeImage | None:
        img = next((i for i in images if isinstance(i, NodeImage)), None)
        if img is None or img.media_type not in MEDIA_TYPES or not 0 < len(img.data) <= MAX_IMAGE_BYTES or not _REF.fullmatch(img.ref):
            return None
        return img


class VisionNodeClient(_NodeClient):
    """pi-vision (port 8300): vlm_yesno, vlm_short, vlm_json and the YOLO fallback `yolo_pi` (POST /v1/detect)."""
    name = "vision"
    SOURCES = frozenset({"vlm_yesno", "vlm_short", "vlm_json", "yolo_pi"})

    def supports(self, source: str) -> bool:
        return source in self.SOURCES

    async def answer(self, source: str, question: str, images: list) -> BackendAnswer:
        img, q = self._first_image(images), _question(question)
        if source not in self.SOURCES:
            return BackendAnswer(False, reason="unsupported", busy=True)
        if img is None or not q:
            return BackendAnswer(False, reason="no_usable_image", busy=True)      # nothing was sent; not the node's fault
        base = {"image_ref": img.ref, "timeout_ms": self.node_timeout_ms}
        if source == "vlm_yesno":
            return await self._call(source, "/v1/vlm/yesno", {"question": q, **base}, img,
                                    lambda o: _render(validate_yesno(o, img.ref), _yesno_text))
        if source == "vlm_short":
            return await self._call(source, "/v1/vlm/short", {"question": q, "max_tokens": 48, **base}, img,
                                    lambda o: _render(validate_short(o, img.ref), _short_text))
        if source == "vlm_json":
            return await self._call(source, "/v1/vlm/json", {"question": q, "schema": JSON_SCHEMA, "max_tokens": 160, **base}, img,
                                    lambda o: _render(validate_json(o, img.ref), _json_text))
        params = {"min_conf": self.min_conf, "max_detections": 20, **base}
        return await self._call(source, "/v1/detect", params, img,
                                lambda o: _render(validate_detect(o, img.ref), lambda d: detection_answer(q, d, self.min_conf)))


@dataclass(frozen=True)
class SnapshotSource:
    source_id: str
    room: str                                  # cleaned label, compared with the caller's scope rooms


class YoloNodeClient(_NodeClient):
    """jetson-yolo (port 8400): yolo_jetson via POST /v1/detect (uploaded image) or /v1/detect/snapshot (the node's camera)."""
    name = "yolo"

    def __init__(self, *a, snapshot_sources: list[dict] | tuple = (), **kw) -> None:
        super().__init__(*a, **kw)
        self.sources = tuple(SnapshotSource(s["source_id"], clean_label(s["room"], 48)) for s in snapshot_sources)

    def supports(self, source: str) -> bool:
        return source == "yolo_jetson"

    def snapshot_source(self, role: str, scope: Scope, question: str) -> SnapshotRef | None:
        """The one camera this caller may be shown for this question, or None. Fail closed: guests and unknown roles never;
        the camera's room must be inside the scope; if the question names a room, only that room's camera qualifies (a camera
        the caller cannot see is never swapped in); with no room named, only a node with a single configured camera answers."""
        if role not in ("admin", "member") or not self.sources:
            return None
        q = " " + normalise(question) + " "
        named = [s for s in self.sources if s.room and f" {normalise(s.room)} " in q]
        pool = named if named else (list(self.sources) if len(self.sources) == 1 else [])
        if len(pool) != 1 or not scope.room_ok(pool[0].room):         # none, ambiguous, or a room the caller may not see
            return None
        return SnapshotRef(pool[0].source_id)

    async def answer(self, source: str, question: str, images: list) -> BackendAnswer:
        if source != "yolo_jetson":
            return BackendAnswer(False, reason="unsupported", busy=True)
        q, img = _question(question), self._first_image(images)
        snap = next((i for i in images if isinstance(i, SnapshotRef)), None) if img is None else None
        if img is None and snap is None:
            return BackendAnswer(False, reason="no_usable_image", busy=True)
        if snap is not None:
            if not _SOURCE_ID.fullmatch(snap.source_id) or snap.source_id not in {s.source_id for s in self.sources}:
                return BackendAnswer(False, reason="unknown_source", busy=True)
            params = {"source_id": snap.source_id, "min_conf": self.min_conf, "max_detections": 20, "timeout_ms": self.node_timeout_ms}
            return await self._call(source, "/v1/detect/snapshot", params, None,
                                    lambda o: _render(validate_snapshot(o, snap.source_id), lambda d: detection_answer(q, d, self.min_conf)),
                                    snapshot=True)
        params = {"min_conf": self.min_conf, "max_detections": 20, "image_ref": img.ref, "timeout_ms": self.node_timeout_ms}
        return await self._call(source, "/v1/detect", params, img,
                                lambda o: _render(validate_detect(o, img.ref), lambda d: detection_answer(q, d, self.min_conf)))


# ------------------------------------------------------------------------------------------------ construction
def build_backends(nodes: dict, http: httpx.AsyncClient | None = None) -> list:
    """Clients for the nodes that are enabled in `SchedConfig.nodes` (all disabled by default). A secret file that cannot be
    read disables that node (perception is optional) and logs the node name only, never the path's content."""
    out: list = []
    for key, cls in (("vision", VisionNodeClient), ("yolo", YoloNodeClient)):
        cfg = nodes.get(key) or {}
        if not cfg.get("enabled"):
            continue
        try:
            secret = read_secret_file(cfg["secret_file"])
        except (OSError, ValueError):
            log.error("node %s is enabled but its secret file cannot be read: node disabled", key)
            continue
        kw = {"snapshot_sources": cfg.get("snapshot_sources", [])} if key == "yolo" else {}
        out.append(cls(cfg["base_url"], secret, cfg.get("timeout_s", 30.0), http, cfg.get("ca_file"), cfg.get("min_conf", 0.25), **kw))
    return out
