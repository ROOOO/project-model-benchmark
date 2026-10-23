"""Historical capability replay and continuation, with no inference or RPC calls."""
import copy
import json
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from agentic import TreeLedger
import agentic_runtime as native
from core import BenchmarkError, digest, read, sha, write
import test_capability_sharing as sharing


def usage(inputs=100, outputs=20):
    return {"inputTokens": inputs, "cachedInputTokens": 10, "cacheWriteInputTokens": 0,
            "outputTokens": outputs, "reasoningOutputTokens": 5, "totalTokens": inputs + outputs}


class SmokeRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = sharing.CapabilitySharingTests()
        self.fixture.setUp()
        self.source, self.target = self.fixture.source, self.fixture.target
        shutil.rmtree(self.source / "capability")
        self.prices = {"schema_version": 2, "service_tiers": {tier: {"models": {model: {
            "input_usd": 2, "cached_usd": .2, "write_usd": 2, "output_usd": 10}
            for model in ("gpt-6.1-sol", "gpt-6-astra")}} for tier in ("default", "priority")}}
        for root in (self.source, self.target):
            protocol = self.fixture.protocols[root]
            protocol.update(case_id="unchanged-question", input_hashes={"question.txt": "question", "execution-manifest.json": str(root)})
            protocol["execution_profile"]["permission_profile_id"] = "permission-" + root.name
            write(root / "prices.json", self.prices)
            protocol["prices_sha256"] = sha(root / "prices.json")
            write(root / "protocol.json", protocol)
            write(root / "preparation-seal.json", {"protocol_sha256": sha(root / "protocol.json")})
            write(root / "runtime.json", {"fingerprint": self.fixture.fingerprints[root]})
            write(root / "instruction-context.json", {"sha256": "frozen-instructions"})
        self.original()

    def tearDown(self):
        self.fixture.tearDown()

    def original(self):
        profile = self.fixture.protocols[self.source]["execution_profile"]
        settings = {"model": "gpt-6.1-sol", "modelProvider": "openai", "reasoningEffort": "ultra", "serviceTier": "default",
                    "activePermissionProfile": {"id": profile["permission_profile_id"]}, "approvalPolicy": "never"}
        ledger = TreeLedger("root", "turn", settings)
        def event(method, time, tid="root", **params):
            ledger.observe({"method": method, "params": {"threadId": tid, "turnId": "turn" if tid == "root" else "child-turn", **params}}, time)
        event("item/completed", 1, item={"type": "subAgentActivity", "id": "spawn", "kind": "started", "agentThreadId": "child",
                                                "model": "gpt-6.1-sol", "reasoningEffort": "xhigh"})
        ledger.reconcile_settings("child", settings, 2)
        event("rawResponse/completed", 4, responseId="root-response", usage=usage())
        event("rawResponse/completed", 5, "child", responseId="child-response", usage=usage(200, 30))
        event("thread/tokenUsage/updated", 6, tokenUsage={"total": usage(), "last": usage()})
        event("thread/tokenUsage/updated", 6, "child", tokenUsage={"total": usage(200, 30), "last": usage(200, 30)})
        event("item/completed", 7, item={"type": "agentMessage", "id": "answer", "phase": "final_answer", "text": "原始答案不变。"})
        event("turn/completed", 8, turn={"id": "turn", "status": "completed"})
        event("turn/completed", 8, "child", turn={"id": "child-turn", "status": "completed"})
        for tid in ("root", "child"):
            ledger.reconcile_thread({"id": tid, "status": {"type": "idle"}}, 9)
        ledger.mark_discovery_complete(9)
        snapshot = ledger.snapshot()
        self.assertTrue(ledger.accounting_complete, ledger.accounting_issues())
        stale = {"method": "thread/tokenUsage/updated", "t_s": 3, "params": {
                 "threadId": "root", "turnId": "turn", "tokenUsage": {"total": usage(50, 10), "last": usage(50, 10)}}}
        snapshot["events"].append(stale)
        for node in snapshot["nodes"]:
            if node["thread_id"] == "root":
                node["latest_cumulative_usage"] = stale["params"]["tokenUsage"]
        snapshot["accounting_complete"] = False
        snapshot["accounting_issues"] = ["Root response usage disagrees with cumulative counters"]
        directory = self.source / "capability/runs/0001/attempt-001"
        directory.mkdir(parents=True)
        (directory / "answer.txt").write_text(ledger.final_answer, encoding="utf-8", newline="\n")
        (directory / "events.jsonl").write_text("".join(json.dumps(event, ensure_ascii=False) + "\n" for event in snapshot["events"]), encoding="utf-8")
        write(directory / "task-tree.json", snapshot)
        record = {"slot_id": "0001", "attempt": 1, "model": "gpt-6.1-sol", "service_tier": "default", "effort": "ultra",
                  "purpose": "capability", "valid": True, "status": "completed", "protocol_issues": [], "delegation_count": 1,
                  "accounting_complete": False, "warnings": snapshot["accounting_issues"], "agentic": snapshot,
                  "fingerprint": self.fixture.fingerprints[self.source], "execution_profile_sha256": profile["profile_sha256"],
                  "permission_receipt": settings, "prompt_sha256": digest(native._capability_prompt()), "answer_sha256": sha(directory / "answer.txt"),
                  "thread_id": "root", "turn_id": "turn", "workflow_elapsed_s": 9, "actual_gateway_charge": None}
        write(directory / "result.json", record)

    def recover(self):
        with patch("runtime.Server", side_effect=AssertionError("replay must not issue RPC")):
            return native.recover_capability(self.target, self.source)

    def test_archived_order_replay_preserves_source_answer_usage_and_runtime(self):
        before = native._collection_hashes(self.source)
        recovered = self.recover()
        result = read(self.target / "capability/runs/0001/attempt-001/result.json")
        original = read(self.source / "capability/runs/0001/attempt-001/result.json")
        self.assertEqual((recovered["inference_calls"], recovered["historical_paid_attempts"], recovered["remaining_capability_attempts"]), (0, 1, 3))
        self.assertEqual((recovered["unique_responses"], recovered["tree_input_tokens"], recovered["tree_output_tokens"]), (2, 300, 50))
        self.assertEqual(result["fingerprint"], original["fingerprint"])
        self.assertNotEqual(result["fingerprint"], self.fixture.fingerprints[self.target])
        self.assertEqual(result["agentic"]["events"], original["agentic"]["events"])
        self.assertEqual(before, native._collection_hashes(self.source))
        self.assertIsNone(result["actual_gateway_charge"])
        self.assertTrue(result["accounting_complete"])
        with self.assertRaisesRegex(BenchmarkError, "already exists"):
            self.recover()

    def continue_smoke(self, callback=None):
        fake = SimpleNamespace(fingerprint=self.fixture.fingerprints[self.target], active=False, close=lambda: None,
                               preflight=lambda protocol: read(self.target / "preflight.json"))
        calls = []
        def trial(server, item, attempt, **kwargs):
            calls.append(item["id"])
            record = self.fixture.attempt(self.target, item)
            if callback:
                callback(record)
                write(self.target / "capability/runs" / item["id"] / "attempt-001/result.json", record)
            return record
        with patch("runtime.Server", return_value=fake), patch("runtime.recover_inflight"), patch.object(native, "run_trial", side_effect=trial):
            result = native.capability_smoke(self.target, continue_existing=True)
        return result, calls

    def test_continue_runs_only_three_new_positions_then_adopts_without_double_charge(self):
        self.recover()
        result, calls = self.continue_smoke()
        self.assertEqual(calls, ["0002", "0003", "0004"])
        self.assertEqual((result["capability_attempts"], result["historical_paid_attempts"], result["verified_attempts"]), (3, 1, 4))
        sealed = native.require_capability(self.target, self.fixture.fingerprints[self.target])
        self.assertEqual((sealed["attempts"], sealed["new_paid_attempts"], sealed["paid_ledger_attempts"], sealed["replay_checks"]), (3, 3, 4, 1))
        other = self.fixture.batch("third")
        adopted = native.adopt_capability(other, self.target)
        self.assertEqual(adopted["inference_calls"], 0)
        shared = native.require_capability(other, self.fixture.fingerprints[other])
        self.assertEqual((shared["attempts"], shared["new_paid_attempts"], shared["shared_attempts"]), (0, 0, 4))
        self.assertEqual(shared["attempt_origins"], sealed["attempt_origins"])
        self.assertEqual(shared["capability_id"], sealed["capability_id"])

    def test_failure_is_retained_and_blocks_further_continuation(self):
        self.recover()
        with self.assertRaisesRegex(BenchmarkError, "capability check failed"):
            self.continue_smoke(lambda record: record.update(valid=False))
        self.assertTrue((self.target / "capability/runs/0002/attempt-001/result.json").is_file())
        with patch("runtime.Server", side_effect=AssertionError("failed capability must block before RPC")):
            with self.assertRaisesRegex(BenchmarkError, "prior capability position failed"):
                native.capability_smoke(self.target, continue_existing=True)
        self.assertFalse((self.target / "capability/runs/0003").exists())

    def test_extra_or_partial_positions_block_before_any_server(self):
        for name in ("runs/9999/attempt-001/result.json", "runs/0002/attempt-002/result.json", "partial"):
            with self.subTest(name=name):
                if (self.target / "capability").exists():
                    shutil.rmtree(self.target / "capability")
                self.recover()
                if name == "partial":
                    (self.target / "capability/runs/0002/attempt-001").mkdir(parents=True)
                else:
                    write(self.target / "capability" / name, {"valid": True})
                with patch("runtime.Server", side_effect=AssertionError("invalid capability must block before RPC")):
                    with self.assertRaises(BenchmarkError):
                        native.capability_smoke(self.target, continue_existing=True)

    def test_tampered_source_request_or_archive_is_not_repaired(self):
        directory = self.source / "capability/runs/0001/attempt-001"
        original = read(directory / "result.json")
        for change in (lambda value: value.update(prompt_sha256="wrong-request"),
                       lambda value: value.update(valid=False),
                       lambda value: value.update(protocol_issues=["permission failure"])):
            value = copy.deepcopy(original)
            change(value)
            write(directory / "result.json", value)
            with self.assertRaises(BenchmarkError):
                self.recover()
            self.assertFalse((self.target / "capability").exists())
        write(directory / "result.json", original)
        with (directory / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write('{"method":"invented"}\n')
        with self.assertRaisesRegex(BenchmarkError, "archived order"):
            self.recover()

    def test_changed_repair_code_or_context_blocks_continuation(self):
        self.recover()
        receipt_path = self.target / "capability/recovery/receipt.json"
        receipt = read(receipt_path)
        receipt["repair_runner_sha256"]["agentic.py"] = "stale-reducer"
        write(receipt_path, receipt)
        with patch("runtime.Server", side_effect=AssertionError("stale replay must block before RPC")):
            with self.assertRaisesRegex(BenchmarkError, "reducer or runner changed"):
                native.capability_smoke(self.target, continue_existing=True)

    def test_missing_price_stays_unknown_without_changing_accounting_gate(self):
        write(self.source / "prices.json", {"schema_version": 2, "service_tiers": {}})
        protocol = self.fixture.protocols[self.source]
        protocol["prices_sha256"] = sha(self.source / "prices.json")
        write(self.source / "protocol.json", protocol)
        write(self.source / "preparation-seal.json", {"protocol_sha256": sha(self.source / "protocol.json")})
        recovered = self.recover()
        result = read(self.target / "capability/runs/0001/attempt-001/result.json")
        self.assertTrue(result["accounting_complete"])
        self.assertFalse(result["pricing_complete"])
        self.assertIsNone(result["tree_estimated_api_usd"])
        self.assertIsNone(recovered["estimated_api_usd"])

    def test_original_permission_or_runtime_proof_cannot_be_substituted(self):
        for name, field in (("permission-probe-seal.json", "fingerprint_sha256"), ("runtime.json", "fingerprint")):
            with self.subTest(name=name):
                path = self.source / name
                before = read(path)
                tampered = copy.deepcopy(before)
                tampered[field] = "different-runtime"
                write(path, tampered)
                with self.assertRaises(BenchmarkError):
                    self.recover()
                self.assertFalse((self.target / "capability").exists())
                write(path, before)

    def test_nested_recovery_paths_are_rejected_before_any_source_write(self):
        for parent, child in ((self.source, self.source / "nested"), (self.target, self.target / "nested")):
            child.mkdir()
            before = native._collection_hashes(parent)
            target, source = (child, parent) if parent == self.source else (parent, child)
            with self.assertRaisesRegex(BenchmarkError, "must not be nested"):
                native.recover_capability(target, source)
            self.assertEqual(before, native._collection_hashes(parent))


if __name__ == "__main__":
    unittest.main()
