import itertools
import sys
from pathlib import Path
import h5py
from tqdm import tqdm


GZIP_LEVEL = 2      # output compression (1 = fastest, 9 = smallest)
BLOCK = 256         # input voxels per axis read at once (a multiple of the factor and of the 64 voxel chunks)


def downsampled_path(path, factor=16):
    """Where downsample_h5 saves its result: xxx.h5 -> xxx_res_16x16x16.h5 next to it."""
    path = Path(path)
    return path.with_name(f"{path.stem}_res_{factor}x{factor}x{factor}.h5")


def downsample_h5(path, dataset="data", factor=16):
    """Downsample one (z, y, x) dataset of an .h5 by `factor` per axis: each output voxel = mean of factor^3 voxels.

    Saved next to the input as xxx_res_16x16x16.h5, with the same dataset name. A border thinner than `factor`
    voxels is dropped. Returns the output path.
    """
    out_path = downsampled_path(path, factor)
    if out_path.exists():
        raise FileExistsError(f"Output already exists: {out_path}")

    with h5py.File(path, "r") as f_in, h5py.File(out_path, "w") as f_out:
        src = f_in[dataset]
        if src.ndim != 3:
            raise ValueError(f"Expected a (z, y, x) dataset, got shape {src.shape}")

        out_shape = tuple(size // factor for size in src.shape)
        dst = f_out.create_dataset(dataset, shape=out_shape, dtype=src.dtype, chunks=True,
                                   compression="gzip", compression_opts=GZIP_LEVEL)
        dst.attrs.update(src.attrs)
        if "element_size_um" in src.attrs:
            dst.attrs["element_size_um"] = src.attrs["element_size_um"] * factor

        # Block by block, so the full volume never has to fit in memory
        used = [n * factor for n in out_shape]        # input size without the dropped border
        starts = list(itertools.product(*(range(0, size, BLOCK) for size in used)))
        for start in tqdm(starts, unit="block", desc="Downsampling", file=sys.stdout):
            block = src[tuple(slice(s, min(s + BLOCK, size)) for s, size in zip(start, used))]

            # (z, y, x) -> (z/f, f, y/f, f, x/f, f), then average each f x f x f cube
            nz, ny, nx = (n // factor for n in block.shape)
            mean = block.reshape(nz, factor, ny, factor, nx, factor).mean(axis=(1, 3, 5))

            z, y, x = (s // factor for s in start)
            dst[z:z + nz, y:y + ny, x:x + nx] = mean.round().astype(src.dtype)

    return out_path
