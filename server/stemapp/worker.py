"""The background worker that turns an upload into four stems.

Separation is CPU-bound and saturates every core it is given, so the default
worker count is one: running two jobs concurrently on a four-core box makes
both slower without finishing either sooner.
"""

from __future__ import annotations

import queue
import threading
import time
from pathlib import Path

from . import pipeline
from .config import STEM_NAMES, Config
from .jobs import (
    STATE_DONE,
    STATE_ENCODING,
    STATE_ERROR,
    STATE_PREPARING,
    STATE_SEPARATING,
    JobStore,
)


class Worker:
    def __init__(self, cfg: Config, store: JobStore) -> None:
        self._cfg = cfg
        self._store = store
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._threads: list[threading.Thread] = []
        self._stopping = threading.Event()

    def start(self) -> None:
        for index in range(self._cfg.worker_count):
            thread = threading.Thread(
                target=self._loop, name=f"stem-worker-{index}", daemon=True
            )
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        self._stopping.set()
        for _ in self._threads:
            self._queue.put(None)

    def submit(self, job_id: str) -> None:
        self._queue.put(job_id)

    @property
    def pending(self) -> int:
        return self._queue.qsize()

    def _loop(self) -> None:
        while not self._stopping.is_set():
            job_id = self._queue.get()
            if job_id is None:
                return
            try:
                self._process(job_id)
            except Exception as exc:  # noqa: BLE001 - worker must not die
                self._fail(job_id, f"unexpected failure: {exc}")
            finally:
                self._queue.task_done()

    def _process(self, job_id: str) -> None:
        job = self._store.get(job_id)
        if job is None:
            return

        job_dir = self._store.job_dir(job_id)
        upload = _find_upload(job_dir)
        if upload is None:
            self._fail(job_id, "the uploaded file is missing from disk")
            return

        self._store.update(
            job_id, state=STATE_PREPARING, started_at=time.time(), progress=None
        )

        try:
            info = pipeline.probe_audio(upload, self._cfg)
        except pipeline.StageError as exc:
            self._fail(job_id, str(exc))
            return

        if info["duration_seconds"] > self._cfg.max_duration_seconds:
            self._fail(
                job_id,
                "this track is "
                f"{info['duration_seconds'] / 60:.1f} minutes; the limit is "
                f"{self._cfg.max_duration_seconds / 60:.0f} minutes",
            )
            return

        self._store.update(
            job_id,
            duration_seconds=round(info["duration_seconds"], 3),
            sample_rate=info["sample_rate"],
            channels=info["channels"],
        )

        source = job_dir / "source.wav"
        stem_dir = job_dir / "stems"
        preview_dir = job_dir / "preview"

        try:
            pipeline.decode_to_wav(upload, source, self._cfg)

            self._store.update(
                job_id, state=STATE_SEPARATING, progress=0.0, separation_pass=1
            )
            pipeline.separate(
                source, stem_dir, self._cfg, on_progress=self._progress_sink(job_id)
            )
            pipeline.normalise_stems(stem_dir, self._cfg)

            self._store.update(job_id, state=STATE_ENCODING, progress=None)
            pipeline.encode_previews(stem_dir, preview_dir, self._cfg)
        except pipeline.StageError as exc:
            self._fail(job_id, str(exc))
            return

        # The decoded source is reconstructible from the upload and is the
        # single largest file in the job; drop it once the stems exist.
        source.unlink(missing_ok=True)

        self._store.update(
            job_id,
            state=STATE_DONE,
            progress=100.0,
            stems=list(STEM_NAMES),
            finished_at=time.time(),
            error=None,
        )

    def _progress_sink(self, job_id: str):
        """Translate raw progress percentages into a persisted job update.

        Two things are handled here. The percentage restarts at zero for each
        model pass, so a drop is counted as a new pass rather than reported as
        the job going backwards. And every update rewrites job.json, so writes
        are throttled to twice a second.
        """
        state = {"pass": 1, "last": 0.0, "written_at": 0.0}

        def sink(value: float) -> None:
            now = time.time()
            new_pass = value < state["last"] - 1.0
            if new_pass:
                state["pass"] += 1
            state["last"] = value
            # A completed pass and a pass boundary always land; everything in
            # between is rate limited, since each update rewrites job.json.
            if value < 100.0 and not new_pass and now - state["written_at"] < 0.5:
                return
            state["written_at"] = now
            self._store.update(
                job_id, progress=value, separation_pass=state["pass"]
            )

        return sink

    def _fail(self, job_id: str, message: str) -> None:
        self._store.update(
            job_id,
            state=STATE_ERROR,
            error=pipeline.scrub(message, self._cfg),
            finished_at=time.time(),
            progress=None,
        )


def _find_upload(job_dir: Path) -> Path | None:
    for candidate in sorted(job_dir.glob("input.*")):
        if candidate.is_file():
            return candidate
    return None
