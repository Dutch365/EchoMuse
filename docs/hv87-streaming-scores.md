# Hey Leo: stock embedding vs streaming (#87)

180 house clips, scored 2026-11-09 with `controller/tools/hv87_compare.py`.
Both paths use `hey_leo_v6.onnx` and `melspectrogram.onnx` from the house,
and the device scorer (80 ms chunks, mel ring seeded with ones, score is
the max over the clip). Stock embedding is the house `embedding_model.onnx`
(sha256 `70d16429…`, the same file the controller image pins). Streaming
is `embedding_stream.onnx` built from that file (sha256 `7a867551…`).

60 positives are true Hey Leo from `wakeclips/keep` (no wakeverify row).
120 negatives are false wakes from 8 and 9 Nov. All are 16 kHz mono
16-bit, about 1.52 s. The clips are not in this repo.

The number compared is the highest classifier score in the file. The
first ~1.28 s only fills the 16-embedding window.

## Distributions (max score)

|  | n | min | p10 | p50 | p90 | max | mean |
|---|---|---|---|---|---|---|---|
| Positive, stock | 60 | 0.000 | 0.169 | 0.924 | 0.994 | 0.998 | 0.745 |
| Positive, stream | 60 | 0.000 | 0.169 | 0.924 | 0.994 | 0.998 | 0.745 |
| Negative, stock | 120 | 0.000 | 0.001 | 0.045 | 0.794 | 0.982 | 0.229 |
| Negative, stream | 120 | 0.000 | 0.001 | 0.045 | 0.794 | 0.982 | 0.229 |

## 0.65

|  | stock ≥ 0.65 | stream ≥ 0.65 | crossed up | crossed down |
|---|---|---|---|---|
| Positive | 43 / 60 | 43 / 60 | 0 | 0 |
| Negative | 18 / 120 | 18 / 120 | 0 | 0 |

Largest absolute difference on any clip is 0 in float32. The streaming
session did run (28 embedding steps on a checked positive).

## Decision

Streaming does not change these scores, so it does not make real wakes
harder to tell from false ones. `hey_leo_v6` does not need retraining
on streaming features before a flash. The firmware already refuses a
streaming file that disagrees with the stock embedding; this file agrees.

The 17 positives under 0.65 and the 18 negatives at or above it are the
same on both paths. That is the current model on these files, not a
streaming regression. A few negatives were checked against their
wakeverify `oww_score`: the replay is in the same range but not the same
number, because a saved 1.52 s clip is not the exact window the live
scorer saw. Device scores on those negatives were higher, and wakeverify
had already rejected them.
