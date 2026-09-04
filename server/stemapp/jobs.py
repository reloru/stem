"""Job records: identity, on-disk layout, state transitions and expiry.

A job owns one directory under `<data>/jobs/<id>/`:

    input.<ext>        the file exactly as uploaded
    source.wav         44.1 kHz stereo decode of the upload
    stems/<name>.wav   separated stems, 16-bit PCM
    preview/<name>.mp3 lossy copies the browser loads into the mixer
    mixes/<mix>.<fmt>  rendered mixdowns
    stems.zip          all four stems, built on first request
    job.json           the record below, rewritten on every transition

The id is 32 characters of URL-safe randomness. It is the only thing guarding
a job's audio, so it is generated with `secrets` and never derived from the
filename.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .config import STEM_NAMES

JOB_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")

STATE_QUEUED = "queued"
STATE_PREPARING = "preparing"
STATE_SEPARATING = "separating"
STATE_ENCODING = "encoding"
STATE_DONE = "done"
STATE_ERROR = "error"

ACTIVE_STATES = frozenset(
    {STATE_QUEUED, STATE_PREPARING, STATE_SEPARATING, STATE_ENCODING}
)

_STAGE_LABELS = {
    STATE_QUEUED: "Waiting for a worker",
    STATE_PREPARING: "Decoding the upload",
    STATE_SEPARATING: "Separating stems",
    STATE_ENCODING: "Preparing playback copies",
    STATE_DONE: "Ready",
    STATE_ERROR: "Failed",
}


def new_job_id() -> str:
    return secrets.token_urlsafe(24)


def is_valid_job_id(value: str) -> bool:
    return bool(JOB_ID_RE.match(value))


@dataclass
class Job:
    id: str
    created_at: float
    updated_at: float
    state: str = STATE_QUEUED
    original_name: str = ""
    input_bytes: int = 0
    duration_seconds: float | None = None
    sample_rate: int | None = None
    channels: int | None = None
    progress: float | None = None
    # htdemucs runs the model over the track more than once; the observed
    # progress bar restarts on each pass, so the pass number is reported
    # alongside it rather than pretending the first pass was the whole job.
    separation_pass: int = 0
    started_at: float | None = None
    finished_at: float | None = None
    stems: list[str] = field(default_factory=list)
    error: str | None = None

    def to_public_dict(self, ttl_seconds: int) -> dict:
        payload = asdict(self)
        payload["stage"] = _STAGE_LABELS.get(self.state, self.state)
        payload["expires_at"] = self.created_at + ttl_seconds
        payload["elapsed_seconds"] = (
            (self.finished_at or time.time()) - self.started_at
            if self.started_at
            else None
        )
        return payload


class JobStore:
    """Thread-safe registry of jobs, mirrored to disk so restarts are honest."""

    def __init__(self, jobs_dir: Path, ttl_seconds: int) -> None:
        self._dir = jobs_dir
        self._ttl = ttl_seconds
        self._lock = threading.RLock()
        self._jobs: dict[str, Job] = {}

    # -- layout ----------------------------------------------------------

    def job_dir(self, job_id: str) -> Path:
        if not is_valid_job_id(job_id):
            raise ValueError(f"invalid job id: {job_id!r}")
        return self._dir / job_id

    def stem_path(self, job_id: str, stem: str) -> Path:
        if stem not in STEM_NAMES:
            raise ValueError(f"unknown stem: {stem!r}")
        return self.job_dir(job_id) / "stems" / f"{stem}.wav"

    def preview_path(self, job_id: str, stem: str) -> Path:
        if stem not in STEM_NAMES:
            raise ValueError(f"unknown stem: {stem!r}")
        return self.job_dir(job_id) / "preview" / f"{stem}.mp3"

    def zip_path(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "stems.zip"

    def mix_path(self, job_id: str, mix_id: str, suffix: str) -> Path:
        if not is_valid_job_id(mix_id):
            raise ValueError(f"invalid mix id: {mix_id!r}")
        return self.job_dir(job_id) / "mixes" / f"{mix_id}.{suffix}"

    # -- lifecycle -------------------------------------------------------

    def load_from_disk(self) -> None:
        """Re-read persisted jobs; anything mid-flight is marked failed.

        The work queue does not survive a restart, so a job left in an active
        state is dead. Recording that is more useful than leaving a spinner
        turning forever in the browser.
        """
        with self._lock:
            self._jobs.clear()
            if not self._dir.is_dir():
                return
            for entry in self._dir.iterdir():
                record = entry / "job.json"
                if not entry.is_dir() or not record.is_file():
                    continue
                try:
                    raw = json.loads(record.read_text("utf-8"))
                    job = Job(**raw)
                except (OSError, ValueError, TypeError):
                    continue
                if job.state in ACTIVE_STATES:
                    job.state = STATE_ERROR
                    job.error = "The server restarted while this job was running."
                    job.finished_at = time.time()
                    self._persist(job)
                self._jobs[job.id] = job

    def create(self, original_name: str) -> Job:
        now = time.time()
        with self._lock:
            job_id = new_job_id()
            while job_id in self._jobs:
                job_id = new_job_id()
            job = Job(
                id=job_id,
                created_at=now,
                updated_at=now,
                original_name=original_name,
            )
            self.job_dir(job_id).mkdir(parents=True, exist_ok=True)
            self._jobs[job_id] = job
            self._persist(job)
            return job

    def get(self, job_id: str) -> Job | None:
        if not is_valid_job_id(job_id):
            return None
        with self._lock:
            return self._jobs.get(job_id)

    def update(self, job_id: str, **changes) -> Job | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            for key, value in changes.items():
                if not hasattr(job, key):
                    raise AttributeError(f"Job has no field {key!r}")
                setattr(job, key, value)
            job.updated_at = time.time()
            self._persist(job)
            return job

    def delete(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.pop(job_id, None)
            directory = self.job_dir(job_id) if is_valid_job_id(job_id) else None
        if directory is not None and directory.exists():
            shutil.rmtree(directory, ignore_errors=True)
        return job is not None

    def expired_ids(self, now: float | None = None) -> list[str]:
        cutoff = (now or time.time()) - self._ttl
        with self._lock:
            return [j.id for j in self._jobs.values() if j.created_at < cutoff]

    def sweep(self) -> int:
        removed = 0
        for job_id in self.expired_ids():
            if self.delete(job_id):
                removed += 1
        # Directories with no in-memory record (a crashed create, or a job.json
        # that failed to parse) still consume disk; clear the stale ones.
        cutoff = time.time() - self._ttl
        if self._dir.is_dir():
            with self._lock:
                known = set(self._jobs)
            for entry in self._dir.iterdir():
                if entry.name in known or not entry.is_dir():
                    continue
                try:
                    if entry.stat().st_mtime < cutoff:
                        shutil.rmtree(entry, ignore_errors=True)
                        removed += 1
                except OSError:
                    continue
        return removed

    # -- persistence -----------------------------------------------------

    def _persist(self, job: Job) -> None:
        directory = self.job_dir(job.id)
        try:
            directory.mkdir(parents=True, exist_ok=True)
            temporary = directory / "job.json.tmp"
            temporary.write_text(json.dumps(asdict(job), indent=2), "utf-8")
            os.replace(temporary, directory / "job.json")
        except OSError:
            # Losing the mirror is survivable: the in-memory record is
            # authoritative while the process lives.
            pass
