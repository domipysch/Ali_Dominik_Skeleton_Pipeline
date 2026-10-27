import subprocess
import sys
from pathlib import Path
import h5py
import numpy as np
import pytest
from scipy import ndimage

REPO = Path(__file__).parent.parent
SHAPE = (100, 1100, 1030)       # spans several (64, 512, 512) blocks, with partial blocks and chunks at the border


def run_script(name, *args):
    """Run one of the pipeline scripts as a command (so its worker processes run as in real use)."""
    subprocess.run([sys.executable, str(REPO / name), *map(str, args)], check=True, stdout=subprocess.DEVNULL)


def read_data(path):
    with h5py.File(path, "r") as f:
        return f["data"][:]


@pytest.fixture(scope="session")
def prob_path(tmp_path_factory):
    """Synthetic probability-like map (.h5, uint8, dataset "data"): many blobs crossing block borders, an empty area."""
    rng = np.random.default_rng(0)
    prob = ndimage.gaussian_filter(rng.random(SHAPE, dtype=np.float32), 2.5)
    prob = np.clip((prob - 0.5) * 2500 + 128, 0, 255).astype(np.uint8)
    prob[:, :300, :] = 0

    path = tmp_path_factory.mktemp("data") / "prob.h5"
    with h5py.File(path, "w") as f:
        f.create_dataset("data", data=prob, chunks=(64, 64, 64), compression="gzip")
    return path
