"""Convert the Keras blank-answer CNN into a NumPy-only weights file.

Usage:
    python scripts/convert_blank_cnn.py [model.keras] [out.npz]

Defaults: Blank_cnn/cnn_blank_neg.keras -> src/lmgrader/assets/blank_cnn.npz.

Reads model.weights.h5 from the .keras zip (needs h5py, which is only required
when the model is retrained and this script is re-run; inference does not need it).
Writes conv0..conv3 kernels/biases and the dense kernel/bias as float32 with
Keras shapes: conv kernels (kh, kw, in, out), dense kernel (in, 1).
"""

import io
import sys
import zipfile
from pathlib import Path

import h5py
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MODEL = ROOT / "Blank_cnn" / "cnn_blank_neg.keras"
DEFAULT_OUT = ROOT / "src" / "lmgrader" / "assets" / "blank_cnn.npz"

CONV_GROUPS = ["conv2d", "conv2d_1", "conv2d_2", "conv2d_3"]
DENSE_GROUP = "dense"


def _read_var(f, group, index):
    return np.asarray(f[f"layers/{group}/vars/{index}"], dtype=np.float32)


def convert(model_path: Path, out_path: Path) -> None:
    with zipfile.ZipFile(model_path) as zf:
        h5_bytes = zf.read("model.weights.h5")
    arrays = {}
    with h5py.File(io.BytesIO(h5_bytes), "r") as f:
        for i, group in enumerate(CONV_GROUPS):
            arrays[f"conv{i}_w"] = _read_var(f, group, 0)
            arrays[f"conv{i}_b"] = _read_var(f, group, 1)
        arrays["dense_w"] = _read_var(f, DENSE_GROUP, 0)
        arrays["dense_b"] = _read_var(f, DENSE_GROUP, 1)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    np.savez(out_path, **arrays)
    for key, arr in arrays.items():
        print(f"{key}: {arr.shape} {arr.dtype}")
    print(f"wrote {out_path}")


def main(argv):
    model = Path(argv[1]) if len(argv) > 1 else DEFAULT_MODEL
    out = Path(argv[2]) if len(argv) > 2 else DEFAULT_OUT
    convert(model, out)


if __name__ == "__main__":
    main(sys.argv)
