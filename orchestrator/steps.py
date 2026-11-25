"""The pipeline steps: what each one needs (inputs, parameters), how it runs, and what it produces.

The GUI is built from this registry. To add a step or a parameter, add it here.

Every channel of a sample goes through the steps on its own. A run continues one run of an earlier step (its parent);
its inputs are taken from that parent and the runs before it on the same branch.

Script steps run one of the pipeline scripts with a command built from the inputs and parameters.
Manual steps (ilastik training, choosing thresholds, Maskel) are done outside the pipeline: the GUI makes their run
folder, shows instructions with the actual paths, and records the result (a file or values the user enters).
"""
import itertools
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import h5py

from .store import CANCELLED, DONE, FAILED, REPO, SCRIPTS, Run, Sample


@dataclass
class Param:
    name: str
    label: str
    type: str = "float"         # int, float, str, choice
    default: Any = None
    help: str = ""
    min: float | None = None
    max: float | None = None
    choices: list | Callable[[], list] | None = None
    sweep: bool = False         # several comma-separated values -> one run per value (all combinations)
    optional: bool = False
    group: str = ""             # parameters of the same group are shown together in one row of their own

    def options(self):
        return self.choices() if callable(self.choices) else list(self.choices or [])

    def parse(self, text):
        """Text from the GUI -> list of values (one value unless sweep). Raises ValueError with a readable message."""
        if self.type in ("str", "choice"):
            values = [text if text is not None else ""]
        else:
            parts = [p.strip() for p in str(text if text is not None else "").replace(";", ",").split(",")]
            parts = [p for p in parts if p]
            if not parts:
                if self.optional:
                    return [None]
                raise ValueError(f"{self.label}: a value is needed")
            if len(parts) > 1 and not self.sweep:
                raise ValueError(f"{self.label}: only one value allowed")
            cast = int if self.type == "int" else float
            try:
                values = [cast(p) for p in parts]
            except ValueError:
                raise ValueError(f"{self.label}: '{text}' is not a valid {'whole number' if cast is int else 'number'}")
            for v in values:
                if (self.min is not None and v < self.min) or (self.max is not None and v > self.max):
                    raise ValueError(f"{self.label}: {v} is outside {self.min} .. {self.max}")
        if not self.optional and self.type == "str" and not values[0]:
            raise ValueError(f"{self.label}: a value is needed")
        return values


@dataclass
class Input:
    name: str
    label: str
    steps: list[str] = field(default_factory=list)  # the nearest run of these steps on the branch is the input
    pattern: str = "*.h5"           # glob in that run's folder: which files / folders can be chosen
    exclude: str | None = None      # glob of files to leave out, e.g. the low-res copies
    kind: str = "file"              # file: a file or folder of a run; ims: the sample's .ims; values: a manual run's results
    fills: dict = field(default_factory=dict)   # kind values: {param name: result name} filled in as defaults
    optional: bool = False
    help: str = ""


@dataclass
class Context:
    """Everything a step needs to build its command, instructions, checks."""
    sample: Sample
    run_dir: Path
    params: dict
    inputs: dict            # input name -> absolute Path (None if an optional input is not chosen)
    input_runs: dict        # input name -> Run (the run the input comes from)
    channel: int | None = None
    parent: Run | None = None


@dataclass
class Step:
    key: str
    number: str
    folder: str
    title: str
    kind: str                       # script or manual
    summary: str                    # one line, shown in tables
    description: str = ""           # markdown
    script: str | None = None
    inputs: list[Input] = field(default_factory=list)
    params: list[Param] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)   # globs that must exist after a successful run
    command: Callable[[Context], list] | None = None
    label: Callable[[Context], str] = lambda ctx: ""   # folder name suffix, e.g. low40_high120
    validate: Callable[[Context], str | None] = lambda ctx: None      # error message, or None
    warnings: Callable[[Context], list] = lambda ctx: []              # shown before running, does not block
    # Manual steps
    instructions: Callable[[Context], str] | None = None           # markdown with the actual paths
    results: list[Param] = field(default_factory=list)               # values the user enters when done
    prepare: Callable[[Context], None] | None = None                 # e.g. write a config into the run folder
    check_done: Callable[[Run], str | None] = lambda run: None       # error message if not really done
    tools: list[str] = field(default_factory=list)                   # buttons: ilastik, napari, folder
    optional: bool = False          # the pipeline can go on without it
    once: bool = False              # one run per parent: nothing that changes the result can be set, so a second
                                    # run after the same parent would be the same
    coming_soon: bool = False       # shown in the pipeline, but cannot be started (or imported) yet

    @property
    def manual(self):
        return self.kind == "manual"

    def describe(self, run):
        """Short text for a run in tables: its parameters / results ('' if it has none)."""
        return run.id.split("_", 1)[1] if "_" in run.id else ""


# ---------------------------------------------------------------------------------------------------------------------
# Helpers

def script(name, *args):
    return [sys.executable, str(SCRIPTS / name), *map(str, args)]


def read_voxel_size(path, dataset="data"):
    try:
        with h5py.File(path, "r") as f:
            value = f[dataset].attrs.get("element_size_um")
        return None if value is None else [round(float(v), 4) for v in value]
    except (OSError, KeyError):
        return None


def fmt(v):
    """1.2 -> '1.2', 2.0 -> '2' (for folder names)."""
    return f"{v:g}"


def find_ilastik():
    import glob
    for pattern in (r"C:\Program Files\ilastik-*\ilastik.exe", r"C:\Program Files (x86)\ilastik-*\ilastik.exe"):
        hits = sorted(glob.glob(pattern))
        if hits:
            return Path(hits[-1])
    return None


def maskel_recipes():
    return sorted(p.name for p in (REPO / "maskel_configs").glob("*.json"))


def code(path):
    return f"`{path}`"


# ---------------------------------------------------------------------------------------------------------------------
# Step functions

def convert_validate(ctx):
    lo, hi = ctx.params.get("lo"), ctx.params.get("hi")
    if (lo is None) != (hi is None):
        return "8-bit window: give both LO and HI, or neither"
    if lo is not None and hi <= lo:
        return "8-bit window: HI must be larger than LO"


def convert_command(ctx):
    cmd = script("01_convert.py", "--input", ctx.inputs["ims"], "--output", ctx.run_dir / "converted.h5",
                 "--channel", ctx.channel)
    if ctx.params.get("lo") is not None:
        cmd += ["--range", ctx.channel, ctx.params["lo"], ctx.params["hi"]]
    return [str(c) for c in cmd]


def ilastik_predict_command(ctx):
    cmd = script("03_run_ilastik_headless.py", "--project", ctx.inputs["project"], "--input", ctx.inputs["h5"],
                 "--output", ctx.run_dir / "prob.h5")
    if ctx.params.get("ilastik"):
        cmd += ["--ilastik", ctx.params["ilastik"]]
    return cmd


def ilastik_predict_validate(ctx):
    exe = ctx.params.get("ilastik")
    if exe and not Path(exe).is_file():
        return f"ilastik.exe not found: {exe}"
    if not exe and find_ilastik() is None:
        return "No ilastik found in C:\\Program Files: give the path to ilastik.exe"


def ilastik_predict_warnings(ctx):
    """The classifier should run on data of the same voxel size it was trained on."""
    warnings = []
    project_run = ctx.input_runs.get("project")
    if project_run is None or ctx.inputs.get("h5") is None:
        return warnings
    crops_meta = None
    crops = project_run.input_paths().get("crops")
    if crops is not None and (crops / "meta.json").is_file():
        crops_meta = json.loads((crops / "meta.json").read_text(encoding="utf-8"))
    trained = crops_meta and crops_meta.get("voxel_size_um")
    current = read_voxel_size(ctx.inputs["h5"])
    if trained and current and any(abs(a - b) > 1e-3 for a, b in zip(trained, current)):
        warnings.append(f"The classifier was trained on crops with voxel size {[round(v, 4) for v in trained]} µm, "
                        f"but this input has {current} µm. Train on crops of the same resolution as the input.")
    return warnings


def crops_command(filetype):
    def command(ctx):
        cmd = script("02_generate_crops.py", "--input", ctx.inputs["h5"], "--ims", ctx.inputs["ims"],
                     "--output", ctx.run_dir / "crops", "--n-crops", ctx.params["n_crops"],
                     "--output_filetype", ctx.params.get("filetype", filetype))
        if ctx.params.get("min_tissue_fraction") is not None:
            cmd += ["--min-tissue-fraction", ctx.params["min_tissue_fraction"]]
        if all(ctx.params.get(f"crop_{axis}") is not None for axis in "zyx"):
            cmd += ["--crop-shape", *(ctx.params[f"crop_{axis}"] for axis in "zyx")]
        return cmd
    return command


CROP_DEFAULT = (200, 500, 500)      # the default of 02_generate_crops.py --crop-shape (z, y, x)


def crop_params():
    """Size of each crop in voxels, z / y / x."""
    return [Param(f"crop_{axis}", f"Crop size {axis} (voxels)", "int", default, min=1, group="crop size",
                  help=f"Size of each crop along {axis}, in voxels of the input volume")
            for axis, default in zip("zyx", CROP_DEFAULT)]


def crops_label(ctx, tissue=False):
    """n5, plus t0.7 for the tissue fraction and the crop size if it is not the default: n5_t0.7_100x300x300."""
    label = f"n{ctx.params['n_crops']}"
    if tissue and ctx.params.get("min_tissue_fraction") is not None:
        label += f"_t{fmt(ctx.params['min_tissue_fraction'])}"
    shape = tuple(ctx.params.get(f"crop_{axis}") for axis in "zyx")
    if None not in shape and shape != CROP_DEFAULT:
        label += "_" + "x".join(map(str, shape))
    return label


def threshold_validate(ctx):
    if ctx.params["low"] > ctx.params["high"]:
        return f"low ({ctx.params['low']}) must not be larger than high ({ctx.params['high']})"


def ilastik_training_instructions(ctx):
    crops = ctx.inputs.get("crops")
    return f"""
1. Open **ilastik** (button below) and create a new **Pixel Classification** project.
2. **Input Data → Raw Data → Add New… → Add separate Image(s)**: select all `crop_*.h5` in
   {code(crops)}
   (ilastik reads the voxel size from the files).
3. **Feature Selection**: select the features (all sigmas up to 10 are a good start).
4. **Training**: the **first label must be the foreground** (vessels), the second the background.
   The prediction step keeps only the probability of the first label.
5. Save the project into this run's folder as:
   {code(ctx.run_dir / 'project.ilp')}
   (or save it elsewhere and import it with the field below).
6. Press **Mark as done**.

Train on crops of the same resolution (isotropic or not) as the volume you will predict on.
"""


def ilastik_training_done(run):
    if not list(run.path.glob("*.ilp")):
        return f"No .ilp project in {run.path}"


def maskel_prepare(ctx):
    """Copy the recipe into the run folder, with the voxel size of the input filled in as spacing."""
    recipe = json.loads((REPO / "maskel_configs" / ctx.params["recipe"]).read_text(encoding="utf-8"))
    voxel = read_voxel_size(ctx.inputs["mask"])
    if voxel is not None:
        recipe["spacing"] = voxel
    (ctx.run_dir / "maskel_recipe.json").write_text(json.dumps(recipe, indent=2), encoding="utf-8")
    (ctx.run_dir / "output").mkdir(exist_ok=True)


def maskel_instructions(ctx):
    mask = ctx.inputs.get("mask")
    voxel = read_voxel_size(mask) if mask else None
    spacing = (f"The spacing in the recipe was set to the voxel size of the mask, **{voxel} µm (z, y, x)**."
               if voxel else "**The mask has no voxel size: set the spacing in the recipe by hand!**")
    return f"""
1. Open **napari** with the mask (button below), or drag it in:
   {code(mask)}
2. Open the **Maskel** plugin (Plugins → napari-maskel), memory mode for full-resolution masks.
3. Load the recipe prepared for this run:
   {code(ctx.run_dir / 'maskel_recipe.json')}
   {spacing}
4. Run it and save all outputs (skeleton, graph, feature CSVs) into:
   {code(ctx.run_dir / 'output')}
5. Press **Mark as done**.
"""


def maskel_done(run):
    output = run.path / "output"
    if not output.is_dir() or not any(output.iterdir()):
        return f"No outputs in {output}"


# ---------------------------------------------------------------------------------------------------------------------
# The registry, in pipeline order

ims_input = Input("ims", "Imaris file", kind="ims")

STEP_LIST = [
    Step(
        key="convert", number="1", folder="01_convert", title="Convert to 8-bit .h5", kind="script",
        script="01_convert.py",
        summary="The channel of the .ims -> an 8-bit, chunked, compressed .h5",
        inputs=[ims_input],
        params=[Param("lo", "8-bit window LO (optional)", "int", None, min=0, max=65535, optional=True,
                      help="Maps to 0. Empty: min of the coarsest resolution level"),
                Param("hi", "8-bit window HI (optional)", "int", None, min=0, max=65535, optional=True,
                      help="Maps to 255. Empty: max of the coarsest resolution level")],
        outputs=["converted_channel*.h5"],
        command=convert_command,
        label=lambda ctx: f"{ctx.params['lo']}-{ctx.params['hi']}" if ctx.params.get("lo") is not None else "",
        validate=convert_validate,
    ),
    Step(
        key="isotropic", number="1b", folder="01b_isotropic", title="Resample to isotropic", kind="script",
        script="01b_downsample_isotropic.py", optional=True,
        summary="Resample y and x to the z voxel size",
        description="Resamples y and x to the z voxel size (anti-aliasing + linear interpolation). Optional: "
                    "decide whether to resample, or to skip it and continue with the full-resolution volume.",
        inputs=[Input("h5", "Volume", ["convert"], "*_channel*.h5"), ims_input],
        outputs=["isotropic.h5"],
        command=lambda ctx: script("01b_downsample_isotropic.py", "--input", ctx.inputs["h5"], "--ims",
                                   ctx.inputs["ims"], "--output", ctx.run_dir / "isotropic.h5"),
    ),
    Step(
        key="crops_training", number="2", folder="02_crops_training", title="Crops for ilastik training",
        kind="script", script="02_generate_crops.py",
        summary="Random crops from the tissue region, for training ilastik",
        inputs=[Input("h5", "Volume", ["convert", "isotropic"], "*.h5", exclude="*_res_*"), ims_input],
        params=[Param("n_crops", "Number of crops", "int", 5, min=1, max=100),
                Param("filetype", "File type", "choice", "h5", choices=["h5", "tif"]),
                Param("min_tissue_fraction", "Min. tissue fraction (0-1)", "float", 0.9, min=0, max=1,
                      help="A crop needs at least this fraction of its volume inside the tissue mask"),
                *crop_params()],
        outputs=["crops/meta.json"],
        command=crops_command("h5"),
        label=lambda ctx: crops_label(ctx, tissue=True),
    ),
    Step(
        key="ilastik_training", number="3", folder="03_ilastik_training", title="Train ilastik", kind="manual",
        summary="Manual: label foreground / background in the ilastik GUI",
        description="Train a pixel classifier on the training crops. The result is a trained `.ilp` project.",
        inputs=[Input("crops", "Training crops", ["crops_training"], "crops")],
        params=[],
        instructions=ilastik_training_instructions,
        check_done=ilastik_training_done,
        tools=["ilastik", "folder"],
    ),
    Step(
        key="ilastik_prediction", number="4", folder="04_ilastik_prediction", title="ilastik prediction",
        kind="script", script="03_run_ilastik_headless.py", once=True,
        summary="Run the trained classifier headless on the whole volume",
        inputs=[Input("h5", "Volume", ["convert", "isotropic"], "*.h5", exclude="*_res_*"),
                Input("project", "Trained project", ["ilastik_training"], "*.ilp")],
        params=[Param("ilastik", "ilastik.exe (empty: newest in C:\\Program Files)", "str", "", optional=True)],
        outputs=["prob.h5"],
        command=ilastik_predict_command,
        validate=ilastik_predict_validate,
        warnings=ilastik_predict_warnings,
    ),
    Step(
        key="gaussian", number="5", folder="05_gaussian", title="Gaussian smoothing", kind="script",
        script="04_apply_gaussian.py",
        summary="Smooth the probability map",
        inputs=[Input("prob", "Probability map", ["ilastik_prediction"], "prob.h5")],
        params=[Param("sigma_z", "Sigma z (voxels)", "float", 0.0, min=0),
                Param("sigma_y", "Sigma y (voxels)", "float", 1.2, min=0),
                Param("sigma_x", "Sigma x (voxels)", "float", 1.2, min=0)],
        outputs=["smoothed.h5"],
        command=lambda ctx: script("04_apply_gaussian.py", "--input", ctx.inputs["prob"],
                                   "--output", ctx.run_dir / "smoothed.h5",
                                   "--sigma_z", ctx.params["sigma_z"], "--sigma_y", ctx.params["sigma_y"],
                                   "--sigma_x", ctx.params["sigma_x"]),
        label=lambda ctx: f"s{fmt(ctx.params['sigma_z'])}-{fmt(ctx.params['sigma_y'])}-{fmt(ctx.params['sigma_x'])}",
    ),
    Step(
        key="crops_viewing", number="6", folder="06_crops_viewing", title="Crops for choosing thresholds",
        kind="script", script="02_generate_crops.py",
        summary="Random crops of the smoothed map, to view in Imaris",
        inputs=[Input("h5", "Smoothed map", ["gaussian"], "smoothed.h5"), ims_input],
        params=[Param("n_crops", "Number of crops", "int", 5, min=1, max=100),
                Param("filetype", "File type", "choice", "tif", choices=["tif", "h5"]),
                *crop_params()],
        outputs=["crops/meta.json"],
        command=crops_command("tif"),
        label=crops_label,
    ),
    # Choosing the thresholds (viewing the crops in Imaris) is not a step of its own: the values are entered here.
    # The folders keep their numbers (08_threshold, ...) so that existing projects stay readable.
    Step(
        key="threshold", number="7", folder="08_threshold", title="Hysteresis threshold", kind="script",
        script="05_apply_threshold.py",
        summary="Binary mask: voxels > low connected to a voxel > high",
        inputs=[Input("smoothed", "Smoothed map", ["gaussian"], "smoothed.h5"),
                Input("crops", "Crops", ["crops_viewing"], "crops", optional=True,
                      help="The crops to choose low / high in (not used by the script)")],
        params=[Param("low", "Low (0-255)", "int", None, min=0, max=255),
                Param("high", "High (0-255)", "int", None, min=0, max=255)],
        outputs=["mask.h5"],
        command=lambda ctx: script("05_apply_threshold.py", "--input", ctx.inputs["smoothed"],
                                   "--low", ctx.params["low"], "--high", ctx.params["high"],
                                   "--output", ctx.run_dir / "mask.h5"),
        label=lambda ctx: f"low{ctx.params['low']}_high{ctx.params['high']}",
        validate=threshold_validate,
    ),
    Step(
        key="lcc", number="8", folder="09_largest_component", title="Largest connected component", kind="script",
        script="06_select_lcc.py", once=True,
        summary="Keep only the largest connected component",
        inputs=[Input("mask", "Mask", ["threshold"], "mask.h5")],
        outputs=["lcc.h5"],
        command=lambda ctx: script("06_select_lcc.py", "--input", ctx.inputs["mask"],
                                   "--output", ctx.run_dir / "lcc.h5"),
    ),
    Step(
        key="maskel", number="9", folder="10_maskel", title="Maskel skeleton", kind="manual", coming_soon=True,
        summary="Manual: skeleton, graph and features with Maskel in napari",
        inputs=[Input("mask", "Largest component", ["lcc"], "lcc*.h5")],
        params=[Param("recipe", "Recipe", "choice", "maskel_recipe_lymphatics.json", choices=maskel_recipes)],
        instructions=maskel_instructions,
        prepare=maskel_prepare,
        check_done=maskel_done,
        tools=["napari", "folder"],
    ),
]

STEPS = {step.key: step for step in STEP_LIST}


STEP_INDEX = {step.key: i for i, step in enumerate(STEP_LIST)}
DEAD = (FAILED, CANCELLED)      # such runs do not continue a branch (a retry replaces them)


# ---------------------------------------------------------------------------------------------------------------------
# Branches: the runs of a channel form a tree (each run continues its parent)

def chain(run):
    """The runs from the first step of the branch to this run (inclusive)."""
    out = []
    while run is not None:
        out.append(run)
        run = run.parent
    return out[::-1]


@dataclass
class Branch:
    channel: int
    runs: list                      # first step .. last run of the branch, one run per step done on it
    attempt: Run | None = None      # newest failed / cancelled try of the next step (shown, but no branch of its own)

    @property
    def leaf(self):
        return self.runs[-1] if self.runs else None

    @property
    def key(self):
        """Identifies the branch: the path of its last run ('' for a channel without runs)."""
        return self.leaf.rel_path if self.leaf else ""

    def run_of(self, step_key):
        return next((run for run in self.runs if run.data["step"] == step_key), None)


def branches(sample, channel, runs=None):
    """All branches of a channel (one per leaf of its tree of runs), depth first, oldest first.

    Failed and cancelled runs do not make branches: a retry with the same settings replaces them.
    """
    runs = runs if runs is not None else sample.runs(channel=channel)
    live = [run for run in runs if run.status not in DEAD]
    live_paths = {run.rel_path for run in live}
    by_parent, attempts = {}, {}
    order = lambda run: (STEP_INDEX.get(run.data["step"], 99), run.data.get("created", ""), run.id)
    for run in sorted(live, key=order):
        parent = run.parent_path
        by_parent.setdefault(parent if parent in live_paths else None, []).append(run)
    for run in sorted((r for r in runs if r.status in DEAD), key=order):
        attempts[run.parent_path] = run                 # the newest one wins

    found = []

    def walk(run, path):
        children = by_parent.get(run.rel_path, [])
        if not children:
            found.append(Branch(channel, path, attempts.get(run.rel_path)))
        for child in children:
            walk(child, path + [child])

    for root in by_parent.get(None, []):
        walk(root, [root])
    if not found:
        found.append(Branch(channel, [], attempts.get(None)))
    return found


def next_steps(run):
    """The step that can continue a branch ending in this run (None: the start of a channel), as a list.

    Nothing until the run is done. An optional step is not passed over: it is run or skipped (see skip_run), an
    explicit decision that stays on the branch.
    """
    if run is None:
        return STEP_LIST[:1]
    if run.status != DONE:
        return []
    return STEP_LIST[STEP_INDEX[run.data["step"]] + 1:][:1]


def is_skipped(run):
    return bool(run.data.get("skipped"))


def skip_run(sample, step, channel, parent):
    """Record the decision to skip an optional step: a 'done' run without outputs; the next step continues it.

    Returns the existing skip run if this step was already skipped after this parent.
    """
    from .store import now
    if not step.optional:
        raise ValueError(f"{step.title} cannot be skipped")
    if parent is None or parent.status != DONE or parent.channel != channel:
        raise ValueError("Skipping needs a finished run before it")
    for run in sample.runs(step.key, channel):
        if is_skipped(run) and run.parent_path == parent.rel_path:
            return run
    return sample.new_run(channel, step.key, "skipped", dict(
        status=DONE, skipped=True, parent=parent.rel_path, params={}, inputs={}, results={}, notes="",
        finished=now()))


def parent_for(path, step):
    """The run a new run of this step continues on a branch (path: its runs): the last one of an earlier step."""
    index = STEP_INDEX[step.key]
    earlier = [run for run in path if STEP_INDEX[run.data["step"]] < index]
    return earlier[-1] if earlier else None


# ---------------------------------------------------------------------------------------------------------------------
# Inputs and making runs

def input_options(sample, spec, parent):
    """What can be the input of a new run continuing parent: list of dict(path, run, label).

    The input comes from the nearest run of spec.steps on the branch up to parent. Several options only when that
    run has several matching files (e.g. the full-resolution mask and its downsampled copy).
    path and run are relative to the sample folder (that is what run.json stores).
    """
    if spec.kind == "ims":
        ims = sample.ims
        return [dict(path=sample.data.get("ims"), run=None, label=ims.name)] if ims else []
    # A skipped step made nothing: the input comes from the run before it
    source = next((run for run in reversed(chain(parent)) if run.data["step"] in spec.steps and not is_skipped(run)),
                  None)
    if source is None or source.status != DONE:
        return []
    prefix = f"{source.step.number} · {source.id}"
    if spec.kind == "values":
        return [dict(path=None, run=source.rel_path, label=f"{prefix} ({source.step.describe(source)})")]
    options = []
    for path in sorted(source.path.glob(spec.pattern)):
        if spec.exclude and path.match(spec.exclude):
            continue
        options.append(dict(path=path.relative_to(sample.path).as_posix(), run=source.rel_path,
                            label=f"{prefix} / {path.name}"))
    return options


def make_context(sample, step, params, chosen, run_dir=None, channel=None, parent=None):
    """chosen: {input name: option dict from input_options, or None}."""
    inputs, input_runs = {}, {}
    for spec in step.inputs:
        option = chosen.get(spec.name)
        inputs[spec.name] = sample.path / option["path"] if option and option.get("path") else None
        if option and option.get("run"):
            input_runs[spec.name] = sample.run(option["run"])
    return Context(sample=sample, run_dir=run_dir or Path("<run folder>"), params=params, inputs=inputs,
                   input_runs=input_runs, channel=channel, parent=parent)


def expand_params(step, texts):
    """{param name: text from the GUI} -> list of parameter dicts (more than one when sweeping)."""
    values = {p.name: p.parse(texts.get(p.name)) for p in step.params}
    names = list(values)
    return [dict(zip(names, combo)) for combo in itertools.product(*(values[n] for n in names))]


def check_inputs(step, chosen):
    for spec in step.inputs:
        if not spec.optional and not chosen.get(spec.name):
            return f"Choose an input for '{spec.label}'"


def same_run(run, params, chosen, parent):
    """Was this run made with these parameters from these inputs, continuing this parent?"""
    inputs = run.data.get("inputs", {})
    return (run.params == params and run.parent_path == (parent.rel_path if parent else None)
            and all((inputs.get(name) or {}).get("path") == (option or {}).get("path")
                    and (inputs.get(name) or {}).get("run") == (option or {}).get("run")
                    for name, option in chosen.items()))


def create_run(sample, step, params, chosen, channel, parent=None):
    """Make the run folder and run.json. Script runs are 'queued', manual runs 'open'."""
    from .store import OPEN, QUEUED
    if step.coming_soon:
        raise ValueError(f"{step.title} is coming soon: it cannot be started yet")
    if parent is not None and (parent.channel != channel or parent.status != DONE):
        raise ValueError(f"Cannot continue {parent.id}: it is not done or of another channel")
    ctx = make_context(sample, step, params, chosen, channel=channel, parent=parent)
    label = step.label(ctx)
    inputs = {spec.name: dict(path=(chosen.get(spec.name) or {}).get("path"),
                              run=(chosen.get(spec.name) or {}).get("run"))
              for spec in step.inputs if chosen.get(spec.name)}
    run = sample.new_run(channel, step.key, label, dict(
        status=OPEN if step.manual else QUEUED, parent=parent.rel_path if parent else None, params=params,
        inputs=inputs, results={}, notes=""))
    ctx.run_dir = run.path
    if step.manual:
        if step.prepare:
            step.prepare(ctx)
    else:
        run.update(command=[str(c) for c in step.command(ctx)], expected_outputs=step.outputs)
    return run


def run_context(run):
    """Context of an existing run (for its instructions)."""
    chosen = {name: entry for name, entry in run.data.get("inputs", {}).items()}
    return make_context(run.sample, run.step, run.params, chosen, run.path, run.channel, run.parent)
