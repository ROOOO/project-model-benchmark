"""Offline service-tier transport checks; no real app-server or model calls."""
import collections
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import BenchmarkError, check_inputs, home, prepare, read, write
from runtime import Server, recover_inflight
from test_benchmark import fixture


class TierRuntimeTests(unittest.TestCase):
    def setUp(self):
        parent = home() / "benchmark-validation"
        parent.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="tier-runtime-", dir=parent)
        self.base = Path(self.temp.name).resolve()
        self.case, self.prices = fixture(self.base)
        self.public_home = self.base / "empty-codex-home"
        self.public_home.mkdir()
        self.next_root = 0

    def tearDown(self):
        self.temp.cleanup()

    def batch(self, tiers=("default", "fast")):
        self.next_root += 1
        root = prepare(self.case, self.base / f"run-{self.next_root}", ["gpt-6.1-sol"], ["max"], 1,
                       self.prices, service_tiers=tiers)
        return root, check_inputs(root)

    def transport(self, root):
        server = Server.__new__(Server)
        server.root, server.provider = root, "test"
        server.fingerprint = {"global_instructions_sha256": None}
        server.proc = SimpleNamespace(pid=os.getpid(), poll=lambda: None)
        server.counts, server.pending, server.active = collections.Counter(), [], False
        return server

    def completed_trial(self, root, slot, receipt=None, observed=None):
        server = self.transport(root)
        calls = []

        def rpc(method, params):
            calls.append((method, params))
            if method == "thread/start":
                return {"model": slot["model"], "modelProvider": "test", "reasoningEffort": slot["effort"],
                        "serviceTier": receipt if receipt is not None else slot.get("service_tier", "default"),
                        "thread": {"id": "fixture-thread"}}
            t = time.perf_counter()
            events = [(1, "item/agentMessage/delta", {"delta": "答案"}),
                      (2, "item/agentMessage/delta", {"delta": "正确"}),
                      (3, "item/completed", {"item": {"type": "agentMessage", "text": "答案正确"}}),
                      (4, "turn/completed", {"turn": {"status": "completed"}})]
            if observed is not None:
                events.insert(0, (.5, "thread/updated", {"serviceTier": observed}))
            server.pending.extend((t + offset, {"method": name, "params": {"threadId": "fixture-thread", "turnId": "fixture-turn", **payload}})
                                  for offset, name, payload in events)
            return {"turn": {"id": "fixture-turn"}}

        server.rpc = rpc
        with patch("runtime.home", return_value=self.public_home):
            result = server.run_trial(slot, 1)
        return result, calls

    def catalog(self, root, supported):
        server = self.transport(root)
        server.rpc = Mock(return_value={"data": [{"model": "gpt-6.1-sol",
                          "supportedReasoningEfforts": [{"reasoningEffort": "max"}],
                          "serviceTiers": [{"id": "priority", "name": "Fast"}] if supported else []}]})
        return server

    def test_missing_fast_preflight_never_starts_inference(self):
        root, protocol = self.batch()
        server = self.catalog(root, False)
        with self.assertRaisesRegex(BenchmarkError, "service_tier=priority"):
            server.preflight(protocol)
        self.assertEqual([c.args[0] for c in server.rpc.call_args_list], ["model/list"])
        self.assertEqual(read(root / "preflight.json")["inference_calls"], 0)
        self.assertFalse((root / "runs").exists())

    def test_supported_fast_preflight_records_catalog(self):
        root, protocol = self.batch()
        server = self.catalog(root, True)
        result = server.preflight(protocol)
        self.assertEqual(result["unsupported"], [])
        self.assertEqual(result["inference_calls"], 0)
        self.assertEqual(result["models"][0]["serviceTiers"][0]["id"], "priority")
        self.assertEqual([c.args[0] for c in server.rpc.call_args_list], ["model/list"])

    def test_tier_receipt_mismatch_does_not_start_turn(self):
        root, protocol = self.batch()
        slot = next(s for s in protocol["schedule"] if s["service_tier"] == "priority")
        result, calls = self.completed_trial(root, slot, receipt="default")
        self.assertFalse(result["valid"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual([method for method, _ in calls], ["thread/start"])
        self.assertIn("settings differ", result["error_detail"])
        self.assertEqual(result["service_tier"], "priority")
        self.assertFalse((root / "inflight.json").exists())

    def test_thread_config_and_turn_receive_each_slot_tier(self):
        root, protocol = self.batch()
        for slot in protocol["schedule"]:
            with self.subTest(tier=slot["service_tier"]):
                result, calls = self.completed_trial(root, slot)
                self.assertTrue(result["valid"])
                self.assertEqual([method for method, _ in calls], ["thread/start", "turn/start"])
                self.assertEqual(calls[0][1]["serviceTier"], slot["service_tier"])
                self.assertEqual(calls[0][1]["config"]["service_tier"], slot["service_tier"])
                self.assertEqual(calls[1][1]["serviceTierForTurn"], slot["service_tier"])
                self.assertEqual(result["effective"]["serviceTier"], slot["service_tier"])
                self.assertEqual(result["service_tier"], slot["service_tier"])
                self.assertIn("estimated_api_usd", result)
                self.assertNotIn("estimated_standard_api_usd", result)

    def test_observed_tier_mismatch_invalidates_completed_answer(self):
        root, protocol = self.batch()
        slot = next(s for s in protocol["schedule"] if s["service_tier"] == "priority")
        result, calls = self.completed_trial(root, slot, observed="default")
        self.assertEqual(result["status"], "completed")
        self.assertFalse(result["valid"])
        self.assertIn("Observed service tier differs from requested service tier", result["warnings"])
        self.assertEqual(len(calls), 2)

    def test_fast_feature_override_is_explicit_only_for_fast_batch(self):
        for tiers, enabled in [(("default", "fast"), True), (("default",), False), (None, False)]:
            with self.subTest(tiers=tiers):
                root, _ = self.batch(tiers)
                proc = SimpleNamespace(stdin=Mock(), stdout=[], stderr=[], poll=lambda: None, wait=Mock(), terminate=Mock())
                with patch("runtime.home", return_value=self.public_home), \
                     patch("runtime.discover_binary", return_value=Path(__file__)), \
                     patch("runtime.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout="fixture-version")), \
                     patch("runtime.subprocess.Popen", return_value=proc) as spawn, \
                     patch("runtime.threading.Thread"), \
                     patch.object(Server, "rpc", return_value={}) as rpc:
                    server = Server(root)
                    server.close()
                args = spawn.call_args.args[0]
                self.assertEqual("features.fast_mode=true" in args, enabled)
                self.assertIn('service_tier="default"', args)
                self.assertEqual([c.args[0] for c in rpc.call_args_list], ["initialize"])

    def test_legacy_trial_keeps_default_shape_and_metric_name(self):
        root, protocol = self.batch(None)
        result, calls = self.completed_trial(root, protocol["schedule"][0])
        self.assertTrue(result["valid"])
        self.assertNotIn("service_tier", result)
        self.assertIn("estimated_standard_api_usd", result)
        self.assertNotIn("estimated_api_usd", result)
        self.assertEqual(calls[0][1]["serviceTier"], "default")
        self.assertEqual(calls[1][1]["serviceTierForTurn"], "default")

    def test_recovered_new_attempt_preserves_tier_and_neutral_cost_metric(self):
        root, protocol = self.batch()
        slot = next(s for s in protocol["schedule"] if s["service_tier"] == "priority")
        directory = root / "runs" / slot["id"] / "attempt-001"
        directory.mkdir(parents=True)
        write(root / "inflight.json", {"slot_id": slot["id"], "attempt": 1,
              "relative_dir": directory.relative_to(root).as_posix()})
        recover_inflight(root)
        result = read(directory / "result.json")
        self.assertEqual(result["status"], "interrupted")
        self.assertEqual(result["service_tier"], "priority")
        self.assertIn("estimated_api_usd", result)
        self.assertNotIn("estimated_standard_api_usd", result)
        self.assertFalse((root / "inflight.json").exists())


if __name__ == "__main__":
    unittest.main()
