import argparse
from pathlib import Path

from .store import PROJECT_FILE, Project


def main():
    parser = argparse.ArgumentParser(prog="python -m orchestrator",
                                     description="GUI for running the skeleton pipeline on many samples")
    parser.add_argument("root", type=Path, help="project folder (holds pipeline-project.json and samples/)")
    parser.add_argument("--init", action="store_true",
                        help="make a new project in ROOT even if the folder already has other files")
    parser.add_argument("--host", default="127.0.0.1",
                        help="127.0.0.1 (default): only this PC; 0.0.0.0: reachable from the network")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--no-browser", action="store_true", help="do not open a browser tab")
    args = parser.parse_args()

    root = args.root.resolve()
    if Project.exists(root):
        project = Project(root)
    elif not root.exists() or not any(root.iterdir()) or args.init:
        project = Project.create(root)
        print(f"Made a new project in {root}")
    else:
        raise SystemExit(f"{root} has no {PROJECT_FILE} and is not empty. "
                         f"Pass --init to make a project there anyway.")

    from .app import start
    start(project, host=args.host, port=args.port, show=not args.no_browser)


if __name__ == "__main__":
    main()
