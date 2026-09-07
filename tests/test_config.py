"""Tests for environment parsing and model resolution.

Everything here is read once at startup, and a bad value has to be fatal then
rather than surfacing as a confusing failure mid-job.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from stemapp.config import DEFAULT_MODEL, MODELS, Config, ConfigError, spec_for_model

VALID_KEY = "a-key-long-enough"


def env(**overrides):
    """Patch the environment down to just PATH plus the given STEM_* values."""
    base = {"PATH": os.environ.get("PATH", "")}
    base.update(overrides)
    return mock.patch.dict(os.environ, base, clear=True)


class ConfigTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)


class TestAccessKey(ConfigTestCase):
    def test_missing_key_is_fatal(self) -> None:
        with env(), self.assertRaises(ConfigError):
            Config.from_env(self.root)

    def test_short_key_is_fatal(self) -> None:
        with env(STEM_ACCESS_KEY="tooshort"), self.assertRaises(ConfigError):
            Config.from_env(self.root)

    def test_key_of_exactly_twelve_characters_is_accepted(self) -> None:
        with env(STEM_ACCESS_KEY="a" * 12):
            self.assertEqual(Config.from_env(self.root).access_key, "a" * 12)

    def test_open_access_removes_the_requirement(self) -> None:
        with env(STEM_ALLOW_OPEN="1"):
            cfg = Config.from_env(self.root)
            self.assertTrue(cfg.open_access)
            self.assertEqual(cfg.access_key, "")

    def test_open_access_accepts_several_spellings(self) -> None:
        for value in ["1", "true", "TRUE", "yes", "on"]:
            with self.subTest(value=value), env(STEM_ALLOW_OPEN=value):
                self.assertTrue(Config.from_env(self.root).open_access)

    def test_other_values_do_not_enable_open_access(self) -> None:
        for value in ["0", "false", "no", "off", ""]:
            with self.subTest(value=value), env(STEM_ALLOW_OPEN=value):
                with self.assertRaises(ConfigError):
                    Config.from_env(self.root)


class TestIntegerSettings(ConfigTestCase):
    def test_non_numeric_value_is_fatal(self) -> None:
        with env(STEM_ACCESS_KEY=VALID_KEY, STEM_PORT="eighty"):
            with self.assertRaises(ConfigError):
                Config.from_env(self.root)

    def test_value_below_the_minimum_is_fatal(self) -> None:
        with env(STEM_ACCESS_KEY=VALID_KEY, STEM_WORKERS="0"):
            with self.assertRaises(ConfigError):
                Config.from_env(self.root)

    def test_empty_value_falls_back_to_the_default(self) -> None:
        with env(STEM_ACCESS_KEY=VALID_KEY, STEM_PORT=""):
            self.assertEqual(Config.from_env(self.root).port, 8080)

    def test_units_are_applied(self) -> None:
        with env(
            STEM_ACCESS_KEY=VALID_KEY,
            STEM_MAX_UPLOAD_MB="12",
            STEM_JOB_TTL_HOURS="3",
        ):
            cfg = Config.from_env(self.root)
            self.assertEqual(cfg.max_upload_bytes, 12 * 1024 * 1024)
            self.assertEqual(cfg.job_ttl_seconds, 3 * 3600)


class TestDefaults(ConfigTestCase):
    def test_documented_defaults(self) -> None:
        with env(STEM_ACCESS_KEY=VALID_KEY):
            cfg = Config.from_env(self.root)
            self.assertEqual(cfg.host, "127.0.0.1")
            self.assertEqual(cfg.port, 8080)
            self.assertEqual(cfg.max_duration_seconds, 300)
            self.assertEqual(cfg.worker_count, 1)
            self.assertEqual(cfg.model_filename, DEFAULT_MODEL)
            # Previews are mono, so the default bitrate spends more bits per
            # channel than the 192k stereo default it replaced.
            self.assertEqual(cfg.preview_bitrate, "128k")

    def test_derived_directories(self) -> None:
        with env(STEM_ACCESS_KEY=VALID_KEY):
            cfg = Config.from_env(self.root)
            self.assertEqual(cfg.data_dir, self.root / "data")
            self.assertEqual(cfg.jobs_dir, self.root / "data" / "jobs")
            self.assertEqual(cfg.model_dir, self.root / "data" / "models")
            # Kept inside the data directory because it is the only path the
            # systemd unit grants write access to; numba raises rather than
            # degrading when it has nowhere to cache.
            self.assertEqual(
                cfg.numba_cache_dir, self.root / "data" / "numba-cache"
            )


class TestModelSelection(ConfigTestCase):
    def test_unknown_default_model_is_fatal_at_startup(self) -> None:
        with env(STEM_ACCESS_KEY=VALID_KEY, STEM_MODEL="mystery.yaml"):
            with self.assertRaises(ConfigError):
                Config.from_env(self.root)

    def test_every_registered_model_is_accepted_as_the_default(self) -> None:
        for name, spec in MODELS.items():
            with self.subTest(model=name):
                with env(STEM_ACCESS_KEY=VALID_KEY, STEM_MODEL=name):
                    cfg = Config.from_env(self.root)
                    self.assertEqual(cfg.default_model.stems, spec.stems)

    def test_resolve_model_falls_back_to_the_default_when_unasked(self) -> None:
        with env(STEM_ACCESS_KEY=VALID_KEY):
            cfg = Config.from_env(self.root)
            for requested in (None, ""):
                with self.subTest(requested=requested):
                    self.assertEqual(
                        cfg.resolve_model(requested).filename, DEFAULT_MODEL
                    )

    def test_resolve_model_honours_a_non_default_choice(self) -> None:
        with env(STEM_ACCESS_KEY=VALID_KEY):
            cfg = Config.from_env(self.root)
            spec = cfg.resolve_model("htdemucs_6s.yaml")
            self.assertEqual(len(spec.stems), 6)
            self.assertIn("guitar", spec.stems)
            self.assertIn("piano", spec.stems)

    def test_resolve_model_rejects_anything_unregistered(self) -> None:
        with env(STEM_ACCESS_KEY=VALID_KEY):
            cfg = Config.from_env(self.root)
            for requested in ["nope.yaml", "../../etc/passwd", "htdemucs"]:
                with self.subTest(requested=requested):
                    with self.assertRaises(ConfigError):
                        cfg.resolve_model(requested)

    def test_spec_for_model_never_raises(self) -> None:
        # Used on the job-reading path, where a record may name a model that
        # has since been removed from the registry.
        self.assertEqual(spec_for_model("removed.yaml").filename, DEFAULT_MODEL)


class TestExecutableResolution(ConfigTestCase):
    def test_explicit_paths_win_over_path_lookup(self) -> None:
        with env(
            STEM_ACCESS_KEY=VALID_KEY,
            STEM_FFMPEG="/opt/custom/ffmpeg",
            STEM_SEPARATOR_BIN="/opt/custom/audio-separator",
        ):
            cfg = Config.from_env(self.root)
            self.assertEqual(cfg.ffmpeg_bin, "/opt/custom/ffmpeg")
            self.assertEqual(cfg.separator_bin, "/opt/custom/audio-separator")

    def test_missing_executables_are_reported_not_raised(self) -> None:
        with env(
            STEM_ACCESS_KEY=VALID_KEY,
            STEM_FFMPEG="/nonexistent/ffmpeg",
            STEM_FFPROBE="/nonexistent/ffprobe",
            STEM_SEPARATOR_BIN="/nonexistent/audio-separator",
        ):
            missing = Config.from_env(self.root).missing_executables()
            self.assertEqual(len(missing), 3)


if __name__ == "__main__":
    unittest.main()
