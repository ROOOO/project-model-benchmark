"""Subscription display estimates remain distinct from historical API accounting."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from billing import SUBSCRIPTION_CREDIT_METRICS, billing_context, format_usd, response_credits, subscription_metrics, subscription_view


def usage(i=1000, c=200, o=400):
    return {"inputTokens": i, "cachedInputTokens": c, "cacheWriteInputTokens": 100,
            "outputTokens": o, "reasoningOutputTokens": 100, "totalTokens": i + o}


def response(thread, model="gpt-6.1-sol", tier="priority"):
    return {"thread_id": thread, "settings": {"model": model, "serviceTier": tier}, "usage": usage()}


def tree():
    return {"slot_id": "1", "attempt": 1, "estimated_api_usd": 123.45,
            "agentic": {"root_thread_id": "root", "accounting_complete": True, "discovery_complete": True,
                        "nodes": [{"thread_id": "root", "parent_thread_id": None},
                                  {"thread_id": "child", "parent_thread_id": "root"}],
                        "responses": [response("root"), response("child", "gpt-6-astra", "default")]}}


class BillingTests(unittest.TestCase):
    def test_subscription_fast_is_2_5_not_purchased_credits_2(self):
        standard = response_credits(usage(), "gpt-6.1-sol", "default")
        self.assertAlmostEqual(standard, 0.1405)
        self.assertAlmostEqual(response_credits(usage(), "gpt-6.1-sol", "priority"), standard * 2.5)
        self.assertAlmostEqual(response_credits(usage(), "gpt-6.1-sol", "fast"), standard * 2.5)

    def test_cache_writes_and_reasoning_not_charged_twice(self):
        counts = usage()
        before = response_credits(counts, "gpt-6.1-sol", "default")
        counts["cacheWriteInputTokens"] = 0
        counts["reasoningOutputTokens"] = 0
        self.assertEqual(response_credits(counts, "gpt-6.1-sol", "default"), before)

    def test_sol_long_context_does_not_inherit_api_surcharge(self):
        counts = usage(300000, 20000, 1000)
        self.assertAlmostEqual(response_credits(counts, "gpt-6.1-sol", "default"), 14.3)

    def test_each_child_uses_observed_model_and_speed(self):
        record = tree()
        record["agentic"]["nodes"].append({"thread_id": "unrelated", "parent_thread_id": None})
        record["agentic"]["responses"].append(response("unrelated"))
        result = subscription_metrics(record)
        self.assertAlmostEqual(result["root_estimated_subscription_credits"], 0.35125)
        self.assertAlmostEqual(result["child_estimated_subscription_credits"], 0.705)
        self.assertAlmostEqual(result["estimated_subscription_credits"], 1.05625)
        self.assertAlmostEqual(result["root_estimated_subscription_usd"], 0.01405)
        self.assertAlmostEqual(result["child_estimated_subscription_usd"], 0.0282)
        self.assertAlmostEqual(result["estimated_subscription_usd"], 0.04225)
        self.assertAlmostEqual(result["estimated_subscription_usd"], result["root_estimated_subscription_usd"] + result["child_estimated_subscription_usd"])
        self.assertEqual(record["estimated_api_usd"], 123.45)

    def test_missing_child_rate_keeps_full_total_unknown(self):
        record = tree()
        record["agentic"]["responses"][1]["settings"]["model"] = "unpriced-model"
        result = subscription_metrics(record)
        self.assertIsNone(result["estimated_subscription_credits"])
        self.assertAlmostEqual(result["observed_subscription_credits"], 0.35125)
        self.assertIsNone(result["child_estimated_subscription_credits"])
        self.assertIsNone(result["estimated_subscription_usd"])
        self.assertAlmostEqual(result["observed_subscription_usd"], 0.01405)

    def test_incomplete_tree_never_promotes_partial_total(self):
        record = tree()
        record["agentic"]["accounting_complete"] = False
        result = subscription_metrics(record)
        self.assertIsNone(result["estimated_subscription_credits"])
        self.assertAlmostEqual(result["observed_subscription_credits"], 1.05625)
        self.assertIsNone(result["estimated_subscription_usd"])
        self.assertAlmostEqual(result["observed_subscription_usd"], 0.04225)

    def test_unknown_usage_models_and_speed_do_not_fall_back(self):
        self.assertIsNone(response_credits(None, "gpt-6.1-sol", "priority"))
        self.assertIsNone(response_credits(usage(), "not-a-model", "priority"))
        self.assertIsNone(response_credits(usage(), "gpt-6.1-sol", "ultrafast"))
        invalid = usage()
        invalid["cachedInputTokens"] = 2000
        self.assertIsNone(response_credits(invalid, "gpt-6.1-sol", "default"))
        self.assertIsNone(subscription_metrics({"model": "gpt-6.1-sol", "estimated_api_usd": 10})["estimated_subscription_credits"])

    def test_view_does_not_rewrite_canonical_summary_or_api_fields(self):
        record = tree()
        row = {"slot_id": "1", "attempt": 1, "model": "gpt-6.1-sol", "effort": "ultra", "service_tier": "priority",
               "selected": True, "valid": True, "status": "completed", "estimated_api_usd": 123.45}
        data = {"rows": [row], "conditions": [{"model": row["model"], "effort": row["effort"],
                 "service_tier": "priority", "statistics": {"estimated_api_usd": {"n": 1, "mean": 123.45}}}]}
        original = deepcopy(data)
        view = subscription_view(data, [record])
        self.assertEqual(data, original)
        self.assertEqual(view["rows"][0]["estimated_api_usd"], 123.45)
        self.assertAlmostEqual(view["conditions"][0]["statistics"]["estimated_subscription_credits"]["mean"], 1.05625)
        self.assertEqual(view["subscription_coverage_attempts"], 1)
        self.assertEqual(view["billing_context"]["speed_multipliers"]["priority"], 2.5)
        self.assertEqual(billing_context()["unit"], "USD")
        self.assertAlmostEqual(view["conditions"][0]["statistics"]["estimated_subscription_usd"]["mean"], 0.04225)
        self.assertAlmostEqual(view["known_subscription_usd"], 0.04225)
        self.assertEqual(view["rows"][0]["billing_unit"], "USD")

    def test_dollar_equivalents_preserve_every_credit_metric(self):
        result = subscription_metrics(tree())
        for key in SUBSCRIPTION_CREDIT_METRICS:
            self.assertAlmostEqual(result[key.removesuffix("_credits") + "_usd"], result[key] / 25)
        record = {"model": "gpt-6.1-sol", "usage": usage(), "service_tier": "default"}
        self.assertAlmostEqual(subscription_metrics(record)["estimated_subscription_usd"], 0.00562)
        record["service_tier"] = "priority"
        self.assertAlmostEqual(subscription_metrics(record)["estimated_subscription_usd"], 0.01405)

    def test_unknown_normalization_never_relabels_credits_or_api_as_dollars(self):
        for factor in [None, 0, -25, True]:
            prices = billing_context()
            prices["usd_equivalent"]["credits_per_usd"] = factor
            result = subscription_metrics(tree(), prices)
            self.assertAlmostEqual(result["estimated_subscription_credits"], 1.05625)
            self.assertIsNone(result["estimated_subscription_usd"])
            self.assertIsNone(result["observed_subscription_usd"])

    def test_usd_formatting_preserves_zero_unknown_and_signed_differences(self):
        self.assertEqual(format_usd(0), "$0.0000")
        self.assertEqual(format_usd(0.7777542), "$0.7778")
        self.assertEqual(format_usd(None), "—")
        self.assertEqual(format_usd(float("nan")), "—")
        self.assertEqual(format_usd(-0.1), "$-0.1000")


if __name__ == "__main__":
    unittest.main()
