"""01_convert.py: --channel converts only the given channel."""
import h5py
import numpy as np

from conftest import run_script


def text(value):
    return np.frombuffer(str(value).encode(), dtype="S1")


def make_ims(path, shape=(20, 70, 90), channels=2):
    rng = np.random.default_rng(0)
    with h5py.File(path, "w") as f:
        info = f.require_group("DataSetInfo/Image")
        info.attrs["Unit"] = text("um")
        for i, extent in enumerate((90 * 0.5, 70 * 0.5, 20 * 2.0)):        # x, y, z
            info.attrs[f"ExtMin{i}"] = text(0)
            info.attrs[f"ExtMax{i}"] = text(extent)
        for channel in range(channels):
            group = f.require_group(f"DataSet/ResolutionLevel 0/TimePoint 0/Channel {channel}")
            for axis, n in zip("ZYX", shape):
                group.attrs[f"ImageSize{axis}"] = text(n)
            data = (rng.random(shape) * 1000 * (channel + 1)).astype(np.uint16)
            group.create_dataset("Data", data=data, chunks=(16, 32, 32), compression="gzip", shuffle=True)
    return path


def test_convert_one_channel(tmp_path):
    ims = make_ims(tmp_path / "x.ims")
    run_script("01_convert.py", "--input", ims, "--output", tmp_path / "converted.h5", "--channel", 1,
               "--range", 1, 0, 2000)
    assert sorted(p.name for p in tmp_path.glob("converted*")) == ["converted-meta.json", "converted_channel1.h5"]
    with h5py.File(tmp_path / "converted_channel1.h5") as f:
        assert f["data"].dtype == np.uint8 and f["data"].shape == (20, 70, 90)
        assert list(f["data"].attrs["range"]) == [0, 2000]
        assert np.allclose(f["data"].attrs["element_size_um"], [2.0, 0.5, 0.5])
        with h5py.File(ims) as src:
            raw = src["DataSet/ResolutionLevel 0/TimePoint 0/Channel 1/Data"][:]
        expected = np.clip(np.floor(raw * (256.0 / 2001) + 0.5), 0, 255).astype(np.uint8)
        assert np.array_equal(f["data"][:], expected)
