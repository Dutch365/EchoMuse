# oww_stream: openWakeWord's embedding, 8 new frames per call (#893)

`make_streaming.py` rewrites the stock `embedding_model.onnx` so each call
takes the 8 mel frames that are new, plus a few small state tensors, in place
of a 76-frame window that is 90% the same as the last one. The output equals
the stock model's on the last 76 frames fed.

```sh
python make_streaming.py embedding_model.onnx embedding_stream.onnx   # needs onnx, numpy
python make_streaming.py embedding_model.onnx embedding_clip.onnx --stock
```

`--stock` keeps the 76-frame input and only rewrites the activation, so the
file is a drop-in replacement for the stock one.

`stream_probe.c` checks any number of variants against stock on an Echo and
times each at the real duty cycle, with the firmware's session settings. It
prints the cost lines the audio bench reads (`tools/audio_bench`, `--cpu`).

```sh
# in the echomuse-compiler image
$CC -O2 -I ../../internal/wakeword/ort/include -o stream_probe stream_probe.c -ldl -lm -pie -fPIE
# on the Echo, firmware stopped
O=/data/local/share/echomuse/oww
./stream_probe $O/libonnxruntime.so $O/embedding_model.onnx 20 1 $O/embedding_model.onnx embedding_stream.onnx
```

## Measured on VVV (FireOS 5), 2026-10-13

One thread, XNNPACK, no spinning, 20 s each in one run. Every variant's
maximum difference from stock was 0.

| Model | States | % of one core | p50 ms |
|---|---|---|---|
| stock | - | 37.6 | 24.6 |
| stock, activation rewritten (`--stock`) | - | 33.6 | 22.2 |
| streaming, per convolution, NCHW states, stock activation | 11 | 11.6 | 9.3 |
| + activation rewritten | 11 | 10.9 | 8.7 |
| + NHWC states (**the default**) | 11 | 9.9 | 7.9 |
| one state per block, NHWC | 4 | 15.4 | 12.3 |
| one state per block, NCHW | 4 | 15.5 | 12.5 |

Stock varies between runs (30.8% in an earlier 30 s run, p95 35-40 ms); the
streaming figures repeated to 0.1 points.

What the variants are:

- **Activation.** The model uses `Max(LeakyRelu(x, 0.2), -0.4)`: two nodes on
  the CPU provider after every convolution. It equals
  `LeakyRelu(Clip(x, min=-2))`, and the XNNPACK provider fuses a Clip into
  the convolution before it, so 19 nodes disappear with no change in output.
- **NHWC states.** XNNPACK computes in NHWC. States declared NCHW cost a
  Transpose in and out each (22 per call in the profile); declared NHWC with
  explicit transposes in the model, the runtime cancels them.
- **One state per block.** Fewer Concat and Slice nodes, more rows
  recomputed. Slower: the arithmetic costs more than the nodes saved.

A per-node profile of the first streaming version is what pointed at these:
20 convolutions at about 0.2 ms each whatever their size, then LeakyRelu,
Max, Transpose, Slice and Concat. What is left at 7.9 ms is mostly that
per-convolution floor, which is the runtime's and not the model's.

Nothing in the firmware uses the streaming model yet.
