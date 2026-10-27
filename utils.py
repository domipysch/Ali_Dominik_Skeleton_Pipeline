import itertools
import sys
import zlib
from collections import deque
from pathlib import Path
import h5py
import numpy as np
from tqdm import tqdm


GZIP_LEVEL = 2      # output compression (1 = fastest, 9 = smallest)
BLOCK = 256         # input voxels per axis read at once (a multiple of the factor and of the 64 voxel chunks)


def blocks(shape, block):
    """Slices (z, y, x) that tile a volume of `shape` in blocks of `block`, in z, y, x order (smaller at the borders)."""
    starts = itertools.product(*(range(0, size, step) for size, step in zip(shape, block)))
    return [tuple(slice(s, min(s + b, size)) for s, b, size in zip(start, block, shape)) for start in starts]


def ordered_results(pool, function, tasks, max_pending):
    """Run function(*task) for every task in the worker pool; yield the results in task order.

    At most max_pending tasks are in flight at once, which bounds the memory use.
    """
    pending = deque()
    for task in tasks:
        pending.append(pool.submit(function, *task))
        if len(pending) > max_pending:
            yield pending.popleft().result()        # waits in case it is not ready yet
    while pending:
        yield pending.popleft().result()


def compress_chunks(data, region, chunks, gzip_level):
    """Split a block `data` (located at `region` in the volume) into gzip-compressed HDF5 chunks.

    Returns [(chunk offset, bytes)] for dataset.id.write_direct_chunk; all-zero chunks are left out, they read back
    as 0 (the fill value) and take no space. `region` must start on chunk borders.
    """
    out = []
    for piece_region in blocks(data.shape, chunks):
        piece = data[piece_region]
        if not piece.any():
            continue
        # HDF5 stores border chunks at full size too: pad them up
        piece = np.pad(piece, [(0, c - n) for c, n in zip(chunks, piece.shape)])
        offset = tuple(r.start + p.start for r, p in zip(region, piece_region))
        out.append((offset, zlib.compress(piece.tobytes(), gzip_level)))
    return out


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
