"""Offline versioned scoring and immutable revision regressions."""
import copy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import core
from benchmark import main, parser, retest
from evaluation import regrade
from reporting import archive, blind, build_report, seal, validate
from runtime import Server, execute
from test_benchmark import FakeServer, fixture


def v2_gold(gold):
    return gold | {"scoring_version": 2, "rubric": [
        {"id": "A", "dimension": "accuracy", "max": 50, "criterion": "已陈述的None/空值语义正确，无依据推断扣分"},
        {"id": "C", "dimension": "coverage", "max": 20, "criterion": "两个输入及验证建议均回应，遗漏在此扣分"},
        {"id": "I", "dimension": "insight", "max": 10, "criterion": "说明显式值与默认值边界的实际影响"},
        {"id": "R", "dimension": "readability", "max": 20, "criterion": "自然、简洁、清晰"}]}


class ScoringV2Tests(unittest.TestCase):
    def setUp(self):
        base = core.home() / "benchmark-validation"
        base.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="scoring-v2-unit-", dir=base)
        self.base = Path(self.temp.name)
        self.case, self.rates = fixture(self.base)
        self.root = core.prepare(self.case, self.base / "source", ["gpt-6-sol"], ["max"], 2, self.rates)
        FakeServer.failures, FakeServer.calls = set(), []
        execute(self.root, server_factory=FakeServer)
        blind(self.root)
        scores = core.read(self.root / "grading/packet/scores-template.json")
        for s in scores.values():
            s.update(points={"A1": 70, "R1": 29}, notes={"A1": "None与显式空值区别正确", "R1": "简洁，验证尚可更具体"})
        core.write(self.base / "old-scores.json", scores)
        seal(self.root, self.base / "old-scores.json")
        build_report(self.root)
        validate(self.root)
        self.rubric = self.base / "v2.json"
        core.write(self.rubric, v2_gold(core.read(self.case / "gold.json")))
        self.revision = self.base / "revision"

    def tearDown(self):
        self.temp.cleanup()

    def create_revision(self):
        regrade(self.root, self.rubric, self.revision)
        blind(self.revision)

    def scores(self, root=None):
        root = root or self.revision
        scores = core.read(root / "grading/packet/scores-template.json")
        for s in scores.values():
            s.update(points={"A": 50, "C": 20, "I": 5, "R": 19},
                     notes={"A": "给出的输入语义正确", "C": "两输入及验证都回应", "I": "给出默认值边界但未展开影响", "R": "简洁清楚"},
                     evidence={k: ["None 使用默认值"] for k in ["A", "C", "I", "R"]}, issues=[])
        return scores

    def complete_revision(self):
        self.create_revision()
        core.write(self.base / "new-scores.json", self.scores())
        seal(self.revision, self.base / "new-scores.json")
        return build_report(self.revision)

    def test_versions_weights_and_invalid_values(self):
        old = core.read(self.case / "gold.json")
        self.assertEqual(core.scoring_weights(old), {"accuracy": 70, "readability": 30})
        new = v2_gold(old)
        core.validate_gold(new)
        self.assertEqual(sum(core.scoring_weights(new).values()), 100)
        for version in [0, 3, "2", True, None]:
            with self.subTest(version=version), self.assertRaisesRegex(ValueError, "version"):
                core.validate_gold(new | {"scoring_version": version})
        for value in [49, float("inf"), float("nan"), True, -1]:
            changed = copy.deepcopy(new)
            changed["rubric"][0]["max"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                core.validate_gold(changed)
        changed = copy.deepcopy(new)
        changed["rubric"] = changed["rubric"][:-1]
        with self.assertRaises(ValueError):
            core.validate_gold(changed)

    def test_revision_preserves_source_and_metrics_and_omits_unrelated_files(self):
        (self.root / "unrelated-private.txt").write_text("not part of bundle", encoding="utf-8")
        before = {p.relative_to(self.root).as_posix(): core.sha(p) for p in self.root.rglob("*") if p.is_file()}
        data = self.complete_revision()
        self.assertEqual(data["scoring_version"], 2)
        self.assertEqual(data["conditions"][0]["statistics"]["quality"]["mean"], 94)
        self.assertEqual(data["previous_evaluation"]["conditions"][0]["statistics"]["quality"]["mean"], 99)
        self.assertFalse((self.revision / "unrelated-private.txt").exists())
        self.assertEqual(before, {p.relative_to(self.root).as_posix(): core.sha(p) for p in self.root.rglob("*") if p.is_file()})
        old_rows = core.read(self.root / "summary.json")["rows"]
        self.assertEqual([{k:r[k] for k in core.METRICS} for r in old_rows], [{k:r[k] for k in core.METRICS} for r in data["rows"]])
        validate(self.revision)
        result = archive(self.revision)
        self.assertGreater(result["files"], 15)
        page = (self.revision / "report.html").read_text(encoding="utf-8")
        self.assertIn("任务覆盖 /20", page)
        self.assertIn("事后新增细则", page)
        self.assertIn("逐份新旧总分对照", page)

    def test_new_v2_case_and_retest_keep_scoring_version(self):
        core.write(self.case / "gold.json", core.read(self.rubric))
        target = core.prepare(self.case, self.base / "fresh-v2", ["gpt-6-sol"], ["xhigh"], 1, self.rates)
        execute(target, server_factory=FakeServer)
        blind(target)
        core.write(self.base / "fresh-scores.json", self.scores(target))
        seal(target, self.base / "fresh-scores.json")
        data = build_report(target)
        validate(target)
        self.assertEqual(data["scoring_weights"], core.SCORING_WEIGHTS[2])
        child = retest(target, self.base / "retest", efforts=["xhigh"], prices=self.rates)
        self.assertEqual(core.scoring_gold(child)["scoring_version"], 2)
        self.assertEqual(core.sha(child / "input/gold.json"), core.sha(target / "input/gold.json"))

    def test_regrade_rejects_changed_facts_and_existing_destination(self):
        gold = core.read(self.rubric)
        gold["reference_facts"].append("unverified additional fact")
        core.write(self.base / "bad.json", gold)
        with self.assertRaisesRegex(ValueError, "preserve original"):
            regrade(self.root, self.base / "bad.json", self.revision)
        self.assertFalse(self.revision.exists())
        self.create_revision()
        with self.assertRaisesRegex(ValueError, "new sibling"):
            regrade(self.root, self.rubric, self.revision)

    def test_offline_revision_cannot_preflight_run_resume_or_retest(self):
        self.create_revision()
        factory = Mock(side_effect=AssertionError("must never construct runtime"))
        for resume in [False, True]:
            with self.assertRaisesRegex(ValueError, "Offline regrade"):
                execute(self.revision, resume=resume, retry_failed=True, server_factory=factory)
        factory.assert_not_called()
        with patch("runtime.discover_binary", side_effect=AssertionError("must not discover runtime")):
            with self.assertRaisesRegex(ValueError, "Offline regrade"):
                Server(self.revision)
        with self.assertRaisesRegex(ValueError, "Offline regrade"):
            retest(self.revision, self.base / "bad-child")

    def test_snapshot_rubric_manifest_and_answer_set_tampering_rejected(self):
        self.create_revision()
        targets = ["evaluation/rubric.json", "evaluation/manifest.json", "evaluation/previous-summary.json", "runs/0001/attempt-001/answer.txt"]
        for name in targets:
            p = self.revision / name
            original = p.read_bytes()
            p.write_bytes(original + b" ")
            # Manifest JSON whitespace alone leaves its semantic digest unchanged.
            if name.endswith("manifest.json"):
                value = core.read(p); value["note"] = "changed"; core.write(p, value)
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "changed"):
                core.check_inputs(self.revision)
            p.write_bytes(original)
        extra = self.revision / "runs/0001/attempt-001/unexpected.json"
        core.write(extra, {})
        with self.assertRaisesRegex(ValueError, "set changed"):
            core.check_inputs(self.revision)

    def test_packet_override_must_match_frozen_revision(self):
        self.create_revision()
        path = self.revision / "grading/packet/rubric.json"
        path.write_text("{}", encoding="utf-8")
        core.write(self.base / "new-scores.json", self.scores())
        with self.assertRaisesRegex(ValueError, "frozen revision"):
            seal(self.revision, self.base / "new-scores.json")

    def test_v2_evidence_issues_and_scores_checked(self):
        self.create_revision()
        good = self.scores()
        key = next(iter(good))
        changes = [lambda s:s[key]["points"].update(A=51), lambda s:s[key]["points"].pop("C"),
                   lambda s:s[key]["evidence"].update(A=["not in answer"]), lambda s:s[key]["evidence"].update(C=[]),
                   lambda s:s[key].pop("issues"), lambda s:s[key]["issues"].append({"kind":"error","severity":"critical","criterion":"A","quote":"invented","note":"bad"})]
        for change in changes:
            scores=copy.deepcopy(good); change(scores)
            with self.assertRaises(ValueError):
                core.validate_scores(scores, core.read(self.revision / "grading/private-mapping.json"), self.revision / "grading/packet", core.scoring_gold(self.revision))

    def test_issue_counts_and_zero_score_omission_survive_export(self):
        self.create_revision()
        scores = self.scores()
        first = next(iter(scores))
        scores[first]["points"].update(A=45, C=0)
        scores[first]["evidence"]["C"] = []
        scores[first]["notes"]["C"] = "未覆盖验证步骤，测试空摘录遗漏记录"
        scores[first]["issues"] = [
            {"kind":"error","severity":"critical","criterion":"A","quote":"None 使用默认值","note":"受控测试的问题标记，用于验证计数和导出"},
            {"kind":"omission","severity":"minor","criterion":"C","quote":"","note":"测试遗漏可没有直接引文"}]
        core.write(self.base/"issue-scores.json",scores)
        seal(self.revision,self.base/"issue-scores.json")
        data=build_report(self.revision)
        validate(self.revision)
        self.assertEqual(data["conditions"][0]["critical_errors"],1)
        self.assertEqual(data["conditions"][0]["error_answers"],1)
        self.assertEqual(data["conditions"][0]["omission_answers"],1)
        self.assertEqual(sum(r["critical_errors"] for r in data["rows"]),1)
        self.assertEqual(sum(len(s["issues"]) for s in data["grading_details"].values()),2)

    def test_v2_export_and_sealed_score_changes_rejected(self):
        self.complete_revision()
        for name, change in [("summary.json", lambda p:core.write(p,core.read(p)|{"scoring_version":1})),
                             ("metrics.csv", lambda p:p.write_text(p.read_text(encoding="utf-8-sig").replace(",20,10,20,",",999,10,20,"),encoding="utf-8-sig")),
                             ("report.html", lambda p:p.write_text(p.read_text(encoding="utf-8").replace("任务覆盖 /20","任务覆盖 /200"),encoding="utf-8"))]:
            p=self.revision/name; content=p.read_bytes();change(p)
            self.assertNotEqual(content,p.read_bytes())
            with self.subTest(name=name),self.assertRaises(ValueError):validate(self.revision)
            p.write_bytes(content)
        scores_path=self.revision/"grading/scores.json"
        scores=core.read(scores_path);scores[next(iter(scores))]["points"]["A"]=49;core.write(scores_path,scores)
        with self.assertRaisesRegex(ValueError,"Sealed scores"):
            validate(self.revision)

    def test_regrade_cli_shape(self):
        args=parser().parse_args(["regrade","--run-dir",str(self.root),"--rubric",str(self.rubric),"--output",str(self.revision)])
        self.assertEqual(args.command,"regrade")


if __name__ == "__main__":
    unittest.main()
