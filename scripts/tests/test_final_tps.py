"""Final answer TPS estimates use only validated, complete offline evidence."""
import csv
import hashlib
import importlib.util
from importlib import metadata
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agentic import TreeLedger
from comparison import compare
from core import BenchmarkError, read
from final_tps import ESTIMATE_FIELDS, VOCAB_SHA256, VOCAB_URL, estimate_final_tps, load_proxy_estimator
from reporting import build_report
import test_final_answer_repair as repair_fixture


class FakeEncoding:
    name = "o200k_base"

    def __init__(self):
        self.texts = []

    def encode_ordinary(self, text):
        self.texts.append(text)
        return list(text)


def example(*, text="答案正文", first=2, last=4, delta_text=None, retry=False, final_items=1):
    ledger = TreeLedger("root", "turn", {"model": "gpt-6-unknown"})

    def event(method, time, **params):
        ledger.observe({"method": method, "params": {"threadId": "root", "turnId": "turn", **params}}, time)

    if retry:
        event("item/agentMessage/delta", 0.5, itemId="abandoned", delta="不应计入的重试")
    item = {"type": "agentMessage", "id": "final", "phase": "final_answer", "text": text}
    event("item/started", 1, item=item)
    stream = text if delta_text is None else delta_text
    if first is not None:
        event("item/agentMessage/delta", first, itemId="final", delta=stream[:1])
    if last is not None:
        event("item/agentMessage/delta", last, itemId="final", delta=stream[1:])
    event("item/completed", 5, item=item)
    items = [item] if final_items else []
    if final_items == 2:
        second = {"type": "agentMessage", "id": "second", "phase": "final_answer", "text": "另一个正文"}
        items.append(second)
    event("turn/completed", 6, turn={"id": "turn", "status": "completed", "items": items})
    row = {"selected": True, "valid": True, "status": "completed", "visible_decode_tps": None, "visible_tokens": None}
    return {"agentic": ledger.snapshot()}, row


class FinalTPSUnitTests(unittest.TestCase):
    def setUp(self):
        self.encoding = FakeEncoding()

    def estimate(self, record, row, answer="答案正文"):
        return estimate_final_tps(record, row, answer, self.encoding)

    def test_uses_completed_root_item_and_ignores_retry_stream(self):
        record, row = example(retry=True)
        value = self.estimate(record, row)
        self.assertEqual(self.encoding.texts, ["答案正文"])
        self.assertEqual(value["estimated_final_item_id"], "final")
        self.assertEqual(value["estimated_final_text_tokens"], 4)
        self.assertEqual(value["estimated_final_stream_s"], 2)
        self.assertEqual(value["estimated_final_text_tps"], 2)
        self.assertEqual(value["estimated_final_tps_method"], "o200k_base_proxy")
        self.assertIsNone(row["visible_decode_tps"])

    def test_missing_or_equal_timestamps_remain_unknown(self):
        for first, last, reason in [(None, None, "final_stream_timestamps_missing"),
                                    (2, 2, "final_stream_interval_not_positive")]:
            record, row = example(first=first, last=last)
            with self.subTest(first=first, last=last):
                value = self.estimate(record, row)
                self.assertIsNone(value["estimated_final_text_tps"])
                self.assertEqual(value["estimated_final_tps_reason"], reason)
        self.assertEqual(self.encoding.texts, [])

    def test_missing_conflicting_or_mismatched_final_never_guessed(self):
        cases = [(*example(final_items=0), "答案正文", "authoritative_final_item_count_not_one"),
                 (*example(final_items=2), "答案正文", "authoritative_final_item_count_not_one"),
                 (*example(delta_text="其他内容"), "答案正文", "native_final_evidence_conflicting"),
                 (*example(), "不同答案", "authoritative_final_differs_from_answer")]
        for record, row, answer, reason in cases:
            with self.subTest(reason=reason):
                value = self.estimate(record, row, answer)
                self.assertIsNone(value["estimated_final_text_tps"])
                self.assertEqual(value["estimated_final_tps_reason"], reason)
        self.assertEqual(self.encoding.texts, [])

    def test_validated_native_visible_rate_has_precedence(self):
        record, row = example()
        row.update(visible_decode_tps=12.5, visible_tokens=50)
        value = self.estimate(record, row)
        self.assertEqual(value["estimated_final_tps_method"], "native_available")
        self.assertIsNone(value["estimated_final_text_tps"])
        self.assertEqual(self.encoding.texts, [])

    def test_unselected_or_invalid_attempt_is_not_estimated(self):
        for field in ("selected", "valid"):
            record, row = example()
            row[field] = False
            with self.subTest(field=field):
                self.assertEqual(self.estimate(record, row)["estimated_final_tps_reason"], "not_selected_valid_completion")
        self.assertEqual(self.encoding.texts, [])

    def test_missing_dependency_stops_before_creating_destination(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(sys.modules, {"tiktoken": None}):
            output = Path(temp) / "out"
            with self.assertRaisesRegex(BenchmarkError, "tiktoken 0.14.0"):
                compare([Path(temp) / "source"], output, estimate_answer_tps=True)
            self.assertFalse(output.exists())

    def test_missing_or_tampered_cache_is_rejected_before_encoding(self):
        class FakeModule:
            def get_encoding(self, name):
                raise AssertionError("must not load or download an unverified vocabulary")

        with tempfile.TemporaryDirectory() as temp, patch.dict(sys.modules, {"tiktoken": FakeModule()}), \
                patch.dict("os.environ", {"TIKTOKEN_CACHE_DIR": temp}), \
                patch.object(metadata, "version", side_effect=lambda name: "0.14.0" if name == "tiktoken" else "test"):
            with self.assertRaisesRegex(BenchmarkError, "vocabulary is missing"):
                load_proxy_estimator()
            (Path(temp) / hashlib.sha1(VOCAB_URL.encode()).hexdigest()).write_bytes(b"invalid")
            with self.assertRaisesRegex(BenchmarkError, "wrong SHA256"):
                load_proxy_estimator()

    def test_pinned_proxy_loads_verified_cache_without_network(self):
        if importlib.util.find_spec("tiktoken") is None or not os.environ.get("TIKTOKEN_CACHE_DIR"):
            self.skipTest("isolated tiktoken runtime and preloaded vocabulary not configured")
        import tiktoken  # Import before intercepting network primitives.
        with patch("socket.create_connection", side_effect=AssertionError("must not connect")), \
                patch("socket.socket.connect", side_effect=AssertionError("must not connect")):
            encoding, details = load_proxy_estimator()
        self.assertEqual(details["vocabulary_sha256"], VOCAB_SHA256)
        self.assertEqual(details["model_tokenizer_mapping"], "unverified")
        self.assertEqual(encoding.name, "o200k_base")
        self.assertEqual(len(encoding.encode_ordinary("None 使用默认值。")), 5)


class FinalTPSIntegrationTests(unittest.TestCase):
    def test_new_comparison_keeps_source_and_exports_explicit_unknowns(self):
        fixture = repair_fixture.FinalAnswerRepairTests("test_repair_preserves_source_and_history_and_recomputes_final_metrics")
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        fixture.create_revision()
        fixture.grade(fixture.revision)
        build_report(fixture.revision)
        before = fixture.hashes(fixture.revision)
        output = fixture.base / "tps-comparison"
        info = {"method": "local_proxy_tokenization", "encoding": "o200k_base", "version": "0.14.0",
                "model_tokenizer_mapping": "unverified", "vocabulary_sha256": "test"}
        with patch("comparison.load_proxy_estimator", return_value=(FakeEncoding(), info)), \
                patch("runtime.Server", side_effect=AssertionError("must not sample")), \
                patch("socket.socket", side_effect=AssertionError("must not use the network")):
            result = compare([fixture.revision], output, estimate_answer_tps=True)
        self.assertFalse(result["inference_started"])
        self.assertEqual(before, fixture.hashes(fixture.revision))
        data = read(output / "comparison-data.json")
        self.assertEqual(data["final_tps_estimation"]["available_samples"], 0)
        self.assertEqual(data["final_tps_estimation"]["selected_valid_samples"], 3)
        self.assertTrue(all(r["estimated_final_tps_reason"] == "final_stream_interval_not_positive" for r in data["samples"]))
        self.assertTrue(all(r["visible_decode_tps"] is None for r in data["samples"]))
        with (output / "samples.csv").open(encoding="utf-8-sig", newline="") as f:
            samples = list(csv.DictReader(f))
        self.assertEqual(len(samples), 3)
        self.assertTrue(all(set(ESTIMATE_FIELDS) <= set(row) for row in samples))
        self.assertIn("最终正文 TPS：代理分词估算", (output / "report.html").read_text(encoding="utf-8"))
        with zipfile.ZipFile(output / "comparison.zip") as archive:
            for name in archive.namelist():
                self.assertEqual(archive.read(name), (output / name).read_bytes())


if __name__ == "__main__":
    unittest.main()
