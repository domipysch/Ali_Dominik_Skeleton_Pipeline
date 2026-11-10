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


DATASET = "data"            # dataset name in input and output
GZIP_LEVEL = 2              # output compression (1 = fastest, 9 = smallest)
DOWNSAMPLING = 16           # downsampling factor per axis of the low-res copy
CHUNKS = (64, 64, 64)       # output chunks (z, y, x)
BLOCK = (64, 512, 512)      # voxels (z, y, x) per worker task (a multiple of CHUNKS)
N_WORKERS = min(32, (os.cpu_count() or 1))  # parallel worker processes
MAX_PENDING = 2 * N_WORKERS # blocks in flight (bounds the memory use)

source = None               # input dataset, opened once per worker process


def open_input(path):
    global source
    source = h5py.File(path, "r")[DATASET]


def label_block(region):
    """Label the nonzero voxels of one block (6-connectivity), numbered 1..n.

    Returns (n, voxel count per label 1..n, first planes along z/y/x, last planes along z/y/x).
    """
    labels, n = ndimage.label(source[region] > 0)
    sizes = np.bincount(labels.ravel(), minlength=n + 1)[1:]
    first = [np.take(labels, 0, axis=axis) for axis in range(3)]
    last = [np.take(labels, -1, axis=axis) for axis in range(3)]
    return n, sizes, first, last


def mask_block(region, keep, chunks):
    """Relabel one block (same numbers as label_block), keep[label] -> 0/1 mask, as compressed chunks."""
    if not keep.any():
        return []           # no kept voxels in this block
    labels, _ = ndimage.label(source[region] > 0)
    return compress_chunks(keep[labels].astype(np.uint8), region, chunks, GZIP_LEVEL)


def shifted(labels, offset):
    """Block labels 1..n -> global labels offset+1..offset+n (0 stays 0)."""
    labels = labels.astype(np.int64)
    labels[labels > 0] += offset
    return labels


def main():
    parser = argparse.ArgumentParser(description="Keep only the largest connected component of a binary mask "
                                                 "(.h5, 0/1) and write it as a 0/1 mask (.h5)")
    parser.add_argument("--input", type=Path, required=True,
                        help="mask .h5 from 05_apply_threshold.py (or its _res_16x16x16.h5 copy)")
    parser.add_argument("--output", type=Path, required=True, help="output mask .h5")
    args = parser.parse_args()

    # Check input
    if not args.input.is_file():
        raise SystemExit(f"Input not found: {args.input}")
    if args.output.suffix != ".h5":
        raise SystemExit(f"--output must end with .h5: {args.output}")
    with h5py.File(args.input, "r") as f_in:
        src = f_in[DATASET]
        if src.ndim != 3 or src.dtype != np.uint8:
            raise SystemExit(f"Expected a uint8 (z, y, x) mask, got {src.dtype} {src.shape}")
        shape = src.shape
        attrs = dict(src.attrs)
    # Chunks must not exceed the volume shape
    chunks = tuple(min(c, size) for c, size in zip(CHUNKS, shape))
    make_low_res = min(shape) >= DOWNSAMPLING

    # Check output (e.g. lcc.h5 -> lcc-meta.json)
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
        # downsample_h5 refuses to overwrite
        low_res.unlink(missing_ok=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)

    print("-" * 80)
    print(f"Input:  {args.input}")
    print(f"Output: {args.output}")
    if make_low_res:
        print(f"Low-res: {low_res} ({DOWNSAMPLING}x downsampled per axis)")
    print(f"Shape: {shape} (z, y, x), {N_WORKERS} workers")
    print("Mask: nonzero voxels in the largest connected component (6 neighbours) -> 1, else 0")
    print("-" * 80)

    # Two passes, block by block:
    # Pass 1: label each block, number labels globally, join labels touching across block borders,
    #         sum their voxel counts, pick the largest component.
    # Pass 2: relabel each block and write the voxels of the largest component.
    regions = blocks(shape, BLOCK)
    offsets = []            # first global label - 1 per block
    counts = []             # number of labels per block
    sizes = [np.zeros(1, dtype=np.int64)]  # voxel count per global label (index 0 = background)
    pairs = []              # (label, label) touching across a block border
    last_faces = {}         # (block start, axis) -> global labels on the block's last plane along axis
    n_labels = 0

    with ProcessPoolExecutor(N_WORKERS, initializer=open_input, initargs=(str(args.input),)) as pool:
        tasks = ((region,) for region in regions)
        results = ordered_results(pool, label_block, tasks, MAX_PENDING)
        for region, (n, block_sizes, first, last) in tqdm(zip(regions, results), total=len(regions), unit="block",
                                                          desc="Pass 1/2: labelling", file=sys.stdout):
            offset = n_labels
            offsets.append(offset)
            counts.append(n)
            n_labels += n
            sizes.append(block_sizes.astype(np.int64))

            start = tuple(r.start for r in region)
            for axis in range(3):
                # First plane of this block vs. last plane of the previous block along axis
                if start[axis] > 0:
                    previous = list(start)
                    previous[axis] -= BLOCK[axis]
                    theirs = last_faces.pop((tuple(previous), axis))
                    ours = shifted(first[axis], offset)
                    touching = (theirs > 0) & (ours > 0)
                    pairs.append(np.unique(np.stack([theirs[touching], ours[touching]], axis=1), axis=0))
                if region[axis].stop < shape[axis]:
                    last_faces[(start, axis)] = shifted(last[axis], offset)

        # Join touching labels: connected components of the label graph
        pairs = np.concatenate(pairs) if pairs else np.empty((0, 2), dtype=np.int64)
        graph = coo_matrix((np.ones(len(pairs)), (pairs[:, 0], pairs[:, 1])), shape=(n_labels + 1, n_labels + 1))
        _, joined = connected_components(graph, directed=False)
        sizes = np.concatenate(sizes)
        joined_sizes = np.bincount(joined[1:], weights=sizes[1:], minlength=joined.max() + 1).astype(np.int64)
        n_components = len(np.unique(joined[1:]))
        foreground = int(sizes.sum())

        if n_labels == 0:
            print("Warning: the mask is empty, the output will be all 0")
            keep = np.zeros(1, dtype=bool)
            largest = 0
        else:
            best = joined_sizes.argmax()    # first one on a tie
            keep = joined == best           # per global label: part of the largest component?
            keep[0] = False
            largest = int(joined_sizes[best])
            print(f"{n_components} components, largest: {largest} of {foreground} foreground voxels "
                  f"({100 * largest / foreground:.1f}%)")

        # Per block: keep values of its own labels (index 0 = background)
        def mask_tasks():
            for region, offset, n in zip(regions, offsets, counts):
                block_keep = keep[offset:offset + n + 1].copy()
                block_keep[0] = False
                yield region, block_keep, chunks

        with h5py.File(args.output, "w") as f_out:
            dst = f_out.create_dataset(DATASET, shape=shape, dtype=np.uint8, chunks=chunks,
                                       compression="gzip", compression_opts=GZIP_LEVEL)
            dst.attrs.update(attrs)         # copy input attributes

            results = ordered_results(pool, mask_block, mask_tasks(), MAX_PENDING)
            for written in tqdm(results, total=len(regions), unit="block", desc="Pass 2/2: writing",
                                file=sys.stdout):
                for offset, data in written:
                    dst.id.write_direct_chunk(offset, data)

    # Settings and result of this run
    meta = dict(input=str(args.input), connectivity=6, n_components=n_components,
                largest_component_voxels=largest, foreground_voxels=foreground, gzip_level=GZIP_LEVEL)
    meta_path.write_text(json.dumps(meta, indent=2))

    print("-" * 80)
    print(f"Mask written to {args.output}")
    print(f"Settings written to {meta_path}")

    # Low-res copy of the mask
    if make_low_res:
        downsample_h5(args.output, dataset=DATASET, factor=DOWNSAMPLING)
        print(f"Done. Low-res copy written to {low_res}")
    else:
        print(f"Done. No low-res copy: the volume is smaller than {DOWNSAMPLING} voxels along an axis")


if __name__ == "__main__":
    main()
