"""Principle penalties, fixed-denominator audits and selected offline revisions."""
import contextlib
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import core
from comparison import compare
from evaluation import regrade
from reporting import blind, build_report, seal, validate
import test_comparison as comparison_fixtures
from test_comparison import snapshot
from test_scoring_v2 import v2_gold


def v3_gold(gold):
    result = v2_gold(gold)
    result.update(scoring_version=3, principle_policy=copy.deepcopy(core.V3_POLICY),
                  checkpoints=[{"id": level, "level": level, "requirement": "Verify " + level,
                                "error_criterion": "A", "omission_criterion": "C"}
                               for level in ("core", "key", "local")])
    return result


def sample_score():
    quote = "None 使用默认值"
    return {"points": {"A": 48, "C": 18, "I": 6, "R": 20},
            "notes": {k: "Controlled grading fixture" for k in "ACIR"},
            "evidence": {k: [quote] for k in "ACIR"}, "issues": [],
            "checks": {k: {"status": "correct", "note": "Controlled checkpoint", "evidence": [quote]}
                       for k in ("core", "key", "local")}}


def add_issue(score, kind, level, group=None):
    score["checks"][level]["status"] = kind
    score["issues"].append({"kind": kind, "severity": "minor" if level == "local" else "critical",
                            "criterion": "A" if kind == "error" else "C",
                            "quote": "None 使用默认值" if kind == "error" else "",
                            "note": "Controlled issue", "impact_level": level, "check_id": level,
                            "group_id": group or level, "reference": "Controlled reference", "impact": "Controlled consequence"})
    return score


class PrincipleScoringTests(unittest.TestCase):
    def setUp(self):
        self.gold = v3_gold({"reference_facts": ["fixture"], "verification": ["fixture"]})

    def test_agreed_92_point_examples_and_clean_answer(self):
        expected = {("error", "core"): 59, ("error", "key"): 79, ("error", "local"): 89,
                    ("omission", "core"): 69, ("omission", "key"): 84, ("omission", "local"): 90}
        self.assertEqual(core.principle_score(sample_score(), self.gold)["quality"], 92)
        for (kind, level), final in expected.items():
            with self.subTest(kind=kind, level=level):
                score = add_issue(sample_score(), kind, level)
                core.validate_v3_score(score, self.gold, "None 使用默认值")
                result = core.principle_score(score, self.gold)
                self.assertEqual(result["quality"], final)
                self.assertEqual(result["raw_quality"], 92)

    def test_same_cause_is_charged_once_and_error_does_not_double_charge_omission(self):
        score = add_issue(sample_score(), "error", "key", "shared")
        add_issue(score, "omission", "local", "shared")
        result = core.validate_v3_score(score, self.gold, "None 使用默认值")
        self.assertEqual(result["principle_penalty"], 10)
        self.assertEqual(result["errors"], 1)
        self.assertEqual(result["omissions"], 0)
        self.assertEqual(len(result["principle_groups"]), 1)

    def test_independent_issues_accumulate_and_negative_result_clamps_to_zero(self):
        score = add_issue(sample_score(), "error", "key")
        add_issue(score, "omission", "local")
        self.assertEqual(core.principle_score(score, self.gold)["principle_penalty"], 12)
        score["points"] = {"A": 1, "C": 1, "I": 0, "R": 0}
        self.assertEqual(core.principle_score(score, self.gold)["quality"], 0)

    def test_ratio_uses_fixed_checkpoints_and_extra_claims_cannot_dilute_it(self):
        score = add_issue(sample_score(), "error", "key")
        before = core.principle_score(score, self.gold)
        score["issues"].append({**score["issues"][0], "group_id": "extension", "check_id": None})
        after = core.principle_score(score, self.gold)
        self.assertEqual(before["weighted_error_rate"], 2 / 6)
        self.assertEqual(after["weighted_error_rate"], before["weighted_error_rate"])
        self.assertEqual(after["principle_penalty"], before["principle_penalty"] + 10)

    def test_policy_and_checkpoint_home_are_frozen(self):
        core.validate_gold(self.gold)
        changed = copy.deepcopy(self.gold)
        changed["principle_policy"]["error"]["local"][1] = 100
        with self.assertRaises(ValueError):
            core.validate_gold(changed)
        for change in (lambda s:s["checks"].pop("key"),
                       lambda s:s["checks"]["local"].update(status="omission"),
                       lambda s:s["checks"]["core"].update(evidence=["invented"])):
            score = sample_score()
            change(score)
            with self.assertRaises(ValueError):
                core.validate_v3_score(score, self.gold, "None 使用默认值")

    def test_optional_insight_absence_has_no_principle_penalty(self):
        score = sample_score()
        score["points"]["I"] = 0
        self.assertEqual(core.principle_score(score, self.gold)["principle_penalty"], 0)


class SelectedRegradeV3Tests(unittest.TestCase):
    setUp = comparison_fixtures.ComparisonTests.setUp
    tearDown = comparison_fixtures.ComparisonTests.tearDown
    grade = comparison_fixtures.ComparisonTests.grade
    run_fixture = comparison_fixtures.ComparisonTests.run_fixture

    def test_selected_revision_preserves_slot_ids_metrics_and_original_scores(self):
        with contextlib.redirect_stdout(io.StringIO()):
            source = self.run_fixture("matrix", tiers=["default", "fast"], efforts=["xhigh", "max"], version=2)
        before = snapshot(source)
        rubric = self.base / "v3.json"
        core.write(rubric, v3_gold(core.read(source / "input/gold.json")))
        selection = self.base / "selection.json"
        core.write(selection, [{"model": core.MODELS[0], "effort": "max", "service_tier": "default"}])
        root = self.base / "revision"
        # Reconstruct the retired four-dimensional archive only for compatibility tests.
        with patch("runtime.Server", side_effect=AssertionError("No inference")), patch("evaluation.require_current_gold"):
            regrade(source, rubric, root, condition_filter=selection)
            packet = blind(root)
        self.assertEqual(packet["answers"], 1)
        mapping = core.read(root / "grading/private-mapping.json")
        scores = {anon: add_issue(sample_score(), "error", "local") for anon in mapping}
        core.write(self.base / "scores.json", scores)
        seal(root, self.base / "scores.json")
        data = build_report(root)
        validate(root)
        self.assertEqual(data["planned"], 1)
        self.assertEqual(data["scoring_version"], 3)
        self.assertEqual(data["rows"][0]["quality"], 89)
        self.assertEqual(data["rows"][0]["raw_quality"], 92)
        self.assertEqual(data["rows"][0]["slot_id"], core.evaluation_schedule(root)[0]["id"])
        self.assertEqual(core.read(root / "protocol.json")["schedule"], core.read(source / "protocol.json")["schedule"])
        compare([root], self.base / "comparison")
        exported = core.read(self.base / "comparison/comparison-data.json")
        self.assertEqual(exported["counts"]["planned_slots"], 1)
        self.assertEqual(exported["samples"][0]["raw_quality"], 92)
        exported_source = self.base / "comparison/sources" / exported["source_batches"][0]["id"]
        self.assertEqual((exported_source / "evaluation/previous-grading/scores.json").read_bytes(),
                         (source / "grading/scores.json").read_bytes())
        self.assertEqual((exported_source / "evaluation/previous-summary.json").read_bytes(),
                         (source / "summary.json").read_bytes())
        matrix = (self.base / "comparison/matrix.html").read_text(encoding="utf-8")
        self.assertIn("最终分 / 分钟", matrix)
        self.assertIn('class="score">89.0', matrix)
        self.assertIn("错 1/1 · 漏 0/1", matrix)
        self.assertIn("原则扣分", matrix)
        self.assertNotIn("<script", matrix)
        self.assertEqual(snapshot(source), before)


if __name__ == "__main__":
    unittest.main()
