"""Offline mixed-speed scheduling, pricing, and export regressions."""
import collections
import csv
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import core
from benchmark import parser, retest
from reporting import blind, build_report, seal, validate
from test_benchmark import fixture, write_attempt, RATES


class ServiceTierTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="benchmark-tier-")
        self.base = Path(self.temp.name)
        self.case, self.legacy_prices = fixture(self.base)
        self.prices = self.base / "tier-prices.json"
        core.write(self.prices, {"schema_version": 2, "as_of": "2026-10-20",
            "sources": ["https://developers.openai.com/api/docs/pricing"],
            "service_tiers": {t: {"models": {m: {k:v*factor for k,v in RATES.items()} for m in core.MODELS[:2]}}
                              for t, factor in [("default", 1), ("priority", 2)]}})

    def tearDown(self):
        self.temp.cleanup()

    def prepare(self, repetitions=1):
        return core.prepare(self.case, self.base / "run", core.MODELS[:2], core.EFFORTS, repetitions,
                            self.prices, service_tiers=["default", "fast"])

    def complete(self, root):
        protocol = core.check_inputs(root)
        for slot in protocol["schedule"]:
            result = write_attempt(root, slot)
            for key in core.METRICS:
                result.pop(key, None)
            tier = slot["service_tier"]
            result.update(service_tier=tier)
            result["effective"]["serviceTier"] = tier
            result.update(core.metrics_for_protocol(protocol, result["usage"], 4, 1, 3,
                (root / "runs" / slot["id"] / "attempt-001/answer.txt").read_text(encoding="utf-8"),
                core.price_for(core.read(self.prices), slot["model"], tier)))
            core.write(root / "runs" / slot["id"] / "attempt-001/result.json", result)
        blind(root)
        scores = core.read(root / "grading/packet/scores-template.json")
        for item in scores.values():
            item.update(points={"A1":70, "R1":29}, notes={"A1":"输入语义正确", "R1":"表述清楚"})
        core.write(self.base / "scores.json", scores)
        seal(root, self.base / "scores.json")
        return build_report(root)

    def test_schedule_sixty_balanced_and_frozen(self):
        schedule = core.build_schedule(core.MODELS[:2], core.EFFORTS, 5, 17, ["default", "fast"])
        self.assertEqual(len(schedule), 60)
        self.assertEqual(schedule, core.build_schedule(core.MODELS[:2], core.EFFORTS, 5, 17, ["default", "priority"]))
        counts = collections.Counter((x["model"], x["effort"], x["service_tier"]) for x in schedule)
        self.assertEqual(len(counts), 12)
        self.assertEqual(set(counts.values()), {5})
        self.assertTrue(all(x["model"] != "gpt-6-sol" for x in schedule))
        for repetition in range(1, 6):
            self.assertEqual(len({(x["model"],x["effort"],x["service_tier"]) for x in schedule if x["repetition"] == repetition}), 12)
        root = self.prepare(5)
        protocol = core.check_inputs(root)
        self.assertEqual(protocol["schema_version"], 2)
        self.assertEqual(protocol["service_tiers"], ["default", "priority"])
        self.assertEqual(len(protocol["schedule"]), 60)

    def test_cli_and_defaults_preserved(self):
        args = parser().parse_args(["prepare", "--case-dir", "x", "--service-tiers", "default", "fast"])
        self.assertEqual(args.service_tiers, ["default", "fast"])
        self.assertIsNone(parser().parse_args(["prepare", "--case-dir", "x"]).service_tiers)
        self.assertEqual(len(core.build_schedule(core.MODELS, core.EFFORTS, 5, 17)), 45)
        self.assertTrue(all("service_tier" not in s for s in core.build_schedule(core.MODELS, core.EFFORTS, 1, 17)))
        for tiers in [[], ["fast", "priority"], ["ultrafast"]]:
            with self.assertRaises(ValueError):
                core.normalize_tiers(tiers)

    def test_tier_prices_unknown_and_neutral_metric_names(self):
        snapshot = core.load_prices(self.prices)
        usage = {"inputTokens":1000, "cachedInputTokens":100, "cacheWriteInputTokens":0, "outputTokens":100, "reasoningOutputTokens":60}
        protocol = {"service_tiers":["default", "priority"]}
        a = core.metrics_for_protocol(protocol, usage, 4, 1, 3, "回答", core.price_for(snapshot, core.MODELS[0], "default"))
        b = core.metrics_for_protocol(protocol, usage, 4, 1, 3, "回答", core.price_for(snapshot, core.MODELS[0], "priority"))
        self.assertEqual(b["estimated_api_usd"], a["estimated_api_usd"]*2)
        self.assertNotIn("estimated_standard_api_usd", b)
        self.assertIsNone(core.price_for(core.read(self.legacy_prices), core.MODELS[0], "priority"))
        self.assertIsNone(core.price_for(snapshot, "unknown", "priority"))
        self.assertIsNone(core.metrics_for_protocol(protocol, usage, 4, rates=None)["estimated_api_usd"])
        self.assertIn("estimated_standard_api_usd", core.metrics_for_protocol({}, usage, 4))

    def test_mixed_report_groups_validation_and_retest_inheritance(self):
        root = self.prepare()
        data = self.complete(root)
        self.assertEqual(len(data["conditions"]), 12)
        self.assertEqual(data["valid_completed"], 12)
        self.assertEqual({c["service_tier"] for c in data["conditions"]}, {"default", "priority"})
        self.assertEqual(len(data["tier_comparisons"]), 6)
        with (root / "metrics.csv").open(encoding="utf-8-sig", newline="") as handle:
            fields = csv.DictReader(handle).fieldnames
        self.assertIn("service_tier", fields)
        self.assertIn("estimated_api_usd", fields)
        self.assertNotIn("estimated_standard_api_usd", fields)
        self.assertTrue(validate(root)["passed"])
        child = retest(root, self.base / "child", repetitions=1)
        self.assertEqual(core.check_inputs(child)["service_tiers"], ["default", "priority"])
        single = retest(root, self.base / "single", repetitions=1, service_tiers=["fast"])
        self.assertEqual(len(core.check_inputs(single)["schedule"]), 6)


if __name__ == "__main__":
    unittest.main()
