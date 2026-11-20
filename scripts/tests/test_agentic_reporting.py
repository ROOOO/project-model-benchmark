"""Offline export/validation regressions for native readonly agent workflows."""
import csv
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import core
from agentic import TreeLedger
from comparison import compare
from compact import compact_artifacts
from reporting import agentic_html, archive, capability_summary, check_artifact_sensitive, execution_identity, native_evidence_files, summarize, validate_records
from test_agentic import usage
from test_benchmark import fixture, RATES, write_attempt
import test_comparison as comparison_fixtures


class AgenticReportingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agentic-export-")
        self.base = Path(self.temp.name)
        self.case, self.prices = fixture(self.base)
        self.source = self.base / "source"
        self.source.mkdir()
        (self.source / "config.py").write_text("def choose(v, d): return d if v is None else v\n", encoding="utf-8")
        self.tier_prices = self.base / "tier-prices.json"
        core.write(self.tier_prices, {"schema_version": 2, "as_of": "2026-10-22",
            "sources": ["https://developers.openai.com/api/docs/pricing"], "service_tiers": {
                tier: {"models": {model: {key: value * factor for key, value in RATES.items()} for model in core.MODELS}}
                for tier, factor in (("default", 1), ("priority", 2))}})

    def tearDown(self):
        self.temp.cleanup()

    def grade(self, root):
        comparison_fixtures.ComparisonTests.grade(self, root)

    def make_run(self, name, tiers=("default",), *, agentic=True, repetitions=1,
                 unknown_child=False, child_model=None, missing_child_usage=False, failed_last=False):
        spec = {"source_roots": [{"path": str(self.source), "logical_path": "project/src"}],
                "model_work_root": str(self.base / (name + "-work"))} if agentic else None
        root = core.prepare(self.case, self.base / name, [core.MODELS[0]], ["ultra"], repetitions,
                            self.tier_prices, service_tiers=list(tiers) if tiers is not None else None, execution_profile=spec)
        protocol = core.check_inputs(root)
        for index, slot in enumerate(protocol["schedule"]):
            if not agentic:
                record = write_attempt(root, slot)
                record["service_tier"] = slot["service_tier"]
                record["effective"]["serviceTier"] = slot["service_tier"]
                for key in core.METRICS:
                    record.pop(key, None)
                record.update(core.metrics_for_protocol(protocol, record["usage"], 4, 1, 3,
                    (root / "runs" / slot["id"] / "attempt-001/answer.txt").read_text(encoding="utf-8"),
                    core.price_for(core.read(self.tier_prices), slot["model"], slot["service_tier"])))
                core.write(root / "runs" / slot["id"] / "attempt-001/result.json", record)
                continue
            directory = root / "runs" / slot["id"] / "attempt-001"
            directory.mkdir(parents=True)
            tid, turn = name + "-" + slot["id"], "turn-" + slot["id"]
            tier = slot.get("service_tier", "default")
            settings = {"model": slot["model"], "modelProvider": "test", "reasoningEffort": "ultra", "serviceTier": tier}
            ledger = TreeLedger(tid, turn, settings)
            def event(method, time=1, target=tid, target_turn=turn, **params):
                ledger.observe({"method": method, "params": {"threadId": target, "turnId": target_turn, **params}}, time)
            event("item/completed", item={"type": "commandExecution", "id": "read", "status": "completed"})
            event("item/started", item={"type": "agentMessage", "id": "progress", "phase": "commentary"})
            event("item/agentMessage/delta", time=1.1, itemId="progress", delta="检查源码")
            event("item/completed", time=1.2, item={"type": "agentMessage", "id": "progress", "phase": "commentary", "text": "检查源码"})
            event("rawResponse/completed", time=4, responseId=tid + "-response", usage=usage())
            if unknown_child or child_model:
                child = tid + "-child"
                model = child_model or "unknown-child"
                event("item/completed", time=2, item={"type": "subAgentActivity", "id": "spawn", "kind": "started",
                      "agentThreadId": child, "model": model, "reasoningEffort": "xhigh"})
                event("thread/settings/updated", time=2.1, target=child, target_turn=None,
                      threadSettings={"model": model, "modelProvider": "test", "effort": "xhigh", "serviceTier": "default"})
                event("rawResponse/completed", time=3, target=child, target_turn="child-turn", responseId=child + "-response", usage=None if missing_child_usage else usage(50, 10))
                ledger.reconcile_thread({"id": child, "status": {"type": "idle"}}, 7)
            failed = failed_last and index == len(protocol["schedule"]) - 1
            answer = "" if failed else "None 使用默认值；空字符串保持原值。建议分别断言这两种输入。"
            if answer:
                event("item/started", time=5, item={"type": "agentMessage", "id": "final", "phase": "final_answer"})
                event("item/agentMessage/delta", time=5.1, itemId="final", delta=answer)
                event("item/completed", time=5.5, item={"type": "agentMessage", "id": "final", "phase": "final_answer", "text": answer})
            event("turn/completed", time=6, turn={"id": turn, "status": "failed" if failed else "completed", "items": [
                {"type": "agentMessage", "id": "final", "phase": "final_answer", "text": answer}] if answer else []})
            ledger.reconcile_thread({"id": tid, "status": {"type": "idle"}}, 8)
            ledger.mark_discovery_complete(9)
            (directory / "answer.txt").write_text(answer, encoding="utf-8")
            (directory / "events.jsonl").write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in ledger.events) + "\n", encoding="utf-8")
            core.write(directory / "task-tree.json", ledger.snapshot())
            record = {"slot_id": slot["id"], "attempt": 1, "model": slot["model"], "effort": slot["effort"],
                      "service_tier": tier, "repetition": slot["repetition"], "valid": not failed,
                      "status": "failed" if failed else "completed", "thread_id": tid, "turn_id": turn,
                      "effective": settings, "usage": None, "tool_items": ["commandExecution"], "warnings": [],
                      "prompt_sha256": core.sha(root / "input/prompt.txt"), "answer_sha256": core.sha(directory / "answer.txt"),
                      "actual_gateway_charge": None, "agentic": ledger.snapshot(),
                      **ledger.metrics(core.read(self.tier_prices), protocol, 9)}
            core.write(directory / "result.json", record)
        self.grade(root)
        return root

    def test_legacy_and_readonly_never_merge_or_fast_pair(self):
        standard = self.make_run("legacy", agentic=False)
        fast = self.make_run("agentic", ("fast",))
        output = self.base / "comparison"
        compare([standard, fast], output)
        data = core.read(output / "comparison-data.json")
        self.assertEqual(len(data["cases"]), 2)
        self.assertEqual(data["counts"]["independent_questions"], 1)
        self.assertEqual({c["execution_mode"] for c in data["cases"]}, {"fixed-evidence", "project-readonly"})
        self.assertEqual(data["paired_speed_observations"], [])
        compact = core.read(output / "compact-data.json")
        self.assertEqual(compact["topic_count"], 2)
        native = next(r for r in compact["rows"] if r["execution_mode"] == "project-readonly")
        condition = next(c for g in data["cases"] for c in g["conditions"] if c["id"] == native["condition_id"])
        for metric in ("answer_ready_s", "workflow_elapsed_s", "tree_input_tokens", "tree_output_tokens", "tree_estimated_api_usd"):
            for field in ("median", "mean", "n"):
                self.assertEqual(native[metric + "_" + field], condition["statistics"][metric][field])
        page = (output / "compact.html").read_text(encoding="utf-8")
        self.assertIn("答案交付（秒）", page)
        self.assertIn("全部任务结束（秒）", page)

    def test_same_snapshot_cross_run_fingerprint_ignores_work_paths(self):
        standard = self.make_run("std")
        fast = self.make_run("fast", ("fast",))
        self.assertEqual(execution_identity(core.check_inputs(standard)), execution_identity(core.check_inputs(fast)))
        output = self.base / "comparison"
        compare([standard, fast], output)
        data = core.read(output / "comparison-data.json")
        self.assertEqual(len(data["cases"]), 1)
        self.assertEqual(data["paired_speed_observations"][0]["kind"], "historical-observation")

    def test_cli_overview_keeps_modes_metrics_and_unknown_cost_separate(self):
        readonly = self.make_run("readonly")
        cli_fixture = self.make_run("cli-fixture", agentic=False)
        output = self.base / "comparison"
        compare([readonly, cli_fixture], output)
        data = core.read(output / "comparison-data.json")
        cli_case = next(c for c in data["cases"] if c["execution_mode"] == "fixed-evidence")
        cli_case["execution_mode"] = "project-full-access-cli"
        for condition in cli_case["conditions"]:
            condition.update(execution_mode="project-full-access-cli", model="gemini-4-neon-xhigh", effort="xhigh")
            for key in ("estimated_subscription_usd", "estimated_subscription_credits", "estimated_api_usd"):
                condition["statistics"][key] = core.distribution([])
        artifacts = compact_artifacts(data, "sealed-source-hash")
        compact = json.loads(artifacts["compact-data.json"])
        overview = json.loads(artifacts["question-overview.json"])
        self.assertEqual(compact["topic_count"], 2)
        self.assertEqual(len(overview["topics"]), 1)
        self.assertFalse(overview["statistics_merged"])
        self.assertEqual(overview["additional_model_calls"], 0)
        rows = {r["execution_mode"]: r for r in overview["rows"]}
        cli = rows["project-full-access-cli"]
        native = rows["project-readonly"]
        self.assertEqual(cli["delivery_metric"], "elapsed_s")
        self.assertEqual(cli["delivery_s_median"], cli["elapsed_s_median"])
        self.assertIsNone(cli["workflow_s_median"])
        self.assertEqual(cli["workflow_s_n"], 0)
        self.assertIsNone(cli["estimated_subscription_usd_mean"])
        self.assertEqual(cli["estimated_subscription_usd_n"], 0)
        self.assertEqual(cli["token_basis"], "CLI 原生累计")
        self.assertEqual(native["delivery_s_median"], native["answer_ready_s_median"])
        self.assertEqual(native["observed_input_tokens_mean"], native["tree_input_tokens_mean"])
        originals = {c["id"]: c for case in data["cases"] for c in case["conditions"]}
        for row in overview["rows"]:
            self.assertEqual(row["quality_mean"], originals[row["condition_id"]]["statistics"]["quality"]["mean"])
            self.assertEqual(row["valid_n"], originals[row["condition_id"]]["valid_completed"])
        self.assertIn("工程 CLI · Full Access", artifacts["compact.html"])
        self.assertNotIn("固定材料 · 禁工具", artifacts["question-overview.html"])
        exported = list(csv.DictReader(__import__('io').StringIO(artifacts["question-overview.csv"])))
        self.assertEqual({r["condition_id"] for r in exported}, set(originals))

    def test_source_change_splits_comparability(self):
        first = self.make_run("one")
        (self.source / "config.py").write_text("def choose(v, d): return d if v is None else v # source changed\n", encoding="utf-8")
        second = self.make_run("two", ("fast",))
        output = self.base / "comparison"
        compare([first, second], output)
        data = core.read(output / "comparison-data.json")
        self.assertEqual(len(data["cases"]), 2)
        self.assertFalse(data["paired_speed_observations"])

    def test_instruction_and_native_agent_configuration_part_of_identity(self):
        root = self.make_run("identity")
        protocol = core.check_inputs(root)
        fingerprint = {"binary_sha256": "binary", "global_instructions_sha256": "global",
                       "execution": {"profile_sha256": "physical-one", "launch_overrides_sha256": "volatile-one",
                                     "agent_settings": {"max_threads": 4}, "agent_roles_sha256": {}}}
        before = execution_identity(protocol, fingerprint)
        fingerprint["execution"]["profile_sha256"] = "physical-two"
        fingerprint["execution"]["launch_overrides_sha256"] = "volatile-two"
        self.assertEqual(before, execution_identity(protocol, fingerprint))
        fingerprint["execution"]["agent_settings"]["max_threads"] = 6
        self.assertNotEqual(before, execution_identity(protocol, fingerprint))
        changed = dict(protocol, instructions_sha256="different-instructions")
        self.assertNotEqual(execution_identity(protocol), execution_identity(changed))

    def test_non_tiered_native_protocol_keeps_legacy_cost_alias(self):
        root = self.make_run("non-tiered", tiers=None)
        validate_records(root)
        data = summarize(root)
        self.assertGreater(data["rows"][0]["estimated_standard_api_usd"], 0)
        output = self.base / "comparison"
        compare([root], output)
        self.assertGreater(core.read(output / "comparison-data.json")["samples"][0]["estimated_api_usd"], 0)

    def test_optional_native_runtime_provenance_splits_identity(self):
        root = self.make_run("runtime-provenance")
        protocol = core.check_inputs(root)
        fingerprint = {"binary_sha256": "binary", "execution": {"agent_settings": {"max_threads": 4}}}
        legacy_identity = execution_identity(protocol, fingerprint)
        fields = {"instruction_sources_sha256": "1" * 64, "inherited_config_sha256": "2" * 64,
                  "model_catalog_sha256": "3" * 64, "binary_companions_sha256": {"codex-exec.exe": "4" * 64}}
        for name, original in fields.items():
            with self.subTest(field=name):
                copied = json.loads(json.dumps(fingerprint))
                copied["execution"][name] = original
                known_identity = execution_identity(protocol, copied)
                self.assertNotEqual(legacy_identity, known_identity)
                copied["execution"][name] = {"codex-exec.exe": "5" * 64} if isinstance(original, dict) else "6" * 64
                self.assertNotEqual(known_identity, execution_identity(protocol, copied))
        self.assertEqual(legacy_identity, execution_identity(protocol, fingerprint))

    def test_zero_delegation_tools_are_valid_and_exports_keep_timings(self):
        root = self.make_run("zero")
        records = validate_records(root)
        self.assertTrue(records[0]["valid"])
        summary = summarize(root)
        cell = summary["conditions"][0]
        self.assertEqual(cell["delegation_rate"], 0)
        self.assertEqual(cell["statistics"]["child_estimated_api_usd"]["mean"], 0)
        self.assertEqual(cell["statistics"]["answer_ready_s"]["median"], 5.5)
        self.assertEqual(cell["statistics"]["workflow_elapsed_s"]["median"], 9)
        self.assertEqual(cell["statistics"]["first_progress_s"]["median"], 1.1)
        self.assertEqual(cell["statistics"]["final_ttft_s"]["median"], 5.1)
        self.assertIsNone(cell["statistics"]["visible_decode_tps"]["mean"])
        with (root / "metrics.csv").open(encoding="utf-8-sig", newline="") as handle:
            row = next(csv.DictReader(handle))
        self.assertEqual(row["delegation_count"], "0")
        self.assertEqual(row["accounting_complete"], "True")

    def test_unknown_child_price_retains_observed_cost_without_faking_total(self):
        root = self.make_run("unknown", unknown_child=True)
        validate_records(root)
        data = summarize(root)
        row = data["rows"][0]
        self.assertIsNone(row["tree_estimated_api_usd"])
        self.assertIsNone(row["estimated_api_usd"])
        self.assertIsNone(row["child_observed_api_usd"])
        self.assertGreater(row["tree_observed_api_usd"], 0)
        self.assertEqual(data["cost_coverage_attempts"], 0)
        self.assertEqual(data["observed_cost_coverage_attempts"], 1)

    def test_mixed_child_model_and_standard_tier_use_own_rates(self):
        root = self.make_run("mixed", ("fast",), child_model=core.MODELS[1])
        validate_records(root)
        row = summarize(root)["rows"][0]
        prices = core.read(self.tier_prices)
        expected_root = core.metrics(usage(), None, rates=core.price_for(prices, core.MODELS[0], "priority"))["estimated_standard_api_usd"]
        expected_child = core.metrics(usage(50, 10), None, rates=core.price_for(prices, core.MODELS[1], "default"))["estimated_standard_api_usd"]
        self.assertAlmostEqual(row["root_estimated_api_usd"], expected_root)
        self.assertAlmostEqual(row["child_estimated_api_usd"], expected_child)
        self.assertAlmostEqual(row["tree_estimated_api_usd"], expected_root + expected_child)
        self.assertEqual(row["root_output_tokens"], 20)
        self.assertEqual(row["child_output_tokens"], 10)
        self.assertEqual(row["tree_output_tokens"], 30)

    def test_partial_accounting_keeps_answer_but_cost_is_unknown(self):
        root = self.make_run("partial", child_model=core.MODELS[1], missing_child_usage=True)
        validate_records(root)
        data = summarize(root)
        row = data["rows"][0]
        self.assertEqual(data["valid_completed"], 1)
        self.assertFalse(row["accounting_complete"])
        self.assertIsNone(row["tree_output_tokens"])
        self.assertIsNone(row["tree_estimated_api_usd"])
        self.assertEqual(row["root_observed_output_tokens"], 20)
        self.assertGreater(row["tree_observed_api_usd"], 0)

    def test_failed_attempt_cost_is_preserved_outside_quality_statistics(self):
        root = self.make_run("failed", repetitions=3, failed_last=True)
        validate_records(root)
        data = summarize(root)
        self.assertEqual(data["valid_completed"], 2)
        fee = data["rows"][0]["tree_estimated_api_usd"]
        self.assertAlmostEqual(data["known_cost_usd"], fee * 3)
        self.assertAlmostEqual(data["known_observed_cost_usd"], fee * 3)
        self.assertEqual(data["conditions"][0]["statistics"]["quality"]["n"], 2)
        output = self.base / "comparison"
        compare([root], output)
        compared = core.read(output / "comparison-data.json")
        self.assertGreater(compared["failures"][0]["estimated_api_usd"], 0)
        self.assertEqual(compared["cases"][0]["conditions"][0]["observed_cost_coverage_attempts"], 3)

    def test_metric_and_event_tampering_rejected(self):
        root = self.make_run("tamper")
        record_path = next(root.glob("runs/*/attempt-*/result.json"))
        record = core.read(record_path)
        record["tree_output_tokens"] += 1
        core.write(record_path, record)
        with self.assertRaisesRegex(ValueError, "Task-tree metric inconsistent"):
            validate_records(root)
        record["tree_output_tokens"] -= 1
        core.write(record_path, record)
        events = record_path.with_name("events.jsonl")
        events.write_text(events.read_text(encoding="utf-8").replace("检查源码", "被改写"), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Task-tree events differ"):
            validate_records(root)

    def test_protocol_issues_cannot_be_hidden_by_valid_flag(self):
        root = self.make_run("protocol-issues")
        record_path = next(root.glob("runs/*/attempt-*/result.json"))
        record = core.read(record_path)
        record["protocol_issues"] = ["Unexpected interactive request"]
        core.write(record_path, record)
        with self.assertRaisesRegex(ValueError, "Invalid final answer/protocol"):
            validate_records(root)

    def test_portable_archive_keeps_original_source_bytes_and_ledger(self):
        (self.source / "旧源码.cpp").write_bytes("// 中文源码\r\n".encode("gb2312"))
        root = self.make_run("portable")
        archive(root)
        before = comparison_fixtures.snapshot(root)
        target = self.base / "comparison"
        compare([root], target)
        self.assertEqual(before, comparison_fixtures.snapshot(root))
        with zipfile.ZipFile(target / "comparison.zip") as bundle:
            source_entry = next(n for n in bundle.namelist() if n.endswith("source-snapshot/project/src/旧源码.cpp"))
            self.assertEqual(bundle.read(source_entry), (self.source / "旧源码.cpp").read_bytes())
            self.assertTrue(any(n.endswith("task-tree.json") for n in bundle.namelist()))
            for name in bundle.namelist():
                self.assertEqual(bundle.read(name), (target / name).read_bytes())
        node = shutil.which("node")
        if node:
            checked = subprocess.run([node, str(Path(__file__).resolve().parents[1] / "check_comparison.cjs"), str(target)],
                                     capture_output=True, encoding="utf-8", timeout=30)
            self.assertEqual(checked.returncode, 0, checked.stderr)

    def test_capability_and_scratch_are_archived_outside_formal_costs(self):
        from agentic_runtime import capability_identity
        root = self.make_run("capability", child_model=core.MODELS[1])
        attempt = next(root.glob("runs/*/attempt-*"))
        scratch = attempt / "scratch"
        scratch.mkdir()
        probe_bytes = "// 核验探针\r\n".encode("gb2312")
        (scratch / "probe.cpp").write_bytes(probe_bytes)
        capability = root / "capability/runs/0001/attempt-001"
        shutil.copytree(attempt, capability)
        fingerprint = {"binary_sha256": "a" * 64, "version": "test", "provider": "test", "endpoint_host": None,
                       "global_instructions_sha256": None, "execution": {"agent_settings": {}, "agent_roles_sha256": {},
                       "feature_overrides": {}, "models": [], "catalog_client_version": None, "native_defaults": "native"}}
        record = core.read(capability / "result.json")
        record.update(purpose="capability", fingerprint=fingerprint)
        core.write(capability / "result.json", record)
        files = {p.relative_to(root / "capability").as_posix(): core.sha(p)
                 for p in capability.rglob("*") if p.is_file()}
        compatibility = capability_identity(core.check_inputs(root), fingerprint)
        identity = core.digest({"schema": 1, "compatibility_id": compatibility,
                                "origin_fingerprint_sha256": core.digest(fingerprint), "files_sha256": files})
        receipt = {"passed": True, "paid_here": True, "attempts": 1, "verified_attempts": 1,
                   "capability_id": identity, "compatibility_id": compatibility, "fingerprint": fingerprint,
                   "origin_fingerprint_sha256": core.digest(fingerprint), "files_sha256": files, "formal_samples": 0}
        core.write(root / "capability/seal.json", receipt)
        data = summarize(root)
        fee = data["rows"][0]["tree_estimated_api_usd"]
        self.assertAlmostEqual(data["known_cost_usd"], fee)
        self.assertAlmostEqual(data["capability_verification"]["known_complete_cost_usd"], fee)
        core.write(root / "summary.json", data)
        archive(root)
        with zipfile.ZipFile(root / "benchmark-results.zip") as bundle:
            self.assertEqual(bundle.read("capability/runs/0001/attempt-001/scratch/probe.cpp"), probe_bytes)
            self.assertEqual(bundle.read(scratch.relative_to(root).as_posix() + "/probe.cpp"), probe_bytes)
        output = self.base / "capability-comparison"
        compare([root], output)
        compared = core.read(output / "comparison-data.json")
        self.assertEqual(compared["source_batches"][0]["capability_verification"]["attempts"], 1)
        with zipfile.ZipFile(output / "comparison.zip") as bundle:
            self.assertTrue(any(name.endswith("capability/seal.json") for name in bundle.namelist()))
            self.assertTrue(any(name.endswith("capability/runs/0001/attempt-001/scratch/probe.cpp") for name in bundle.namelist()))
        # Offline V3 review must preserve all bytes named by this native seal,
        # including the original-encoding scratch probe, without new sampling.
        from evaluation import regrade
        from reporting import blind, build_report, seal, validate
        from test_scoring_v3 import sample_score, v3_gold
        rubric = self.base / "capability-v3.json"
        core.write(rubric, v3_gold(core.read(root / "input/gold.json")))
        revision = self.base / "capability-revision"
        # Historical archive fixture; current entry points reject this retired scale.
        with patch("evaluation.require_current_gold"):
            regrade(root, rubric, revision)
        blind(revision)
        scores = {anon: sample_score() for anon in core.read(revision / "grading/private-mapping.json")}
        score_path = self.base / "capability-v3-scores.json"
        core.write(score_path, scores)
        seal(revision, score_path)
        reviewed = build_report(revision)
        validate(revision)
        self.assertEqual((revision / "capability/runs/0001/attempt-001/scratch/probe.cpp").read_bytes(), probe_bytes)
        self.assertEqual(reviewed["capability_verification"], data["capability_verification"])
        receipt.update(paid_here=False, attempts=0, shared_attempts=1,
                       shared_from={"capability_id": identity, "compatibility_id": compatibility,
                                    "seal_sha256": "d" * 64, "root": "original-verification-source"})
        core.write(root / "capability/seal.json", receipt)
        shared = summarize(root)["capability_verification"]
        self.assertEqual(shared["attempts"], 0)
        self.assertEqual(shared["archived_attempts"], 1)
        self.assertEqual(shared["local_known_complete_cost_usd"], 0)
        self.assertAlmostEqual(shared["known_complete_cost_usd"], fee)
        (capability / "scratch/probe.cpp").write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "Capability smoke artifacts changed"):
            archive(root)

    def test_utf16_credentials_cannot_be_exported(self):
        credential = self.base / "credential.bin"
        credential.write_bytes(("api_key=" + "A" * 30).encode("utf-16-le"))
        with self.assertRaises(ValueError):
            check_artifact_sensitive(credential, binary_source=True)

    def test_recovery_evidence_and_paid_origins_are_counted_once(self):
        root = self.make_run("recovery-costs", child_model=core.MODELS[1])
        source_attempt = next(root.glob("runs/*/attempt-*"))
        fee = core.read(source_attempt / "result.json")["tree_estimated_api_usd"]
        capability = root / "capability"
        original = capability / "recovery/source"
        original.mkdir(parents=True)
        for name in ("answer.txt", "result.json", "events.jsonl", "task-tree.json"):
            shutil.copyfile(source_attempt / name, original / name)
        for name in ("protocol.json", "prices.json", "preparation-seal.json"):
            shutil.copyfile(root / name, original / name)
        original_bytes = {p.name: p.read_bytes() for p in original.iterdir()}
        origins = {}
        for index in range(1, 5):
            directory = capability / f"runs/{index:04d}/attempt-001"
            shutil.copytree(source_attempt, directory)
            record = core.read(directory / "result.json")
            record["purpose"] = "capability"
            name = directory.relative_to(capability).as_posix() + "/result.json"
            origins[name] = {"kind": "historical-paid-offline-replay" if index == 1 else "new-paid"}
            if index == 1:
                record["replay_provenance"] = {"kind": "historical-paid-offline-replay", "inference_calls": 0,
                                               "receipt": "recovery/receipt.json"}
            elif index == 4:
                record["tree_estimated_api_usd"] = None
            core.write(directory / "result.json", record)
        core.write(capability / "recovery/receipt.json", {"replay_inference_calls": 0, "historical_paid_attempts": 1})
        core.write(capability / "seal.json", {"schema_version": 2, "passed": False, "paid_here": True,
              "attempts": 3, "verified_attempts": 4, "replay_checks": 1, "attempt_origins": origins})
        (capability / "recovery/unrelated.txt").write_text("not an export", encoding="utf-8")
        summary = capability_summary(root)
        self.assertEqual((summary["attempts"], summary["paid_ledger_attempts"], summary["archived_attempts"]), (3, 4, 4))
        self.assertEqual((summary["historical_paid_attempts"], summary["origin_new_paid_attempts"], summary["new_paid_attempts"]), (1, 3, 3))
        self.assertEqual((summary["replay_checks"], summary["replay_inference_calls"], summary["replay_added_api_usd"]), (1, 0, 0))
        self.assertEqual(summary["complete_cost_attempts"], 3)
        self.assertAlmostEqual(summary["known_complete_cost_usd"], 3 * fee)
        self.assertAlmostEqual(summary["historical_known_complete_cost_usd"], fee)
        self.assertAlmostEqual(summary["local_known_complete_cost_usd"], 2 * fee)
        self.assertAlmostEqual(summary["known_observed_cost_usd"], 4 * fee)
        self.assertIsNone(summary["actual_gateway_charge"])
        page = agentic_html(summarize(root))
        self.assertIn("零调用离线复核 1 次", page)
        self.assertIn("本批新增付费验收 3 次", page)
        self.assertIn("实际网关扣费未知", page)
        archive(root)
        with zipfile.ZipFile(root / "benchmark-results.zip") as bundle:
            self.assertIn("capability/recovery/receipt.json", bundle.namelist())
            self.assertNotIn("capability/recovery/unrelated.txt", bundle.namelist())
            for name, expected in original_bytes.items():
                self.assertEqual(bundle.read("capability/recovery/source/" + name), expected)
            for name in bundle.namelist():
                self.assertEqual(bundle.read(name), (root / name).read_bytes())

        seal = core.read(capability / "seal.json")
        seal.update(paid_here=False, attempts=0)
        core.write(capability / "seal.json", seal)
        adopted = capability_summary(root)
        self.assertEqual(adopted["new_paid_attempts"], 0)
        self.assertEqual(adopted["origin_new_paid_attempts"], 3)
        self.assertEqual(adopted["local_known_complete_cost_usd"], 0)
        self.assertAlmostEqual(adopted["known_complete_cost_usd"], 3 * fee)

    def test_unsealed_recovery_is_zero_local_attempts_and_preserves_unknown_cost(self):
        root = self.make_run("recovery-unknown", child_model=core.MODELS[1])
        directory = root / "capability/runs/0001/attempt-001"
        shutil.copytree(next(root.glob("runs/*/attempt-*")), directory)
        record = core.read(directory / "result.json")
        record.update(tree_estimated_api_usd=None, tree_observed_api_usd=None,
                      replay_provenance={"kind": "historical-paid-offline-replay", "inference_calls": 0})
        core.write(directory / "result.json", record)
        core.write(root / "capability/recovery/receipt.json", {"replay_inference_calls": 0})
        summary = capability_summary(root)
        self.assertEqual((summary["attempts"], summary["new_paid_attempts"], summary["historical_paid_attempts"]), (0, 0, 1))
        self.assertEqual(summary["local_known_complete_cost_usd"], 0)
        self.assertIsNone(summary["known_complete_cost_usd"])
        self.assertIsNone(summary["historical_known_complete_cost_usd"])

    def test_recovery_evidence_hardlinks_cannot_be_exported(self):
        root = self.make_run("recovery-hardlink")
        source = root / "capability/recovery/source"
        source.mkdir(parents=True)
        os.link(self.source / "config.py", source / "answer.txt")
        with self.assertRaisesRegex(ValueError, "hardlink alias"):
            native_evidence_files(root)

    def test_scratch_links_cannot_be_exported(self):
        root = self.make_run("linked")
        attempt = next(root.glob("runs/*/attempt-*"))
        try:
            (attempt / "scratch").symlink_to(self.source, target_is_directory=True)
        except OSError as exc:
            self.skipTest("Local account cannot create directory links: " + str(exc))
        with self.assertRaisesRegex(ValueError, "link|escapes run"):
            native_evidence_files(root)

    def test_scratch_hardlinks_cannot_be_exported(self):
        root = self.make_run("hardlinked")
        scratch = next(root.glob("runs/*/attempt-*")) / "scratch"
        scratch.mkdir()
        os.link(self.source / "config.py", scratch / "external-alias.py")
        with self.assertRaisesRegex(ValueError, "hardlink alias"):
            native_evidence_files(root)


if __name__ == "__main__":
    unittest.main()
