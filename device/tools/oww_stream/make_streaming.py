#!/usr/bin/env python3
"""Rewrite openWakeWord's embedding model to take 8 new mel frames per call.

The stock model takes a 76-frame window and is called every 80 ms on a window
that has moved 8 frames, so 68 of 76 frames are recomputed each time. Its
convolutions are unpadded in time and its pooling has a total stride of 8, so
the same outputs can be had by keeping, for every layer that looks across
time, the last two rows of its input from the previous call.

    make_streaming.py embedding_model.onnx embedding_stream.onnx

Inputs:  mel [1, 8, 32, 1], state_0..state_N (zeros to start)
Outputs: embedding [1, 96], state_0..state_N for the next call
The first 9 calls are warm-up; from the 10th the output equals the stock
model's on the last 76 frames fed.
"""
import sys

import numpy as np
import onnx
from onnx import TensorProto, helper, numpy_helper, shape_inference

STEP = 8


def main(src, dst):
    m = onnx.load(src)
    shapes = {v.name: [d.dim_value for d in v.type.tensor_type.shape.dim]
              for v in shape_inference.infer_shapes(m).graph.value_info}
    g = m.graph
    first = next(n for n in g.node if n.op_type == "Reshape" and n.input[0] == g.input[0].name)
    last = next(n for n in g.node if n.op_type == "Reshape" and n.output[0] == g.output[0].name)
    inits = [i for i in g.initializer if i.name not in (first.input[1], last.input[1])]
    inits += [
        numpy_helper.from_array(np.array([1, 1, STEP, 32], np.int64), "stream_in_shape"),
        numpy_helper.from_array(np.array([1, 96], np.int64), "stream_out_shape"),
        numpy_helper.from_array(np.array([-2], np.int64), "tail_start"),
        numpy_helper.from_array(np.array([2 ** 31], np.int64), "tail_end"),
        numpy_helper.from_array(np.array([2], np.int64), "time_axis"),
    ]
    nodes, ins, outs = [], [], []
    ins.append(helper.make_tensor_value_info("mel", TensorProto.FLOAT, [1, STEP, 32, 1]))
    nodes.append(helper.make_node("Reshape", ["mel", "stream_in_shape"], [first.output[0]]))
    k = 0
    for n in g.node:
        if n is first or n is last:
            continue
        if n.op_type == "Conv":
            kernel = next(a.ints for a in n.attribute if a.name == "kernel_shape")
            if kernel[0] == 3:                       # looks across time
                x = n.input[0]
                c, w = (1, 32) if x == first.output[0] else (shapes[x][1], shapes[x][3])
                st_in, st_out, cat = f"state_{k}", f"state_{k}_next", f"stream_cat_{k}"
                ins.append(helper.make_tensor_value_info(st_in, TensorProto.FLOAT, [1, c, 2, w]))
                outs.append(helper.make_tensor_value_info(st_out, TensorProto.FLOAT, [1, c, 2, w]))
                nodes.append(helper.make_node("Concat", [st_in, x], [cat], axis=2))
                nodes.append(helper.make_node("Slice", [cat, "tail_start", "tail_end", "time_axis"], [st_out]))
                n.input[0] = cat
                k += 1
            elif kernel[0] != 1:
                raise SystemExit(f"unexpected kernel {list(kernel)}")
        nodes.append(n)
    nodes.append(helper.make_node("Reshape", [last.input[0], "stream_out_shape"], ["embedding"]))
    outs.insert(0, helper.make_tensor_value_info("embedding", TensorProto.FLOAT, [1, 96]))
    graph = helper.make_graph(nodes, "oww_embedding_stream", ins, outs, inits)
    out = helper.make_model(graph, opset_imports=m.opset_import, ir_version=m.ir_version)
    onnx.checker.check_model(out)
    onnx.save(out, dst)
    print(f"{dst}: {k} states, {sum(int(np.prod([d.dim_value for d in i.type.tensor_type.shape.dim])) for i in ins[1:])} floats of state")


if __name__ == "__main__":
    main(*sys.argv[1:3])
