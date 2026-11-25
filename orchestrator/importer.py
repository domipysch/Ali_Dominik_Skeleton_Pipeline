"""Import results made outside the GUI (e.g. before it existed) as finished runs of one branch.

For each step the user points at what that step produced (a file or a folder). It is copied (or moved) into a new
run folder under the name the later steps look for, and the run.json is written: status done, marked as imported,
continuing the previous imported step. There is no log and no command for such runs.
"""
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .ims import COPY_CHUNK, FREE_SPACE_MARGIN, format_bytes
from .steps import STEP_INDEX, STEPS, input_options, make_context
from .store import DONE, now


@dataclass
class Target:
    kind: str                   # file, dir, or none (only values, e.g. the chosen thresholds)
    name: str = ""              # name in the run folder; {channel} is replaced
    suffixes: tuple = ()        # file types that can be chosen (file targets)


# What each step's run folder must contain for the later steps (see the inputs in steps.py)
TARGETS = {
    "convert": Target("file", "converted_channel{channel}.h5", (".h5",)),
    "isotropic": Target("file", "isotropic.h5", (".h5",)),
    "crops_training": Target("dir", "crops"),
    "ilastik_training": Target("file", "project.ilp", (".ilp",)),
    "ilastik_prediction": Target("file", "prob.h5", (".h5",)),
    "gaussian": Target("file", "smoothed.h5", (".h5",)),
    "crops_viewing": Target("dir", "crops"),
    "threshold": Target("file", "mask.h5", (".h5",)),
    "lcc": Target("file", "lcc.h5", (".h5",)),
    "maskel": Target("dir", "output"),
}

# Parameters not worth recording for an imported run
SKIP_PARAMS = {"ilastik"}


@dataclass
class Item:
    """One step to import."""
    step: str
    source: Path | None = None      # the file / folder that step produced (None: a skipped step, or only values)
    params: dict = field(default_factory=dict)
    results: dict = field(default_factory=dict)
    skipped: bool = False           # an optional step that was not done (e.g. no resampling)


def files_to_copy(step_key, source, channel):
    """[(source file or folder, name in the run folder)] for one step: just the chosen file or folder (files next
    to it, e.g. old downsampled _res_16x16x16.h5 copies, are not imported)."""
    target = TARGETS[step_key]
    if target.kind == "none" or source is None:
        return []
    return [(Path(source), target.name.format(channel=channel))]


def size_of(path):
    path = Path(path)
    if path.is_file():
        return path.stat().st_size
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def check(sample, channel, items):
    """Problems that keep the import from starting (list of messages)."""
    problems = []
    if channel not in [c["index"] for c in sample.channels]:
        problems.append(f"{sample.name} has no channel {channel}")
    if not items:
        problems.append("Nothing to import: choose the result of at least one step")
    keys = [item.step for item in items]
    if len(set(keys)) != len(keys):
        problems.append("Each step can be imported only once")
    for item in items:
        step, target = STEPS[item.step], TARGETS[item.step]
        if step.coming_soon:
            problems.append(f"{step.title} is coming soon: it cannot be imported yet")
            continue
        if item.skipped:
            if not step.optional:
                problems.append(f"{step.title} cannot be skipped")
            continue
        if target.kind == "none":
            for param in step.results:
                if item.results.get(param.name) is None:
                    problems.append(f"{step.title}: enter {param.label}")
            continue
        if item.source is None:
            problems.append(f"{step.title}: choose the {'folder' if target.kind == 'dir' else 'file'}")
            continue
        source = Path(item.source)
        if target.kind == "dir" and not source.is_dir():
            problems.append(f"{step.title}: not a folder: {source}")
        elif target.kind == "file" and not source.is_file():
            problems.append(f"{step.title}: not a file: {source}")
        elif target.kind == "file" and target.suffixes and source.suffix.lower() not in target.suffixes:
            problems.append(f"{step.title}: needs a {' or '.join(target.suffixes)} file, not {source.name}")
    return problems


def import_branch(sample, channel, items, move=False, progress=None):
    """Make one finished run per item, each continuing the one before: a new branch of the channel.

    items: Item list (any order; sorted into pipeline order). move: move the files instead of copying them.
    progress(done_bytes, total_bytes) is called while copying. Returns the new runs. On an error the runs made so far
    are removed again (moved files are moved back as far as possible).
    """
    items = sorted(items, key=lambda item: STEP_INDEX[item.step])
    problems = check(sample, channel, items)
    if problems:
        raise ValueError("\n".join(problems))

    plan = {item.step: files_to_copy(item.step, None if item.skipped else item.source, channel) for item in items}
    total = sum(size_of(source) for files in plan.values() for source, _ in files)
    if not move:
        free = shutil.disk_usage(sample.path).free
        if total + FREE_SPACE_MARGIN > free:
            raise ValueError(f"Not enough space: {format_bytes(total)} to copy, {format_bytes(free)} free")

    made, moved = [], []
    done = 0
    try:
        parent = None
        for item in items:
            step = STEPS[item.step]
            params = {name: value for name, value in item.params.items()
                      if value is not None and name not in SKIP_PARAMS}
            chosen = {}
            if parent is not None:
                for spec in step.inputs:
                    options = input_options(sample, spec, parent)
                    if options:
                        chosen[spec.name] = options[0]
            if item.skipped:
                label = "skipped"
            else:
                try:
                    label = step.label(make_context(sample, step, params, chosen, channel=channel, parent=parent))
                except (KeyError, TypeError, ValueError):       # parameters not given
                    label = ""
            sources = [str(source) for source, _ in plan[item.step]]
            data = dict(status=DONE, imported=True, parent=parent.rel_path if parent else None, params=params,
                        inputs={name: dict(path=o.get("path"), run=o.get("run")) for name, o in chosen.items()},
                        results=item.results, notes="", finished=now())
            if item.skipped:
                data["skipped"] = True
            if sources:
                data["imported_from"] = sources[0] if len(sources) == 1 else sources
            run = sample.new_run(channel, step.key, label or "imported", data)
            made.append(run)
            for source, name in plan[item.step]:
                target = run.path / name
                if move:
                    shutil.move(str(source), str(target))
                    moved.append((target, source))
                    done += size_of(target)
                    if progress:
                        progress(done, total)
                else:
                    done = copy_with_progress(source, target, done, total, progress)
            parent = run
        return made
    except BaseException:
        for target, source in reversed(moved):
            try:
                shutil.move(str(target), str(source))
            except OSError:
                pass
        for run in reversed(made):
            shutil.rmtree(run.path, ignore_errors=True)
        raise


def copy_with_progress(source, target, done, total, progress):
    """Copy a file or a folder (recursively). Returns the bytes copied so far."""
    source, target = Path(source), Path(target)
    if source.is_dir():
        target.mkdir(parents=True, exist_ok=True)
        for child in sorted(source.iterdir()):
            done = copy_with_progress(child, target / child.name, done, total, progress)
        return done
    partial = target.with_name(target.name + ".partial")
    with open(source, "rb") as src, open(partial, "wb") as dst:
        while chunk := src.read(COPY_CHUNK):
            dst.write(chunk)
            done += len(chunk)
            if progress:
                progress(done, total)
    shutil.copystat(source, partial)
    os.replace(partial, target)
    return done
