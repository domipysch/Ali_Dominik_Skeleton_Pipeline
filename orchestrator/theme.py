"""Look and feel: fonts, colours, component defaults, and the CSS classes the pages use."""
from nicegui import ui

FONTS = """
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
"""

CSS = r"""
:root {
  --bg: #f6f7f9;
  --surface: #ffffff;
  --border: #e5e7eb;
  --border-soft: #eef0f3;
  --text: #0f172a;
  --muted: #64748b;
  --faint: #94a3b8;
  --accent: #2563eb;
  --accent-soft: #eff6ff;
}
body { background: var(--bg); color: var(--text);
       font-family: Inter, ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
       -webkit-font-smoothing: antialiased; font-feature-settings: "cv11", "ss01"; }
.font-mono, code, pre { font-family: "JetBrains Mono", ui-monospace, Consolas, monospace !important; }
.nicegui-content { padding: 0; }
.nicegui-markdown code { overflow-wrap: anywhere; background: #f1f5f9; padding: 1px 5px; border-radius: 4px;
                         font-size: 0.85em; }
.nicegui-markdown ol { padding-left: 1.2rem; }
.nicegui-markdown li { margin: 4px 0; }

/* Top bar */
.topbar { background: rgba(255,255,255,0.85) !important; backdrop-filter: saturate(180%) blur(8px);
          border-bottom: 1px solid var(--border); color: var(--text) !important; }
.brand-mark { width: 34px; height: 34px; border-radius: 10px; display: grid; place-items: center; color: white;
              background: linear-gradient(135deg, #3b82f6 0%, #1d4ed8 100%);
              box-shadow: 0 1px 2px rgba(29,78,216,.35), inset 0 1px 0 rgba(255,255,255,.2); }
.nav-link { padding: 6px 12px !important; border-radius: 8px; color: var(--muted) !important; font-weight: 500;
            font-size: 14px; min-height: 0 !important; }
.nav-link:hover { background: #f1f5f9; color: var(--text) !important; }
.nav-link.active { background: var(--accent-soft); color: var(--accent) !important; }
.job-chip { border: 1px solid var(--border); border-radius: 9999px; padding: 4px 12px 4px 10px;
            font-size: 13px; background: var(--surface); }
.job-chip.busy { border-color: #bfdbfe; background: var(--accent-soft); color: #1e40af; cursor: pointer; }

/* Surfaces */
.surface { background: var(--surface); border: 1px solid var(--border); border-radius: 14px;
           box-shadow: 0 1px 2px rgba(15,23,42,.04); }
.q-card.surface { box-shadow: 0 1px 2px rgba(15,23,42,.04); }
.page-title { font-size: 24px; font-weight: 650; letter-spacing: -0.02em; }
.page-subtitle { color: var(--muted); font-size: 14px; }
.section-title { font-size: 15px; font-weight: 600; letter-spacing: -0.01em; }
.section-hint { color: var(--muted); font-size: 13px; }
.label-sm { color: var(--faint); font-size: 11px; font-weight: 600; text-transform: uppercase;
            letter-spacing: .06em; }
.dialog-card { border-radius: 18px !important; padding: 28px 32px 24px 32px !important; gap: 16px; }
.scroll-card > * { flex-shrink: 0; }     /* a dialog with a max height scrolls instead of squeezing its content */

/* Status pills: a soft background and a dot in the status colour */
.pill { display: inline-flex; align-items: center; gap: 6px; padding: 1px 9px 1px 8px; border-radius: 9999px;
        font-size: 12px; font-weight: 500; line-height: 20px; white-space: nowrap;
        background: #f1f5f9; color: #475569; }
.pill::before { content: ""; width: 6px; height: 6px; border-radius: 50%; background: currentColor; flex: none; }
.pill-done { background: #ecfdf5; color: #047857; }
.pill-running, .pill-starting { background: #eff6ff; color: #1d4ed8; }
.pill-running::before, .pill-starting::before { animation: pulse 1.2s ease-in-out infinite; }
.pill-queued { background: #f1f5f9; color: #475569; }
.pill-failed { background: #fff1f2; color: #be123c; }
.pill-cancelled { background: #fff7ed; color: #c2410c; }
.pill-open { background: #fffbeb; color: #b45309; }
.pill-skipped { background: #f1f5f9; color: #64748b; }
.pill-skipped::before { display: none; }
.pill-more { background: transparent; color: var(--faint); padding-left: 2px; }
.pill-more::before { display: none; }
.pill.clickable { cursor: pointer; }
.pill.clickable:hover { filter: brightness(0.96); }
@keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: .25; } }

.tag { display: inline-block; padding: 1px 9px; border-radius: 6px; font-size: 12px; font-weight: 500;
       background: #f1f5f9; color: #334155; border: 1px solid #e2e8f0; }

/* Tables */
.modern-table { background: transparent !important; }
.modern-table thead tr th { background: #f8fafc; color: var(--muted); font-size: 11.5px; font-weight: 600;
                            text-transform: uppercase; letter-spacing: .05em; border-bottom: 1px solid var(--border); }
.modern-table tbody tr td { border-bottom: 1px solid var(--border-soft); font-size: 13.5px; }
.modern-table tbody tr:last-child td { border-bottom: none; }
.modern-table tbody tr:hover td { background: #f8fafc; }
.modern-table .q-table__bottom { border-top: 1px solid var(--border-soft); color: var(--muted); }
.modern-table.clickable tbody tr { cursor: pointer; }

/* Branch tables: one row per branch, a cell shared by several branches spans their rows */
.branch-table tbody tr:hover td { background: transparent; }
.branch-table td.branch-cell { padding: 8px 14px; vertical-align: top; cursor: pointer; font-size: 13.5px;
                               border-bottom: 1px solid var(--border-soft); border-right: 1px solid #f4f5f7; }
.branch-table td.branch-cell:hover { background: #f8fafc; }
.branch-table td.step-cell { min-width: 110px; }
.branch-table tr.selected-branch td.branch-cell { background: var(--accent-soft); }
.branch-table thead tr th { white-space: nowrap; }
.structure { font-weight: 500; color: #334155; }
.done-cell { display: inline-flex; align-items: center; gap: 5px; color: #334155; font-size: 13px;
             font-family: "JetBrains Mono", monospace; white-space: nowrap; }
.branch-mark { margin-left: 8px; font-size: 11px; font-weight: 600; color: var(--accent); background: var(--accent-soft);
               padding: 1px 7px; border-radius: 9999px; }

/* Run tree on the sample page: nested lists, the lines drawn with borders */
.tree-scroll { overflow-x: auto; padding: 8px 0 24px 0; }
.tree { display: inline-block; min-width: 100%; text-align: center; }
.tree ul { position: relative; display: flex; justify-content: center; margin: 0; padding: 22px 0 0 0;
           list-style: none; }
.tree > ul { padding-top: 0; }
.tree li { position: relative; display: flex; flex-direction: column; align-items: center; padding: 22px 10px 0 10px; }
.tree > ul > li { padding-top: 0; }
/* the horizontal line over the children, and each child's line down */
.tree li::before, .tree li::after { content: ""; position: absolute; top: 0; right: 50%; width: 50%; height: 22px;
                                    border-top: 1.5px solid #cbd5e1; }
.tree li::after { right: auto; left: 50%; border-left: 1.5px solid #cbd5e1; }
.tree li:only-child::before, .tree li:only-child::after { display: none; }
.tree li:only-child { padding-top: 0; }
.tree li:first-child::before, .tree li:last-child::after { border: 0 none; }
.tree li:last-child::before { border-right: 1.5px solid #cbd5e1; border-top-right-radius: 8px; }
.tree li:first-child::after { border-top-left-radius: 8px; }
/* the line down from a parent to its children */
.tree ul ul::before { content: ""; position: absolute; top: 0; left: 50%; height: 22px;
                      border-left: 1.5px solid #cbd5e1; }
.tree > ul > li > ul::before { display: block; }
/* a new run is not a run yet: its line is dashed */
.tree li.add-li::before, .tree li.add-li::after { border-top-style: dashed; border-left-style: dashed; }

.tree-node { width: 230px; text-align: left; background: var(--surface); border: 1px solid var(--border);
             border-radius: 12px; padding: 10px 12px; cursor: pointer; box-shadow: 0 1px 2px rgba(15,23,42,.05);
             transition: border-color .15s, box-shadow .15s; display: flex; flex-direction: column; gap: 2px; }
.tree-node:hover { border-color: #bfdbfe; box-shadow: 0 2px 8px rgba(37,99,235,.10); }
.tree-node.root { cursor: default; background: #f8fafc; }
.tree-node.root:hover { border-color: var(--border); box-shadow: 0 1px 2px rgba(15,23,42,.05); }
.tree-node.highlighted { border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-soft); }
.tree-node.s-failed, .tree-node.s-cancelled { border-style: dashed; opacity: .75; }
.tree-node.skipped { background: #f8fafc; border-style: dashed; }
.tree-node.s-open { border-color: #fcd34d; box-shadow: 0 0 0 3px #fef3c7; }
.node-num { width: 24px; height: 24px; border-radius: 7px; display: grid; place-items: center; flex: none;
            font-size: 11px; font-weight: 600; background: #f1f5f9; color: #334155;
            font-family: "JetBrains Mono", monospace; }
.node-num.done { background: #ecfdf5; color: #047857; }
.node-num.manual { background: #fffbeb; color: #b45309; }
.tree-add { width: 230px; border: 1.5px dashed #cbd5e1; border-radius: 12px; padding: 10px 12px;
            display: flex; flex-direction: column; gap: 6px; text-align: left; background: rgba(255,255,255,.6); }
.tree-add.small { width: auto; padding: 4px; border-radius: 9999px; }
/* the '+' beside a single child (so the child stays right below its parent) */
.node-wrap { position: relative; padding: 0 56px; }      /* the same space left and right: the card stays centred */
.side-add { position: absolute; left: calc(100% - 56px); top: 50%; transform: translateY(-50%); margin-left: 14px; }
.side-add::before { content: ""; position: absolute; right: 100%; top: 50%; width: 14px;
                    border-top: 1.5px dashed #cbd5e1; }
.tree-add.next { width: auto; flex-direction: row; align-items: center; gap: 4px; padding: 4px 14px 4px 4px;
                 border-radius: 9999px; cursor: pointer; white-space: nowrap; }
.tree-add.next:hover { border-color: #93c5fd; }
/* a step that cannot be started yet: no '+', not clickable */
.tree-add.next.soon { cursor: default; padding: 6px 6px 6px 14px; gap: 8px; }
.tree-add.next.soon:hover { border-color: #cbd5e1; }
.soon-badge { font-size: 11px; font-weight: 600; color: #64748b; background: #f1f5f9; border-radius: 9999px;
              padding: 2px 8px; }

/* Step sections */
.step-card { background: var(--surface); border: 1px solid var(--border); border-radius: 14px; overflow: hidden;
             box-shadow: 0 1px 2px rgba(15,23,42,.04); }
.step-card > .q-expansion-item__container > .q-item { padding: 14px 18px; }
.step-card .q-expansion-item__content { border-top: 1px solid var(--border-soft); }
.step-num { width: 34px; height: 34px; border-radius: 10px; display: grid; place-items: center; font-size: 12px;
            font-weight: 600; background: #f1f5f9; color: #334155; font-family: "JetBrains Mono", monospace; }
.step-num.manual { background: #fffbeb; color: #b45309; }
.step-num.done { background: #ecfdf5; color: #047857; }
.kind-tag { font-size: 11px; color: var(--faint); font-weight: 500; }

/* The new-run form sits directly in the step card, set off by a divider (not a card of its own) */
.form-panel { border-top: 1px solid var(--border-soft); padding: 16px 0 0 0; }
.form-panel:first-child { border-top: none; padding-top: 4px; }
.form-panel .q-field__bottom { padding-left: 2px; }
.manual-card { background: var(--surface); }

.log-box { background: #0b1020; border-radius: 12px; border: 1px solid #1e293b; }
.code-box { background: #f8fafc; border: 1px solid var(--border); border-radius: 10px; padding: 10px 12px; }
.empty-state { padding: 48px 24px; text-align: center; }
.empty-icon { width: 52px; height: 52px; border-radius: 14px; display: grid; place-items: center; margin: 0 auto;
              background: var(--accent-soft); color: var(--accent); }

/* Quasar details */
.q-field--outlined .q-field__control { border-radius: 10px; }
/* Fields in a grey box are white: the rounded control is filled, not the square field around it (white corners) */
.code-box .q-field--outlined .q-field__control { background: var(--surface); }
.q-field--outlined .q-field__control:before { border-color: var(--border); }
.q-btn { border-radius: 10px; font-weight: 500; letter-spacing: 0; }
/* Secondary (outlined grey) buttons: white with a hairline border */
.q-btn--outline.text-grey-8 { background: #fff !important; color: #334155 !important;
                              box-shadow: 0 1px 2px rgba(15,23,42,.05); }
.q-btn--outline.text-grey-8:before { border: 1px solid #d9dde3 !important; }
.q-btn--outline.text-grey-8 .q-icon { color: #64748b; font-size: 1.15em; }
.q-tab { text-transform: none; font-weight: 500; }
.q-notification { border-radius: 12px; }
.q-linear-progress { border-radius: 9999px; }
"""


def apply():
    """Call once before ui.run: shared by all pages."""
    ui.add_head_html(FONTS, shared=True)
    ui.add_css(CSS, shared=True)
    ui.button.default_props("unelevated no-caps")
    ui.input.default_props("outlined dense")
    ui.select.default_props("outlined dense")
    ui.textarea.default_props("outlined")


def page_colors():
    """Per page (Quasar's colour variables belong to the page)."""
    ui.colors(primary="#2563eb", secondary="#64748b", accent="#2563eb", positive="#059669", negative="#e11d48",
              warning="#d97706", info="#2563eb", dark="#0f172a")
