"""Entry point: `python -m stemapp` from the `server/` directory.

    python -m stemapp            start the server
    python -m stemapp --check    verify the external tools and exit
"""

from __future__ import annotations

import argparse
import signal
import sys
import threading
from pathlib import Path

from . import pipeline
from .config import Config, ConfigError
from .jobs import JobStore
from .server import StemHandler, StemServer
from .worker import Worker

_SWEEP_INTERVAL_SECONDS = 600


def _sweeper(store: JobStore, stop: threading.Event) -> None:
    while not stop.wait(_SWEEP_INTERVAL_SECONDS):
        try:
            removed = store.sweep()
        except OSError as exc:
            print(f"sweep failed: {exc}", file=sys.stderr, flush=True)
            continue
        if removed:
            print(f"swept {removed} expired job(s)", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="stemapp", description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify ffmpeg, ffprobe and audio-separator, then exit",
    )
    args = parser.parse_args(argv)

    repo_root = Path(__file__).resolve().parents[2]
    try:
        cfg = Config.from_env(repo_root)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    problems = pipeline.check_tools(cfg)
    if args.check:
        print(f"data dir:   {cfg.data_dir}")
        print(f"web dir:    {cfg.web_dir}")
        print(f"model:      {cfg.model_filename}")
        print(f"model dir:  {cfg.model_dir}")
        print(f"access key: {'set' if cfg.access_key else 'DISABLED (open)'}")
        if problems:
            for problem in problems:
                print(f"MISSING {problem}", file=sys.stderr)
            return 1
        print("all external tools present")
        return 0
    if problems:
        for problem in problems:
            print(f"cannot start: {problem}", file=sys.stderr)
        return 1

    cfg.ensure_directories()
    store = JobStore(cfg.jobs_dir, cfg.job_ttl_seconds)
    store.load_from_disk()
    store.sweep()

    worker = Worker(cfg, store)
    worker.start()

    stop = threading.Event()
    sweeper = threading.Thread(
        target=_sweeper, args=(store, stop), name="stem-sweeper", daemon=True
    )
    sweeper.start()

    httpd = StemServer((cfg.host, cfg.port), StemHandler, cfg, store, worker)

    def shutdown(signum, _frame):
        print(f"received signal {signum}, shutting down", flush=True)
        stop.set()
        worker.stop()
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    print(
        f"stem server listening on http://{cfg.host}:{cfg.port} "
        f"(model {cfg.model_filename}, {cfg.worker_count} worker"
        f"{'s' if cfg.worker_count != 1 else ''})",
        flush=True,
    )
    try:
        httpd.serve_forever(poll_interval=0.5)
    finally:
        httpd.server_close()
        stop.set()
        worker.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
