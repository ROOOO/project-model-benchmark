"""Offline task-tree accounting checks; no app-server or paid model calls."""
import copy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agentic import TreeLedger
from core import digest


def usage(inputs=100, outputs=20, cached=10, reasoning=5):
    return {"inputTokens": inputs, "cachedInputTokens": cached, "cacheWriteInputTokens": 0,
            "outputTokens": outputs, "reasoningOutputTokens": reasoning, "totalTokens": inputs+outputs}


PRICES = {"schema_version": 2, "service_tiers": {
    "default": {"models": {"sol": {"input_usd": 2, "cached_usd": .2, "write_usd": 2, "output_usd": 10},
                            "astra": {"input_usd": 6, "cached_usd": .6, "write_usd": 6, "output_usd": 30}}},
    "priority": {"models": {"sol": {"input_usd": 4, "cached_usd": .4, "write_usd": 4, "output_usd": 20}}}}}
PROTOCOL = {"service_tiers": ["default", "priority"]}


class TreeLedgerTests(unittest.TestCase):
    def ledger(self, tier="priority"):
        return TreeLedger("root", "root-turn", {"model": "sol", "modelProvider": "test", "reasoningEffort": "ultra", "serviceTier": tier})

    def event(self, ledger, method, tid="root", turn_id="root-turn", time=1, **params):
        return ledger.observe({"method": method, "params": {"threadId": tid, "turnId": turn_id, **params}}, time)

    def response(self, ledger, rid="response-1", tid="root", turn="root-turn", time=4, counters=None):
        return self.event(ledger, "rawResponse/completed", tid, turn, time, responseId=rid, usage=usage() if counters is None else counters)

    def settings(self, ledger, tid, model="astra", tier="default", time=1):
        return self.event(ledger, "thread/settings/updated", tid, None, time,
                          threadSettings={"model": model, "serviceTier": tier, "effort": "xhigh", "modelProvider": "test"})

    def child(self, ledger, tid="child", parent="root", time=1, version=2):
        if version == 2:
            self.event(ledger, "item/completed", parent, "root-turn", time, item={"type": "subAgentActivity", "id": "spawn-"+tid,
                       "kind": "started", "agentThreadId": tid, "model": "astra", "reasoningEffort": "xhigh"})
        else:
            self.event(ledger, "item/completed", parent, "root-turn", time, item={"type": "collabAgentToolCall", "id": "spawn-"+tid,
                       "tool": "spawnAgent", "status": "completed", "senderThreadId": parent, "receiverThreadIds": [tid],
                       "model": "astra", "reasoningEffort": "xhigh"})

    def final(self, ledger, text="答案", time=5, phase="final_answer"):
        self.event(ledger, "item/started", time=time, item={"type": "agentMessage", "id": "final", "phase": phase})
        self.event(ledger, "item/agentMessage/delta", time=time+.1, itemId="final", delta=text)
        self.event(ledger, "item/completed", time=time+.5, item={"type": "agentMessage", "id": "final", "phase": phase, "text": text})

    def complete_root(self, ledger, time=6):
        items = [{"type": "agentMessage", "id": entry["id"], "phase": entry["phase"], "text": entry["text"]}
                 for entry in ledger.messages.values() if entry["turn_id"] == "root-turn" and entry["completed"]]
        self.event(ledger, "turn/completed", time=time, turn={"id": "root-turn", "status": "completed", "items": items})

    def reconcile(self, ledger, *tids, time=9):
        for tid in tids or ledger.related_thread_ids:
            ledger.reconcile_thread({"id": tid, "status": {"type": "idle"}}, time)
        ledger.mark_discovery_complete(time)

    def completed(self):
        ledger = self.ledger()
        self.response(ledger)
        self.final(ledger)
        self.complete_root(ledger)
        self.reconcile(ledger)
        return ledger

    def test_zero_delegation_is_complete_and_child_cost_is_zero(self):
        ledger = self.completed()
        result = ledger.metrics(PRICES, PROTOCOL, 9)
        self.assertTrue(ledger.accounting_complete)
        self.assertTrue(ledger.tree_terminal)
        self.assertFalse(result["delegation_occurred"])
        self.assertEqual(result["child_estimated_api_usd"], 0)
        self.assertEqual(result["child_input_tokens"], 0)
        self.assertEqual(result["tree_input_tokens"], 100)

    def test_per_response_dedup_ignores_cumulative_updates(self):
        ledger = self.completed()
        self.response(ledger, time=10)
        counters = usage()
        for time in (10, 11):
            self.event(ledger, "thread/tokenUsage/updated", time=time, tokenUsage={"total": counters, "last": counters})
        self.assertEqual(len(ledger.responses), 1)
        self.assertEqual(ledger.metrics(PRICES, PROTOCOL, 12)["input_tokens"], 100)
        self.assertTrue(ledger.accounting_complete)

    def test_conflicting_response_id_is_not_counted_twice(self):
        ledger = self.completed()
        self.response(ledger, counters=usage(inputs=101))
        self.assertEqual(len(ledger.responses), 1)
        self.assertIn("Conflicting duplicate response ID", ledger.accounting_issues())
        self.assertIsNone(ledger.metrics(PRICES, PROTOCOL, 12)["estimated_api_usd"])

    def test_nested_out_of_order_v2_relations_keep_usage(self):
        ledger = self.ledger()
        self.settings(ledger, "grandchild")
        self.response(ledger, "grand-response", "grandchild", "grand-turn")
        self.child(ledger, "grandchild", "child", time=5)
        self.assertEqual(ledger.related_thread_ids, ["root"])
        self.settings(ledger, "child")
        self.response(ledger, "child-response", "child", "child-turn")
        self.child(ledger, "child", "root", time=6)
        self.response(ledger)
        self.final(ledger)
        self.complete_root(ledger, 8)
        self.reconcile(ledger)
        result = ledger.metrics(PRICES, PROTOCOL, 10)
        self.assertEqual(ledger.related_thread_ids, ["child", "grandchild", "root"])
        self.assertTrue(ledger.accounting_complete)
        self.assertEqual(result["delegation_count"], 2)
        self.assertEqual(result["child_input_tokens"], 200)
        self.assertEqual(result["tree_input_tokens"], 300)

    def test_v1_spawn_relation_without_child_thread_started(self):
        ledger = self.ledger()
        self.child(ledger, version=1)
        self.settings(ledger, "child")
        self.response(ledger, "child-response", "child", "child-turn")
        self.response(ledger)
        self.final(ledger)
        self.complete_root(ledger)
        self.reconcile(ledger)
        self.assertTrue(ledger.accounting_complete)
        self.assertEqual(ledger.metrics(PRICES, PROTOCOL, 10)["delegation_count"], 1)

    def test_root_completion_does_not_finish_active_child(self):
        ledger = self.ledger()
        self.child(ledger)
        self.event(ledger, "turn/started", "child", "child-turn", turn={"id": "child-turn", "status": "inProgress"})
        self.response(ledger)
        self.final(ledger)
        self.complete_root(ledger)
        self.assertTrue(ledger.root_completed)
        self.assertIn("child", ledger.active_threads)
        self.assertFalse(ledger.tree_terminal)
        ledger.reconcile_thread({"id": "child", "status": {"type": "active"}})
        self.assertFalse(ledger.tree_terminal)
        self.event(ledger, "turn/completed", "child", "child-turn", turn={"id": "child-turn", "status": "completed"}, time=12)
        self.assertTrue(ledger.tree_terminal)
        self.assertFalse(ledger.accounting_complete)

    def test_idle_reconciliation_terminates_without_requiring_thread_close(self):
        ledger = self.ledger()
        self.child(ledger)
        self.settings(ledger, "child")
        self.response(ledger, "child-response", "child", "child-turn")
        self.response(ledger)
        self.final(ledger)
        self.complete_root(ledger)
        self.reconcile(ledger)
        self.assertEqual(ledger.active_threads, [])
        self.assertTrue(ledger.accounting_complete)

    def test_failed_tool_does_not_invalidate_answer_or_usage(self):
        ledger = self.completed()
        self.event(ledger, "item/completed", time=2, item={"type": "commandExecution", "id": "command-1", "status": "failed", "command": "not archived", "aggregatedOutput": "private"})
        self.assertEqual(next(iter(ledger.tools.values()))["status"], "failed")
        self.assertEqual(ledger.errors, [])
        self.assertTrue(ledger.accounting_complete)
        self.assertEqual(ledger.final_answer, "答案")

    def test_inherited_child_cumulative_usage_is_never_added(self):
        ledger = self.ledger()
        self.child(ledger)
        self.settings(ledger, "child")
        self.event(ledger, "thread/tokenUsage/updated", "child", "child-turn", tokenUsage={"total": usage(1000000), "last": usage(50)})
        self.response(ledger, "child-response", "child", "child-turn", counters=usage(50))
        self.response(ledger)
        self.final(ledger)
        self.complete_root(ledger)
        self.reconcile(ledger)
        self.assertEqual(ledger.metrics(PRICES, PROTOCOL, 9)["tree_input_tokens"], 150)
        self.assertTrue(ledger.accounting_complete)

    def test_lost_root_response_detected_by_cumulative_cross_check(self):
        ledger = self.completed()
        self.event(ledger, "thread/tokenUsage/updated", tokenUsage={"total": usage(200, 40), "last": usage()})
        self.assertIn("Root response usage disagrees with cumulative counters", ledger.accounting_issues())
        result = ledger.metrics(PRICES, PROTOCOL, 9)
        self.assertIsNone(result["tree_estimated_api_usd"])
        self.assertGreater(result["tree_observed_api_usd"], 0)

    def test_copied_fork_history_does_not_become_new_child_work(self):
        ledger = self.completed()
        ledger.reconcile_thread({"id": "child", "parentThreadId": "root", "forkedFromId": "root", "status": {"type": "idle"},
                                 "turns": [{"id": "old-turn", "status": "completed", "items": [{"type": "agentMessage", "text": "OLD ANSWER"}]}]})
        ledger.mark_discovery_complete()
        self.assertEqual(ledger.nodes["child"]["turns"], {})
        self.assertNotIn("OLD ANSWER", json.dumps(ledger.snapshot()))
        self.assertEqual(ledger.metrics(PRICES, PROTOCOL, 10)["tree_input_tokens"], 100)

    def test_mixed_model_tiers_are_priced_separately(self):
        ledger = self.ledger()
        self.child(ledger)
        self.settings(ledger, "child", "astra", "default")
        self.response(ledger, "child-response", "child", "child-turn")
        self.response(ledger)
        self.final(ledger)
        self.complete_root(ledger)
        self.reconcile(ledger)
        result = ledger.metrics(PRICES, PROTOCOL, 9)
        root_fee = (90*4+10*.4+20*20)/1e6
        child_fee = (90*6+10*.6+20*30)/1e6
        self.assertAlmostEqual(result["root_estimated_api_usd"], root_fee)
        self.assertAlmostEqual(result["child_estimated_api_usd"], child_fee)
        self.assertAlmostEqual(result["estimated_api_usd"], root_fee+child_fee)

    def test_context_band_is_selected_per_response_with_exact_threshold(self):
        ledger = self.ledger()
        self.response(ledger, "short", counters=usage(272000, 20, 1000))
        self.response(ledger, "long", counters=usage(272001, 30, 2000))
        self.final(ledger)
        self.complete_root(ledger)
        self.reconcile(ledger)
        rates = {"input_usd": 4, "cached_usd": .4, "output_usd": 20, "context_threshold_tokens": 272000,
                 "long_input_usd": 8, "long_cached_usd": .8, "long_output_usd": 30}
        prices = {"service_tiers": {"priority": {"models": {"sol": rates}}}}
        result = ledger.metrics(prices, PROTOCOL, 9)
        expected = ((272000-1000)*4+1000*.4+20*20 + (272001-2000)*8+2000*.8+30*30)/1e6
        self.assertAlmostEqual(result["tree_estimated_api_usd"], expected)
        self.assertTrue(result["pricing_complete"])

    def test_summed_short_responses_do_not_become_one_long_context(self):
        ledger = self.ledger()
        self.response(ledger, "one", counters=usage(200000, 20))
        self.response(ledger, "two", counters=usage(200000, 20))
        self.final(ledger)
        self.complete_root(ledger)
        self.reconcile(ledger)
        prices = {"service_tiers": {"priority": {"models": {"sol": {
            "input_usd": 4, "cached_usd": .4, "output_usd": 20, "context_threshold_tokens": 272000}}}}}
        result = ledger.metrics(prices, PROTOCOL, 9)
        self.assertEqual(result["tree_input_tokens"], 400000)
        self.assertAlmostEqual(result["tree_estimated_api_usd"], 2*((200000-10)*4+10*.4+20*20)/1e6)

    def test_missing_long_prices_leave_total_unknown_and_keep_known_partial_cost(self):
        ledger = self.ledger()
        self.response(ledger, "short")
        self.response(ledger, "long", counters=usage(300000, 20))
        self.final(ledger)
        self.complete_root(ledger)
        self.reconcile(ledger)
        prices = copy.deepcopy(PRICES)
        rates = prices["service_tiers"]["priority"]["models"]["sol"]
        rates.update(context_threshold_tokens=272000, long_input_usd=8, long_output_usd=30)
        result = ledger.metrics(prices, PROTOCOL, 9)
        self.assertIsNone(result["estimated_api_usd"])
        self.assertFalse(result["pricing_complete"])
        self.assertAlmostEqual(result["tree_observed_api_usd"], (90*4+10*.4+20*20)/1e6)

    def test_cache_write_rate_is_unknown_unless_explicit_or_no_writes(self):
        ledger = self.completed()
        prices = copy.deepcopy(PRICES)
        rates = prices["service_tiers"]["priority"]["models"]["sol"]
        rates.pop("write_usd")
        self.assertIsNotNone(ledger.metrics(prices, PROTOCOL, 9)["estimated_api_usd"])
        ledger = self.ledger()
        counters = usage()
        counters["cacheWriteInputTokens"] = 15
        self.response(ledger, counters=counters)
        self.final(ledger)
        self.complete_root(ledger)
        self.reconcile(ledger)
        self.assertIsNone(ledger.metrics(prices, PROTOCOL, 9)["estimated_api_usd"])
        rates["write_usd"] = 5
        self.assertAlmostEqual(ledger.metrics(prices, PROTOCOL, 9)["estimated_api_usd"], (75*4+10*.4+15*5+20*20)/1e6)

    def test_long_context_selection_respects_actual_child_model_and_tier(self):
        ledger = self.ledger()
        self.child(ledger)
        self.settings(ledger, "child", "astra", "default")
        self.response(ledger, "child-response", "child", "child-turn", counters=usage(300000, 20))
        self.response(ledger)
        self.final(ledger)
        self.complete_root(ledger)
        self.reconcile(ledger)
        prices = copy.deepcopy(PRICES)
        prices["service_tiers"]["default"]["models"]["astra"].update(
            context_threshold_tokens=272000, long_input_usd=12, long_cached_usd=1.2, long_output_usd=45)
        result = ledger.metrics(prices, PROTOCOL, 9)
        self.assertAlmostEqual(result["child_estimated_api_usd"], ((300000-10)*12+10*1.2+20*45)/1e6)
        self.assertAlmostEqual(result["root_estimated_api_usd"], (90*4+10*.4+20*20)/1e6)

    def test_child_tier_is_not_assumed_to_be_root_fast(self):
        ledger = self.ledger()
        self.child(ledger)
        self.response(ledger, "child-response", "child", "child-turn")
        self.response(ledger)
        self.final(ledger)
        self.complete_root(ledger)
        self.reconcile(ledger)
        result = ledger.metrics(PRICES, PROTOCOL, 9)
        self.assertTrue(result["accounting_complete"])
        self.assertFalse(result["pricing_complete"])
        self.assertIsNone(result["child_estimated_api_usd"])
        self.assertIsNone(result["child_observed_api_usd"])
        self.assertGreater(result["tree_observed_api_usd"], 0)
        self.assertIsNone(result["estimated_api_usd"])

    def test_later_settings_do_not_reprice_earlier_responses(self):
        ledger = self.ledger()
        self.response(ledger)
        self.settings(ledger, "root", "astra", "default", time=5)
        self.response(ledger, "root-response-2", time=6)
        self.final(ledger)
        self.complete_root(ledger)
        self.reconcile(ledger)
        values = list(ledger.responses.values())
        self.assertEqual(values[0]["settings"]["model"], "sol")
        self.assertEqual(values[0]["settings"]["serviceTier"], "priority")
        self.assertEqual(values[1]["settings"]["model"], "astra")
        self.assertEqual(values[1]["settings"]["serviceTier"], "default")

    def test_child_permission_receipt_is_allowlisted_and_replayable(self):
        ledger = self.ledger()
        self.child(ledger)
        receipt = {"model": "astra", "serviceTier": "default", "activePermissionProfile": {
            "id": "benchmark-readonly-abc", "extends": ":workspace", "private": "DO NOT ARCHIVE"},
            "approvalPolicy": "never", "cwd": "C:\\frozen source\\src", "rawInstructions": "DO NOT ARCHIVE"}
        self.event(ledger, "thread/settings/updated", "child", None, threadSettings=receipt)
        settings = ledger.nodes["child"]["settings"]
        self.assertEqual(settings["activePermissionProfile"], {"id": "benchmark-readonly-abc", "extends": ":workspace"})
        self.assertEqual(settings["approvalPolicy"], "never")
        self.assertEqual(settings["cwd"], "C:\\frozen source\\src")
        self.assertNotIn("DO NOT ARCHIVE", json.dumps(ledger.snapshot()))
        self.assertEqual(TreeLedger.from_snapshot(ledger.snapshot()).snapshot(), ledger.snapshot())

    def test_missing_child_permission_is_not_inherited_and_policy_changes_survive_replay(self):
        ledger = TreeLedger("root", "root-turn", {"model": "sol", "serviceTier": "priority",
            "activePermissionProfile": {"id": "root-profile", "extends": None}, "approvalPolicy": "never", "cwd": "C:\\root"})
        self.child(ledger)
        self.assertNotIn("activePermissionProfile", ledger.nodes["child"]["settings"])
        self.event(ledger, "thread/settings/updated", "child", None, threadSettings={
            "activePermissionProfile": None, "approvalPolicy": {"granular": {"sandbox_approval": False, "rules": True, "private": "discard"}},
            "cwd": "C:\\scratch"})
        settings = ledger.nodes["child"]["settings"]
        self.assertIsNone(settings["activePermissionProfile"])
        self.assertEqual(settings["approvalPolicy"], {"granular": {"sandbox_approval": False, "rules": True}})
        replayed = TreeLedger.from_snapshot(ledger.snapshot())
        self.assertEqual(replayed.nodes["child"]["settings"], settings)
        self.assertEqual(replayed.snapshot(), ledger.snapshot())

    def test_missing_usage_and_interruption_are_unknown_not_zero(self):
        ledger = self.ledger()
        self.event(ledger, "rawResponse/completed", responseId="response-1", usage=None)
        ledger.mark_transport_incomplete()
        result = ledger.metrics(PRICES, PROTOCOL, 12)
        self.assertFalse(result["accounting_complete"])
        self.assertIsNone(result["tree_input_tokens"])
        self.assertIsNone(result["tree_observed_input_tokens"])
        self.assertIsNone(result["tree_observed_api_usd"])
        self.assertIsNone(result["estimated_api_usd"])

    def test_commentary_child_and_final_are_separate(self):
        ledger = self.ledger()
        self.event(ledger, "item/started", time=1, item={"type": "agentMessage", "id": "progress", "phase": "commentary"})
        self.event(ledger, "item/agentMessage/delta", time=1.1, itemId="progress", delta="正在查源码")
        self.event(ledger, "item/completed", time=1.2, item={"type": "agentMessage", "id": "progress", "phase": "commentary", "text": "正在查源码"})
        self.event(ledger, "item/completed", "child", "child-turn", time=2, item={"type": "agentMessage", "id": "child-answer", "phase": "final_answer", "text": "子代理正文"})
        self.response(ledger)
        self.final(ledger)
        self.complete_root(ledger)
        self.reconcile(ledger)
        result = ledger.metrics(PRICES, PROTOCOL, 9)
        self.assertEqual(ledger.final_answer, "答案")
        self.assertEqual(result["first_progress_s"], 1.1)
        self.assertEqual(result["final_ttft_s"], 5.1)
        self.assertEqual(result["answer_ready_s"], 5.5)
        self.assertEqual(result["root_turn_completed_s"], 6)
        self.assertEqual(result["workflow_elapsed_s"], 9)
        self.assertNotIn("子代理正文", json.dumps(ledger.snapshot(), ensure_ascii=False))

    def test_unknown_phase_is_preserved_as_candidate_not_final(self):
        ledger = self.ledger()
        self.final(ledger, phase=None)
        self.complete_root(ledger)
        self.assertEqual(ledger.final_answer, "")
        self.assertEqual(ledger.candidate_answer, "答案")
        self.assertIsNone(ledger.metrics(PRICES, PROTOCOL, 10)["final_ttft_s"])

    def test_phase_arrives_at_completion_after_delta(self):
        ledger = self.ledger()
        self.event(ledger, "item/agentMessage/delta", itemId="final", delta="答案", time=1)
        self.event(ledger, "item/completed", item={"type": "agentMessage", "id": "final", "phase": "final_answer", "text": "答案"}, time=2)
        self.assertEqual(ledger.final_answer, "")
        self.complete_root(ledger, time=2.5)
        self.assertEqual(ledger.final_answer, "答案")
        self.assertEqual(ledger.metrics(PRICES, PROTOCOL, 3)["final_ttft_s"], 1)

    def test_authoritative_final_excludes_abandoned_retry_deltas(self):
        ledger = self.ledger()
        for item_id, text, time in (("retry-1", "被中断的正文", 1), ("retry-2", "半张表格|", 2)):
            self.event(ledger, "item/started", time=time, item={"type": "agentMessage", "id": item_id, "phase": "final_answer"})
            self.event(ledger, "item/agentMessage/delta", time=time+.1, itemId=item_id, delta=text)
            self.event(ledger, "error", time=time+.2, willRetry=True)
        self.response(ledger)
        self.final(ledger)
        self.complete_root(ledger)
        self.reconcile(ledger)
        self.assertEqual(ledger.final_answer, "答案")
        self.assertEqual([entry["id"] for entry in ledger.final_messages], ["final"])
        metrics = ledger.metrics(PRICES, PROTOCOL, 9)
        self.assertEqual(metrics["final_ttft_s"], 5.1)
        self.assertEqual(metrics["answer_ready_s"], 5.5)
        self.assertEqual(metrics["tree_output_tokens"], 20)
        self.assertEqual(digest(ledger.snapshot()), digest(TreeLedger.from_snapshot(ledger.snapshot()).snapshot()))

    def test_authoritative_final_does_not_join_duplicate_completed_item(self):
        ledger = self.ledger()
        self.event(ledger, "item/completed", time=2, item={"type": "agentMessage", "id": "retired", "phase": "final_answer", "text": "答案"})
        self.final(ledger)
        self.event(ledger, "turn/completed", time=6, turn={"id": "root-turn", "status": "completed", "items": [
            {"type": "agentMessage", "id": "final", "phase": "final_answer", "text": "答案"}]})
        self.assertEqual(ledger.final_answer, "答案")
        self.assertEqual(len(ledger.final_messages), 1)

    def test_empty_final_and_late_standalone_items_do_not_change_authoritative_answer(self):
        ledger = self.completed()
        before = ledger.metrics(PRICES, PROTOCOL, 9)
        self.event(ledger, "item/completed", time=10, item={"type": "agentMessage", "id": "empty", "phase": "final_answer", "text": ""})
        self.event(ledger, "item/completed", time=11, item={"type": "agentMessage", "id": "final", "phase": "final_answer", "text": "迟到的另一版本"})
        self.assertEqual(ledger.final_answer, "答案")
        self.assertEqual(ledger.answer_ready_s, 5.5)
        self.assertEqual(ledger.metrics(PRICES, PROTOCOL, 9)["final_ttft_s"], before["final_ttft_s"])
        self.assertEqual(digest(ledger.snapshot()), digest(TreeLedger.from_snapshot(ledger.snapshot()).snapshot()))

    def test_authoritative_final_follows_turn_item_order_and_ignores_empty_items(self):
        ledger = self.ledger()
        for item_id, text in (("a", "第一部分"), ("b", "第二部分"), ("empty", "  ")):
            self.event(ledger, "item/completed", time=2, item={"type": "agentMessage", "id": item_id, "phase": "final_answer", "text": text})
        self.event(ledger, "turn/completed", time=3, turn={"id": "root-turn", "status": "completed", "items": [
            {"type": "agentMessage", "id": "b", "phase": "final_answer", "text": "第二部分"},
            {"type": "agentMessage", "id": "empty", "phase": "final_answer", "text": "  "},
            {"type": "agentMessage", "id": "a", "phase": "final_answer", "text": "第一部分"}]})
        self.assertEqual(ledger.final_answer, "第二部分\n\n第一部分")

    def test_missing_authoritative_final_is_not_guessed_from_completed_item(self):
        ledger = self.ledger()
        self.final(ledger)
        self.event(ledger, "turn/completed", time=6, turn={"id": "root-turn", "status": "completed"})
        self.assertEqual(ledger.final_answer, "")
        self.assertIsNone(ledger.answer_ready_s)
        self.assertIsNone(ledger.metrics(PRICES, PROTOCOL, 9)["final_ttft_s"])
        self.assertIn("Missing authoritative root final-answer completion", ledger.accounting_issues())
        self.complete_root(ledger, time=7)
        self.assertEqual(ledger.final_answer, "答案")
        self.assertNotIn("Missing authoritative root final-answer completion", ledger.accounting_issues())

    def test_discarded_stream_mismatch_does_not_override_completed_turn_final(self):
        ledger = self.ledger()
        self.event(ledger, "item/agentMessage/delta", time=1, itemId="retired", delta="旧流")
        self.event(ledger, "item/completed", time=2, item={"type": "agentMessage", "id": "retired", "phase": "final_answer", "text": "被放弃的重试"})
        self.final(ledger)
        self.event(ledger, "turn/completed", time=6, turn={"id": "root-turn", "status": "completed", "items": [
            {"type": "agentMessage", "id": "final", "phase": "final_answer", "text": "答案"}]})
        self.assertEqual(ledger.final_answer, "答案")
        self.assertEqual(ledger.errors, [])

    def test_authoritative_final_stream_mismatch_is_still_rejected(self):
        ledger = self.ledger()
        self.event(ledger, "item/agentMessage/delta", time=1, itemId="final", delta="流中的答案")
        self.event(ledger, "turn/completed", time=2, turn={"id": "root-turn", "status": "completed", "items": [
            {"type": "agentMessage", "id": "final", "phase": "final_answer", "text": "不同的完成答案"}]})
        self.assertIn("Final answer stream disagrees with completed message", ledger.errors)

    def test_legacy_snapshot_keeps_original_policy_unless_repair_is_explicit(self):
        ledger = TreeLedger("root", "root-turn", {}, final_answer_policy="legacy")
        self.event(ledger, "item/started", time=1, item={"type": "agentMessage", "id": "retired", "phase": "final_answer"})
        self.event(ledger, "item/agentMessage/delta", time=1.1, itemId="retired", delta="中断文本")
        self.final(ledger)
        self.event(ledger, "turn/completed", time=6, turn={"id": "root-turn", "status": "completed", "items": [
            {"type": "agentMessage", "id": "final", "phase": "final_answer", "text": "答案"}]})
        original = ledger.snapshot()
        self.assertNotIn("final_answer_policy", original)
        self.assertEqual(ledger.final_answer, "中断文本\n\n答案")
        self.assertEqual(TreeLedger.from_snapshot(original).snapshot(), original)
        repaired = TreeLedger.from_snapshot(original, final_answer_policy="turn-completed-v1")
        self.assertEqual(repaired.final_answer, "答案")
        self.assertEqual(repaired.snapshot()["final_answer_policy"], "turn-completed-v1")
        self.assertEqual(repaired.events, original["events"])

    def test_late_middle_turn_completion_does_not_displace_newer_active_turn(self):
        observations = [
            ("turn/started", {"turn": {"id": "latest", "status": "inProgress"}}),
            ("item/agentMessage/delta", {"itemId": "latest-final", "delta": "尚未完成"}),
            ("rawResponse/completed", {"responseId": "latest-response", "usage": usage()}),
        ]
        for method, params in observations:
            with self.subTest(first_latest_event=method):
                ledger = self.ledger()
                self.final(ledger)
                self.complete_root(ledger, time=10)
                self.event(ledger, method, turn_id="latest", time=30, **params)
                self.event(ledger, "turn/completed", turn_id="middle", time=20,
                           turn={"id": "middle", "status": "completed", "items": [
                               {"type": "agentMessage", "id": "middle-final", "phase": "final_answer", "text": "旧根turn答案"}]})
                self.assertEqual(ledger.root_turn_order, ["root-turn", "middle", "latest"])
                self.assertEqual(ledger.root_status, "inProgress")
                self.assertEqual(ledger.final_answer, "")
                self.assertIsNone(ledger.answer_ready_s)
                self.assertIsNone(ledger.root_turn_completed_s)
                self.assertEqual(digest(ledger.snapshot()), digest(TreeLedger.from_snapshot(ledger.snapshot()).snapshot()))

    def test_post_terminal_observations_do_not_move_queued_followup_into_history(self):
        observations = [
            ("item/agentMessage/delta", {"itemId": "orphan", "delta": "迟到文本"}),
            ("rawResponse/completed", {"responseId": "late-response", "usage": usage()}),
            ("turn/completed", {"turn": {"id": "root-turn", "status": "completed"}}),
            ("reconcile", {}),
        ]
        for method, params in observations:
            with self.subTest(late_old_observation=method):
                ledger = self.ledger()
                self.final(ledger)
                self.complete_root(ledger, time=10)
                if method == "reconcile":
                    ledger.reconcile_thread({"id": "root", "status": "idle"}, 100)
                else:
                    self.event(ledger, method, time=100, **params)
                self.event(ledger, "turn/started", turn_id="latest", time=50,
                           turn={"id": "latest", "status": "inProgress"})
                self.assertEqual(ledger.root_turn_order, ["root-turn", "latest"])
                self.assertEqual(ledger.final_answer, "")
                self.event(ledger, "turn/completed", turn_id="latest", time=70,
                           turn={"id": "latest", "status": "completed", "items": [
                               {"type": "agentMessage", "id": "latest-final", "phase": "final_answer", "text": "新根turn答案"}]})
                self.assertEqual(ledger.root_status, "completed")
                self.assertEqual(ledger.final_answer, "新根turn答案")
                self.assertEqual(digest(ledger.snapshot()), digest(TreeLedger.from_snapshot(ledger.snapshot()).snapshot()))

    def test_duplicate_completion_without_items_cannot_clear_confirmed_final(self):
        ledger = self.completed()
        self.event(ledger, "turn/completed", time=10, turn={"id": "root-turn", "status": "completed"})
        self.assertEqual(ledger.final_answer, "答案")
        self.assertEqual(ledger.answer_ready_s, 5.5)
        self.assertEqual(ledger.errors, [])

    def test_conflicting_terminal_finals_fail_closed_at_different_observation_times(self):
        for time in (4, 10):
            with self.subTest(conflicting_time=time):
                ledger = self.completed()
                self.event(ledger, "turn/completed", time=time, turn={"id": "root-turn", "status": "completed", "items": [
                    {"type": "agentMessage", "id": "other-final", "phase": "final_answer", "text": "冲突的完成答案"}]})
                self.assertEqual(ledger.final_answer, "答案")
                self.assertIn("Conflicting authoritative final completion", ledger.errors)
                self.assertFalse(ledger.accounting_complete)

    def test_raw_reasoning_errors_metadata_and_commands_are_not_archived(self):
        ledger = self.completed()
        self.assertIsNone(self.event(ledger, "rawResponseItem/completed", item={"type": "reasoning", "content": "PRIVATE-REASONING"}))
        self.event(ledger, "item/completed", item={"type": "reasoning", "id": "reason", "summary": ["PRIVATE-SUMMARY"], "content": ["PRIVATE-REASONING"]})
        self.event(ledger, "error", message="SECRET-ERROR", additionalDetails="SECRET-TOKEN")
        self.event(ledger, "rawResponse/completed", responseId="response-1", usage=usage(), usageMetadata={"amount": "SECRET-CHARGE", "metadata": {"token": "SECRET-TOKEN"}})
        self.event(ledger, "item/completed", item={"type": "commandExecution", "id": "cmd", "command": "SECRET-COMMAND", "aggregatedOutput": "SECRET-OUTPUT"})
        snapshot = json.dumps(ledger.snapshot())
        for secret in ("PRIVATE-REASONING", "PRIVATE-SUMMARY", "SECRET-ERROR", "SECRET-TOKEN", "SECRET-CHARGE", "SECRET-COMMAND", "SECRET-OUTPUT"):
            self.assertNotIn(secret, snapshot)

    def test_decode_tps_not_computed_from_whole_tree_tokens(self):
        ledger = self.completed()
        result = ledger.metrics(PRICES, PROTOCOL, 9)
        self.assertIsNone(result["visible_decode_tps"])
        self.assertIsNone(result["visible_e2e_tps"])
        self.assertEqual(result["root_output_e2e_tps"], 20/5.5)
        self.assertEqual(result["tree_output_e2e_tps"], 20/9)

    def test_snapshot_replay_is_byte_equivalent(self):
        ledger = self.completed()
        self.event(ledger, "thread/tokenUsage/updated", tokenUsage={"total": usage(), "last": usage()})
        snapshot = ledger.snapshot()
        restored = TreeLedger.from_snapshot(snapshot)
        self.assertEqual(digest(snapshot), digest(restored.snapshot()))
        self.assertEqual(restored.metrics(PRICES, PROTOCOL, 9), ledger.metrics(PRICES, PROTOCOL, 9))
        tampered = copy.deepcopy(snapshot)
        tampered["responses"][0]["usage"]["inputTokens"] += 1
        self.assertNotEqual(digest(tampered), digest(TreeLedger.from_snapshot(tampered).snapshot()))

    def test_late_relation_revokes_previous_discovery_completion(self):
        ledger = self.completed()
        self.assertTrue(ledger.accounting_complete)
        self.child(ledger, time=10)
        self.assertFalse(ledger.discovery_complete)
        self.assertFalse(ledger.tree_terminal)
        self.assertFalse(ledger.accounting_complete)

    def test_invalid_usage_counters_do_not_become_estimates(self):
        ledger = self.completed()
        invalid = usage()
        invalid.update(outputTokens=True, totalTokens=101)
        self.response(ledger, "invalid-response", counters=invalid)
        self.assertFalse(ledger.accounting_complete)
        self.assertIsNone(ledger.metrics(PRICES, PROTOCOL, 9)["estimated_api_usd"])

    def test_parent_completion_activity_is_not_a_structural_parent(self):
        ledger = self.completed()
        self.child(ledger)
        self.child(ledger, "grandchild", "child")
        self.event(ledger, "item/completed", "root", "root-turn", item={"type": "subAgentActivity", "id": "done-grand",
                   "kind": "completed", "agentThreadId": "grandchild"})
        self.assertEqual(ledger.nodes["grandchild"]["parent_thread_id"], "child")
        self.assertEqual(ledger.errors, [])

    def test_thread_metadata_corrects_non_authoritative_activity_hint(self):
        ledger = self.ledger()
        self.child(ledger, "grandchild", "root")
        self.child(ledger, "child", "root")
        ledger.reconcile_thread({"id": "grandchild", "parentThreadId": "child", "status": {"type": "idle"}})
        self.assertEqual(ledger.nodes["grandchild"]["parent_thread_id"], "child")
        self.assertEqual(ledger.nodes["grandchild"]["parent_source"], "thread_metadata")
        self.assertEqual(ledger.errors, [])

    def test_missing_last_child_response_is_detected_without_adding_history(self):
        ledger = self.completed()
        self.child(ledger)
        self.response(ledger, "child-response", "child", "child-turn")
        self.event(ledger, "thread/tokenUsage/updated", "child", "child-turn", tokenUsage={"total": usage(1000000), "last": usage(123)})
        self.reconcile(ledger)
        self.assertIn("Latest response usage disagrees with last counters: child", ledger.accounting_issues())
        self.assertEqual(ledger.metrics(PRICES, PROTOCOL, 10)["child_observed_input_tokens"], 100)
        self.assertIsNone(ledger.metrics(PRICES, PROTOCOL, 10)["child_input_tokens"])

    def test_followup_root_turn_is_counted_and_latest_final_is_delivered(self):
        ledger = self.completed()
        self.event(ledger, "turn/started", "root", "root-followup", time=10, turn={"id": "root-followup", "status": "inProgress"})
        self.assertFalse(ledger.root_completed)
        self.assertFalse(ledger.tree_terminal)
        self.assertIsNone(ledger.answer_ready_s)
        self.response(ledger, "followup-response", "root", "root-followup", time=11)
        self.event(ledger, "item/started", "root", "root-followup", time=12, item={"type": "agentMessage", "id": "final-2", "phase": "final_answer"})
        self.event(ledger, "item/agentMessage/delta", "root", "root-followup", time=12.1, itemId="final-2", delta="复核后的答案")
        self.event(ledger, "item/completed", "root", "root-followup", time=13, item={"type": "agentMessage", "id": "final-2", "phase": "final_answer", "text": "复核后的答案"})
        self.event(ledger, "turn/completed", "root", "root-followup", time=14, turn={"id": "root-followup", "status": "completed", "items": [
            {"type": "agentMessage", "id": "final-2", "phase": "final_answer", "text": "复核后的答案"}]})
        self.reconcile(ledger, time=15)
        result = ledger.metrics(PRICES, PROTOCOL, 15)
        self.assertTrue(ledger.root_completed)
        self.assertTrue(ledger.accounting_complete)
        self.assertEqual(ledger.final_answer, "复核后的答案")
        self.assertEqual(result["root_input_tokens"], 200)
        self.assertEqual(result["answer_ready_s"], 13)
        self.assertEqual(result["root_turn_completed_s"], 14)
        self.assertEqual(digest(ledger.snapshot()), digest(TreeLedger.from_snapshot(ledger.snapshot()).snapshot()))

    def test_turn_payload_repeats_message_without_moving_delivery_time(self):
        ledger = self.completed()
        self.event(ledger, "turn/completed", time=10, turn={"id": "root-turn", "status": "completed", "items": [
            {"type": "agentMessage", "id": "final", "phase": "final_answer", "text": "答案"}]})
        self.assertEqual(ledger.answer_ready_s, 5.5)
        self.assertEqual(ledger.root_turn_completed_s, 10)

    def test_usage_before_lost_turn_start_ack_is_retained(self):
        ledger = TreeLedger("root", None, {"model": "sol", "serviceTier": "priority"})
        self.assertEqual(ledger.nodes["root"]["turns"], {})
        self.assertFalse(ledger.root_completed)
        self.response(ledger)
        ledger.mark_transport_incomplete()
        self.assertEqual(ledger.root_turn_id, "root-turn")
        self.assertEqual(ledger.root_turn_order, ["root-turn"])
        result = ledger.metrics(PRICES, PROTOCOL, 5)
        self.assertEqual(result["root_observed_input_tokens"], 100)
        self.assertGreater(result["tree_observed_api_usd"], 0)
        self.assertIsNone(result["estimated_api_usd"])
        self.assertEqual(digest(ledger.snapshot()), digest(TreeLedger.from_snapshot(ledger.snapshot()).snapshot()))

    def test_bind_root_turn_after_pre_ack_events_is_idempotent_for_usage(self):
        ledger = TreeLedger("root", None, {"model": "sol", "serviceTier": "priority"})
        self.response(ledger)
        ledger.bind_root_turn("root-turn")
        self.final(ledger)
        self.complete_root(ledger)
        self.reconcile(ledger)
        self.assertEqual(len(ledger.responses), 1)
        self.assertTrue(ledger.accounting_complete)
        self.assertEqual(ledger.metrics(PRICES, PROTOCOL, 9)["input_tokens"], 100)
        self.assertEqual(digest(ledger.snapshot()), digest(TreeLedger.from_snapshot(ledger.snapshot()).snapshot()))

    def test_conflicting_admission_receipt_is_protocol_error(self):
        ledger = TreeLedger("root", None, {})
        self.response(ledger)
        ledger.bind_root_turn("different-turn")
        self.assertEqual(ledger.root_turn_id, "root-turn")
        self.assertIn("Turn admission receipt conflicts with observed root turn", ledger.errors)

    def test_cumulative_usage_uses_latest_observation_and_keeps_archive_order(self):
        ledger = self.completed()
        current = {"total": usage(), "last": usage()}
        old = {"total": usage(1000, 10), "last": usage(1000, 10)}
        self.event(ledger, "thread/tokenUsage/updated", time=8, tokenUsage=current)
        self.event(ledger, "thread/tokenUsage/updated", time=2, tokenUsage=old)
        self.assertEqual(ledger.nodes["root"]["latest_cumulative_usage"], current)
        self.assertEqual(ledger.nodes["root"]["latest_cumulative_usage_t_s"], 8)
        self.assertEqual([e["t_s"] for e in ledger.events[-2:]], [8, 2])
        self.assertTrue(ledger.accounting_complete)
        self.assertEqual(digest(ledger.snapshot()), digest(TreeLedger.from_snapshot(ledger.snapshot()).snapshot()))

    def test_newer_cumulative_replaces_whole_observation_instead_of_field_maxima(self):
        ledger = self.completed()
        self.event(ledger, "thread/tokenUsage/updated", time=7, tokenUsage={"total": usage(1000, 10), "last": usage(1000, 10)})
        self.event(ledger, "thread/tokenUsage/updated", time=8, tokenUsage={"total": usage(), "last": usage()})
        self.assertEqual(ledger.nodes["root"]["latest_cumulative_usage"], {"total": usage(), "last": usage()})
        self.assertTrue(ledger.accounting_complete)

    def test_cumulative_same_time_duplicates_are_idempotent_and_conflicts_are_anomalies(self):
        ledger = self.completed()
        for _ in range(2):
            self.event(ledger, "thread/tokenUsage/updated", time=8, tokenUsage={"total": usage(), "last": usage()})
        self.assertTrue(ledger.accounting_complete)
        self.event(ledger, "thread/tokenUsage/updated", time=8, tokenUsage={"total": usage(101), "last": usage(101)})
        self.assertIn("Conflicting cumulative usage at same observation time: root", ledger.accounting_issues())
        self.assertEqual(ledger.nodes["root"]["latest_cumulative_usage"], {"total": usage(), "last": usage()})
        self.assertIsNone(ledger.metrics(PRICES, PROTOCOL, 9)["estimated_api_usd"])
        self.assertEqual(sum(e["method"] == "thread/tokenUsage/updated" for e in ledger.events), 3)

    def test_conflict_between_two_stale_cumulative_observations_is_still_an_anomaly(self):
        ledger = self.completed()
        self.event(ledger, "thread/tokenUsage/updated", time=8, tokenUsage={"total": usage(), "last": usage()})
        self.event(ledger, "thread/tokenUsage/updated", time=2, tokenUsage={"total": usage(50), "last": usage(50)})
        self.event(ledger, "thread/tokenUsage/updated", time=2, tokenUsage={"total": usage(51), "last": usage(51)})
        self.assertEqual(ledger.nodes["root"]["latest_cumulative_usage"], {"total": usage(), "last": usage()})
        self.assertIn("Conflicting cumulative usage at same observation time: root", ledger.accounting_issues())

    def test_stale_thread_and_turn_notifications_do_not_reactivate_completed_work(self):
        ledger = self.completed()
        self.event(ledger, "thread/status/changed", time=2, status={"type": "active"})
        self.event(ledger, "turn/started", time=1, turn={"id": "root-turn", "status": "inProgress"})
        self.event(ledger, "thread/started", time=0, thread={"id": "root", "status": {"type": "active"}})
        self.assertTrue(ledger.root_completed)
        self.assertEqual(ledger.nodes["root"]["status"], "idle")
        self.assertTrue(ledger.tree_terminal)
        self.assertTrue(ledger.accounting_complete)
        self.assertEqual(digest(ledger.snapshot()), digest(TreeLedger.from_snapshot(ledger.snapshot()).snapshot()))

    def test_stale_root_completion_does_not_regress_delivery_metric(self):
        ledger = self.completed()
        self.event(ledger, "turn/completed", time=5, turn={"id": "root-turn", "status": "completed"})
        self.assertEqual(ledger.root_turn_completed_s, 6)
        self.assertEqual(ledger.metrics(PRICES, PROTOCOL, 9)["root_turn_completed_s"], 6)

    def test_unknown_stale_root_turn_preserves_current_answer_and_bound_turn(self):
        for reconciled in (False, True):
            with self.subTest(reconciled=reconciled):
                ledger = self.ledger()
                self.response(ledger)
                self.final(ledger)
                self.complete_root(ledger)
                if reconciled:
                    self.reconcile(ledger)
                before = (ledger.answer_ready_s, ledger.root_turn_completed_s, ledger.discovery_complete)
                self.event(ledger, "turn/started", turn_id="old-turn", time=2,
                           turn={"id": "old-turn", "status": "inProgress"})
                self.event(ledger, "item/completed", turn_id="old-turn", time=3,
                           item={"type": "agentMessage", "id": "old-final", "phase": "final_answer", "text": "历史答案"})
                self.event(ledger, "turn/completed", turn_id="old-turn", time=4,
                           turn={"id": "old-turn", "status": "completed"})
                ledger.bind_root_turn("root-turn")
                self.assertEqual(ledger.root_turn_id, "root-turn")
                self.assertEqual(ledger.root_turn_order, ["old-turn", "root-turn"])
                self.assertEqual(ledger.final_answer, "答案")
                self.assertTrue(ledger.root_completed)
                self.assertEqual(before, (ledger.answer_ready_s, ledger.root_turn_completed_s, ledger.discovery_complete))
                self.assertEqual(digest(ledger.snapshot()), digest(TreeLedger.from_snapshot(ledger.snapshot()).snapshot()))
                if reconciled:
                    self.assertTrue(ledger.accounting_complete)

    def test_unknown_stale_root_turn_does_not_displace_genuine_active_followup(self):
        ledger = self.completed()
        self.event(ledger, "turn/started", turn_id="followup", time=10,
                   turn={"id": "followup", "status": "inProgress"})
        self.event(ledger, "turn/started", turn_id="old-turn", time=2,
                   turn={"id": "old-turn", "status": "inProgress"})
        self.assertEqual(ledger.root_turn_order[-1], "followup")
        self.assertEqual(ledger.root_status, "inProgress")
        self.assertEqual(ledger.nodes["root"]["turns"]["old-turn"], "terminalObservedIdle")
        self.assertFalse(ledger.root_completed)
        self.assertIsNone(ledger.answer_ready_s)

    def test_idle_reconciliation_settles_late_unknown_child_turn_but_allows_new_followup(self):
        ledger = self.completed()
        self.child(ledger, time=1)
        self.settings(ledger, "child", time=1)
        self.response(ledger, "child-response", "child", "child-turn", time=4)
        self.reconcile(ledger, time=9)
        self.event(ledger, "turn/started", "child", "child-turn", time=2, turn={"id": "child-turn", "status": "inProgress"})
        self.event(ledger, "thread/status/changed", "child", "child-turn", time=2, status={"type": "active"})
        self.assertEqual(ledger.nodes["child"]["turns"]["child-turn"], "terminalObservedIdle")
        self.assertTrue(ledger.tree_terminal)
        self.assertTrue(ledger.accounting_complete)
        self.event(ledger, "turn/started", "child", "new-turn", time=10, turn={"id": "new-turn", "status": "inProgress"})
        self.assertEqual(ledger.nodes["child"]["turns"]["new-turn"], "inProgress")
        self.assertFalse(ledger.tree_terminal)

    def test_late_earlier_settings_reprice_responses_but_not_from_later_receipts(self):
        ledger = self.ledger()
        self.response(ledger, time=4)
        ledger.reconcile_settings("root", {"model": "sol", "serviceTier": "priority"}, 6)
        self.settings(ledger, "root", "astra", "default", time=2)
        self.settings(ledger, "root", "sol", "priority", time=1)
        response = ledger.responses["response-1"]
        self.assertEqual((response["settings"]["model"], response["settings"]["serviceTier"]), ("astra", "default"))
        self.assertEqual((ledger.nodes["root"]["settings"]["model"], ledger.nodes["root"]["settings"]["serviceTier"]), ("sol", "priority"))
        self.final(ledger)
        self.complete_root(ledger)
        self.reconcile(ledger)
        self.assertAlmostEqual(ledger.metrics(PRICES, PROTOCOL, 9)["estimated_api_usd"], (90*6+10*.6+20*30)/1e6)
        self.assertEqual(digest(ledger.snapshot()), digest(TreeLedger.from_snapshot(ledger.snapshot()).snapshot()))

    def test_first_late_metadata_or_rpc_receipt_does_not_fill_unknown_past_child_settings(self):
        ledger = self.ledger()
        self.response(ledger, "child-response", "child", "child-turn", time=4)
        ledger.reconcile_thread({"id": "child", "parentThreadId": "root", "status": {"type": "idle"}, "model": "astra", "serviceTier": "default"}, 8)
        ledger.reconcile_settings("child", {"model": "astra", "serviceTier": "default"}, 9)
        self.assertNotIn("serviceTier", ledger.responses["child-response"]["settings"])
        self.settings(ledger, "child", "astra", "default", time=2)
        self.assertEqual(ledger.responses["child-response"]["settings"]["serviceTier"], "default")

    def test_settings_same_time_conflict_is_unknown_and_not_arrival_order_dependent(self):
        for models in (("astra", "sol"), ("sol", "astra")):
            ledger = self.ledger()
            self.settings(ledger, "root", models[0], "default", time=2)
            self.response(ledger, time=4)
            self.settings(ledger, "root", models[1], "default", time=2)
            self.assertIsNone(ledger.responses["response-1"]["settings"]["model"])
            self.assertIn("Conflicting settings at same observation time: root", ledger.accounting_issues())

    def test_nested_stale_notifications_preserve_terminal_state_and_response_totals(self):
        ledger = self.ledger()
        for tid, parent in (("child", "root"), ("grandchild", "child")):
            self.child(ledger, tid, parent, time=1)
            self.settings(ledger, tid, time=1)
            self.response(ledger, tid+"-response", tid, tid+"-turn", time=4)
            self.event(ledger, "thread/tokenUsage/updated", tid, tid+"-turn", time=8,
                       tokenUsage={"total": usage(1000000), "last": usage()})
        self.response(ledger)
        self.final(ledger)
        self.complete_root(ledger)
        self.reconcile(ledger, time=9)
        for tid in ("child", "grandchild"):
            self.event(ledger, "thread/tokenUsage/updated", tid, tid+"-turn", time=2,
                       tokenUsage={"total": usage(500000), "last": usage(50)})
            self.event(ledger, "thread/status/changed", tid, tid+"-turn", time=2, status={"type": "active"})
            self.event(ledger, "turn/started", tid, tid+"-turn", time=2, turn={"id": tid+"-turn", "status": "inProgress"})
        self.assertTrue(ledger.accounting_complete)
        self.assertEqual(ledger.metrics(PRICES, PROTOCOL, 10)["tree_input_tokens"], 300)
        self.assertEqual(len(ledger.responses), 3)
        self.assertEqual(digest(ledger.snapshot()), digest(TreeLedger.from_snapshot(ledger.snapshot()).snapshot()))


if __name__ == "__main__":
    unittest.main()
