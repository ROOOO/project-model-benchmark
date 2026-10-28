"""Read-only discovery and execution coverage of benchmark batches.

Observe protocols and attempt receipts without opening answer or score files,
inferring process liveness, or choosing between regrade versions.
"""
from __future__ import annotations

from collections import Counter
import os
from pathlib import Path
import re
import stat

from core import all_attempts, chosen_attempts, home, read, require


PRUNED_DIRECTORIES = frozenset({
    "_staging", "input", "runs", "grading", "grading-history", "evaluation",
    "capability", "sources", "answers", "report-history",
})
HISTORY_FIELDS = ("title", "project", "case_id", "created_at")
COUNT_FIELDS = ("planned", "started_slots", "valid_completed", "failed_attempts",
                "interrupted_attempts", "invalid_completed_attempts", "not_started")


def _linked(path):
    """Include Windows junctions, not just POSIX symbolic links."""
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _root(path):
    candidate = Path(path).expanduser().absolute()
    require(all(not _linked(part) for part in (candidate, *candidate.parents)),
            "Inventory does not follow linked or reparse paths")
    require(candidate.is_dir(), "Inventory root must be an existing directory")
    return candidate.resolve()


def _diagnostic(path, reason):
    return {"path": str(path), "reason": reason}


def _file(path):
    try:
        return not _linked(path) and stat.S_ISREG(path.lstat().st_mode)
    except FileNotFoundError:
        return False


def _present(root, relative):
    path = root
    for part in Path(relative).parts:
        path /= part
        try:
            if _linked(path):
                return False
        except FileNotFoundError:
            return False
    return _file(path)


def discover_runs(root):
    """Find run leaves without descending into copied protocols or links."""
    root = _root(root)
    found, diagnostics = [], []
    pending = [root]
    while pending:
        current = pending.pop()
        try:
            if _file(current / "comparison-data.json"):
                continue
            if _file(current / "protocol.json"):
                found.append(current)
                continue
            for child in reversed(sorted(current.iterdir(), key=lambda p: p.name)):
                if child.name in PRUNED_DIRECTORIES or _linked(child):
                    continue
                if child.is_dir():
                    pending.append(child)
        except (OSError, ValueError):
            diagnostics.append(_diagnostic(current, "directory_unreadable"))
    return sorted(found), diagnostics


def _protocol(root):
    require(_file(root / "protocol.json"), "Inventory requires a regular protocol.json")
    protocol = read(root / "protocol.json")
    require(isinstance(protocol, dict) and isinstance(protocol.get("schedule"), list),
            "Invalid inventory protocol")
    ids = set()
    for slot in protocol["schedule"]:
        require(isinstance(slot, dict), "Invalid inventory schedule")
        sid = slot.get("id")
        require(isinstance(sid, str) and re.fullmatch(r"[0-9]{4,}", sid) and sid not in ids,
                "Invalid or duplicate inventory slot")
        require(all(isinstance(slot.get(key), str) and slot[key] for key in ("model", "effort")),
                "Invalid inventory condition")
        require(isinstance(slot.get("service_tier", "default"), str), "Invalid inventory speed")
        ids.add(sid)
    return protocol


def history(root):
    """Keep the existing topic-only output shape while finding nested runs."""
    if not os.path.lexists(Path(root).expanduser()):
        return []
    roots, _ = discover_runs(root)
    result = []
    for run in roots:
        try:
            protocol = _protocol(run)
            result.append({key: protocol.get(key) for key in HISTORY_FIELDS})
        except (OSError, ValueError, KeyError, TypeError):
            # Inventory exposes per-run diagnostics; history remains topic-only.
            continue
    return result


def _condition(slot):
    tier = slot.get("service_tier", "default")
    return slot["model"], slot["effort"], "priority" if tier == "fast" else tier


def _receipt_metadata(receipt):
    return {key: receipt.get(key) for key in (
        "slot_id", "attempt", "model", "effort", "service_tier", "status", "valid", "relative_result")}


def _attempt_state(root, slots):
    """Guard the receipt glob before using the runner's selection rules."""
    runs, started = root / "runs", set()
    if not os.path.lexists(runs):
        return [], {}, started
    require(not _linked(runs) and runs.is_dir(), "Unsafe inventory attempts")
    for slot_dir in runs.iterdir():
        require(not _linked(slot_dir), "Unsafe inventory slot")
        if not slot_dir.is_dir():
            continue
        for attempt in slot_dir.iterdir():
            if not attempt.name.startswith("attempt-"):
                continue
            require(not _linked(attempt) and attempt.is_dir() and
                    re.fullmatch(r"attempt-[0-9]{3,}", attempt.name), "Unsafe inventory attempt")
            require(slot_dir.name in slots, "Attempt is outside the planned schedule")
            started.add(slot_dir.name)
            receipt = attempt / "result.json"
            require(not os.path.lexists(receipt) or _file(receipt), "Unsafe inventory receipt")
    attempts = all_attempts(root)
    for receipt in attempts:
        sid = receipt.get("slot_id")
        require(sid in slots and sid in started and receipt["relative_result"].split("/")[1] == sid,
                "Receipt is outside the planned slot")
        require(isinstance(receipt.get("status"), str) and isinstance(receipt.get("valid"), bool),
                "Invalid inventory receipt state")
        require(isinstance(receipt.get("attempt"), int) and receipt["attempt"] > 0,
                "Invalid inventory attempt number")
        expected = slots[sid]
        require(receipt.get("model") == expected["model"] and receipt.get("effort") == expected["effort"] and
                _condition(receipt) == _condition(expected), "Receipt condition differs from schedule")
    selected = chosen_attempts(root)
    # The core helpers may race an active receipt replacement.
    observed = {}
    for receipt in attempts:
        old = observed.get(receipt["slot_id"])
        if old is None or not (old.get("status") == "completed" and old.get("valid")):
            observed[receipt["slot_id"]] = receipt
    require({sid: _receipt_metadata(r) for sid, r in selected.items()} ==
            {sid: _receipt_metadata(r) for sid, r in observed.items()},
            "Inventory receipts changed during observation")
    return attempts, selected, started


def _source(root, protocol, diagnostics):
    relation = {"parent": protocol.get("parent"), "offline_regrade": bool(protocol.get("offline_regrade"))}
    if protocol.get("offline_regrade"):
        manifest = root / "evaluation" / "manifest.json"
        try:
            require(not _linked(manifest.parent) and _file(manifest), "Unsafe evaluation metadata")
            value = read(manifest)
            relation.update(source_root=value.get("source_root"), source_batch=value.get("source_batch"),
                            evaluation_manifest_sha256=protocol.get("evaluation_manifest_sha256"),
                            final_answer_repair=bool(value.get("final_answer_repair")))
        except (OSError, ValueError, KeyError, TypeError):
            diagnostics.append(_diagnostic(manifest, "evaluation_metadata_unavailable"))
    return relation


def _run(root, protocol, diagnostics):
    slots = {slot["id"]: slot for slot in protocol["schedule"]}
    planned = Counter(_condition(slot) for slot in slots.values())
    profile = protocol.get("execution_profile") or {}
    output = {"run_dir": str(root), **{key: protocol.get(key) for key in HISTORY_FIELDS},
              "execution_mode": profile.get("mode", "fixed-evidence"),
              "relation": _source(root, protocol, diagnostics),
              "sealed_present": _present(root, "grading/seal.json"),
              "report_present": _file(root / "report.html"),
              "runner_lock_present": os.path.lexists(root / "runner-lock.json"),
              "inflight_present": os.path.lexists(root / "inflight.json"),
              "process_liveness": "not_checked", "coverage_available": True}
    try:
        attempts, selected, started = _attempt_state(root, slots)
    except (OSError, ValueError, KeyError, TypeError):
        attempts, selected, started = [], {}, set()
        output["coverage_available"] = False
        diagnostics.append(_diagnostic(root, "attempt_metadata_unavailable_or_changed"))
    conditions = []
    for key, count in sorted(planned.items()):
        relevant = {sid for sid, slot in slots.items() if _condition(slot) == key}
        receipts = [r for r in attempts if r["slot_id"] in relevant]
        begun = len(relevant & started)
        item = {"model": key[0], "effort": key[1], "service_tier": key[2],
                "speed_mode": {"default": "Standard", "priority": "Fast"}.get(key[2], key[2]),
                "planned": count, "started_slots": begun,
                "valid_completed": sum(r.get("status") == "completed" and r.get("valid") for sid, r in selected.items() if sid in relevant),
                "failed_attempts": sum(r["status"] == "failed" for r in receipts),
                "interrupted_attempts": sum(r["status"] == "interrupted" for r in receipts),
                "invalid_completed_attempts": sum(r["status"] == "completed" and not r["valid"] for r in receipts),
                "not_started": count - begun}
        if not output["coverage_available"]:
            item.update({name: None for name in COUNT_FIELDS if name != "planned"})
        conditions.append(item)
    output["conditions"] = conditions
    output["counts"] = {name: (sum(c[name] for c in conditions) if name == "planned" or output["coverage_available"] else None)
                        for name in COUNT_FIELDS}
    return output


def inventory(root=None, run_dirs=None):
    """Observe exact batches or discover them under a root, without mutation."""
    require(not (root is not None and run_dirs is not None), "Choose inventory root or exact run directories")
    diagnostics = []
    if run_dirs is not None:
        require(bool(run_dirs), "Inventory run directories cannot be empty")
        roots = sorted({_root(path) for path in run_dirs})
        for run in roots:
            require(_file(run / "protocol.json"), "Exact inventory roots require protocol.json")
        scope = {"root": None, "run_dirs": [str(path) for path in roots]}
    else:
        scan_root = _root(root if root is not None else home() / "benchmarks")
        roots, diagnostics = discover_runs(scan_root)
        scope = {"root": str(scan_root), "run_dirs": None}
    result = []
    for run in roots:
        try:
            protocol = _protocol(run)
            result.append(_run(run, protocol, diagnostics))
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            diagnostics.append(_diagnostic(run, "invalid_protocol"))
    return {"schema_version": 1, "scope": scope, "runs": result, "diagnostics": diagnostics,
            "no_new_inference": True, "read_only": True,
            "note": "Observed execution metadata only; report/seal presence is not validation, and lock presence is not process liveness."}
