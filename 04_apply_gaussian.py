import argparse
import json
import os
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
import h5py
import numpy as np
from scipy import ndimage
from tqdm import tqdm
from utils import blocks, compress_chunks, ordered_results


DATASET = "data"            # dataset in the input (as written by 03_run_ilastik_headless.py) and in the output
GZIP_LEVEL = 2              # output compression (1 = fastest, 9 = smallest)
CHUNKS = (64, 64, 64)       # output chunks (z, y, x)
BLOCK = (64, 512, 512)      # voxels (z, y, x) one worker filters at once (a multiple of CHUNKS)
TRUNCATE = 4.0              # the Gaussian is cut off at TRUNCATE * sigma (scipy's default)
N_WORKERS = min(32, (os.cpu_count() or 1))  # parallel worker processes
MAX_PENDING = 2 * N_WORKERS # blocks in flight (bounds the memory use)

source = None               # the input dataset, opened once in each worker process


def open_input(path):
    global source
    source = h5py.File(path, "r")[DATASET]


def smooth_block(region, sigma, halo):
    """Smooth one block in a worker -> its output chunks as [(chunk offset, gzip bytes)], all-zero chunks left out."""
    # Block plus halo (cut at the volume border, where the filter mirrors the data as usual)
    padded = tuple(slice(max(r.start - h, 0), min(r.stop + h, size)) for r, h, size in zip(region, halo, source.shape))
    # A trailing channel axis (z, y, x, 1) is dropped on reading
    read = padded + (0,) if source.ndim == 4 else padded
    smoothed = ndimage.gaussian_filter(source[read].astype(np.float32), sigma, truncate=TRUNCATE)

    # Cut the halo off again
    inner = tuple(slice(r.start - p.start, r.stop - p.start) for r, p in zip(region, padded))
    out = np.clip(np.round(smoothed[inner]), 0, 255).astype(np.uint8)

    # Compressed here, so the main process only has to write the bytes
    return compress_chunks(out, region, CHUNKS, GZIP_LEVEL)


def main():
    parser = argparse.ArgumentParser(description="Smooth an ilastik probability map (.h5, uint8) with a Gaussian")
    parser.add_argument("--input", type=Path, required=True, help="probability .h5 from 03_run_ilastik_headless.py")
    parser.add_argument("--output", type=Path, required=True, help="output smoothed .h5")
    parser.add_argument("--sigma_z", type=float, default=0.0, help="Gaussian sigma along z, in voxels (default 0.0)")
    parser.add_argument("--sigma_y", type=float, default=1.2, help="Gaussian sigma along y, in voxels (default 1.2)")
    parser.add_argument("--sigma_x", type=float, default=1.2, help="Gaussian sigma along x, in voxels (default 1.2)")
    args = parser.parse_args()
    sigma = (args.sigma_z, args.sigma_y, args.sigma_x)

    # Check input
    if not args.input.is_file():
        raise SystemExit(f"Input not found: {args.input}")
    if min(sigma) < 0:
        raise SystemExit("Sigmas must not be negative")
    if args.output.suffix != ".h5":
        raise SystemExit(f"--output must end with .h5: {args.output}")
    with h5py.File(args.input, "r") as f_in:
        src = f_in[DATASET]
        single_channel = src.ndim == 4 and src.shape[3] == 1
        if (src.ndim != 3 and not single_channel) or src.dtype != np.uint8:
            raise SystemExit(f"Expected uint8 (z, y, x) or (z, y, x, 1) probabilities, got {src.dtype} {src.shape}")
        shape = src.shape[:3]  # the output is always (z, y, x)

    # Check output (e.g. smoothed.h5 -> smoothed-meta.json, as in convert.py)
    meta_path = args.output.with_name(f"{args.output.stem}-meta.json")
    existing = [path for path in (args.output, meta_path) if path.exists()]
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
    print(f"Shape: {shape} (z, y, x)")
    print(f"Gaussian sigma: {sigma} voxels (z, y, x), {N_WORKERS} workers")
    print("-" * 80)

    # Each block is read with this many extra voxels per side, so the filter sees the same neighbourhood
    # as on the whole volume: the result is identical to filtering everything at once (scipy's kernel radius)
    halo = [int(TRUNCATE * s + 0.5) for s in sigma]
    regions = blocks(shape, BLOCK)

    with h5py.File(args.output, "w") as f_out:
        dst = f_out.create_dataset(DATASET, shape=shape, dtype=np.uint8, chunks=CHUNKS,
                                   compression="gzip", compression_opts=GZIP_LEVEL)
        dst.attrs["sigma"] = sigma

        with ProcessPoolExecutor(N_WORKERS, initializer=open_input, initargs=(str(args.input),)) as pool:
            tasks = ((region, sigma, halo) for region in regions)
            results = ordered_results(pool, smooth_block, tasks, MAX_PENDING)
            for chunks in tqdm(results, total=len(regions), unit="block", desc="Smoothing", file=sys.stdout):
                for offset, data in chunks:
                    dst.id.write_direct_chunk(offset, data)

    # Settings of this run, saved next to the output
    meta = dict(input=str(args.input), sigma_zyx=sigma, truncate=TRUNCATE, gzip_level=GZIP_LEVEL)
    meta_path.write_text(json.dumps(meta, indent=2))

    print("-" * 80)
    print(f"Done. Smoothed map written to {args.output}")
    print(f"Settings written to {meta_path}")


if __name__ == "__main__":
    main()
