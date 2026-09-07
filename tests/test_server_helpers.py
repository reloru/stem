"""Tests for the request-handling helpers that are pure functions.

`_parse_range` decides what bytes a ranged download returns, and
`safe_base_name` produces the string that ends up inside a Content-Disposition
header and in derived filenames. Both take attacker-influenced input.
"""

from __future__ import annotations

import unittest

from stemapp.server import _parse_range, safe_base_name


class TestParseRange(unittest.TestCase):
    SIZE = 1000

    def test_explicit_range(self) -> None:
        self.assertEqual(_parse_range("bytes=0-99", self.SIZE), (0, 99))
        self.assertEqual(_parse_range("bytes=200-399", self.SIZE), (200, 399))

    def test_open_ended_range_runs_to_the_last_byte(self) -> None:
        self.assertEqual(_parse_range("bytes=100-", self.SIZE), (100, 999))
        self.assertEqual(_parse_range("bytes=0-", self.SIZE), (0, 999))

    def test_suffix_range_counts_back_from_the_end(self) -> None:
        self.assertEqual(_parse_range("bytes=-100", self.SIZE), (900, 999))

    def test_suffix_longer_than_the_file_clamps_to_the_whole_file(self) -> None:
        self.assertEqual(_parse_range("bytes=-5000", self.SIZE), (0, 999))

    def test_end_past_the_last_byte_is_clamped(self) -> None:
        self.assertEqual(_parse_range("bytes=500-99999", self.SIZE), (500, 999))

    def test_surrounding_whitespace_is_tolerated(self) -> None:
        self.assertEqual(_parse_range("  bytes=0-9  ", self.SIZE), (0, 9))

    def test_unsatisfiable_and_malformed_ranges_return_none(self) -> None:
        for header in [
            "bytes=1000-",       # start at EOF
            "bytes=2000-3000",   # start past EOF
            "bytes=500-100",     # end before start
            "bytes=-0",          # zero-length suffix
            "bytes=-",           # neither side given
            "bytes=",
            "bytes=abc-def",
            "items=0-99",        # unit is not bytes
            "bytes=0-10,20-30",  # multi-range is not supported
            "",
            "garbage",
        ]:
            with self.subTest(header=header):
                self.assertIsNone(_parse_range(header, self.SIZE))


class TestSafeBaseName(unittest.TestCase):
    def test_strips_the_extension(self) -> None:
        self.assertEqual(safe_base_name("song.mp3"), "song")
        self.assertEqual(safe_base_name("song.tar.gz"), "song.tar")
        self.assertEqual(safe_base_name("noextension"), "noextension")

    def test_discards_any_directory_component(self) -> None:
        # Whatever the client claims, only the final component survives, so a
        # traversal attempt cannot reach the path this name is spliced into.
        self.assertEqual(safe_base_name("../../etc/passwd"), "passwd")
        self.assertEqual(safe_base_name("/absolute/path/track.wav"), "track")
        self.assertEqual(safe_base_name(r"C:\Users\someone\track.wav"), "track")
        self.assertEqual(safe_base_name("../.."), "track")

    def test_removes_characters_that_would_break_a_header(self) -> None:
        # The result is interpolated into Content-Disposition between double
        # quotes; a quote or a newline surviving would let the client inject
        # a header.
        for hostile in ['a"b', "a\r\nb", "a\nb", "a\rb", 'x";y']:
            with self.subTest(hostile=hostile):
                cleaned = safe_base_name(hostile)
                self.assertNotIn('"', cleaned)
                self.assertNotIn("\r", cleaned)
                self.assertNotIn("\n", cleaned)

    def test_collapses_runs_of_whitespace(self) -> None:
        self.assertEqual(safe_base_name("a    b.wav"), "a b")

    def test_empty_and_unusable_names_fall_back(self) -> None:
        for name in ["", ".", "...", ".mp3", "___", "   ", "!!!.wav"]:
            with self.subTest(name=name):
                self.assertEqual(safe_base_name(name), "track")

    def test_length_is_capped(self) -> None:
        self.assertEqual(len(safe_base_name("a" * 500 + ".wav")), 64)

    def test_ordinary_names_are_left_alone(self) -> None:
        self.assertEqual(
            safe_base_name("Nightcall - Kavinsky.flac"), "Nightcall - Kavinsky"
        )


if __name__ == "__main__":
    unittest.main()
