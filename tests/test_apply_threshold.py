import numpy as np
import pytest
from skimage.filters import apply_hysteresis_threshold
from conftest import read_data, run_script


@pytest.mark.parametrize("low, high", [(140, 190), (128, 220), (150, 150)])
def test_tiled_equals_one_call(prob_path, tmp_path, low, high):
    """05_apply_threshold.py (tiled, parallel) is bit-identical to one apply_hysteresis_threshold call."""
    output = tmp_path / "mask.h5"
    run_script("05_apply_threshold.py", "--input", prob_path, "--output", output, "--low", low, "--high", high)
    tiled = read_data(output)

    # Whole volume in one call
    whole = apply_hysteresis_threshold(read_data(prob_path), low, high).astype(np.uint8)

    assert tiled.dtype == whole.dtype
    assert tiled.shape == whole.shape
    assert np.array_equal(tiled, whole), f"{np.count_nonzero(tiled != whole)} voxels differ"
    assert (tmp_path / "mask_res_16x16x16.h5").exists()
