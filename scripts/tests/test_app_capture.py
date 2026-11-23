"""App answer capture and downstream source binding; no model or App calls."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import core
from app_capture import POLICY, capture_app_answer, validate_answer_entry, validate_capture
from benchmark import parser
from evaluation import formal_v3_gold, v4_gold_from_v3
from fresh_blind import check_topic_seal, prepare_topic
from test_scoring_v3 import v3_gold


def receipts(text="  支持证据\r\n\r\n|a|b|\r\n|1|2|  \n末尾不添换行🚀"):
    message = {
        "thread": {"id": "thread-1", "hostId": "local"},
        "turns": [{"id": "turn-1", "status": "completed", "error": None, "items": [
            {"type": "agentMessage", "id": "message-1", "phase": "final_answer",
             "text": text, "truncated": False}]}],
    }
    completion = {"polls": [{
        "thread": {"id": "thread-1", "hostId": "local"},
        "latestTurn": {"id": "turn-1", "status": "completed", "error": None,
                       "startedAt": 1, "completedAt": 2, "durationMs": 1000},
        "latestAssistantMessageId": "message-1",
        "latestAssistantMessage": {"id": "message-1", "turnId": "turn-1", "phase": "final_answer"},
    }]}
    return message, completion


class AppCaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.message, self.completion = receipts()
        self.message_path, self.completion_path = self.root / "message.json", self.root / "completion.json"
        self.output = self.root / "captured"

    def tearDown(self):
        self.temp.cleanup()

    def capture(self, output=None):
        core.write(self.message_path, self.message)
        core.write(self.completion_path, self.completion)
        return capture_app_answer(self.message_path, self.completion_path,
                                  output or self.output, "thread-1", "turn-1")

    def test_full_completed_message_is_saved_verbatim_including_whitespace_and_crlf(self):
        result = self.capture()
        self.assertFalse(result["reused"])
        expected = self.message["turns"][0]["items"][0]["text"].encode("utf-8")
        self.assertEqual((self.output / "answer.txt").read_bytes(), expected)
        receipt = validate_capture(result["answer_receipt"])
        self.assertEqual(receipt["answer_source"], POLICY)
        self.assertEqual(receipt["answer_utf8_bytes"], len(expected))
        self.assertEqual(receipt["new_model_calls"], 0)

    def test_same_capture_is_idempotent_without_changing_any_bytes(self):
        self.capture()
        before = {p.name: p.read_bytes() for p in self.output.iterdir()}
        self.assertTrue(self.capture()["reused"])
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.output.iterdir()})

    def test_conflicting_source_and_incomplete_destination_are_never_overwritten(self):
        self.capture()
        before = (self.output / "answer.txt").read_bytes()
        self.message["turns"][0]["items"][0]["text"] = "另一份正文"
        with self.assertRaisesRegex(ValueError, "different source"):
            self.capture()
        self.assertEqual((self.output / "answer.txt").read_bytes(), before)
        other = self.root / "incomplete"
        other.mkdir()
        (other / "answer.txt").write_bytes(b"partial")
        with self.assertRaisesRegex(ValueError, "without a complete receipt"):
            self.capture(other)
        self.assertEqual((other / "answer.txt").read_bytes(), b"partial")

    def test_incomplete_failed_and_aborted_turns_cannot_produce_an_answer(self):
        for status in ("inProgress", "failed", "interrupted", "aborted"):
            for location in ("message", "completion"):
                with self.subTest(status=status, location=location):
                    self.message, self.completion = receipts()
                    if location == "message":
                        self.message["turns"][0]["status"] = status
                    else:
                        self.completion["polls"][0]["latestTurn"]["status"] = status
                    with self.assertRaisesRegex(ValueError, "completed|terminal"):
                        self.capture()
                    self.assertFalse(self.output.exists())

    def test_empty_truncated_unclassified_and_multiple_finals_are_rejected(self):
        variants = [
            {"text": ""}, {"text": " \n\t"}, {"truncated": True}, {"truncated": None},
            {"phase": "commentary"}, {"phase": "partial_answer"}, {"type": "reasoning"},
        ]
        for update in variants:
            with self.subTest(update=update):
                self.message, self.completion = receipts()
                self.message["turns"][0]["items"][0].update(update)
                with self.assertRaises(ValueError):
                    self.capture()
        self.message, self.completion = receipts()
        self.message["turns"][0]["items"].append(copy.deepcopy(self.message["turns"][0]["items"][0]))
        with self.assertRaisesRegex(ValueError, "unambiguous"):
            self.capture()
        self.assertFalse(self.output.exists())

    def test_all_thread_turn_host_and_message_bindings_are_checked(self):
        mutations = [
            ("message-thread", lambda m, c: m["thread"].update(id="wrong")),
            ("message-turn", lambda m, c: m["turns"][0].update(id="wrong")),
            ("completion-thread", lambda m, c: c["polls"][0]["thread"].update(id="wrong")),
            ("completion-host", lambda m, c: c["polls"][0]["thread"].update(hostId="wrong")),
            ("completion-turn", lambda m, c: c["polls"][0]["latestTurn"].update(id="wrong")),
            ("latest-message", lambda m, c: c["polls"][0].update(latestAssistantMessageId="wrong")),
            ("marker-message", lambda m, c: c["polls"][0]["latestAssistantMessage"].update(id="wrong")),
            ("marker-turn", lambda m, c: c["polls"][0]["latestAssistantMessage"].update(turnId="wrong")),
            ("marker-phase", lambda m, c: c["polls"][0]["latestAssistantMessage"].update(phase="commentary")),
            ("completed-error", lambda m, c: c["polls"][0]["latestTurn"].update(error={"code": "failure"})),
        ]
        for label, mutate in mutations:
            with self.subTest(binding=label):
                self.message, self.completion = receipts()
                mutate(self.message, self.completion)
                with self.assertRaises(ValueError):
                    self.capture()
                self.assertFalse(self.output.exists())

    def test_full_chat_history_private_reasoning_or_progress_summary_are_not_archived(self):
        for location in ("top", "turn", "item", "completion"):
            with self.subTest(location=location):
                self.message, self.completion = receipts()
                target = {"top": self.message, "turn": self.message["turns"][0],
                          "item": self.message["turns"][0]["items"][0],
                          "completion": self.completion["polls"][0]["latestAssistantMessage"]}[location]
                target["reasoning" if location != "completion" else "text"] = "not public final metadata"
                with self.assertRaisesRegex(ValueError, "allowlisted"):
                    self.capture()
                self.assertFalse(self.output.exists())

    def test_capture_and_selected_answer_tampering_are_detected(self):
        result = self.capture()
        entry = {"answer": result["answer"], "answer_sha256": result["answer_sha256"],
                 "answer_source": POLICY, "answer_receipt": result["answer_receipt"],
                 "answer_receipt_sha256": result["answer_receipt_sha256"]}
        self.assertEqual(validate_answer_entry(entry)["answer_source"], POLICY)
        original = (self.output / "answer.txt").read_bytes()
        (self.output / "answer.txt").write_bytes(original + b"\n")
        with self.assertRaisesRegex(ValueError, "hash differs"):
            validate_answer_entry(entry)
        (self.output / "answer.txt").write_bytes(original)
        self.message["turns"][0]["items"][0]["text"] += "changed"
        core.write(self.output / "thread-message.json", self.message)
        with self.assertRaisesRegex(ValueError, "hash differs"):
            validate_capture(result["answer_receipt"])

    def test_receipt_binds_source_even_if_local_file_hash_is_recomputed(self):
        result = self.capture()
        receipt_path = Path(result["answer_receipt"])
        receipt = core.read(receipt_path)
        receipt["source"]["message_id"] = "unrelated"
        core.write(receipt_path, receipt)
        with self.assertRaisesRegex(ValueError, "source binding"):
            validate_capture(receipt_path)

    def test_adjacent_receipt_is_discovered_and_declared_receipt_cannot_be_missing(self):
        result = self.capture()
        entry = validate_answer_entry({"answer": result["answer"], "answer_sha256": result["answer_sha256"]})
        self.assertEqual(entry["answer_receipt_sha256"], result["answer_receipt_sha256"])
        for relative_receipt in (None, "answer-receipt.json"):
            relative = {"answer": os.path.relpath(result["answer"]), "answer_sha256": result["answer_sha256"]}
            if relative_receipt:
                relative["answer_receipt"] = relative_receipt
            self.assertEqual(validate_answer_entry(relative)["answer_receipt_sha256"], result["answer_receipt_sha256"])
        plain = self.root / "plain.txt"
        plain.write_text("historical", encoding="utf-8")
        legacy = {"answer": str(plain), "answer_sha256": core.sha(plain)}
        self.assertEqual(validate_answer_entry(legacy), legacy)
        with self.assertRaisesRegex(ValueError, "requires its capture"):
            validate_answer_entry({**legacy, "answer_source": POLICY})

    def test_capture_cli_is_offline_and_uses_the_same_validation(self):
        core.write(self.message_path, self.message)
        core.write(self.completion_path, self.completion)
        command = [sys.executable, "-X", "utf8", str(Path(__file__).resolve().parents[1] / "benchmark.py"),
                   "capture-app-answer", "--message-json", str(self.message_path),
                   "--completion-json", str(self.completion_path), "--thread-id", "thread-1",
                   "--turn-id", "turn-1", "--output-dir", str(self.output)]
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["new_model_calls"], 0)
        args = parser().parse_args(["validate-topic", "--packet", "p", "--private-mapping", "m"])
        self.assertEqual(args.private_mapping, "m")

    def test_topic_packet_and_export_validate_the_same_capture_without_revealing_its_label(self):
        self.message, self.completion = receipts("correct supporting evidence")
        result = self.capture()
        source = self.root / "source"
        (source / "input").mkdir(parents=True)
        (source / "input/question.txt").write_text("question", encoding="utf-8")
        (source / "input/prompt.txt").write_text("prompt", encoding="utf-8")
        gold = formal_v3_gold(v4_gold_from_v3(v3_gold({"reference_facts": ["fixture"], "verification": ["fixture"]})))
        rubric = self.root / "rubric.json"
        core.write(rubric, gold)
        entry = {"identity": "one", "source": str(source), **{key: result[key] for key in
                 ("answer", "answer_sha256", "answer_receipt", "answer_receipt_sha256", "answer_source")}}
        packet, mapping = self.root / "packet", self.root / "private.json"
        prepare_topic([entry], rubric, packet, mapping)
        input_seal = core.read(packet / "input-seal.json")
        self.assertNotIn("A001", json.dumps(input_seal["source_validation"]))
        self.assertNotIn("thread-1", json.dumps(input_seal))
        score = {
            "points": {r["id"]: r["max"] for r in gold["rubric"]},
            "notes": {r["id"]: "Reviewed." for r in gold["rubric"]},
            "evidence": {r["id"]: ["correct supporting evidence"] for r in gold["rubric"]}, "issues": [],
            "checks": {c["id"]: {"status": "correct", "note": "Reviewed.",
                       "evidence": ["correct supporting evidence"]} for c in gold["checkpoints"]},
            "presentation_notes": [],
        }
        core.write(packet / "reviews/A001.json", score)
        seal = subprocess.run([sys.executable, "-X", "utf8", str(packet / "review_io.py"), "seal"],
                              capture_output=True, text=True)
        self.assertEqual(seal.returncode, 0, seal.stderr)
        with self.assertRaisesRegex(ValueError, "private mapping"):
            check_topic_seal(packet)
        self.assertEqual(check_topic_seal(packet, private_mapping=mapping)["answers"], 1)
        (self.output / "answer.txt").write_text("altered after grading", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "answer changed|hash differs"):
            check_topic_seal(packet, private_mapping=mapping)


if __name__ == "__main__":
    unittest.main()
