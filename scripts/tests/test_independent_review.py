"""Independent reviewer isolation, durable launch and native-source invariants."""
from pathlib import Path
import copy
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import antigravity as agy
import core
import independent_review as review
import paired
from app_capture import validate_answer_entry
from benchmark import parser
from test_antigravity import FakeProcess, native_events
from fresh_blind import prepare_topic, check_topic_seal
from evaluation import formal_v3_gold, v4_gold_from_v3
from test_scoring_v3 import v3_gold


class IndependentReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def native_capture(self, response="无法回答", events=None, guard=False):
        root = self.root / "transport"; (root / "input").mkdir(parents=True)
        (root / "input/prompt.txt").write_text("question", encoding="utf-8")
        core.write(root / "input/instructions.json", {"base": None, "developer": None})
        proto = {"runtime_backend": "antigravity", "execution_profile": {"mode": agy.FULL_MODE}}
        core.write(root / "protocol.json", proto)
        slot = {"id": "0001", "model": "gemini-4-neon-xhigh", "effort": "xhigh", "repetition": 1}
        with patch.object(agy.subprocess, "Popen", return_value=FakeProcess(events or native_events(response=response, permission_mode="always-proceed"))) as spawn:
            record = agy.run_trial(root, proto, slot, 1, Path(sys.executable), {}, self.root / "scratch", prompt_override="question", new_project=True, delegation_guard=guard)
        self.assertIn("--new-project", spawn.call_args.args[0]); self.assertIn("--dangerously-skip-permissions", spawn.call_args.args[0])
        if guard:
            self.assertEqual(spawn.call_args.kwargs["env"]["CODEX_PEER_ACTIVE"], "1")
            self.assertEqual(spawn.call_args.kwargs["env"]["PEER_ANALYSIS_ACTIVE"], "1")
        else:
            self.assertIsNone(spawn.call_args.kwargs["env"])
        return root, root / "runs/0001/attempt-001", record

    def test_successful_nonempty_low_quality_response_is_not_resampled(self):
        root, d, record = self.native_capture()
        self.assertTrue(record["transport_valid"])
        entry = agy.capture_terminal(root, d)
        self.assertEqual(entry["answer_source"], "antigravity-terminal-v1")
        self.assertNotIn("answer_receipt", entry)
        self.assertEqual(validate_answer_entry(entry)["answer_sha256"], core.sha(d / "answer.txt"))
        self.assertEqual((d / "answer.txt").read_text(encoding="utf-8"), "无法回答")
        self.assertEqual(agy.capture_terminal(root, d), entry)

    def test_native_terminal_and_receipt_cannot_be_replaced(self):
        root, d, _ = self.native_capture("valid final")
        entry = agy.capture_terminal(root, d)
        (d / "answer.txt").write_text("different", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "bytes changed"):
            validate_answer_entry(entry)

    def test_wrong_native_model_is_rejected_even_with_a_terminal_answer(self):
        events = native_events(model="wrong", permission_mode="always-proceed")
        root, d, record = self.native_capture(events=events)
        self.assertFalse(record["transport_valid"])
        with self.assertRaisesRegex(ValueError, "successful"):
            agy.capture_terminal(root, d)

    def test_native_line_endings_are_bound_verbatim(self):
        root, d, _ = self.native_capture("start\r\nline\nend")
        entry = agy.capture_terminal(root, d)
        self.assertEqual(Path(entry["answer"]).read_bytes(), b"start\r\nline\nend")
        agy.validate_terminal_capture(entry["cli_answer_receipt"])

    def test_per_process_guard_does_not_change_parent_environment(self):
        import os
        before = dict(os.environ)
        _, _, record = self.native_capture(guard=True)
        self.assertEqual(record["delegation_guard"]["CODEX_PEER_ACTIVE"], "1")
        self.assertEqual(dict(os.environ), before)

    def test_interference_uses_actual_commands_not_a_skill_listing_or_negative_audit(self):
        d = self.root / "events"; d.mkdir()
        def audit(command, tool="run_command"):
            core.write(d / "event.json", {})
            event = {"event": "step_update", "step_update": {"step_index": 1, "tool_name": tool,
                "tool_info": {"parameters": {"CommandLine": command}}}}
            (d / "events.jsonl").write_text(__import__('json').dumps(event), encoding="utf-8")
            return review.audit_interference(d)
        self.assertFalse(audit("python -c \"print('No Codex usage')\"")["affected"])
        self.assertTrue(audit("codex exec --model gpt-6.1-sol task")["affected"])
        self.assertTrue(audit("python C:/tools/codex_peer.py ask --request request.json")["affected"])
        self.assertTrue(audit("C:/skills/codex-peer/SKILL.md", "read_file")["affected"])

    def test_user_hold_blocks_only_new_neon_positions(self):
        self.make_dispatch()
        core.write(self.root / "sampling-hold.json", {"status": "held", "arms": ["b"]})
        with patch.object(review, "cli_quota", side_effect=AssertionError("Held jobs must not query or launch")):
            jobs = paired.dispatch(self.root)["ready"]
        self.assertEqual([j["backend"] for j in jobs], ["app", "app"])

    def test_conditional_parallel_expansion_waits_for_the_entire_other_arm(self):
        units = self.make_dispatch()
        core.write(self.root / "concurrency-override.json", {"authorization": "User approved expansion after Neon completes.",
            "max_active_per_arm": {"a": 5, "b": 3}, "activate_after_completed_arm": {"arm": "b", "count": 4}})
        for u in units:
            if u.endswith("-b"):
                s = paired.state(self.root, u); s["status"] = "completed"; paired.save_state(self.root, u, s)
        with patch.object(review, "cli_quota", side_effect=AssertionError("No new CLI calls")):
            jobs = paired.dispatch(self.root)["ready"]
        self.assertEqual(len(jobs), 4)
        self.assertTrue(all(j["backend"] == "app" for j in jobs))
        self.assertEqual(core.read(self.root / "concurrency-activation.json")["effective_limits"]["a"], 5)
        self.assertEqual(core.read(self.root / "protocol.json")["max_answer_concurrency"]["a"], 2)

    def make_dispatch(self):
        units = []
        for n in range(1, 5):
            pair = f"r1-t{n}-m1"; baseline = self.root / (pair + ".txt"); baseline.write_text("draft")
            for arm, backend in (("a", "app"), ("b", "antigravity")):
                u = pair + "-" + arm; units.append(u); d = self.root / "units" / u; d.mkdir(parents=True)
                (d / "prompt.txt").write_text("review __REVIEW_SCRATCH__")
                paired.save_state(self.root, u, {"unit": u, "pair_id": pair, "arm": arm, "backend": backend,
                    "status": "pending", "attempt": 1, "model": "reviewer", "effort": "max", "prompt_sha256": core.sha(d / "prompt.txt")})
        p = {"design": review.DESIGN, "units": units, "pairs": [{"pair_id": f"r1-t{n}-m1", "topic": paired.TOPICS[n-1],
            "draft_view": str(self.root / f"r1-t{n}-m1.txt"), "draft_view_sha256": core.sha(self.root / f"r1-t{n}-m1.txt")} for n in range(1,5)],
            "baselines": 20, "new_revisions": 40, "grading_packets": 4, "work_root": str(self.root / "work"),
            "max_answer_concurrency": {"a": 2, "b": 5}}
        core.write(self.root / "protocol.json", p)
        core.write(self.root / "preparation-seal.json", {"protocol_sha256": core.sha(self.root / "protocol.json")})
        return units

    def test_parallel_arm_limits_and_durable_intents(self):
        units = self.make_dispatch()
        with patch.object(review, "cli_quota", return_value={"eligible": True}):
            jobs = paired.dispatch(self.root)["ready"]
            self.assertEqual(Counter(j["backend"] for j in jobs), {"app": 2, "antigravity": 4})
            with self.assertRaisesRegex(ValueError, "ambiguous"):
                paired.dispatch(self.root)
        cli = next(j for j in jobs if j["backend"] == "antigravity")
        with self.assertRaisesRegex(ValueError, "native terminal"):
            paired.register(self.root, cli["unit"], "fake-app")

    def test_zero_quota_holds_only_cli_and_does_not_start_it(self):
        self.make_dispatch()
        with patch.object(review, "cli_quota", return_value={"eligible": False}):
            jobs = paired.dispatch(self.root)["ready"]
        self.assertEqual([j["backend"] for j in jobs], ["app", "app"])

    def test_success_and_unknown_inflight_cannot_be_retried(self):
        units = self.make_dispatch(); u = units[1]
        for status in ("completed", "unknown-inflight", "running"):
            s = paired.state(self.root, u); s["status"] = status; paired.save_state(self.root, u, s)
            with self.assertRaisesRegex(ValueError, "verified terminal"):
                review.retry(self.root, u)

    def test_cli_parser_preserves_legacy_mode_and_supports_independent_selection(self):
        base = ["paired-prepare", "--selection", "selection", "--comparison", "comparison", "--batch-dir", "batch"]
        self.assertEqual(parser().parse_args(base + ["--pilot", "pilot"]).design, "matched-ab")
        value = parser().parse_args(base + ["--design", "independent-reviewers", "--source-model", "neon", "--reviewers", "reviewers"])
        self.assertEqual(value.source_model, "neon"); self.assertIsNone(value.pilot)

    def test_external_grader_packet_is_opaque_and_cannot_point_to_model_named_batch(self):
        self.make_dispatch(); opaque = "0123456789abcdef01234567"
        with patch.object(paired, "home", return_value=self.root / "home"):
            neutral = self.root / "home/benchmarks/blind-context" / opaque
            self.assertEqual(paired.grade_packet(self.root, {"external_packet": str(neutral), "anonymous_packet_id": opaque}), neutral)
            with self.assertRaisesRegex(ValueError, "neutral opaque"):
                paired.grade_packet(self.root, {"external_packet": str(self.root / "neon-review" / opaque), "anonymous_packet_id": opaque})

    def test_mixed_packet_seal_rechecks_native_cli_binding(self):
        root, d, _ = self.native_capture("correct evidence")
        entry = agy.capture_terminal(root, d)
        source = self.root / "source"; (source / "input").mkdir(parents=True)
        for n in ("question.txt", "prompt.txt"):
            (source / "input" / n).write_text("question", encoding="utf-8")
        gold = formal_v3_gold(v4_gold_from_v3(v3_gold({"reference_facts": ["fixture"], "verification": ["fixture"]})))
        core.write(self.root / "gold.json", gold)
        packet, mapping = self.root / "packet", self.root / "private.json"
        prepare_topic([{**entry, "identity": "one", "source": str(source)}], self.root / "gold.json", packet, mapping)
        self.assertEqual(core.read(packet / "input-seal.json")["source_validation"]["policy"], "mixed-final-answer-v1")
        self.assertNotIn("antigravity", (packet / "answers/A001.txt").read_text(encoding="utf-8"))

    def test_reconcile_cli_cleans_leftover_ephemeral_project_on_verified_terminal(self):
        units = self.make_dispatch()
        unit = next(u for u in units if u.endswith("-b"))
        transport, attempt, _ = self.native_capture("revised answer")
        scratch = self.root / "scratch"
        state = paired.state(self.root, unit)
        state.update(status="running", pid=999999999, process_identity="dead",
                     transport_root=str(transport), scratch=str(scratch))
        paired.save_state(self.root, unit, state)
        projects = self.root / "agy-projects"
        projects.mkdir()
        leftover = projects / "leftover-uuid.json"
        core.write(leftover, {"id": "leftover-uuid", "name": scratch.name,
                              "projectResources": {"resources": [{"folderUri": "file://" + scratch.resolve().as_posix()}]}})
        unrelated = projects / "other-uuid.json"
        core.write(unrelated, {"id": "other-uuid", "name": "other",
                               "projectResources": {"resources": [{"folderUri": "file://" + (self.root / "other").resolve().as_posix()}]}})
        with patch.object(agy, "projects_config_dir", return_value=projects), \
             patch.object(review, "still_running", return_value=False):
            result = review.reconcile_cli(self.root, unit)
        self.assertEqual(result["status"], "completed")
        self.assertFalse(leftover.exists())
        self.assertTrue(unrelated.exists())


from collections import Counter

if __name__ == "__main__":
    unittest.main()
