"""Offline repair regressions; all native events and scores are synthetic."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import core
import final_repair
from agentic import TreeLedger
from evaluation import regrade
from reporting import blind, build_report, seal, validate, validate_records
from runtime import Server, execute
from test_agentic import usage
from test_benchmark import fixture
from test_scoring_v2 import v2_gold


ANSWER = "None 使用默认值；空字符串保持原值。建议分别断言这两种输入。"


class FinalAnswerRepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="final-repair-unit-")
        self.base = Path(self.temp.name).resolve()
        self.case, self.prices = fixture(self.base)
        core.write(self.case / "gold.json", v2_gold(core.read(self.case / "gold.json")))
        self.rubric = self.case / "gold.json"
        source = self.base / "project-source"
        source.mkdir()
        (source / "config.py").write_bytes((self.case / "evidence/S1.txt").read_bytes())
        self.root = core.prepare(self.case, self.base / "source", [core.MODELS[0]], ["max"], 3,
                                 self.prices, execution_profile={"source_roots": [
                                     {"path": str(source), "logical_path": "project/src"}],
                                     "model_work_root": str(self.base / "model-work")})
        self.protocol = core.check_inputs(self.root)
        for index, slot in enumerate(self.protocol["schedule"]):
            self.write_native_attempt(slot, index)
        self.grade(self.root)
        build_report(self.root)
        validate_records(self.root)
        self.revision = self.base / "revision"

    def tearDown(self):
        self.temp.cleanup()

    def write_native_attempt(self, slot, mode, *, authoritative=True, valid=True):
        tid, turn = "root-" + slot["id"], "turn-" + slot["id"]
        effective = {"model": slot["model"], "reasoningEffort": slot["effort"],
                     "serviceTier": "default", "modelProvider": "test"}
        ledger = TreeLedger(tid, turn, effective, final_answer_policy="legacy")

        def event(method, time, **params):
            ledger.observe({"method": method, "params": {"threadId": tid, "turnId": turn, **params}}, time)

        def final(item_id, text, time, completed=True):
            item = {"type": "agentMessage", "id": item_id, "phase": "final_answer"}
            event("item/started", time, item=item)
            event("item/agentMessage/delta", time + .1, itemId=item_id, delta=text)
            if completed:
                event("item/completed", time + .5, item=item | {"text": text})
            return item | {"text": text}

        event("rawResponse/completed", 4, responseId="response-" + slot["id"], usage=usage())
        if valid:
            if mode == 0:
                final("abandoned", "未完成的草稿", 1, completed=False)
            elif mode == 1:
                final("duplicate-first", ANSWER, 2)
            canonical = final("final", ANSWER, 5)
            if mode == 2:
                event("item/completed", 6.2,
                      item={"type": "agentMessage", "id": "empty-tail", "phase": "final_answer", "text": ""})
            completion = {"id": turn, "status": "completed"}
            if authoritative:
                completion["items"] = [canonical]
            event("turn/completed", 6.5, turn=completion)
        else:
            event("turn/completed", 6.5, turn={"id": turn, "status": "failed"})
        ledger.reconcile_thread({"id": tid, "status": {"type": "idle"}}, 9)
        ledger.mark_discovery_complete(10)
        directory = self.root / "runs" / slot["id"] / "attempt-001"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "answer.txt").write_text(ledger.final_answer, encoding="utf-8", newline="")
        (directory / "events.jsonl").write_text(
            "\n".join(json.dumps(event, ensure_ascii=False) for event in ledger.events) + "\n",
            encoding="utf-8", newline="")
        snapshot = ledger.snapshot()
        core.write(directory / "task-tree.json", snapshot)
        result = {"slot_id": slot["id"], "attempt": 1, "model": slot["model"], "effort": slot["effort"],
                  "repetition": slot["repetition"], "thread_id": tid, "valid": valid,
                  "status": "completed" if valid else "failed", "effective": effective,
                  "usage": None, "warnings": [], "tool_items": [], "completed_at": "2026-10-27T01:02:03Z",
                  "prompt_sha256": core.sha(self.root / "input/prompt.txt"),
                  "answer_sha256": core.sha(directory / "answer.txt"), "agentic": snapshot,
                  **ledger.metrics(core.read(self.prices), self.protocol, 10)}
        core.write(directory / "result.json", result)

    def grade(self, root):
        blind(root)
        scores = core.read(root / "grading/packet/scores-template.json")
        for score in scores.values():
            score.update(points={"A": 50, "C": 20, "I": 0, "R": 19},
                         notes={"A": "输入语义正确", "C": "两个输入及验证均回应", "I": "未说明影响", "R": "简洁清楚"},
                         evidence={"A": ["None 使用默认值"], "C": ["建议分别断言"], "I": [], "R": ["空字符串保持原值"]},
                         issues=[])
        path = self.base / (root.name + "-scores.json")
        core.write(path, scores)
        seal(root, path)

    def hashes(self, root):
        return {path.relative_to(root).as_posix(): core.sha(path)
                for path in root.rglob("*") if path.is_file()}

    def create_revision(self):
        with patch("runtime.Server", side_effect=AssertionError("must not create runtime")), \
                patch("runtime.execute", side_effect=AssertionError("must not sample")):
            return regrade(self.root, self.rubric, self.revision, repair_final_answers=True)

    def test_repair_preserves_source_and_history_and_recomputes_final_metrics(self):
        before = self.hashes(self.root)
        code_before = self.hashes(self.base / "project-source")
        source_results = core.all_attempts(self.root)
        info = self.create_revision()
        self.assertEqual(before, self.hashes(self.root))
        self.assertFalse(info["inference_started"])
        metadata = info["final_answer_repair"]
        self.assertEqual(metadata["attempts_replayed"], 3)
        self.assertEqual(metadata["changed_answers"], 3)
        self.assertEqual(metadata["substantive_text_changes"], 2)
        self.assertEqual(metadata["outer_whitespace_changes"], 1)
        self.assertEqual(metadata["new_inference_calls"], 0)
        self.assertEqual(metadata["added_api_usd"], 0)
        self.assertTrue(metadata["costs_are_historical"])
        for old, revised in zip(source_results, core.all_attempts(self.revision)):
            directory = Path(old["relative_result"]).parent
            self.assertEqual((self.revision / directory / "answer.txt").read_text(encoding="utf-8"), ANSWER)
            self.assertEqual(revised["agentic"], core.read(self.revision / directory / "task-tree.json"))
            self.assertEqual(revised["agentic"]["final_answer_policy"], final_repair.FINAL_POLICY)
            self.assertEqual(revised["completed_at"], old["completed_at"])
            self.assertEqual(revised["usage"], old["usage"])
            self.assertEqual(revised["agentic"]["events"], old["agentic"]["events"])
            for name in ("result.json", "answer.txt", "events.jsonl", "task-tree.json"):
                relative = directory / name
                self.assertEqual((self.root / relative).read_bytes(),
                                 (self.revision / final_repair.SOURCE_RECORD_PREFIX / relative).read_bytes())
            self.assertEqual((self.root / directory / "events.jsonl").read_bytes(),
                             (self.revision / directory / "events.jsonl").read_bytes())
            for key, value in old.items():
                if key.endswith("_tokens") or key.endswith("_api_usd") or key in {
                        "elapsed_s", "workflow_elapsed_s", "root_turn_completed_s", "delegation_count"}:
                    self.assertEqual(revised[key], value, key)
        self.assertEqual(core.all_attempts(self.revision)[2]["answer_ready_s"], 5.5)
        self.assertEqual(core.sha(self.root / "grading/scores.json"),
                         core.sha(self.revision / "evaluation/previous-grading/scores.json"))
        validate_records(self.revision)
        self.grade(self.revision)
        data = build_report(self.revision)
        self.assertEqual(data["attempt_count"], 3)
        self.assertEqual(data["known_cost_usd"], core.read(self.root / "summary.json")["known_cost_usd"])
        self.assertEqual(data["evaluation"]["new_inference_calls"], 0)
        report = (self.revision / "report.html").read_text(encoding="utf-8")
        self.assertIn("提取修复前评分", report)
        self.assertIn("分差可能来自文本恢复、评分细则与重新评阅", report)
        self.assertNotIn("同一批答案；分数变化来自评分口径", report)
        self.assertTrue(validate(self.revision)["passed"])
        self.assertEqual(before, self.hashes(self.root))
        self.assertEqual(code_before, self.hashes(self.base / "project-source"))

    def test_revision_is_bound_to_provenance_code_originals_and_final_files(self):
        self.create_revision()
        targets = [final_repair.PROVENANCE_PATH,
                   final_repair.CODE_PREFIX + "/final_repair.py",
                   final_repair.SOURCE_RECORD_PREFIX + "/runs/0001/attempt-001/result.json",
                   "runs/0001/attempt-001/answer.txt", "runs/0001/attempt-001/task-tree.json"]
        for name in targets:
            path = self.revision / name
            content = path.read_bytes()
            path.write_bytes(content + b" ")
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "changed"):
                core.check_inputs(self.revision)
            path.write_bytes(content)
        core.check_inputs(self.revision)

    def test_offline_revision_refuses_runtime_and_resampling(self):
        self.create_revision()
        factory = Mock(side_effect=AssertionError("must not instantiate runtime"))
        with self.assertRaisesRegex(ValueError, "Offline regrade"):
            execute(self.revision, resume=True, retry_failed=True, server_factory=factory)
        factory.assert_not_called()
        with patch("runtime.discover_binary", side_effect=AssertionError("must not locate runtime")):
            with self.assertRaisesRegex(ValueError, "Offline regrade"):
                Server(self.revision)
        with self.assertRaisesRegex(ValueError, "Offline regrade"):
            core.require_sampling_allowed(self.revision)

    def test_missing_authoritative_completion_refuses_before_destination_exists(self):
        # Regenerate a complete, valid historical ledger whose old extraction
        # succeeded, but whose completion receipt cannot prove the final items.
        slot = self.protocol["schedule"][0]
        self.write_native_attempt(slot, 0, authoritative=False)
        before = self.hashes(self.root)
        with self.assertRaisesRegex(ValueError, "authoritative final"):
            self.create_revision()
        self.assertFalse(self.revision.exists())
        self.assertEqual(before, self.hashes(self.root))

    def test_missing_completion_event_is_rejected_without_guessing(self):
        result_path = self.root / "runs/0001/attempt-001/result.json"
        result = core.read(result_path)
        snapshot = result["agentic"]
        snapshot["events"] = [event for event in snapshot["events"] if event["method"] != "turn/completed"]
        # Direct preparation proves the completion check; the public regrade
        # additionally requires the original sealed summary to remain intact.
        core.write(result_path, result)
        events_path = result_path.with_name("events.jsonl")
        events_path.write_text("\n".join(json.dumps(event) for event in snapshot["events"]), encoding="utf-8")
        core.write(result_path.with_name("task-tree.json"), snapshot)
        with self.assertRaisesRegex(ValueError, "replay|completion|completed root"):
            final_repair.prepare_final_answer_repair(self.root, self.protocol)
        self.assertFalse(self.revision.exists())

    def test_event_snapshot_disagreement_is_rejected_before_output(self):
        path = self.root / "runs/0001/attempt-001/events.jsonl"
        path.write_bytes(path.read_bytes() + b"{}\n")
        with self.assertRaisesRegex(ValueError, "events differ"):
            self.create_revision()
        self.assertFalse(self.revision.exists())

    def test_source_change_after_analysis_refuses_before_output(self):
        prepare = final_repair.prepare_final_answer_repair

        def change_after_plan(*args):
            plan = prepare(*args)
            path = self.root / "runs/0001/attempt-001/events.jsonl"
            path.write_bytes(path.read_bytes() + b"\n")
            return plan

        with patch("final_repair.prepare_final_answer_repair", side_effect=change_after_plan):
            with self.assertRaisesRegex(ValueError, "Source changed after"):
                self.create_revision()
        self.assertFalse(self.revision.exists())

    def test_missing_historical_cost_metric_is_not_injected(self):
        path = self.root / "runs/0001/attempt-001/result.json"
        value = core.read(path)
        value.pop("tree_estimated_api_usd")
        core.write(path, value)
        with self.assertRaisesRegex(ValueError, "missing historical metric"):
            final_repair.prepare_final_answer_repair(self.root, self.protocol)

    def test_historical_cost_or_workflow_change_is_rejected(self):
        path = self.root / "runs/0001/attempt-001/result.json"
        original = core.read(path)
        for key in ("tree_estimated_api_usd", "workflow_elapsed_s", "first_progress_s", "accounting_complete"):
            value = dict(original)
            value[key] = -1
            core.write(path, value)
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "historical usage, cost or workflow"):
                final_repair.prepare_final_answer_repair(self.root, self.protocol)
        core.write(path, original)

    def test_unchanged_canonical_answers_are_replayed_and_recorded(self):
        self.write_native_attempt(self.protocol["schedule"][2], 3)
        plan = final_repair.prepare_final_answer_repair(self.root, self.protocol)
        self.assertEqual(plan["metadata"]["attempts_replayed"], 3)
        self.assertEqual(plan["metadata"]["changed_answers"], 2)
        unchanged = plan["provenance"]["attempts"][2]
        self.assertEqual(unchanged["change_kind"], "none")
        self.assertEqual(unchanged["source_answer_sha256"], unchanged["canonical_answer_sha256"])

    def test_failed_attempt_is_copied_without_repair_or_new_attempt(self):
        # The failed slot remains settled and contributes its historical cost.
        self.write_native_attempt(self.protocol["schedule"][2], 2, valid=False)
        # Start a new synthetic sealed grading after changing the fixture.
        import shutil
        shutil.rmtree(self.root / "grading")
        self.grade(self.root)
        build_report(self.root)
        info = self.create_revision()
        self.assertEqual(info["final_answer_repair"]["attempts_replayed"], 2)
        for path in (self.root / "runs/0003/attempt-001").iterdir():
            self.assertEqual(path.read_bytes(), (self.revision / path.relative_to(self.root)).read_bytes())
        self.assertEqual(len(core.all_attempts(self.revision)), 3)
        validate_records(self.revision)

    def test_default_regrade_preserves_legacy_four_file_evidence(self):
        info = regrade(self.root, self.rubric, self.revision)
        self.assertNotIn("final_answer_repair", info)
        for path in (self.root / "runs").rglob("*"):
            if path.is_file():
                self.assertEqual(path.read_bytes(), (self.revision / path.relative_to(self.root)).read_bytes())
        validate_records(self.revision)


if __name__ == "__main__":
    unittest.main()
