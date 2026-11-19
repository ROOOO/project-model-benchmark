"""Offline protocol/transport regression tests; never invokes a real model."""
import collections
import csv
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import core
from benchmark import main, parser, retest
from core import build_schedule, check_inputs, home, metrics, prepare, read, select_project, sha, write
from reporting import archive, blind, build_report, markdown, seal, summarize, validate
import reporting
from runtime import RunLock, Server, execute, process_identity, recover_inflight


RATES = {"input_usd": 2, "cached_usd": .2, "write_usd": 2.5, "output_usd": 10, "input_credits": 50, "cached_credits": 5, "output_credits": 250}


def fixture(base):
    case = base / "case"
    (case / "evidence").mkdir(parents=True)
    source = case / "evidence/S1.txt"
    source.write_text("def choose(value, default):\n    return default if value is None else value\n", encoding="utf-8")
    write(case / "case.json", {"title": "配置默认值与显式空值的区别", "project": {"name": "synthetic", "root": str(base / "project")},
        "candidates": [{"title": x, "why_common": "fixture evidence", "verifiable": "pure function assertions", "selected": i == 0} for i, x in enumerate(["配置默认值与显式空值的区别", "缓存刷新", "错误返回"])],
        "sources": [{"id": "S1", "file": "evidence/S1.txt", "logical_path": "src/config.py", "ranges": "L1-L2", "source_sha256": sha(source)}]})
    (case / "question.txt").write_text("请解释 None 与空字符串的处理，给出验证建议。", encoding="utf-8")
    write(case / "gold.json", {"reference_facts": ["None uses default; empty string preserved"], "verification": [{"method": "pure function assertions", "result": "passed"}],
        "rubric": [{"id": "A1", "dimension": "accuracy", "max": 70, "criterion": "准确区分 None 与显式空字符串"}, {"id": "R1", "dimension": "readability", "max": 30, "criterion": "清晰且可行动"}]})
    rates = base / "prices.json"
    write(rates, {"as_of": "2026-10-15", "sources": ["https://developers.openai.com/api/docs/pricing"], "models": {m: RATES for m in core.MODELS}})
    return case, rates


def write_attempt(root, slot, attempt=1, valid=True, usage=True):
    directory = root / "runs" / slot["id"] / f"attempt-{attempt:03d}"
    directory.mkdir(parents=True)
    answer = "None 使用默认值；空字符串保持原值。建议分别断言这两种输入。" if valid else ""
    (directory / "answer.txt").write_text(answer, encoding="utf-8")
    u = {"total": {"inputTokens": 1000, "cachedInputTokens": 100, "cacheWriteInputTokens": 0, "outputTokens": 100, "reasoningOutputTokens": 60, "totalTokens": 1100}} if usage else None
    events = [{"method": "item/agentMessage/delta", "t_s": 1, "delta": answer[:4]}, {"method": "item/agentMessage/delta", "t_s": 3, "delta": answer[4:]}, {"method": "turn/completed", "t_s": 4, "status": "completed" if valid else "failed"}]
    (directory / "events.jsonl").write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in events), encoding="utf-8")
    rates = read(root / "prices.json").get("models", {}).get(slot["model"])
    value = {"slot_id": slot["id"], "attempt": attempt, "model": slot["model"], "effort": slot["effort"], "repetition": slot["repetition"], "valid": valid, "status": "completed" if valid else "failed",
        "thread_id": "fixture-"+root.name+slot["id"]+str(attempt), "effective": {"model": slot["model"], "reasoningEffort": slot["effort"], "serviceTier": "default", "modelProvider": "test"}, "tool_items": [], "warnings": [],
        "prompt_sha256": sha(root / "input/prompt.txt"), "answer_sha256": sha(directory / "answer.txt"), "usage": u, "first_delta_s": 1, "last_delta_s": 3,
        **metrics(u, 4, 1, 3, answer, rates)}
    write(directory / "result.json", value)
    return value


class FakeServer:
    failures = set()
    calls = []

    def __init__(self, root, binary=None):
        self.root, self.active = Path(root), False
        self.proc = SimpleNamespace(poll=lambda: None)

    def preflight(self, p):
        return {"fingerprint": {"test": True}}

    def run_trial(self, slot, attempt):
        self.calls.append((slot["id"], attempt))
        return write_attempt(self.root, slot, attempt, not (slot["id"] in self.failures and attempt == 1))

    def close(self):
        pass


class BenchmarkTests(unittest.TestCase):
    def setUp(self):
        parent = home() / "benchmark-validation"
        parent.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="unit-", dir=parent)
        self.base = Path(self.temp.name).resolve()
        self.assertTrue(self.base.is_relative_to(parent.resolve()))
        self.case, self.prices = fixture(self.base)
        self.root = prepare(self.case, self.base / "run", ["gpt-6-sol"], ["xhigh", "max"], 2, self.prices)
        FakeServer.failures, FakeServer.calls = set(), []

    def tearDown(self):
        self.temp.cleanup()

    def complete(self):
        execute(self.root, server_factory=FakeServer)
        blind(self.root)
        path = self.completed_scores()
        seal(self.root, path)
        build_report(self.root)
        validate(self.root, write_receipt=True)

    def completed_scores(self):
        scores = read(self.root / "grading/packet/scores-template.json")
        for item in scores.values():
            item["points"] = {"A1": 70, "R1": 29}
            item["notes"] = {"A1": "两个边界均正确", "R1": "简洁清晰，验证建议尚可更具体"}
        path = self.base / "scores.json"
        write(path, scores)
        return path

    def transport_server(self, root=None):
        server = Server.__new__(Server)
        server.root, server.provider = root or self.root, "test"
        server.fingerprint = {"global_instructions_sha256": sha(home() / "AGENTS.md") if (home() / "AGENTS.md").exists() else None}
        server.proc = SimpleNamespace(pid=os.getpid(), poll=lambda: None, stdin=SimpleNamespace(close=Mock()), wait=Mock(), terminate=Mock())
        server.counts, server.pending, server.active, server.seq = collections.Counter(), [], False, 0
        server.send = Mock()
        return server

    def thread_reply(self, server, slot):
        return time.perf_counter(), {"id": server.seq, "result": {"model": slot["model"], "modelProvider": "test", "reasoningEffort": slot["effort"], "serviceTier": "default", "thread": {"id": "transport-fixture"}}}

    def change_csv(self, change):
        path = self.root / "metrics.csv"
        with path.open(encoding="utf-8-sig", newline="") as f:
            rows = list(csv.reader(f))
        change(rows)
        with path.open("w", encoding="utf-8-sig", newline="") as f:
            csv.writer(f).writerows(rows)

    def test_default_matrix_balanced_and_reproducible(self):
        s = build_schedule(core.MODELS, core.EFFORTS, 5, 17)
        self.assertEqual(len(s), 45)
        self.assertEqual(s, build_schedule(core.MODELS, core.EFFORTS, 5, 17))
        for repetition in range(1, 6):
            self.assertEqual(len({(x["model"], x["effort"]) for x in s if x["repetition"] == repetition}), 9)
        self.assertNotEqual(s, build_schedule(core.MODELS, core.EFFORTS, 5, 18))

    def test_missing_usage_and_single_chunk_are_unknown(self):
        m = metrics(None, 2, 1, 1, "回答", RATES)
        self.assertIsNone(m["visible_decode_tps"])
        self.assertIsNone(m["output_tokens"])
        self.assertIsNone(m["estimated_standard_api_usd"])
        m = metrics({"inputTokens": 100, "outputTokens": 10}, 2, 1, 2, "x", RATES)
        self.assertIsNone(m["reasoning_tokens"])
        self.assertIsNone(m["visible_tokens"])
        self.assertIsNone(m["cache_write_input_tokens"])
        self.assertIsNotNone(m["estimated_cold_input_api_usd"])

    def test_contradictory_usage_rejected_but_missing_is_not_zero(self):
        self.assertTrue(core.usage_issues({"total": {"inputTokens": 2, "outputTokens": 5, "totalTokens": 9}}))
        self.assertTrue(core.usage_issues({"outputTokens": 4, "reasoningOutputTokens": 5}))
        self.assertTrue(core.usage_issues({"inputTokens": -1}))
        self.assertFalse(core.usage_issues(None))
        self.assertFalse(core.usage_issues({"total": {"outputTokens": 4}}))

    def test_null_total_usage_is_unavailable(self):
        m = metrics({"total": None}, 10, 2, 4, "答案", RATES)
        self.assertIsNone(m["input_tokens"])
        self.assertIsNone(m["estimated_standard_api_usd"])
        self.assertFalse(core.usage_issues({"total": None}))

    def test_nested_fences_preserve_code_and_escape_html(self):
        text = '````markdown\n```js\n<script>never_execute()</script>\n```\n````'
        rendered = markdown(text)
        self.assertEqual(rendered.count('<pre>'), 1)
        self.assertIn('```js', rendered)
        self.assertNotIn('<script>', rendered)
        self.assertIn('&lt;script&gt;', rendered)

    def test_cost_and_visible_timing(self):
        u = {"inputTokens": 1000, "cachedInputTokens": 100, "cacheWriteInputTokens": 50, "outputTokens": 100, "reasoningOutputTokens": 60}
        m = metrics(u, 10, 5, 7, "中文", RATES)
        self.assertEqual(m["visible_decode_tps"], 20)
        self.assertAlmostEqual(m["estimated_standard_api_usd"], (850*2+100*.2+50*2.5+100*10)/1e6)
        self.assertEqual(m["chinese_characters"], 2)

    def test_frozen_input_and_protocol_changes_rejected(self):
        (self.root / "input/question.txt").write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Frozen input"):
            check_inputs(self.root)
        p = read(self.root / "protocol.json")
        p["repetitions"] = 20
        write(self.root / "protocol.json", p)
        with self.assertRaisesRegex(ValueError, "protocol"):
            check_inputs(self.root)

    def test_bundle_escape_and_secret_rejected_before_calls(self):
        data = read(self.case / "case.json")
        data["sources"][0]["file"] = "../outside.txt"
        write(self.case / "case.json", data)
        with self.assertRaisesRegex(ValueError, "inside"):
            prepare(self.case, self.base / "escape")
        with self.assertRaisesRegex(ValueError, "authentication"):
            core.check_sensitive("Bearer " + "z"*32)

    def test_discovery_current_precedence_and_projectless_fallback(self):
        a, b = self.base / "a", self.base / "b"
        for p in [a, b]:
            p.mkdir()
            (p / "Cargo.toml").write_text("[package]", encoding="utf-8")
        inventory = {"projects": [{"projectId": "a", "path": str(a)}, {"projectId": "b", "path": str(b)}, {"projectId": "home", "path": str(Path.home())}, {"projectId": "remote", "path": str(b), "projectKind": "remote"}]}
        threads = {"threads": [{"id": "1", "projectId": "a", "updatedAt": 1000}, {"id": "2", "projectId": "a", "updatedAt": 2000}, {"id": "3", "projectId": "b", "updatedAt": 9999999999999}]}
        self.assertEqual(select_project(current=b, inventory=inventory, threads=threads)["path"], str(b))
        self.assertEqual(select_project(inventory=inventory, threads=threads)["path"], str(a))
        self.assertEqual(select_project(explicit=b, current=a)["path"], str(b))
        with self.assertRaisesRegex(ValueError, "no fallback"):
            select_project(explicit=self.base / "missing", inventory=inventory)

    def test_successful_resume_does_not_repeat(self):
        execute(self.root, server_factory=FakeServer)
        self.assertEqual(len(FakeServer.calls), 4)
        execute(self.root, resume=True, server_factory=FakeServer)
        self.assertEqual(len(FakeServer.calls), 4)

    def test_failure_retained_and_only_explicit_retry(self):
        FakeServer.failures = {"0001"}
        execute(self.root, server_factory=FakeServer)
        execute(self.root, resume=True, server_factory=FakeServer)
        self.assertEqual(len(FakeServer.calls), 4)
        execute(self.root, resume=True, retry_failed=True, server_factory=FakeServer)
        self.assertEqual(len(FakeServer.calls), 5)
        s = summarize(self.root, partial=True)
        self.assertEqual(s["attempt_count"], 5)
        self.assertEqual(s["valid_completed"], 4)
        self.assertTrue((self.root / "runs/0001/attempt-001/result.json").exists())

    def test_live_lock_prevents_duplicate_runner(self):
        with RunLock(self.root):
            with self.assertRaisesRegex(ValueError, "active"):
                with RunLock(self.root):
                    self.fail("duplicate acquired")

    def test_stale_inflight_preserved_as_interrupted(self):
        d = self.root / "runs/0001/attempt-001"
        d.mkdir(parents=True)
        write(self.root / "inflight.json", {"slot_id": "0001", "attempt": 1, "relative_dir": "runs/0001/attempt-001", "pid": 0, "server_pid": 0})
        recover_inflight(self.root)
        self.assertEqual(read(d / "result.json")["status"], "interrupted")
        self.assertFalse((self.root / "inflight.json").exists())
        validate(self.root, partial=True)

    def test_live_inflight_is_not_restarted(self):
        write(self.root / "inflight.json", {"pid": os.getpid(), "identity": process_identity(os.getpid())})
        with self.assertRaisesRegex(ValueError, "active"):
            recover_inflight(self.root)

    def test_null_or_missing_rationale_never_gets_default_score(self):
        execute(self.root, server_factory=FakeServer)
        blind(self.root)
        path = self.root / "grading/packet/scores-template.json"
        with self.assertRaisesRegex(ValueError, "Invalid/missing"):
            seal(self.root, path)
        scores = read(path)
        for item in scores.values():
            item["points"] = {"A1": 70, "R1": 30}
        write(path, scores)
        with self.assertRaisesRegex(ValueError, "rationale"):
            seal(self.root, path)

    def test_report_requires_sealed_scores(self):
        execute(self.root, server_factory=FakeServer)
        with self.assertRaisesRegex(ValueError, "not sealed"):
            build_report(self.root)
        build_report(self.root, partial=True)
        validate(self.root, partial=True)
        self.assertIn("评分未封存", (self.root / "report.html").read_text(encoding="utf-8"))

    def test_validate_rejects_changed_csv_metric(self):
        self.complete()
        self.change_csv(lambda rows: rows[1].__setitem__(rows[0].index("elapsed_s"), "999999"))
        with self.assertRaisesRegex(ValueError, "CSV differs"):
            validate(self.root)

    def artifact_snapshot(self):
        return {p.relative_to(self.root).as_posix(): (sha(p), p.stat().st_mtime_ns)
                for p in self.root.rglob("*") if p.is_file()}

    def test_repeated_cli_validation_preserves_delivered_files_and_archive(self):
        self.complete()
        archive(self.root)
        before = self.artifact_snapshot()
        with patch("sys.stdout", new_callable=io.StringIO), patch("benchmark.Server", side_effect=AssertionError("inference started")):
            self.assertEqual(main(["validate", "--run-dir", str(self.root)]), 0)
            self.assertEqual(main(["validate", "--run-dir", str(self.root)]), 0)
        self.assertEqual(self.artifact_snapshot(), before)
        with patch("reporting.shutil.which", return_value=None):
            self.assertTrue(validate(self.root)["passed"])
        self.assertEqual(self.artifact_snapshot(), before)

    def test_validation_does_not_create_missing_receipts(self):
        self.complete()
        for name in ("validation.json", "ui-check.json"):
            (self.root / name).unlink()
        before = self.artifact_snapshot()
        self.assertTrue(validate(self.root)["passed"])
        self.assertEqual(self.artifact_snapshot(), before)
        with patch("reporting.shutil.which", return_value=None):
            self.assertTrue(validate(self.root)["passed"])
        self.assertEqual(self.artifact_snapshot(), before)

    def test_report_refreshes_receipts_before_packaging(self):
        self.complete()
        from zipfile import ZipFile
        with patch("sys.stdout", new_callable=io.StringIO):
            main(["report", "--run-dir", str(self.root)])
        original = (self.root / "report.html").read_bytes()
        write(self.root / "assessment.json", {"conclusion": "Updated review", "evidence": ["Fixture results"], "limitations": ["Synthetic task"]})
        with patch("sys.stdout", new_callable=io.StringIO), patch("reporting.shutil.which", return_value=None):
            main(["report", "--run-dir", str(self.root)])
        self.assertNotEqual(original, (self.root / "report.html").read_bytes())
        self.assertIsNone(read(self.root / "ui-check.json")["passed"])
        with ZipFile(self.root / "benchmark-results.zip") as package:
            for name in ("report.html", "assessment.json", "validation.json", "ui-check.json"):
                self.assertEqual(package.read(name), (self.root / name).read_bytes())

    def test_validate_rejects_duplicate_csv_rows(self):
        self.complete()
        self.change_csv(lambda rows: rows.__setitem__(2, list(rows[1])))
        with self.assertRaisesRegex(ValueError, "CSV differs"):
            validate(self.root)

    def test_validate_rejects_changed_csv_header(self):
        self.complete()
        self.change_csv(lambda rows: rows[0].__setitem__(rows[0].index("elapsed_s"), "renamed_elapsed"))
        with self.assertRaisesRegex(ValueError, "CSV differs"):
            validate(self.root)

    def test_validate_checks_csv_even_without_summary(self):
        self.complete()
        (self.root / "summary.json").unlink()
        self.change_csv(lambda rows: rows[1].__setitem__(rows[0].index("elapsed_s"), "999999"))
        with self.assertRaises(ValueError):
            validate(self.root)

    def test_validate_checks_every_summary_field_except_generation_time(self):
        self.complete()
        path = self.root / "summary.json"
        original = read(path)
        for name, value in original.items():
            if name == "generated_at":
                continue
            with self.subTest(field=name):
                changed = dict(original)
                if isinstance(value, bool):
                    changed[name] = not value
                elif isinstance(value, (int, float)):
                    changed[name] = value + 1
                elif isinstance(value, str):
                    changed[name] = value + " changed"
                elif isinstance(value, list):
                    changed[name] = value + [{"unexpected": True}]
                elif isinstance(value, dict):
                    changed[name] = {**value, "unexpected": True}
                else:
                    changed[name] = "unexpected"
                write(path, changed)
                with self.assertRaisesRegex(ValueError, "Summary differs"):
                    validate(self.root)
        write(path, {**original, "generated_at": "2026-01-01T00:00:00+00:00"})
        self.assertTrue(validate(self.root)["passed"])
        write(path, {**original, "unexpected": True})
        with self.assertRaisesRegex(ValueError, "Summary differs"):
            validate(self.root)

    def test_validate_rejects_html_numbers_labels_and_answers(self):
        self.complete()
        path = self.root / "report.html"
        original = path.read_text(encoding="utf-8")
        data_start = original.index('<script id="data" type="application/json">') + len('<script id="data" type="application/json">')
        data_end = original.index("</script>", data_start)
        payload = json.loads(original[data_start:data_end])
        subscription_total = read(self.root / "subscription-prices.json")["known_subscription_usd"]
        cases = {
            "table_metric": original.replace("<td>4.00</td>", "<td>999999.00</td>", 1),
            "total_cost": original.replace(f"估算费用合计 ${subscription_total:,.4f}", "估算费用合计 $999999", 1),
            "answer_label": original.replace('<details><summary>'+payload["answers"][0]["label"], '<details><summary>wrong-model', 1),
        }
        for key in ["cells", "answers"]:
            changed = json.loads(json.dumps(payload))
            if key == "cells":
                changed[key][0]["statistics"]["elapsed_s"]["median"] = 999999
            else:
                changed[key][0]["text"] = "different answer"
                changed[key][0]["html"] = "<p>different answer</p>"
            encoded = json.dumps(changed, ensure_ascii=False).replace("<", "\\u003c").replace("&", "\\u0026")
            cases[key] = original[:data_start] + encoded + original[data_end:]
        for name, changed in cases.items():
            with self.subTest(part=name):
                self.assertNotEqual(changed, original)
                path.write_text(changed, encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "HTML differs"):
                    validate(self.root)
                self.assertEqual(path.read_text(encoding="utf-8"), changed)

    def test_html_renderer_has_no_file_or_clock_dependency(self):
        self.complete()
        data, context = summarize(self.root), reporting.report_context(self.root)
        from billing import _pricing
        _pricing.cache_clear()
        with patch("pathlib.Path.open", side_effect=AssertionError("renderer opened a file")), patch("reporting.now", side_effect=AssertionError("renderer read clock")):
            actual = reporting.render_html(data, context)
            self.assertEqual(actual, reporting.render_html(data, context))
        self.assertEqual(actual, (self.root / "report.html").read_text(encoding="utf-8"))

    def test_changed_packet_inputs_rejected_before_seal_and_partial_report(self):
        execute(self.root, server_factory=FakeServer)
        blind(self.root)
        scores = self.completed_scores()
        for name in ["gold.json", "prompt.txt"]:
            with self.subTest(name=name):
                path = self.root / "grading/packet" / name
                before = path.read_bytes()
                if name == "gold.json":
                    gold = read(path)
                    gold["reference_facts"] = ["Changed after inference"]
                    gold["rubric"][0]["criterion"] = "Reward the opposite factual answer"
                    write(path, gold)
                else:
                    path.write_text("Changed question", encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "Scoring input"):
                    seal(self.root, scores)
                with self.assertRaisesRegex(ValueError, "Scoring input"):
                    build_report(self.root, partial=True)
                self.assertFalse((self.root / "grading/seal.json").exists())
                path.write_bytes(before)

    def test_missing_duplicate_and_extra_mapping_entries_rejected(self):
        execute(self.root, server_factory=FakeServer)
        blind(self.root)
        scores = self.completed_scores()
        path = self.root / "grading/private-mapping.json"
        original = read(path)
        first = next(iter(original))
        missing = dict(original)
        missing.pop(first)
        duplicate = dict(original)
        duplicate["Q999"] = original[first]
        extra = dict(original)
        extra["Q998"] = "runs/9999/attempt-001/result.json"
        for label, mapping in [("missing", missing), ("duplicate", duplicate), ("extra", extra)]:
            with self.subTest(label=label):
                write(path, mapping)
                with self.assertRaisesRegex(ValueError, "mapping"):
                    seal(self.root, scores)
                with self.assertRaisesRegex(ValueError, "mapping"):
                    validate(self.root, partial=True)
        write(path, original)

    def test_answer_replacement_and_extra_anonymous_files_rejected(self):
        execute(self.root, server_factory=FakeServer)
        blind(self.root)
        scores = self.completed_scores()
        anon, relative = next(iter(read(self.root / "grading/private-mapping.json").items()))
        packet_answer = self.root / "grading/packet/answers" / (anon + ".txt")
        raw = (self.root / relative).parent / "answer.txt"
        original = packet_answer.read_bytes()
        packet_answer.write_text("Replacement answer", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Anonymous answer"):
            seal(self.root, scores)
        raw.write_text("Replacement answer", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "recorded hash"):
            seal(self.root, scores)
        raw.write_bytes(original)
        packet_answer.write_bytes(original)
        extra = packet_answer.with_name("Q999.txt")
        extra.write_text("Stale answer", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Anonymous answer files"):
            seal(self.root, scores)

    def test_retry_after_blind_requires_refresh_and_scores_every_valid_answer(self):
        FakeServer.failures = {"0001"}
        execute(self.root, server_factory=FakeServer)
        blind(self.root)
        old_scores = self.completed_scores()
        execute(self.root, resume=True, retry_failed=True, server_factory=FakeServer)
        with self.assertRaisesRegex(ValueError, "mapping"):
            seal(self.root, old_scores)
        with self.assertRaisesRegex(ValueError, "mapping"):
            build_report(self.root, partial=True)
        with self.assertRaisesRegex(ValueError, "refresh"):
            blind(self.root)
        refreshed = blind(self.root, refresh=True)
        self.assertEqual(refreshed["answers"], 4)
        seal(self.root, self.completed_scores())
        data = build_report(self.root)
        self.assertEqual(data["valid_completed"], 4)
        self.assertEqual(sum(c["statistics"]["quality"]["n"] for c in data["conditions"]), 4)
        validate(self.root)

    def test_refresh_preserves_previous_packet_and_rejects_stale_scores(self):
        execute(self.root, server_factory=FakeServer)
        blind(self.root)
        old_scores = self.completed_scores()
        old_ids = set(read(old_scores))
        grading = self.root / "grading"
        (grading / "packet/working-notes.txt").write_text("Unfinished grading work", encoding="utf-8")
        before = {p.relative_to(grading).as_posix(): p.read_bytes() for p in grading.rglob("*") if p.is_file()}
        refreshed = blind(self.root, refresh=True)
        archived = Path(refreshed["archived_grading"])
        self.assertTrue(archived.is_relative_to(self.root / "grading-history"))
        self.assertEqual(before, {p.relative_to(archived).as_posix(): p.read_bytes() for p in archived.rglob("*") if p.is_file()})
        self.assertFalse(old_ids & set(read(self.root / "grading/private-mapping.json")))
        with self.assertRaisesRegex(ValueError, "exactly once"):
            seal(self.root, old_scores)
        seal(self.root, self.completed_scores())
        build_report(self.root)
        validate(self.root)
        info = archive(self.root)
        import zipfile
        with zipfile.ZipFile(info["archive"]) as z:
            self.assertIn((archived / "packet/working-notes.txt").relative_to(self.root).as_posix(), z.namelist())

    def test_refresh_archives_damaged_unsealed_packet_without_reusing_it(self):
        execute(self.root, server_factory=FakeServer)
        blind(self.root)
        gold_path = self.root / "grading/packet/gold.json"
        gold_path.write_text("Invalid JSON preserved for audit", encoding="utf-8")
        refreshed = blind(self.root, refresh=True)
        self.assertEqual((Path(refreshed["archived_grading"]) / "packet/gold.json").read_text(encoding="utf-8"), "Invalid JSON preserved for audit")
        self.assertEqual(sha(gold_path), sha(self.root / "input/gold.json"))
        seal(self.root, self.completed_scores())
        validate(self.root)

    def test_refresh_never_changes_sealed_grading(self):
        self.complete()
        grading = self.root / "grading"
        before = {p.relative_to(grading).as_posix(): p.read_bytes() for p in grading.rglob("*") if p.is_file()}
        with self.assertRaisesRegex(ValueError, "sealed"):
            blind(self.root, refresh=True)
        blind(self.root)
        self.assertEqual(before, {p.relative_to(grading).as_posix(): p.read_bytes() for p in grading.rglob("*") if p.is_file()})
        self.assertFalse((self.root / "grading-history").exists())

    def test_report_rechecks_sealed_inputs_and_current_valid_set(self):
        self.complete()
        packet_gold = self.root / "grading/packet/gold.json"
        original = packet_gold.read_bytes()
        gold = read(packet_gold)
        gold["reference_facts"] = ["Changed factual answer"]
        write(packet_gold, gold)
        seal_path = self.root / "grading/seal.json"
        saved_seal = seal_path.read_bytes()
        value = read(seal_path)
        value["packet_hashes"]["gold.json"] = sha(packet_gold)
        write(seal_path, value)
        with self.assertRaisesRegex(ValueError, "Scoring input"):
            build_report(self.root)
        packet_gold.write_bytes(original)
        seal_path.write_bytes(saved_seal)
        relative = next(iter(read(self.root / "grading/private-mapping.json").values()))
        result = read(self.root / relative)
        result["valid"] = False
        write(self.root / relative, result)
        with self.assertRaisesRegex(ValueError, "mapping"):
            build_report(self.root)
        with self.assertRaisesRegex(ValueError, "mapping"):
            validate(self.root)

    def test_blind_refresh_cli_flag(self):
        args = parser().parse_args(["blind", "--run-dir", str(self.root), "--refresh"])
        self.assertTrue(args.refresh)
        self.assertFalse(parser().parse_args(["blind", "--run-dir", str(self.root)]).refresh)

    def test_seal_report_archive_and_no_secret_extras(self):
        self.complete()
        (self.root / "auth.json").write_text("never archive", encoding="utf-8")
        info = archive(self.root)
        import zipfile
        with zipfile.ZipFile(info["archive"]) as z:
            self.assertNotIn("auth.json", z.namelist())
            self.assertIn("grading/seal.json", z.namelist())
        self.assertTrue(read(self.root / "validation.json")["passed"])
        with self.assertRaisesRegex(ValueError, "sealed"):
            execute(self.root, resume=True, server_factory=FakeServer)

    def test_retest_keeps_frozen_question_and_separate_statistics(self):
        self.complete()
        before = sha(self.root / "summary.json")
        child = retest(self.root, self.base / "retest", efforts=["max"], repetitions=5)
        a, b = check_inputs(self.root), check_inputs(child)
        self.assertEqual(a["input_hashes"], b["input_hashes"])
        self.assertEqual(len(b["schedule"]), 5)
        execute(child, server_factory=FakeServer)
        data = build_report(child, partial=True)
        self.assertEqual(data["attempt_count"], 5)
        self.assertEqual(len(data["conditions"]), 1)
        self.assertEqual(sha(self.root / "summary.json"), before)
        self.assertIn("上一批次", (child / "report.html").read_text(encoding="utf-8"))

    def test_valid_answer_can_have_missing_usage(self):
        for slot in check_inputs(self.root)["schedule"]:
            write_attempt(self.root, slot, usage=False)
        validate(self.root, partial=True)
        data = summarize(self.root, partial=True)
        self.assertEqual(data["valid_completed"], 4)
        self.assertEqual(data["conditions"][0]["statistics"]["visible_decode_tps"]["n"], 0)

    def test_effective_parameter_mismatch_never_starts_turn(self):
        server = Server.__new__(Server)
        server.root, server.provider = self.root, "test"
        server.fingerprint = {"global_instructions_sha256": sha(home() / "AGENTS.md") if (home() / "AGENTS.md").exists() else None}
        server.proc = SimpleNamespace(pid=os.getpid())
        server.counts, server.pending, server.active = collections.Counter(), [], False
        calls = []
        def rpc(method, params):
            calls.append(method)
            return {"model": "wrong-model", "modelProvider": "test", "reasoningEffort": params["config"]["model_reasoning_effort"], "serviceTier": "default", "thread": {"id": "fake"}}
        server.rpc = rpc
        r = server.run_trial(check_inputs(self.root)["schedule"][0], 1)
        self.assertFalse(r["valid"])
        self.assertEqual(calls, ["thread/start"])
        self.assertIn("settings differ", r["error_detail"])

    def test_explicit_turn_rejection_is_failed_and_allows_safe_resume(self):
        server = self.transport_server()
        slot = check_inputs(self.root)["schedule"][0]
        def event():
            if server.seq == 1:
                return self.thread_reply(server, slot)
            return time.perf_counter(), {"id": server.seq, "error": {"code": -32602, "message": "private-error-canary; no turn created"}}
        server.event = event
        result = server.run_trial(slot, 1)
        self.assertEqual([c.args[0]["method"] for c in server.send.call_args_list], ["thread/start", "turn/start"])
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["valid"])
        self.assertFalse(server.active)
        self.assertFalse((self.root / "inflight.json").exists())
        self.assertNotIn("private-error-canary", json.dumps(result))
        server.close()
        server.proc.stdin.close.assert_called_once()
        server.proc.wait.assert_called_once()
        directory = self.root / "runs" / slot["id"] / "attempt-001"
        before = sha(directory / "result.json")
        execute(self.root, resume=True, server_factory=FakeServer)
        self.assertEqual(len(FakeServer.calls), 3)
        self.assertEqual(summarize(self.root, partial=True)["valid_completed"], 3)
        execute(self.root, resume=True, retry_failed=True, server_factory=FakeServer)
        self.assertEqual(len(FakeServer.calls), 4)
        self.assertEqual(summarize(self.root, partial=True)["valid_completed"], 4)
        self.assertEqual(sha(directory / "result.json"), before)
        validate(self.root, partial=True)

    def test_transport_loss_keeps_inflight_until_processes_are_gone(self):
        for stage in ["submission", "stream"]:
            with self.subTest(stage=stage):
                root = prepare(self.case, self.base / ("loss-"+stage), ["gpt-6-sol"], ["max"], 1, self.prices)
                server = self.transport_server(root)
                slot = check_inputs(root)["schedule"][0]
                responded = False
                def event():
                    nonlocal responded
                    if server.seq == 1:
                        return self.thread_reply(server, slot)
                    if stage == "stream" and not responded:
                        responded = True
                        return time.perf_counter(), {"id": server.seq, "result": {"turn": {"id": "turn"}}}
                    raise ConnectionError("synthetic transport loss")
                server.event = event
                result = server.run_trial(slot, 1)
                self.assertEqual(result["status"], "interrupted")
                self.assertTrue(server.active)
                self.assertTrue((root / "inflight.json").exists())
                server.close()
                server.proc.stdin.close.assert_not_called()
                server.proc.terminate.assert_not_called()
                with self.assertRaisesRegex(ValueError, "active"):
                    recover_inflight(root)
                before = sha(root / "runs/0001/attempt-001/result.json")
                with patch("runtime.process_identity", return_value=None):
                    recover_inflight(root)
                self.assertFalse((root / "inflight.json").exists())
                self.assertEqual(sha(root / "runs/0001/attempt-001/result.json"), before)
                execute(root, resume=True, server_factory=FakeServer)
                self.assertEqual(summarize(root, partial=True)["valid_completed"], 0)
                execute(root, resume=True, retry_failed=True, server_factory=FakeServer)
                self.assertEqual(summarize(root, partial=True)["valid_completed"], 1)

    def test_transport_stream_excludes_reasoning_payloads(self):
        server = Server.__new__(Server)
        server.root, server.provider = self.root, "test"
        server.fingerprint = {"global_instructions_sha256": sha(home() / "AGENTS.md") if (home() / "AGENTS.md").exists() else None}
        server.proc = SimpleNamespace(pid=os.getpid())
        server.counts, server.pending, server.active = collections.Counter(), [], False
        slot = check_inputs(self.root)["schedule"][0]
        def rpc(method, params):
            if method == "thread/start":
                return {"model": slot["model"], "modelProvider": "test", "reasoningEffort": slot["effort"], "serviceTier": "default", "thread": {"id": "fake"}}
            t = time.perf_counter()
            events = [(1, "item/reasoning/textDelta", {"text": "private-reasoning-payload"}), (2, "item/agentMessage/delta", {"delta": "答案"}), (3, "item/agentMessage/delta", {"delta": "正确"}), (4, "item/completed", {"item": {"type": "agentMessage", "text": "答案正确"}}), (5, "turn/completed", {"turn": {"status": "completed"}})]
            server.pending.extend((t+offset, {"method": method, "params": {"threadId": "fake", **payload}}) for offset, method, payload in events)
            return {"turn": {"id": "turn"}}
        server.rpc = rpc
        r = server.run_trial(slot, 1)
        self.assertTrue(r["valid"])
        events = (self.root / "runs/0001/attempt-001/events.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("private-reasoning-payload", events)
        self.assertIsNone(r["visible_decode_tps"])

    def test_unsupported_preflight_has_no_inference(self):
        server = Server.__new__(Server)
        server.root, server.fingerprint, server.counts = self.root, {}, collections.Counter()
        calls = []
        def rpc(method, params):
            calls.append(method)
            return {"data": [{"model": "gpt-6-sol", "supportedReasoningEfforts": [{"reasoningEffort": "xhigh"}]}]}
        server.rpc = rpc
        with self.assertRaisesRegex(ValueError, "unsupported"):
            server.preflight(check_inputs(self.root))
        self.assertEqual(calls, ["model/list"])
        self.assertEqual(read(self.root / "preflight.json")["inference_calls"], 0)


if __name__ == "__main__":
    unittest.main()
