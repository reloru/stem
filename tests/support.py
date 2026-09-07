"""Fixtures shared by the tests. Not a test module; `discover` skips it.

The Config here is built by calling the dataclass directly rather than through
`Config.from_env`, so a test that is not about environment parsing does not
have to stage environment variables to get one.
"""

from __future__ import annotations

from pathlib import Path

from stemapp.config import DEFAULT_MODEL, Config

CRLF = b"\r\n"


def make_config(root: Path, **overrides) -> Config:
    """A Config rooted at `root`, with every field explicit."""
    fields = {
        "host": "127.0.0.1",
        "port": 8080,
        "data_dir": root / "data",
        "web_dir": root / "web",
        "access_key": "test-access-key",
        "open_access": False,
        "max_upload_bytes": 100 * 1024 * 1024,
        "max_duration_seconds": 300,
        "job_ttl_seconds": 24 * 3600,
        "model_filename": DEFAULT_MODEL,
        "model_dir": root / "data" / "models",
        "separator_bin": "audio-separator",
        "ffmpeg_bin": "ffmpeg",
        "ffprobe_bin": "ffprobe",
        "preview_bitrate": "128k",
        "separator_timeout_seconds": 3600,
        "worker_count": 1,
    }
    fields.update(overrides)
    return Config(**fields)


def multipart_body(boundary: str, parts, *, preamble: bytes = b"") -> bytes:
    """Assemble a multipart/form-data body.

    `parts` is a sequence of (header_block, payload) where header_block is the
    part's headers as one string with CRLF between lines and no trailing CRLF.
    The shape produced is the one RFC 7578 describes and the parser expects:
    an opening `--boundary`, then for each part CRLF, headers, a blank line,
    the payload, and a closing CRLF`--boundary`, finished with `--`.
    """
    marker = boundary.encode()
    out = bytearray(preamble)
    out += b"--" + marker
    for headers, payload in parts:
        out += CRLF + headers.encode() + CRLF + CRLF + payload + CRLF + b"--" + marker
    out += b"--" + CRLF
    return bytes(out)


def file_part(field: str, filename: str, *, content_type: str = "audio/wav") -> str:
    return (
        f'Content-Disposition: form-data; name="{field}"; filename="{filename}"'
        f"{CRLF.decode()}Content-Type: {content_type}"
    )


def field_part(name: str) -> str:
    return f'Content-Disposition: form-data; name="{name}"'
