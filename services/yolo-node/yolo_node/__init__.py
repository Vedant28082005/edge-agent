"""yolo-node: object detection service for the Jetson (and, with the ONNX backend, any ARM64/x86 CPU).

Implements contracts/openapi/yolo-node-internal.yaml. Python 3.8+ (the TensorRT engine runs in a separate
Python 3.6 worker process, see backends/trt_worker.py)."""

CONTRACT_VERSION = "1.0.0"   # info.version of yolo-node-internal.yaml this code implements
SERVICE = "yolo-node"
