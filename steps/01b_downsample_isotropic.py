import argparse
import json
import os
import sys
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from multiprocessing import shared_memory
from pathlib import Path
import h5py
import numpy as np
from skimage.transform import resize
from tqdm import tqdm
from utils import blocks, compress_chunks, ordered_results, voxel_size


DATASET = "data"            # dataset in the input (as written by 01_convert.py) and in the output
GZIP_LEVEL = 2              # output compression (1 = fastest, 9 = smallest)
CHUNKS = (64, 64, 64)       # output chunks (z, y, x)
WRITE_BLOCK = (64, 512, 512)  # voxels (z, y, x) compressed per task when writing the output (a multiple of CHUNKS)
ORDER = 1                   # interpolation of skimage's resize: 1 = linear
N_WORKERS = min(8, (os.cpu_count() or 1))
N_WRITERS = min(32, (os.cpu_count() or 1))  # threads compressing the output at the end

slab = None                 # the current slab (z, y, x) in shared memory, attached once in each worker process
slab_memory = None


def attach_slab(name, shape):
    global slab, slab_memory
    slab_memory = shared_memory.SharedMemory(name=name)
    slab = np.ndarray(shape, dtype=np.uint8, buffer=slab_memory.buf)


def output_plan(shape, spacing):
    """Output size (z, y, x) and output voxel size: y and x resampled to the z voxel size if they are finer.

    The output size is a whole number of voxels, so the output voxel size is extent / output size, which is
    the z voxel size up to rounding (less than half a voxel over the whole extent).
    """
    assert spacing[0] > spacing[1] and spacing[0] > spacing[2]
    out_shape = [shape[0]] + [round(n * s / spacing[0]) for n, s in zip(shape[1:], spacing[1:])]
    out_spacing = [round(n * s / m, 2) for n, s, m in zip(shape, spacing, out_shape)]
    return out_shape, out_spacing


def resize_slice(i, out_size):
    """Worker: resize slice i of the current slab to out_size (y, x), with anti-aliasing -> uint8."""
    # float32: skimage would otherwise convert the uint8 slice to float64 (twice the memory)
    image = slab[i].astype(np.float32)
    out = resize(image, out_size, order=ORDER, anti_aliasing=True, preserve_range=True)
    return i, np.clip(np.rint(out), 0, 255).astype(np.uint8)


def main():
    parser = argparse.ArgumentParser(description="Resample an .h5 volume (uint8, z/y/x) in y and x to the z voxel "
                                                 "size, slice by slice with skimage's resize (anti-aliasing + "
                                                 "linear interpolation)")
    parser.add_argument("--input", type=Path, required=True, help="input .h5 from 01_convert.py")
    parser.add_argument("--ims", type=Path, required=True,
                        help="the .ims the .h5 was converted from (its extent gives the voxel size)")
    parser.add_argument("--output", type=Path, required=True, help="output resampled .h5")
    args = parser.parse_args()

    # Check input
    for path in (args.input, args.ims):
        if not path.is_file():
            raise SystemExit(f"Input not found: {path}")
    if args.output.suffix != ".h5":
        raise SystemExit(f"--output must end with .h5: {args.output}")
    with h5py.File(args.input, "r") as f_in:
        src = f_in[DATASET]
        if src.ndim != 3 or src.dtype != np.uint8:
            raise SystemExit(f"Expected a uint8 (z, y, x) volume, got {src.dtype} {src.shape}")
        in_shape = src.shape
        # Slabs as deep as the input chunks: every input chunk is then read and decompressed exactly once
        slab_depth = src.chunks[0] if src.chunks else N_WORKERS
        attrs = dict(src.attrs)

    spacing = voxel_size(args.ims, in_shape)
    out_shape, out_spacing = output_plan(in_shape, spacing)
    if min(out_shape) < 1:
        raise SystemExit(f"Volume {in_shape} too small to resample to {out_shape}")
    out_shape = tuple(out_shape)
    # Chunks must not exceed the volume shape
    chunks = tuple(min(c, size) for c, size in zip(CHUNKS, out_shape))

    # Check output (e.g. iso.h5 -> iso-meta.json, iso-tmp.npy for the intermediate result)
    meta_path = args.output.with_name(f"{args.output.stem}-meta.json")
    tmp_path = args.output.with_name(f"{args.output.stem}-tmp.npy")
    existing = [path for path in (args.output, meta_path, tmp_path) if path.exists()]
    if existing:
        print("Output already exists:")
        for path in existing:
            print(f"  {path}")
        answer = input("Overwrite? [y/N] ")
        if answer.strip().lower() != "y":
            raise SystemExit("Aborted, nothing written.")
    args.output.parent.mkdir(parents=True, exist_ok=True)

    print("-" * 80)
    print(f"Input:  {args.input}")
    print(f"Output: {args.output}")
    print(f"Voxel size: {', '.join(f'{s:.4f}' for s in spacing)} -> "
          f"{', '.join(f'{s:.4f}' for s in out_spacing)} µm (z, y, x)")
    print(f"Shape: {in_shape} -> {out_shape} (z, y, x)")
    for axis, s in zip("yx", spacing[1:]):
        if s >= spacing[0]:
            print(f"Warning: {axis} is not finer than z ({s:.4f} >= {spacing[0]:.4f} µm), it is left as it is")
    print(f"Slabs of {slab_depth} slices ({slab_depth * in_shape[1] * in_shape[2] / 1e9:.1f} GB), "
          f"{N_WORKERS} workers")
    print("-" * 80)

    # Pass 1: slab by slab, read every slice of the slab (whole y-x planes), resize its slices in parallel,
    #         store them in an uncompressed intermediate file on disk
    slab_shape = (slab_depth, in_shape[1], in_shape[2])
    memory = shared_memory.SharedMemory(create=True, size=int(np.prod(slab_shape)))
    try:
        current = np.ndarray(slab_shape, dtype=np.uint8, buffer=memory.buf)
        resized = np.lib.format.open_memmap(tmp_path, mode="w+", dtype=np.uint8, shape=out_shape)

        with (h5py.File(args.input, "r") as f_in,
              ProcessPoolExecutor(N_WORKERS, initializer=attach_slab, initargs=(memory.name, slab_shape)) as pool,
              tqdm(total=in_shape[0], unit="slice", desc="Resizing", file=sys.stdout) as bar):
            src = f_in[DATASET]
            for z0 in range(0, in_shape[0], slab_depth):
                n = min(slab_depth, in_shape[0] - z0)
                src.read_direct(current, np.s_[z0:z0 + n], np.s_[0:n])
                for i, out in pool.map(resize_slice, range(n), [out_shape[1:]] * n):
                    resized[z0 + i] = out
                    bar.update()

        resized.flush()
        del resized, current
    finally:
        memory.close()
        memory.unlink()

    # Pass 2: load the resized volume into memory and write it as a chunked, compressed .h5
    print(f"Loading {tmp_path} ({np.prod(out_shape) / 1e9:.1f} GB)")
    data = np.load(tmp_path)
    regions = blocks(out_shape, WRITE_BLOCK)
    with h5py.File(args.output, "w") as f_out, ThreadPoolExecutor(N_WRITERS) as pool:
        dst = f_out.create_dataset(DATASET, shape=out_shape, dtype=np.uint8, chunks=chunks,
                                   compression="gzip", compression_opts=GZIP_LEVEL)
        dst.attrs.update(attrs)                             # e.g. the 8-bit range of 01_convert.py
        dst.attrs["element_size_um"] = out_spacing

        tasks = ((data[region], region, chunks, GZIP_LEVEL) for region in regions)
        results = ordered_results(pool, compress_chunks, tasks, 2 * N_WRITERS)
        for written in tqdm(results, total=len(regions), unit="block", desc="Writing", file=sys.stdout):
            for offset, compressed in written:
                dst.id.write_direct_chunk(offset, compressed)
    del data
    tmp_path.unlink()

    # Settings of this run, saved next to the output
    meta = dict(input=str(args.input), ims=str(args.ims), shape_in=list(in_shape), shape_out=list(out_shape),
                spacing_in_um=spacing, spacing_out_um=out_spacing, method="skimage.transform.resize",
                order=ORDER, anti_aliasing=True, gzip_level=GZIP_LEVEL)
    meta_path.write_text(json.dumps(meta, indent=2))

    print("-" * 80)
    print(f"Done. Resampled volume written to {args.output}")
    print(f"Settings written to {meta_path}")


if __name__ == "__main__":
    main()
