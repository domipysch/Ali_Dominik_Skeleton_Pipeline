import argparse
import json
from pathlib import Path
import h5py
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import tifffile
from matplotlib.patches import Rectangle
from scipy import ndimage
from skimage.filters import threshold_otsu


CROP_SHAPE = (200, 500, 500)     # (z, y, x) voxels per crop
SEED = None                      # same seed -> same crops ("controls randomness")
DATASET = "data"                 # dataset name in the input .h5 (as written by convert.py)
PREVIEW_LEVEL = 3                # .ims level the whole-image projection in the overview image is made from

# Tissue region: crops are only taken where the tissue is
MASK_CHANNEL = 0                # .ims channel for the tissue mask and the overview (use the channel of --input)
BLUR_SIGMA = 2.0                # blur before Otsu, in voxels of the coarsest level
MIN_TISSUE_FRACTION = 0.9       # a crop needs at least this fraction inside the tissue mask
MAX_TRIES = 10000               # random positions tried per crop before giving up (cheap: only the mask is checked)

EXTENSIONS = {"h5": ".h5", "tif": ".tif"}     # --output_filetype -> file extension of the crops


def read_ims_level(ims_path, level, channel):
    """One resolution level of one channel of the .ims. level=None means the coarsest. Returns (data, level)."""
    with h5py.File(ims_path, "r") as f:
        n_levels = len([name for name in f["DataSet"] if name.startswith("ResolutionLevel ")])
        level = n_levels - 1 if level is None else min(level, n_levels - 1)
        group = f[f"DataSet/ResolutionLevel {level}/TimePoint 0/Channel {channel}"]
        # The stored data is padded up to whole chunks: cut it to the real image size
        size_z, size_y, size_x = [int(group.attrs[f"ImageSize{axis}"].tobytes()) for axis in "ZYX"]
        return group["Data"][:size_z, :size_y, :size_x], level


def tissue_mask(ims_path, channel):
    """Tissue mask on the coarsest level of the .ims: blur, Otsu, largest connected component, closing.
    Returns (mask, Otsu threshold, level).
    """
    data, level = read_ims_level(ims_path, None, channel)

    smooth = ndimage.gaussian_filter(data.astype(np.float32), BLUR_SIGMA)
    threshold = float(threshold_otsu(smooth))
    mask = smooth > threshold

    # Keep only the largest connected piece (the tissue), drop specks
    labels, n = ndimage.label(mask)
    if n == 0:
        raise SystemExit("No tissue found in the coarsest level")
    sizes = np.bincount(labels.ravel())[1:]     # voxels per component, label 0 is background
    mask = labels == 1 + int(np.argmax(sizes))
    mask = ndimage.binary_closing(mask, structure=np.ones((3, 3, 3)))
    return mask, threshold, level


def tissue_fraction(mask, scale, start, stop):
    """Fraction of the crop [start, stop) that lies inside the mask.
    """
    lo = [int(np.floor(s * f)) for s, f in zip(start, scale)]
    hi = [max(l + 1, int(np.ceil(e * f))) for l, e, f in zip(lo, stop, scale)]
    return float(mask[lo[0]:hi[0], lo[1]:hi[1], lo[2]:hi[2]].mean())


def find_crop(shape, mask, scale, rng):
    """Random crop inside the image with at least MIN_TISSUE_FRACTION in the tissue: (start, stop, fraction)."""
    for size, crop in zip(shape, CROP_SHAPE):
        if size < crop:
            raise SystemExit(f"Image too small: {size} voxels cannot hold a crop of {crop}")

    for tries in range(1, MAX_TRIES + 1):
        start = [int(rng.integers(0, size - crop + 1)) for size, crop in zip(shape, CROP_SHAPE)]
        stop = [s + c for s, c in zip(start, CROP_SHAPE)]
        fraction = tissue_fraction(mask, scale, start, stop)
        if fraction >= MIN_TISSUE_FRACTION:
            return start, stop, fraction
    raise SystemExit(f"No crop with {MIN_TISSUE_FRACTION:.0%} inside the tissue found in {MAX_TRIES} tries. "
                     f"Lower MIN_TISSUE_FRACTION or use smaller crops.")


def save_crop(crop, path, filetype):
    """Write one (z, y, x) crop as .h5 (dataset "data", gzip) or as a zlib-compressed ImageJ z-stack .tif."""
    if filetype == "h5":
        with h5py.File(path, "w") as f_out:
            f_out.create_dataset("data", data=crop, compression="gzip")
    else:
        tifffile.imwrite(path, crop, imagej=True, metadata={"axes": "ZYX"}, compression="zlib")


def save_overview(overview, shape, crops, path):
    """Maximum projection along z of the whole image with every crop drawn in its own colour and labelled.

    overview = z projection of the whole image (from a coarse .ims level), shown stretched to the
    full-resolution size `shape`, so all axes are in full-resolution voxels.
    crops = list of (name, start, stop) with start/stop in (z, y, x).
    """
    height, width = shape[1], shape[2]
    fig, ax = plt.subplots(figsize=(12, 12 * height / width + 1))
    ax.imshow(overview, cmap="gray", extent=(0, width, height, 0))

    colors = plt.cm.tab10.colors        # 10 distinct colours, repeated for more crops (labels tell them apart)
    for i, (name, start, stop) in enumerate(crops):
        color = colors[i % len(colors)]
        ax.add_patch(Rectangle((start[2], start[1]), stop[2] - start[2], stop[1] - start[1],
                               fill=False, edgecolor=color, linewidth=1.5))
        # Label just above the top-left corner of the crop
        ax.text(start[2], start[1], name, color=color, fontsize=13, fontweight="bold", va="bottom", ha="left")

    ax.set(title=f"{len(crops)} crops, max projection along z", xlabel="x", ylabel="y")
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description="Cut random crops out of the tissue region of an .h5 volume")
    parser.add_argument("--input", type=Path, required=True, help="input .h5 file (e.g. from convert.py)")
    parser.add_argument("--ims", type=Path, required=True,
                        help="the .ims the .h5 was converted from (its coarsest level gives the tissue region)")
    parser.add_argument("--output", type=Path, required=True, help="output folder for the crops")
    parser.add_argument("--n-crops", type=int, required=True, help="number of crops to generate")
    parser.add_argument("--output_filetype", choices=list(EXTENSIONS), default="h5",
                        help="file type of the crops: h5 (default) or tif")
    args = parser.parse_args()

    # Check input and output
    for path in (args.input, args.ims):
        if not path.is_file():
            raise SystemExit(f"Input not found: {path}")
    if args.n_crops < 1:
        raise SystemExit("--n-crops must be at least 1")
    names = [f"crop_{i:03d}" for i in range(args.n_crops)]
    outputs = [args.output / f"{name}{EXTENSIONS[args.output_filetype]}" for name in names]
    meta_path = args.output / "meta.json"
    overview_path = args.output / "crops_overview.png"
    existing = [path for path in outputs + [meta_path, overview_path] if path.exists()]
    if existing:
        print(f"{len(existing)} output files already exist in {args.output}")
        answer = input("Overwrite? [y/N] ")
        if answer.strip().lower() != "y":
            raise SystemExit("Aborted, nothing written.")
    args.output.mkdir(parents=True, exist_ok=True)

    # Print user info
    print("-" * 80)
    print(f"Input: {args.input}")
    print(f"Tissue region from: {args.ims} (channel {MASK_CHANNEL})")
    print(f"Output: {args.output} ({args.output_filetype})")
    print(f"Creating {args.n_crops} crops of {CROP_SHAPE} (z, y, x) with at least {MIN_TISSUE_FRACTION:.0%} overlap with tissue")
    print("-" * 80)

    rng = np.random.default_rng(SEED)

    # Get estimate for binary mask of tissue at coarse level
    mask, otsu, level = tissue_mask(args.ims, MASK_CHANNEL)

    # Overview image: z projection of the whole image
    preview_data, preview_level = read_ims_level(args.ims, PREVIEW_LEVEL, MASK_CHANNEL)
    overview = preview_data.max(axis=0)

    with h5py.File(args.input, "r") as f_in:
        src = f_in[DATASET]
        print(f"Input Image Size: {src.shape} (z, y, x), {src.dtype}")

        meta = dict(
            input=str(args.input), ims=str(args.ims), dataset=DATASET, seed=SEED,
            output_filetype=args.output_filetype,
            crop_shape=list(CROP_SHAPE), mask_channel=MASK_CHANNEL, mask_level=level,
            mask_shape=list(mask.shape), otsu_threshold=otsu, blur_sigma=BLUR_SIGMA,
            min_tissue_fraction=MIN_TISSUE_FRACTION, crops=[]
        )

        # Mask voxels per image voxel, per axis (both cover the same physical volume)
        scale = [m / n for m, n in zip(mask.shape, src.shape)]

        for name, output in zip(names, outputs):
            start, stop, fraction = find_crop(src.shape, mask, scale, rng)
            crop = src[start[0]:stop[0], start[1]:stop[1], start[2]:stop[2]]
            save_crop(crop, output, args.output_filetype)

            meta["crops"].append(dict(name=name, start=start, stop=stop, tissue_fraction=fraction))
            print(f"{name}: start {start}, stop {stop} (z, y, x)")

        shape = src.shape

    # Coordinates first: they belong to the crops just written, even if the image below fails
    meta_path.write_text(json.dumps(meta, indent=2))

    print("-" * 80)
    print(f"Done. {args.n_crops} crops written to {args.output}")
    print(f"Coordinates written to {meta_path}")

    crops = [(c["name"], c["start"], c["stop"]) for c in meta["crops"]]
    try:
        save_overview(overview, shape, crops, overview_path)
        print(f"Overview image written to {overview_path}")
    except OSError as error:
        # On Windows typically: the old image is still open in a viewer, which blocks overwriting it
        print(f"Could not write the overview image ({error}). Is it open in an image viewer? "
              f"Close it and run again; crops and meta.json are already written.")


if __name__ == "__main__":
    main()
