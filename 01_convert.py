import argparse
import json
import os
import sys
import threading
import zlib
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import h5py
import numpy as np
from tqdm import tqdm


TARGET_INPUT_RESOLUTION_LEVEL = 0
GZIP_LEVEL = 2              # output compression (1 = fastest, 9 = smallest)
N_WORKERS = os.cpu_count()
BATCH = 32                  # chunks per worker task
MAX_PENDING = 4 * N_WORKERS # batches in flight (bounds the memory use)

local = threading.local()   # one open input file per worker thread


def data_range(f_in, channel):
    """(min, max) of one channel, measured on the coarsest resolution level.
    Coarse voxels are averages, so the brightest full-resolution voxels can lie above this max and clip at 255.
    """
    n_levels = len([name for name in f_in["DataSet"] if name.startswith("ResolutionLevel ")])
    group = f_in[f"DataSet/ResolutionLevel {n_levels - 1}/TimePoint 0/Channel {channel}"]
    # The stored data is padded up to whole chunks: cut it to the real image size
    size_z, size_y, size_x = [int(group.attrs[f"ImageSize{axis}"].tobytes()) for axis in "ZYX"]
    data = group["Data"][:size_z, :size_y, :size_x]
    return int(data.min()), int(data.max())


def lookup_table(lo, hi):
    """The uint8 value for each of the 65536 possible uint16 values.

    ImageJ's 16 -> 8 bit conversion: lo -> 0, hi -> 255, linear in between, clipped outside.
    """
    values = np.arange(2**16)
    scaled = np.floor(np.clip(values - lo, 0, None) * (256.0 / (hi - lo + 1)) + 0.5)
    return np.clip(scaled, 0, 255).astype(np.uint8)


def open_input(path):
    local.file = open(path, "rb")


def convert_chunk(position, size, lut):
    """Read one compressed uint16 chunk from the file -> compressed uint8 chunk (None if all zero)."""
    local.file.seek(position)
    payload = local.file.read(size)
    raw = np.frombuffer(zlib.decompress(payload, bufsize=2 * 64 ** 3), dtype=np.uint8)
    low, high = raw.reshape(2, -1)            # shuffle stores all low bytes, then all high bytes
    values = low | (high.astype(np.uint16) << 8)
    out = lut[values]
    return zlib.compress(out.tobytes(), GZIP_LEVEL) if out.any() else None


def convert_batch(batch, lut):
    return [(offset, convert_chunk(position, size, lut)) for position, size, offset in batch]


def convert_channel(src, dst, lut):
    """Convert one input dataset (uint16) into the output dataset (uint8), chunk by chunk."""

    # Collect data on every chunk of the input image
    chunks = []
    src.id.chunk_iter(lambda c: chunks.append((c.byte_offset, c.size, c.chunk_offset)))
    # Sort via byte offset
    chunks.sort()

    # Split chunks into batches
    batches = [chunks[i:i + BATCH] for i in range(0, len(chunks), BATCH)]

    # Manage running batches
    pending = deque()

    # Progress bar over written chunks, redrawn in place
    # (on stdout like print: keeps the order, and stderr would show in red)
    bar = tqdm(total=len(chunks), unit="chunk", desc="Converting", file=sys.stdout)

    # Write a ready-to-be-written chunk back to memory
    def write_oldest():
        # .result() waits in case not ready yet
        results = pending.popleft().result()
        for offset, data in results:
            if data is not None:
                dst.id.write_direct_chunk(offset, data)
        bar.update(len(results))

    # Each channel lives in its own file (the .ims only links to it), so every channel gets a new pool
    with bar, ThreadPoolExecutor(N_WORKERS, initializer=open_input, initargs=(src.file.filename,)) as pool:

        # Work through all batches
        for batch in batches:

            # Submit next batch to the next free thread
            pending.append(pool.submit(convert_batch, batch, lut))

            # Make sure not too many running batches are submitted at once
            if len(pending) > MAX_PENDING:
                write_oldest()

        # Write all chunks of remaining batches to file
        while pending:
            write_oldest()


def main():
    parser = argparse.ArgumentParser(description="Copy all .ims channels into 8-bit, chunked, gzip .h5 files "
                                                 "for ilastik, one file per channel")
    parser.add_argument("--input", type=Path, required=True, help="input .ims file")
    parser.add_argument("--output", type=Path, required=True,
                        help="output name, e.g. out.h5 -> out_channel0.h5, out_channel1.h5, ...")
    parser.add_argument("--range", type=int, nargs=3, action="append", default=[],
                        metavar=("CHANNEL", "LO", "HI"),
                        help="fixed 8-bit window for one channel, e.g. --range 0 0 30000 (repeat for more "
                             "channels); channels without --range use min/max of the coarsest level")
    args = parser.parse_args()

    # Manual 8-bit windows: {channel: (lo, hi)}
    manual_ranges = {channel: (lo, hi) for channel, lo, hi in args.range}

    # Check input
    if not args.input.is_file():
        raise SystemExit(f"Input not found: {args.input}")
    for channel, (lo, hi) in manual_ranges.items():
        if hi <= lo:
            raise SystemExit(f"--range {channel} {lo} {hi}: HI must be larger than LO")

    with h5py.File(args.input, "r") as f_in:

        timepoint = f_in[f"DataSet/ResolutionLevel {TARGET_INPUT_RESOLUTION_LEVEL}/TimePoint 0"]
        n_channels = len([name for name in timepoint if name.startswith("Channel ")])

        unknown = [channel for channel in manual_ranges if not 0 <= channel < n_channels]
        if unknown:
            raise SystemExit(f"--range for channel(s) {unknown}, but the input only has channels 0..{n_channels - 1}")

        # One output file per channel, e.g. out.h5 -> out_channel0.h5
        outputs = [args.output.with_name(f"{args.output.stem}_channel{channel}.h5") for channel in range(n_channels)]

        # Check output
        existing = [path for path in outputs if path.exists()]
        if existing:
            print("Output already exists:")
            for path in existing:
                print(f"  {path}")
            answer = input("Overwrite? [y/N] ")
            if answer.strip().lower() != "y":
                raise SystemExit("Aborted, nothing written.")

        print("-" * 80)
        print(f"Input: {args.input}")
        print("Output:")
        for path in outputs:
            print(f"  {path}")
        print(f"Converting {n_channels} channels.")

        # Settings of this run, saved next to the output
        meta = dict(resolution_level=TARGET_INPUT_RESOLUTION_LEVEL, gzip_level=GZIP_LEVEL, channels={})

        for channel, out_path in enumerate(outputs):

            # Define input file
            src = timepoint[f"Channel {channel}/Data"]

            # Syntax checks
            if src.dtype != np.uint16 or not src.shuffle or src.compression != "gzip":
                raise SystemExit("input expected to be uint16 with shuffle + gzip")

            # 8-bit window of this channel: given via --range, or min/max of the coarsest level
            if channel in manual_ranges:
                lo, hi = manual_ranges[channel]
                range_source = "manual"
            else:
                lo, hi = data_range(f_in, channel)
                range_source = "min/max"

            print("-" * 80)
            print(f"Channel {channel}")
            print(f"Shape: {src.shape} (z, y, x)")
            print(f"Chunk-size: {src.chunks} (z, y, x)")
            print(f"8-bit window: {lo} .. {hi} ({range_source})")

            # Create output file with one dataset
            with h5py.File(out_path, "w") as f_out:
                dst = f_out.create_dataset(
                    "data",
                    shape=src.shape,
                    dtype=np.uint8,
                    chunks=src.chunks,
                    compression="gzip",
                    compression_opts=GZIP_LEVEL
                )
                dst.attrs["range"] = [lo, hi]

                convert_channel(src, dst, lookup_table(lo, hi))

            meta["channels"][f"channel{channel}"] = dict(file=out_path.name, lo=lo, hi=hi, range=range_source)

    # e.g. out.h5 -> out-meta.json
    meta_path = args.output.with_name(f"{args.output.stem}-meta.json")
    meta_path.write_text(json.dumps(meta, indent=2))

    print("-" * 80)
    print("Done. Written to:")
    for path in outputs:
        print(f"  {path}")
    print(f"Settings written to {meta_path}")


if __name__ == "__main__":
    main()
