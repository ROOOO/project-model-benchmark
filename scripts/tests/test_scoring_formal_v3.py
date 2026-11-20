"""Formal V3 adopts existing three-dimensional scores and compares original V2."""
import copy
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import core
import test_scoring_v4 as archival_fixtures
from comparison import compare
from evaluation import formal_v3_gold, formal_v3_score, regrade, v4_gold_from_v3, v4_score_from_v3
from reporting import build_report, render_csv, validate
from test_comparison import snapshot
from test_scoring_v3 import sample_score, v3_gold


class FormalV3RulesTests(unittest.TestCase):
    def setUp(self):
        self.trial3 = v3_gold({"reference_facts":["fixture"], "verification":["fixture"]})
        self.trial4 = v4_gold_from_v3(self.trial3)
        self.formal = formal_v3_gold(self.trial4)

    def test_reused_version_is_disambiguated_by_frozen_dimensions_and_weights(self):
        self.assertEqual(self.formal["scoring_version"], 3)
        self.assertEqual(core.scoring_weights(self.formal), {"accuracy":70, "coverage":20, "insight":10})
        self.assertEqual(core.scoring_weights(self.trial3), {"accuracy":50, "coverage":20, "insight":10, "readability":20})
        self.assertEqual(core.scoring_weights(self.trial4), core.scoring_weights(self.formal))
        core.require_current_gold(self.formal)
        for gold in (self.trial3, self.trial4):
            with self.assertRaisesRegex(ValueError, "read-only"):
                core.require_current_gold(gold)
        with self.assertRaisesRegex(ValueError, "already scored"):
            formal_v3_gold(self.trial3)
        with self.assertRaisesRegex(ValueError, "four-dimensional"):
            v4_gold_from_v3(self.formal)

    def test_import_changes_labels_without_changing_any_points_or_judgments(self):
        source = v4_score_from_v3(sample_score(), self.trial3)
        original = copy.deepcopy(source)
        score = formal_v3_score(source, self.trial4)
        self.assertEqual(source, original)
        for key in ("points", "evidence", "issues", "checks", "presentation_notes"):
            self.assertEqual(score[key], source[key])
        self.assertEqual(core.principle_score(score, self.formal), core.principle_score(source, self.trial4))
        self.assertEqual(formal_v3_gold(self.formal), self.formal)
        self.assertEqual(formal_v3_score(score, self.formal), score)
        self.assertEqual(sum(core.scoring_weights(self.formal).values()), 100)

    def test_signed_delta_statistics_preserve_negative_values(self):
        result = core.distribution([-1.8, -2.2, 0, 1, None, True], decimal=True, signed=True)
        self.assertEqual(result["n"], 4)
        self.assertEqual(result["mean"], -0.75)
        self.assertEqual(core.distribution([-1.8, 2])["n"], 1)

    def test_empty_csv_still_uses_frozen_weights_to_distinguish_same_number(self):
        legacy = render_csv([],3,weights=core.scoring_weights(self.trial3)).splitlines()[0]
        formal = render_csv([],3,weights=core.scoring_weights(self.formal)).splitlines()[0]
        self.assertIn("readability",legacy)
        self.assertNotIn("presentation_notes",legacy)
        self.assertNotIn("readability",formal)
        self.assertIn("presentation_notes",formal)


class FormalV3OfflineTests(unittest.TestCase):
    setUp = archival_fixtures.V4OfflineMigrationTests.setUp
    tearDown = archival_fixtures.V4OfflineMigrationTests.tearDown
    grade = archival_fixtures.V4OfflineMigrationTests.grade
    run_fixture = archival_fixtures.V4OfflineMigrationTests.run_fixture
    make_v3 = archival_fixtures.V4OfflineMigrationTests.make_v3

    def test_adopts_scores_unchanged_and_uses_original_v2_for_all_exports(self):
        # Make the intermediate score differ from V2, so a wrong comparison cannot pass.
        fixture_score = sample_score(); fixture_score["points"]["I"] = 5
        with patch.object(archival_fixtures, "sample_score", return_value=fixture_score):
            source, retired3, rubric4, selection = self.make_v3()
        retired4 = self.base / "retired-three-dimensional"
        with patch("evaluation.require_current_gold"):
            regrade(source, rubric4, retired4, scores_from=retired3, condition_filter=selection)
        old = build_report(retired4); validate(retired4)
        preserved = {str(p):snapshot(p) for p in (source, retired3, retired4)}
        target = self.base / "formal-v3"
        rubric = self.base / "formal.json"
        core.write(rubric, formal_v3_gold(core.read(rubric4)))
        with patch("runtime.Server", side_effect=AssertionError("No inference")):
            result = regrade(source, rubric, target, scores_from=retired4, condition_filter=selection)
        self.assertTrue(result["scores_imported_unchanged"])
        self.assertFalse(result["inference_started"])
        summary = build_report(target); validate(target)
        self.assertEqual(summary["scoring_version"], 3)
        self.assertEqual(summary["previous_evaluation"]["scoring_version"], 2)
        self.assertEqual(summary["rows"][0]["quality"], old["rows"][0]["quality"])
        self.assertEqual(summary["rows"][0]["quality"], 87.2)
        for key in ("slot_id", "elapsed_s", "input_tokens", "estimated_api_usd", "quality", "raw_quality",
                    "principle_penalty", "quality_cap", "principle_groups", "weighted_error_rate", "weighted_omission_rate"):
            self.assertEqual(summary["rows"][0][key], old["rows"][0][key])
        output = self.base / "comparison"; compare([target], output)
        data = core.read(output / "comparison-data.json")
        row = data["samples"][0]
        self.assertEqual(row["previous_scoring_version"], 2)
        self.assertEqual(row["previous_quality"], 89)
        self.assertEqual(row["quality_delta"], -1.8)
        self.assertEqual(data["cases"][0]["conditions"][0]["statistics"]["quality_delta"]["mean"], -1.8)
        for name in ("samples.csv", "conditions.csv", "compact.csv", "question-overview.csv"):
            header = (output / name).read_text(encoding="utf-8-sig").splitlines()[0]
            self.assertNotIn("readability", header)
            self.assertIn("quality_delta", header)
        matrix = (output / "matrix.html").read_text(encoding="utf-8")
        self.assertIn("V3 四题紧凑评分报告", matrix)
        self.assertIn("V2 89.00 · Δ-1.80", matrix)
        self.assertIn("不代表模型能力变化", matrix)
        self.assertNotIn("V4", matrix)
        self.assertNotIn("表达20", matrix)
        source_dir = output / "sources" / data["source_batches"][0]["id"]
        self.assertEqual((source_dir / "evaluation/previous-grading/scores.json").read_bytes(), (source / "grading/scores.json").read_bytes())
        self.assertEqual((source_dir / "evaluation/derivation-source/grading/scores.json").read_bytes(), (retired4 / "grading/scores.json").read_bytes())
        for root in (source, retired3, retired4):
            self.assertEqual(snapshot(root), preserved[str(root)])
        checked = subprocess.run(["node", str(Path(__file__).resolve().parents[1] / "check_comparison.cjs"), str(output)], capture_output=True, encoding="utf-8")
        self.assertEqual(checked.returncode, 0, checked.stderr)

    def test_current_prepare_rejects_retired_formats(self):
        gold3 = v3_gold(core.read(self.case / "gold.json"))
        for index, gold in enumerate((gold3, v4_gold_from_v3(gold3))):
            core.write(self.case / "gold.json", gold)
            with self.assertRaisesRegex(ValueError, "read-only"):
                core.prepare(self.case, self.base / f"rejected-{index}", [core.MODELS[0]], ["max"], 1, self.prices)


if __name__ == "__main__":
    unittest.main()
