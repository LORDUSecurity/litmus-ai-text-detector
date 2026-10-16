"""PyInstaller entry point for the local single-file app.

Kept at the repo root so `pyinstaller run_local.py` resolves the `backend` package
by name. All real logic lives in backend.serve_local; this only bootstraps it and
calls multiprocessing.freeze_support() so a onefile build does not spawn a second
copy of itself if anything ever forks.
"""
import multiprocessing


def _run() -> None:
    from backend.serve_local import main
    main()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    _run()
