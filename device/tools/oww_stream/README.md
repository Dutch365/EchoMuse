# oww_stream: openWakeWord's embedding, 8 new frames per call (#893)

`make_streaming.py` rewrites the stock `embedding_model.onnx` so each call
takes the 8 mel frames that are new, plus 11 small state tensors, in place of
a 76-frame window that is 90% the same as the last one. The output equals
the stock model's on the last 76 frames fed.

```sh
python make_streaming.py embedding_model.onnx embedding_stream.onnx   # needs onnx, numpy
```

`stream_probe.c` checks that on an Echo and times both at the real duty
cycle, with the firmware's session settings. It prints the cost lines the
audio bench reads (`tools/audio_bench`, `--cpu`).

```sh
# in the echomuse-compiler image
$CC -O2 -I ../../internal/wakeword/ort/include -o stream_probe stream_probe.c -ldl -lm -pie -fPIE
# on the Echo, firmware stopped
./stream_probe /data/local/share/echomuse/oww/libonnxruntime.so \
    /data/local/share/echomuse/oww/embedding_model.onnx embedding_stream.onnx 30 1
```

Measured on VVV (FireOS 5), 2026-10-13, one thread, XNNPACK, no spinning:
stock 30.8% of one core (p50 23.8 ms), streaming 11.7% (p50 9.3 ms), maximum
difference 0. With the plain CPU provider: 47.3% and 21.5%.

Nothing in the firmware uses the streaming model yet.
