"""Tests for the pipeline logic that runs no subprocess.

`scrub` decides what a failure tells the user, and `_mix_graph` decides what
ffmpeg is asked to render. Both are pure, and both have been wrong before:
scrub truncated from the front, which hid the only useful line of a Python
traceback everywhere it was read.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from stemapp.config import MODELS, stems_for_model
from stemapp.pipeline import _MESSAGE_LIMIT, _mix_graph, scrub
from support import make_config

FOUR_STEM = ("vocals", "drums", "bass", "other")
SIX_STEM = ("vocals", "drums", "bass", "guitar", "piano", "other")


class ScrubTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cfg = make_config(Path(self._tmp.name))


class TestScrubRedaction(ScrubTestCase):
    def test_data_directory_is_removed(self) -> None:
        message = f"could not read {self.cfg.data_dir}/jobs/abc/source.wav"
        self.assertNotIn(str(self.cfg.data_dir), scrub(message, self.cfg))

    def test_model_directory_is_removed(self) -> None:
        message = f"no such file: {self.cfg.model_dir}/htdemucs_6s.yaml"
        self.assertNotIn(str(self.cfg.model_dir), scrub(message, self.cfg))

    def test_site_packages_paths_are_removed(self) -> None:
        message = (
            'File "/home/ubuntu/stem/.venv/lib/python3.12/site-packages/'
            'librosa/core/notation.py", line 12'
        )
        cleaned = scrub(message, self.cfg)
        self.assertNotIn("site-packages", cleaned)
        self.assertNotIn("/home/ubuntu", cleaned)
        # The useful part -- which module failed -- survives.
        self.assertIn("librosa/core/notation.py", cleaned)

    def test_job_ids_are_collapsed_out_of_paths(self) -> None:
        message = "jobs/AbCdEfGhIjKlMnOpQrStUvWx/stems/vocals.wav is missing"
        cleaned = scrub(message, self.cfg)
        self.assertNotIn("AbCdEfGhIjKlMnOpQrStUvWx", cleaned)
        self.assertIn("stems", cleaned)

    def test_whitespace_is_collapsed(self) -> None:
        self.assertEqual(scrub("a \n\n  b\tc", self.cfg), "a b c")


class TestScrubTruncation(ScrubTestCase):
    """Truncation keeps the end of the message, not the start.

    A Python traceback's only load-bearing line is its last one. Slicing from
    the front cut every deep failure off partway through the second stack
    frame, so the actual exception was invisible in the browser, the API
    response and the on-disk job record alike.
    """

    def _traceback(self, frames: int) -> str:
        lines = ["Traceback (most recent call last):"]
        for index in range(frames):
            lines.append(
                f'  File "/some/long/path/to/module_{index}.py", '
                f"line {index}, in function_{index}"
            )
            lines.append(f"    call_number_{index}()")
        lines.append("RuntimeError: the actual thing that went wrong")
        return "\n".join(lines)

    def test_short_messages_are_untouched(self) -> None:
        self.assertEqual(scrub("plain failure", self.cfg), "plain failure")

    def test_the_final_exception_line_survives_truncation(self) -> None:
        message = self._traceback(200)
        self.assertGreater(len(message), _MESSAGE_LIMIT)
        cleaned = scrub(message, self.cfg)
        self.assertIn("RuntimeError: the actual thing that went wrong", cleaned)
        self.assertTrue(cleaned.endswith("went wrong"))

    def test_truncation_marks_the_cut_and_respects_the_limit(self) -> None:
        cleaned = scrub("z" * 5000, self.cfg)
        self.assertTrue(cleaned.startswith("…"))
        self.assertEqual(len(cleaned), _MESSAGE_LIMIT + 1)

    def test_the_head_is_what_gets_dropped(self) -> None:
        message = "UNIQUEHEADMARKER " + ("filler " * 400) + "UNIQUETAILMARKER"
        cleaned = scrub(message, self.cfg)
        self.assertNotIn("UNIQUEHEADMARKER", cleaned)
        self.assertIn("UNIQUETAILMARKER", cleaned)


class TestMixGraph(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.stem_dir = Path(self._tmp.name) / "stems"

    def test_inputs_follow_the_stem_order_given(self) -> None:
        for stems in (FOUR_STEM, SIX_STEM):
            with self.subTest(count=len(stems)):
                inputs, _ = _mix_graph(self.stem_dir, stems, {})
                paths = [inputs[i] for i in range(1, len(inputs), 2)]
                self.assertEqual(
                    paths, [str(self.stem_dir / f"{n}.wav") for n in stems]
                )
                self.assertEqual(inputs[0::2], ["-i"] * len(stems))

    def test_sum_is_unnormalised_and_counts_every_input(self) -> None:
        # normalize=0 is what makes the sum reproduce the original mix instead
        # of a fraction of it -- and it is what stops the six-stem sum coming
        # out quieter than the four-stem one.
        for stems in (FOUR_STEM, SIX_STEM):
            with self.subTest(count=len(stems)):
                _, filters = _mix_graph(self.stem_dir, stems, {})
                self.assertIn(f"amix=inputs={len(stems)}", filters[-1])
                self.assertIn("normalize=0", filters[-1])
                self.assertTrue(filters[-1].endswith("[summed]"))

    def test_each_stem_gets_its_own_gain_stage(self) -> None:
        _, filters = _mix_graph(self.stem_dir, SIX_STEM, {})
        for index in range(len(SIX_STEM)):
            self.assertEqual(
                filters[index], f"[{index}:a]volume=1.000000[g{index}]"
            )

    def test_missing_gains_default_to_unity(self) -> None:
        _, filters = _mix_graph(self.stem_dir, SIX_STEM, {"vocals": 0.0})
        self.assertEqual(filters[0], "[0:a]volume=0.000000[g0]")
        self.assertEqual(filters[1], "[1:a]volume=1.000000[g1]")

    def test_gains_are_clamped(self) -> None:
        gains = {"vocals": -3.0, "drums": 99.0, "bass": 2.5}
        _, filters = _mix_graph(self.stem_dir, SIX_STEM, gains)
        self.assertEqual(filters[0], "[0:a]volume=0.000000[g0]")
        self.assertEqual(filters[1], "[1:a]volume=4.000000[g1]")
        self.assertEqual(filters[2], "[2:a]volume=2.500000[g2]")

    def test_gains_for_stems_this_model_does_not_have_are_ignored(self) -> None:
        # A four-stem job handed a six-stem balance must render four inputs.
        inputs, filters = _mix_graph(
            self.stem_dir, FOUR_STEM, {"guitar": 0.0, "piano": 0.0}
        )
        self.assertEqual(len(inputs), 2 * len(FOUR_STEM))
        self.assertIn("amix=inputs=4", filters[-1])


class TestModelRegistry(unittest.TestCase):
    def test_labels_and_stems_agree_for_every_model(self) -> None:
        # A mismatch here means separate() would look for a file the separator
        # was never told to write, which only shows up as a failed job.
        for name, spec in MODELS.items():
            with self.subTest(model=name):
                self.assertEqual(
                    sorted(spec.output_names.values()), sorted(spec.stems)
                )
                self.assertEqual(spec.filename, name)
                self.assertEqual(len(set(spec.stems)), len(spec.stems))

    def test_registered_models_cover_both_stem_counts(self) -> None:
        self.assertEqual(stems_for_model("htdemucs.yaml"), FOUR_STEM)
        self.assertEqual(stems_for_model("htdemucs_6s.yaml"), SIX_STEM)

    def test_unknown_model_falls_back_to_the_four_stem_layout(self) -> None:
        # Job records written before the model field existed can only have
        # been separated with the four-stem model.
        self.assertEqual(stems_for_model("gone.yaml"), FOUR_STEM)
        self.assertEqual(stems_for_model(""), FOUR_STEM)


if __name__ == "__main__":
    unittest.main()
