"""Native workflow transport regression with deterministic fake RPC only."""
import collections
import contextlib
import copy
import io
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agentic_runtime
import benchmark
from core import BenchmarkError, check_inputs, prepare, read, write
from runtime import RpcRejected
from test_agentic import usage
from test_benchmark import fixture


def event(method, tid="root", turn_id="root-turn", **params):
    return {"method": method, "params": {"threadId": tid, "turnId": turn_id, **params}}


def completed_events(*, child=False, explicit_phase=True):
    result = []
    if child:
        result += [event("item/completed", item={"type": "subAgentActivity", "id": "spawn-child", "kind": "started", "agentThreadId": "child", "model": "gpt-6-astra", "reasoningEffort": "xhigh"}),
                   event("thread/settings/updated", "child", None, threadSettings={"model": "gpt-6-astra", "serviceTier": "default", "effort": "xhigh", "modelProvider": "test"}),
                   event("turn/started", "child", "child-turn", turn={"id": "child-turn", "status": "inProgress"})]
    result += [event("item/completed", item={"type": "commandExecution", "id": "search-1", "status": "completed", "command": "rg choose", "aggregatedOutput": "PRIVATE TOOL PAYLOAD"}),
               event("item/started", item={"type": "agentMessage", "id": "progress", "phase": "commentary"}),
               event("item/agentMessage/delta", itemId="progress", delta="正在核验。"),
               event("item/completed", item={"type": "agentMessage", "id": "progress", "phase": "commentary", "text": "正在核验。"}),
               event("rawResponse/completed", responseId="root-response", usage=usage()),
               event("item/started", item={"type": "agentMessage", "id": "answer", **({"phase": "final_answer"} if explicit_phase else {})}),
               event("item/agentMessage/delta", itemId="answer", delta="None使用默认值，空串保留。"),
               event("item/completed", item={"type": "agentMessage", "id": "answer", "text": "None使用默认值，空串保留。", **({"phase": "final_answer"} if explicit_phase else {})}),
               event("turn/completed", turn={"id": "root-turn", "status": "completed", "items": [
                   {"type": "agentMessage", "id": "answer", "text": "None使用默认值，空串保留。", **({"phase": "final_answer"} if explicit_phase else {})}]})]
    if child:
        result += [event("rawResponse/completed", "child", "child-turn", responseId="child-response", usage=usage(80, 10)),
                   event("turn/completed", "child", "child-turn", turn={"id": "child-turn", "status": "completed"})]
    return result


class NativeFakeServer:
    """A root completes first; descendant discovery observes real remaining work."""
    def __init__(self, root, item, messages, *, failure=None):
        self.root = root
        self.provider = "test"
        self.fingerprint = {"global_instructions_sha256": None, "test": "no-inference"}
        self.proc = SimpleNamespace(pid=os.getpid())
        self.counts = collections.Counter()
        self.pending = []
        self.active = False
        self.item = item
        self.profile = check_inputs(root)["execution_profile"]
        self.active_permission_id = self.profile["permission_profile_id"]
        messages = copy.deepcopy(messages)
        for message in messages:
            settings = message.get("params", {}).get("threadSettings")
            if settings is not None:
                settings.update(activePermissionProfile={"id": self.active_permission_id}, approvalPolicy="never", cwd=self.profile["work_cwd"])
        self.messages = collections.deque(messages)
        self.calls = []
        self.delivered = []
        self.child_done = False
        self.child_loaded = True
        self.rollout = root.parent / "fake-native-rollout.jsonl"
        self.rollout.touch()
        self.child_receipt = {"model": "gpt-6-astra", "modelProvider": "test", "reasoningEffort": "xhigh", "serviceTier": "default",
                              "activePermissionProfile": {"id": self.active_permission_id}, "approvalPolicy": "never", "cwd": self.profile["work_cwd"]}
        self.child_exists = any(x.get("params", {}).get("item", {}).get("agentThreadId") == "child" for x in messages)
        self.failure = failure
        self.background_rounds = 0
        self.background_calls = 0
        self.empty_poll_calls = 0

    def rpc(self, method, params):
        self.calls.append((method, params))
        if self.failure and method == self.failure[0]:
            raise self.failure[1]
        if method == "thread/start":
            return {"thread": {"id": "root"}, "model": self.item["model"], "modelProvider": "test", "reasoningEffort": self.item["effort"], "serviceTier": self.item.get("service_tier", "default"),
                    "activePermissionProfile": {"id": self.active_permission_id}, "approvalPolicy": "never", "cwd": self.profile["work_cwd"], "instructionSources": []}
        if method == "turn/start":
            return {"turn": {"id": "root-turn", "status": "inProgress"}}
        if method == "thread/list":
            return {"data": [self.thread("child")] if self.child_exists else [], "nextCursor": None}
        if method == "thread/loaded/list":
            return {"data": ["root", "child"] if self.child_exists and self.child_loaded else ["root"]}
        if method == "thread/resume":
            assert params == {"threadId": "child", "excludeTurns": True}
            assert self.child_loaded
            return {"thread": self.thread("child"), **self.child_receipt}
        if method == "thread/read":
            return {"thread": self.thread(params["threadId"])}
        if method == "thread/backgroundTerminals/list":
            self.background_calls += 1
            return {"data": [{"itemId": "background-tool", "processId": "background-1"}] if self.background_calls <= self.background_rounds else [], "nextCursor": None}
        raise AssertionError("Unexpected RPC " + method)

    def thread(self, tid):
        return {"id": tid, "path": str(self.rollout), **({"parentThreadId": "root"} if tid == "child" else {}),
                "status": {"type": "active" if tid == "child" and not self.child_done else "idle"}}

    def event(self):
        if not self.messages:
            raise ConnectionError("fake stream ended")
        message = self.messages.popleft()
        self.delivered.append(message)
        if message.get("params", {}).get("threadId") == "child" and message.get("method") == "turn/completed":
            self.child_done = True
        return time.perf_counter(), message

    def poll_event(self, *args, **kwargs):
        return self.event()


class AgenticRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.case, self.prices = fixture(self.base)
        self.empty_home = self.base / "empty-home"
        self.empty_home.mkdir()
        self.root = prepare(self.case, self.base / "run", ["gpt-6.1-sol"], ["ultra"], 1, self.prices,
                            service_tiers=["default"], execution_profile={"source_roots": [{"path": str(self.case / "evidence"), "logical_path": "project/src"}], "model_work_root": str(self.base / "model-work")})
        self.protocol = check_inputs(self.root)
        self.item = self.protocol["schedule"][0]

    def run_fake(self, messages=None, failure=None, capability=False):
        server = NativeFakeServer(self.root, self.item, completed_events() if messages is None else messages, failure=failure)
        with patch("agentic_runtime.home", return_value=self.empty_home), patch("execution.require_permission_probe"), patch("agentic_runtime.require_capability"):
            result = agentic_runtime.run_trial(server, self.item, 1, capability=capability)
        return result, server

    def test_native_tools_valid_zero_children_and_only_final_scored(self):
        result, server = self.run_fake()
        self.assertTrue(result["valid"], result)
        self.assertEqual(result["status"], "completed")
        self.assertIn("commandExecution", result["tool_items"])
        self.assertEqual(result["delegation_count"], 0)
        self.assertTrue(result["accounting_complete"])
        answer = (self.root / "runs/0001/attempt-001/answer.txt").read_text(encoding="utf-8")
        self.assertEqual(answer, "None使用默认值，空串保留。")
        self.assertNotIn("正在核验", answer)
        self.assertFalse(server.active)
        self.assertFalse((self.root / "inflight.json").exists())
        params = server.calls[0][1]
        self.assertFalse(params["ephemeral"])
        self.assertTrue(params["experimentalRawEvents"])
        self.assertIn("permissions", params)
        self.assertNotIn("sandbox", params)

    def test_root_completion_does_not_drop_late_child_usage(self):
        result, server = self.run_fake(completed_events(child=True))
        self.assertTrue(result["valid"], result)
        self.assertTrue(server.child_done)
        self.assertEqual(result["delegation_count"], 1)
        self.assertEqual(result["root_input_tokens"], 100)
        self.assertEqual(result["child_input_tokens"], 80)
        self.assertEqual(result["tree_input_tokens"], 180)
        self.assertTrue(result["accounting_complete"])
        self.assertGreaterEqual(sum(m == "thread/list" for m, _ in server.calls), 3)
        self.assertEqual(server.delivered[-1]["params"]["threadId"], "child")
        events = (self.root / "runs/0001/attempt-001/events.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("PRIVATE TOOL PAYLOAD", events)

    def test_rpc_pending_notifications_are_consumed_fifo_before_live_stream(self):
        from agentic import TreeLedger
        server = NativeFakeServer(self.root, self.item, completed_events(child=True))
        native_rpc, native_event = server.rpc, server.event
        observed = []
        queued = [event("rawResponse/completed", "child", "child-turn", responseId="rpc-first", usage=usage(11, 2)),
                  event("rawResponse/completed", "child", "child-turn", responseId="rpc-second", usage=usage(12, 3))]
        injected = False
        native_observe = TreeLedger.observe

        def observe(ledger, message, elapsed):
            observed.append(message.get("params", {}).get("responseId"))
            return native_observe(ledger, message, elapsed)

        def rpc(method, params):
            nonlocal injected
            if method == "thread/resume" and not injected:
                injected = True
                server.pending.extend((time.perf_counter(), message) for message in queued)
            return native_rpc(method, params)

        def live_event():
            if injected:
                self.assertEqual([rid for rid in observed if rid and rid.startswith("rpc-")],
                                 ["rpc-first", "rpc-second"])
            return native_event()

        with patch.object(server, "rpc", side_effect=rpc), patch.object(server, "event", side_effect=live_event), \
                patch.object(TreeLedger, "observe", new=observe), patch("agentic_runtime.home", return_value=self.empty_home), \
                patch("execution.require_permission_probe"), patch("agentic_runtime.require_capability"):
            result = agentic_runtime.run_trial(server, self.item, 1)
        self.assertTrue(result["valid"], result)
        self.assertEqual(result["tree_observed_input_tokens"], 203)
        self.assertEqual(server.pending, [])

    def test_root_done_waits_for_background_terminal_before_clearing_scratch(self):
        server = NativeFakeServer(self.root, self.item, completed_events())
        server.background_rounds = 3

        def poll(*args, **kwargs):
            if server.messages:
                return server.event()
            server.empty_poll_calls += 1
            self.assertTrue(server.active)
            self.assertTrue((self.root / "inflight.json").exists())
            self.assertFalse((self.root / "runs/0001/attempt-001/scratch").exists())
            return None

        with patch.object(server, "poll_event", side_effect=poll), patch("agentic_runtime.home", return_value=self.empty_home), patch("execution.require_permission_probe"), patch("agentic_runtime.require_capability"):
            result = agentic_runtime.run_trial(server, self.item, 1)
        self.assertTrue(result["valid"], result)
        self.assertTrue(result["accounting_complete"])
        self.assertGreater(server.background_calls, server.background_rounds)
        self.assertGreaterEqual(server.empty_poll_calls, 1)
        self.assertFalse(server.active)
        self.assertTrue((self.root / "runs/0001/attempt-001/scratch/scratch-manifest.json").is_file())

    def test_unspecified_message_phase_is_not_silently_final(self):
        result, _ = self.run_fake(completed_events(explicit_phase=False))
        self.assertFalse(result["valid"])
        self.assertEqual(result["status"], "completed")
        self.assertIn("No explicit root final-answer phase was observed", result["protocol_issues"])

    def test_interruption_keeps_partial_stream_for_audit_without_final_answer(self):
        messages = completed_events()[:-1]
        result, server = self.run_fake(messages)
        self.assertFalse(result["valid"])
        self.assertEqual(result["status"], "interrupted")
        self.assertTrue(server.active)
        self.assertTrue((self.root / "inflight.json").exists())
        self.assertFalse(result["accounting_complete"])
        self.assertEqual(result["tree_observed_input_tokens"], 100)
        self.assertIsNone(result["tree_input_tokens"])
        self.assertTrue((self.root / "runs/0001/attempt-001/task-tree.json").is_file())
        self.assertEqual((self.root / "runs/0001/attempt-001/answer.txt").read_text(encoding="utf-8"), "")
        self.assertIn("None使用默认值", (self.root / "runs/0001/attempt-001/events.jsonl").read_text(encoding="utf-8"))
        self.assertEqual(sum(m == "turn/start" for m, _ in server.calls), 1)

    def test_matching_turn_rejection_is_terminal_failure_without_retry(self):
        result, server = self.run_fake(failure=("turn/start", RpcRejected("turn/start")))
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["valid"])
        self.assertFalse(server.active)
        self.assertFalse((self.root / "inflight.json").exists())
        self.assertEqual(sum(m == "turn/start" for m, _ in server.calls), 1)

    def test_root_permission_mismatch_blocks_before_turn_submission(self):
        server = NativeFakeServer(self.root, self.item, completed_events())
        server.active_permission_id = ":read-only"
        with patch("agentic_runtime.home", return_value=self.empty_home), patch("execution.require_permission_probe"), patch("agentic_runtime.require_capability"):
            result = agentic_runtime.run_trial(server, self.item, 1)
        self.assertFalse(result["valid"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(sum(m == "thread/start" for m, _ in server.calls), 1)
        self.assertEqual(sum(m == "turn/start" for m, _ in server.calls), 0)
        self.assertFalse(server.active)

    def test_child_permission_mismatch_invalidates_answer(self):
        server = NativeFakeServer(self.root, self.item, completed_events(child=True))
        for message in server.messages:
            settings = message.get("params", {}).get("threadSettings")
            if settings is not None:
                settings["activePermissionProfile"] = {"id": ":read-only"}
        with patch("agentic_runtime.home", return_value=self.empty_home), patch("execution.require_permission_probe"), patch("agentic_runtime.require_capability"):
            result = agentic_runtime.run_trial(server, self.item, 1)
        self.assertFalse(result["valid"])
        self.assertEqual(result["status"], "completed")
        self.assertTrue(any("permission receipt" in issue and "child" in issue for issue in result["protocol_issues"]))

    def test_unobserved_child_permission_inheritance_is_not_assumed(self):
        server = NativeFakeServer(self.root, self.item, completed_events(child=True))
        server.child_loaded = False
        for message in server.messages:
            settings = message.get("params", {}).get("threadSettings")
            if settings is not None:
                settings.pop("activePermissionProfile")
                settings.pop("approvalPolicy")
        with patch("agentic_runtime.home", return_value=self.empty_home), patch("execution.require_permission_probe"), patch("agentic_runtime.require_capability"):
            result = agentic_runtime.run_trial(server, self.item, 1)
        self.assertFalse(result["valid"])
        self.assertTrue(any("permission receipt" in issue and "child" in issue for issue in result["protocol_issues"]))

    def test_initial_child_settings_notification_is_not_required_when_own_receipt_is_observed(self):
        messages = [message for message in completed_events(child=True) if message["method"] != "thread/settings/updated"]
        result, server = self.run_fake(messages)
        self.assertTrue(result["valid"], result)
        self.assertTrue(result["accounting_complete"])
        calls = [(method, params) for method, params in server.calls if method == "thread/resume"]
        self.assertEqual(calls, [("thread/resume", {"threadId": "child", "excludeTurns": True})])
        self.assertEqual(sum(method == "turn/start" for method, _ in server.calls), 1)
        events = (self.root / "runs/0001/attempt-001/events.jsonl").read_text(encoding="utf-8")
        self.assertIn("ledger/threadSettingsObserved", events)

    def test_ambiguous_turn_submission_keeps_inflight_and_does_not_retry(self):
        result, server = self.run_fake(failure=("turn/start", ConnectionError("delivery unknown")))
        self.assertEqual(result["status"], "interrupted")
        self.assertTrue(server.active)
        self.assertTrue((self.root / "inflight.json").exists())
        self.assertIsNone(result["estimated_api_usd"])
        self.assertEqual(sum(m == "turn/start" for m, _ in server.calls), 1)

    def test_lost_turn_acknowledgement_retains_already_received_usage(self):
        server = NativeFakeServer(self.root, self.item, [])
        native_rpc = server.rpc

        def rpc(method, params):
            if method == "turn/start":
                server.calls.append((method, params))
                server.pending.extend((time.perf_counter(), message) for message in [
                    event("turn/started", turn={"id": "root-turn", "status": "inProgress"}),
                    event("rawResponse/completed", responseId="before-ack", usage=usage()),
                ])
                raise ConnectionError("turn accepted; acknowledgement lost")
            return native_rpc(method, params)

        with patch.object(server, "rpc", side_effect=rpc), patch("agentic_runtime.home", return_value=self.empty_home), patch("execution.require_permission_probe"), patch("agentic_runtime.require_capability"):
            result = agentic_runtime.run_trial(server, self.item, 1)
        self.assertEqual(result["status"], "interrupted")
        self.assertTrue(server.active)
        self.assertFalse(result["accounting_complete"])
        self.assertEqual(result["tree_observed_input_tokens"], 100)
        self.assertIsNone(result["tree_input_tokens"])
        self.assertEqual(result["agentic"]["root_turn_id"], "root-turn")
        self.assertTrue((self.root / "inflight.json").exists())
        self.assertTrue((self.root / "runs/0001/attempt-001/task-tree.json").is_file())
        self.assertEqual(sum(m == "turn/start" for m, _ in server.calls), 1)
        self.assertEqual(server.pending, [])

    def test_missing_capability_gate_blocks_before_thread_or_turn(self):
        server = NativeFakeServer(self.root, self.item, completed_events())
        with patch("execution.require_permission_probe"), self.assertRaisesRegex(BenchmarkError, "smoke"):
            agentic_runtime.run_trial(server, self.item, 1)
        self.assertEqual(server.calls, [])
        self.assertFalse((self.root / "runs").exists())

    def test_missing_permission_proof_blocks_before_thread_or_turn(self):
        server = NativeFakeServer(self.root, self.item, completed_events())
        with patch("agentic_runtime.require_capability"), self.assertRaisesRegex(BenchmarkError, "no-model"):
            agentic_runtime.run_trial(server, self.item, 1)
        self.assertEqual(server.calls, [])

    def test_old_scratch_is_not_exposed_to_new_native_thread(self):
        profile = self.protocol["execution_profile"]
        stale = Path(profile["scratch_root"]) / "previous-answer.txt"
        stale.write_text("previous trial answer", encoding="utf-8")
        result, _ = self.run_fake()
        self.assertTrue(result["valid"])
        self.assertFalse(stale.exists())
        scratch = self.root / "runs/0001/attempt-001/scratch"
        self.assertTrue((scratch / "scratch-manifest.json").is_file())
        self.assertNotIn("previous-answer.txt", read(scratch / "scratch-manifest.json")["files_sha256"])

    def test_failed_local_tool_is_a_quality_observation_not_protocol_failure(self):
        messages = completed_events()
        messages[0]["params"]["item"]["status"] = "failed"
        result, _ = self.run_fake(messages)
        self.assertTrue(result["valid"])
        self.assertFalse(result["protocol_issues"])
        self.assertEqual(result["agentic"]["tools"][0]["status"], "failed")

    def test_capability_trial_is_separate_and_never_counts_as_formal(self):
        result, _ = self.run_fake(capability=True)
        self.assertEqual(result["purpose"], "capability")
        self.assertTrue((self.root / "capability/runs/0001/attempt-001/result.json").is_file())
        self.assertFalse((self.root / "runs").exists())

    def test_capability_failure_cannot_be_silently_retried(self):
        (self.root / "capability").mkdir()
        with self.assertRaisesRegex(BenchmarkError, "already exist"):
            agentic_runtime.capability_smoke(self.root)

    def test_cli_native_default_requires_source_spec_and_rejects_closed_ultra(self):
        args = ["prepare", "--case-dir", str(self.case), "--run-dir", str(self.base / "new-run")]
        with self.assertRaisesRegex(BenchmarkError, "execution-spec"):
            benchmark.main(args)
        with self.assertRaisesRegex(BenchmarkError, "Ultra"):
            benchmark.main(args + ["--execution-mode", "fixed-evidence"])
        with contextlib.redirect_stdout(io.StringIO()):
            benchmark.main(args + ["--execution-mode", "fixed-evidence", "--efforts", "xhigh", "--models", "gpt-6.1-sol", "--repetitions", "1"])
        self.assertNotIn("execution_profile", check_inputs(self.base / "new-run"))


if __name__ == "__main__":
    unittest.main()
