"""Read-only inventory regressions; never starts inference."""
import hashlib
import io
import json
from pathlib import Path
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import inventory as module
from benchmark import main, parser
from core import BenchmarkError


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="benchmark-inventory-")
        self.base = Path(self.temp.name).resolve()

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def write(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def batch(self, relative, slots=1, **extra):
        root = self.base / relative
        protocol = {"title": relative, "project": {"name": "fixture"}, "case_id": relative,
                    "created_at": "2026-10-28T00:00:00Z", "parent": None,
                    "schedule": [{"id": f"{i:04d}", "model": "test-model", "effort": "max",
                                  "service_tier": "default" if i % 2 else "priority", "repetition": i}
                                 for i in range(1, slots + 1)], **extra}
        self.write(root / "protocol.json", protocol)
        return root, protocol

    def receipt(self, root, protocol, slot=0, attempt=1, status="completed", valid=True, **extra):
        planned = protocol["schedule"][slot]
        record = {"slot_id": planned["id"], "model": planned["model"], "effort": planned["effort"],
                  "service_tier": planned["service_tier"], "status": status, "valid": valid,
                  "attempt": attempt, **extra}
        path = root / "runs" / planned["id"] / f"attempt-{attempt:03d}" / "result.json"
        self.write(path, record)
        return path

    @staticmethod
    def snapshot(root):
        return {p.relative_to(root).as_posix(): (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
                for p in root.rglob("*") if p.is_file()}

    def test_nested_history_retains_shape_and_prunes_copies(self):
        for name in ("top", "study/preload", "study/module", "top/input/copied", "sources/copied",
                     "answers/copied", "capability/copied", "_staging/preparation", "comparison/sources/copied"):
            self.batch(name)
        self.write(self.base / "comparison/comparison-data.json", {})
        rows = module.history(self.base)
        self.assertEqual([row["title"] for row in rows], ["study/module", "study/preload", "top"])
        self.assertTrue(all(set(row) == set(module.HISTORY_FIELDS) for row in rows))
        self.assertEqual(len(module.inventory(run_dirs=[self.base / "sources/copied"])["runs"]), 1)

    def test_retry_unfinished_and_capability_are_distinct(self):
        root, protocol = self.batch("study/preload", slots=4)
        self.receipt(root, protocol, attempt=1, status="failed", valid=False)
        self.receipt(root, protocol, attempt=2)
        self.receipt(root, protocol, attempt=3, status="failed", valid=False)
        self.receipt(root, protocol, slot=1, status="interrupted", valid=False)
        (root / "runs/0003/attempt-001").mkdir(parents=True)
        self.write(root / "inflight.json", {"slot_id": "0003", "pid": 1234})
        self.write(root / "runner-lock.json", {"pid": 1234})
        self.write(root / "capability/runs/0004/attempt-001/result.json", {"not": "formal"})
        self.write(root / "grading/seal.json", {"not": "read"})
        (root / "report.html").write_text("not read", encoding="utf-8")
        run = module.inventory(root=self.base)["runs"][0]
        self.assertEqual(run["counts"], {"planned": 4, "started_slots": 3, "valid_completed": 1,
                                        "failed_attempts": 2, "interrupted_attempts": 1,
                                        "invalid_completed_attempts": 0, "not_started": 1})
        self.assertEqual(len(run["conditions"]), 2)
        self.assertTrue(run["sealed_present"] and run["report_present"])
        self.assertTrue(run["inflight_present"] and run["runner_lock_present"])
        self.assertEqual(run["process_liveness"], "not_checked")

    def test_read_only_without_answers_scores_or_quality_output(self):
        root, protocol = self.batch("run")
        self.receipt(root, protocol, agentic={"text": "DO_NOT_RETURN_FINAL_TEXT"},
                     arbitrary_secret="DO_NOT_RETURN_RECEIPT_FIELD")
        for name in ("runs/0001/attempt-001/answer.txt", "grading/scores.json", "grading/private-mapping.json"):
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_text("DO_NOT_READ", encoding="utf-8")
        before, original = self.snapshot(self.base), Path.read_text

        def guarded(path, *args, **kwargs):
            self.assertNotIn(path.name, {"answer.txt", "scores.json", "private-mapping.json"})
            return original(path, *args, **kwargs)

        with patch.object(Path, "read_text", guarded), patch("core.write", side_effect=AssertionError("write")), \
                patch("runtime.Server", side_effect=AssertionError("inference")):
            result = module.inventory(run_dirs=[root, root / "."])
        self.assertEqual(len(result["runs"]), 1)
        self.assertTrue(result["read_only"] and result["no_new_inference"])
        self.assertNotIn("DO_NOT_RETURN", json.dumps(result))
        self.assertNotIn("DO_NOT_READ", json.dumps(result))
        self.assertEqual(self.snapshot(self.base), before)

    def test_regrades_keep_source_relation_without_automatic_choice(self):
        original, _ = self.batch("original")
        revised, _ = self.batch("revised", offline_regrade=True, evaluation_manifest_sha256="hash")
        self.write(revised / "evaluation/manifest.json", {"source_root": str(original), "source_batch": "original",
                    "final_answer_repair": {"method": "fixture"}, "private_notes": "DO_NOT_RETURN"})
        result = module.inventory(self.base)
        self.assertEqual(len(result["runs"]), 2)
        relation = next(r for r in result["runs"] if r["run_dir"] == str(revised))["relation"]
        self.assertTrue(relation["offline_regrade"] and relation["final_answer_repair"])
        self.assertEqual(relation["source_root"], str(original))
        self.assertNotIn("DO_NOT_RETURN", json.dumps(result))

    def test_invalid_metadata_does_not_invent_missing_samples(self):
        root, protocol = self.batch("run", slots=2)
        self.receipt(root, protocol).write_text("invalid receipt DO_NOT_RETURN", encoding="utf-8")
        self.write(self.base / "bad/protocol.json", {"schedule": [{"id": "../escape"}]})
        result = module.inventory(self.base)
        self.assertEqual(len(result["runs"]), 1)
        run = result["runs"][0]
        self.assertFalse(run["coverage_available"])
        self.assertEqual(run["counts"]["planned"], 2)
        self.assertIsNone(run["counts"]["not_started"])
        self.assertIsNone(run["conditions"][0]["valid_completed"])
        self.assertEqual({d["reason"] for d in result["diagnostics"]},
                         {"invalid_protocol", "attempt_metadata_unavailable_or_changed"})
        self.assertNotIn("DO_NOT_RETURN", json.dumps(result))

    def test_nonterminal_receipt_is_not_a_failure(self):
        root, protocol = self.batch("run")
        self.receipt(root, protocol, status="starting", valid=False)
        counts = module.inventory(run_dirs=[root])["runs"][0]["counts"]
        self.assertEqual(counts["started_slots"], 1)
        self.assertEqual(counts["failed_attempts"], 0)
        self.assertEqual(counts["valid_completed"], 0)

    def test_reparse_directories_and_exact_linked_roots_not_followed(self):
        good, _ = self.batch("good")
        linked, _ = self.batch("linked/run")
        original = module._linked
        with patch.object(module, "_linked", side_effect=lambda path: path == linked.parent or original(path)):
            self.assertEqual([Path(r["run_dir"]) for r in module.inventory(self.base)["runs"]], [good])
            with self.assertRaises(BenchmarkError):
                module.inventory(run_dirs=[linked])
        fake = SimpleNamespace(lstat=lambda: SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400))
        self.assertTrue(module._linked(fake))

    def test_linked_receipts_are_not_loaded(self):
        root, protocol = self.batch("run")
        record = self.receipt(root, protocol)
        original = module._linked
        with patch.object(module, "_linked", side_effect=lambda path: path == record or original(path)), \
                patch.object(module, "all_attempts", side_effect=AssertionError("unsafe read")):
            run = module.inventory(run_dirs=[root])["runs"][0]
        self.assertFalse(run["coverage_available"])

    def test_new_slot_receipt_during_observation_is_unknown(self):
        root, protocol = self.batch("run")
        original = module.all_attempts

        def arriving_receipt(run):
            self.receipt(root, protocol)
            return original(run)

        (root / "runs").mkdir()
        with patch.object(module, "all_attempts", side_effect=arriving_receipt):
            run = module.inventory(run_dirs=[root])["runs"][0]
        self.assertFalse(run["coverage_available"])
        self.assertIsNone(run["counts"]["valid_completed"])

    def test_scope_validation_and_empty_batch(self):
        root, _ = self.batch("empty", slots=0)
        self.assertEqual(module.inventory(run_dirs=[root])["runs"][0]["counts"]["planned"], 0)
        with self.assertRaises(BenchmarkError):
            module.inventory(root=self.base, run_dirs=[root])
        with self.assertRaises(BenchmarkError):
            module.inventory(run_dirs=[])
        with self.assertRaises(BenchmarkError):
            module.inventory(run_dirs=[self.base])

    def test_cli_history_and_inventory_are_read_only_without_server(self):
        root, protocol = self.batch("study/run")
        self.receipt(root, protocol)
        before = self.snapshot(self.base)
        with patch("benchmark.Server", side_effect=AssertionError("inference started")):
            with patch("sys.stdout", new_callable=io.StringIO) as output:
                self.assertEqual(main(["history", "--root", str(self.base)]), 0)
                self.assertEqual(json.loads(output.getvalue())[0]["title"], "study/run")
            for args in (["--root", str(self.base)], ["--run-dirs", str(root), str(root)]):
                with patch("sys.stdout", new_callable=io.StringIO) as output:
                    self.assertEqual(main(["inventory", *args]), 0)
                    value = json.loads(output.getvalue())
                    self.assertEqual(len(value["runs"]), 1)
                    self.assertEqual(value["runs"][0]["counts"]["valid_completed"], 1)
        self.assertEqual(self.snapshot(self.base), before)
        with patch("sys.stderr", new_callable=io.StringIO), self.assertRaises(SystemExit):
            parser().parse_args(["inventory", "--root", str(self.base), "--run-dirs", str(root)])

    def test_first_run_history_without_benchmark_directory_is_empty(self):
        missing = self.base / "benchmarks-not-created"
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(main(["history", "--root", str(missing)]), 0)
            self.assertEqual(json.loads(output.getvalue()), [])
        self.assertFalse(missing.exists())
        with self.assertRaises((BenchmarkError, OSError)):
            module.inventory(root=missing)


if __name__ == "__main__":
    unittest.main()
