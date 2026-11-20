"""Exact V3-to-V4 migration, frozen judgments and three-dimension exports."""
import contextlib
import copy
import io
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import core
from benchmark import parser
from comparison import compare
from evaluation import formal_v3_gold, formal_v3_score, regrade, v4_gold_from_v3, v4_score_from_v3
from reporting import blind, build_report, seal, validate
import test_comparison as comparison_fixtures
from test_comparison import snapshot
from test_scoring_v3 import add_issue, sample_score, v3_gold


class V4ArithmeticTests(unittest.TestCase):
    def setUp(self):
        self.gold3 = v3_gold({"reference_facts": ["fixture"], "verification": ["fixture"]})
        self.gold4 = v4_gold_from_v3(self.gold3)

    def test_decimal_weights_and_unchanged_non_accuracy_judgments(self):
        original = add_issue(sample_score(), "error", "local")
        before = copy.deepcopy(original)
        migrated = v4_score_from_v3(original, self.gold3)
        self.assertEqual(original, before)
        self.assertEqual(core.scoring_weights(self.gold4), {"accuracy": 70, "coverage": 20, "insight": 10})
        self.assertEqual(migrated["points"], {"A": 67.2, "C": 18, "I": 6})
        for field in ("checks", "issues"):
            self.assertEqual(migrated[field], original[field])
        self.assertEqual(migrated["evidence"], {k:v for k,v in original["evidence"].items() if k != "R"})
        result = core.validate_v3_score(migrated, self.gold4, "None 使用默认值")
        self.assertEqual(result["raw_quality"], 91.2)
        self.assertEqual(result["quality"], 88.2)
        self.assertEqual(result["principle_penalty"], 3)
        self.assertEqual(result["quality_cap"], 89)
        self.assertEqual(core.score_sum([0.1, 0.2]), 0.3)
        self.assertEqual(core.score_scale(12, "1.4"), 16.8)

    def test_full_accuracy_and_presentation_remarks_do_not_change_score(self):
        original = sample_score()
        original["points"].update(A=50, C=20)
        migrated = v4_score_from_v3(original, self.gold3)
        self.assertEqual(core.principle_score(original, self.gold3)["quality"], core.principle_score(migrated, self.gold4)["quality"])
        migrated["presentation_notes"] = [{"quote": "None 使用默认值", "note": "A non-scoring example of a concrete reading obstacle."}]
        self.assertEqual(core.principle_score(migrated, self.gold4)["quality"], 96)
        self.assertNotIn("readability", core.scoring_weights(self.gold4))

    def test_principle_caps_dedup_and_fixed_denominators_survive_migration(self):
        for kind in ("error", "omission"):
            for level in ("core", "key", "local"):
                original = add_issue(sample_score(), kind, level)
                migrated = v4_score_from_v3(original, self.gold3)
                old = core.principle_score(original, self.gold3)
                new = core.principle_score(migrated, self.gold4)
                for key in ["principle_penalty", "quality_cap", "principle_groups", "weighted_error_rate", "weighted_omission_rate"]:
                    self.assertEqual(old[key], new[key])
        original = add_issue(sample_score(), "error", "key", "same")
        add_issue(original, "omission", "local", "same")
        migrated = v4_score_from_v3(original, self.gold3)
        self.assertEqual(core.principle_score(migrated, self.gold4)["principle_penalty"], 10)
        migrated["points"] = {"A": 0, "C": 0, "I": 0}
        self.assertEqual(core.principle_score(migrated, self.gold4)["quality"], 0)

    def test_v4_notes_require_real_excerpts_and_cannot_carry_points(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            packet = Path(directory); (packet / "answers").mkdir()
            (packet / "answers/Q001.txt").write_text("None 使用默认值", encoding="utf-8")
            score = v4_score_from_v3(sample_score(), self.gold3)
            core.validate_scores({"Q001":score}, {"Q001":None}, packet, self.gold4)
            for remark in [{"quote":"invented", "note":"bad"}, {"quote":"None", "note":"bad", "points":3}]:
                score["presentation_notes"] = [remark]
                with self.assertRaises(ValueError):
                    core.validate_scores({"Q001":score}, {"Q001":None}, packet, self.gold4)


class V4OfflineMigrationTests(unittest.TestCase):
    setUp = comparison_fixtures.ComparisonTests.setUp
    tearDown = comparison_fixtures.ComparisonTests.tearDown
    grade = comparison_fixtures.ComparisonTests.grade
    run_fixture = comparison_fixtures.ComparisonTests.run_fixture

    def test_new_formal_v3_case_uses_three_dimensions_and_non_scoring_notes(self):
        from test_benchmark import write_attempt
        gold3 = v3_gold(core.read(self.case / "gold.json"))
        gold4 = v4_gold_from_v3(gold3)
        core.write(self.case / "gold.json", formal_v3_gold(gold4))
        root = core.prepare(self.case, self.base / "new-formal-v3", [core.MODELS[0]], ["max"], 1, self.prices)
        write_attempt(root, core.check_inputs(root)["schedule"][0])
        blind(root)
        template = core.read(root / "grading/packet/scores-template.json")
        scores = {}
        for anon, item in template.items():
            self.assertEqual(set(item["points"]), {"A", "C", "I"})
            self.assertEqual(item["presentation_notes"], [])
            scores[anon] = formal_v3_score(v4_score_from_v3(sample_score(), gold3), gold4)
            scores[anon]["presentation_notes"] = [{"quote":"None 使用默认值", "note":"A quoted, non-scoring handoff obstacle."}]
        path = self.base / "fresh-scores.json"; core.write(path, scores); seal(root, path)
        data = build_report(root); validate(root)
        self.assertEqual(data["rows"][0]["quality"], 91.2)
        self.assertEqual(data["scoring_weights"], {"accuracy":70,"coverage":20,"insight":10})
        self.assertNotIn("previous_evaluation", data)

    def make_v3(self):
        with contextlib.redirect_stdout(io.StringIO()):
            source = self.run_fixture("original", tiers=["default", "fast"], efforts=["max"], version=2)
        gold3 = v3_gold(core.read(source / "input/gold.json"))
        rubric3 = self.base / "v3.json"; core.write(rubric3, gold3)
        selection = self.base / "selection.json"
        core.write(selection, [{"model":core.MODELS[0], "effort":"max", "service_tier":"default"}])
        review = self.base / "v3"
        # Only these fixture constructors bypass the guard on retired formats.
        with patch("evaluation.require_current_gold"):
            regrade(source, rubric3, review, condition_filter=selection)
        blind(review)
        scores = {anon:add_issue(sample_score(), "error", "local") for anon in core.read(review / "grading/private-mapping.json")}
        core.write(self.base / "v3-scores.json", scores)
        seal(review, self.base / "v3-scores.json"); build_report(review); validate(review)
        rubric4 = self.base / "v4.json"; core.write(rubric4, v4_gold_from_v3(gold3))
        return source, review, rubric4, selection

    def test_selected_sealed_migration_preserves_identity_and_primary_v3_history(self):
        source, review, rubric4, selection = self.make_v3()
        before, old_review = snapshot(source), snapshot(review)
        target = self.base / "v4"
        with patch("runtime.Server", side_effect=AssertionError("No inference")), patch("evaluation.require_current_gold"):
            result = regrade(source, rubric4, target, scores_from=review, condition_filter=selection)
        self.assertEqual(result["sealed_answers"], 1)
        self.assertEqual(result["next"], "report")
        summary = build_report(target); validate(target)
        self.assertEqual(summary["scoring_weights"], {"accuracy":70,"coverage":20,"insight":10})
        self.assertEqual(summary["previous_evaluation"]["scoring_version"], 3)
        self.assertEqual(summary["rows"][0]["raw_quality"], 91.2)
        self.assertEqual(summary["rows"][0]["quality"], 88.2)
        self.assertNotIn("readability", summary["rows"][0])
        for row in summary["rows"]:
            previous = next(r for r in core.read(review / "summary.json")["rows"] if r["answer_link"] == row["answer_link"])
            self.assertEqual(row["elapsed_s"], previous["elapsed_s"])
            self.assertEqual(row["slot_id"], previous["slot_id"])
        output = self.base / "comparison"; compare([target], output)
        data = core.read(output / "comparison-data.json")
        self.assertEqual(data["samples"][0]["previous_quality"], 89)
        self.assertEqual(data["samples"][0]["previous_scoring_version"], 3)
        self.assertNotIn("readability", data["samples"][0])
        self.assertNotIn("readability", (output / "samples.csv").read_text(encoding="utf-8").splitlines()[0])
        compact = (output / "compact.html").read_text(encoding="utf-8")
        self.assertIn("准确（70） / 覆盖（20） / 洞察（10）", compact)
        self.assertNotIn("表达（", compact)
        matrix = (output / "matrix.html").read_text(encoding="utf-8")
        self.assertIn("V4 四题紧凑评分报告", matrix)
        self.assertIn("基础分：准确性70 + 覆盖20 + 洞察10", matrix)
        self.assertIn("presentation_notes", matrix)
        archive_source = output / "sources" / data["source_batches"][0]["id"]
        self.assertEqual((archive_source / "evaluation/reweight-source/grading/scores.json").read_bytes(), (review / "grading/scores.json").read_bytes())
        self.assertEqual(snapshot(source), before); self.assertEqual(snapshot(review), old_review)
        script = Path(__file__).resolve().parents[1] / "check_comparison.cjs"
        checked = subprocess.run(["node", str(script), str(output)], capture_output=True, encoding="utf-8")
        self.assertEqual(checked.returncode, 0, checked.stderr)

    def test_migration_rejects_a_different_protocol_and_modified_rubric(self):
        source, review, rubric4, selection = self.make_v3()
        altered = core.read(rubric4); altered["checkpoints"][0]["requirement"] += " altered"
        core.write(rubric4, altered)
        with self.assertRaisesRegex(ValueError, "exact declared"), patch("evaluation.require_current_gold"):
            regrade(source, rubric4, self.base / "bad", scores_from=review, condition_filter=selection)
        self.assertFalse((self.base / "bad").exists())
        core.write(rubric4, v4_gold_from_v3(core.read(review / "evaluation/rubric.json")))
        with contextlib.redirect_stdout(io.StringIO()):
            other = self.run_fixture("other", tiers=["default"], efforts=["max"], version=2)
        with self.assertRaisesRegex(ValueError, "different original"), patch("evaluation.require_current_gold"):
            regrade(other, rubric4, self.base / "wrong-source", scores_from=review)
        self.assertFalse((self.base / "wrong-source").exists())
        args = parser().parse_args(["regrade","--run-dir",str(source),"--rubric",str(rubric4),"--output",str(self.base/'parsed'),"--scores-from",str(review)])
        self.assertEqual(args.scores_from, str(review))


if __name__ == "__main__":
    unittest.main()
