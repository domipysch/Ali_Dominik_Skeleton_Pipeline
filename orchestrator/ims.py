"""An .ims file and the files it links to.

A Luxendo .ims is often only a small wrapper: its image data are HDF5 external links into other files
(e.g. uni_tp-0_ch-1_...lux.h5) next to it. Copying the .ims alone breaks it, so the files it links to are copied
with it, keeping their place relative to the .ims (that is how HDF5 finds them again).
"""
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath

import h5py

COPY_CHUNK = 16 * 1024 * 1024       # bytes read / written at once when copying
FREE_SPACE_MARGIN = 1024 ** 3       # keep at least this much free on the target drive


@dataclass
class Channel:
    index: int                  # Imaris channel number (DataSet/.../Channel <index>)
    name: str                   # DataSetInfo/Channel <index> "Name", e.g. 'chan:1:lyve-1 -570 stack:... cam:long'
    file: Path | None = None    # the linked file with its data (relative to the .ims folder), None if inside the .ims

    @property
    def label(self):
        """The name without the acquisition details: 'chan:1:lyve-1 -570'."""
        return short_channel_name(self.name) or f"Channel {self.index}"


def short_channel_name(name):
    """Luxendo names a channel 'chan:<laser channel>:<marker> stack:... obj:... cam:...': keep the part before stack."""
    return (name or "").split(" stack:")[0].strip()


@dataclass
class Bundle:
    ims: Path                                   # the .ims
    files: list = field(default_factory=list)   # [(source Path, path relative to the .ims folder)], .ims first
    problems: list = field(default_factory=list)  # why it cannot be copied (empty: fine)
    channels: list = field(default_factory=list)  # [Channel], in the order of the .ims

    @property
    def size(self):
        return sum(source.stat().st_size for source, _ in self.files if source.is_file())


def external_links(path):
    """{link path in the file: file name it points to} of all external links in an HDF5 file."""
    links = {}

    def visit(name, link):
        if isinstance(link, h5py.ExternalLink):
            links[name] = link.filename

    with h5py.File(path, "r") as f:
        f.visititems_links(visit)
    return links


def attr_text(value):
    """Imaris stores text attributes as arrays of single bytes."""
    try:
        return value.tobytes().decode("utf-8", errors="replace") if hasattr(value, "tobytes") else str(value)
    except Exception:
        return str(value)


def read_channels(path, links=None):
    """[Channel] of an .ims: number, name and the file holding its full-resolution data."""
    links = external_links(path) if links is None else links
    with h5py.File(path, "r") as f:
        timepoint = f["DataSet/ResolutionLevel 0/TimePoint 0"]
        indices = sorted(int(name.split()[-1]) for name in timepoint if name.startswith("Channel "))
        out = []
        for index in indices:
            info = f.get(f"DataSetInfo/Channel {index}")
            name = attr_text(info.attrs["Name"]) if info is not None and "Name" in info.attrs else ""
            target = links.get(f"DataSet/ResolutionLevel 0/TimePoint 0/Channel {index}/Data")
            file = Path(*(PureWindowsPath(target) if "\\" in target else PurePosixPath(target)).parts) \
                if target else None
            out.append(Channel(index, name.strip(), file))
    return out


def bundle(ims):
    """The .ims plus every file its external links point to, with any problem that prevents copying them."""
    ims = Path(ims)
    result = Bundle(ims=ims)
    if not ims.is_file():
        result.problems.append(f"Not found: {ims}")
        return result
    if ims.suffix.lower() != ".ims":
        result.problems.append(f"Not an .ims file: {ims.name}")
        return result
    try:
        links = external_links(ims)
    except OSError as error:
        result.problems.append(f"Cannot read {ims.name} as an .ims (HDF5) file: {error}")
        return result

    result.files.append((ims, Path(ims.name)))
    seen = set()
    for target in sorted(set(links.values())):
        # Link targets are written by whatever made the .ims, so either slash may appear
        rel = PureWindowsPath(target) if "\\" in target else PurePosixPath(target)
        if rel.is_absolute() or rel.drive or ".." in rel.parts:
            result.problems.append(f"Links to {target}, which is not next to the .ims: "
                                   f"a copy would still read the original there")
            continue
        rel = Path(*rel.parts)
        source = ims.parent / rel
        if not source.is_file():
            result.problems.append(f"Linked file missing: {rel} (expected next to the .ims)")
            continue
        if rel not in seen:
            seen.add(rel)
            result.files.append((source, rel))
    if not result.problems:
        try:
            result.channels = read_channels(ims, links)
        except (OSError, KeyError) as error:
            result.problems.append(f"Cannot read the channels of {ims.name}: {error}")
        else:
            if not result.channels:
                result.problems.append(f"{ims.name} has no channels")
    return result


def copy_bundle(found, target_dir, progress=None):
    """Copy the .ims and its linked files into target_dir (same layout). progress(done_bytes, total_bytes).

    Returns the path of the copied .ims. Checks free space before and that the copy reads its own files after.
    """
    if found.problems:
        raise ValueError("; ".join(found.problems))
    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    total = found.size
    free = shutil.disk_usage(target_dir).free
    if total + FREE_SPACE_MARGIN > free:
        raise ValueError(f"Not enough space: {format_bytes(total)} to copy, {format_bytes(free)} free")

    done = 0
    for source, rel in found.files:
        target = target_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(target.name + ".partial")
        with open(source, "rb") as src, open(partial, "wb") as dst:
            while chunk := src.read(COPY_CHUNK):
                dst.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
        shutil.copystat(source, partial)
        os.replace(partial, target)

    copied = target_dir / found.files[0][1]
    verify(copied)
    return copied


def verify(ims):
    """Every external link of the .ims must open a file in its own folder (not the original's)."""
    ims = Path(ims)
    folder = ims.parent.resolve()
    with h5py.File(ims, "r") as f:
        for name in external_links(ims):
            try:
                opened = Path(f[name].file.filename).resolve()
            except (KeyError, OSError) as error:
                raise ValueError(f"The copied .ims cannot open {name}: {error}")
            if not opened.is_relative_to(folder):
                raise ValueError(f"The copied .ims reads {name} from {opened}, not from its own folder")


def format_bytes(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000
