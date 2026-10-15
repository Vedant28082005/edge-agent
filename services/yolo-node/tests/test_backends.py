"""Post-processing, the ONNX CPU backend on a tiny synthetic model, the TensorRT worker protocol (no GPU), sources."""
import io
import sys
import textwrap
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from conftest import image_bytes
from yolo_node.backends import make_backend
from yolo_node.backends.coco import COCO80
from yolo_node.backends.yolo_post import letterbox, postprocess
from yolo_node.config import Config
from yolo_node.errors import BackendError, NodeError


def fake_output(boxes, n=8400, nc=80):
    out = np.zeros((1, 4 + nc, n), dtype=np.float32)
    for i, (cx, cy, w, h, cls, conf) in enumerate(boxes):
        out[0, :4, i] = (cx, cy, w, h)
        out[0, 4 + cls, i] = conf
    return out


def test_letterbox_geometry():
    x, r, px, py = letterbox(np.zeros((480, 640, 3), np.uint8), 640)
    assert x.shape == (1, 3, 640, 640) and x.dtype == np.float32 and (r, px, py) == (1.0, 0, 80)


def test_postprocess_maps_back_nms_and_sorts():
    out = fake_output([(320, 320, 200, 400, 0, 0.9), (322, 322, 200, 400, 0, 0.8),   # duplicate -> NMS
                       (100, 300, 40, 40, 16, 0.6), (500, 300, 40, 40, 2, 0.1)])
    res = postprocess(out, COCO80, 1.0, 0, 80, 640, 480, 0.25)
    assert [r[0] for r in res] == ["person", "dog"]
    assert res[0][2] == pytest.approx([220, 40, 200, 400])
    assert postprocess(out, COCO80, 1.0, 0, 80, 640, 480, 0.95) == []


def test_postprocess_clips_to_image():
    res = postprocess(fake_output([(10, 400, 100, 100, 0, 0.9)]), COCO80, 1.0, 0, 80, 640, 480, 0.25)
    x, y, w, h = res[0][2]
    assert x == 0 and x + w <= 640 and y + h <= 480


def test_onnx_backend_on_synthetic_model(tmp_path):
    onnx = pytest.importorskip("onnx")
    pytest.importorskip("onnxruntime")
    from onnx import TensorProto, helper, numpy_helper
    const = numpy_helper.from_array(fake_output([(320, 320, 200, 400, 0, 0.9)], n=4), name="c")
    node = helper.make_node("Identity", ["c"], ["output0"])
    g = helper.make_graph([node], "g",
                          [helper.make_tensor_value_info("images", TensorProto.FLOAT, [1, 3, 640, 640])],
                          [helper.make_tensor_value_info("output0", TensorProto.FLOAT, [1, 84, 4])], [const])
    m = helper.make_model(g, opset_imports=[helper.make_opsetid("", 12)])
    m.ir_version = 8
    path = tmp_path / "tiny.onnx"
    onnx.save(m, str(path))

    from yolo_node.backends.onnx_backend import OnnxBackend
    b = OnnxBackend(str(path), COCO80, "yolov8n", 640, 1)
    assert not b.engine_loaded
    with pytest.raises(BackendError):
        b.infer(np.zeros((480, 640, 3), np.uint8), 0.25, 50)
    b.load()
    assert b.engine_loaded
    res = b.infer(np.zeros((480, 640, 3), np.uint8), 0.25, 50)
    assert res[0][0] == "person" and res[0][2] == pytest.approx([220, 40, 200, 400])


def test_onnx_missing_model_leaves_engine_unloaded(tmp_path):
    pytest.importorskip("onnxruntime")
    from yolo_node.backends.onnx_backend import OnnxBackend
    b = OnnxBackend(str(tmp_path / "nope.onnx"), COCO80, "yolov8n")
    with pytest.raises(RuntimeError):
        b.load()
    assert not b.engine_loaded


def test_make_backend_dispatch():
    assert make_backend(Config(backend="mock")).mock
    assert make_backend(Config(backend="onnx")).name == "onnx"
    assert make_backend(Config(backend="trt")).name == "trt"          # constructing imports no GPU library


# ---- TensorRT worker protocol (the engine itself needs the Jetson) ---------------------------------------------------
def test_trt_worker_is_python36_syntax():
    src = (Path(__file__).parents[1] / "yolo_node" / "backends" / "trt_worker.py").read_text(encoding="utf-8")
    import ast
    ast.parse(src, feature_version=(3, 6))
    tree = ast.parse(src)
    assert not [n for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module == "__future__"]
    assert not [n for n in ast.walk(tree) if isinstance(n, ast.NamedExpr)]


STUB = textwrap.dedent('''
    import os, sys
    sys.path.insert(0, {backends!r})
    import numpy as np
    import trt_worker
    mode = sys.argv[1]
    def infer(data):
        if mode == "err":
            raise RuntimeError("x")
        if mode == "hang":
            import time; time.sleep(60)
        x = np.frombuffer(data, dtype="<f4")
        assert x.size == 1 * 3 * 64 * 64
        out = np.zeros((1, 84, 5), dtype="<f4")
        out[0, :4, 0] = (32, 32, 20, 40); out[0, 4, 0] = 0.9
        return out.tobytes()
    sys.exit(trt_worker.serve(infer, os.fdopen(os.dup(0), "rb"), os.fdopen(os.dup(1), "wb"), (1, 3, 64, 64), (1, 84, 5)))
''')


def _trt(tmp_path, mode, io_timeout=10):
    from yolo_node.backends.trt_backend import TrtBackend
    stub = tmp_path / "stub.py"
    stub.write_text(STUB.format(backends=str(Path(__file__).parents[1] / "yolo_node" / "backends")))
    return TrtBackend({"worker_python": sys.executable, "worker_script": str(stub), "engine_path": mode,
                       "start_timeout_s": 30, "io_timeout_s": io_timeout}, COCO80, "yolov8n-trt-fp16")


def test_trt_backend_roundtrip_with_stub_worker(tmp_path):
    b = _trt(tmp_path, "ok")
    try:
        b.load()
        assert b.engine_loaded and b.size == 64
        res = b.infer(np.zeros((48, 64, 3), np.uint8), 0.25, 50)
        assert res[0][0] == "person" and res[0][1] == pytest.approx(0.9)
        assert len(b.infer(np.zeros((48, 64, 3), np.uint8), 0.25, 50)) == 1          # worker stays up
    finally:
        b.close()
    assert not b.engine_loaded


def test_trt_backend_engine_error_and_hang(tmp_path):
    b = _trt(tmp_path, "err")
    try:
        b.load()
        with pytest.raises(BackendError):
            b.infer(np.zeros((48, 64, 3), np.uint8), 0.25, 50)
    finally:
        b.close()
    b = _trt(tmp_path, "hang", io_timeout=1)
    try:
        b.load()
        with pytest.raises(BackendError):
            b.infer(np.zeros((48, 64, 3), np.uint8), 0.25, 50)
        assert not b.engine_loaded                       # hung worker killed; restart only after the backoff
        with pytest.raises(BackendError):
            b.infer(np.zeros((48, 64, 3), np.uint8), 0.25, 50)
    finally:
        b.close()


def test_trt_worker_start_failure_is_reported_not_raised_by_node(tmp_path):
    from yolo_node.app import Node
    from yolo_node.backends.trt_backend import TrtBackend
    b = TrtBackend({"worker_python": sys.executable, "worker_script": str(tmp_path / "missing.py"),
                    "engine_path": "x", "start_timeout_s": 5}, COCO80, "m")
    n = Node(Config(), b, {}, None)
    n.load_backend()                                     # logs the exception type, leaves engine_loaded False
    assert not b.engine_loaded


# ---- sources -------------------------------------------------------------------------------------------------------------
def test_folder_replay(tmp_path):
    from yolo_node.sources import FolderReplay
    s = FolderReplay("replay", str(tmp_path))
    assert not s.online()
    with pytest.raises(NodeError):
        s.grab()
    (tmp_path / "a.jpg").write_bytes(image_bytes("JPEG", size=(80, 60)))
    (tmp_path / "b.png").write_bytes(image_bytes("PNG", size=(40, 30)))
    (tmp_path / "note.txt").write_text("ignored")
    assert s.online()
    assert [s.grab()[0].shape[:2] for _ in range(3)] == [(60, 80), (30, 40), (60, 80)]


def test_opencv_stream_with_fake_cv2():
    import time as t
    from yolo_node.sources import OpenCVStream

    class Cap:
        def read(self):
            t.sleep(0.01)
            return True, np.zeros((24, 32, 3), np.uint8)

        def release(self):
            pass

    class Cv2:
        CAP_GSTREAMER = 1800

        def VideoCapture(self, *a):
            return Cap()

    s = OpenCVStream("cam0", "usb", 0, cv2_module=Cv2())
    assert not s.online()
    s.start()
    t.sleep(0.2)
    try:
        assert s.online() and s.grab()[0].shape == (24, 32, 3)
    finally:
        s.close()


def test_http_snapshot_only_http_urls():
    from yolo_node.sources import HttpSnapshot
    with pytest.raises(SystemExit):
        HttpSnapshot("c", "file:///etc/passwd")
