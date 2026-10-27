import argparse
import json
import os
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
import h5py
import numpy as np
from scipy import ndimage
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components
from tqdm import tqdm
from utils import blocks, compress_chunks, downsample_h5, downsampled_path, ordered_results


DATASET = "data"            # dataset in the input (as written by 04_apply_gaussian.py) and in the output
GZIP_LEVEL = 2              # output compression (1 = fastest, 9 = smallest)
DOWNSAMPLING = 16           # a low-res copy of the mask (factor per axis) is saved next to it
CHUNKS = (64, 64, 64)       # output chunks (z, y, x)
BLOCK = (64, 512, 512)      # voxels (z, y, x) one worker labels at once (a multiple of CHUNKS)
N_WORKERS = os.cpu_count()  # parallel worker processes
MAX_PENDING = 2 * N_WORKERS # blocks in flight (bounds the memory use)

source = None               # the input dataset, opened once in each worker process


def open_input(path):
    global source
    source = h5py.File(path, "r")[DATASET]


def label_block(region, low, high):
    """Pass 1 worker: connected regions of voxels > low in one block, numbered 1..n (0 = background).

    Face connectivity (6 neighbours), as in skimage.filters.apply_hysteresis_threshold.
    Returns (n, labels containing a voxel > high, first planes along z/y/x, last planes along z/y/x).
    """
    data = source[region]
    labels, n = ndimage.label(data > low)
    seeded = np.unique(labels[(data > high) & (labels > 0)])
    first = [np.take(labels, 0, axis=axis) for axis in range(3)]
    last = [np.take(labels, -1, axis=axis) for axis in range(3)]
    return n, seeded, first, last


def mask_block(region, low, keep):
    """Pass 2 worker: label the block again (same numbers as in pass 1), keep[label] -> 0/1 mask, compressed chunks."""
    labels, _ = ndimage.label(source[region] > low)
    return compress_chunks(keep[labels].astype(np.uint8), region, CHUNKS, GZIP_LEVEL)


def shifted(labels, offset):
    """Block labels 1..n -> global labels offset+1..offset+n (0 stays 0)."""
    labels = labels.astype(np.int64)
    labels[labels > 0] += offset
    return labels


def main():
    parser = argparse.ArgumentParser(description="Hysteresis threshold of a probability map (.h5, uint8) "
                                                 "into a binary 0/1 mask (.h5)")
    parser.add_argument("--input", type=Path, required=True, help="smoothed .h5 from 04_apply_gaussian.py")
    parser.add_argument("--low", type=int, required=True,
                        help="0..255: voxels > low are kept if their connected region contains a voxel > high")
    parser.add_argument("--high", type=int, required=True, help="0..255: voxels > high are always kept")
    parser.add_argument("--output", type=Path, required=True, help="output mask .h5")
    args = parser.parse_args()

    # Check input
    if not args.input.is_file():
        raise SystemExit(f"Input not found: {args.input}")
    if not 0 <= args.low <= args.high <= 255:
        raise SystemExit("Need 0 <= --low <= --high <= 255")
    if args.output.suffix != ".h5":
        raise SystemExit(f"--output must end with .h5: {args.output}")
    with h5py.File(args.input, "r") as f_in:
        src = f_in[DATASET]
        if src.ndim != 3 or src.dtype != np.uint8:
            raise SystemExit(f"Expected uint8 (z, y, x) probabilities, got {src.dtype} {src.shape}")
        shape = src.shape

    # Check output (e.g. mask.h5 -> mask-meta.json, as in convert.py)
    meta_path = args.output.with_name(f"{args.output.stem}-meta.json")
    low_res = downsampled_path(args.output, DOWNSAMPLING)
    existing = [path for path in (args.output, meta_path, low_res) if path.exists()]
    if existing:
        print("Output already exists:")
        for path in existing:
            print(f"  {path}")
        answer = input("Overwrite? [y/N] ")
        if answer.strip().lower() != "y":
            raise SystemExit("Aborted, nothing written.")
        # downsample_h5 refuses to overwrite, so remove the old low-res copy
        low_res.unlink(missing_ok=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)

    print("-" * 80)
    print(f"Input:  {args.input}")
    print(f"Output: {args.output}")
    print(f"Low-res: {low_res} ({DOWNSAMPLING}x downsampled per axis)")
    print(f"Shape: {shape} (z, y, x), {N_WORKERS} workers")
    print(f"Mask: voxels > {args.low} whose connected region contains a voxel > {args.high} -> 1, else 0")
    print("-" * 80)

    # The volume does not fit in memory, but a connected region can span many blocks. So:
    # Pass 1: workers label the regions of each block; here they are numbered across all blocks, and regions
    #         touching across a block border are joined. A joined region is kept if it contains a voxel > high.
    # Pass 2: workers label each block again (same numbers) and write the kept regions.
    regions = blocks(shape, BLOCK)
    offsets = []            # first label number - 1 of each block
    counts = []             # number of labels of each block
    seeded = []             # labels that contain a voxel > high
    pairs = []              # (label, label) touching across a block border
    last_faces = {}         # (block start, axis) -> labels on that block's last plane along axis
    n_labels = 0

    with ProcessPoolExecutor(N_WORKERS, initializer=open_input, initargs=(str(args.input),)) as pool:
        tasks = ((region, args.low, args.high) for region in regions)
        results = ordered_results(pool, label_block, tasks, MAX_PENDING)
        for region, (n, block_seeded, first, last) in tqdm(zip(regions, results), total=len(regions), unit="block",
                                                           desc="Pass 1/2: labelling", file=sys.stdout):
            offset = n_labels
            offsets.append(offset)
            counts.append(n)
            n_labels += n
            seeded.append(block_seeded.astype(np.int64) + offset)

            start = tuple(r.start for r in region)
            for axis in range(3):
                # First plane of this block against the last plane of the previous block along this axis
                if start[axis] > 0:
                    previous = list(start)
                    previous[axis] -= BLOCK[axis]
                    theirs = last_faces.pop((tuple(previous), axis))
                    ours = shifted(first[axis], offset)
                    touching = (theirs > 0) & (ours > 0)
                    pairs.append(np.unique(np.stack([theirs[touching], ours[touching]], axis=1), axis=0))
                if region[axis].stop < shape[axis]:
                    last_faces[(start, axis)] = shifted(last[axis], offset)

        # Join touching regions: connected components of the graph "label - label touches"
        pairs = np.concatenate(pairs) if pairs else np.empty((0, 2), dtype=np.int64)
        graph = coo_matrix((np.ones(len(pairs)), (pairs[:, 0], pairs[:, 1])), shape=(n_labels + 1, n_labels + 1))
        _, joined = connected_components(graph, directed=False)
        keep_joined = np.zeros(joined.max() + 1, dtype=bool)
        keep_joined[joined[np.concatenate(seeded)]] = True
        keep = keep_joined[joined]          # per label: is it part of a region with a voxel > high?

        # Each worker only gets the keep values of its own block's labels (index 0 = background)
        def mask_tasks():
            for region, offset, n in zip(regions, offsets, counts):
                block_keep = keep[offset:offset + n + 1].copy()
                block_keep[0] = False
                yield region, args.low, block_keep

        with h5py.File(args.output, "w") as f_out:
            dst = f_out.create_dataset(DATASET, shape=shape, dtype=np.uint8, chunks=CHUNKS,
                                       compression="gzip", compression_opts=GZIP_LEVEL)
            dst.attrs["low"] = args.low
            dst.attrs["high"] = args.high

            results = ordered_results(pool, mask_block, mask_tasks(), MAX_PENDING)
            for chunks in tqdm(results, total=len(regions), unit="block", desc="Pass 2/2: writing", file=sys.stdout):
                for offset, data in chunks:
                    dst.id.write_direct_chunk(offset, data)

    # Settings of this run, saved next to the output
    meta = dict(input=str(args.input), low=args.low, high=args.high, gzip_level=GZIP_LEVEL)
    meta_path.write_text(json.dumps(meta, indent=2))

    print("-" * 80)
    print(f"Mask written to {args.output}")
    print(f"Settings written to {meta_path}")

    # Low-res copy of the mask, e.g. for a quick look at the whole volume
    downsample_h5(args.output, dataset=DATASET, factor=DOWNSAMPLING)
    print(f"Done. Low-res copy written to {low_res}")


if __name__ == "__main__":
    main()
