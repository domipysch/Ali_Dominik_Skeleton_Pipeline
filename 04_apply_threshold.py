import argparse
import json
import sys
from pathlib import Path
import h5py
import numpy as np
from tqdm import tqdm
from utils import downsample_h5, downsampled_path


DATASET = "data"        # dataset in the input (as written by 03_run_ilastik_headless.py) and in the output
GZIP_LEVEL = 2          # output compression (1 = fastest, 9 = smallest)
DOWNSAMPLING = 16       # a low-res copy of the mask (factor per axis) is saved next to it


def main():
    parser = argparse.ArgumentParser(description="Threshold an ilastik probability map (.h5, uint8) "
                                                 "into a binary 0/1 mask (.h5)")
    parser.add_argument("--input", type=Path, required=True, help="probability .h5 from 03_run_ilastik_headless.py")
    parser.add_argument("--threshold", type=int, required=True,
                        help="0..255: voxels >= threshold become 1, all others 0")
    parser.add_argument("--output", type=Path, required=True, help="output mask .h5")
    args = parser.parse_args()

    # Check input
    if not args.input.is_file():
        raise SystemExit(f"Input not found: {args.input}")
    if not 0 <= args.threshold <= 255:
        raise SystemExit("--threshold must be between 0 and 255")
    if args.output.suffix != ".h5":
        raise SystemExit(f"--output must end with .h5: {args.output}")

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

    with h5py.File(args.input, "r") as f_in, h5py.File(args.output, "w") as f_out:
        src = f_in[DATASET]
        if src.ndim != 3 or src.dtype != np.uint8:
            raise SystemExit(f"Expected uint8 (z, y, x) probabilities, got {src.dtype} {src.shape}")

        print("-" * 80)
        print(f"Input:  {args.input}")
        print(f"Output: {args.output}")
        print(f"Low-res: {low_res} ({DOWNSAMPLING}x downsampled per axis)")
        print(f"Shape: {src.shape} (z, y, x), chunks {src.chunks}")
        print(f"Mask: probability >= {args.threshold} -> 1, else 0")
        print("-" * 80)

        dst = f_out.create_dataset(DATASET, shape=src.shape, dtype=np.uint8, chunks=src.chunks,
                                   compression="gzip", compression_opts=GZIP_LEVEL)
        dst.attrs["threshold"] = args.threshold

        # Chunk by chunk, so the whole volume never has to fit in memory
        for region in tqdm(list(src.iter_chunks()), unit="chunk", desc="Thresholding", file=sys.stdout):
            mask = (src[region] >= args.threshold).astype(np.uint8)
            # Empty chunks are not written at all: they read back as 0 (the fill value) and take no space
            if mask.any():
                dst[region] = mask

    # Settings of this run, saved next to the output
    meta = dict(input=str(args.input), threshold=args.threshold, gzip_level=GZIP_LEVEL)
    meta_path.write_text(json.dumps(meta, indent=2))

    print("-" * 80)
    print(f"Mask written to {args.output}")
    print(f"Settings written to {meta_path}")

    # Low-res copy of the mask, e.g. for a quick look at the whole volume
    downsample_h5(args.output, dataset=DATASET, factor=DOWNSAMPLING)
    print(f"Done. Low-res copy written to {low_res}")


if __name__ == "__main__":
    main()
