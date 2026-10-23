"""Warm child metadata receipts only; no real app server or model invocation."""
import copy
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentic import TreeLedger
from agentic_runtime import _capture_child_receipts
from core import BenchmarkError
from test_agentic import PRICES, PROTOCOL, usage


class ChildReceiptTests(unittest.TestCase):
    def setUp(self):
        self.ledger = TreeLedger("root", "root-turn", {"model": "sol", "serviceTier": "priority"})
        self.profile = {"permission_profile_id": "sealed-profile"}
        self.temp = tempfile.TemporaryDirectory(prefix="child-materialization-")
        self.addCleanup(self.temp.cleanup)
        self.rollout = Path(self.temp.name) / "own-rollout.jsonl"
        self.rollout.write_text("PRIVATE SESSION BODY MUST NEVER BE READ", encoding="utf-8")
        self.calls = []
        self.loaded = ["root", "child", "unrelated"]
        self.receipt = {"thread": {"id": "child", "parentThreadId": "root", "status": {"type": "active"}, "path": str(self.rollout)},
                        "activePermissionProfile": {"id": "sealed-profile", "extends": None},
                        "approvalPolicy": "never", "cwd": "C:\\frozen\\source", "model": "astra",
                        "modelProvider": "test", "reasoningEffort": "xhigh", "serviceTier": "default",
                        "instructionSources": ["PRIVATE SOURCE PATH NOT IN RECEIPT"],
                        "raw": "PRIVATE BODY NOT IN RECEIPT"}
        self.server = SimpleNamespace(rpc=self.rpc)
        self.spawn()

    def spawn(self, child="child", parent="root", time=1):
        self.ledger.observe({"method": "item/completed", "params": {"threadId": parent, "turnId": "root-turn",
            "item": {"type": "subAgentActivity", "id": "spawn-" + child, "agentThreadId": child, "kind": "started",
                     "model": "astra", "reasoningEffort": "xhigh"}}}, time)

    def rpc(self, method, params):
        self.calls.append((method, copy.deepcopy(params)))
        if method == "thread/loaded/list":
            self.assertEqual(params, {})
            return {"data": self.loaded}
        if method == "thread/read":
            self.assertEqual(set(params), {"threadId", "includeTurns"})
            self.assertFalse(params["includeTurns"])
            thread = copy.deepcopy(self.receipt["thread"])
            thread["id"] = params["threadId"]
            return {"thread": thread}
        if method == "thread/resume":
            self.assertEqual(set(params), {"threadId", "excludeTurns"})
            self.assertTrue(params["excludeTurns"])
            self.assertIn(params["threadId"], self.loaded)
            response = copy.deepcopy(self.receipt)
            response["thread"]["id"] = params["threadId"]
            response["thread"]["parentThreadId"] = self.ledger.nodes[params["threadId"]]["parent_thread_id"]
            return response
        raise AssertionError("Must not mutate settings or submit a turn: " + method)

    def response(self, response_id, time):
        self.ledger.observe({"method": "rawResponse/completed", "params": {
            "threadId": "child", "turnId": "child-turn", "responseId": response_id, "usage": usage()}}, time)

    def capture(self, time=2):
        return _capture_child_receipts(self.server, self.ledger, lambda: time, profile=self.profile)

    def test_loaded_owned_child_gets_actual_receipt_once_with_no_overrides(self):
        result = self.capture()
        self.assertEqual(result, {"captured": ["child"], "unloaded": [], "unmaterialized": []})
        self.assertEqual([method for method, _ in self.calls], ["thread/loaded/list", "thread/read", "thread/resume"])
        self.assertEqual(self.ledger.nodes["child"]["settings"]["serviceTier"], "default")
        self.assertEqual(self.ledger.nodes["child"]["settings"]["activePermissionProfile"]["id"], "sealed-profile")
        self.assertEqual(self.ledger.nodes["child"]["settings_history"][-1]["source"], "thread/resume")
        self.assertEqual(self.capture(3), {"captured": [], "unloaded": [], "unmaterialized": []})
        self.assertEqual(len(self.calls), 3)
        self.assertNotIn("unrelated", self.ledger.nodes)
        self.assertNotIn("PRIVATE", str(self.ledger.snapshot()))
        self.assertEqual(TreeLedger.from_snapshot(self.ledger.snapshot()).snapshot(), self.ledger.snapshot())

    def test_unloaded_child_is_never_resumed_and_remains_unknown(self):
        self.loaded = ["root", "unrelated"]
        self.assertEqual(self.capture(), {"captured": [], "unloaded": ["child"], "unmaterialized": []})
        self.assertEqual([method for method, _ in self.calls], ["thread/loaded/list"])
        self.assertNotIn("activePermissionProfile", self.ledger.nodes["child"]["settings"])
        self.assertFalse(any(event["method"] == "ledger/threadSettingsObserved" for event in self.ledger.events))

    def test_fresh_loaded_thread_is_deferred_until_native_storage_exists(self):
        self.rollout.unlink()
        self.assertEqual(self.capture(), {"captured": [], "unloaded": [], "unmaterialized": ["child"]})
        self.assertEqual([method for method, _ in self.calls], ["thread/loaded/list", "thread/read"])
        self.assertNotIn("activePermissionProfile", self.ledger.nodes["child"]["settings"])
        self.rollout.write_text("materialized by native child work", encoding="utf-8")
        self.assertEqual(self.capture(3)["captured"], ["child"])
        self.assertEqual(sum(method == "thread/resume" for method, _ in self.calls), 1)

    def test_null_native_storage_path_never_triggers_resume_or_materialization(self):
        self.receipt["thread"]["path"] = None
        self.assertEqual(self.capture()["unmaterialized"], ["child"])
        self.assertFalse(any(method == "thread/resume" for method, _ in self.calls))

    def test_descendants_are_captured_but_unrelated_threads_are_not(self):
        self.spawn("grandchild", "child")
        self.loaded.append("grandchild")
        result = self.capture()
        self.assertEqual(result["captured"], ["child", "grandchild"])
        self.assertEqual([params["threadId"] for method, params in self.calls if method == "thread/resume"], ["child", "grandchild"])

    def test_wrong_profile_or_approval_is_recorded_and_rejected(self):
        for field, value in (("activePermissionProfile", {"id": ":read-only"}), ("approvalPolicy", "on-request")):
            with self.subTest(field=field):
                self.setUp()
                self.receipt[field] = value
                with self.assertRaisesRegex(BenchmarkError, "permission receipt|approval policy"):
                    self.capture()
                self.assertEqual(self.ledger.nodes["child"]["settings"][field], value)
                self.assertTrue(any(event["method"] == "ledger/threadSettingsObserved" for event in self.ledger.events))

    def test_missing_fields_and_null_tier_do_not_inherit_root_defaults(self):
        self.receipt["serviceTier"] = None
        self.capture()
        self.response("after-null-receipt", 3)
        self.assertIsNone(self.ledger.responses["after-null-receipt"]["settings"]["serviceTier"])
        self.assertIsNone(self.ledger.metrics(PRICES, PROTOCOL, 4)["child_observed_api_usd"])
        self.setUp()
        self.receipt.pop("activePermissionProfile")
        with self.assertRaisesRegex(BenchmarkError, "permission receipt"):
            self.capture()

    def test_earlier_usage_is_not_repriced_by_later_current_receipt(self):
        self.response("before", 1.5)
        self.capture(2)
        self.response("after", 3)
        self.assertNotIn("serviceTier", self.ledger.responses["before"]["settings"])
        self.assertEqual(self.ledger.responses["after"]["settings"]["serviceTier"], "default")
        result = self.ledger.metrics(PRICES, PROTOCOL, 4)
        self.assertAlmostEqual(result["child_observed_api_usd"], (90*6+10*.6+20*30)/1e6)
        self.assertIsNone(result["child_estimated_api_usd"])

    def test_queued_earlier_usage_arriving_after_rpc_keeps_earlier_unknown_tier(self):
        self.capture(2)
        self.response("queued-before-receipt", 1.5)
        self.response("after-receipt", 3)
        self.assertNotIn("serviceTier", self.ledger.responses["queued-before-receipt"]["settings"])
        self.assertEqual(self.ledger.responses["after-receipt"]["settings"]["serviceTier"], "default")
        self.assertEqual(TreeLedger.from_snapshot(self.ledger.snapshot()).snapshot(), self.ledger.snapshot())

    def test_unloaded_then_loaded_can_be_observed_without_restarting_it(self):
        self.loaded = ["root"]
        self.capture()
        self.loaded.append("child")
        self.assertEqual(self.capture(3)["captured"], ["child"])
        self.assertEqual(sum(method == "thread/resume" for method, _ in self.calls), 1)


if __name__ == "__main__":
    unittest.main()
