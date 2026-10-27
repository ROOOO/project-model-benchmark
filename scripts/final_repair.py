"""Plan a zero-inference correction from authoritative archived completions.

The returned files are written only into a new offline evaluation. Source
answers, results, events, timestamps and scoring seals are never changed.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import stat

from agentic import TreeLedger
from core import check_sensitive, digest, inside, now, read, require, sha


FINAL_POLICY = "turn-completed-v1"
PROVENANCE_PATH = "evaluation/final-answer-repair/provenance.json"
SOURCE_RECORD_PREFIX = "evaluation/final-answer-repair/source"
CODE_PREFIX = "evaluation/final-answer-repair/code"
FINAL_TEXT_METRICS = {"answer_characters", "answer_utf8_bytes", "chinese_characters",
                      "ttft_visible_s", "final_ttft_s", "answer_ready_s", "root_output_e2e_tps"}


def _bytes_sha(value):
    return hashlib.sha256(value).hexdigest()


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def _evidence_file(root, relative):
    """Do not follow source evidence aliases into unrelated private material."""
    path = inside(root, relative)
    require(path.is_file(), "Final-answer repair evidence file is unavailable: " + relative)
    for entry in (root / relative, *(root / relative).parents):
        info = entry.lstat()
        require(not entry.is_symlink() and not (getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)),
                "Final-answer repair evidence must not contain links")
        if entry == root:
            break
    require(path.stat().st_nlink == 1, "Final-answer repair evidence must not contain hardlink aliases")
    return path


def _evidence_bytes(root, relative):
    data = _evidence_file(root, relative).read_bytes()
    check_sensitive(data.decode("utf-8-sig"))
    return data


def _completion(snapshot, events):
    root_id = snapshot.get("root_thread_id")
    turns = snapshot.get("root_turn_order") or [snapshot.get("root_turn_id")]
    last_turn = turns[-1]
    completions = [event for event in events
                   if event.get("method") == "turn/completed"
                   and event.get("params", {}).get("threadId") == root_id
                   and event.get("params", {}).get("turn", {}).get("id") == last_turn
                   and event.get("params", {}).get("turn", {}).get("status") == "completed"]
    require(bool(completions), "Final-answer repair requires an authoritative completed root turn")
    signatures = {digest(event["params"]["turn"]) for event in completions}
    require(len(signatures) == 1, "Final-answer repair found conflicting root completion evidence")
    event = max(completions, key=lambda value: value.get("t_s", 0))
    items = [item for item in event["params"]["turn"].get("items", [])
             if isinstance(item, dict) and item.get("type") == "agentMessage"
             and item.get("phase") == "final_answer" and isinstance(item.get("text"), str)
             and item["text"].strip()]
    require(bool(items), "Final-answer repair requires nonempty authoritative final items")
    require(all(isinstance(item.get("id"), str) and item["id"] for item in items)
            and len({item["id"] for item in items}) == len(items),
            "Final-answer repair final item IDs are missing or duplicated")
    return last_turn, event, items


def prepare_final_answer_repair(source, protocol):
    """Validate every valid native attempt before the caller creates output.

    Returns replacement bytes, original-record archive copies, provenance and
    portable repair-code evidence. Nothing is executed or written here.
    """
    source = Path(source).resolve()
    require(protocol.get("execution_profile", {}).get("mode") == "project-readonly",
            "Final-answer repair requires a project-readonly source batch")
    prices = read(source / "prices.json")
    replacements, source_copies, attempts = {}, {}, []
    for result_path in sorted((source / "runs").glob("*/attempt-*/result.json")):
        relative = result_path.relative_to(source).as_posix()
        result_path = _evidence_file(source, relative)
        result_bytes = _evidence_bytes(source, relative)
        original = json.loads(result_bytes.decode("utf-8-sig"))
        if not original.get("valid"):
            continue
        require(original.get("status") == "completed", "Valid repair source attempt is not completed")
        directory = result_path.parent
        answer_relative = directory.joinpath("answer.txt").relative_to(source).as_posix()
        events_relative = directory.joinpath("events.jsonl").relative_to(source).as_posix()
        answer_bytes = _evidence_bytes(source, answer_relative)
        events_bytes = _evidence_bytes(source, events_relative)
        require(_bytes_sha(answer_bytes) == original.get("answer_sha256"), "Final-answer repair source answer hash differs")
        old_answer = answer_bytes.decode("utf-8")
        snapshot = original.get("agentic")
        require(isinstance(snapshot, dict) and snapshot.get("schema_version") == 1,
                "Final-answer repair source has no archived task-tree ledger")
        events = [json.loads(line) for line in events_bytes.decode("utf-8").splitlines() if line.strip()]
        require(events == snapshot.get("events"), "Final-answer repair source events differ from task-tree ledger")
        source_files = {relative: _bytes_sha(result_bytes), answer_relative: _bytes_sha(answer_bytes),
                        events_relative: _bytes_sha(events_bytes)}
        tree_relative = directory.joinpath("task-tree.json").relative_to(source).as_posix()
        if (source / tree_relative).exists():
            tree_bytes = _evidence_bytes(source, tree_relative)
            require(json.loads(tree_bytes.decode("utf-8-sig")) == snapshot, "Final-answer repair source task-tree export differs")
            source_files[tree_relative] = _bytes_sha(tree_bytes)
        legacy = TreeLedger.from_snapshot(snapshot)
        require(digest(legacy.snapshot()) == digest(snapshot), "Final-answer repair source ledger does not replay")
        require(legacy.final_answer.replace("\r\n", "\n") == old_answer.replace("\r\n", "\n"),
                "Final-answer repair source answer differs from its original extraction policy")
        ledger = TreeLedger.from_snapshot(snapshot, final_answer_policy=FINAL_POLICY)
        turn_id, completion, final_items = _completion(ledger.snapshot(), events)
        answer = ledger.final_answer
        expected = "\n\n".join(item["text"] for item in final_items)
        require(bool(answer.strip()) and answer == expected,
                "Final-answer repair canonical answer differs from authoritative completion")
        require(ledger.root_status == "completed" and ledger.tree_terminal and not ledger.errors,
                "Final-answer repair source lacks a verified terminal task tree")
        revised_snapshot = ledger.snapshot()
        require(revised_snapshot["events"] == events, "Final-answer repair changed original archived events")
        revised = copy.deepcopy(original)
        metrics = ledger.metrics(prices, protocol, original.get("elapsed_s"))
        for key, value in metrics.items():
            require(key in original, "Final-answer repair source is missing historical metric: " + key)
            if key not in FINAL_TEXT_METRICS:
                require(original[key] == value, "Final-answer repair changed historical usage, cost or workflow: " + key)
        changed_metrics = {key: {"before": original.get(key), "after": value}
                           for key, value in metrics.items() if original.get(key) != value}
        answer_bytes = answer.encode("utf-8")
        answer_sha = _bytes_sha(answer_bytes)
        revised.update(metrics)
        revised.update(agentic=revised_snapshot, answer_sha256=answer_sha,
                       final_answer_repair={"policy": FINAL_POLICY, "provenance": PROVENANCE_PATH,
                                            "source_result_sha256": source_files[relative], "new_inference_calls": 0})
        replacements[answer_relative] = answer_bytes
        replacements[relative] = _json_bytes(revised)
        replacements[tree_relative] = _json_bytes(revised_snapshot)
        for name in source_files:
            source_copies[name] = SOURCE_RECORD_PREFIX + "/" + name
        change_kind = "none" if old_answer == answer else ("outer-whitespace" if old_answer.strip() == answer.strip() else "text")
        attempts.append({"slot_id": original["slot_id"], "attempt": original["attempt"],
                         "relative_result": relative, "root_thread_id": snapshot["root_thread_id"],
                         "canonical_turn_id": turn_id, "canonical_completion_t_s": completion.get("t_s"),
                         "canonical_final_item_ids": [item["id"] for item in final_items],
                         "source_files_sha256": source_files, "source_answer_sha256": source_files[answer_relative],
                         "canonical_answer_sha256": answer_sha, "source_answer_characters": len(old_answer),
                         "canonical_answer_characters": len(answer), "change_kind": change_kind,
                         "changed_metrics": changed_metrics})
    require(bool(attempts), "Final-answer repair source has no valid native attempts")
    require(all(sha(_evidence_file(source, name)) == expected
                for attempt in attempts for name, expected in attempt["source_files_sha256"].items()),
            "Source changed during final-answer repair analysis")
    code_files = {}
    for name in ("agentic.py", "evaluation.py", "final_repair.py", "reporting.py"):
        path = Path(__file__).parent / name
        data = path.read_bytes()
        check_sensitive(data.decode("utf-8-sig"))
        code_files[CODE_PREFIX + "/" + name] = data
    code_hashes = {name: _bytes_sha(value) for name, value in code_files.items()}
    counts = {"attempts_replayed": len(attempts),
              "changed_answers": sum(item["change_kind"] != "none" for item in attempts),
              "substantive_text_changes": sum(item["change_kind"] == "text" for item in attempts),
              "outer_whitespace_changes": sum(item["change_kind"] == "outer-whitespace" for item in attempts)}
    metadata = {"policy": FINAL_POLICY, "provenance": PROVENANCE_PATH,
                "repair_code_sha256": code_hashes, "new_inference_calls": 0,
                "added_api_usd": 0, "costs_are_historical": True, **counts}
    provenance = {"schema_version": 1, "mode": "offline-final-answer-repair", "created_at": now(),
                  "source_batch": source.name, "source_root": str(source), **metadata,
                  "method": "Replay original archived events; select only nonempty final-answer items from the last completed native root turn. Preserve raw events and historical usage, costs and workflow timestamps; recompute final-text-derived metrics.",
                  "post_hoc": True, "evaluator_previously_saw_model_identity": True, "attempts": attempts}
    return {"replacements": replacements, "source_copies": source_copies,
            "code_files": code_files, "provenance": provenance, "metadata": metadata}
