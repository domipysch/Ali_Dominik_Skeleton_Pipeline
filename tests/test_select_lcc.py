import h5py
import numpy as np
import pytest
from scipy import ndimage
from conftest import read_data, run_script


def largest_component(mask):
    """Whole volume in one call: keep the largest 6-connected component."""
    labels, n = ndimage.label(mask > 0)
    if n == 0:
        return np.zeros_like(mask)
    sizes = np.bincount(labels.ravel())
    sizes[0] = 0
    return (labels == sizes.argmax()).astype(np.uint8)


@pytest.fixture(scope="session")
def mask_path(prob_path, tmp_path_factory):
    """Binary mask (0/1) from the synthetic probability map: many components crossing block borders."""
    mask = (read_data(prob_path) > 140).astype(np.uint8)
    path = tmp_path_factory.mktemp("mask") / "mask.h5"
    with h5py.File(path, "w") as f:
        f.create_dataset("data", data=mask, chunks=(64, 64, 64), compression="gzip")
    return path


def test_tiled_equals_one_call(mask_path, tmp_path):
    """06_select_lcc.py (tiled, parallel) is bit-identical to labelling the whole volume at once."""
    output = tmp_path / "lcc.h5"
    run_script("06_select_lcc.py", "--input", mask_path, "--output", output)
    tiled = read_data(output)
    whole = largest_component(read_data(mask_path))

    assert tiled.dtype == whole.dtype
    assert tiled.shape == whole.shape
    assert whole.any()
    assert np.array_equal(tiled, whole), f"{np.count_nonzero(tiled != whole)} voxels differ"
    assert (tmp_path / "lcc_res_16x16x16.h5").exists()


def test_small_input(tmp_path):
    """A volume smaller than one chunk (like a low-res copy) works, without a low-res copy of its own."""
    rng = np.random.default_rng(1)
    mask = (rng.random((6, 70, 40)) > 0.6).astype(np.uint8)
    path = tmp_path / "small.h5"
    with h5py.File(path, "w") as f:
        f.create_dataset("data", data=mask)

    output = tmp_path / "small_lcc.h5"
    run_script("06_select_lcc.py", "--input", path, "--output", output)
    assert np.array_equal(read_data(output), largest_component(mask))
    assert not (tmp_path / "small_lcc_res_16x16x16.h5").exists()


def test_empty_mask(tmp_path):
    path = tmp_path / "empty.h5"
    with h5py.File(path, "w") as f:
        f.create_dataset("data", data=np.zeros((20, 30, 30), dtype=np.uint8))

    output = tmp_path / "empty_lcc.h5"
    run_script("06_select_lcc.py", "--input", path, "--output", output)
    assert not read_data(output).any()
