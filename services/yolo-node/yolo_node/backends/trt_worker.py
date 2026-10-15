"""TensorRT engine worker. RUNS UNDER PYTHON 3.6 (JetPack 4.6 system python3): keep this file 3.6-compatible
(no walrus, no f-string `=`, no dataclasses, no `from __future__ import annotations`, no typing generics syntax).
Depends only on the stdlib, numpy, tensorrt (8.2) and pycuda. It imports nothing from the yolo_node package.

Usage:  python3 trt_worker.py ENGINE_PATH
Protocol (binary, over the original stdout/stdin; fd 1 is redirected to stderr so library chatter cannot corrupt it):
    worker -> parent  one ASCII JSON line {"ready": true, "input_shape": [...], "output_shape": [...]}
    parent -> worker  <u32 LE nbytes><nbytes of float32 input tensor>      (1x3xSxS, values 0..1, letterboxed)
    worker -> parent  <u32 LE nbytes><nbytes of float32 output tensor>     or <0xFFFFFFFF> on an engine error
EOF on stdin ends the worker. It never opens image files and never writes to disk."""
import json
import os
import struct
import sys

HDR = struct.Struct("<I")
ERR = 0xFFFFFFFF


def read_exact(f, n):
    buf = b""
    while len(buf) < n:
        chunk = f.read(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def serve(infer, stdin, stdout, in_shape, out_shape):
    """Protocol loop, separated from TensorRT so it is testable without a GPU. infer(bytes) -> bytes."""
    stdout.write((json.dumps({"ready": True, "input_shape": list(in_shape), "output_shape": list(out_shape)})
                  + "\n").encode("ascii"))
    stdout.flush()
    while True:
        h = read_exact(stdin, 4)
        if h is None:
            return 0
        n = HDR.unpack(h)[0]
        data = read_exact(stdin, n)
        if data is None:
            return 0
        try:
            out = infer(data)
            stdout.write(HDR.pack(len(out)) + out)
        except Exception:  # noqa: BLE001
            stdout.write(HDR.pack(ERR))
        stdout.flush()


def build_trt_infer(engine_path):
    import numpy as np
    import pycuda.autoinit  # noqa: F401  (creates the CUDA context)
    import pycuda.driver as cuda
    import tensorrt as trt

    logger = trt.Logger(trt.Logger.WARNING)
    with open(engine_path, "rb") as f, trt.Runtime(logger) as rt:
        engine = rt.deserialize_cuda_engine(f.read())
    ctx = engine.create_execution_context()
    stream = cuda.Stream()
    in_idx = out_idx = None
    for i in range(engine.num_bindings):
        if engine.binding_is_input(i):
            in_idx = i
        else:
            out_idx = i                      # YOLOv8 detect has one output
    in_shape = tuple(engine.get_binding_shape(in_idx))
    out_shape = tuple(engine.get_binding_shape(out_idx))
    in_dt = trt.nptype(engine.get_binding_dtype(in_idx))
    out_dt = trt.nptype(engine.get_binding_dtype(out_idx))
    h_in = cuda.pagelocked_empty(in_shape, in_dt)
    h_out = cuda.pagelocked_empty(out_shape, out_dt)
    d_in = cuda.mem_alloc(h_in.nbytes)
    d_out = cuda.mem_alloc(h_out.nbytes)
    bindings = [0] * engine.num_bindings
    bindings[in_idx], bindings[out_idx] = int(d_in), int(d_out)

    def infer(data):
        x = np.frombuffer(data, dtype="<f4").reshape(in_shape)
        np.copyto(h_in, x.astype(in_dt))
        cuda.memcpy_htod_async(d_in, h_in, stream)
        ctx.execute_async_v2(bindings, stream.handle, None)
        cuda.memcpy_dtoh_async(h_out, d_out, stream)
        stream.synchronize()
        return h_out.astype("<f4").tobytes()

    return infer, in_shape, out_shape


def main(argv):
    if len(argv) != 2:
        sys.stderr.write("usage: trt_worker.py ENGINE_PATH\n")
        return 2
    proto_out = os.fdopen(os.dup(1), "wb")
    os.dup2(2, 1)                            # anything printed to stdout by libraries goes to stderr
    proto_in = os.fdopen(os.dup(0), "rb")
    infer, in_shape, out_shape = build_trt_infer(argv[1])
    return serve(infer, proto_in, proto_out, in_shape, out_shape)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
