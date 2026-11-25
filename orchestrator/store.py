"""The folder structure of a pipeline project, and reading / writing its JSON files.

<root>/
    pipeline-project.json           project manifest: name, tags, structures, schema version
    samples/
        <sample id>/
            sample.json             name, tag, path of the .ims, notes, channels (index, name, structure)
            ims/<name>.ims          a copy of the .ims (and the files it links to), made when the sample is added
            channel<N>/             each channel of the .ims is processed on its own
                <step folder>/      e.g. 08_threshold
                    <run id>/       e.g. 003_low40_high120 (001_default without parameters), never overwritten
                        run.json    step, channel, parent run, parameters, inputs, status, command, timing, results
                        log.txt     everything the script printed
                        ...         the outputs of the script

Runs form a tree per channel: every run (except a convert run) has a parent, the run of the previous step it
continues. Running a step with several settings from the same parent splits the branch; each branch continues on
its own.

Everything the GUI shows is read from these files, so the project stays readable (and fixable) without the GUI.
"""
import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

SCHEMA_VERSION = 2
PROJECT_FILE = "pipeline-project.json"
SAMPLES_DIR = "samples"
SAMPLE_FILE = "sample.json"
RUN_FILE = "run.json"
LOG_FILE = "log.txt"
IMS_DIR = "ims"

REPO = Path(__file__).resolve().parent.parent     # the repository
SCRIPTS = REPO / "steps"                            # the pipeline scripts live here

DEFAULT_TAGS = ["WT", "Wildling"]
DEFAULT_STRUCTURES = ["Nerves", "Blood vessels", "Lymphatics"]

# Run status
QUEUED, RUNNING, DONE, FAILED, CANCELLED, OPEN = "queued", "running", "done", "failed", "cancelled", "open"
ACTIVE = (QUEUED, RUNNING)


def now():
    return datetime.now().isoformat(timespec="seconds")


def read_json(path):
    # Retry: on Windows a file that is being replaced can be briefly unreadable
    for attempt in range(20):
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except (PermissionError, json.JSONDecodeError):
            if attempt == 19:
                raise
            time.sleep(0.05)


def write_json(path, data):
    """Write atomically: a crash never leaves a half-written file behind."""
    path = Path(path)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    for attempt in range(20):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.05)


def update_json(path, **changes):
    """Read, change some keys, write. Re-reads first, so changes made meanwhile by another process are kept."""
    data = read_json(path)
    data.update(changes)
    write_json(path, data)
    return data


def slugify(text, max_length=60):
    """Something safe for a folder name: letters, digits, '-', '_' and '.'."""
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", text.strip()).strip("-.")
    return text[:max_length]


def git_commit():
    """Short commit hash of the pipeline scripts, with '+dirty' if they have local changes, or None."""
    try:
        commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO, capture_output=True,
                                text=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=REPO,
                               capture_output=True, text=True, check=True).stdout.strip()
        return commit + ("+dirty" if dirty else "")
    except (OSError, subprocess.CalledProcessError):
        return None


class Project:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.file = self.root / PROJECT_FILE
        self.data = read_json(self.file)

    @staticmethod
    def exists(root):
        return (Path(root) / PROJECT_FILE).is_file()

    @classmethod
    def create(cls, root, name=None):
        root = Path(root).resolve()
        root.mkdir(parents=True, exist_ok=True)
        (root / SAMPLES_DIR).mkdir(exist_ok=True)
        write_json(root / PROJECT_FILE, dict(schema_version=SCHEMA_VERSION, name=name or root.name,
                                             tags=list(DEFAULT_TAGS), structures=list(DEFAULT_STRUCTURES),
                                             created=now()))
        return cls(root)

    def save(self):
        write_json(self.file, self.data)

    @property
    def name(self):
        return self.data.get("name", self.root.name)

    @property
    def tags(self):
        return list(self.data.get("tags", []))

    def set_tags(self, tags):
        self.data["tags"] = tags
        self.save()

    def rename_tag(self, old, new):
        tags = [new if tag == old else tag for tag in self.tags]
        self.set_tags(list(dict.fromkeys(tags)))
        for sample in self.samples():
            if sample.tag == old:
                sample.update(tag=new)

    @property
    def structures(self):
        """What a channel shows: each channel of a sample gets one."""
        return list(self.data.get("structures", DEFAULT_STRUCTURES))

    def set_structures(self, structures):
        self.data["structures"] = structures
        self.save()

    def rename_structure(self, old, new):
        self.set_structures(list(dict.fromkeys(new if s == old else s for s in self.structures)))
        for sample in self.samples():
            channels = sample.all_channels
            if any(c.get("structure") == old for c in channels):
                sample.update(channels=[dict(c, structure=new) if c.get("structure") == old else c
                                        for c in channels])

    # Samples

    @property
    def samples_dir(self):
        return self.root / SAMPLES_DIR

    def samples(self):
        if not self.samples_dir.is_dir():
            return []
        found = [Sample(self, path) for path in sorted(self.samples_dir.iterdir()) if (path / SAMPLE_FILE).is_file()]
        return sorted(found, key=lambda s: s.name.lower())

    def sample(self, sample_id):
        path = self.samples_dir / sample_id
        if not (path / SAMPLE_FILE).is_file():
            raise KeyError(sample_id)
        return Sample(self, path)

    def add_sample(self, name, tag, ims, structures, notes="", progress=None):
        """Create a sample folder with a copy of the .ims and the files it links to in ims/.

        structures: {channel index: structure}, one for every channel of the .ims; None skips the channel for now
        (its data is copied all the same, as the .ims links to it; it can be given a structure later).
        progress(done_bytes, total_bytes) is called while copying.
        """
        from .ims import bundle, copy_bundle
        sample_id = slugify(name)
        if not sample_id:
            raise ValueError("The name needs at least one letter or digit")
        path = self.samples_dir / sample_id
        if path.exists():
            raise ValueError(f"A sample '{sample_id}' already exists")
        found = bundle(ims)
        if found.problems:
            raise ValueError("; ".join(found.problems))
        structures = {int(k): v for k, v in (structures or {}).items()}
        missing = [c.label for c in found.channels if c.index not in structures
                   or structures[c.index] is not None and structures[c.index] not in self.structures]
        if missing:
            raise ValueError(f"Choose a structure for {', '.join(missing)}")
        if all(structures[c.index] is None for c in found.channels):
            raise ValueError("All channels are skipped: choose a structure for at least one")
        channels = [dict(index=c.index, name=c.name, file=c.file.as_posix() if c.file else None,
                         structure=structures[c.index]) for c in found.channels]

        path.mkdir(parents=True)
        try:
            target = copy_bundle(found, path / IMS_DIR, progress)
            write_json(path / SAMPLE_FILE, dict(
                schema_version=SCHEMA_VERSION, name=name, tag=tag, ims=target.relative_to(path).as_posix(),
                ims_source=str(Path(ims).resolve()),
                ims_files=[rel.as_posix() for _, rel in found.files],
                channels=channels, notes=notes, created=now()))
        except BaseException:
            # Do not leave a half-made sample behind (e.g. an incomplete copy after an error)
            shutil.rmtree(path, ignore_errors=True)
            raise
        return Sample(self, path)

    def all_runs(self):
        return [run for sample in self.samples() for run in sample.runs()]


class Sample:
    def __init__(self, project, path):
        self.project = project
        self.path = Path(path)
        self.id = self.path.name
        self.data = read_json(self.path / SAMPLE_FILE)

    def update(self, **changes):
        self.data = update_json(self.path / SAMPLE_FILE, **changes)

    def delete(self):
        """Remove the sample folder with everything in it. sample.json goes first: a half-deleted folder (e.g. a
        file open in another program) is no sample anymore, and the rest can be removed by hand."""
        (self.path / SAMPLE_FILE).unlink()
        shutil.rmtree(self.path)

    @property
    def name(self):
        return self.data.get("name", self.id)

    @property
    def tag(self):
        return self.data.get("tag")

    @property
    def ims(self):
        """Absolute path of the .ims (stored relative to the sample folder when it lives inside it)."""
        entry = self.data.get("ims")
        if not entry:
            return None
        path = Path(entry)
        return path if path.is_absolute() else self.path / path

    @property
    def all_channels(self):
        """[dict(index, name, file, structure)], one per channel of the .ims (file: its data, in ims/).

        structure None: the channel is skipped for now (not processed, not shown)."""
        return list(self.data.get("channels", []))

    @property
    def channels(self):
        """The channels that are processed: those with a structure."""
        return [c for c in self.all_channels if c.get("structure")]

    def channel(self, index):
        return next((c for c in self.channels if c["index"] == index), None)

    def structure(self, index):
        channel = self.channel(index)
        return channel.get("structure") if channel else None

    def channel_dir(self, channel):
        return self.path / f"channel{channel}"

    def step_dir(self, channel, step_key):
        from .steps import STEPS
        return self.channel_dir(channel) / STEPS[step_key].folder

    def runs(self, step_key=None, channel=None):
        """Runs of one step (or of all steps) of one channel (or of all channels), oldest first per step."""
        from .steps import STEPS
        keys = [step_key] if step_key else list(STEPS)
        channels = [channel] if channel is not None else [c["index"] for c in self.channels]
        found = []
        for index in channels:
            for key in keys:
                folder = self.step_dir(index, key)
                if folder.is_dir():
                    found += [Run(self, path) for path in sorted(folder.iterdir()) if (path / RUN_FILE).is_file()]
        return found

    def run(self, rel_path):
        """A run from its path relative to the sample folder, e.g. 'channel0/08_threshold/003_low40_high120'."""
        return Run(self, self.path / rel_path)

    def new_run(self, channel, step_key, label, data):
        """Make a new, numbered run folder (never reusing an existing one) and write its run.json."""
        folder = self.step_dir(channel, step_key)
        folder.mkdir(parents=True, exist_ok=True)
        numbers = [int(m.group(1)) for p in folder.iterdir() if (m := re.match(r"(\d+)", p.name))]
        number = max(numbers, default=0) + 1
        suffix = slugify(label) or "default"         # e.g. 003_low40_high120; without parameters 001_default
        while True:
            name = f"{number:03d}_{suffix}"
            try:
                (folder / name).mkdir()           # fails if it exists: no two runs can get the same folder
                break
            except FileExistsError:
                number += 1
        path = folder / name
        write_json(path / RUN_FILE, dict(schema_version=SCHEMA_VERSION, step=step_key, channel=channel, id=name,
                                         created=now(), git_commit=git_commit(), **data))
        return Run(self, path)


class Run:
    def __init__(self, sample, path):
        self.sample = sample
        self.path = Path(path)
        self.id = self.path.name
        self.data = read_json(self.path / RUN_FILE)

    def reload(self):
        self.data = read_json(self.path / RUN_FILE)
        return self

    def update(self, **changes):
        self.data = update_json(self.path / RUN_FILE, **changes)

    @property
    def step(self):
        from .steps import STEPS
        return STEPS[self.data["step"]]

    @property
    def status(self):
        return self.data.get("status")

    @property
    def channel(self):
        return self.data.get("channel")

    @property
    def parent_path(self):
        """Path (relative to the sample) of the run this run continues, or None.

        Runs of steps that are no longer part of the pipeline (e.g. the former 'Choose thresholds') are passed over:
        a run that continued one of them continues the run before it.
        """
        from .steps import STEPS
        rel, seen = self.data.get("parent"), set()
        while rel and rel not in seen:
            seen.add(rel)
            file = self.sample.path / rel / RUN_FILE
            if not file.is_file():
                return rel                      # deleted: kept as it is (no parent then)
            data = read_json(file)
            if data.get("step") in STEPS:
                return rel
            rel = data.get("parent")
        return rel

    @property
    def parent(self):
        """The run this run continues (None for the first step of a branch, or if it was deleted)."""
        rel = self.parent_path
        if rel and (self.sample.path / rel / RUN_FILE).is_file():
            return self.sample.run(rel)
        return None

    def children(self, runs=None):
        """Runs that continue this run. runs: all runs of the sample, if already read."""
        runs = runs if runs is not None else self.sample.runs(channel=self.channel)
        return [run for run in runs if run.parent_path == self.rel_path]

    def descendants(self, runs=None):
        runs = runs if runs is not None else self.sample.runs(channel=self.channel)
        out = []
        for child in self.children(runs):
            out += [child] + child.descendants(runs)
        return out

    @property
    def params(self):
        return self.data.get("params", {})

    @property
    def results(self):
        return self.data.get("results", {})

    @property
    def rel_path(self):
        """Path relative to the sample folder: how other runs refer to this one."""
        return self.path.relative_to(self.sample.path).as_posix()

    @property
    def log_path(self):
        return self.path / LOG_FILE

    @property
    def key(self):
        """Unique within the project."""
        return f"{self.sample.id}/{self.rel_path}"

    def input_paths(self):
        """{input name: absolute path} of the files / folders this run was made from."""
        return {name: self.sample.path / entry["path"] for name, entry in self.data.get("inputs", {}).items()
                if entry.get("path")}

    def upstream(self):
        """{input name: Run} of the runs this run was made from."""
        out = {}
        for name, entry in self.data.get("inputs", {}).items():
            if entry.get("run") and (self.sample.path / entry["run"] / RUN_FILE).is_file():
                out[name] = self.sample.run(entry["run"])
        return out

    def duration(self):
        start, end = self.data.get("started"), self.data.get("finished")
        if not start:
            return None
        end = datetime.fromisoformat(end) if end else datetime.now()
        return (end - datetime.fromisoformat(start)).total_seconds()

    def files(self):
        """All files in the run folder (recursive), except run.json, as paths relative to the run folder."""
        return sorted(p.relative_to(self.path) for p in self.path.rglob("*")
                      if p.is_file() and p.name != RUN_FILE and not p.name.endswith(".tmp"))

    def delete(self):
        shutil.rmtree(self.path)


def format_duration(seconds):
    if seconds is None:
        return ""
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m {seconds:02d}s" if minutes else f"{seconds}s"


def format_size(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000
