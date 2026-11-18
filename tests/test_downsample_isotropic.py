import json
import h5py
import numpy as np
import pytest
from skimage.transform import resize
from conftest import read_data, run_script


def write_ims(path, shape, spacing):
    """Minimal .ims with only what the script reads: unit and physical extent (shape * spacing)."""
    with h5py.File(path, "w") as f:
        attrs = f.create_group("DataSetInfo/Image").attrs
        # .ims stores its attributes as arrays of single characters
        text = lambda value: np.array(list(str(value)), dtype="S1")
        attrs["Unit"] = text("um")
        for i, (n, s) in enumerate(zip(reversed(shape), reversed(spacing))):    # 0, 1, 2 = x, y, z
            attrs[f"ExtMin{i}"] = text(0.0)
            attrs[f"ExtMax{i}"] = text(repr(n * s))


@pytest.mark.parametrize("spacing, out_yx", [
    ((3.0, 0.43, 0.5), (158, 172)),     # y and x resampled by different, non-integer factors
    ((2.0, 0.5, 2.5), (275, 1030)),     # y by 4, x not finer than z: left as it is
])
def test_equals_resize_per_slice(prob_path, tmp_path, spacing, out_yx):
    """01b_downsample_isotropic.py (slabs, parallel, intermediate file) equals skimage's resize on every slice."""
    volume = read_data(prob_path)       # chunks (64, 64, 64): two slabs, the second one partial
    ims = tmp_path / "image.ims"
    write_ims(ims, volume.shape, spacing)

    output = tmp_path / "iso.h5"
    run_script("01b_downsample_isotropic.py", "--input", prob_path, "--ims", ims, "--output", output)
    result = read_data(output)
    assert result.shape == (volume.shape[0], *out_yx)

    for z in range(volume.shape[0]):
        expected = resize(volume[z].astype(np.float32), out_yx, order=1, anti_aliasing=True, preserve_range=True)
        expected = np.clip(np.rint(expected), 0, 255).astype(np.uint8)
        assert np.array_equal(result[z], expected), f"slice {z}: {np.count_nonzero(result[z] != expected)} differ"

    # Output voxel size = extent / output size: the z voxel size up to rounding, or the input one if not finer
    with h5py.File(output, "r") as f:
        out_spacing = f["data"].attrs["element_size_um"]
    extent = [n * s for n, s in zip(volume.shape, spacing)]
    assert np.allclose(out_spacing, [e / m for e, m in zip(extent, result.shape)])
    assert np.allclose(out_spacing[1:], [spacing[0] if s < spacing[0] else s for s in spacing[1:]], rtol=0.01)

    meta = json.loads((tmp_path / "iso-meta.json").read_text())
    assert meta["shape_out"] == list(result.shape)
    assert not (tmp_path / "iso-tmp.npy").exists()      # intermediate file removed
