import numpy as np
import pytest
from scipy import ndimage
from conftest import read_data, run_script


@pytest.mark.parametrize("sigma", [(0.0, 1.2, 1.2), (1.0, 2.0, 0.5)])
def test_tiled_equals_one_call(prob_path, tmp_path, sigma):
    """04_apply_gaussian.py (tiled, parallel) is bit-identical to one ndimage.gaussian_filter call."""
    output = tmp_path / "smoothed.h5"
    run_script("04_apply_gaussian.py", "--input", prob_path, "--output", output,
               "--sigma_z", sigma[0], "--sigma_y", sigma[1], "--sigma_x", sigma[2])
    tiled = read_data(output)

    # Whole volume in one call, converted to uint8 the same way as in the script
    prob = read_data(prob_path)
    whole = np.clip(np.round(ndimage.gaussian_filter(prob.astype(np.float32), sigma)), 0, 255).astype(np.uint8)

    assert tiled.dtype == whole.dtype
    assert tiled.shape == whole.shape
    assert np.array_equal(tiled, whole), f"{np.count_nonzero(tiled != whole)} voxels differ"
