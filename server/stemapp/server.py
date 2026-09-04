"""HTTP layer: routing, access control, static files, ranged downloads.

Built on `http.server` rather than a framework. The API is nine routes and one
upload path, which is well inside what the standard library handles, and it
keeps the web tier free of the dependency tree the separator drags in.

Access control has two tiers, matching the "unlisted URL" model this is built
for. Anything that costs CPU or creates state requires the shared access key.
Anything that only reads a job's artifacts is authorised by knowing the job id,
which is 192 bits of randomness and appears in no listing.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

from . import multipart, pipeline
from .config import ACCEPTED_SUFFIXES, MIX_FORMATS, STEM_NAMES, Config
from .jobs import STATE_DONE, JobStore, is_valid_job_id
from .worker import Worker

_MAX_JSON_BODY = 64 * 1024
_UPLOAD_OVERHEAD = 1024 * 1024  # multipart headers and boundaries

_STATIC_ROUTES = {
    "/": "index.html",
    "/index.html": "index.html",
    "/app.js": "app.js",
    "/styles.css": "styles.css",
    "/favicon.svg": "favicon.svg",
    "/manifest.webmanifest": "manifest.webmanifest",
    "/icon-192.png": "icon-192.png",
    "/icon-512.png": "icon-512.png",
}

_CONTENT_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mp3": "audio/mpeg",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".wav": "audio/wav",
    ".webmanifest": "application/manifest+json",
    ".zip": "application/zip",
}

_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; "
    "img-src 'self' data:; media-src 'self' blob:; connect-src 'self'; "
    "form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
)

_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")
_UNSAFE_NAME_RE = re.compile(r"[^A-Za-z0-9 ._-]+")


def safe_base_name(filename: str) -> str:
    """A filesystem- and header-safe stem for derived filenames."""
    tail = filename.replace("\\", "/").rsplit("/", 1)[-1]
    without_suffix = tail.rsplit(".", 1)[0] if "." in tail else tail
    cleaned = _UNSAFE_NAME_RE.sub("_", without_suffix).strip(" ._-")
    cleaned = re.sub(r"\s+", " ", cleaned)[:64]
    return cleaned or "track"


class StemServer(ThreadingHTTPServer):
    daemon_threads = True
    # A separation job holds its request thread only for the upload, but a
    # mixdown runs inline, so allow the socket queue to absorb a burst.
    request_queue_size = 32

    def __init__(self, address, handler, cfg: Config, store: JobStore, worker: Worker):
        super().__init__(address, handler)
        self.cfg = cfg
        self.store = store
        self.worker = worker


class StemHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "stem"
    sys_version = ""

    # -- plumbing --------------------------------------------------------

    @property
    def cfg(self) -> Config:
        return self.server.cfg  # type: ignore[attr-defined]

    @property
    def store(self) -> JobStore:
        return self.server.store  # type: ignore[attr-defined]

    @property
    def worker(self) -> Worker:
        return self.server.worker  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args) -> None:
        print(
            f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] "
            f"{self.address_string()} {fmt % args}",
            flush=True,
        )

    def _base_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", _CSP)

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self._base_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _error(self, status: int, message: str) -> None:
        # An error can be sent before the request body has been drained; a
        # keep-alive connection would then read the leftover bytes as the next
        # request line.
        self.close_connection = True
        self._json(status, {"error": message})

    def _authorised(self) -> bool:
        if self.cfg.open_access:
            return True
        supplied = self.headers.get("X-Stem-Key", "")
        return hmac.compare_digest(supplied, self.cfg.access_key)

    def _require_key(self) -> bool:
        if self._authorised():
            return True
        self._error(HTTPStatus.UNAUTHORIZED, "Access key missing or incorrect.")
        return False

    # -- file responses --------------------------------------------------

    def _send_file(
        self,
        path: Path,
        *,
        download_name: str | None = None,
        cache_seconds: int = 0,
    ) -> None:
        try:
            size = path.stat().st_size
        except OSError:
            self._error(HTTPStatus.NOT_FOUND, "Not found.")
            return

        content_type = _CONTENT_TYPES.get(
            path.suffix.lower(), "application/octet-stream"
        )
        start, end = 0, size - 1
        partial = False

        range_header = self.headers.get("Range")
        if range_header and size:
            parsed = _parse_range(range_header, size)
            if parsed is None:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self._base_headers()
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            start, end = parsed
            partial = True

        length = end - start + 1
        self.send_response(
            HTTPStatus.PARTIAL_CONTENT if partial else HTTPStatus.OK
        )
        self._base_headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        if download_name:
            self.send_header(
                "Content-Disposition",
                f'attachment; filename="{download_name}"',
            )
        self.send_header(
            "Cache-Control",
            f"private, max-age={cache_seconds}" if cache_seconds else "no-cache",
        )
        self.end_headers()

        if self.command == "HEAD":
            return
        try:
            with path.open("rb") as handle:
                handle.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = handle.read(min(256 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    # -- dispatch --------------------------------------------------------

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_HEAD(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        path = self.path.split("?", 1)[0]
        for route_method, pattern, handler in _ROUTES:
            if route_method != method:
                continue
            match = pattern.match(path)
            if match:
                try:
                    handler(self, *match.groups())
                except (BrokenPipeError, ConnectionResetError):
                    self.close_connection = True
                except Exception as exc:  # noqa: BLE001
                    self.log_message("unhandled error on %s: %s", path, exc)
                    try:
                        self._error(
                            HTTPStatus.INTERNAL_SERVER_ERROR,
                            "The server hit an unexpected error.",
                        )
                    except Exception:  # noqa: BLE001
                        self.close_connection = True
                return
        if method == "GET" and path in _STATIC_ROUTES:
            self._serve_static(_STATIC_ROUTES[path])
            return
        self._error(HTTPStatus.NOT_FOUND, "Not found.")

    # -- handlers --------------------------------------------------------

    def _serve_static(self, filename: str) -> None:
        path = self.cfg.web_dir / filename
        if not path.is_file():
            self._error(HTTPStatus.NOT_FOUND, "Not found.")
            return
        self._send_file(path)

    def handle_config(self) -> None:
        self._json(
            HTTPStatus.OK,
            {
                "stems": list(STEM_NAMES),
                "max_upload_mb": self.cfg.max_upload_bytes // (1024 * 1024),
                "max_duration_seconds": self.cfg.max_duration_seconds,
                "job_ttl_hours": self.cfg.job_ttl_seconds // 3600,
                "requires_key": not self.cfg.open_access,
                "model": self.cfg.model_filename,
                "accepted_suffixes": sorted(ACCEPTED_SUFFIXES),
                "mix_formats": sorted(MIX_FORMATS),
                "queue_depth": self.worker.pending,
            },
        )

    def handle_verify_key(self) -> None:
        if not self._require_key():
            return
        self._json(HTTPStatus.OK, {"ok": True})

    def handle_create_job(self) -> None:
        if not self._require_key():
            return

        try:
            content_length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._error(HTTPStatus.LENGTH_REQUIRED, "Content-Length is required.")
            return
        if content_length <= 0:
            self._error(HTTPStatus.BAD_REQUEST, "Empty upload.")
            return
        if content_length > self.cfg.max_upload_bytes + _UPLOAD_OVERHEAD:
            self._error(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                f"That file is larger than the "
                f"{self.cfg.max_upload_bytes // (1024 * 1024)} MB limit.",
            )
            return

        job = self.store.create(original_name="")
        job_dir = self.store.job_dir(job.id)
        captured: dict[str, str] = {}

        def destination(field_name: str, client_filename: str) -> Path:
            suffix = Path(client_filename).suffix.lower()
            if suffix not in ACCEPTED_SUFFIXES:
                raise multipart.MultipartError(
                    f"{suffix or 'this file type'} is not a supported audio "
                    "format"
                )
            captured["filename"] = client_filename
            return job_dir / f"input{suffix}"

        try:
            result = multipart.parse(
                self.rfile,
                self.headers.get("Content-Type"),
                content_length,
                max_file_bytes=self.cfg.max_upload_bytes,
                file_destination=destination,
            )
        except multipart.PayloadTooLarge as exc:
            self.store.delete(job.id)
            self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, str(exc))
            return
        except multipart.MultipartError as exc:
            self.store.delete(job.id)
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
            return

        upload = result.files.get("file")
        if upload is None or upload.size == 0:
            self.store.delete(job.id)
            self._error(HTTPStatus.BAD_REQUEST, "No audio file was attached.")
            return

        self.store.update(
            job.id,
            original_name=safe_base_name(captured.get("filename", "track")),
            input_bytes=upload.size,
        )
        self.worker.submit(job.id)
        job = self.store.get(job.id)
        assert job is not None
        self._json(HTTPStatus.ACCEPTED, job.to_public_dict(self.cfg.job_ttl_seconds))

    def handle_job_status(self, job_id: str) -> None:
        job = self.store.get(job_id)
        if job is None:
            self._error(HTTPStatus.NOT_FOUND, "No such job, or it has expired.")
            return
        self._json(HTTPStatus.OK, job.to_public_dict(self.cfg.job_ttl_seconds))

    def handle_delete_job(self, job_id: str) -> None:
        if not self._require_key():
            return
        if not self.store.delete(job_id):
            self._error(HTTPStatus.NOT_FOUND, "No such job.")
            return
        self._json(HTTPStatus.OK, {"deleted": job_id})

    def handle_preview(self, job_id: str, stem: str) -> None:
        job = self._done_job(job_id)
        if job is None:
            return
        if stem not in STEM_NAMES:
            self._error(HTTPStatus.NOT_FOUND, "No such stem.")
            return
        self._send_file(self.store.preview_path(job_id, stem), cache_seconds=3600)

    def handle_stem_download(self, job_id: str, stem: str) -> None:
        job = self._done_job(job_id)
        if job is None:
            return
        if stem not in STEM_NAMES:
            self._error(HTTPStatus.NOT_FOUND, "No such stem.")
            return
        self._send_file(
            self.store.stem_path(job_id, stem),
            download_name=f"{job.original_name} - {stem}.wav",
            cache_seconds=3600,
        )

    def handle_zip(self, job_id: str) -> None:
        job = self._done_job(job_id)
        if job is None:
            return
        archive = self.store.zip_path(job_id)
        if not archive.is_file():
            pipeline.build_stem_zip(
                self.store.job_dir(job_id) / "stems", archive, job.original_name
            )
        self._send_file(
            archive,
            download_name=f"{job.original_name} - stems.zip",
            cache_seconds=3600,
        )

    def handle_create_mix(self, job_id: str) -> None:
        if not self._require_key():
            return
        job = self._done_job(job_id)
        if job is None:
            return

        payload = self._read_json_body()
        if payload is None:
            return

        raw_gains = payload.get("gains")
        if not isinstance(raw_gains, dict):
            self._error(HTTPStatus.BAD_REQUEST, "Expected a 'gains' object.")
            return

        gains: dict[str, float] = {}
        for name in STEM_NAMES:
            value = raw_gains.get(name, 1.0)
            if not isinstance(value, (int, float)) or value != value:
                self._error(
                    HTTPStatus.BAD_REQUEST, f"Gain for {name} is not a number."
                )
                return
            gains[name] = max(0.0, min(4.0, float(value)))

        output_format = str(payload.get("format", "wav")).lower()
        if output_format not in MIX_FORMATS:
            self._error(
                HTTPStatus.BAD_REQUEST,
                f"Format must be one of {', '.join(sorted(MIX_FORMATS))}.",
            )
            return

        # Naming the mix after its own settings makes re-exporting an unchanged
        # balance free, and keeps the id opaque.
        digest = hashlib.sha256(
            json.dumps(
                {"gains": gains, "format": output_format}, sort_keys=True
            ).encode("utf-8")
        ).hexdigest()[:24]
        destination = self.store.mix_path(job_id, digest, output_format)
        sidecar = destination.with_suffix(destination.suffix + ".json")

        measurement: dict | None = None
        if destination.is_file() and sidecar.is_file():
            try:
                measurement = json.loads(sidecar.read_text("utf-8"))
            except (OSError, ValueError):
                measurement = None

        if measurement is None:
            try:
                measurement = pipeline.render_mix(
                    self.store.job_dir(job_id) / "stems",
                    gains,
                    destination,
                    self.cfg,
                    output_format,
                )
            except pipeline.StageError as exc:
                self._error(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    pipeline.scrub(str(exc), self.cfg),
                )
                return
            try:
                sidecar.write_text(json.dumps(measurement), "utf-8")
            except OSError:
                pass

        self._json(
            HTTPStatus.OK,
            {
                "mix_id": digest,
                "format": output_format,
                "url": f"/api/jobs/{job_id}/mix/{digest}.{output_format}",
                "bytes": destination.stat().st_size,
                "peak_db": measurement.get("peak_db"),
                "attenuation_db": measurement.get("attenuation_db", 0.0),
            },
        )

    def handle_mix_download(self, job_id: str, mix_id: str, suffix: str) -> None:
        job = self._done_job(job_id)
        if job is None:
            return
        if suffix not in MIX_FORMATS:
            self._error(HTTPStatus.NOT_FOUND, "No such mix format.")
            return
        try:
            path = self.store.mix_path(job_id, mix_id, suffix)
        except ValueError:
            self._error(HTTPStatus.NOT_FOUND, "No such mix.")
            return
        self._send_file(
            path,
            download_name=f"{job.original_name} - mix.{suffix}",
            cache_seconds=3600,
        )

    # -- helpers ---------------------------------------------------------

    def _done_job(self, job_id: str):
        if not is_valid_job_id(job_id):
            self._error(HTTPStatus.NOT_FOUND, "No such job.")
            return None
        job = self.store.get(job_id)
        if job is None:
            self._error(HTTPStatus.NOT_FOUND, "No such job, or it has expired.")
            return None
        if job.state != STATE_DONE:
            self._error(
                HTTPStatus.CONFLICT, "This job has not finished separating."
            )
            return None
        return job

    def _read_json_body(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._error(HTTPStatus.LENGTH_REQUIRED, "Content-Length is required.")
            return None
        if length < 0 or length > _MAX_JSON_BODY:
            self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Body too large.")
            return None
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._error(HTTPStatus.BAD_REQUEST, "Body is not valid JSON.")
            return None
        if not isinstance(payload, dict):
            self._error(HTTPStatus.BAD_REQUEST, "Body must be a JSON object.")
            return None
        return payload


def _parse_range(header: str, size: int) -> tuple[int, int] | None:
    match = _RANGE_RE.match(header.strip())
    if not match:
        return None
    first, last = match.group(1), match.group(2)
    if first == "" and last == "":
        return None
    if first == "":
        length = int(last)
        if length <= 0:
            return None
        start = max(0, size - length)
        return start, size - 1
    start = int(first)
    if start >= size:
        return None
    end = int(last) if last else size - 1
    end = min(end, size - 1)
    if end < start:
        return None
    return start, end


_ROUTES: list[tuple[str, re.Pattern[str], Callable]] = [
    ("GET", re.compile(r"^/api/config$"), StemHandler.handle_config),
    ("POST", re.compile(r"^/api/key$"), StemHandler.handle_verify_key),
    ("POST", re.compile(r"^/api/jobs$"), StemHandler.handle_create_job),
    (
        "GET",
        re.compile(r"^/api/jobs/([A-Za-z0-9_-]{16,64})$"),
        StemHandler.handle_job_status,
    ),
    (
        "DELETE",
        re.compile(r"^/api/jobs/([A-Za-z0-9_-]{16,64})$"),
        StemHandler.handle_delete_job,
    ),
    (
        "GET",
        re.compile(r"^/api/jobs/([A-Za-z0-9_-]{16,64})/preview/([a-z]+)\.mp3$"),
        StemHandler.handle_preview,
    ),
    (
        "GET",
        re.compile(r"^/api/jobs/([A-Za-z0-9_-]{16,64})/stems/([a-z]+)\.wav$"),
        StemHandler.handle_stem_download,
    ),
    (
        "GET",
        re.compile(r"^/api/jobs/([A-Za-z0-9_-]{16,64})/stems\.zip$"),
        StemHandler.handle_zip,
    ),
    (
        "POST",
        re.compile(r"^/api/jobs/([A-Za-z0-9_-]{16,64})/mix$"),
        StemHandler.handle_create_mix,
    ),
    (
        "GET",
        re.compile(
            r"^/api/jobs/([A-Za-z0-9_-]{16,64})/mix/([A-Za-z0-9_-]{16,64})"
            r"\.([a-z0-9]{1,5})$"
        ),
        StemHandler.handle_mix_download,
    ),
]
