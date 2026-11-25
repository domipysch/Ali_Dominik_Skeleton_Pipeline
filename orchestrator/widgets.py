"""GUI building blocks shared by the pages."""
import json
import os
import re
import shutil
import string
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from nicegui import ui

from . import steps as steps_module
from .jobs import STARTING, progress, read_log
from .steps import STEPS, check_inputs, create_run, expand_params, input_options, make_context, run_context, same_run
from .store import (ACTIVE, CANCELLED, DONE, FAILED, OPEN, QUEUED, RUNNING, format_duration, format_size, now)

STATUS_TEXT = {OPEN: "in progress"}


class Ctx:
    """The running app: project and job queue (set in app.start)."""
    project = None
    queue = None


def status_pill(status, text=None):
    """A status as a soft pill with a coloured dot (styles in theme.py)."""
    return ui.label(text or STATUS_TEXT.get(status, status or "?")).classes(f"pill pill-{status}")


def file_url(path):
    """URL of a file below the project root (served as static files)."""
    rel = Path(path).resolve().relative_to(Ctx.project.root).as_posix()
    return "/files/" + quote(rel)


def instructions_md(step, ctx):
    """A step's instructions as markdown; an indented `path` line stays on a line of its own."""
    return re.sub(r"\n(\s+)`", r"  \n\1`", step.instructions(ctx))


def run_where(run):
    """'Mouse 12 · Lymphatics': the sample and the structure of the run's channel."""
    structure = run.sample.structure(run.channel)
    return f"{run.sample.name} · {structure}" if structure else run.sample.name


def short_time(iso):
    if not iso:
        return ""
    t = datetime.fromisoformat(iso)
    return t.strftime("%H:%M") if t.date() == datetime.now().date() else t.strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------------------------------------------------------------
# Opening things outside the GUI

def open_folder(path):
    path = Path(path)
    if not path.exists():
        ui.notify(f"Not found: {path}", type="negative")
        return
    if sys.platform == "win32":
        os.startfile(path)
        if path.is_dir():
            # Windows opens the window behind the browser (the server is not the foreground app): pull it up
            threading.Thread(target=bring_explorer_to_front, args=(path,), daemon=True).start()
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)])


def bring_explorer_to_front(path, timeout=5.0):
    """Wait for the Explorer window showing `path` and put it on top of the other windows (Windows only)."""
    import ctypes
    import time
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    titles = {path.name.lower(), str(path).lower()}     # Explorer shows the folder name, or the full path if set so
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def check(hwnd, _):
        name = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, name, 256)
        if name.value == "CabinetWClass" and user32.IsWindowVisible(hwnd):
            title = ctypes.create_unicode_buffer(1024)
            user32.GetWindowTextW(hwnd, title, 1024)
            # Windows 11 adds " – File Explorer" (or the translation, e.g. "Datei-Explorer") after the folder name
            text = title.value.lower()
            if any(text == t or text.startswith(t + " ") for t in titles):
                found.append(hwnd)
                return False
        return True

    end = time.monotonic() + timeout
    while not found and time.monotonic() < end:
        time.sleep(0.15)
        user32.EnumWindows(check, 0)
    if not found:
        return
    hwnd = found[0]
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)                      # SW_RESTORE
    # Windows only lets the foreground app hand over the focus: borrow its input queue for a moment
    foreground = user32.GetForegroundWindow()
    this_thread = ctypes.windll.kernel32.GetCurrentThreadId()
    other_thread = user32.GetWindowThreadProcessId(foreground, None)
    attached = other_thread and other_thread != this_thread and user32.AttachThreadInput(this_thread, other_thread, True)
    try:
        # Topmost and back: on top even if Windows refuses the focus
        flags = 0x0001 | 0x0002 | 0x0040                # SWP_NOSIZE | SWP_NOMOVE | SWP_SHOWWINDOW
        user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, flags)
        user32.SetWindowPos(hwnd, -2, 0, 0, 0, 0, flags)
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(this_thread, other_thread, False)


def open_napari(*paths):
    subprocess.Popen([sys.executable, "-m", "napari", *map(str, paths)])
    ui.notify("Starting napari…")


def open_ilastik():
    exe = steps_module.find_ilastik()
    if exe is None:
        ui.notify("No ilastik found in C:\\Program Files", type="negative")
        return
    subprocess.Popen([str(exe)])
    ui.notify(f"Starting {exe.parent.name}…")


def copy_button(text):
    async def copy():
        ui.clipboard.write(str(text))
        ui.notify("Copied")
    return ui.button(icon="content_copy", on_click=copy).props("flat dense round size=sm color=grey-7") \
        .tooltip("Copy")


def path_row(label, path):
    with ui.row().classes("w-full items-start gap-3 no-wrap"):
        ui.label(label).classes("text-sm text-slate-500 w-28 shrink-0")
        ui.label(str(path)).classes("font-mono text-xs text-slate-700 break-all grow pt-0.5")
        copy_button(path)
        if path is not None and Path(path).exists():
            target = path if Path(path).is_dir() else Path(path).parent
            ui.button(icon="folder_open", on_click=lambda: open_folder(target)) \
                .props("flat dense round size=sm color=grey-7").tooltip("Open folder")


def folder_box(label, path):
    """A folder path in a code box, with buttons to copy it and to open it in the explorer."""
    ui.label(label).classes("label-sm mt-3")
    with ui.row().classes("w-full items-center gap-1 no-wrap code-box"):
        ui.label(str(path)).classes("font-mono text-xs text-slate-700 break-all grow")
        copy_button(path)
        ui.button(icon="folder_open", on_click=lambda: open_folder(path)) \
            .props("flat dense round size=sm color=grey-7").tooltip("Open in explorer")


def input_folders(sample, step, parent, inputs=None):
    """[(label, path)] of the inputs of a run that are folders (e.g. the crops of the step before).

    inputs: {name: path} of an existing run; inputs it does not record are found on the branch up to parent.
    """
    out = []
    for spec in step.inputs:
        path = (inputs or {}).get(spec.name)
        if path is None and parent is not None:
            options = input_options(sample, spec, parent)
            path = sample.path / options[0]["path"] if options and options[0].get("path") else None
        if path is not None and Path(path).is_dir():
            out.append((spec.label, Path(path)))
    return out


def run_input_folders(run):
    return input_folders(run.sample, run.step, run.parent, run_context(run).inputs)


def key_value(label, value, mono=True):
    with ui.row().classes("w-full items-baseline gap-3 no-wrap py-1"):
        ui.label(label).classes("text-sm text-slate-500 w-28 shrink-0")
        ui.label("—" if value in (None, "") else str(value)) \
            .classes(("font-mono text-xs" if mono else "text-sm") + " text-slate-800 break-all")


# ---------------------------------------------------------------------------------------------------------------------
# Small inputs

async def pick_file(start=None, suffixes=(".ims",), title="Choose a file", folder=False):
    """A file browser on the machine running the GUI. Returns the chosen Path or None.

    folder: choose a folder instead of a file (files are not listed; 'Choose this folder' picks the one shown)."""
    state = {"dir": Path(start) if start and Path(start).is_dir() else Path.home()}
    if folder:
        suffixes = ("<none>",)

    def drives():
        return [Path(f"{letter}:/") for letter in string.ascii_uppercase if Path(f"{letter}:/").exists()]

    with ui.dialog() as dialog, ui.card().classes("dialog-card w-[760px] max-w-full"):
        ui.label(title).classes("text-lg font-semibold")
        path_input = ui.input("Folder").classes("w-full font-mono") \
            .on("keydown.enter", lambda: go(Path(path_input.value)))
        with path_input.add_slot("prepend"):
            ui.icon("folder", size="18px").classes("text-slate-400")

        def go(folder):
            if folder is not None and not folder.is_dir():
                ui.notify(f"Not a folder: {folder}", type="warning")
                return
            state["dir"] = folder
            listing.refresh()

        @ui.refreshable
        def listing():
            folder = state["dir"]
            path_input.value = str(folder) if folder else "This PC"
            with ui.scroll_area().classes("h-96 w-full border border-slate-200 rounded-xl"):
                with ui.list().props("dense separator").classes("w-full"):
                    if folder is None:
                        entries = [(d, True) for d in drives()]
                    else:
                        parent = folder.parent if folder.parent != folder else (None if sys.platform == "win32" else folder)
                        with ui.item(on_click=lambda p=parent: go(p)):
                            with ui.item_section().props("avatar"):
                                ui.icon("arrow_upward")
                            ui.item_section("..")
                        try:
                            children = sorted(folder.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
                        except OSError as error:
                            ui.label(str(error)).classes("text-negative p-2")
                            children = []
                        entries = [(p, p.is_dir()) for p in children if not p.name.startswith(".")]
                        entries = [(p, d) for p, d in entries if d or not suffixes or p.suffix.lower() in suffixes]
                    for path, is_dir in entries:
                        on_click = (lambda p=path: go(p)) if is_dir else (lambda p=path: dialog.submit(p))
                        with ui.item(on_click=on_click):
                            with ui.item_section().props("avatar"):
                                ui.icon("folder" if is_dir else "description", size="20px",
                                        color="amber-6" if is_dir else "primary")
                            with ui.item_section():
                                ui.item_label(str(path) if folder is None else path.name)
                            if not is_dir:
                                with ui.item_section().props("side"):
                                    try:
                                        ui.item_label(format_size(path.stat().st_size)).props("caption")
                                    except OSError:
                                        pass

        listing()
        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Cancel", on_click=lambda: dialog.submit(None)).props("flat color=grey-8")
            if folder:
                ui.button("Choose this folder", on_click=lambda: dialog.submit(state["dir"]) if state["dir"] else None)
    result = await dialog
    dialog.delete()
    return result


def confirm(message, ok="Delete", color="negative"):
    """Awaitable yes/no dialog."""
    with ui.dialog() as dialog, ui.card().classes("dialog-card w-[460px] max-w-full"):
        with ui.row().classes("items-start gap-4 no-wrap"):
            with ui.element("div").classes("empty-icon shrink-0 m-0 bg-rose-50 text-rose-600"
                                           if color == "negative" else "empty-icon shrink-0 m-0"):
                ui.icon("warning_amber" if color == "negative" else "help_outline", size="24px")
            ui.markdown(message).classes("text-sm text-slate-700 pt-1")
        with ui.row().classes("w-full justify-end gap-2 mt-2"):
            ui.button("Cancel", on_click=lambda: dialog.submit(False)).props("flat color=grey-8")
            ui.button(ok, color=color, on_click=lambda: dialog.submit(True))
    return dialog


# ---------------------------------------------------------------------------------------------------------------------
# Runs

def describe_params(run):
    step = run.step
    if step.manual and run.results:
        return ", ".join(f"{k}={v}" for k, v in run.results.items())
    labels = {p.name: p.name for p in step.params}
    return ", ".join(f"{labels.get(k, k)}={v}" for k, v in run.params.items() if v not in (None, ""))


def describe_inputs(run):
    out = []
    for name, entry in run.data.get("inputs", {}).items():
        if name == "ims":
            continue
        if entry.get("run"):
            upstream_step = entry["run"].split("/")[1] if "/" in entry["run"] else ""
            number = next((s.number for s in STEPS.values() if s.folder == upstream_step), "")
            text = f"{number}·{entry['run'].rsplit('/', 1)[-1]}"
            if entry.get("path"):
                text += f"/{Path(entry['path']).name}"
            out.append(text)
    return ", ".join(out)


def runs_table(runs, on_open, current=None, on_follow=None):
    """Runs of one step from the same parent. current: path of the run on the shown branch (marked);
    on_follow(path): show the branch of another run (a button on the runs that continue a branch)."""
    from .steps import DEAD
    columns = [
        dict(name="id", label="Run", field="id", align="left"),
        dict(name="status", label="Status", field="status", align="left"),
        # Script steps have parameters (set before running); manual steps record results (entered when done)
        dict(name="params", label="Results" if runs and runs[0].step.manual else "Parameters", field="params",
             align="left"),
        dict(name="created", label="Created", field="created", align="left"),
        dict(name="duration", label="Duration", field="duration", align="left"),
        dict(name="branch", label="", field="branch", align="right"),
    ]
    rows = [dict(key=run.rel_path, id=run.id, status=run.status,
                 status_text=STATUS_TEXT.get(run.status, run.status), params=describe_params(run),
                 created=short_time(run.data.get("created")), duration=format_duration(run.duration()),
                 notes=run.data.get("notes", ""), current=run.rel_path == current,
                 branch=bool(on_follow) and run.rel_path != current and run.status not in DEAD)
            for run in reversed(runs)]
    with ui.element("div").classes("w-full border border-slate-200 rounded-xl overflow-hidden"):
        table = ui.table(columns=columns, rows=rows, row_key="key").props("dense flat hide-bottom") \
            .classes("modern-table clickable w-full")
    table.add_slot("body-cell-status", """
        <q-td :props="props"><span :class="'pill pill-' + props.value">{{ props.row.status_text }}</span></q-td>""")
    table.add_slot("body-cell-id", """
        <q-td :props="props">
          <span class="font-mono text-xs font-medium text-slate-800">{{ props.value }}</span>
          <span v-if="props.row.current" class="branch-mark">this branch</span>
        </q-td>""")
    table.add_slot("body-cell-branch", """
        <q-td :props="props">
          <q-btn v-if="props.value" flat dense no-caps size="sm" color="primary" label="Show branch"
                 icon-right="arrow_forward" @click.stop="$parent.$emit('follow', props.row.key)" />
        </q-td>""")
    table.on("rowClick", lambda e: on_open(e.args[1]["key"]))
    if on_follow:
        table.on("follow", lambda e: on_follow(e.args))
    return table


def log_view(run, height="h-96"):
    """Log of a run with its progress bar, refreshed every second while the run is active."""
    with ui.column().classes("w-full gap-2"):
        progress_row = ui.row().classes("w-full items-center gap-3 no-wrap")
        with progress_row:
            bar = ui.linear_progress(0, show_value=False, size="8px").classes("grow")
            bar_text = ui.label("").classes("text-xs text-slate-600 font-mono whitespace-nowrap")
        area = ui.scroll_area().classes(f"w-full {height} log-box")
        with area:
            text = ui.label("").classes("whitespace-pre font-mono text-[12px] leading-relaxed text-slate-200 p-3")

    state = {"last": None}

    def update():
        lines = read_log(run.log_path)
        content = "\n".join(lines[-1000:]) or "(no output yet)"
        if content != state["last"]:
            state["last"] = content
            text.set_text(content)
            area.scroll_to(percent=1.0)
        found = progress(run.log_path)
        active = run.reload().status in (RUNNING, STARTING)
        progress_row.set_visibility(bool(found) and active)
        if found:
            bar.set_value(found[0])
            bar_text.set_text(found[1])
        timer.active = active

    timer = ui.timer(1.0, update)
    update()


def run_dialog(run, on_change=None):
    """Everything about one run: parameters, inputs, log, files, and the actions on it."""
    step = run.step
    queue = Ctx.queue
    # Made on the page itself: a click handler runs inside the clicked element, so a dialog opened from another run's
    # dialog would otherwise sit inside that one and be deleted with it when it closes
    with ui.context.client.content, ui.dialog() as dialog, ui.card().classes("dialog-card w-[1100px] max-w-full"):
        with ui.row().classes("w-full items-start gap-3 no-wrap"):
            with ui.column().classes("gap-1 grow"):
                with ui.row().classes("items-center gap-3"):
                    ui.label(step.title).classes("text-xl font-semibold tracking-tight")
                    ui.label(run.id).classes("font-mono text-sm text-slate-500")
                    status_pill(run.status)
            ui.button(icon="close", on_click=dialog.close).props("flat round dense color=grey-7")
        if step.manual:
            for label, path in run_input_folders(run):
                folder_box(label, path)
        if run.data.get("error"):
            with ui.row().classes("w-full items-center gap-2 bg-rose-50 text-rose-700 rounded-lg px-3 py-2 "
                                  "text-sm no-wrap"):
                ui.icon("error_outline", size="18px")
                ui.label(run.data["error"])

        with ui.tabs().classes("w-full border-b border-slate-200 text-slate-600") \
                .props("align=left dense active-color=primary indicator-color=primary") as tabs:
            details_tab = ui.tab("Details")
            log_tab = ui.tab("Log") if not step.manual else None
            files_tab = ui.tab("Files")
        with ui.tab_panels(tabs, value=log_tab if run.status in (RUNNING, STARTING) else details_tab) \
                .classes("w-full"):
            with ui.tab_panel(details_tab):
                run_details(run, dialog, on_change)
            if log_tab is not None:
                with ui.tab_panel(log_tab):
                    log_view(run)
            with ui.tab_panel(files_tab):
                run_files(run)

        with ui.row().classes("w-full items-center gap-2 border-t border-slate-100 pt-3"):
            ui.button("Open folder", icon="folder_open", on_click=lambda: open_folder(run.path)) \
                .props("outline color=grey-8")
            ui.space()

            async def cancel():
                queue.cancel(run)
                ui.notify(f"Cancelled {run.id}")
                dialog.close()
                on_change and on_change()

            async def again():
                queue.requeue(run)
                ui.notify(f"Queued {run.id} again")
                dialog.close()
                on_change and on_change()

            async def delete():
                later = run.descendants()
                text = f"Delete run **{run.id}** of *{step.title}* with all its files?"
                if later:
                    text += (f"\n\n**{len(later)} later run{'s' if len(later) != 1 else ''} continue it** "
                             f"({', '.join(f'{r.step.number} · {r.id}' for r in later[:8])}"
                             f"{', …' if len(later) > 8 else ''}): they are deleted too.")
                if any(r.status in ACTIVE + (STARTING,) for r in later):
                    ui.notify("A later run is queued or running: cancel it first", type="negative")
                    return
                if await confirm(text):
                    for r in reversed(later):
                        r.delete()
                    run.delete()
                    queue.changed()
                    ui.notify(f"Deleted {run.id}" + (f" and {len(later)} later run(s)" if later else ""))
                    dialog.close()
                    on_change and on_change()

            if run.status not in ACTIVE + (STARTING,):
                ui.button("Delete", icon="delete_outline", on_click=delete).props("flat color=negative")
            if run.status in ACTIVE + (STARTING,):
                ui.button("Cancel run", icon="stop", color="negative", on_click=cancel)
            if run.status in (FAILED, CANCELLED) and not step.manual:
                ui.button("Run again", icon="replay", on_click=again)
    dialog.on("hide", lambda: dialog.delete())
    dialog.open()


def run_details(run, dialog, on_change):
    step = run.step
    with ui.grid(columns=2).classes("w-full gap-x-10 gap-y-2"):
        with ui.column().classes("gap-0"):
            ui.label("Parameters").classes("label-sm mb-1")
            if step.params:
                labels = {p.name: p.label for p in step.params}
                for name, value in run.params.items():
                    key_value(labels.get(name, name), value)
            else:
                ui.label("none").classes("text-sm text-slate-400")
            if run.results:
                ui.label("Results").classes("label-sm mt-4 mb-1")
                for name, value in run.results.items():
                    key_value(name, value)

            ui.label("Made from").classes("label-sm mt-4 mb-1")
            upstream = run.upstream()
            inputs = run.data.get("inputs", {})
            if not inputs:
                ui.label("—").classes("text-sm text-slate-400")
            for spec in step.inputs:
                entry = inputs.get(spec.name)
                if not entry:
                    continue
                with ui.row().classes("w-full items-center gap-3 no-wrap py-1"):
                    ui.label(spec.label).classes("text-sm text-slate-500 w-28 shrink-0")
                    if spec.name in upstream:
                        up = upstream[spec.name]
                        text = f"{up.step.number} · {up.id}" + (f" / {Path(entry['path']).name}"
                                                                 if entry.get("path") else "")
                        ui.label(text).classes(f"pill pill-{up.status} clickable font-mono") \
                            .on("click", lambda u=up: (dialog.close(), run_dialog(u, on_change)))
                    elif entry.get("run"):
                        ui.label(f"{entry['run']} (deleted)").classes("font-mono text-xs text-negative")
                    else:
                        ui.label(Path(entry.get("path") or "").name).classes("font-mono text-xs text-slate-700")
            for title, related in [("Continues", [run.parent] if run.parent else []),
                                   ("Continued by", run.children())]:
                if not related:
                    continue
                ui.label(title).classes("label-sm mt-4 mb-1")
                with ui.row().classes("gap-1.5"):
                    for other in related:
                        ui.label(f"{other.step.number} · {other.id}").classes(f"pill pill-{other.status} clickable") \
                            .on("click", lambda o=other: (dialog.close(), run_dialog(o, on_change)))

        with ui.column().classes("gap-0"):
            ui.label("Run").classes("label-sm mb-1")
            stamp = lambda key: (run.data.get(key) or "").replace("T", "  ")
            for label, value in [("Created", stamp("created")), ("Started", stamp("started")),
                                 ("Finished", stamp("finished")),
                                 ("Duration", format_duration(run.duration())),
                                 ("Pipeline commit", run.data.get("git_commit"))]:
                if value:
                    key_value(label, value)
            path_row("Folder", run.path)
            notes = ui.textarea("Notes", value=run.data.get("notes", "")).classes("w-full mt-3").props("autogrow")
            notes.on("blur", lambda: (run.update(notes=notes.value), on_change and on_change()))

    if run.data.get("command"):
        import subprocess as sp
        ui.label("Command").classes("label-sm mt-4")
        with ui.row().classes("w-full items-start gap-1 no-wrap code-box"):
            command = sp.list2cmdline(run.data["command"])
            ui.label(command).classes("font-mono text-xs text-slate-700 break-all grow")
            copy_button(command)
    if step.manual and step.instructions and step.key != "ilastik_training":
        with ui.expansion("Instructions", icon="help_outline").classes("w-full"):
            ui.markdown(instructions_md(step, run_context(run)))


def run_files(run):
    files = run.files()
    if not files:
        ui.label("No files yet").classes("text-sm text-slate-400")
        return
    preview = ui.column().classes("w-full")
    with ui.list().props("dense separator").classes("w-full border border-slate-200 rounded-xl overflow-hidden"):
        for rel in files[:300]:
            path = run.path / rel
            with ui.item():
                with ui.item_section().props("avatar").classes("min-w-0 pr-2"):
                    ui.icon({".h5": "view_in_ar", ".png": "image", ".json": "data_object", ".tif": "image",
                             ".txt": "notes", ".ilp": "model_training"}.get(path.suffix, "draft"),
                            size="18px").classes("text-slate-400")
                with ui.item_section():
                    ui.item_label(rel.as_posix()).classes("font-mono text-xs text-slate-700")
                with ui.item_section().props("side"):
                    with ui.row().classes("items-center gap-1"):
                        ui.label(format_size(path.stat().st_size)).classes("text-xs text-slate-400 font-mono")
                        if path.suffix == ".h5":
                            ui.button(icon="visibility", on_click=lambda p=path: open_napari(p)) \
                                .props("flat dense round size=sm color=grey-7").tooltip("Open in napari")
                        if path.suffix in (".png", ".json", ".txt", ".csv"):
                            ui.button(icon="preview", on_click=lambda p=path: show_preview(preview, p)) \
                                .props("flat dense round size=sm color=grey-7").tooltip("Show")
                        copy_button(path)
    if len(files) > 300:
        ui.label(f"… and {len(files) - 300} more").classes("text-gray-500")
    pngs = [run.path / f for f in files if f.suffix == ".png"]
    if pngs:
        show_preview(preview, pngs[0])


def show_preview(container, path):
    container.clear()
    with container:
        ui.label(path.name).classes("label-sm mt-2")
        if path.suffix == ".png":
            ui.image(file_url(path)).classes("w-full max-w-3xl rounded-xl border border-slate-200")
        else:
            text = path.read_text(encoding="utf-8", errors="replace")
            if path.suffix == ".json":
                try:
                    text = json.dumps(json.loads(text), indent=2)
                except json.JSONDecodeError:
                    pass
            ui.label(text[:50_000]).classes("whitespace-pre font-mono text-xs code-box w-full max-h-96 overflow-auto")


# ---------------------------------------------------------------------------------------------------------------------
# Making runs

class NewRunForm:
    """Parameters for a step on one branch: queue (script) or start (manual) runs that continue `parent`.

    The inputs come from the branch (parent and the runs before it); a choice is shown only where that run has
    several matching files.
    """

    def __init__(self, sample, channel, step, parent, on_created):
        self.sample = sample
        self.channel = channel
        self.step = step
        self.parent = parent
        self.on_created = on_created
        self.options = {spec.name: input_options(sample, spec, parent) for spec in step.inputs}
        self.selects = {}
        self.fields = {}
        self.build()

    @staticmethod
    def key(option):
        return f"{option.get('run') or ''}|{option.get('path') or ''}"

    def defaults(self):
        """Parameter defaults, with values taken over from the branch (e.g. the thresholds chosen in step 7)."""
        values = {param.name: param.default for param in self.step.params}
        for spec in self.step.inputs:
            options = self.options.get(spec.name) or []
            if spec.kind == "values" and options:
                results = self.sample.run(options[0]["run"]).results
                for param_name, result_name in spec.fills.items():
                    if results.get(result_name) is not None:
                        values[param_name] = results[result_name]
        return values

    def build(self):
        step = self.step
        defaults = self.defaults()
        with ui.element("div").classes("form-panel w-full"):
            # Input folders, e.g. the crops to choose the thresholds in: the path, to copy or open in the explorer
            folders = input_folders(self.sample, step, self.parent)
            for label, path in folders:
                folder_box(label, path)
            with ui.row().classes("w-full items-start gap-3" + (" mt-6" if folders else "")):
                for spec in step.inputs:
                    options = self.options[spec.name]
                    if len(options) > 1:        # only then there is something to choose
                        select = ui.select({self.key(o): o["label"] for o in options}, label=spec.label,
                                           value=self.key(options[0]), on_change=self.preview) \
                            .classes("min-w-72").props("options-dense")
                        if spec.help:
                            select.tooltip(spec.help)
                        self.selects[spec.name] = select
                for param in step.params:
                    if not param.group:
                        self.param_field(param, defaults)
            # Grouped parameters (e.g. the crop size z / y / x): each group in one row of its own
            for group in dict.fromkeys(p.group for p in step.params if p.group):
                with ui.row().classes("w-full items-start gap-3 no-wrap mt-3"):
                    for param in step.params:
                        if param.group == group:
                            self.param_field(param, defaults)
            self.message = ui.column().classes("gap-1 mt-3")
            with ui.row().classes("w-full items-center gap-4 mt-3"):
                ui.space()
                self.button = ui.button("Start" if step.manual else "Run",
                                        icon="play_arrow" if not step.manual else "back_hand", on_click=self.submit)
        self.preview()

    def param_field(self, param, defaults):
        """The input (or select) of one parameter, filled with its default."""
        default = defaults.get(param.name)
        if param.type == "choice":
            field = ui.select(param.options(), label=param.label, value=default,
                              on_change=self.preview).classes("min-w-40")
        else:
            text = "" if default is None else f"{default:g}" if isinstance(default, float) else str(default)
            field = ui.input(param.label, value=text, on_change=self.preview) \
                .classes("w-52" if param.type != "str" else "min-w-96")
            if param.sweep:
                field.props('hint="several: 30, 40, 50"')
        if param.help:
            field.tooltip(param.help)
        self.fields[param.name] = field

    def chosen(self):
        out = {}
        for spec in self.step.inputs:
            options = self.options.get(spec.name) or []
            select = self.selects.get(spec.name)
            if select is not None:
                out[spec.name] = next((o for o in options if self.key(o) == select.value), None)
            else:
                out[spec.name] = options[0] if options else None
        return out

    def plan(self):
        """(list of (params, ctx), errors, warnings)"""
        chosen = self.chosen()
        missing = next((spec for spec in self.step.inputs if not spec.optional and not chosen.get(spec.name)), None)
        if missing:
            return [], [f"No '{missing.label}' on this branch"], []
        try:
            combos = expand_params(self.step, {name: field.value for name, field in self.fields.items()})
        except ValueError as error:
            return [], [str(error)], []
        planned, errors, warnings = [], [], []
        for params in combos:
            ctx = make_context(self.sample, self.step, params, chosen, channel=self.channel, parent=self.parent)
            error = self.step.validate(ctx)
            if error:
                errors.append(error)
                continue
            planned.append((params, ctx))
        if planned:
            try:
                warnings = self.step.warnings(planned[0][1])
            except Exception as error:      # a check must never block the form
                warnings = [f"Could not check the inputs: {error}"]
        return planned, errors, warnings

    def existing(self):
        return [r for r in self.sample.runs(self.step.key, self.channel) if r.status in (DONE,) + ACTIVE]

    def preview(self, *_):
        planned, errors, warnings = self.plan()
        chosen = self.chosen()
        existing = self.existing() if not self.step.manual else []
        duplicates = [p for p, _ in planned if any(same_run(r, p, chosen, self.parent) for r in existing)]
        self.message.clear()
        with self.message:
            for error in dict.fromkeys(errors):
                # A field not filled in yet is a hint, not an error
                missing = error.endswith("a value is needed")
                with ui.row().classes("items-center gap-1.5 text-sm no-wrap "
                                      + ("text-slate-500" if missing else "text-rose-600")):
                    ui.icon("info_outline" if missing else "error_outline", size="16px")
                    ui.label(error.replace(": a value is needed", ": enter a value") if missing else error)
            for warning in warnings:
                with ui.row().classes("items-start gap-1.5 text-sm text-amber-700 no-wrap"):
                    ui.icon("warning_amber", size="16px").classes("mt-0.5")
                    ui.label(warning)
            if planned and not self.step.manual and len(planned) > 1:
                labels = [self.step.label(ctx) or "(no label)" for _, ctx in planned]
                with ui.row().classes("items-center gap-1.5 flex-wrap"):
                    ui.label(f"Will create {len(planned)} runs, each its own branch:") \
                        .classes("text-sm text-slate-500")
                    for label in labels[:12]:
                        ui.label(label).classes("font-mono text-xs bg-white border border-slate-200 rounded-md "
                                                "px-1.5 py-0.5 text-slate-700")
                    if len(labels) > 12:
                        ui.label("…").classes("text-slate-400")
            if duplicates:      # these are skipped
                ui.label(f"{len(duplicates)} of them already computed or queued here: skipped"
                         if len(planned) > 1 else "Already computed or queued with these settings") \
                    .classes("text-xs text-slate-500")
        n = len(planned) - len(duplicates)
        self.button.set_enabled(n > 0 and not errors)

    def submit(self):
        planned, errors, _ = self.plan()
        if errors or not planned:
            ui.notify(errors[0] if errors else "Nothing to run", type="negative")
            return
        chosen = self.chosen()
        existing = self.existing()
        made, skipped = [], 0
        for params, _ in planned:
            if any(same_run(r, params, chosen, self.parent) for r in existing):     # never compute the same twice
                skipped += 1
                continue
            made.append(create_run(self.sample, self.step, params, chosen, self.channel, self.parent))
        Ctx.queue.changed()
        if self.step.manual:
            ui.notify(f"Started {made[0].id}: follow the instructions", type="positive")
        else:
            text = f"Queued {len(made)} run(s)" + (f", skipped {skipped} already computed" if skipped else "")
            ui.notify(text, type="positive" if made else "warning")
        self.on_created(made)


def manual_run_card(run, on_change):
    """An open manual run: instructions with the real paths, tools, result fields, 'Mark as done'."""
    step = run.step
    ctx = run_context(run)
    brief = step.key == "ilastik_training"
    with ui.element("div").classes("manual-card w-full"):
        with ui.row().classes("w-full items-center gap-3"):
            with ui.element("div").classes("step-num manual"):
                ui.icon("back_hand", size="16px")
            with ui.column().classes("gap-0 grow"):
                with ui.row().classes("items-center gap-2"):
                    ui.label(f"Run {run.id}").classes("section-title font-mono")
                    status_pill(run.status)
                ui.label(f"Started {short_time(run.data.get('created'))}"
                         + ("" if brief else " · follow the steps, then mark it as done")).classes("section-hint")
            ui.button("Details", on_click=lambda: run_dialog(run, on_change)).props("flat color=grey-8")
        # Input folders (e.g. the crops of the step before): the path, to copy or open in the explorer
        for label, path in run_input_folders(run):
            folder_box(label, path)
        # Pictures from the inputs, e.g. where the crops are
        pictures = [p for path in ctx.inputs.values() if path is not None and path.is_dir()
                    for p in sorted(path.glob("*.png"))]
        if brief:
            # Train ilastik: just the crops (path above, pictures here), no instructions or tool buttons
            with ui.row().classes("w-full gap-3 mt-3"):
                for picture in pictures[:2]:
                    image = ui.image(file_url(picture)) \
                        .classes("w-96 rounded-xl border border-slate-200 cursor-zoom-in")
                    image.on("click", lambda p=picture: zoom_image(p))
        with ui.row().classes("w-full items-start gap-6 no-wrap mt-3") as instructions_row:
            with ui.column().classes("grow gap-3"):
                ui.markdown(instructions_md(step, ctx)).classes("text-sm text-slate-700")
                with ui.row().classes("gap-2"):
                    if "ilastik" in step.tools:
                        ui.button("Open ilastik", icon="open_in_new", on_click=open_ilastik)
                    if "napari" in step.tools:
                        paths = [p for p in ctx.inputs.values() if p is not None and p.suffix == ".h5"]
                        ui.button("Open in napari", icon="open_in_new", on_click=lambda: open_napari(*paths))
                    ui.button("Open run folder", icon="folder_open", on_click=lambda: open_folder(run.path)) \
                        .props("outline color=grey-8")
            if pictures:
                with ui.column().classes("w-80 shrink-0"):
                    for picture in pictures[:2]:
                        image = ui.image(file_url(picture)) \
                            .classes("w-80 rounded-xl border border-slate-200 cursor-zoom-in")
                        image.on("click", lambda p=picture: zoom_image(p))
        instructions_row.set_visibility(not brief)

        fields = {}
        with ui.row().classes("w-full items-start gap-3 mt-4 pt-4 border-t border-slate-100"):
            if step.key == "ilastik_training":
                ilp = ui.input("Import an .ilp saved elsewhere").classes("min-w-96")

                async def browse():
                    chosen = await pick_file(Path(ilp.value).parent if ilp.value else run.path,
                                             (".ilp",), "Choose the trained ilastik project")
                    if chosen:
                        ilp.value = str(chosen)

                ui.button(icon="folder_open", on_click=browse).props("flat round color=grey-7").tooltip("Browse")

                def import_ilp():
                    source = Path(ilp.value.strip().strip('"'))
                    if not source.is_file() or source.suffix != ".ilp":
                        ui.notify(f"Not an .ilp file: {source}", type="negative")
                        return
                    shutil.copy2(source, run.path / "project.ilp")
                    run.update(imported_from=str(source))
                    ui.notify("Copied into the run folder as project.ilp", type="positive")

                ui.button("Import", on_click=import_ilp).props("outline color=grey-8").classes("h-10")
            for param in step.results:
                value = run.results.get(param.name)
                fields[param.name] = ui.input(param.label, value="" if value is None else str(value)) \
                    .classes("w-48")
        notes = ui.textarea("Notes", value=run.data.get("notes", "")).classes("w-full mt-3").props("autogrow")

        def save(silent=False):
            results = {}
            for param in step.results:
                try:
                    results[param.name] = param.parse(fields[param.name].value)[0]
                except ValueError as error:
                    if not silent:
                        ui.notify(str(error), type="negative")
                        return False
                    results[param.name] = None
            run.update(results=results, notes=notes.value)
            return True

        def mark_done():
            if not save():
                return
            run.reload()
            problem = step.check_done(run)
            if problem:
                ui.notify(problem, type="negative")
                return
            run.update(status=DONE, finished=now())
            Ctx.queue.changed()
            ui.notify(f"{run.id} done", type="positive")
            on_change()

        async def discard():
            if await confirm(f"Discard run **{run.id}** and delete its folder?"):
                run.delete()
                Ctx.queue.changed()
                on_change()

        with ui.row().classes("w-full items-center gap-2 mt-3"):
            ui.button("Discard", icon="delete_outline", on_click=discard).props("flat color=negative")
            ui.space()
            ui.button("Save", on_click=lambda: save() and ui.notify("Saved")).props("outline color=grey-8")
            ui.button("Mark as done", icon="check", color="positive", on_click=mark_done)


def zoom_image(path):
    with ui.dialog() as dialog, ui.card().classes("dialog-card max-w-[95vw] p-2"):
        ui.image(file_url(path)).classes("w-[90vw] max-w-[1400px] rounded-xl")
    dialog.on("hide", lambda: dialog.delete())
    dialog.open()
