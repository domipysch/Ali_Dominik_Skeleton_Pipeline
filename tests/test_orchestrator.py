"""The orchestrator without its GUI: project folders, channels, branches, the job queue, the runner."""
import json
import time

import h5py
import numpy as np
import pytest

from orchestrator.jobs import JobQueue, progress, read_log
from orchestrator.steps import (STEPS, branches, chain, create_run, expand_params, input_options, make_context,
                                next_steps, same_run)
from orchestrator.store import CANCELLED, DONE, FAILED, OPEN, QUEUED, Project

VOXEL = [3.0, 0.43, 0.43]


@pytest.fixture
def project(tmp_path):
    return Project.create(tmp_path / "project")


def make_ims(folder, name="mouse 1.ims", linked=("ch-1.lux.h5", "ch-2.lux.h5")):
    """A small .ims like Luxendo's: its channel data are external links into other files next to it."""
    folder.mkdir(parents=True, exist_ok=True)
    for channel, filename in enumerate(linked):
        with h5py.File(folder / filename, "w") as f:
            f.create_dataset("Data", data=np.full((4, 4, 4), channel + 1, np.uint16))
    with h5py.File(folder / name, "w") as f:
        for channel, filename in enumerate(linked):
            f[f"DataSet/ResolutionLevel 0/TimePoint 0/Channel {channel}/Data"] = h5py.ExternalLink(filename, "/Data")
            info = f.require_group(f"DataSetInfo/Channel {channel}")
            info.attrs["Name"] = np.frombuffer(f"chan:{channel}:marker-{channel} stack:0-x00 cam:long".encode(),
                                               dtype="S1")
    (folder / "unrelated.log").write_text("not part of the .ims")
    return folder / name


@pytest.fixture
def sample(project, tmp_path):
    return project.add_sample("mouse 1", "WT", make_ims(tmp_path / "source"), {0: "Nerves", 1: "Lymphatics"}, "test")


def fake_run(sample, step_key, parent=None, filename=None, data=None, status=DONE, channel=0, label="fake",
             params=None, results=None):
    """A run as if the step had run (with one .h5 output if filename is given)."""
    run = sample.new_run(channel, step_key, label, dict(status=status, parent=parent.rel_path if parent else None,
                                                        params=params or {}, inputs={}, results=results or {}))
    if filename:
        with h5py.File(run.path / filename, "w") as f:
            f.create_dataset("data", data=data if data is not None else np.zeros((4, 4, 4), np.uint8)) \
                .attrs["element_size_um"] = VOXEL
    return run


def branch_to(sample, last_step, data=None):
    """A done branch convert .. last_step (skipping the optional steps), the last run with the given data."""
    files = dict(convert="converted_channel0.h5", crops_training=None, ilastik_training=None,
                 ilastik_prediction="prob.h5", gaussian="smoothed.h5", crops_viewing=None,
                 threshold="mask.h5", lcc="lcc.h5")
    parent = None
    for key, filename in files.items():
        is_last = key == last_step
        parent = fake_run(sample, key, parent, filename, data if is_last else None)
        if is_last:
            return parent


def chosen_first(sample, step, parent):
    return {spec.name: (input_options(sample, spec, parent) or [None])[0] for spec in step.inputs}


def run_queue(queue, timeout=120):
    queue.changed()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        queue.tick()
        if queue.current is None and not queue.queued():
            return
        time.sleep(0.2)
    raise TimeoutError("queue did not finish")


def test_project_and_sample(project, sample, tmp_path):
    assert Project(project.root).tags == ["WT", "Wildling"]
    assert project.structures == ["Nerves", "Blood vessels", "Lymphatics"]
    assert sample.id == "mouse-1"
    assert sample.ims.is_file() and sample.ims.parent.name == "ims"
    assert sample.data["ims"] == "ims/mouse 1.ims"                 # stored relative to the sample folder
    assert [(c["index"], c["file"], c["structure"]) for c in sample.channels] == [
        (0, "ch-1.lux.h5", "Nerves"), (1, "ch-2.lux.h5", "Lymphatics")]
    assert sample.channels[1]["name"] == "chan:1:marker-1 stack:0-x00 cam:long"     # the full name is kept
    assert [s.id for s in project.samples()] == ["mouse-1"]
    with pytest.raises(ValueError):
        project.add_sample("mouse 1", "WT", sample.ims, {0: "Nerves", 1: "Nerves"})
    with pytest.raises(ValueError):
        project.add_sample("mouse 2", "WT", sample.path / "missing.ims", {})
    with pytest.raises(ValueError, match="structure for chan:1:marker-1"):    # every channel needs a structure
        project.add_sample("mouse 3", "WT", make_ims(tmp_path / "s3"), {0: "Nerves"})
    assert not (project.samples_dir / "mouse-2").exists() and not (project.samples_dir / "mouse-3").exists()

    project.rename_tag("WT", "wildtype")
    assert project.sample("mouse-1").tag == "wildtype"
    project.rename_structure("Lymphatics", "Lymph vessels")
    assert project.structures == ["Nerves", "Blood vessels", "Lymph vessels"]
    assert project.sample("mouse-1").structure(1) == "Lymph vessels"


def test_skipped_channel(project, tmp_path):
    """A channel skipped for now: copied with the .ims, but not processed; it can get a structure later."""
    with pytest.raises(ValueError, match="at least one"):
        project.add_sample("none", "WT", make_ims(tmp_path / "s0"), {0: None, 1: None})
    sample = project.add_sample("mouse 4", "WT", make_ims(tmp_path / "s4"), {0: None, 1: "Nerves"})
    assert [c["index"] for c in sample.channels] == [1]
    assert [c["index"] for c in sample.all_channels] == [0, 1] and sample.structure(0) is None
    assert (sample.ims.parent / "ch-1.lux.h5").is_file()             # its data is copied all the same
    project.rename_structure("Nerves", "Nerve fibres")
    assert [c["structure"] for c in project.sample(sample.id).all_channels] == [None, "Nerve fibres"]
    sample.update(channels=[dict(c, structure="Lymphatics") if c["index"] == 0 else c for c in sample.all_channels])
    assert [c["index"] for c in sample.channels] == [0, 1]


def test_rename_sample(project, sample, tmp_path):
    """Renaming a sample renames its folder; runs refer to each other relative to it, so the branch stays intact."""
    lcc = branch_to(sample, "lcc")
    renamed = project.rename_sample(sample, "Mouse 1 left")
    assert renamed.id == "Mouse-1-left" and renamed.name == "Mouse 1 left"
    assert renamed.path.is_dir() and renamed.ims.is_file()
    assert [s.id for s in project.samples()] == ["Mouse-1-left"]
    assert [p.name for p in project.samples_dir.iterdir()] == ["Mouse-1-left"]       # the old folder is gone
    moved = renamed.run(lcc.rel_path)
    assert [r.data["step"] for r in chain(moved)][0] == "convert"
    assert input_options(renamed, STEPS["maskel"].inputs[0], moved)[0]["path"].endswith("lcc.h5")
    # Only the case changes (the same folder on Windows): renamed all the same
    again = project.rename_sample(renamed, "  Mouse 1 LEFT ")
    assert again.name == "Mouse 1 LEFT" and [p.name for p in project.samples_dir.iterdir()] == ["Mouse-1-LEFT"]

    other = project.add_sample("other", "WT", make_ims(tmp_path / "s5"), {0: "Nerves", 1: "Nerves"})
    with pytest.raises(ValueError, match="already exists"):
        project.rename_sample(other, "mouse 1 left")
    fake_run(other, "convert", status=QUEUED)
    with pytest.raises(ValueError, match="queued or running"):
        project.rename_sample(other, "other 2")
    assert project.sample("other").name == "other"


def test_branches(sample):
    """Runs with different settings from the same parent split the branch; failed tries do not."""
    assert [b.runs for b in branches(sample, 0)] == [[]]
    assert [s.key for s in next_steps(None)] == ["convert"]

    convert = fake_run(sample, "convert", filename="converted_channel0.h5")
    assert [s.key for s in next_steps(convert)] == ["isotropic"]       # optional 1b: run or skip, never passed over
    assert fake_run(sample, "convert", channel=1).path.parent.parent.name == "channel1"
    assert [len(b.runs) for b in branches(sample, 1)] == [1]

    crops = fake_run(sample, "crops_training", convert)
    training = fake_run(sample, "ilastik_training", crops)
    prediction = fake_run(sample, "ilastik_prediction", training, "prob.h5")
    g1 = fake_run(sample, "gaussian", prediction, label="s0-1-1")
    g2 = fake_run(sample, "gaussian", prediction, label="s0-2-2", status=QUEUED)
    failed = fake_run(sample, "crops_viewing", g1, status=FAILED)
    fake_run(sample, "gaussian", prediction, status=CANCELLED)

    found = branches(sample, 0)
    assert [b.key for b in found] == [g1.rel_path, g2.rel_path]           # two branches, the cancelled one is none
    assert [r.id for r in found[0].runs] == [r.id for r in (convert, crops, training, prediction, g1)]
    assert found[0].attempt.id == failed.id and found[1].attempt is None
    assert next_steps(g2) == []                                            # not done: nothing can continue it
    assert [r.id for r in chain(g1)] == [r.id for r in found[0].runs]
    assert [r.id for r in prediction.children()] == [g1.id, g2.id, prediction.children()[2].id]
    assert len(convert.descendants()) == 7

    with pytest.raises(ValueError):
        create_run(sample, STEPS["crops_viewing"], {"n_crops": 5, "filetype": "tif"}, {}, 0, g2)   # parent not done


def test_skip_optional_step(sample):
    """Skipping 1b is a recorded decision; step 2 continues it and reads the full-resolution volume."""
    from orchestrator.steps import is_skipped, skip_run
    convert = fake_run(sample, "convert", filename="converted_channel0.h5")
    with pytest.raises(ValueError):
        skip_run(sample, STEPS["crops_training"], 0, convert)            # only optional steps can be skipped
    skipped = skip_run(sample, STEPS["isotropic"], 0, convert)
    assert is_skipped(skipped) and skipped.status == DONE and skipped.id == "001_skipped"
    assert skip_run(sample, STEPS["isotropic"], 0, convert).id == skipped.id     # skipping twice: the same run
    assert [s.key for s in next_steps(skipped)] == ["crops_training"]
    crops = STEPS["crops_training"]
    assert input_options(sample, crops.inputs[0], skipped)[0]["path"].endswith("converted_channel0.h5")
    params = expand_params(crops, {"n_crops": "5", "filetype": "h5", "min_tissue_fraction": "0.7",
                                   "crop_z": "100", "crop_y": "300", "crop_x": "300"})[0]
    run = create_run(sample, crops, params, chosen_first(sample, crops, skipped), 0, skipped)
    assert [r.id for r in chain(run)] == [convert.id, skipped.id, run.id]
    assert run.id == "001_n5_t0.7_100x300x300"
    command = run.data["command"]
    assert command[command.index("--min-tissue-fraction") + 1] == "0.7"
    assert command[command.index("--crop-shape") + 1:command.index("--crop-shape") + 4] == ["100", "300", "300"]
    default = expand_params(crops, {"n_crops": "5", "filetype": "h5", "min_tissue_fraction": "0.9",
                                    "crop_z": "200", "crop_y": "500", "crop_x": "500"})[0]
    assert create_run(sample, crops, default, chosen_first(sample, crops, skipped), 0, skipped).id == "002_n5_t0.9"


def test_inputs_come_from_the_branch(sample):
    convert = fake_run(sample, "convert", filename="converted_channel0.h5")
    iso = fake_run(sample, "isotropic", convert, "isotropic.h5")
    crops_iso = fake_run(sample, "crops_training", iso)
    crops_full = fake_run(sample, "crops_training", convert)
    step = STEPS["ilastik_prediction"]
    on_iso = fake_run(sample, "ilastik_training", crops_iso)
    on_full = fake_run(sample, "ilastik_training", crops_full)
    for training in (on_iso, on_full):
        (training.path / "project.ilp").write_text("x")
    assert input_options(sample, step.inputs[0], on_iso)[0]["path"].endswith("isotropic.h5")
    assert input_options(sample, step.inputs[0], on_full)[0]["path"].endswith("converted_channel0.h5")
    assert input_options(sample, step.inputs[1], on_full)[0]["run"] == on_full.rel_path


def test_ims_copied_with_its_linked_files(sample, tmp_path):
    """The .ims and exactly the files it links to are copied; the copy reads its own files."""
    folder = sample.path / "ims"
    assert sorted(p.name for p in folder.iterdir()) == ["ch-1.lux.h5", "ch-2.lux.h5", "mouse 1.ims"]
    assert sample.data["ims_files"] == ["mouse 1.ims", "ch-1.lux.h5", "ch-2.lux.h5"]
    assert sample.data["ims_source"] == str((tmp_path / "source" / "mouse 1.ims").resolve())

    # Remove the originals: the copy must still read its data
    for p in (tmp_path / "source").iterdir():
        p.unlink()
    with h5py.File(sample.ims, "r") as f:
        assert f["DataSet/ResolutionLevel 0/TimePoint 0/Channel 1/Data"][0, 0, 0] == 2


def test_ims_problems(project, tmp_path):
    from orchestrator.ims import bundle
    ims = make_ims(tmp_path / "a")
    channels = bundle(ims).channels
    assert [(c.index, c.label, c.file.name) for c in channels] == [(0, "chan:0:marker-0", "ch-1.lux.h5"),
                                                                    (1, "chan:1:marker-1", "ch-2.lux.h5")]
    (tmp_path / "a" / "ch-2.lux.h5").unlink()
    found = bundle(ims)
    assert any("missing" in p and "ch-2.lux.h5" in p for p in found.problems)
    with pytest.raises(ValueError, match="missing"):
        project.add_sample("broken", "WT", ims, {0: "Nerves", 1: "Nerves"})
    assert not (project.samples_dir / "broken").exists()        # no half-made sample

    absolute = tmp_path / "b" / "x.ims"
    absolute.parent.mkdir()
    with h5py.File(absolute, "w") as f:
        f["Data"] = h5py.ExternalLink(str(tmp_path / "a" / "ch-1.lux.h5"), "/Data")
    assert any("not next to the .ims" in p for p in bundle(absolute).problems)

    not_hdf5 = tmp_path / "c.ims"
    not_hdf5.write_bytes(b"not a real ims")
    assert any("Cannot read" in p for p in bundle(not_hdf5).problems)


def test_convert_command(sample):
    step = STEPS["convert"]
    chosen = chosen_first(sample, step, None)
    assert step.validate(make_context(sample, step, {"lo": 10, "hi": None}, chosen, channel=1))
    run = create_run(sample, step, {"lo": 100, "hi": 3000}, chosen, 1)
    assert run.id == "001_100-3000" and run.path.parent.parent.name == "channel1"
    command = run.data["command"]
    assert command[command.index("--channel") + 1] == "1"
    assert command[command.index("--range") + 1:command.index("--range") + 4] == ["1", "100", "3000"]


def test_params_and_run_numbering(sample):
    step = STEPS["threshold"]
    combos = [expand_params(step, {"low": low, "high": "100"})[0] for low in ("30", "40")]
    assert combos == [{"low": 30, "high": 100}, {"low": 40, "high": 100}]
    with pytest.raises(ValueError, match="only one value"):         # one run at a time
        expand_params(step, {"low": "30, 40", "high": "100"})
    with pytest.raises(ValueError, match="only one value"):
        expand_params(STEPS["gaussian"], {"sigma_z": "0", "sigma_y": "1, 2", "sigma_x": "1"})
    with pytest.raises(ValueError):
        expand_params(step, {"low": "abc", "high": "100"})
    with pytest.raises(ValueError):
        expand_params(step, {"low": "300", "high": "100"})

    # The threshold continues the crops it was chosen in; the crops folder is offered (optional, not used)
    crops = branch_to(sample, "crops_viewing")
    (crops.path / "crops").mkdir()
    chosen = chosen_first(sample, step, crops)
    assert chosen["smoothed"]["path"].endswith("smoothed.h5") and chosen["crops"]["path"].endswith("/crops")
    runs = [create_run(sample, step, params, chosen, 0, crops) for params in combos]
    assert [r.id for r in runs] == ["001_low30_high100", "002_low40_high100"]
    assert all(r.status == QUEUED and r.parent.id == crops.id for r in runs)
    assert "crops" not in " ".join(runs[0].data["command"]).replace(str(sample.path), "")
    assert same_run(runs[0], {"low": 30, "high": 100}, chosen, crops)
    assert not same_run(runs[0], {"low": 40, "high": 100}, chosen, crops)
    assert step.validate(make_context(sample, step, {"low": 50, "high": 10}, chosen))


def test_pipeline_runs_through_queue(sample, project):
    """Gaussian -> threshold (two runs: two branches) -> largest component, through the queue and the runner."""
    rng = np.random.default_rng(0)
    prob = (rng.random((70, 140, 150)) * 255).astype(np.uint8)    # at least one 64^3 chunk (the scripts' chunks)
    prediction = branch_to(sample, "ilastik_prediction", prob)
    queue = JobQueue(project)

    gaussian = STEPS["gaussian"]
    params = expand_params(gaussian, {"sigma_z": "0", "sigma_y": "1.2", "sigma_x": "1.2"})[0]
    run = create_run(sample, gaussian, params, chosen_first(sample, gaussian, prediction), 0, prediction)
    run_queue(queue)
    run.reload()
    assert run.status == DONE, read_log(run.log_path)
    with h5py.File(run.path / "smoothed.h5") as f:
        assert list(f["data"].attrs["element_size_um"]) == VOXEL

    crops = fake_run(sample, "crops_viewing", run)
    threshold = STEPS["threshold"]
    runs = [create_run(sample, threshold, p, chosen_first(sample, threshold, crops), 0, crops)
            for low in ("100", "120") for p in expand_params(threshold, {"low": low, "high": "140"})]
    run_queue(queue)
    for run in runs:
        run.reload()
        assert run.status == DONE, read_log(run.log_path)
        with h5py.File(run.path / "mask.h5") as f:
            assert list(f["data"].attrs["element_size_um"]) == VOXEL
    assert progress(runs[0].log_path) is not None
    assert [b.leaf.id for b in branches(sample, 0)] == [r.id for r in runs]

    lcc = STEPS["lcc"]
    options = input_options(sample, lcc.inputs[0], runs[1])
    assert {o["path"].rsplit("/", 1)[-1] for o in options} >= {"mask.h5"}
    full = next(o for o in options if o["path"].endswith("/mask.h5"))
    run = create_run(sample, lcc, {}, {"mask": full}, 0, runs[1])
    run_queue(queue)
    run.reload()
    assert run.status == DONE, read_log(run.log_path)
    assert run.upstream()["mask"].id == runs[1].id
    assert [b.leaf.id for b in branches(sample, 0)] == [runs[0].id, run.id]


def test_failed_run(sample, project):
    """A script that fails: status failed, error recorded."""
    prediction = branch_to(sample, "ilastik_prediction", np.zeros((4, 4, 4, 2), np.uint8))  # 2 channels: rejected
    gaussian = STEPS["gaussian"]
    run = create_run(sample, gaussian, {"sigma_z": 0.0, "sigma_y": 1.0, "sigma_x": 1.0},
                     chosen_first(sample, gaussian, prediction), 0, prediction)
    run_queue(JobQueue(project))
    run.reload()
    assert run.status == FAILED
    assert "Expected uint8" in "\n".join(read_log(run.log_path))
    assert branches(sample, 0)[0].attempt.id == run.id


def test_manual_step(sample):
    """ilastik training: an open run, done only once there is an .ilp; the prediction takes that .ilp."""
    crops = branch_to(sample, "crops_training")
    (crops.path / "crops").mkdir()
    step = STEPS["ilastik_training"]
    run = create_run(sample, step, {}, chosen_first(sample, step, crops), 0, crops)
    assert run.status == OPEN
    assert step.check_done(run)                          # no .ilp yet
    (run.path / "project.ilp").write_bytes(b"ilp")
    run.update(status=DONE)
    assert step.check_done(run.reload()) is None
    option = input_options(sample, STEPS["ilastik_prediction"].inputs[1], run)[0]
    assert option["path"].endswith("project.ilp")


def test_runs_of_removed_steps_are_passed_over(sample):
    """Projects made before 'Choose thresholds' was removed: a threshold run that continued such a run continues
    the crops now (the old run stays on disk, but is not shown)."""
    from orchestrator.store import Run, write_json
    crops = branch_to(sample, "crops_viewing")
    old_dir = sample.channel_dir(0) / "07_threshold_choice" / "001_default"
    old_dir.mkdir(parents=True)
    write_json(old_dir / "run.json", dict(step="threshold_choice", channel=0, id="001_default", status=DONE,
                                          parent=crops.rel_path, results={"low": 40, "high": 120}))
    old = Run(sample, old_dir)
    threshold = fake_run(sample, "threshold", old, "mask.h5")
    assert threshold.data["parent"] == old.rel_path                  # as stored
    assert threshold.parent_path == crops.rel_path and threshold.parent.id == crops.id
    assert old.rel_path not in {r.rel_path for r in sample.runs(channel=0)}
    assert [r.id for r in crops.children()] == [threshold.id]
    assert [b.leaf.id for b in branches(sample, 0)] == [threshold.id]
    assert [r.data["step"] for r in chain(threshold)][-2:] == ["crops_viewing", "threshold"]


def test_maskel_recipe_spacing(sample, monkeypatch):
    lcc = branch_to(sample, "lcc")
    step = STEPS["maskel"]
    with pytest.raises(ValueError, match="coming soon"):            # cannot be started for now
        create_run(sample, step, {"recipe": "maskel_recipe_lymphatics.json"}, chosen_first(sample, step, lcc), 0, lcc)
    assert not sample.runs("maskel")
    monkeypatch.setattr(step, "coming_soon", False)
    run = create_run(sample, step, {"recipe": "maskel_recipe_lymphatics.json"}, chosen_first(sample, step, lcc),
                     0, lcc)
    recipe = json.loads((run.path / "maskel_recipe.json").read_text())
    assert recipe["spacing"] == VOXEL
    assert str(lcc.path / "lcc.h5") in step.instructions(run_context_of(run))


def run_context_of(run):
    from orchestrator.steps import run_context
    return run_context(run)


def test_import_results(sample, tmp_path):
    """Results made outside the GUI become a branch of finished runs; the next step can continue it."""
    from orchestrator.importer import Item, import_branch
    old = tmp_path / "old"
    old.mkdir()
    for name in ("vol.h5", "probabilities.h5", "probabilities_res_16x16x16.h5", "smooth.h5"):
        (old / name).write_bytes(b"x" * 100)
    (old / "trained.ilp").write_bytes(b"ilp")
    (old / "my_crops").mkdir()
    (old / "my_crops" / "crop_0.h5").write_bytes(b"c")

    made = import_branch(sample, 1, [
        Item("gaussian", old / "smooth.h5", {"sigma_z": 0.0, "sigma_y": 1.2, "sigma_x": 1.2}),
        Item("convert", old / "vol.h5"),
        Item("isotropic", skipped=True),
        Item("crops_training", old / "my_crops"),
        Item("ilastik_training", old / "trained.ilp"),
        Item("ilastik_prediction", old / "probabilities.h5"),
    ])
    assert [r.data["step"] for r in made] == ["convert", "isotropic", "crops_training", "ilastik_training",
                                              "ilastik_prediction", "gaussian"]
    assert all(r.status == DONE and r.data["imported"] and r.channel == 1 for r in made)
    assert [r.id for r in made][:2] == ["001_imported", "001_skipped"]
    assert made[5].id == "001_s0-1.2-1.2"                      # the label from the parameters given
    assert made[1].data["skipped"] and not made[1].files()
    for parent, run in zip(made, made[1:]):
        assert run.data["parent"] == parent.rel_path
    assert (made[0].path / "converted_channel1.h5").is_file()
    assert (made[2].path / "crops" / "crop_0.h5").is_file()
    assert (made[3].path / "project.ilp").is_file()
    assert {p.name for p in made[4].path.iterdir()} == {"prob.h5", "run.json"}     # not the _res_ copy next to it
    assert (old / "vol.h5").is_file()                           # copied, not moved
    # Inputs are found through the branch (the skipped resampling is passed over)
    assert made[4].data["inputs"]["h5"]["path"].endswith("converted_channel1.h5")
    assert made[4].data["inputs"]["project"]["path"].endswith("project.ilp")
    assert [b.leaf.id for b in branches(sample, 1)] == [made[-1].id]
    step = STEPS["threshold"]
    option = input_options(sample, step.inputs[0], made[-1])[0]
    assert option["path"].endswith("smoothed.h5")

    # Moving; a problem anywhere imports nothing
    moved = import_branch(sample, 0, [Item("convert", old / "vol.h5")], move=True)
    assert not (old / "vol.h5").exists() and (moved[0].path / "converted_channel0.h5").is_file()
    with pytest.raises(ValueError, match="not a file"):
        import_branch(sample, 0, [Item("convert", old / "smooth.h5"), Item("gaussian", old / "missing.h5")])
    assert len(sample.runs(channel=0)) == 1
