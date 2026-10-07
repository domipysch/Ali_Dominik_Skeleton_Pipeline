import argparse
import glob
import os
import subprocess
import sys
from pathlib import Path


DATASET = "data"            # dataset inside the input .h5 (as written by convert.py)
AXES = "zyx"                # its axis order
N_THREADS = os.cpu_count()  # ilastik worker threads
# ilastik exports one probability channel per label, in the order the labels appear in the project
# (label 1 -> channel 0, ...). Only this one is kept; with two labels the other is just 1 - it.
FOREGROUND_CHANNEL = 0

# Where Windows installers put ilastik
ILASTIK_GLOBS = [
    r"C:\Program Files\ilastik-*\ilastik.exe",
    r"C:\Program Files (x86)\ilastik-*\ilastik.exe",
]

# ilastik can log one of these and still exit with code 0, leaving a broken output file
FATAL_MARKERS = ("Project could not be loaded", "Unhandled exception in thread", "Couldn't delete link")


def find_ilastik():
    """Newest ilastik.exe in the standard install folders, or None."""
    for pattern in ILASTIK_GLOBS:
        hits = sorted(glob.glob(pattern))
        if hits:
            return Path(hits[-1])
    return None


def main():
    parser = argparse.ArgumentParser(description="Run a trained ilastik pixel classification project headless "
                                                 "and export the probability map to .h5")
    parser.add_argument("--project", type=Path, required=True, help="trained .ilp project")
    parser.add_argument("--input", type=Path, required=True, help="input .h5 (e.g. from convert.py)")
    parser.add_argument("--output", type=Path, required=True, help="output probability .h5")
    parser.add_argument("--ilastik", type=Path, default=find_ilastik(),
                        help="path to ilastik.exe (default: newest in C:\\Program Files)")
    args = parser.parse_args()

    # Check input
    if args.ilastik is None or not args.ilastik.is_file():
        raise SystemExit(f"ilastik.exe not found ({args.ilastik}), pass it with --ilastik")
    for path in (args.project, args.input):
        if not path.is_file():
            raise SystemExit(f"Not found: {path}")

    # Check output: ilastik fails on an existing (possibly broken) output file, so remove it first
    if args.output.exists():
        answer = input(f"Output already exists: {args.output}\nOverwrite? [y/N] ")
        if answer.strip().lower() != "y":
            raise SystemExit("Aborted, nothing written.")
        args.output.unlink()
    args.output.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        str(args.ilastik),
        "--headless",
        f"--project={args.project.as_posix()}",
        "--readonly",
        "--export_source=Probabilities",
        "--output_format=hdf5",
        f"--output_filename_format={args.output.as_posix()}",
        "--output_internal_path=exported_data",
        f"--input_axes={AXES}",
        # Output axes are AXES + "c": keep the whole volume, but only the foreground channel
        f"--cutout_subregion=[(None,None,None,{FOREGROUND_CHANNEL}),(None,None,None,{FOREGROUND_CHANNEL + 1})]",
        # Probabilities as uint8 0..255 instead of float32 0..1 (4x smaller)
        "--export_dtype=uint8",
        "--pipeline_result_drange=(0.0,1.0)",
        "--export_drange=(0,255)",
        f"--raw_data={args.input.as_posix()}/{DATASET}",
    ]

    env = os.environ.copy()
    env["LAZYFLOW_THREADS"] = str(N_THREADS)
    # Don't let a project that is open in the ilastik GUI block the read
    env["HDF5_USE_FILE_LOCKING"] = "FALSE"

    print("-" * 80)
    print(f"Project: {args.project}")
    print(f"Input:   {args.input}/{DATASET} ({AXES})")
    print(f"Output:  {args.output}")
    print("-" * 80)

    # Pass ilastik's log through to the console, watching for fatal messages
    fatal = None
    with subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, errors="replace", bufsize=1) as proc:
        for line in proc.stdout:
            sys.stdout.write(line)
            fatal = fatal or next((m for m in FATAL_MARKERS if m in line), None)

    print("-" * 80)
    if proc.returncode != 0 or fatal:
        raise SystemExit(f"ilastik failed (exit {proc.returncode}{', logged ' + repr(fatal) if fatal else ''})")
    if not args.output.exists():
        raise SystemExit("ilastik exited without error but wrote no output")
    print(f"Done. Probabilities written to {args.output} ({args.output.stat().st_size / 1e9:.1f} GB)")


if __name__ == "__main__":
    main()
