"""Scratch-fixture tests for nexus_heartbeat_archive. Never touches the
live ~/Vaults/loupe-vault/heartbeats/ directory — every test builds its own
tempfile.TemporaryDirectory() heartbeats dir and allowlist."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

MODULE_PATH = Path(__file__).with_name("nexus_heartbeat_archive.py")
_spec = importlib.util.spec_from_file_location("nexus_heartbeat_archive", MODULE_PATH)
sha = importlib.util.module_from_spec(_spec)
sys.modules["nexus_heartbeat_archive"] = sha
_spec.loader.exec_module(sha)

OLD_ENOUGH = 25 * 3600  # seconds; > MIN_AGE_HOURS (24h)
TOO_YOUNG = 1 * 3600


def _write_job(d: Path, job_id: str, *, state="done", kind=None, age_s=OLD_ENOUGH,
                run_id="run-1", extra=None) -> Path:
    payload = {"job": job_id, "state": state, "run_id": run_id, "ts": "2026-07-01T00:00:00Z"}
    if kind is not None:
        payload["kind"] = kind
    if extra:
        payload.update(extra)
    fp = d / f"{job_id}.json"
    fp.write_text(json.dumps(payload))
    mtime = time.time() - age_s
    os.utime(fp, (mtime, mtime))
    return fp


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _allowlist(*job_ids) -> dict:
    return {jid: {"evidence": f"test evidence for {jid}"} for jid in job_ids}


class ArchiveUtilityTests(unittest.TestCase):
    def test_t1_denylist_wins_over_allowlist(self):
        with TemporaryDirectory() as td:
            hb = Path(td)
            _write_job(hb, "temple-file-catalog", state="failed", age_s=OLD_ENOUGH * 100)
            allow = _allowlist("temple-file-catalog")  # allowlisted despite failed state
            decisions = sha.scan(hb, allow, time.time())
            d = decisions[0]
            # hard_denied_id fires first (id contains "temple"); either reason is a refusal.
            self.assertFalse(d["eligible"])
            self.assertIn(d["reason"], ("hard_denied_id", "hard_denied_state:failed"))
            self.assertEqual(_sha256(hb / "temple-file-catalog.json"),
                              hashlib.sha256(hb.joinpath("temple-file-catalog.json").read_bytes()).hexdigest())

    def test_t1b_denylist_wins_non_id_denied_state(self):
        # Same as T1 but with a job id that does NOT trip the id-substring
        # denylist, to isolate the state-based denylist path specifically.
        with TemporaryDirectory() as td:
            hb = Path(td)
            fp = _write_job(hb, "some-legacy-job", state="failed", age_s=OLD_ENOUGH * 100)
            before = _sha256(fp)
            allow = _allowlist("some-legacy-job")
            decisions = sha.scan(hb, allow, time.time())
            d = decisions[0]
            self.assertFalse(d["eligible"])
            self.assertEqual(d["reason"], "hard_denied_state:failed")
            self.assertEqual(_sha256(fp), before)

    def test_t2_not_in_allowlist_zero_moves(self):
        with TemporaryDirectory() as td:
            hb = Path(td)
            ids = ["avif-ingest-96", "worker1-1b-scratch-recluster", "worker-claude-paths"]
            for jid in ids:
                _write_job(hb, jid)
            decisions = sha.scan(hb, {}, time.time())  # empty allowlist
            self.assertEqual(len(decisions), len(ids))
            for d in decisions:
                self.assertFalse(d["eligible"])
                self.assertEqual(d["reason"], "not_in_allowlist")
            manifest = sha.apply_archive(hb, {}, decisions)
            self.assertEqual(manifest["moved"], [])
            self.assertEqual(len(manifest["preserved_or_skipped"]), len(ids))
            for jid in ids:
                self.assertTrue((hb / f"{jid}.json").exists())

    def test_t3_race_mutation_between_scan_and_move(self):
        with TemporaryDirectory() as td:
            hb = Path(td)
            fp = _write_job(hb, "racy-job")
            allow = _allowlist("racy-job")
            decisions = sha.scan(hb, allow, time.time())
            self.assertTrue(decisions[0]["eligible"])
            # Mutate state done -> running after scan, before move (simulates a
            # relaunch under the same id).
            fp.write_text(json.dumps({"job": "racy-job", "state": "running", "run_id": "run-2"}))
            os.utime(fp, (time.time() - OLD_ENOUGH, time.time() - OLD_ENOUGH))
            manifest = sha.apply_archive(hb, allow, decisions)
            self.assertEqual(manifest["moved"], [])
            skipped = manifest["preserved_or_skipped"][0]
            self.assertEqual(skipped["reason"], "race_detected_at_move_time")
            self.assertTrue(fp.exists())
            self.assertEqual(json.loads(fp.read_text())["state"], "running")

    def test_t4_os_replace_failure_is_atomic(self):
        with TemporaryDirectory() as td:
            hb = Path(td)
            fp = _write_job(hb, "atomic-job")
            before_hash = _sha256(fp)
            allow = _allowlist("atomic-job")
            decisions = sha.scan(hb, allow, time.time())

            real_replace = os.replace

            def boom(src, dst):
                raise OSError("simulated failure")

            os.replace = boom
            try:
                manifest = sha.apply_archive(hb, allow, decisions)
            finally:
                os.replace = real_replace

            self.assertEqual(manifest["moved"], [])
            self.assertTrue(fp.exists())
            self.assertEqual(_sha256(fp), before_hash)
            skipped = manifest["preserved_or_skipped"][0]
            self.assertTrue(skipped["reason"].startswith("move_failed"))
            # No orphan temp file left behind by the tool itself.
            leftovers = [p for p in hb.rglob("*") if p.is_file() and p != fp
                         and p.name != "manifest.json"]
            self.assertEqual(leftovers, [])

    def test_t5_collision_refusal(self):
        with TemporaryDirectory() as td:
            hb = Path(td)
            fp = _write_job(hb, "dup-job")
            before_src_hash = _sha256(fp)
            allow = _allowlist("dup-job")
            FIXED_NOW = 2_000_000_000.0  # frozen "now" so the archive dir name is deterministic
            decisions = sha.scan(hb, allow, FIXED_NOW)
            run_ts = sha._utc_stamp(FIXED_NOW)
            dest_dir = hb / "archive" / f"{sha.ARCHIVE_PREFIX}{run_ts}"
            dest_dir.mkdir(parents=True)
            dest = dest_dir / "dup-job.json"
            dest.write_text('{"different": "bytes"}')
            before_dest_hash = _sha256(dest)

            manifest = sha.apply_archive(hb, allow, decisions, now_fn=lambda: FIXED_NOW)
            self.assertEqual(manifest["moved"], [])
            skipped = manifest["preserved_or_skipped"][0]
            self.assertEqual(skipped["reason"], "collision")
            self.assertTrue(fp.exists())
            self.assertEqual(_sha256(fp), before_src_hash)
            self.assertEqual(_sha256(dest), before_dest_hash)

    def test_t6_dry_run_is_truly_inert(self):
        with TemporaryDirectory() as td:
            hb = Path(td)
            ids = ["job-a", "job-b"]
            for jid in ids:
                _write_job(hb, jid)
            allow = _allowlist(*ids)
            before_listing = sorted(p.name for p in hb.iterdir())
            before_hashes = {jid: _sha256(hb / f"{jid}.json") for jid in ids}

            decisions = sha.scan(hb, allow, time.time())
            self.assertTrue(all(d["eligible"] for d in decisions))
            # scan() alone (what the dry-run CLI path calls) must never write.
            after_listing = sorted(p.name for p in hb.iterdir())
            after_hashes = {jid: _sha256(hb / f"{jid}.json") for jid in ids}
            self.assertEqual(before_listing, after_listing)
            self.assertEqual(before_hashes, after_hashes)
            self.assertFalse((hb / "archive").exists())

    def test_t7_non_recursive_scan_boundary(self):
        with TemporaryDirectory() as td:
            hb = Path(td)
            _write_job(hb, "top-level-job")
            nested_archive = hb / "archive"
            nested_archive.mkdir()
            _write_job(nested_archive, "top-level-job")  # same id, nested — must be ignored
            nested_inline = hb / "inline"
            nested_inline.mkdir()
            _write_job(nested_inline, "top-level-job")

            allow = _allowlist("top-level-job")
            decisions = sha.scan(hb, allow, time.time())
            self.assertEqual(len(decisions), 1)  # only the top-level file was scanned
            self.assertEqual(decisions[0]["original_path"], str(hb / "top-level-job.json"))

    def test_t8_restore_round_trip_hash_match(self):
        with TemporaryDirectory() as td:
            hb = Path(td)
            fp = _write_job(hb, "restore-job")
            original_hash = _sha256(fp)
            allow = _allowlist("restore-job")
            decisions = sha.scan(hb, allow, time.time())
            manifest = sha.apply_archive(hb, allow, decisions)
            self.assertEqual(len(manifest["moved"]), 1)
            moved = manifest["moved"][0]
            self.assertEqual(moved["sha256"], original_hash)
            self.assertFalse(fp.exists())
            dest = Path(moved["destination_path"])
            self.assertTrue(dest.exists())
            self.assertEqual(_sha256(dest), original_hash)

            # Execute the manifest's literal restore command.
            restore_cmd = moved["restore_cmd"]
            self.assertTrue(restore_cmd.startswith("cp -p "))
            os.system(restore_cmd)
            self.assertTrue(fp.exists())
            self.assertEqual(_sha256(fp), original_hash)

    def test_t9_minimum_age_gate(self):
        with TemporaryDirectory() as td:
            hb = Path(td)
            _write_job(hb, "young-job", age_s=TOO_YOUNG)
            _write_job(hb, "old-job", age_s=OLD_ENOUGH)
            allow = _allowlist("young-job", "old-job")
            decisions = {d["job"]: d for d in sha.scan(hb, allow, time.time())}
            self.assertFalse(decisions["young-job"]["eligible"])
            self.assertTrue(decisions["young-job"]["reason"].startswith("too_young"))
            self.assertTrue(decisions["old-job"]["eligible"])

    def test_t10_kind_validation(self):
        with TemporaryDirectory() as td:
            hb = Path(td)
            _write_job(hb, "no-kind-job", kind=None)
            _write_job(hb, "job-kind-job", kind="job")
            _write_job(hb, "watch-kind-job", kind="watch")
            _write_job(hb, "guard-kind-job", kind="guard")
            allow = _allowlist("no-kind-job", "job-kind-job", "watch-kind-job", "guard-kind-job")
            decisions = {d["job"]: d for d in sha.scan(hb, allow, time.time())}
            self.assertTrue(decisions["no-kind-job"]["eligible"])
            self.assertTrue(decisions["job-kind-job"]["eligible"])
            self.assertFalse(decisions["watch-kind-job"]["eligible"])
            self.assertEqual(decisions["watch-kind-job"]["reason"], "unrecognized_kind:watch")
            self.assertFalse(decisions["guard-kind-job"]["eligible"])

    def test_hard_denied_ids_regardless_of_allowlist(self):
        with TemporaryDirectory() as td:
            hb = Path(td)
            denied_ids = ["host-charlie", "thermal-guard-charlie", "gallery-library-scan",
                          "nas3-photo-mirror", "temple-file-catalog"]
            for jid in denied_ids:
                _write_job(hb, jid)  # state=done, would otherwise be eligible
            allow = _allowlist(*denied_ids)  # mistakenly allowlisted
            decisions = sha.scan(hb, allow, time.time())
            for d in decisions:
                self.assertFalse(d["eligible"], d["job"])
                self.assertEqual(d["reason"], "hard_denied_id")

    def test_wrong_shape_not_a_job_record(self):
        with TemporaryDirectory() as td:
            hb = Path(td)
            fp = hb / "sidecar-shaped.json"
            fp.write_text(json.dumps({"job": "sidecar-shaped", "state": "done"}))  # no run_id
            mtime = time.time() - OLD_ENOUGH
            os.utime(fp, (mtime, mtime))
            allow = _allowlist("sidecar-shaped")
            decisions = sha.scan(hb, allow, time.time())
            self.assertFalse(decisions[0]["eligible"])
            self.assertEqual(decisions[0]["reason"], "wrong_shape_not_a_job_record")

    def test_apply_writes_manifest_with_full_audit_trail(self):
        with TemporaryDirectory() as td:
            hb = Path(td)
            _write_job(hb, "eligible-job")
            _write_job(hb, "not-allowlisted-job")
            _write_job(hb, "temple-file-catalog", state="failed", age_s=OLD_ENOUGH * 50)
            allow = _allowlist("eligible-job", "temple-file-catalog")
            decisions = sha.scan(hb, allow, time.time())
            manifest = sha.apply_archive(hb, allow, decisions)
            self.assertEqual(len(manifest["moved"]), 1)
            self.assertEqual(manifest["moved"][0]["job"], "eligible-job")
            self.assertEqual(len(manifest["preserved_or_skipped"]), 2)
            reasons = {d["job"]: d["reason"] for d in manifest["preserved_or_skipped"]}
            self.assertEqual(reasons["not-allowlisted-job"], "not_in_allowlist")
            self.assertEqual(reasons["temple-file-catalog"], "hard_denied_id")
            manifest_path = Path(manifest["archive_dir"]) / "manifest.json"
            self.assertTrue(manifest_path.exists())
            on_disk = json.loads(manifest_path.read_text())
            self.assertEqual(on_disk["moved"][0]["job"], "eligible-job")

    def test_no_deletion_code_path(self):
        source = MODULE_PATH.read_text()
        for token in ("os.remove", "os.unlink", "shutil.rmtree", "Path.unlink"):
            self.assertNotIn(token, source, f"found forbidden deletion call: {token}")


if __name__ == "__main__":
    unittest.main()
