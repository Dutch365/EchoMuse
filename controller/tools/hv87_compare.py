#!/usr/bin/env python3
"""Score Hey Leo clips with the stock embedding and the streaming one.

Both paths use the device scorer in device/internal/wakeword (80 ms chunks,
mel ring seeded with ones, classifier over the last 16 embeddings). The
streaming path is embedding_stream.onnx: eight new mel frames plus the
carried state, the same model the firmware uses when that file is installed.

The clip the wake word is judged on is the highest score in the file. A
1.5 s clip only has a few scores; the first ~1.28 s fills the classifier
window and is not scored.

Clips are read from a directory the caller passes. This script does not
ship any audio.

    python controller/tools/hv87_compare.py /path/to/hv87 --threshold 0.65
"""

from __future__ import annotations

import argparse
import csv
import json
import wave
from pathlib import Path

import numpy as np
import onnxruntime as ort

SR = 16000
CHUNK = 1280
MEL_BINS = 32
MEL_CONTEXT = 480
MEL_WINDOW = 76
MEL_STEP = 8
FEAT_DIM = 96
FEAT_WINDOW = 16
MEL_SCALE = 10.0
MEL_OFFSET = 2.0
STREAM_PRIME = (MEL_WINDOW + MEL_STEP - 1) // MEL_STEP


def _pcm(path: Path) -> np.ndarray:
    with wave.open(str(path)) as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2 or w.getframerate() != SR:
            raise SystemExit(f"{path}: want 16 kHz mono 16-bit, got "
                             f"{w.getframerate()} Hz {w.getnchannels()}ch {w.getsampwidth()*8}-bit")
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).copy()


class Scorer:
    def __init__(self, mel, emb, clf, stream, use_stream: bool):
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 1
        self.mel = ort.InferenceSession(str(mel), opts, providers=["CPUExecutionProvider"])
        self.emb = ort.InferenceSession(str(emb), opts, providers=["CPUExecutionProvider"])
        self.clf = ort.InferenceSession(str(clf), opts, providers=["CPUExecutionProvider"])
        self.stream = None
        if use_stream:
            self.stream = ort.InferenceSession(str(stream), opts, providers=["CPUExecutionProvider"])
            self.state_names = [i.name for i in self.stream.get_inputs() if i.name.startswith("state_")]
            self.state_shapes = [tuple(int(d) for d in i.shape) for i in self.stream.get_inputs()
                                 if i.name.startswith("state_")]
            self.next_names = [o.name for o in self.stream.get_outputs() if o.name.endswith("_next")]
        self.use_stream = use_stream

    def _melspec(self, samples: np.ndarray) -> np.ndarray:
        x = samples.astype(np.float32)[None, :]
        out = self.mel.run(None, {self.mel.get_inputs()[0].name: x})[0]
        frames = out.reshape(-1, MEL_BINS).astype(np.float32)
        return frames / MEL_SCALE + MEL_OFFSET

    def _embed_stock(self, window: np.ndarray) -> np.ndarray:
        x = window.reshape(1, MEL_WINDOW, MEL_BINS, 1)
        out = self.emb.run(None, {self.emb.get_inputs()[0].name: x})[0]
        return out.reshape(FEAT_DIM).astype(np.float32)

    def _reset_stream(self):
        self.states = [np.zeros(s, np.float32) for s in self.state_shapes]

    def _embed_step(self, step: np.ndarray) -> np.ndarray:
        feed = {"mel": step.reshape(1, MEL_STEP, MEL_BINS, 1)}
        for name, value in zip(self.state_names, self.states):
            feed[name] = value
        outs = self.stream.run(None, feed)
        names = [o.name for o in self.stream.get_outputs()]
        by = dict(zip(names, outs))
        self.states = [by[n] for n in self.next_names]
        return by["embedding"].reshape(FEAT_DIM).astype(np.float32)

    def score_clip(self, pcm: np.ndarray) -> dict:
        """Max classifier score, and how many 80 ms steps produced one."""
        mel = np.ones((MEL_WINDOW, MEL_BINS), np.float32)
        raw = np.zeros(0, np.int16)
        feats = []
        scores = []
        stream_at = -1
        if self.use_stream:
            self._reset_stream()

        def step_frames(end: int) -> np.ndarray:
            out = np.zeros((MEL_STEP, MEL_BINS), np.float32)
            start = end - MEL_STEP
            skip = 0
            if start < 0:
                skip, start = -start, 0
            out[skip:] = mel[start:end]
            return out

        def embed_stream(end: int) -> np.ndarray:
            nonlocal stream_at
            abs_end = end  # ring is not trimmed on a 1.5 s clip
            if stream_at != abs_end - MEL_STEP:
                self._reset_stream()
                stream_at = -1
                for k in range(STREAM_PRIME - 1, 0, -1):
                    self._embed_step(step_frames(end - MEL_STEP * k))
            emb = self._embed_step(step_frames(end))
            stream_at = abs_end
            return emb

        n = len(pcm) // CHUNK
        for c in range(n):
            chunk = pcm[c * CHUNK:(c + 1) * CHUNK]
            raw = np.concatenate([raw, chunk])
            want = CHUNK + MEL_CONTEXT
            src = raw[-want:] if len(raw) >= want else raw
            new = self._melspec(src)
            mel = np.concatenate([mel, new], axis=0)
            end = len(mel)
            start = end - MEL_WINDOW
            if start < 0:
                continue
            if self.use_stream:
                emb = embed_stream(end)
            else:
                emb = self._embed_stock(mel[start:end])
            feats.append(emb)
            if len(feats) >= FEAT_WINDOW:
                window = np.stack(feats[-FEAT_WINDOW:], axis=0)[None, :, :]
                s = float(self.clf.run(None, {self.clf.get_inputs()[0].name: window})[0].reshape(-1)[0])
                scores.append(s)
        if not scores:
            return {"max": None, "n": 0}
        return {"max": max(scores), "n": len(scores), "last": scores[-1]}


def _stats(xs: list[float]) -> dict:
    a = np.array(xs, np.float64)
    pct = np.percentile(a, [0, 10, 50, 90, 100])
    return {
        "n": int(a.size),
        "min": float(pct[0]),
        "p10": float(pct[1]),
        "p50": float(pct[2]),
        "p90": float(pct[3]),
        "max": float(pct[4]),
        "mean": float(a.mean()),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("root", type=Path, help="directory with pos/, neg/, models/")
    ap.add_argument("--threshold", type=float, default=0.65)
    ap.add_argument("--out", type=Path, help="write the summary JSON here")
    a = ap.parse_args()
    root = a.root
    models = root / "models"
    stock = Scorer(models / "melspectrogram.onnx", models / "embedding_model.onnx",
                   models / "hey_leo_v6.onnx", models / "embedding_stream.onnx", False)
    stream_path = root / "models" / "embedding_stream.onnx"
    if not stream_path.exists():
        stream_path = Path(__file__).resolve().parents[2] / "device" / "build" / "embedding_stream.onnx"
    stream = Scorer(models / "melspectrogram.onnx", models / "embedding_model.onnx",
                    models / "hey_leo_v6.onnx", stream_path, True)

    rows = []
    for label, folder in (("pos", "pos"), ("neg", "neg")):
        for path in sorted((root / folder).glob("*.wav")):
            pcm = _pcm(path)
            old = stock.score_clip(pcm)
            new = stream.score_clip(pcm)
            rows.append({
                "label": label,
                "old": old["max"],
                "new": new["max"],
                "n_old": old["n"],
                "n_new": new["n"],
            })

    thr = a.threshold
    summary = {"threshold": thr, "clips": len(rows), "labels": {}}
    for label in ("pos", "neg"):
        part = [r for r in rows if r["label"] == label]
        old = [r["old"] for r in part]
        new = [r["new"] for r in part]
        both = [(r["old"], r["new"]) for r in part]
        summary["labels"][label] = {
            "old": _stats(old),
            "stream": _stats(new),
            "old_at_or_above": sum(s >= thr for s in old),
            "stream_at_or_above": sum(s >= thr for s in new),
            "old_below": sum(s < thr for s in old),
            "stream_below": sum(s < thr for s in new),
            "gained": sum(o < thr <= n for o, n in both),
            "lost": sum(n < thr <= o for o, n in both),
            "max_abs_delta": float(max(abs(o - n) for o, n in both)),
            "median_abs_delta": float(np.median([abs(o - n) for o, n in both])),
        }
    text = json.dumps(summary, indent=2)
    print(text)
    if a.out:
        a.out.write_text(text + "\n")


if __name__ == "__main__":
    main()
