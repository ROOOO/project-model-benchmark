"""Offline cross-run behavior and export regressions; never samples a model."""
import contextlib
import csv
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import core
from benchmark import main, parser
from comparison import compare
from compact import display_topics
from evaluation import regrade
from reporting import blind, build_report, seal
from test_benchmark import fixture, write_attempt, RATES
from test_scoring_v2 import v2_gold


def snapshot(root):
    return {p.relative_to(root).as_posix(): core.sha(p) for p in root.rglob("*") if p.is_file()}


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="benchmark-comparison-")
        self.base = Path(self.temp.name)
        self.case, self.prices = fixture(self.base)
        self.tier_prices = self.base / "tier-prices.json"
        core.write(self.tier_prices, {"schema_version": 2, "as_of": "2026-10-20",
            "sources": ["https://developers.openai.com/api/docs/pricing"], "service_tiers": {
                t: {"models": {m: {k: v * factor for k, v in RATES.items()} for m in core.MODELS}}
                for t, factor in [("default", 1), ("priority", 2)]}})

    def tearDown(self):
        self.temp.cleanup()

    def grade(self, root, version=1):
        blind(root)
        scores = core.read(root / "grading/packet/scores-template.json")
        for item in scores.values():
            if version == 1:
                item.update(points={"A1": 70, "R1": 29}, notes={"A1": "语义正确", "R1": "清晰"})
            else:
                item.update(points={"A": 50, "C": 20, "I": 0, "R": 19},
                    notes={"A": "语义正确", "C": "必答完整", "I": "没有额外推导", "R": "清晰"},
                    evidence={k: ["None 使用默认值"] for k in ["A", "C", "I", "R"]}, issues=[])
        path = self.base / (root.name + "-scores.json")
        core.write(path, scores)
        seal(root, path)
        build_report(root)

    def run_fixture(self, name, *, tiers=None, repetitions=1, failures=False, terminal_failure=False, unknown=False, version=1):
        if version == 2:
            core.write(self.case / "gold.json", v2_gold(core.read(self.case / "gold.json")))
        root = core.prepare(self.case, self.base / name, [core.MODELS[0]], ["max"], repetitions,
                            self.tier_prices if tiers else self.prices, service_tiers=tiers)
        protocol = core.check_inputs(root)
        for i, slot in enumerate(protocol["schedule"]):
            if terminal_failure and i == len(protocol["schedule"]) - 1:
                write_attempt(root, slot, valid=False, usage=False)
                continue
            if failures and i == 0:
                bad = write_attempt(root, slot, valid=False, usage=False)
                bad.update(status="interrupted", remote_completion_unknown=True)
                core.write(root / "runs" / slot["id"] / "attempt-001/result.json", bad)
            attempt = 2 if failures and i == 0 else 1
            result = write_attempt(root, slot, attempt=attempt, usage=not unknown)
            directory = root / "runs" / slot["id"] / f"attempt-{attempt:03d}"
            elapsed = [2, 4, 12][i % 3]
            if tiers:
                for key in core.METRICS:
                    result.pop(key, None)
                tier = slot["service_tier"]
                result.update(service_tier=tier)
                result["effective"]["serviceTier"] = tier
                rates = core.price_for(core.read(self.tier_prices), slot["model"], tier)
            else:
                rates = RATES
            result.update(core.metrics_for_protocol(protocol, result["usage"], elapsed, 1, 3,
                (directory / "answer.txt").read_text(encoding="utf-8"), rates))
            core.write(directory / "result.json", result)
        self.grade(root, version)
        return root

    def output(self, *roots, name="comparison", **kwargs):
        target = self.base / name
        compare([str(r) for r in roots], str(target), **kwargs)
        return target, core.read(target / "comparison-data.json")

    def test_cli_offline_shape_and_dispatch(self):
        args = parser().parse_args(["compare", "--run-dirs", "a", "b", "--output", "new",
                                   "--models", "model-a", "--assessment", "notes.json"])
        self.assertEqual(args.run_dirs, ["a", "b"])
        with patch("benchmark.compare", return_value={"inference_started": False}) as call, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["compare", "--run-dirs", "a", "b", "--output", "new"]), 0)
        call.assert_called_once_with(["a", "b"], "new", None, None)

    def test_legacy_scale_cost_and_median_mean_labels(self):
        root = self.run_fixture("legacy", repetitions=3)
        target, data = self.output(root)
        case = data["cases"][0]
        self.assertEqual(case["weights"], {"accuracy": 70, "readability": 30})
        condition = case["conditions"][0]
        self.assertEqual(condition["statistics"]["elapsed_s"]["median"], 4)
        self.assertEqual(condition["statistics"]["elapsed_s"]["mean"], 6)
        for dim in ["insight", "coverage"]:
            self.assertEqual(condition["statistics"][dim]["n"], 0)
            self.assertIsNone(condition["statistics"][dim]["mean"])
        self.assertGreater(condition["statistics"]["estimated_api_usd"]["mean"], 0)
        page = (target / "report.html").read_text(encoding="utf-8")
        self.assertIn("中位数", page)
        self.assertIn("均值", page)

    def test_v2_zero_insight_and_unknown_cost_not_zero(self):
        root = self.run_fixture("v2", version=2, unknown=True)
        _, data = self.output(root)
        case = data["cases"][0]
        self.assertEqual(case["weights"], core.SCORING_WEIGHTS[2])
        condition = case["conditions"][0]
        self.assertEqual(condition["statistics"]["insight"]["mean"], 0)
        self.assertEqual(condition["statistics"]["coverage"]["mean"], 20)
        self.assertEqual(condition["statistics"]["estimated_api_usd"]["n"], 0)
        self.assertIsNone(condition["statistics"]["estimated_api_usd"]["mean"])

    def test_regrade_duplicate_original_attempt_rejected(self):
        root = self.run_fixture("original")
        rubric = self.base / "new-rubric.json"
        core.write(rubric, v2_gold(core.read(self.case / "gold.json")))
        revision = self.base / "revision"
        regrade(root, rubric, revision)
        self.grade(revision, 2)
        before = snapshot(root), snapshot(revision)
        with self.assertRaisesRegex(ValueError, "(?i)(duplicate|primary|original|overlap)"):
            self.output(root, revision)
        self.assertEqual(before, (snapshot(root), snapshot(revision)))
        self.assertFalse((self.base / "comparison").exists())

    def test_different_rubrics_kept_separate_even_for_identical_answers(self):
        first = self.run_fixture("v1")
        second = self.run_fixture("v2", version=2)
        _, data = self.output(first, second)
        self.assertEqual(data["counts"]["valid_answers"], 2)
        self.assertEqual(len(data["cases"]), 2)
        self.assertEqual({c["scoring_version"] for c in data["cases"]}, {1, 2})

    def test_interrupted_attempt_preserved_and_valid_count_unequal(self):
        first = self.run_fixture("one", failures=True)
        second = self.run_fixture("three", repetitions=3)
        _, data = self.output(first, second)
        self.assertEqual(data["counts"]["valid_answers"], 4)
        self.assertEqual(data["counts"]["attempts"], 5)
        self.assertEqual(len(data["failures"]), 1)
        self.assertEqual(data["failures"][0]["status"], "interrupted")
        self.assertIsNone(data["failures"][0].get("estimated_api_usd"))
        self.assertEqual(sorted(c["valid_completed"] for c in data["cases"][0]["conditions"]), [1, 3])

    def test_same_batch_pair_preferred_over_historical_candidate(self):
        mixed = self.run_fixture("mixed", tiers=["default", "fast"])
        old = self.run_fixture("historical-standard")
        _, data = self.output(mixed, old)
        pairs = data["paired_speed_observations"]
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0]["kind"], "same-batch")
        self.assertEqual(pairs[0]["standard_batch"], pairs[0]["fast_batch"])

    def test_failed_slot_keeps_actual_n_and_unknown_failed_cost(self):
        root = self.run_fixture("partial-success", repetitions=3, terminal_failure=True)
        _, data = self.output(root)
        self.assertEqual(data["counts"]["planned_slots"], 3)
        self.assertEqual(data["counts"]["valid_answers"], 2)
        condition = data["cases"][0]["conditions"][0]
        self.assertEqual(condition["planned"], 3)
        self.assertEqual(condition["statistics"]["elapsed_s"]["n"], 2)
        self.assertEqual(data["failures"][0]["status"], "failed")
        self.assertIsNone(data["failures"][0].get("estimated_api_usd"))

    def test_unique_cross_batch_pair_labeled_historical(self):
        standard = self.run_fixture("standard")
        fast = self.run_fixture("fast", tiers=["fast"])
        _, data = self.output(standard, fast)
        self.assertEqual(len(data["paired_speed_observations"]), 1)
        self.assertEqual(data["paired_speed_observations"][0]["kind"], "historical-observation")

    def test_ambiguous_cross_batch_candidates_never_auto_paired(self):
        a = self.run_fixture("standard-a")
        b = self.run_fixture("standard-b")
        fast = self.run_fixture("fast", tiers=["fast"])
        _, data = self.output(a, b, fast)
        self.assertEqual(data["paired_speed_observations"], [])
        self.assertTrue(data["pairing_notes"])

    def test_read_only_inputs_and_archive_exact_bytes(self):
        root = self.run_fixture("source")
        before = snapshot(root)
        with patch("reporting.validate", side_effect=AssertionError("writes source")), \
             patch("reporting.build_report", side_effect=AssertionError("rewrites source")), \
             patch("runtime.Server", side_effect=AssertionError("inference forbidden")):
            target, data = self.output(root)
        self.assertEqual(before, snapshot(root))
        archives = list(target.glob("*.zip"))
        self.assertEqual(len(archives), 1)
        with zipfile.ZipFile(archives[0]) as bundle:
            self.assertIn("report.html", bundle.namelist())
            self.assertIn("comparison-data.json", bundle.namelist())
            for name in ("compact.html", "compact.csv", "compact-data.json"):
                self.assertIn(name, bundle.namelist())
            for name in bundle.namelist():
                self.assertEqual(bundle.read(name), (target / name).read_bytes())
        with (target / "conditions.csv").open(encoding="utf-8-sig", newline="") as handle:
            self.assertEqual(len(list(csv.DictReader(handle))), data["counts"]["conditions"])
        with (target / "samples.csv").open(encoding="utf-8-sig", newline="") as handle:
            self.assertEqual(len(list(csv.DictReader(handle))), data["counts"]["attempts"])

    def test_corrupted_metrics_rejected_even_if_summary_was_regenerated(self):
        root = self.run_fixture("source")
        record = next((root / "runs").glob("*/attempt-*/result.json"))
        value = core.read(record)
        value["visible_decode_tps"] = 999
        core.write(record, value)
        build_report(root)
        before = snapshot(root)
        with self.assertRaisesRegex(ValueError, "Metric inconsistent"):
            self.output(root)
        self.assertEqual(before, snapshot(root))
        self.assertFalse((self.base / "comparison").exists())

    def test_distinct_runs_with_same_directory_name_keep_correct_answer_sources(self):
        first = self.run_fixture("parent-a/run")
        second = self.run_fixture("parent-b/run")
        target, data = self.output(first, second)
        self.assertEqual(data["counts"]["valid_answers"], 2)
        self.assertEqual(len({r["source_id"] for r in data["samples"]}), 2)
        for row in data["samples"]:
            self.assertEqual(core.sha(target / row["answer_link"]), row["answer_sha256"])
            copied_result = (target / row["answer_link"]).with_name("result.json")
            self.assertEqual(core.sha(copied_result), row["result_sha256"])

    def test_existing_or_overlapping_destination_rejected(self):
        root = self.run_fixture("source")
        for target in [root / "comparison", root, self.base]:
            with self.subTest(target=target), self.assertRaises(ValueError):
                compare([root], target)
        self.assertFalse((root / "comparison").exists())

    def test_failed_archive_never_leaves_successful_validation_receipt(self):
        root = self.run_fixture("source")
        before = snapshot(root)
        with patch("comparison.zipfile.ZipFile", side_effect=OSError("synthetic archive failure")):
            with self.assertRaises(OSError):
                self.output(root)
        validation = core.read(self.base / "comparison/validation.json")
        self.assertFalse(validation["ok"])
        self.assertFalse(validation["archive_bytes_verified"])
        self.assertEqual(before, snapshot(root))

    def test_assessment_is_optional_and_preserves_supported_schema(self):
        root = self.run_fixture("source")
        assessment = {"conclusion": "本题观察", "evidence": ["相同题面"], "limitations": ["单题小样本"]}
        path = self.base / "assessment.json"
        core.write(path, assessment)
        _, data = self.output(root, assessment=path)
        self.assertEqual(data["assessment"], assessment)
        core.write(path, {"conclusion": "x", "evidence": "wrong type", "limitations": []})
        with self.assertRaises(ValueError):
            self.output(root, name="bad-assessment", assessment=path)
        self.assertFalse((self.base / "bad-assessment").exists())

    def test_compact_view_preserves_scale_identity_and_partial_coverage(self):
        root = self.run_fixture("unknown-cost", version=2, repetitions=2, unknown=True)
        target, data = self.output(root)
        compact = core.read(target / "compact-data.json")
        condition = data["cases"][0]["conditions"][0]
        row = compact["rows"][0]
        self.assertEqual(compact["condition_count"], 1)
        self.assertEqual(compact["source_sha256"], core.sha(target / "comparison-data.json"))
        self.assertEqual(row["condition_id"], condition["id"])
        self.assertEqual(row["execution_profile_id"], condition["execution_profile_id"])
        self.assertEqual(row["quality_mean"], condition["statistics"]["quality"]["mean"])
        self.assertEqual(row["valid_n"], 2)
        self.assertEqual(row["estimated_api_usd_n"], 0)
        self.assertIsNone(row["estimated_api_usd_mean"])
        page = (target / "compact.html").read_text(encoding="utf-8")
        self.assertIn("准确（50） / 覆盖（20） / 洞察（10） / 表达（20）", page)
        self.assertIn("— [0/2]", page)
        self.assertNotIn("(2)", page)
        with (target / "compact.csv").open(encoding="utf-8", newline="") as f:
            saved = list(csv.DictReader(f))
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["source_id"], row["source_id"])
        self.assertEqual(saved[0]["estimated_api_usd_n"], "0")

    def test_compact_legacy_missing_dimensions_keep_unknown_scale(self):
        root = self.run_fixture("legacy-compact")
        target, _ = self.output(root)
        compact = core.read(target / "compact-data.json")
        row = compact["rows"][0]
        self.assertEqual(row["accuracy_maximum"], 70)
        self.assertEqual(row["readability_maximum"], 30)
        self.assertIsNone(row["coverage_maximum"])
        self.assertIsNone(row["insight_maximum"])
        page = (target / "compact.html").read_text(encoding="utf-8")
        self.assertIn("准确（70） / 覆盖（—） / 洞察（—） / 表达（30）", page)

    def test_same_topic_profiles_share_view_without_merging_conditions(self):
        first = self.run_fixture("profile-a", tiers=["default", "fast"])
        second = self.run_fixture("profile-b", tiers=["default", "fast"])
        with patch("comparison.execution_identity", side_effect=[("fixed-evidence", "profile-a"), ("fixed-evidence", "profile-b")]):
            target, data = self.output(first, second)
        self.assertEqual(len(data["cases"]), 2)
        topics = display_topics(data["cases"])
        self.assertEqual(len(topics), 1)
        self.assertEqual(len(topics[0]["conditions"]), 4)
        self.assertEqual(len(topics[0]["member_case_ids"]), 2)
        self.assertEqual(len(data["paired_speed_observations"]), 2)
        compact = core.read(target / "compact-data.json")
        self.assertEqual(compact["topic_count"], 1)
        self.assertEqual(compact["condition_count"], 4)
        self.assertEqual({r["execution_profile_id"] for r in compact["rows"]}, {"profile-a", "profile-b"})
        if shutil.which("node"):
            checker = Path(__file__).resolve().parents[1] / "check_comparison.cjs"
            completed = subprocess.run([shutil.which("node"), str(checker), str(target)],
                                       check=True, capture_output=True, text=True, encoding="utf-8")
            checked = json.loads(completed.stdout)
            self.assertEqual(checked["display_topics"], 1)
            self.assertEqual(checked["compact_conditions"], 4)

    def test_display_topics_do_not_join_rubrics_modes_or_incompatible_scales(self):
        common = {"case_id": "topic", "rubric_sha256": "gold", "scoring_version": 2,
                  "execution_mode": "project-readonly", "dimension_maxima": {"accuracy": 50},
                  "conditions": [], "sources": []}
        groups = [{**common, "id": "one"}, {**common, "id": "two", "execution_mode": "fixed-evidence"},
                  {**common, "id": "three", "rubric_sha256": "different"}]
        self.assertEqual(len(display_topics(groups)), 3)
        with self.assertRaisesRegex(ValueError, "maxima"):
            display_topics([groups[0], {**common, "id": "bad", "dimension_maxima": {"accuracy": 70}}])

    def test_same_named_rubrics_have_distinct_selector_labels(self):
        if not shutil.which("node"):
            self.skipTest("Node.js unavailable")
        first = self.run_fixture("rubric-a")
        gold = core.read(self.case / "gold.json")
        gold["rubric"][0]["criterion"] += "；另一个评分细则版本"
        core.write(self.case / "gold.json", gold)
        second = self.run_fixture("rubric-b")
        target, data = self.output(first, second)
        self.assertEqual(len(data["cases"]), 2)
        checker = Path(__file__).resolve().parents[1] / "check_comparison.cjs"
        checked = subprocess.run([shutil.which("node"), str(checker), str(target)],
                                 check=True, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(json.loads(checked.stdout)["display_topics"], 2)


if __name__ == "__main__":
    unittest.main()
