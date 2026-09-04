"""Runtime configuration, resolved once from the environment at startup.

Every knob is an environment variable so the systemd unit is the single place
deployment settings live. Nothing here reads a config file, and nothing here
has a network side effect.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

# Stem identifiers used in URLs, filenames and the JSON API. Order is the
# display order in the mixer.
STEM_NAMES = ("vocals", "drums", "bass", "other")

# audio-separator labels the outputs of a four-stem Demucs model with these
# capitalised names. Passing the mapping to --custom_output_names forces
# deterministic filenames instead of the default
# "<input>_(Vocals)_<model>.wav" convention.
SEPARATOR_STEM_LABELS = {
    "Vocals": "vocals",
    "Drums": "drums",
    "Bass": "bass",
    "Other": "other",
}

# Containers ffmpeg can decode that a user is plausibly uploading. The suffix
# check is a fast reject; ffprobe is the real gate.
ACCEPTED_SUFFIXES = frozenset(
    {
        ".aac",
        ".aif",
        ".aiff",
        ".flac",
        ".m4a",
        ".mp3",
        ".mp4",
        ".oga",
        ".ogg",
        ".opus",
        ".wav",
        ".webm",
        ".wma",
    }
)

MIX_FORMATS = {
    "wav": ("audio/wav", ("-c:a", "pcm_s16le")),
    "mp3": ("audio/mpeg", ("-c:a", "libmp3lame", "-b:a", "320k")),
}


class ConfigError(RuntimeError):
    """Raised for an unusable configuration; fatal at startup."""


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from None
    if value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {value}")
    return value


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _resolve_executable(name: str, env_var: str) -> str:
    configured = os.environ.get(env_var)
    if configured:
        return configured
    found = shutil.which(name)
    return found or name


@dataclass(frozen=True)
class Config:
    host: str
    port: int
    data_dir: Path
    web_dir: Path
    access_key: str
    open_access: bool
    max_upload_bytes: int
    max_duration_seconds: int
    job_ttl_seconds: int
    model_filename: str
    model_dir: Path
    separator_bin: str
    ffmpeg_bin: str
    ffprobe_bin: str
    preview_bitrate: str
    separator_timeout_seconds: int
    worker_count: int

    @classmethod
    def from_env(cls, repo_root: Path) -> "Config":
        data_dir = Path(
            os.environ.get("STEM_DATA_DIR") or (repo_root / "data")
        ).expanduser()
        web_dir = Path(
            os.environ.get("STEM_WEB_DIR") or (repo_root / "web")
        ).expanduser()

        open_access = _env_bool("STEM_ALLOW_OPEN")
        access_key = os.environ.get("STEM_ACCESS_KEY", "").strip()
        if not access_key and not open_access:
            raise ConfigError(
                "STEM_ACCESS_KEY is not set. Set it to a random string, or set "
                "STEM_ALLOW_OPEN=1 to run without an access key."
            )
        if access_key and len(access_key) < 12:
            raise ConfigError("STEM_ACCESS_KEY must be at least 12 characters.")

        return cls(
            host=os.environ.get("STEM_HOST", "127.0.0.1"),
            port=_env_int("STEM_PORT", 8080),
            data_dir=data_dir,
            web_dir=web_dir,
            access_key=access_key,
            open_access=open_access,
            max_upload_bytes=_env_int("STEM_MAX_UPLOAD_MB", 100) * 1024 * 1024,
            max_duration_seconds=_env_int("STEM_MAX_DURATION_S", 300),
            job_ttl_seconds=_env_int("STEM_JOB_TTL_HOURS", 24) * 3600,
            model_filename=os.environ.get("STEM_MODEL", "htdemucs.yaml"),
            model_dir=Path(
                os.environ.get("STEM_MODEL_DIR") or (data_dir / "models")
            ).expanduser(),
            separator_bin=_resolve_executable(
                "audio-separator", "STEM_SEPARATOR_BIN"
            ),
            ffmpeg_bin=_resolve_executable("ffmpeg", "STEM_FFMPEG"),
            ffprobe_bin=_resolve_executable("ffprobe", "STEM_FFPROBE"),
            preview_bitrate=os.environ.get("STEM_PREVIEW_BITRATE", "192k"),
            separator_timeout_seconds=_env_int("STEM_SEPARATOR_TIMEOUT_S", 3600),
            worker_count=_env_int("STEM_WORKERS", 1),
        )

    @property
    def jobs_dir(self) -> Path:
        return self.data_dir / "jobs"

    def ensure_directories(self) -> None:
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.model_dir.mkdir(parents=True, exist_ok=True)

    def missing_executables(self) -> list[str]:
        """Names of required executables that cannot be found on PATH."""
        missing = []
        for label, binary in (
            ("ffmpeg", self.ffmpeg_bin),
            ("ffprobe", self.ffprobe_bin),
            ("audio-separator", self.separator_bin),
        ):
            if shutil.which(binary) is None and not Path(binary).is_file():
                missing.append(f"{label} (looked for {binary!r})")
        return missing
