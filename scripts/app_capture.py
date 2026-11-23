"""Offline capture of one completed App final; never asks a model to copy text."""
from __future__ import annotations

import hashlib
from pathlib import Path

from core import check_sensitive, digest, inside, now, read, require, sha, write


POLICY = "app-final-answer-v1"
MESSAGE_FILE = "thread-message.json"
COMPLETION_FILE = "turn-completion.json"
RECEIPT_FILE = "answer-receipt.json"


def _object(value, allowed, label):
    require(isinstance(value, dict) and not set(value) - set(allowed),
            "Use an allowlisted public App " + label + " receipt; do not save full chat history")
    return value


def _identity(value, label):
    require(isinstance(value, str) and value.strip() and len(value) <= 240,
            "Missing App capture " + label)
    check_sensitive(value)
    return value


def final_message(message, completion, thread_id, turn_id):
    """Read only exact public message and matching completion metadata."""
    _identity(thread_id, "thread ID")
    _identity(turn_id, "turn ID")
    _object(message, ("thread", "turns"), "message")
    thread = _object(message.get("thread"), ("id", "hostId"), "thread")
    require(thread.get("id") == thread_id, "App message thread ID differs")
    host_id = _identity(thread.get("hostId"), "host ID")
    turns = message.get("turns")
    require(isinstance(turns, list) and len(turns) == 1, "Capture exactly one target App turn")
    turn = _object(turns[0], ("id", "status", "error", "items"), "turn")
    require(turn.get("id") == turn_id, "App message turn ID differs")
    require(turn.get("status") == "completed" and "error" in turn and turn["error"] is None,
            "App target turn is not successfully completed")
    items = turn.get("items")
    require(isinstance(items, list) and len(items) == 1, "Capture requires one unambiguous App final message")
    item = _object(items[0], ("type", "id", "phase", "text", "truncated"), "final message")
    require(item.get("type") == "agentMessage" and item.get("phase") == "final_answer",
            "App answer must be a final_answer, not commentary, deltas or a summary")
    message_id = _identity(item.get("id"), "message ID")
    require(item.get("truncated") is False, "App final is truncated or completeness is unverified")
    text = item.get("text")
    require(isinstance(text, str) and bool(text.strip()), "App final answer is empty")
    check_sensitive(text)

    _object(completion, ("polls",), "completion")
    polls = completion.get("polls")
    require(isinstance(polls, list) and len(polls) == 1, "Capture exactly one target completion")
    poll = _object(polls[0], ("thread", "latestTurn", "latestAssistantMessageId", "latestAssistantMessage"),
                   "completion poll")
    target = _object(poll.get("thread"), ("id", "hostId"), "completion thread")
    require(target.get("id") == thread_id and target.get("hostId") == host_id,
            "App completion thread or host differs")
    terminal = _object(poll.get("latestTurn"),
                       ("id", "status", "error", "startedAt", "completedAt", "durationMs"),
                       "completion turn")
    require(terminal.get("id") == turn_id, "App completion turn ID differs")
    require(terminal.get("status") == "completed" and "error" in terminal and terminal["error"] is None,
            "App completion is not a successful terminal turn")
    marker = _object(poll.get("latestAssistantMessage"), ("id", "turnId", "phase"), "completion marker")
    require(poll.get("latestAssistantMessageId") == message_id and marker.get("id") == message_id
            and marker.get("turnId") == turn_id and marker.get("phase") == "final_answer",
            "App completion does not identify the captured final message")
    return text, {"host_id": host_id, "thread_id": thread_id, "turn_id": turn_id, "message_id": message_id}


def validate_capture(receipt_path, answer_path=None):
    """Recompute the source binding and exact UTF-8 bytes without changing files."""
    receipt_path = Path(receipt_path)
    root = receipt_path.parent
    receipt = read(receipt_path)
    _object(receipt, ("schema_version", "answer_source", "status", "captured_at", "source",
                      "source_digests", "answer_sha256", "answer_utf8_bytes", "answer_characters",
                      "files_sha256", "new_model_calls"), "capture")
    require(isinstance(receipt, dict) and receipt.get("schema_version") == 1
            and receipt.get("answer_source") == POLICY and receipt.get("status") == "captured",
            "App answer capture receipt is incomplete or unsupported")
    files = receipt.get("files_sha256")
    require(isinstance(files, dict) and set(files) == {MESSAGE_FILE, COMPLETION_FILE, "answer.txt"},
            "App capture receipt file set differs")
    for name, expected in files.items():
        path = inside(root, name)
        require(path.is_file() and sha(path) == expected, "App capture file hash differs: " + name)
    message, completion = read(root / MESSAGE_FILE), read(root / COMPLETION_FILE)
    source = receipt.get("source")
    require(isinstance(source, dict), "App capture source is missing")
    text, identity = final_message(message, completion, source.get("thread_id"), source.get("turn_id"))
    require(source == identity and receipt.get("source_digests") == {
        "message": digest(message), "completion": digest(completion)}, "App capture source binding differs")
    encoded = text.encode("utf-8")
    require((root / "answer.txt").read_bytes() == encoded
            and receipt.get("answer_sha256") == hashlib.sha256(encoded).hexdigest()
            and receipt.get("answer_utf8_bytes") == len(encoded)
            and receipt.get("answer_characters") == len(text), "Captured answer differs from completed App final")
    if answer_path is not None:
        require(Path(answer_path).read_bytes() == encoded, "Selected answer differs from App capture")
    return receipt


def capture_app_answer(message_json, completion_json, output_dir, thread_id, turn_id):
    """Capture once, then verify or reuse; conflicts never overwrite an answer."""
    message, completion = read(message_json), read(completion_json)
    text, source = final_message(message, completion, thread_id, turn_id)
    root = Path(output_dir).resolve()
    source_digests = {"message": digest(message), "completion": digest(completion)}
    encoded = text.encode("utf-8")
    if root.exists():
        require(root.is_dir() and (root / RECEIPT_FILE).is_file(),
                "Capture target exists without a complete receipt; inspect it or use a fresh directory")
        receipt = validate_capture(root / RECEIPT_FILE)
        require(receipt["source"] == source and receipt["source_digests"] == source_digests,
                "Capture target belongs to a different source; no files were overwritten")
        reused = True
    else:
        root.mkdir(parents=True, exist_ok=False)
        write(root / MESSAGE_FILE, message)
        write(root / COMPLETION_FILE, completion)
        # write_bytes preserves leading/trailing whitespace, CRLF and a missing final newline.
        (root / "answer.txt").write_bytes(encoded)
        receipt = {
            "schema_version": 1, "answer_source": POLICY, "status": "captured", "captured_at": now(),
            "source": source, "source_digests": source_digests,
            "answer_sha256": hashlib.sha256(encoded).hexdigest(), "answer_utf8_bytes": len(encoded),
            "answer_characters": len(text),
            "files_sha256": {name: sha(root / name) for name in (MESSAGE_FILE, COMPLETION_FILE, "answer.txt")},
            "new_model_calls": 0,
        }
        # The receipt is published last; an interrupted write is never a valid capture.
        write(root / RECEIPT_FILE, receipt)
        validate_capture(root / RECEIPT_FILE)
        reused = False
    return {"status": "captured", "reused": reused, "answer": str(root / "answer.txt"),
            "answer_sha256": receipt["answer_sha256"], "answer_source": POLICY,
            "answer_receipt": str(root / RECEIPT_FILE), "answer_receipt_sha256": sha(root / RECEIPT_FILE),
            "source": source, "new_model_calls": 0}


def validate_answer_entry(entry):
    """Optional new source binding; legacy answers retain their existing checks."""
    require(isinstance(entry, dict), "Invalid answer entry")
    result = dict(entry)
    answer = Path(result["answer"]).resolve()
    if result.get("answer_source") == "antigravity-terminal-v1" or result.get("cli_answer_receipt"):
        from antigravity import validate_terminal_capture
        path = result.get("cli_answer_receipt")
        require(isinstance(path, str) and path and not result.get("answer_receipt"), "CLI answer needs its native receipt")
        receipt = validate_terminal_capture(path, answer)
        require(result.get("answer_sha256") == receipt["answer_sha256"], "CLI answer hash differs from receipt")
        require(result.get("cli_answer_receipt_sha256", sha(path)) == sha(path), "CLI receipt changed")
        result.update(answer_source=receipt["answer_source"], cli_answer_receipt_sha256=sha(path))
        return result
    receipt_path = result.get("answer_receipt")
    adjacent = answer.parent / RECEIPT_FILE
    if not receipt_path and adjacent.is_file():
        receipt_path = str(adjacent)
        result["answer_receipt"] = receipt_path
    required = result.get("answer_source") == POLICY or receipt_path is not None
    if not required:
        require("answer_receipt_sha256" not in result, "Answer receipt hash lacks its source receipt")
        return result
    require(isinstance(receipt_path, str) and receipt_path, "App answer requires its capture receipt")
    if not Path(receipt_path).is_absolute():
        receipt_path = str(answer.parent / receipt_path)
        result["answer_receipt"] = receipt_path
    receipt = validate_capture(receipt_path, answer)
    actual = sha(receipt_path)
    if "answer_receipt_sha256" in result:
        require(result["answer_receipt_sha256"] == actual, "App capture receipt differs from frozen mapping")
    require(result.get("answer_sha256") == receipt["answer_sha256"], "Answer entry differs from App capture")
    result.update(answer_receipt_sha256=actual, answer_source=POLICY)
    return result
