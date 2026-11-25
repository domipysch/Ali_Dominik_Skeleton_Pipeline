"""The pages of the GUI."""
from pathlib import Path
from urllib.parse import quote

from nicegui import app, run as background, ui

from . import theme
from .jobs import STARTING, JobQueue, progress
from .steps import (STEP_INDEX, STEP_LIST, branches, chain, create_run, input_options, is_skipped, next_steps,
                    same_run, skip_run)
from .store import ACTIVE, CANCELLED, DONE, FAILED, OPEN, QUEUED, RUNNING, format_duration, git_commit
from .widgets import (STATUS_TEXT, Ctx, NewRunForm, confirm, instructions_md, log_view, manual_run_card, open_folder,
                      pick_file, run_dialog, run_where, runs_table, short_time, status_pill)


def on_version_change(callback, interval=1.0):
    """Call callback whenever a run status changed somewhere (job started / finished, run created / deleted)."""
    seen = {"version": Ctx.queue.version}

    def check():
        if Ctx.queue.version != seen["version"]:
            seen["version"] = Ctx.queue.version
            callback()
    ui.timer(interval, check)


def header(active):
    theme.page_colors()
    with ui.header().classes("topbar px-6 py-0 h-16 items-center gap-8 no-wrap"):
        with ui.row().classes("items-center gap-3 no-wrap cursor-pointer").on("click", lambda: ui.navigate.to("/")):
            with ui.column().classes("gap-0"):
                ui.label(Ctx.project.name).classes("text-[15px] font-semibold leading-tight")
                ui.label(str(Ctx.project.root)).classes("text-[11px] text-slate-400 font-mono whitespace-nowrap")
        ui.space()
        with ui.row().classes("gap-1 no-wrap"):
            for label, target in [("Samples", "/"), ("Queue", "/queue"), ("Settings", "/settings")]:
                ui.button(label, on_click=lambda t=target: ui.navigate.to(t)) \
                    .props("flat color=" + ("primary" if label == active else "grey-7")) \
                    .classes("nav-link" + (" active" if label == active else ""))

        @ui.refreshable
        def job_indicator():
            current = Ctx.queue.current
            if current is not None:
                with ui.row().classes("job-chip busy items-center gap-2 no-wrap") \
                        .on("click", lambda: ui.navigate.to("/queue")):
                    ui.spinner(size="14px", color="primary", thickness=6)
                    ui.label(f"{run_where(current)} · {current.step.title}").classes("truncate max-w-[300px]")
                    found = progress(current.log_path)
                    if found:
                        ui.label(f"{found[0]:.0%}").classes("font-mono font-medium")
                return
            n_queued = len(Ctx.queue.queued())
            with ui.row().classes("job-chip items-center gap-2 no-wrap text-slate-500"):
                ui.icon("schedule" if n_queued else "check_circle", size="16px") \
                    .classes("text-slate-400" if n_queued else "text-emerald-500")
                ui.label(f"{n_queued} queued" if n_queued else "Idle")

        job_indicator()
        ui.timer(2.0, job_indicator.refresh)


def page(width="max-w-[1400px]"):
    return ui.column().classes(f"w-full {width} mx-auto px-6 py-8 gap-6")


def page_heading(title, subtitle=""):
    """Title row; returns (row, subtitle label). Actions added to the row go to its right."""
    row = ui.row().classes("w-full items-end gap-4 no-wrap")
    with row:
        with ui.column().classes("gap-1"):
            ui.label(title).classes("page-title")
            subtitle_label = ui.label(subtitle).classes("page-subtitle")
        ui.space()
    return row, subtitle_label


def sample_url(sample_id, channel=None, branch=None):
    url = f"/sample/{sample_id}"
    query = []
    if channel is not None:
        query.append(f"channel={channel}")
    if branch:
        query.append(f"branch={quote(branch, safe='')}")
    return url + ("?" + "&".join(query) if query else "")


# ---------------------------------------------------------------------------------------------------------------------
# The samples table: one row per channel (sample and tag cells span the rows of their sample)

def last_done(sample, channel):
    """The finished run of the furthest step of a channel (on any branch), or None."""
    done = [run for run in sample.runs(channel=channel) if run.status == DONE]
    if not done:
        return None
    return max(done, key=lambda run: (STEP_INDEX[run.data["step"]], run.data.get("finished") or ""))


def channel_row(sample, channel):
    """A table row (without spans) for one channel, plus the group id of each cell for merging."""
    run = last_done(sample, channel)
    status = None
    if run is not None:
        status = dict(text=f"{run.step.number} · {run.step.title}" + (" (skipped)" if is_skipped(run) else ""),
                      tip=f"{run.rel_path}")
    row = dict(key=f"{sample.id}|{channel}", sample_id=sample.id, channel=channel, branch="", name=sample.name,
               tag=sample.tag or "", structure=sample.structure(channel) or "", status=status)
    row["groups"] = dict(name=sample.id, tag=sample.id, structure=row["key"], status=row["key"])
    return row


def add_spans(rows, columns):
    """row['span'][column]: how many rows the cell spans (0: covered by the cell above)."""
    for row in rows:
        row["span"] = {}
    for column in columns:
        start = 0
        for i in range(1, len(rows) + 1):
            if i == len(rows) or rows[i]["groups"][column] != rows[start]["groups"][column]:
                rows[start]["span"][column] = i - start
                for j in range(start + 1, i):
                    rows[j]["span"][column] = 0
                start = i
    for row in rows:
        del row["groups"]
    return rows


BRANCH_TABLE_BODY = r"""
<q-tr :props="props" :class="props.row.selected ? 'selected-branch' : ''">
  <template v-for="col in props.cols" :key="col.name">
    <td v-if="props.row.span[col.name]" :rowspan="props.row.span[col.name]"
        class="branch-cell info-cell"
        @click="$parent.$emit('open', {sample: props.row.sample_id, channel: props.row.channel, branch: props.row.branch})">
      <template v-if="col.name === 'status'">
        <span v-if="props.row.status" class="done-cell">
          <q-icon name="check" size="16px" class="text-emerald-600" />{{ props.row.status.text }}
          <q-tooltip>{{ props.row.status.tip }}</q-tooltip>
        </span>
        <span v-else class="text-slate-400">Not started</span>
      </template>
      <span v-else-if="col.name === 'tag'"><span v-if="props.row.tag" class="tag">{{ props.row.tag }}</span></span>
      <span v-else-if="col.name === 'structure'" class="structure">{{ props.row.structure }}</span>
      <span v-else-if="col.name === 'name'" class="font-semibold text-slate-900">{{ props.row.name }}</span>
    </td>
  </template>
</q-tr>
"""


def branch_table(columns, on_open):
    with ui.element("div").classes("surface w-full overflow-x-auto") as box:
        table = ui.table(columns=columns, rows=[], row_key="key", pagination=dict(rowsPerPage=0)) \
            .props("flat hide-bottom").classes("modern-table branch-table w-full")
    table.add_slot("body", BRANCH_TABLE_BODY)
    table.on("open", lambda e: on_open(e.args))
    return box, table


# ---------------------------------------------------------------------------------------------------------------------
# Samples overview

@ui.page("/")
def samples_page():
    header("Samples")
    ui.page_title(f"Samples · {Ctx.project.name}")
    with page():
        heading, subtitle = page_heading("Samples")
        subtitle.set_visibility(False)
        with heading:
            ui.button("Import", icon="file_download", on_click=lambda: import_dialog()) \
                .props("flat color=grey-8")
            ui.button("Add sample", icon="add", on_click=lambda: add_sample_dialog())

        columns = [dict(name="name", label="Sample", field="name", align="left"),
                   dict(name="tag", label="Tag", field="tag", align="left"),
                   dict(name="structure", label="Structure", field="structure", align="left"),
                   dict(name="status", label="Status", field="status", align="left")]
        table_box, table = branch_table(
            columns, lambda a: ui.navigate.to(sample_url(a["sample"], a["channel"], a["branch"])))

        with ui.element("div").classes("surface w-full empty-state") as empty:
            with ui.element("div").classes("empty-icon"):
                ui.icon("science", size="26px")
            ui.label("No samples yet").classes("section-title mt-4")
            ui.label("Add a sample with its .ims file to start the pipeline.").classes("section-hint mt-1")
            ui.button("Add sample", icon="add", on_click=lambda: add_sample_dialog()).classes("mt-5")

        def refresh():
            rows = []
            for sample in Ctx.project.samples():
                for channel in sample.channels:
                    rows.append(channel_row(sample, channel["index"]))
            table.rows = add_spans(rows, [c["name"] for c in columns])
            table.update()
            table_box.set_visibility(bool(rows))
            empty.set_visibility(not rows)

        refresh()
        on_version_change(refresh)


def dialog_title(title, subtitle=None):
    with ui.column().classes("gap-1 mb-2"):
        ui.label(title).classes("text-lg font-semibold")
        if subtitle:
            ui.label(subtitle).classes("section-hint")


SKIP_CHANNEL = "__skip__"      # in a channel's structure select: do not process this channel for now


def channel_structures(selects):
    """{channel index: structure, or None for a skipped channel} from the selects of channel_rows."""
    return {index: None if select.value == SKIP_CHANNEL else select.value for index, select in selects.items()}


def channel_rows(channels, structures, values=None, title=None, size=None, extra=()):
    """One row per channel (its name, its data file, a structure select): {channel index: select}.

    channels: [dict(index, name, file, size)]; title / size: the header of the card; extra: other files copied.
    Each select also offers 'Skip for now' (value SKIP_CHANNEL)."""
    from .ims import format_bytes, short_channel_name
    selects = {}
    with ui.column().classes("w-full gap-0 code-box"):
        with ui.row().classes("w-full items-center justify-between mb-1 no-wrap gap-3"):
            ui.label(title or "Channels").classes("text-xs font-semibold text-slate-600 truncate")
            if size is not None:
                ui.label(format_bytes(size)).classes("text-xs font-semibold text-slate-600 font-mono shrink-0")
        for channel in channels:
            with ui.row().classes("w-full items-center gap-3 no-wrap py-2 border-t border-slate-200"):
                with ui.column().classes("gap-0 grow min-w-0"):
                    label = short_channel_name(channel.get("name")) or f"Channel {channel['index']}"
                    ui.label(label).classes("font-mono text-sm font-medium text-slate-800 truncate") \
                        .tooltip(channel.get("name") or label)
                    file = channel.get("file")
                    details = (Path(file).name if file else "stored in the .ims") + (
                        f" · {format_bytes(channel['size'])}" if channel.get("size") is not None else "")
                    ui.label(details).classes("font-mono text-xs text-slate-400 truncate").tooltip(details)
                options = {s: s for s in structures} | {SKIP_CHANNEL: "Skip for now"}
                selects[channel["index"]] = ui.select(options, label="Structure",
                                                      value=(values or {}).get(channel["index"])) \
                    .classes("w-48 shrink-0")
        for rel, n in extra:
            with ui.row().classes("w-full items-center justify-between gap-3 no-wrap py-1 border-t border-slate-200"):
                ui.label(rel).classes("font-mono text-xs text-slate-500 truncate")
                ui.label(format_bytes(n)).classes("font-mono text-xs text-slate-400 shrink-0")
    return selects


def add_sample_dialog(on_added=None):
    """on_added(sample): called after adding instead of opening the sample's page (e.g. from the import dialog)."""
    from .ims import bundle, format_bytes
    project = Ctx.project
    state = {"bundle": None, "done": 0, "total": 0, "structures": {}}
    with ui.dialog() as dialog, ui.card().classes("dialog-card w-[920px]").style("max-width: 95vw"):
        # (inline max-width: Quasar limits dialog cards to 560px with a more specific CSS rule)
        dialog_title("Add sample")
        with ui.column().classes("w-full gap-3"):
            with ui.row().classes("w-full gap-3 no-wrap"):
                name = ui.input("Name").classes("grow")
                tag = ui.select(project.tags, label="Tag", value=project.tags[0] if project.tags else None) \
                    .classes("w-48")
            with ui.row().classes("w-full items-start gap-2 no-wrap"):
                ims = ui.input("Imaris file (.ims)").classes("grow font-mono")

                async def browse():
                    start = Path(ims.value).parent if ims.value else None
                    chosen = await pick_file(start, (".ims",), "Choose the .ims of this sample")
                    if chosen:
                        ims.value = str(chosen)
                        if not name.value:
                            name.value = chosen.stem
                        await inspect()
                browse_button = ui.button("Browse", icon="folder_open", on_click=browse) \
                    .props("outline").classes("h-10")
            files_box = ui.column().classes("w-full gap-3")
            notes = ui.textarea("Notes (optional)").classes("w-full").props("autogrow")

        progress_box = ui.column().classes("w-full gap-1")
        progress_box.set_visibility(False)
        with progress_box:
            bar = ui.linear_progress(0, show_value=False, size="8px").classes("w-full")
            bar_text = ui.label("").classes("text-xs text-slate-500 font-mono")

        def ims_path():
            return Path(ims.value.strip().strip('"')) if ims.value and ims.value.strip() else None

        async def inspect():
            """Show which files will be copied (or why not) and the channels."""
            path = ims_path()
            files_box.clear()
            state["bundle"], state["structures"] = None, {}
            if path is None:
                return
            found = await background.io_bound(bundle, path)
            if ims_path() != path:          # the field changed meanwhile
                return
            state["bundle"] = found
            if not name.value and not found.problems:
                name.value = path.stem
            with files_box:
                if found.problems:
                    for problem in found.problems:
                        with ui.row().classes("items-start gap-1.5 text-sm text-rose-600 no-wrap"):
                            ui.icon("error_outline", size="16px").classes("mt-0.5")
                            ui.label(problem)
                    return
                # One card: the channels with the file each one's data is in, and what gets copied in total
                sizes = {rel: source.stat().st_size for source, rel in found.files}
                channels = [dict(index=c.index, name=c.name, file=c.file, size=sizes.get(c.file))
                            for c in found.channels]
                used = {c.file for c in found.channels} | {found.files[0][1]}
                extra = [(rel.as_posix(), n) for rel, n in sizes.items() if rel not in used]
                n_files = len(found.files)
                title = (f"{len(channels)} channel{'s' if len(channels) != 1 else ''} · {n_files} "
                         f"file{'s' if n_files != 1 else ''} to copy (.ims included)")
                state["structures"] = channel_rows(channels, project.structures, title=title, size=found.size,
                                                   extra=extra)

        ims.on("blur", inspect)
        ims.on("keydown.enter", inspect)

        def update_progress():
            if state["total"]:
                bar.set_value(state["done"] / state["total"])
                bar_text.set_text(f"Copying… {format_bytes(state['done'])} of {format_bytes(state['total'])}")
        progress_timer = ui.timer(0.5, update_progress, active=False)

        async def submit():
            if not name.value or ims_path() is None:
                ui.notify("Name and .ims are needed", type="negative")
                return
            if state["bundle"] is None or state["bundle"].ims != ims_path():
                await inspect()
            if state["bundle"] is None or state["bundle"].problems:
                ui.notify("This .ims cannot be copied, see above", type="negative")
                return
            if not all(select.value for select in state["structures"].values()):
                ui.notify("Choose a structure (or 'Skip for now') for every channel", type="negative")
                return
            structures = channel_structures(state["structures"])
            if not any(structures.values()):
                ui.notify("All channels are skipped: choose a structure for at least one", type="negative")
                return

            def progress(done, total):
                state["done"], state["total"] = done, total

            # While copying: no closing, no editing
            elements = [add_button, cancel_button, browse_button, ims, name, tag, notes,
                        *state["structures"].values()]
            dialog.props("persistent")
            for element in elements:
                element.disable()
            progress_box.set_visibility(True)
            progress_timer.activate()
            try:
                sample = await background.io_bound(
                    project.add_sample, name.value.strip(), tag.value, ims_path(), structures, notes.value, progress)
            except (ValueError, OSError) as error:
                progress_timer.deactivate()
                progress_box.set_visibility(False)
                dialog.props(remove="persistent")
                for element in elements:
                    element.enable()
                ui.notify(str(error), type="negative", multi_line=True)
                return
            progress_timer.deactivate()
            dialog.close()
            Ctx.queue.changed()
            ui.notify(f"Added {sample.name}", type="positive")
            if on_added is not None:
                on_added(sample)        # (otherwise: stay on the page; the samples table shows the new sample)

        with ui.row().classes("w-full justify-end gap-2 mt-2"):
            cancel_button = ui.button("Cancel", on_click=dialog.close).props("flat color=grey-8")
            add_button = ui.button("Add sample", on_click=submit)
    dialog.on("hide", lambda: dialog.delete())
    dialog.open()


def import_dialog():
    """Results made outside the GUI -> finished runs of one new branch of a sample's channel."""
    from .ims import format_bytes, short_channel_name
    from .importer import SKIP_PARAMS, TARGETS, Item, check, import_branch
    project = Ctx.project
    new = "__new__"
    state = {"sample": None, "done": 0, "total": 0}
    rows = {}           # step key -> dict(source, skipped, params, results): the fields of that step

    with ui.dialog() as dialog, ui.card().classes("dialog-card scroll-card w-[1100px] no-wrap") \
            .style("max-width: 95vw; max-height: 90vh; overflow-y: auto"):
        dialog_title("Import results")
        with ui.row().classes("w-full gap-3 no-wrap"):
            sample_select = ui.select({}, label="Sample").classes("grow")
            channel_select = ui.select({}, label="Channel").classes("w-96")

        with ui.column().classes("w-full gap-0 code-box"):
            for step in STEP_LIST:
                target = TARGETS[step.key]
                fields = dict(source=None, skipped=None, params={}, results={})
                rows[step.key] = fields
                if step.coming_soon:
                    with ui.row().classes("w-full items-center gap-3 no-wrap py-2 border-t border-slate-200"):
                        with ui.row().classes("w-64 shrink-0 items-center gap-2 no-wrap"):
                            ui.label(step.number).classes("node-num")
                            ui.label(step.title).classes("text-sm font-medium text-slate-400 truncate")
                        ui.label("Coming soon").classes("soon-badge")
                    continue
                with ui.row().classes("w-full items-center gap-3 no-wrap py-2 border-t border-slate-200 "
                                      "first:border-t-0"):
                    with ui.row().classes("w-64 shrink-0 items-center gap-2 no-wrap"):
                        ui.label(step.number).classes("node-num" + (" manual" if step.manual else ""))
                        ui.label(step.title).classes("text-sm font-medium text-slate-800 truncate")
                    if target.kind != "none":
                        what = "folder" if target.kind == "dir" else " / ".join(target.suffixes) + " file"
                        source = ui.input(placeholder=f"{what} (empty: not imported)") \
                            .classes("grow font-mono").props("dense outlined clearable")
                        fields["source"] = source

                        async def browse(source=source, step=step, target=target):
                            current = Path(source.value.strip().strip('"')) if source.value else None
                            start = (current if current and current.is_dir() else current.parent if current else
                                     state.get("last_dir"))
                            chosen = await pick_file(start, target.suffixes,
                                                     f"{step.number} · {step.title}: choose the "
                                                     f"{'folder' if target.kind == 'dir' else 'file'}",
                                                     folder=target.kind == "dir")
                            if chosen:
                                source.value = str(chosen)
                                state["last_dir"] = chosen.parent
                        ui.button(icon="folder_open", on_click=browse) \
                            .props("flat round dense color=grey-7").tooltip("Browse")
                    else:
                        ui.space()
                    if step.optional:
                        fields["skipped"] = ui.checkbox("Skipped").classes("shrink-0")
                params = [p for p in step.params if p.name not in SKIP_PARAMS] + list(step.results)
                if params:
                    with ui.row().classes("w-full items-start gap-2 pb-2 pl-[264px]"):
                        for param in params:
                            if param.type == "choice":
                                field = ui.select(param.options(), label=param.label, value=None) \
                                    .props("dense outlined clearable options-dense").classes("w-44")
                            else:
                                field = ui.input(param.label).props("dense outlined").classes("w-44")
                            if param.help:
                                field.tooltip(param.help)
                            (fields["results"] if param in step.results else fields["params"])[param.name] = \
                                (param, field)

        mode = ui.radio({"copy": "Copy the files", "move": "Move the files (faster, removes them where they are)"},
                        value="copy").props("inline dense")

        progress_box = ui.column().classes("w-full gap-1")
        progress_box.set_visibility(False)
        with progress_box:
            bar = ui.linear_progress(0, show_value=False, size="8px").classes("w-full")
            bar_text = ui.label("").classes("text-xs text-slate-500 font-mono")

        def sample_options():
            options = {s.id: s.name for s in project.samples()}
            options[new] = "+ New sample…"
            sample_select.set_options(options, value=state["sample"])

        def show_channels():
            if state["sample"] is None:
                channel_select.set_options({}, value=None)
                return
            sample = project.sample(state["sample"])
            options = {c["index"]: f"{short_channel_name(c.get('name')) or 'Channel ' + str(c['index'])}"
                                   f" · {c.get('structure') or ''}" for c in sample.channels}
            channel_select.set_options(options, value=next(iter(options), None))

        def added(sample):
            state["sample"] = sample.id
            sample_options()
            show_channels()

        def on_sample(event):
            if event.value == new:
                sample_select.set_value(state["sample"])
                add_sample_dialog(on_added=added)
                return
            if event.value != state["sample"]:
                state["sample"] = event.value
                show_channels()

        sample_select.on_value_change(on_sample)
        sample_options()

        def items():
            """The steps to import, from the fields. Raises ValueError for a wrong value."""
            out = []
            for step in STEP_LIST:
                fields = rows[step.key]
                text = (fields["source"].value or "").strip().strip('"') if fields["source"] is not None else ""
                skipped = bool(fields["skipped"] and fields["skipped"].value)

                def values(group):
                    found = {}
                    for name, (param, field) in fields[group].items():
                        raw = field.value
                        if raw is None or str(raw).strip() == "":
                            continue
                        found[name] = raw if param.type == "choice" else param.parse(str(raw))[0]
                    return found

                params, results = values("params"), values("results")
                if not (text or skipped or results):
                    continue
                if text and skipped:
                    raise ValueError(f"{step.title}: either a file or 'Skipped', not both")
                out.append(Item(step.key, Path(text) if text else None, params, results, skipped))
            return out

        def update_progress():
            if state["total"]:
                bar.set_value(state["done"] / state["total"])
                verb = "Moving" if mode.value == "move" else "Copying"
                bar_text.set_text(f"{verb}… {format_bytes(state['done'])} of {format_bytes(state['total'])}")
        progress_timer = ui.timer(0.5, update_progress, active=False)

        async def submit():
            if state["sample"] is None or channel_select.value is None:
                ui.notify("Choose the sample and the channel", type="negative")
                return
            sample = project.sample(state["sample"])
            channel = channel_select.value
            try:
                chosen = items()
            except ValueError as error:
                ui.notify(str(error), type="negative")
                return
            problems = check(sample, channel, chosen)
            if problems:
                ui.notify("\n".join(problems), type="negative", multi_line=True)
                return

            def progress(done, total):
                state["done"], state["total"] = done, total

            dialog.props("persistent")
            import_button.disable()
            cancel_button.disable()
            progress_box.set_visibility(True)
            progress_timer.activate()
            try:
                made = await background.io_bound(import_branch, sample, channel, chosen, mode.value == "move",
                                                 progress)
            except (ValueError, OSError) as error:
                ui.notify(str(error), type="negative", multi_line=True)
                return
            finally:
                progress_timer.deactivate()
                progress_box.set_visibility(False)
                dialog.props(remove="persistent")
                import_button.enable()
                cancel_button.enable()
            dialog.close()
            Ctx.queue.changed()
            ui.notify(f"Imported {len(made)} step{'s' if len(made) != 1 else ''} into {sample.name}",
                      type="positive")
            ui.navigate.to(sample_url(sample.id, channel, made[-1].rel_path))

        with ui.row().classes("w-full justify-end gap-2 mt-2"):
            cancel_button = ui.button("Cancel", on_click=dialog.close).props("flat color=grey-8")
            import_button = ui.button("Import", on_click=submit)
    dialog.on("hide", lambda: dialog.delete())
    dialog.open()


# ---------------------------------------------------------------------------------------------------------------------
# One sample: one channel at a time, its runs as a tree

@ui.page("/sample/{sample_id}")
def sample_page(sample_id: str, channel: int | None = None, branch: str | None = None):
    header("Samples")
    try:
        sample = Ctx.project.sample(sample_id)
    except KeyError:
        with page():
            ui.label(f"No sample '{sample_id}'").classes("text-negative")
        return
    ui.page_title(f"{sample.name} · {Ctx.project.name}")
    channels = [c["index"] for c in sample.channels]
    if channel not in channels:
        channel = channels[0] if channels else None

    with page(width="max-w-none"):      # the tree grows sideways with every branch
        with ui.row().classes("items-center gap-1 text-sm text-slate-500 -mb-2"):
            ui.link("Samples", "/").classes("text-slate-500 no-underline hover:text-slate-900")
            ui.icon("chevron_right", size="16px")
            ui.label(sample.name).classes("text-slate-900")

        @ui.refreshable
        def info():
            s = Ctx.project.sample(sample_id)
            with ui.row().classes("w-full items-start gap-4 no-wrap"):
                with ui.column().classes("gap-2 grow"):
                    with ui.row().classes("items-center gap-3"):
                        ui.label(s.name).classes("page-title")
                        if s.tag:
                            ui.label(s.tag).classes("tag")
                    if s.data.get("notes"):
                        ui.label(s.data["notes"]).classes("text-sm text-slate-600 whitespace-pre-wrap max-w-3xl")
                with ui.row().classes("gap-2 no-wrap"):
                    ui.button("Open folder", icon="folder_open", on_click=lambda: open_folder(s.path)) \
                        .props("outline color=grey-8")
                    ui.button("Edit", icon="edit", on_click=lambda: edit_sample_dialog(s, refresh_everything)) \
                        .props("outline color=grey-8")
                    ui.button("Delete", icon="delete_outline", on_click=lambda: delete_sample(s)) \
                        .props("flat color=negative")

        def refresh_everything(saved=None):
            if saved is not None and saved.id != sample_id:     # renamed: the folder, so the page address changed
                ui.navigate.to(sample_url(saved.id, channel))
            else:
                ui.navigate.reload()

        info()
        if channel is None:
            ui.label("This sample has no channels.").classes("section-hint")
            return

        # Channels as tabs: each one is its own image with its own branches
        with ui.tabs(value=str(channel),
                     on_change=lambda e: ui.navigate.to(sample_url(sample_id, int(e.value)))) \
                .classes("w-full border-b border-slate-200 text-slate-600 -mb-2") \
                .props("align=left dense active-color=primary indicator-color=primary"):
            for c in sample.channels:
                ui.tab(str(c["index"]), label=c.get("structure") or f"Channel {c['index']}") \
                    .tooltip(f"Channel {c['index']}: {c.get('name') or ''}")

        state = {"highlight": branch}

        def signature():
            s = Ctx.project.sample(sample_id)
            return tuple((r.rel_path, r.status) for r in s.runs(channel=channel))

        seen = {"signature": signature()}

        @ui.refreshable
        def content():
            run_tree(Ctx.project.sample(sample_id), channel, state, content.refresh)

        def check():
            current = signature()
            if current != seen["signature"]:
                seen["signature"] = current
                content.refresh()

        content()
        on_version_change(check)


# ---------------------------------------------------------------------------------------------------------------------
# The runs of a channel as a tree: the .ims at the top, every run a card below the run it continues

def run_tree(sample, channel, state, refresh):
    """One card per run; runs of the same parent side by side. A dashed card after each group adds a run there."""
    runs = sample.runs(channel=channel)
    paths = {run.rel_path for run in runs}
    children = {}
    for run in sorted(runs, key=lambda r: (STEP_INDEX.get(r.data["step"], 99), r.data.get("created", ""), r.id)):
        parent = run.parent_path
        children.setdefault(parent if parent in paths else None, []).append(run)

    # The branch to highlight: the runs from the top to the given run (e.g. the row clicked in the samples table)
    highlight = set()
    if state.get("highlight") in paths:
        highlight = {r.rel_path for r in chain(sample.run(state["highlight"]))}

    def made(new_runs):
        """After queueing / starting runs: highlight the new branch; a manual run opens its instructions."""
        Ctx.queue.changed()
        if new_runs:
            state["highlight"] = new_runs[0].rel_path
        refresh()
        if new_runs and new_runs[0].step.manual:
            manual_dialog(new_runs[0].reload(), refresh)

    def changed():
        Ctx.queue.changed()
        refresh()

    def branches_below(parent):
        items = children.get(parent.rel_path if parent else None, [])
        addable = next_steps(parent)        # nothing while the parent is not done
        # An optional step has only two choices: once it is both skipped and run here, there is nothing left to add
        if addable and addable[0].optional:
            mine = [r for r in items if r.data["step"] == addable[0].key and r.status not in (FAILED, CANCELLED)]
            if any(is_skipped(r) for r in mine) and any(not is_skipped(r) for r in mine):
                addable = []
        # A step that is coming soon: shown as the next step, but no second '+' beside runs made before
        if addable and addable[0].coming_soon and any(r.data["step"] == addable[0].key for r in items):
            addable = []
        # A step without settings that change its result (ilastik prediction, largest component): one run is enough
        if addable and addable[0].once:
            if any(r.data["step"] == addable[0].key and r.status not in (FAILED, CANCELLED) for r in items):
                addable = []
        if not items and not addable:
            return
        with ui.element("ul"):
            if len(items) == 1 and addable:
                # A single child stays right below its parent: the '+' for another branch sits beside it
                run = items[0]
                with ui.element("li"):
                    with ui.element("div").classes("node-wrap"):
                        run_node(run, run.rel_path in highlight, changed)
                        with ui.element("div").classes("side-add"):
                            add_node(sample, channel, parent, addable, True, made)
                    branches_below(run)
                return
            for run in items:
                with ui.element("li"):
                    run_node(run, run.rel_path in highlight, changed)
                    branches_below(run)
            if addable:
                with ui.element("li").classes("add-li"):
                    add_node(sample, channel, parent, addable, bool(items), made)

    with ui.element("div").classes("tree-scroll w-full"):
        with ui.element("div").classes("tree"):
            with ui.element("ul"):
                with ui.element("li"):
                    root_node(sample, channel)
                    branches_below(None)


def root_node(sample, channel):
    from .ims import short_channel_name
    info = sample.channel(channel) or {}
    with ui.element("div").classes("tree-node root"):
        with ui.row().classes("items-center gap-2 no-wrap"):
            ui.icon("science", size="18px").classes("text-blue-600")
            ui.label(info.get("structure") or f"Channel {channel}").classes("text-sm font-semibold")
        ui.label(short_channel_name(info.get("name")) or f"Channel {channel}") \
            .classes("font-mono text-xs text-slate-500 truncate").tooltip(info.get("name") or "")


def run_node(run, highlighted, changed):
    """One run: its step, name, settings and status. Click: details (log, files, delete, run again)."""
    from .widgets import describe_params
    step = run.step
    status = run.status
    skipped = is_skipped(run)
    classes = (f"tree-node s-{status}" + (" highlighted" if highlighted else "") + (" manual" if step.manual else "")
               + (" skipped" if skipped else ""))
    with ui.element("div").classes(classes) as card:
        with ui.row().classes("w-full items-center gap-2 no-wrap"):
            with ui.element("div").classes("node-num" + (" done" if status == DONE and not skipped else
                                                         " manual" if step.manual else "")):
                if skipped:
                    ui.icon("redo", size="14px")
                elif status == DONE:
                    ui.icon("check", size="14px")
                else:
                    ui.label(step.number)
            ui.label(step.title).classes("text-[13px] font-semibold leading-tight grow min-w-0 truncate") \
                .tooltip(f"{step.number} · {step.title}")
        with ui.row().classes("w-full items-center gap-2 no-wrap mt-1"):
            ui.label(run.id).classes("font-mono text-xs text-slate-600 truncate grow min-w-0").tooltip(run.id)
            if skipped:
                ui.label("skipped").classes("pill pill-skipped")
            elif status != DONE:
                status_pill(status)
        params = describe_params(run)
        if params:
            ui.label(params).classes("font-mono text-[11px] text-slate-400 truncate w-full").tooltip(params)
        if status in (RUNNING, STARTING, QUEUED):
            bar = ui.linear_progress(0, show_value=False, size="4px").classes("mt-1")

            def update():
                found = progress(run.log_path)
                bar.set_value(found[0] if found else 0)
            ui.timer(2.0, update)
        if status == OPEN:
            with ui.row().classes("items-center gap-1 mt-1 text-xs text-amber-700 no-wrap"):
                ui.icon("back_hand", size="14px")
                ui.label("Click to continue this step")
    card.on("click", lambda: manual_dialog(run, changed) if run.reload().status == OPEN
            else run_dialog(run, changed))


def add_node(sample, channel, parent, addable, has_children, made):
    """Start a run that continues `parent`: the next step, or (next to existing runs) another branch."""
    def start(step):
        new_run_dialog(sample, channel, step, parent, made)

    if not has_children:
        # Nothing continues the parent yet: show what comes next
        step = addable[0]
        if step.coming_soon:
            with ui.element("div").classes("tree-add next soon"):
                ui.label(f"Next: {step.number} · {step.title}").classes("label-sm")
                ui.label("Coming soon").classes("soon-badge")
            return
        with ui.element("div").classes("tree-add next").on("click", lambda: start(step)):
            ui.button(icon="add").props("round flat dense color=primary")
            ui.label(f"Next: {step.number} · {step.title}").classes("label-sm")
        return
    # Next to existing runs: a small '+' for another run with other settings (a new branch)
    with ui.element("div").classes("tree-add small"):
        with ui.button(icon="add").props("round flat dense color=primary") as button:
            button.tooltip("Another run: a new branch")
            if len(addable) == 1:
                button.on_click(lambda: start(addable[0]))
            else:
                with ui.menu():
                    for step in addable:
                        ui.menu_item(f"{step.number} · {step.title}", on_click=lambda s=step: start(s))


def manual_dialog(run, on_change):
    """An open manual run: instructions, tools, result fields, 'Mark as done'."""
    with ui.context.client.content, ui.dialog() as dialog, \
            ui.card().classes("dialog-card w-[1000px]").style("max-width: 95vw"):
        with ui.row().classes("w-full items-start gap-3 no-wrap"):
            with ui.column().classes("gap-1 grow"):
                ui.label(run.step.title).classes("text-lg font-semibold")
            ui.button(icon="close", on_click=dialog.close).props("flat round dense color=grey-7")

        def done():
            dialog.close()
            on_change()

        manual_run_card(run, done)
    dialog.on("hide", lambda: dialog.delete())
    dialog.open()


def new_run_dialog(sample, channel, step, parent, made):
    """The parameters of a new run that continues `parent` (a new branch if parent has other runs already)."""
    # Scrolls when the step has more fields than fit on the screen
    with ui.dialog() as dialog, ui.card().classes("dialog-card scroll-card w-[860px] no-wrap") \
            .style("max-width: 95vw; max-height: 90vh; overflow-y: auto"):
        with ui.row().classes("w-full items-start gap-3 no-wrap"):
            with ui.column().classes("gap-1 grow"):
                with ui.row().classes("items-baseline gap-3"):
                    ui.label(step.title).classes("text-lg font-semibold")
                    if step.script:
                        ui.label(step.script).classes("font-mono text-xs text-slate-400")
                if step.description and not step.optional:
                    ui.markdown(step.description).classes("text-sm text-slate-600")
            ui.button(icon="close", on_click=dialog.close).props("flat round dense color=grey-7")

        def created(new_runs):
            dialog.close()
            made(new_runs)

        if not step.optional:
            NewRunForm(sample, channel, step, parent, created)
        else:
            # An optional step (without parameters) is a decision: skip it, or run it. Nothing happens until then.
            def skip():
                created([skip_run(sample, step, channel, parent)])

            def run_it():
                chosen = {spec.name: (input_options(sample, spec, parent) or [None])[0] for spec in step.inputs}
                missing = [spec.label for spec in step.inputs if not spec.optional and not chosen[spec.name]]
                if missing:
                    ui.notify(f"No '{missing[0]}' on this branch", type="negative")
                    return
                existing = [r for r in sample.runs(step.key, channel) if r.status in (DONE,) + ACTIVE
                            and not is_skipped(r) and same_run(r, {}, chosen, parent)]
                if existing:
                    ui.notify("Already resampled (or queued) here", type="warning")
                    return
                created([create_run(sample, step, {}, chosen, channel, parent)])

            with ui.row().classes("w-full justify-end gap-2 mt-2"):
                ui.button("Skip", icon="redo", on_click=skip).props("outline color=grey-8")
                ui.button("Resample", icon="play_arrow", on_click=run_it)
    dialog.on("hide", lambda: dialog.delete())
    dialog.open()


async def delete_sample(sample):
    """Delete a sample with everything in its folder (its .ims copy and all runs), after asking."""
    runs = sample.runs()
    if any(run.status in ACTIVE + (STARTING,) for run in runs):
        ui.notify("A run of this sample is queued or running: cancel it first", type="negative")
        return
    text = (f"Delete the sample **{sample.name}** with all its files?\n\n"
            f"This removes its folder `{sample.path}`: the copy of the .ims and "
            f"{len(runs)} run{'s' if len(runs) != 1 else ''}. It cannot be undone "
            f"(the original .ims stays where it is).")
    if not await confirm(text):
        return
    try:
        await background.io_bound(sample.delete)
    except OSError as error:
        ui.notify(f"Could not delete everything (is a file open in another program?): {error}. The sample is gone "
                  f"from the GUI; what is left of {sample.path} can be removed by hand.", type="negative",
                  multi_line=True)
        ui.navigate.to("/")
        return
    Ctx.queue.changed()
    ui.notify(f"Deleted {sample.name}", type="positive")
    ui.navigate.to("/")


def edit_sample_dialog(sample, on_saved):
    project = Ctx.project
    # (inline max-width: Quasar limits dialog cards to 560px with a more specific CSS rule)
    with ui.dialog() as dialog, ui.card().classes("dialog-card w-[780px]").style("max-width: 95vw"):
        dialog_title("Edit sample")
        with ui.column().classes("w-full gap-3"):
            with ui.row().classes("w-full gap-3 no-wrap"):
                name = ui.input("Name", value=sample.name).classes("grow")
                tag = ui.select(project.tags, label="Tag",
                                value=sample.tag if sample.tag in project.tags else None).classes("w-48")
            selects = channel_rows(sample.all_channels, project.structures,
                                   {c["index"]: c.get("structure") if c.get("structure") else SKIP_CHANNEL
                                    for c in sample.all_channels
                                    if not c.get("structure") or c.get("structure") in project.structures})
            notes = ui.textarea("Notes", value=sample.data.get("notes", "")).classes("w-full").props("autogrow")

        def save():
            if any(not select.value for select in selects.values()):
                ui.notify("Choose a structure (or 'Skip for now') for every channel", type="negative")
                return
            structures = channel_structures(selects)
            if not any(structures.values()):
                ui.notify("All channels are skipped: choose a structure for at least one", type="negative")
                return
            # A channel with runs cannot be skipped: its runs would disappear from the GUI
            with_runs = [c for c in sample.channels if structures[c["index"]] is None
                         and sample.runs(channel=c["index"])]
            if with_runs:
                from .ims import short_channel_name
                ui.notify(f"{short_channel_name(with_runs[0].get('name')) or 'This channel'} has runs already: "
                          "it cannot be skipped",
                          type="negative")
                return
            channels = [dict(c, structure=structures[c["index"]]) for c in sample.all_channels]
            # The name first: its folder is renamed with it, which can fail (a file open elsewhere, a run active)
            try:
                saved = project.rename_sample(sample, name.value.strip() or sample.name)
            except ValueError as error:
                ui.notify(str(error), type="negative", multi_line=True)
                return
            saved.update(tag=tag.value, notes=notes.value, channels=channels)
            dialog.close()
            Ctx.queue.changed()
            on_saved(saved)
            ui.notify("Saved", type="positive")

        with ui.row().classes("w-full justify-end gap-2 mt-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat color=grey-8")
            ui.button("Save", on_click=save)
    dialog.on("hide", lambda: dialog.delete())
    dialog.open()


# ---------------------------------------------------------------------------------------------------------------------
# Queue

@ui.page("/queue")
def queue_page():
    header("Queue")
    ui.page_title(f"Queue · {Ctx.project.name}")
    queue = Ctx.queue
    with page(width="max-w-[1100px]"):
        heading, _ = page_heading("Queue", "Jobs run one at a time, in the order they were queued.")
        with heading:
            pause = ui.switch("Pause queue", value=queue.paused).props("color=primary")
            pause.tooltip("A running job continues; the next one waits")
            pause.on_value_change(lambda e: setattr(queue, "paused", e.value))

        @ui.refreshable
        def current():
            run = queue.current
            with ui.element("div").classes("surface w-full p-5"):
                ui.label("Now running").classes("label-sm")
                if run is None:
                    with ui.row().classes("items-center gap-2 mt-2 text-slate-500"):
                        ui.icon("check_circle", size="20px").classes("text-emerald-500")
                        ui.label("Nothing running")
                    return
                with ui.row().classes("w-full items-center gap-3 mt-2 no-wrap"):
                    with ui.column().classes("gap-0 grow"):
                        with ui.row().classes("items-center gap-2"):
                            ui.label(f"{run.step.number} · {run.step.title}").classes("section-title")
                            status_pill(run.status)
                        ui.label(f"{run_where(run)} · {run.id} · started "
                                 f"{short_time(run.data.get('started') or run.data.get('created'))}") \
                            .classes("section-hint font-mono")
                    ui.button("Details", on_click=lambda: run_dialog(run, refresh)).props("outline color=grey-8")

                    async def cancel():
                        if await confirm(f"Cancel the running job **{run.id}**? Its partial outputs stay in its "
                                         f"folder.", ok="Cancel job"):
                            queue.cancel(run)
                            refresh()
                    ui.button("Cancel", icon="stop", on_click=cancel).props("outline color=negative")
                with ui.element("div").classes("w-full mt-4"):
                    log_view(run, "h-80")

        @ui.refreshable
        def waiting():
            runs = queue.queued()
            with ui.element("div").classes("surface w-full p-5"):
                with ui.row().classes("items-center gap-2"):
                    ui.label("Up next").classes("label-sm")
                    ui.label(str(len(runs))).classes("pill")
                if not runs:
                    ui.label("The queue is empty").classes("section-hint mt-2")
                    return
                with ui.column().classes("w-full gap-0 mt-2"):
                    for i, run in enumerate(runs):
                        with ui.row().classes("w-full items-center gap-4 py-2.5 no-wrap "
                                              + ("border-t border-slate-100" if i else "")):
                            ui.label(str(i + 1)).classes("font-mono text-xs text-slate-400 w-5 text-right")
                            with ui.column().classes("gap-0 grow"):
                                ui.label(f"{run.step.number} · {run.step.title}").classes("text-sm font-medium")
                                ui.label(f"{run_where(run)} · {run.id}").classes("text-xs text-slate-500 font-mono")
                            ui.button(icon="info_outline", on_click=lambda r=run: run_dialog(r, refresh)) \
                                .props("flat round dense color=grey-7").tooltip("Details")
                            ui.button(icon="close", on_click=lambda r=run: (queue.cancel(r), refresh())) \
                                .props("flat round dense color=grey-7").tooltip("Remove from the queue")

        recent_state = {"open": False}      # collapsed by default; stays as the user left it across refreshes

        @ui.refreshable
        def recent():
            runs = [r for r in Ctx.project.all_runs() if r.status in (DONE, FAILED, CANCELLED)
                    and not r.step.manual and r.data.get("finished")]
            runs.sort(key=lambda r: r.data["finished"], reverse=True)
            expansion = ui.expansion(value=recent_state["open"]) \
                .classes("step-card w-full").props("expand-icon-class=text-slate-400")
            expansion.on_value_change(lambda e: recent_state.update(open=e.value))
            with expansion.add_slot("header"):
                with ui.row().classes("w-full items-center gap-2"):
                    ui.label("Recently finished").classes("label-sm")
                    ui.label(str(min(len(runs), 30))).classes("pill")
            with expansion:
                if not runs:
                    ui.label("Nothing yet").classes("section-hint px-2 pb-3")
                    return
                columns = [dict(name=n, label=l, field=n, align="left") for n, l in
                           [("status", "Status"), ("step", "Step"), ("sample", "Sample"), ("id", "Run"),
                            ("finished", "Finished"), ("duration", "Duration")]]
                rows = [dict(key=r.key, sample=run_where(r), sample_id=r.sample.id, rel=r.rel_path,
                             step=f"{r.step.number} · {r.step.title}", id=r.id, status=r.status,
                             status_text=STATUS_TEXT.get(r.status, r.status), finished=short_time(r.data["finished"]),
                             duration=format_duration(r.duration())) for r in runs[:30]]
                table = ui.table(columns=columns, rows=rows, row_key="key").props("flat") \
                    .classes("modern-table clickable w-full")
                table.add_slot("body-cell-status", """
                    <q-td :props="props"><span :class="'pill pill-' + props.value">{{ props.row.status_text }}</span></q-td>""")
                table.add_slot("body-cell-id", """
                    <q-td :props="props"><span class="font-mono text-xs text-slate-500">{{ props.value }}</span></q-td>""")
                table.on("rowClick", lambda e: run_dialog(
                    Ctx.project.sample(e.args[1]["sample_id"]).run(e.args[1]["rel"]), refresh))

        def refresh():
            current.refresh()
            waiting.refresh()
            recent.refresh()

        current()
        waiting()
        recent()
        on_version_change(refresh)


# ---------------------------------------------------------------------------------------------------------------------
# Settings

def settings_section(title, hint=None):
    box = ui.element("div").classes("surface w-full p-6")
    with box:
        ui.label(title).classes("section-title")
        if hint:
            ui.label(hint).classes("section-hint mt-1")
    return box


def name_list(get, set_all, rename, usage_of, noun, used_by):
    """An editable list of names (tags, structures): rename, delete when unused, add."""
    @ui.refreshable
    def names():
        usage = usage_of()
        with ui.column().classes("w-full gap-0 mt-4"):
            for i, value in enumerate(get()):
                with ui.row().classes("w-full items-center gap-3 py-2 no-wrap "
                                      + ("border-t border-slate-100" if i else "")):
                    field = ui.input(value=value).classes("grow")
                    n = usage.get(value, 0)

                    def do_rename(old=value, f=field):
                        new = f.value.strip()
                        if not new or new == old:
                            return
                        if new in get():
                            ui.notify(f"'{new}' exists already", type="negative")
                            return
                        rename(old, new)
                        names.refresh()
                        ui.notify(f"Renamed {old} → {new}", type="positive")

                    def remove(v=value):
                        set_all([x for x in get() if x != v])
                        names.refresh()

                    ui.button("Rename", on_click=do_rename).props("flat color=grey-8")
                    delete = ui.button(icon="delete_outline", on_click=remove).props("flat round color=grey-7")
                    if n:
                        delete.disable()
                        delete.tooltip("In use")
        with ui.row().classes("w-full items-center gap-2 mt-3 no-wrap"):
            new = ui.input(placeholder=f"New {noun}").classes("grow")

            def add():
                value = new.value.strip()
                if value and value not in get():
                    set_all(get() + [value])
                    names.refresh()
            new.on("keydown.enter", add)
            ui.button(f"Add {noun}", icon="add", on_click=add).props("outline").classes("h-10")

    names()


@ui.page("/settings")
def settings_page():
    header("Settings")
    ui.page_title(f"Settings · {Ctx.project.name}")
    project = Ctx.project
    with page(width="max-w-[760px]"):
        page_heading("Settings")

        with settings_section("Project", "Shown in the top bar and the browser tab."):
            with ui.row().classes("w-full items-start gap-2 mt-4 no-wrap"):
                name = ui.input("Name", value=project.name).classes("grow")

                def save_name():
                    project.data["name"] = name.value.strip() or project.root.name
                    project.save()
                    ui.notify("Saved", type="positive")
                ui.button("Save", on_click=save_name).classes("h-10")

        with settings_section("Tags", "Each sample gets one tag. Renaming a tag renames it on all its samples; "
                                      "a tag can be deleted once no sample uses it."):
            def tag_usage():
                usage = {}
                for sample in project.samples():
                    usage[sample.tag] = usage.get(sample.tag, 0) + 1
                return usage
            name_list(lambda: project.tags, project.set_tags, project.rename_tag, tag_usage, "tag", "sample")

        with settings_section("Structures", "What a channel shows: each channel of a sample gets one. Renaming a "
                                            "structure renames it on all channels; it can be deleted once no "
                                            "channel uses it."):
            def structure_usage():
                usage = {}
                for sample in project.samples():
                    for channel in sample.channels:
                        usage[channel.get("structure")] = usage.get(channel.get("structure"), 0) + 1
                return usage
            name_list(lambda: project.structures, project.set_structures, project.rename_structure,
                      structure_usage, "structure", "channel")

        with settings_section("About"):
            import sys
            from .store import SCRIPTS
            with ui.column().classes("w-full gap-0 mt-3"):
                for i, (label, value) in enumerate([("Project folder", project.root), ("Pipeline scripts", SCRIPTS),
                                                    ("Pipeline commit", git_commit()), ("Python", sys.executable)]):
                    with ui.row().classes("w-full gap-4 py-2 no-wrap " + ("border-t border-slate-100" if i else "")):
                        ui.label(label).classes("text-sm text-slate-500 w-36 shrink-0")
                        ui.label(str(value)).classes("font-mono text-xs text-slate-700 break-all")


def start(project, host="127.0.0.1", port=8080, show=True):
    Ctx.project = project
    Ctx.queue = JobQueue(project)
    theme.apply()
    app.add_static_files("/files", project.root)
    app.timer(1.0, Ctx.queue.tick)
    ui.run(host=host, port=port, show=show, reload=False, title=project.name, favicon="🔬",
           show_welcome_message=True)
