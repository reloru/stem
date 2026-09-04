"""Streaming multipart/form-data reader.

The standard library lost a usable multipart parser when `cgi` was deprecated
(removed in Python 3.13), and `email` buffers the whole body in memory. Uploads
here are whole audio files, so the file part is written to disk as it arrives
and never fully materialises in RAM.

Wire format handled (RFC 7578 / RFC 2046):

    --BOUNDARY CRLF
    header-line CRLF ... CRLF
    part body
    CRLF --BOUNDARY CRLF
    ... more parts ...
    CRLF --BOUNDARY -- CRLF
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Callable

CRLF = b"\r\n"
_CHUNK = 64 * 1024
_MAX_HEADER_BYTES = 16 * 1024
_MAX_FIELD_BYTES = 8 * 1024

_BOUNDARY_RE = re.compile(rb'boundary=(?:"([^"]+)"|([^;]+))', re.IGNORECASE)
_DISPOSITION_NAME_RE = re.compile(r'\bname="([^"]*)"', re.IGNORECASE)
_DISPOSITION_FILENAME_RE = re.compile(r'\bfilename="([^"]*)"', re.IGNORECASE)


class MultipartError(ValueError):
    """The request body is not well-formed multipart/form-data."""


class PayloadTooLarge(MultipartError):
    """A part exceeded the configured byte budget."""


@dataclass
class UploadedFile:
    field_name: str
    filename: str
    content_type: str
    path: Path
    size: int


@dataclass
class MultipartResult:
    fields: dict[str, str]
    files: dict[str, UploadedFile]


def boundary_from_content_type(content_type: str | None) -> bytes:
    """Extract the boundary token, or raise if this is not multipart."""
    if not content_type:
        raise MultipartError("missing Content-Type")
    raw = content_type.encode("latin-1", "replace")
    if not raw.lower().startswith(b"multipart/form-data"):
        raise MultipartError("Content-Type is not multipart/form-data")
    match = _BOUNDARY_RE.search(raw)
    if not match:
        raise MultipartError("multipart Content-Type has no boundary")
    boundary = match.group(1) or match.group(2)
    boundary = boundary.strip()
    if not boundary:
        raise MultipartError("multipart boundary is empty")
    return boundary


class _Body:
    """A bounded, re-fillable view over the request body."""

    def __init__(self, stream: BinaryIO, length: int) -> None:
        self._stream = stream
        self._remaining = length
        self._buf = bytearray()
        self.exhausted = False

    def _pull(self) -> int:
        if self._remaining <= 0:
            self.exhausted = True
            return 0
        want = min(_CHUNK, self._remaining)
        data = self._stream.read(want)
        if not data:
            self._remaining = 0
            self.exhausted = True
            return 0
        self._remaining -= len(data)
        self._buf += data
        return len(data)

    def fill_to(self, minimum: int) -> int:
        while len(self._buf) < minimum and not self.exhausted:
            if self._pull() == 0:
                break
        return len(self._buf)

    def find(self, marker: bytes, *, max_scan: int) -> int:
        """Index of `marker`, pulling more body in until found or exhausted.

        `max_scan` bounds how much body may be buffered while searching, so a
        body that never produces the marker cannot exhaust memory.
        """
        start = 0
        while True:
            index = self._buf.find(marker, start)
            if index != -1:
                return index
            if len(self._buf) > max_scan:
                raise PayloadTooLarge(
                    f"no {marker!r} within {max_scan} bytes"
                )
            if self.exhausted:
                return -1
            start = max(0, len(self._buf) - len(marker) + 1)
            if self._pull() == 0:
                return self._buf.find(marker, 0)

    def take(self, count: int) -> bytes:
        self.fill_to(count)
        if len(self._buf) < count:
            raise MultipartError("body ended mid-part")
        chunk = bytes(self._buf[:count])
        del self._buf[:count]
        return chunk

    def peek(self, count: int) -> bytes:
        self.fill_to(count)
        return bytes(self._buf[:count])

    def drop(self, count: int) -> None:
        del self._buf[:count]

    def scan(self, marker: bytes) -> int:
        """Index of `marker` in what is already buffered; no further reads."""
        return self._buf.find(marker)

    @property
    def buffered(self) -> int:
        return len(self._buf)


def _parse_part_headers(raw: bytes) -> tuple[str, str, str]:
    """Return (field_name, filename, content_type) for one part."""
    field_name = ""
    filename = ""
    content_type = "application/octet-stream"
    for line in raw.split(CRLF):
        if not line:
            continue
        decoded = line.decode("utf-8", "replace")
        name, _, value = decoded.partition(":")
        key = name.strip().lower()
        value = value.strip()
        if key == "content-disposition":
            name_match = _DISPOSITION_NAME_RE.search(value)
            if name_match:
                field_name = name_match.group(1)
            file_match = _DISPOSITION_FILENAME_RE.search(value)
            if file_match:
                filename = file_match.group(1)
        elif key == "content-type":
            content_type = value
    if not field_name:
        raise MultipartError("part is missing a Content-Disposition name")
    return field_name, filename, content_type


def parse(
    stream: BinaryIO,
    content_type: str | None,
    content_length: int,
    *,
    max_file_bytes: int,
    file_destination: Callable[[str, str], Path],
) -> MultipartResult:
    """Read the whole body, writing file parts through `file_destination`.

    `file_destination(field_name, client_filename)` returns the path a file
    part should be written to. It is only called for parts that carry a
    filename, and only once per part.
    """
    boundary = boundary_from_content_type(content_type)
    delimiter = CRLF + b"--" + boundary
    body = _Body(stream, content_length)

    # The preamble before the first boundary is legal but unusual; skipping to
    # the first delimiter handles both "--boundary" at offset 0 and a preamble.
    opening = b"--" + boundary
    if body.peek(len(opening)) == opening:
        body.drop(len(opening))
    else:
        index = body.find(delimiter, max_scan=_MAX_HEADER_BYTES)
        if index == -1:
            raise MultipartError("no multipart boundary found in body")
        body.drop(index + len(delimiter))

    fields: dict[str, str] = {}
    files: dict[str, UploadedFile] = {}

    while True:
        terminator = body.peek(2)
        if terminator == b"--":
            break
        if terminator != CRLF:
            raise MultipartError("malformed boundary terminator")
        body.take(2)

        header_end = body.find(CRLF + CRLF, max_scan=_MAX_HEADER_BYTES)
        if header_end == -1:
            raise MultipartError("part headers are unterminated")
        raw_headers = body.take(header_end)
        body.take(4)

        field_name, filename, part_type = _parse_part_headers(raw_headers)

        if filename:
            path = file_destination(field_name, filename)
            written = _drain_to_file(body, delimiter, path, max_file_bytes)
            files[field_name] = UploadedFile(
                field_name=field_name,
                filename=filename,
                content_type=part_type,
                path=path,
                size=written,
            )
        else:
            value = _drain_to_memory(body, delimiter, _MAX_FIELD_BYTES)
            fields[field_name] = value.decode("utf-8", "replace")

    return MultipartResult(fields=fields, files=files)


def _drain_to_file(
    body: _Body, delimiter: bytes, path: Path, max_bytes: int
) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with path.open("wb") as handle:
        for chunk in _part_chunks(body, delimiter):
            written += len(chunk)
            if written > max_bytes:
                handle.close()
                path.unlink(missing_ok=True)
                raise PayloadTooLarge(
                    f"upload exceeds the {max_bytes} byte limit"
                )
            handle.write(chunk)
    return written


def _drain_to_memory(body: _Body, delimiter: bytes, max_bytes: int) -> bytes:
    collected = bytearray()
    for chunk in _part_chunks(body, delimiter):
        collected += chunk
        if len(collected) > max_bytes:
            raise PayloadTooLarge("form field is too large")
    return bytes(collected)


def _part_chunks(body: _Body, delimiter: bytes):
    """Yield the body of the current part, stopping at its closing delimiter.

    A delimiter can straddle two reads, so everything except the last
    len(delimiter)-1 bytes of the buffer is safe to emit before refilling.
    """
    keep = len(delimiter) - 1
    while True:
        index = body.scan(delimiter)
        if index != -1:
            if index:
                yield body.take(index)
            body.take(len(delimiter))
            return
        if body.exhausted:
            raise MultipartError("body ended before the closing boundary")
        if body.buffered > keep:
            yield body.take(body.buffered - keep)
        body.fill_to(body.buffered + 1)
