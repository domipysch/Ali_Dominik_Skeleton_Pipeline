import argparse
import itertools
import json
import sys
from pathlib import Path
import h5py
import numpy as np
from tqdm import tqdm


INPUT_DATASET = "exported_data"     # dataset in the probability .h5 (as written by run_ilastik_headless.py)
OUTPUT_DATASET = "data"             # dataset in the mask .h5 (same name as convert.py)
GZIP_LEVEL = 2                      # output compression (1 = fastest, 9 = smallest)
DEFAULT_CHUNKS = (64, 64, 64)       # output chunks if the input is not chunked


def blocks(shape, block):
    """Slices (z, y, x) that tile a volume of `shape` in blocks of `block` (smaller at the borders)."""
    starts = [range(0, size, step) for size, step in zip(shape, block)]
    for start in itertools.product(*starts):
        yield tuple(slice(s, min(s + b, size)) for s, b, size in zip(start, block, shape))


def main():
    parser = argparse.ArgumentParser(description="Threshold an ilastik probability map (.h5, uint8) "
                                                 "into a binary 0/1 mask (.h5)")
    parser.add_argument("--input", type=Path, required=True, help="probability .h5 from run_ilastik_headless.py")
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
    existing = [path for path in (args.output, meta_path) if path.exists()]
    if existing:
        print("Output already exists:")
        for path in existing:
            print(f"  {path}")
        answer = input("Overwrite? [y/N] ")
        if answer.strip().lower() != "y":
            raise SystemExit("Aborted, nothing written.")
    args.output.parent.mkdir(parents=True, exist_ok=True)

    with h5py.File(args.input, "r") as f_in:
        src = f_in[INPUT_DATASET]

        # ilastik writes (z, y, x, c); only a single (foreground) channel can be thresholded
        if src.ndim == 4:
            if src.shape[3] != 1:
                raise SystemExit(f"Input has {src.shape[3]} channels, expected 1 (the foreground probability). "
                                 f"Export only that channel in run_ilastik_headless.py.")
            channel = (0,)
        elif src.ndim == 3:
            channel = ()
        else:
            raise SystemExit(f"Expected a (z, y, x) or (z, y, x, 1) dataset, got shape {src.shape}")
        if src.dtype != np.uint8:
            raise SystemExit(f"Expected uint8 probabilities (0..255), got {src.dtype}")

        shape = src.shape[:3]
        chunks = src.chunks[:3] if src.chunks else DEFAULT_CHUNKS

        print("-" * 80)
        print(f"Input:  {args.input}/{INPUT_DATASET}")
        print(f"Output: {args.output}/{OUTPUT_DATASET}")
        print(f"Shape: {shape} (z, y, x), chunks {chunks}")
        print(f"Mask: probability >= {args.threshold} -> 1, else 0")
        print("-" * 80)

        with h5py.File(args.output, "w") as f_out:
            dst = f_out.create_dataset(
                OUTPUT_DATASET,
                shape=shape,
                dtype=np.uint8,
                chunks=chunks,
                compression="gzip",
                compression_opts=GZIP_LEVEL
            )
            dst.attrs["threshold"] = args.threshold

            # Block by block (one chunk each), so the whole volume never has to fit in memory
            all_blocks = list(blocks(shape, chunks))
            for region in tqdm(all_blocks, unit="chunk", desc="Thresholding", file=sys.stdout):
                mask = (src[region + channel] >= args.threshold).astype(np.uint8)
                # Empty chunks are not written at all: they read back as 0 (the fill value) and take no space
                if mask.any():
                    dst[region] = mask

    # Settings of this run, saved next to the output
    meta = dict(input=str(args.input), threshold=args.threshold, gzip_level=GZIP_LEVEL)
    meta_path.write_text(json.dumps(meta, indent=2))

    print("-" * 80)
    print(f"Done. Mask written to {args.output}.")
    print(f"Settings written to {meta_path}")


if __name__ == "__main__":
    main()
