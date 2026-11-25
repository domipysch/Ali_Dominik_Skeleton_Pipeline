"""The job queue: script runs are executed one at a time, in the order they were queued.

The queue itself is just the runs whose run.json says 'queued' (oldest first), so it survives a restart of the GUI.
A started job runs in its own process (orchestrator.runner), which writes the end status into run.json itself.
"""
import ctypes
import os
import re
import subprocess
import sys
import time

from .store import ACTIVE, CANCELLED, FAILED, LOG_FILE, QUEUED, REPO, RUNNING, now

STARTING = "starting"


def pid_alive(pid):
    if not pid:
        return False
    if sys.platform == "win32":
        PROCESS_QUERY_LIMITED_INFORMATION, STILL_ACTIVE = 0x1000, 259
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return bool(ok) and code.value == STILL_ACTIVE
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def kill_tree(pid):
    """Stop a process with all its children (the scripts start worker processes)."""
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    else:
        import signal
        try:
            os.killpg(os.getpgid(int(pid)), signal.SIGTERM)
        except OSError:
            pass


class JobQueue:
    def __init__(self, project):
        self.project = project
        self.current = None         # the Run being executed
        self.paused = False
        self.version = 0            # counts status changes: pages refresh when it changes
        self._next_scan = 0.0       # reading every run.json is not free: scan for queued runs only when needed
        self._last_status = None
        self._queued_cache = None
        self._recover()

    def changed(self):
        """Call after creating / deleting / changing runs from the GUI."""
        self.version += 1
        self._next_scan = 0.0

    def _recover(self):
        """After a GUI restart: pick up a job that is still running, mark jobs that died with the GUI as failed."""
        for run in self.project.all_runs():
            if run.status in (RUNNING, STARTING):
                if pid_alive(run.data.get("pid")):
                    self.current = run
                else:
                    run.update(status=FAILED, finished=run.data.get("finished") or now(),
                               error="Interrupted (the process ended without recording a result)")

    def queued(self):
        """Queued runs, oldest first. Re-read from disk only after a change (reading every run.json is not free)."""
        if self._queued_cache is None or self._queued_cache[0] != self.version:
            runs = [run for run in self.project.all_runs() if run.status == QUEUED]
            runs.sort(key=lambda run: (run.data.get("queued_at") or run.data.get("created") or "", run.key))
            self._queued_cache = (self.version, runs)
        return list(self._queued_cache[1])

    def tick(self):
        """Called every second: notice the end of the current job, start the next one."""
        if self.current is not None:
            try:
                self.current.reload()
            except FileNotFoundError:           # deleted
                self.current = None
                return
            status = self.current.status
            pid = self.current.data.get("pid")
            if status != self._last_status:
                self._last_status = status
                self.version += 1
            if status in (RUNNING, STARTING) and not pid_alive(pid):
                # The runner records the end itself; this only catches a runner that was killed
                self.current.reload()
                if self.current.status in (RUNNING, STARTING):
                    self.current.update(status=FAILED, finished=now(), error="The job process ended unexpectedly")
                self.current = None
                self.changed()
            elif status not in (RUNNING, STARTING):
                self.current = None
                self.changed()
        if self.current is None and not self.paused and time.monotonic() >= self._next_scan:
            self._next_scan = time.monotonic() + 30
            self._queued_cache = None       # also notice runs queued from outside (e.g. a second GUI)
            queued = self.queued()
            if queued:
                self.start(queued[0])

    def start(self, run):
        flags = 0
        if sys.platform == "win32":
            # Own process group: Ctrl+C in the GUI's console does not stop the job; no console window pops up
            flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        proc = subprocess.Popen([sys.executable, "-m", "orchestrator.runner", str(run.path)], cwd=REPO,
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                creationflags=flags, start_new_session=sys.platform != "win32")
        run.update(status=STARTING, pid=proc.pid)
        self.current = run
        self._last_status = STARTING
        self.version += 1

    def cancel(self, run):
        run.reload()
        if run.status in (RUNNING, STARTING):
            kill_tree(run.data.get("pid"))
            if run.data.get("child_pid"):
                kill_tree(run.data["child_pid"])
            with open(run.path / LOG_FILE, "ab") as log:
                log.write(f"\n[{now()}] cancelled\n".encode())
        if run.status in ACTIVE + (STARTING,):
            run.update(status=CANCELLED, finished=now(), error="Cancelled")
        if self.current is not None and self.current.path == run.path:
            self.current = None
        self.changed()

    def requeue(self, run):
        """Run a failed / cancelled run again, in the same folder (the log is appended to)."""
        run.update(status=QUEUED, queued_at=now(), error=None, returncode=None, started=None, finished=None,
                   pid=None, child_pid=None)
        self.changed()


TQDM = re.compile(r"(?:(?P<desc>[^:|\r\n]*?):\s*)?(?P<pct>\d+)%\|.*?\|\s*(?P<n>[\d.]+[kMG]?)/(?P<total>[\d.]+[kMG]?)"
                  r"(?:\s*\[(?P<times>[^\]]*)\])?")


def read_log(path, max_bytes=200_000):
    """The end of a log as lines. Progress bars redraw with '\\r': only the latest state of each line is kept."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            text = f.read().decode("utf-8", errors="replace")
    except FileNotFoundError:
        return []
    lines = [line.rstrip("\r").split("\r")[-1] for line in text.split("\n")]
    if size > max_bytes:
        lines = ["…"] + lines[1:]
    while lines and not lines[-1]:
        lines.pop()
    return lines


def progress(path):
    """(fraction, text) of the latest progress bar in a log, or None."""
    for line in reversed(read_log(path, 20_000)[-30:]):
        match = TQDM.search(line)
        if match:
            desc = (match.group("desc") or "").strip()
            times = match.group("times") or ""
            text = f"{desc + ': ' if desc else ''}{match.group('n')}/{match.group('total')}"
            if times:
                text += f"  [{times}]"
            return int(match.group("pct")) / 100, text
    return None
