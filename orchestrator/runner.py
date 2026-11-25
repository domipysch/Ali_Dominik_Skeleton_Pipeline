"""Runs one queued run: python -m orchestrator.runner <run folder>

Started by the GUI as its own process, so a job keeps running (and records its end in run.json) even if the GUI is
closed. Everything the script prints goes to log.txt in the run folder.
"""
import os
import subprocess
import sys
from pathlib import Path

from .store import DONE, FAILED, LOG_FILE, REPO, RUN_FILE, RUNNING, now, read_json, update_json


def main(run_dir):
    run_dir = Path(run_dir)
    run_file = run_dir / RUN_FILE
    data = read_json(run_file)
    command = data["command"]
    update_json(run_file, status=RUNNING, started=now(), finished=None, returncode=None, error=None,
                pid=os.getpid())

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"          # log lines appear as they are printed
    env["PYTHONIOENCODING"] = "utf-8"      # the scripts print µm
    with open(run_dir / LOG_FILE, "ab") as log:
        log.write(f"[{now()}] {subprocess.list2cmdline(command)}\n\n".encode())
        log.flush()
        try:
            # stdin closed: a script asking "Overwrite? [y/N]" fails instead of waiting forever
            proc = subprocess.Popen(command, cwd=REPO, stdin=subprocess.DEVNULL, stdout=log,
                                    stderr=subprocess.STDOUT, env=env)
        except OSError as error:
            update_json(run_file, status=FAILED, finished=now(), error=f"Could not start: {error}")
            return 1
        update_json(run_file, child_pid=proc.pid)
        returncode = proc.wait()

    missing = [pattern for pattern in data.get("expected_outputs", []) if not list(run_dir.glob(pattern))]
    if returncode != 0:
        status, error = FAILED, f"Exit code {returncode}, see the log"
    elif missing:
        status, error = FAILED, f"Finished, but missing output: {', '.join(missing)}"
    else:
        status, error = DONE, None
    update_json(run_file, status=status, finished=now(), returncode=returncode, error=error)
    with open(run_dir / LOG_FILE, "ab") as log:
        log.write(f"\n[{now()}] {status}{': ' + error if error else ''}\n".encode())
    return 0 if status == DONE else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
