"""Tests for the job registry: identity, on-disk layout, expiry, restart.

The store is the only thing that turns a client-supplied id into a filesystem
path, and the only thing that decides what happens to work in flight when the
process dies.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from stemapp.config import DEFAULT_MODEL
from stemapp.jobs import (
    ACTIVE_STATES,
    STATE_DONE,
    STATE_ERROR,
    STATE_SEPARATING,
    Job,
    JobStore,
    is_valid_job_id,
    new_job_id,
)

TTL = 3600


class StoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.jobs_dir = Path(self._tmp.name) / "jobs"
        self.store = JobStore(self.jobs_dir, TTL)

    def record(self, job_id: str) -> dict:
        return json.loads((self.jobs_dir / job_id / "job.json").read_text("utf-8"))


class TestJobIds(unittest.TestCase):
    def test_generated_ids_are_valid_and_distinct(self) -> None:
        ids = {new_job_id() for _ in range(200)}
        self.assertEqual(len(ids), 200)
        for job_id in ids:
            self.assertTrue(is_valid_job_id(job_id))

    def test_ids_that_could_escape_the_jobs_directory_are_rejected(self) -> None:
        for candidate in [
            "..",
            "../secrets",
            "a/b",
            "a\\b",
            "with space",
            "with.dot",
            "",
            "short",
            "x" * 65,
            "abcdefghijklmno",  # 15 characters, one below the minimum
        ]:
            with self.subTest(candidate=candidate):
                self.assertFalse(is_valid_job_id(candidate))

    def test_minimum_and_maximum_lengths_are_inclusive(self) -> None:
        self.assertTrue(is_valid_job_id("a" * 16))
        self.assertTrue(is_valid_job_id("a" * 64))


class TestPaths(StoreTestCase):
    def test_invalid_ids_never_produce_a_path(self) -> None:
        for candidate in ["../escape", "a/b", "short"]:
            with self.subTest(candidate=candidate):
                with self.assertRaises(ValueError):
                    self.store.job_dir(candidate)

    def test_stem_paths_cover_every_registered_stem(self) -> None:
        job = self.store.create("track", "htdemucs_6s.yaml")
        for stem in ("vocals", "drums", "bass", "guitar", "piano", "other"):
            with self.subTest(stem=stem):
                self.assertEqual(
                    self.store.stem_path(job.id, stem).name, f"{stem}.wav"
                )
                self.assertEqual(
                    self.store.preview_path(job.id, stem).name, f"{stem}.mp3"
                )

    def test_unknown_stem_names_are_rejected(self) -> None:
        job = self.store.create("track")
        for stem in ["../../etc/passwd", "synth", "", "Vocals"]:
            with self.subTest(stem=stem):
                with self.assertRaises(ValueError):
                    self.store.stem_path(job.id, stem)

    def test_mix_ids_are_validated_like_job_ids(self) -> None:
        job = self.store.create("track")
        self.assertEqual(
            self.store.mix_path(job.id, "a" * 24, "wav").name, f"{'a' * 24}.wav"
        )
        with self.assertRaises(ValueError):
            self.store.mix_path(job.id, "../escape", "wav")


class TestLifecycle(StoreTestCase):
    def test_create_writes_a_directory_and_a_record(self) -> None:
        job = self.store.create("My Track", "htdemucs_6s.yaml")
        self.assertTrue((self.jobs_dir / job.id).is_dir())
        record = self.record(job.id)
        self.assertEqual(record["original_name"], "My Track")
        self.assertEqual(record["model"], "htdemucs_6s.yaml")

    def test_create_defaults_to_the_four_stem_model(self) -> None:
        self.assertEqual(self.store.create("t").model, DEFAULT_MODEL)

    def test_get_rejects_a_malformed_id_without_raising(self) -> None:
        self.assertIsNone(self.store.get("../escape"))
        self.assertIsNone(self.store.get("nosuchjobidentifier12345"))

    def test_update_persists_and_rejects_unknown_fields(self) -> None:
        job = self.store.create("t")
        self.store.update(job.id, state=STATE_DONE, progress=100.0)
        self.assertEqual(self.record(job.id)["state"], STATE_DONE)
        with self.assertRaises(AttributeError):
            self.store.update(job.id, nonexistent_field=1)

    def test_update_on_a_missing_job_returns_none(self) -> None:
        self.assertIsNone(self.store.update("a" * 24, state=STATE_DONE))

    def test_delete_removes_the_directory_and_the_record(self) -> None:
        job = self.store.create("t")
        (self.jobs_dir / job.id / "input.wav").write_bytes(b"data")
        self.assertTrue(self.store.delete(job.id))
        self.assertFalse((self.jobs_dir / job.id).exists())
        self.assertIsNone(self.store.get(job.id))
        self.assertFalse(self.store.delete(job.id))


class TestRestart(StoreTestCase):
    def test_finished_jobs_survive_a_reload(self) -> None:
        job = self.store.create("t", "htdemucs_6s.yaml")
        self.store.update(job.id, state=STATE_DONE, stems=["vocals"])

        reloaded = JobStore(self.jobs_dir, TTL)
        reloaded.load_from_disk()
        restored = reloaded.get(job.id)
        self.assertIsNotNone(restored)
        self.assertEqual(restored.state, STATE_DONE)
        self.assertEqual(restored.model, "htdemucs_6s.yaml")

    def test_jobs_in_flight_are_marked_failed_rather_than_left_spinning(self) -> None:
        # The work queue does not survive a restart, so an active job is dead;
        # a browser polling it would otherwise spin forever.
        for state in sorted(ACTIVE_STATES):
            with self.subTest(state=state):
                store = JobStore(self.jobs_dir, TTL)
                job = store.create("t")
                store.update(job.id, state=state)

                reloaded = JobStore(self.jobs_dir, TTL)
                reloaded.load_from_disk()
                restored = reloaded.get(job.id)
                self.assertEqual(restored.state, STATE_ERROR)
                self.assertIn("restarted", restored.error)
                # The change is persisted, not just held in memory.
                self.assertEqual(self.record(job.id)["state"], STATE_ERROR)

    def test_records_written_before_the_model_field_existed_still_load(self) -> None:
        # The deployed box has job records on disk that predate per-job model
        # selection. Dropping them on upgrade would orphan their files until
        # the TTL expired, and 404 any link already handed out.
        job_id = new_job_id()
        directory = self.jobs_dir / job_id
        directory.mkdir(parents=True)
        legacy = {
            "id": job_id,
            "created_at": time.time(),
            "updated_at": time.time(),
            "state": STATE_DONE,
            "original_name": "Old Track",
            "input_bytes": 1234,
            "duration_seconds": 200.0,
            "sample_rate": 44100,
            "channels": 2,
            "progress": 100.0,
            "separation_pass": 2,
            "started_at": time.time(),
            "finished_at": time.time(),
            "stems": ["vocals", "drums", "bass", "other"],
            "error": None,
        }
        self.assertNotIn("model", legacy)
        (directory / "job.json").write_text(json.dumps(legacy), "utf-8")

        store = JobStore(self.jobs_dir, TTL)
        store.load_from_disk()
        restored = store.get(job_id)
        self.assertIsNotNone(restored)
        self.assertEqual(restored.model, DEFAULT_MODEL)
        self.assertEqual(restored.original_name, "Old Track")

    def test_unparseable_records_are_skipped_not_fatal(self) -> None:
        good = self.store.create("good")
        broken = self.jobs_dir / new_job_id()
        broken.mkdir(parents=True)
        (broken / "job.json").write_text("{not json", "utf-8")

        store = JobStore(self.jobs_dir, TTL)
        store.load_from_disk()
        self.assertIsNotNone(store.get(good.id))


class TestExpiry(StoreTestCase):
    def _age(self, job_id: str, seconds: float) -> None:
        job = self.store.get(job_id)
        self.store.update(job_id, created_at=job.created_at - seconds)

    def test_expired_jobs_are_swept(self) -> None:
        fresh = self.store.create("fresh")
        stale = self.store.create("stale")
        self._age(stale.id, TTL + 60)

        self.assertEqual(self.store.expired_ids(), [stale.id])
        self.assertEqual(self.store.sweep(), 1)
        self.assertIsNone(self.store.get(stale.id))
        self.assertIsNotNone(self.store.get(fresh.id))
        self.assertFalse((self.jobs_dir / stale.id).exists())

    def test_orphaned_directories_are_swept_once_old_enough(self) -> None:
        # A crashed create, or a job.json that failed to parse, still occupies
        # roughly 450 MB per job until something removes it.
        orphan = self.jobs_dir / new_job_id()
        orphan.mkdir(parents=True)
        (orphan / "stems").mkdir()
        old = time.time() - (TTL + 600)
        os.utime(orphan, (old, old))

        self.assertEqual(self.store.sweep(), 1)
        self.assertFalse(orphan.exists())

    def test_recent_orphans_are_left_alone(self) -> None:
        # A directory created moments ago may be an upload in progress.
        orphan = self.jobs_dir / new_job_id()
        orphan.mkdir(parents=True)
        self.assertEqual(self.store.sweep(), 0)
        self.assertTrue(orphan.exists())

    def test_sweeping_an_absent_directory_is_harmless(self) -> None:
        self.assertEqual(JobStore(self.jobs_dir / "gone", TTL).sweep(), 0)


class TestPublicView(StoreTestCase):
    def test_elapsed_and_expiry_are_derived(self) -> None:
        job = Job(
            id="a" * 24,
            created_at=1000.0,
            updated_at=1000.0,
            started_at=1000.0,
            finished_at=1090.0,
            state=STATE_DONE,
        )
        payload = job.to_public_dict(TTL)
        self.assertEqual(payload["expires_at"], 1000.0 + TTL)
        self.assertEqual(payload["elapsed_seconds"], 90.0)
        self.assertEqual(payload["stage"], "Ready")
        self.assertEqual(payload["model"], DEFAULT_MODEL)

    def test_elapsed_is_absent_before_work_starts(self) -> None:
        job = Job(id="a" * 24, created_at=1000.0, updated_at=1000.0)
        self.assertIsNone(job.to_public_dict(TTL)["elapsed_seconds"])

    def test_every_state_has_a_readable_stage_label(self) -> None:
        for state in sorted(ACTIVE_STATES | {STATE_DONE, STATE_ERROR}):
            with self.subTest(state=state):
                job = Job(
                    id="a" * 24, created_at=0.0, updated_at=0.0, state=state
                )
                stage = job.to_public_dict(TTL)["stage"]
                self.assertNotEqual(stage, state)
                self.assertTrue(stage)

    def test_separating_state_carries_the_pass_number(self) -> None:
        job = Job(
            id="a" * 24,
            created_at=0.0,
            updated_at=0.0,
            state=STATE_SEPARATING,
            separation_pass=2,
        )
        self.assertEqual(job.to_public_dict(TTL)["separation_pass"], 2)


if __name__ == "__main__":
    unittest.main()
