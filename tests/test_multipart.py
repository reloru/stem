"""Tests for the hand-written streaming multipart parser.

This parser exists because `cgi` was removed in Python 3.13 and `email`
buffers whole bodies in memory, and it is the only place in the project that
reads a wire format by hand. Until now nothing had ever run it against a
malformed body.
"""

from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path

from stemapp import multipart
from support import field_part, file_part, multipart_body

BOUNDARY = "----stemtestboundary9f2c"


class ParserTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.written: list[Path] = []

    def destination(self, field_name: str, client_filename: str) -> Path:
        path = self.root / f"input{Path(client_filename).suffix}"
        self.written.append(path)
        return path

    def parse(self, body: bytes, *, boundary: str = BOUNDARY, max_file_bytes: int = 1 << 20):
        return multipart.parse(
            io.BytesIO(body),
            f"multipart/form-data; boundary={boundary}",
            len(body),
            max_file_bytes=max_file_bytes,
            file_destination=self.destination,
        )


class TestHappyPath(ParserTestCase):
    def test_file_and_field(self) -> None:
        payload = b"RIFF....WAVEfmt " + bytes(range(256)) * 4
        body = multipart_body(
            BOUNDARY,
            [
                (field_part("model"), b"htdemucs_6s.yaml"),
                (file_part("file", "Some Track.wav"), payload),
            ],
        )
        result = self.parse(body)

        self.assertEqual(result.fields["model"], "htdemucs_6s.yaml")
        upload = result.files["file"]
        self.assertEqual(upload.filename, "Some Track.wav")
        self.assertEqual(upload.content_type, "audio/wav")
        self.assertEqual(upload.size, len(payload))
        self.assertEqual(upload.path.read_bytes(), payload)

    def test_field_after_the_file_part_is_still_parsed(self) -> None:
        # The upload handler resolves the model after parsing precisely
        # because a client may order the parts either way.
        payload = b"\x00\x01\x02" * 100
        body = multipart_body(
            BOUNDARY,
            [
                (file_part("file", "t.mp3"), payload),
                (field_part("model"), b"htdemucs.yaml"),
            ],
        )
        result = self.parse(body)
        self.assertEqual(result.fields["model"], "htdemucs.yaml")
        self.assertEqual(result.files["file"].size, len(payload))

    def test_preamble_before_the_first_boundary_is_skipped(self) -> None:
        body = multipart_body(
            BOUNDARY,
            [(file_part("file", "t.flac"), b"payload")],
            preamble=b"This is a preamble a client is allowed to send.\r\n",
        )
        result = self.parse(body)
        self.assertEqual(result.files["file"].path.read_bytes(), b"payload")

    def test_quoted_boundary_token(self) -> None:
        payload = b"quoted-boundary-payload"
        body = multipart_body(BOUNDARY, [(file_part("file", "t.wav"), payload)])
        result = multipart.parse(
            io.BytesIO(body),
            f'multipart/form-data; boundary="{BOUNDARY}"',
            len(body),
            max_file_bytes=1 << 20,
            file_destination=self.destination,
        )
        self.assertEqual(result.files["file"].path.read_bytes(), payload)

    def test_empty_file_part(self) -> None:
        # The handler rejects this as "No audio file was attached"; the parser
        # itself should report it as a zero-byte part, not an error.
        body = multipart_body(BOUNDARY, [(file_part("file", "t.wav"), b"")])
        result = self.parse(body)
        self.assertEqual(result.files["file"].size, 0)


class TestChunkBoundaries(ParserTestCase):
    """A delimiter can straddle two reads of the underlying stream.

    `_part_chunks` holds back len(delimiter)-1 bytes before refilling for
    exactly this reason. These sizes put the payload's end at, just before and
    just after the 64 KiB read size so the closing delimiter lands across a
    read in at least one of them.
    """

    def test_payload_sizes_around_the_read_size(self) -> None:
        chunk = multipart._CHUNK
        delimiter_length = len(b"\r\n--" + BOUNDARY.encode())
        sizes = [
            chunk - delimiter_length,
            chunk - delimiter_length + 1,
            chunk - 1,
            chunk,
            chunk + 1,
            chunk + delimiter_length,
            3 * chunk + 7,
        ]
        for size in sizes:
            with self.subTest(size=size):
                payload = bytes((i * 7 + size) % 256 for i in range(size))
                body = multipart_body(
                    BOUNDARY, [(file_part("file", "t.wav"), payload)]
                )
                result = self.parse(body, max_file_bytes=8 * chunk)
                self.assertEqual(result.files["file"].size, size)
                self.assertEqual(result.files["file"].path.read_bytes(), payload)

    def test_payload_containing_a_partial_delimiter(self) -> None:
        # Bytes that look like the start of the delimiter but are not it must
        # not terminate the part early.
        marker = b"\r\n--" + BOUNDARY.encode()
        payload = b"before" + marker[:-1] + b"after" + marker[:4] + b"tail"
        body = multipart_body(BOUNDARY, [(file_part("file", "t.wav"), payload)])
        result = self.parse(body)
        self.assertEqual(result.files["file"].path.read_bytes(), payload)


class TestLimits(ParserTestCase):
    def test_file_over_budget_raises_and_removes_the_partial_file(self) -> None:
        payload = b"x" * 5000
        body = multipart_body(BOUNDARY, [(file_part("file", "t.wav"), payload)])
        with self.assertRaises(multipart.PayloadTooLarge):
            self.parse(body, max_file_bytes=1000)
        # A rejected upload must not leave bytes on disk for the sweeper.
        self.assertTrue(self.written)
        self.assertFalse(self.written[0].exists())

    def test_field_over_budget_raises(self) -> None:
        oversized = b"y" * (multipart._MAX_FIELD_BYTES + 10)
        body = multipart_body(BOUNDARY, [(field_part("model"), oversized)])
        with self.assertRaises(multipart.PayloadTooLarge):
            self.parse(body)

    def test_headers_over_budget_raise(self) -> None:
        # A part whose headers never terminate must be bounded rather than
        # buffered until memory runs out.
        marker = BOUNDARY.encode()
        body = (
            b"--" + marker + b"\r\n" + b"X-Filler: " + b"z" * (multipart._MAX_HEADER_BYTES + 64)
        )
        with self.assertRaises(multipart.PayloadTooLarge):
            self.parse(body)


class TestMalformed(ParserTestCase):
    def test_content_type_is_not_multipart(self) -> None:
        with self.assertRaises(multipart.MultipartError):
            multipart.parse(
                io.BytesIO(b""),
                "application/json",
                0,
                max_file_bytes=1024,
                file_destination=self.destination,
            )

    def test_content_type_missing(self) -> None:
        with self.assertRaises(multipart.MultipartError):
            multipart.parse(
                io.BytesIO(b""),
                None,
                0,
                max_file_bytes=1024,
                file_destination=self.destination,
            )

    def test_content_type_without_boundary(self) -> None:
        with self.assertRaises(multipart.MultipartError):
            multipart.parse(
                io.BytesIO(b""),
                "multipart/form-data",
                0,
                max_file_bytes=1024,
                file_destination=self.destination,
            )

    def test_boundary_absent_from_the_body(self) -> None:
        with self.assertRaises(multipart.MultipartError):
            self.parse(b"nothing here resembles a boundary at all")

    def test_body_truncated_before_the_closing_boundary(self) -> None:
        body = multipart_body(BOUNDARY, [(file_part("file", "t.wav"), b"abcdef")])
        with self.assertRaises(multipart.MultipartError):
            self.parse(body[: len(body) - 30])

    def test_part_without_a_name(self) -> None:
        body = multipart_body(
            BOUNDARY, [("Content-Disposition: form-data", b"value")]
        )
        with self.assertRaises(multipart.MultipartError):
            self.parse(body)

    def test_malformed_boundary_terminator(self) -> None:
        marker = BOUNDARY.encode()
        # Neither CRLF (another part) nor "--" (the end) follows the boundary.
        body = b"--" + marker + b"XX\r\n\r\n"
        with self.assertRaises(multipart.MultipartError):
            self.parse(body)


class TestBoundaryExtraction(unittest.TestCase):
    def test_bare_and_quoted_and_trailing_parameters(self) -> None:
        cases = {
            "multipart/form-data; boundary=abc123": b"abc123",
            'multipart/form-data; boundary="abc 123"': b"abc 123",
            "multipart/form-data; boundary=abc123; charset=utf-8": b"abc123",
            "MULTIPART/FORM-DATA; BOUNDARY=abc123": b"abc123",
        }
        for header, expected in cases.items():
            with self.subTest(header=header):
                self.assertEqual(
                    multipart.boundary_from_content_type(header), expected
                )


if __name__ == "__main__":
    unittest.main()
