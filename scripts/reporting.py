"""Anonymous scoring, portable reports, validation and allowlisted archives."""
from __future__ import annotations

import csv
import html
from html.parser import HTMLParser
import io
import json
import math
from pathlib import Path
import random
import re
import secrets
import shutil
import stat
import subprocess
from urllib.parse import quote, unquote, urlparse
import zipfile

from core import DIMENSION_LABELS, METRICS, all_attempts, check_inputs, check_sensitive, chosen_attempts, digest, distribution, inside, metric_keys, metrics_for_protocol, now, price_for, protocol_tiers, read, require, scoring_gold, scoring_weights, sha, usage_issues, validate_scores, write
from runtime import still_running


CSV_FIELDS = list(dict.fromkeys(["slot_id", "attempt", "model", "effort", "repetition", "status", "valid", "selected", "accuracy", "readability", "quality", "anonymous_id", "actual_gateway_charge", *METRICS, "answer_link"]))
CSV_V2_FIELDS = CSV_FIELDS[:9] + ["coverage", "insight"] + CSV_FIELDS[9:] + ["scoring_version", "accuracy_max", "coverage_max", "insight_max", "readability_max", "critical_errors", "errors", "omissions"]
AGENTIC_METRICS = (
    "answer_ready_s", "root_turn_completed_s", "workflow_elapsed_s", "first_progress_s", "final_ttft_s", "delegation_count", "root_output_e2e_tps", "tree_output_e2e_tps",
    *tuple(prefix + key for prefix in ("root_", "child_", "tree_")
           for key in ("input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens", "reasoning_tokens",
                       "observed_input_tokens", "observed_cached_input_tokens", "observed_cache_write_input_tokens", "observed_output_tokens", "observed_reasoning_tokens",
                       "estimated_api_usd", "observed_api_usd")),
)
AGENTIC_FLAGS = ("delegation_occurred", "accounting_complete", "pricing_complete")


def execution_identity(protocol, fingerprint=None):
    """A stable comparison identity, excluding per-run physical work paths."""
    profile = protocol.get("execution_profile") or {"mode": "fixed-evidence"}
    mode = profile.get("mode", "fixed-evidence")
    if mode == "fixed-evidence":
        return mode, digest({"mode": mode})
    require(mode == "project-readonly", "Unknown execution mode")
    identity = profile.get("comparison_fingerprint")
    require(isinstance(identity, str) and bool(re.fullmatch(r"[0-9a-f]{64}", identity)),
            "Project-readonly profile needs a frozen comparison fingerprint")
    environment = None
    if fingerprint is not None:
        require(isinstance(fingerprint, dict), "Malformed runtime fingerprint")
        state = fingerprint.get("execution", {})
        environment = {k: fingerprint.get(k) for k in ("binary_sha256", "provider", "endpoint_host", "global_instructions_sha256")}
        environment["execution"] = {k: state.get(k) for k in ("agent_settings", "agent_roles_sha256", "feature_overrides",
                                                           "models", "catalog_client_version", "native_defaults")}
        environment["execution"].update({k: state[k] for k in ("instruction_sources_sha256", "inherited_config_sha256",
                                                              "model_catalog_sha256", "binary_companions_sha256",
                                                              "effective_configuration_fingerprint", "effective_native_settings") if k in state})
    return mode, digest({"execution_profile": identity, "instructions_sha256": protocol.get("instructions_sha256"),
                         "runtime_environment": environment})


def runtime_fingerprint(root):
    path = Path(root) / "runtime.json"
    return read(path).get("fingerprint") if path.is_file() else None


def check_artifact_sensitive(path, binary_source=False):
    """Scan frozen source bytes without requiring the source's encoding to change."""
    data = Path(path).read_bytes()
    if binary_source:
        # ASCII credential patterns remain intact in UTF-8, GB2312 and binaries.
        decoded = data.decode("latin-1")
        check_sensitive(decoded)
        check_sensitive(decoded.replace("\x00", ""))
        if data.startswith((b"\xff\xfe", b"\xfe\xff")):
            check_sensitive(data.decode("utf-16"))
    else:
        check_sensitive(data.decode("utf-8-sig"))


def native_evidence_files(root):
    """Allowlist native capability receipts and archived scratch; never follow links."""
    root = Path(root)
    if execution_identity(read(root / "protocol.json"))[0] != "project-readonly":
        return []

    def no_links(path):
        require(path.resolve().is_relative_to(root.resolve()), "Native evidence escapes run")
        for entry in (path, *path.parents):
            if entry == root:
                break
            attributes = getattr(entry.lstat(), "st_file_attributes", 0)
            require(not entry.is_symlink() and not (attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT),
                    "Native evidence contains a link or reparse point")
            require(not entry.is_file() or entry.lstat().st_nlink == 1, "Native evidence contains a hardlink alias")

    def attempt_dirs(directory):
        if not directory.exists() and not directory.is_symlink():
            return
        no_links(directory)
        for group in sorted(directory.iterdir()):
            no_links(group)
            if group.is_dir():
                for attempt in sorted(group.iterdir()):
                    no_links(attempt)
                    if attempt.name.startswith("attempt-") and attempt.is_dir():
                        yield attempt

    def scratch_files(directory):
        if not directory.exists() and not directory.is_symlink():
            return
        no_links(directory)
        require(directory.is_dir(), "Archived scratch must be a directory")
        for entry in sorted(directory.iterdir()):
            no_links(entry)
            if entry.is_dir():
                yield from scratch_files(entry)
            else:
                require(entry.is_file(), "Unsupported archived scratch entry")
                yield entry

    files = []
    capability = root / "capability"
    if capability.exists() or capability.is_symlink():
        no_links(capability)
        seal = capability / "seal.json"
        if seal.is_file():
            no_links(seal)
            files.append(seal)
        # Recovery is a new derived record, not a replacement execution. Retain
        # its receipt and the exact original attempt alongside the repaired
        # canonical result, without admitting unrelated files into exports.
        for relative in ("recovery/receipt.json", *("recovery/source/" + name for name in
                         ("answer.txt", "result.json", "events.jsonl", "task-tree.json", "protocol.json",
                          "preparation-seal.json", "prices.json", "runtime.json", "preflight.json",
                          "permission-probe.json", "permission-probe-seal.json", "instruction-context.json"))):
            path = capability / relative
            if path.exists() or path.is_symlink():
                no_links(path)
                require(path.is_file(), "Capability recovery evidence must be a file")
                files.append(path)
        for directory in attempt_dirs(capability / "runs"):
            for name in ("answer.txt", "result.json", "events.jsonl", "task-tree.json"):
                path = directory / name
                if path.is_file():
                    no_links(path)
                    files.append(path)
            files.extend(scratch_files(directory / "scratch"))
    for directory in attempt_dirs(root / "runs"):
        files.extend(scratch_files(directory / "scratch"))
    return sorted(set(files))


def capability_summary(root):
    """Capability checks are paid evidence, separate from formal sample statistics."""
    root = Path(root)
    seal_path = root / "capability/seal.json"
    records = {path.relative_to(root / "capability").as_posix(): read(path) for path in native_evidence_files(root)
               if path.name == "result.json" and path.is_relative_to(root / "capability/runs")}
    if not seal_path.is_file() and not records:
        return None
    seal = read(seal_path) if seal_path.is_file() else {}
    if seal.get("passed") is True:
        from agentic_runtime import require_capability
        require_capability(root, seal.get("fingerprint", {}))
    def cost_values(selected, field):
        return [r[field] for r in selected if r.get(field) is not None]

    def cost_sum(values):
        return sum(values) if values else None

    complete = cost_values(records.values(), "tree_estimated_api_usd")
    observed = cost_values(records.values(), "tree_observed_api_usd")
    origins = seal.get("attempt_origins", {})
    historical_names = {name for name, record in records.items()
                        if origins.get(name, {}).get("kind") == "historical-paid-offline-replay"
                        or record.get("replay_provenance", {}).get("kind") == "historical-paid-offline-replay"}
    historical = [record for name, record in records.items() if name in historical_names]
    new = [record for name, record in records.items() if name not in historical_names]
    historical_complete = cost_values(historical, "tree_estimated_api_usd")
    historical_observed = cost_values(historical, "tree_observed_api_usd")
    new_complete = cost_values(new, "tree_estimated_api_usd")
    new_observed = cost_values(new, "tree_observed_api_usd")
    replay_receipt = root / "capability/recovery/receipt.json"
    replay_checks = seal.get("replay_checks", 1 if replay_receipt.is_file() else 0)
    local_new_attempts = 0 if seal.get("paid_here") is False else len(new)
    return {"capability_id": seal.get("capability_id"), "compatibility_id": seal.get("compatibility_id"),
            "passed": seal.get("passed", False), "paid_here": seal.get("paid_here"), "shared_from": seal.get("shared_from"),
            "attempts": seal.get("attempts", local_new_attempts), "archived_attempts": len(records),
            "verified_attempts": seal.get("verified_attempts", len(records)), "formal_samples": 0,
            "known_complete_cost_usd": cost_sum(complete), "complete_cost_attempts": len(complete),
            "known_observed_cost_usd": cost_sum(observed), "observed_cost_attempts": len(observed),
            "paid_ledger_attempts": len(records), "historical_paid_attempts": len(historical),
            "origin_new_paid_attempts": len(new), "new_paid_attempts": local_new_attempts,
            "replay_checks": replay_checks, "replay_inference_calls": 0, "replay_added_api_usd": 0.0,
            "historical_known_complete_cost_usd": cost_sum(historical_complete) if historical else 0.0,
            "historical_complete_cost_attempts": len(historical_complete),
            "historical_known_observed_cost_usd": cost_sum(historical_observed) if historical else 0.0,
            "new_known_complete_cost_usd": cost_sum(new_complete) if new else 0.0, "new_complete_cost_attempts": len(new_complete),
            "new_known_observed_cost_usd": cost_sum(new_observed) if new else 0.0,
            "local_known_complete_cost_usd": 0.0 if not local_new_attempts else cost_sum(new_complete),
            "local_known_observed_cost_usd": 0.0 if not local_new_attempts else cost_sum(new_observed),
            "actual_gateway_charge": None,
            "cost_scope": "Separate capability verification; not included in formal sample costs. Canonical paid attempts are counted once; offline replay adds no inference cost. Shared capability_id is counted once. Gateway charge is unknown."}


def speed_mode(tier):
    return {"default": "Standard", "priority": "Fast"}[tier]


def condition_label(condition, separator=" · "):
    parts = [condition["model"], condition["effort"]]
    if "service_tier" in condition:
        parts.append(speed_mode(condition["service_tier"]))
    return separator.join(parts)


def tier_comparisons(cells, weights):
    """Compare group medians, without implying per-repetition paired samples."""
    indexed = {(c["model"], c["effort"], c["service_tier"]): c for c in cells}
    pairs = []
    for model, effort in dict.fromkeys((c["model"], c["effort"]) for c in cells):
        standard = indexed.get((model, effort, "default"))
        fast = indexed.get((model, effort, "priority"))
        if standard is None or fast is None:
            continue
        comparisons = {}
        for key in [*weights, "quality", "elapsed_s", "ttft_visible_s", "visible_decode_tps", "output_tokens", "estimated_api_usd"]:
            a, b = standard["statistics"][key], fast["statistics"][key]
            known = a["median"] is not None and b["median"] is not None
            comparisons[key] = {"default": {"n": a["n"], "median": a["median"]},
                                "priority": {"n": b["n"], "median": b["median"]},
                                "fast_minus_standard": b["median"] - a["median"] if known else None,
                                "fast_over_standard": b["median"] / a["median"] if known and a["median"] else None}
        pairs.append({"model": model, "effort": effort, "default_valid_completed": standard["valid_completed"],
                      "priority_valid_completed": fast["valid_completed"], "metrics": comparisons})
    return pairs


def idle(root):
    root = Path(root)
    lock = root / "runner-lock.json"
    require(not lock.exists() or not still_running(read(lock)), "Runner is still active")
    active = root / "inflight.json"
    require(not active.exists(), "Unresolved inflight attempt; inspect/recover it with resume before grading/reporting")


def settled_valid_answers(root, protocol):
    chosen = chosen_attempts(root)
    require(set(chosen) == {s["id"] for s in protocol["schedule"]}, "Sampling not settled; finish/resume before anonymizing")
    valid = {r["relative_result"]: r for r in chosen.values() if r.get("valid") and r.get("status") == "completed"}
    require(bool(valid), "No valid completed answers to grade")
    return valid


def check_packet(root):
    """Bind anonymous grading to the frozen inputs and every selected valid answer."""
    root = Path(root)
    protocol = check_inputs(root)
    valid = settled_valid_answers(root, protocol)
    packet = root / "grading/packet"
    mapping = read(root / "grading/private-mapping.json")
    require(isinstance(mapping, dict) and bool(mapping), "Invalid scoring mapping")
    require(all(isinstance(anon, str) and re.fullmatch(r"Q(?:[0-9a-f]{8}-)?[0-9]{3,}", anon) and isinstance(relative, str)
                for anon, relative in mapping.items()), "Invalid scoring mapping IDs/paths")
    require(len(set(mapping.values())) == len(mapping), "Duplicate answer in scoring mapping")
    require(set(mapping.values()) == set(valid), "Scoring mapping differs from current valid answers; use blind --refresh before scoring again")
    for name in ["prompt.txt", "gold.json"]:
        source = inside(root / "input", name)
        destination = inside(packet, name)
        require(destination.is_file() and sha(destination) == sha(source), "Scoring input differs from frozen input: " + name)
    if protocol.get("offline_regrade"):
        require((packet / "rubric.json").is_file() and sha(packet / "rubric.json") == sha(root / "evaluation/rubric.json"), "Scoring rubric differs from frozen revision")
    else:
        require(not (packet / "rubric.json").exists(), "Unexpected scoring rubric override")
    answers = packet / "answers"
    require(answers.is_dir() and {p.name for p in answers.iterdir()} == {anon + ".txt" for anon in mapping}, "Anonymous answer files differ from scoring mapping")
    for anon, relative in mapping.items():
        record = valid[relative]
        source = inside(root, relative).parent / "answer.txt"
        actual = sha(source)
        require(actual == record.get("answer_sha256"), "Raw answer differs from recorded hash")
        require(record.get("prompt_sha256") == protocol["input_hashes"]["prompt.txt"], "Recorded prompt differs from frozen input")
        destination = inside(answers, anon + ".txt")
        require(destination.is_file() and sha(destination) == actual, "Anonymous answer differs from original answer")
    return mapping


def blind(root, refresh=False):
    root = Path(root).resolve()
    p = check_inputs(root)
    idle(root)
    valid = settled_valid_answers(root, p)
    grading = root / "grading"
    packet = grading / "packet"
    mapping_path = grading / "private-mapping.json"
    archived = None
    if refresh:
        require(not (grading / "seal.json").exists(), "Scores already sealed; refresh cannot replace sealed grading")
        if grading.exists():
            # Both resolved endpoints must remain in this run before moving the
            # complete, unsealed packet; user grading work is never deleted.
            source = inside(root, "grading")
            stamp = re.sub(r"[^0-9TZ]", "", now())
            archived = inside(root, f"grading-history/{stamp}-{secrets.token_hex(4)}")
            archived.parent.mkdir(parents=True, exist_ok=True)
            require(not archived.exists(), "Archived grading destination already exists")
            source.rename(archived)
    if mapping_path.exists():
        mapping = check_packet(root)
        if (grading / "seal.json").exists():
            score_data(root)
        return {"packet": str(packet), "answers": len(mapping), "mapping_not_displayed": True, "archived_grading": None}
    else:
        require(not grading.exists() or not any(grading.iterdir()), "Incomplete scoring mapping/packet; use blind --refresh to retain it and create a fresh packet")
        order = list(valid)
        random.SystemRandom().shuffle(order)
        packet_id = secrets.token_hex(4)
        mapping = {f"Q{packet_id}-{i:03d}": relative for i, relative in enumerate(order, 1)}
        write(mapping_path, mapping)
    (packet / "answers").mkdir(parents=True, exist_ok=True)
    for name in ["prompt.txt", "gold.json"]:
        source, destination = root / "input" / name, packet / name
        if destination.exists():
            require(sha(source) == sha(destination), "Scoring input changed")
        else:
            shutil.copyfile(source, destination)
    gold = scoring_gold(root)
    rubric = gold["rubric"]
    if p.get("offline_regrade"):
        shutil.copyfile(root / "evaluation/rubric.json", packet / "rubric.json")
    template = {}
    for anon, relative in mapping.items():
        source = inside(root, relative).parent / "answer.txt"
        destination = packet / "answers" / f"{anon}.txt"
        if destination.exists():
            require(sha(source) == sha(destination), "Anonymous answer changed")
        else:
            shutil.copyfile(source, destination)
        template[anon] = {"points": {r["id"]: None for r in rubric}, "notes": {r["id"]: "" for r in rubric}}
        if gold.get("scoring_version", 1) == 2:
            template[anon].update(evidence={r["id"]: [] for r in rubric}, issues=[])
    if not (packet / "scores-template.json").exists():
        write(packet / "scores-template.json", template)
    (packet / "instructions.txt").write_text("只读取本 packet 的题面、gold 与匿名答案。逐项填写 points 和 notes；null 不是分数，不存在默认分数。评分前不读模型、性能、映射或运行结果。完成后用 seal 封存。这是 AI 辅助评分，不是人工偏好研究。\n", encoding="utf-8")
    if gold.get("scoring_version", 1) == 2:
        with (packet / "instructions.txt").open("a", encoding="utf-8") as f:
            f.write("V2：准确性50/任务覆盖20/边界洞察10/可读性20。若有rubric.json，以其中细则评分，gold保留原始事实。每项填写evidence原文摘录；遗漏可留空摘录但需注明。issues逐项区分error/omission及critical/minor，错误需原文。错误扣准确性、遗漏扣覆盖，不重复扣；不以篇幅或术语数量奖励广度。\n")
            if p.get("offline_regrade"):
                f.write("本次是事后补评，评分者此前见过模型身份。新编号与性能隐藏不能消除先验印象，不宣称完全独立盲评。\n")
    check_packet(root)
    return {"packet": str(packet), "answers": len(mapping), "mapping_not_displayed": True, "archived_grading": str(archived) if archived else None}


def seal(root, scores_path):
    root = Path(root)
    check_inputs(root)
    idle(root)
    grading = root / "grading"
    require(not (grading / "seal.json").exists(), "Scores already sealed")
    mapping = check_packet(root)
    scores = read(scores_path)
    gold = scoring_gold(root)
    validate_scores(scores, mapping, grading / "packet", gold)
    check_sensitive(json.dumps(scores, ensure_ascii=False))
    write(grading / "scores.json", scores)
    hashes = {p.relative_to(grading / "packet").as_posix(): sha(p) for p in (grading / "packet").rglob("*") if p.is_file() and p.name != "scores-template.json"}
    value = {"at": now(), "evaluator": "AI-assisted rubric scoring; model/effort and performance labels hidden. Not independent human preference research.",
             "scores_sha256": sha(grading / "scores.json"), "mapping_sha256": sha(grading / "private-mapping.json"), "packet_hashes": hashes, "answers_scored": len(scores)}
    if gold.get("scoring_version", 1) == 2:
        value.update(scoring_version=2, scoring_weights=scoring_weights(gold))
        if check_inputs(root).get("offline_regrade"):
            value["evaluator"] = read(root / "evaluation/manifest.json")["note"] + " AI-assisted, no new inference."
    write(grading / "seal.json", value)
    return {"sealed_answers": len(scores)}


def score_data(root, partial=False):
    root = Path(root)
    grading = root / "grading"
    if not (grading / "seal.json").exists():
        require(partial, "Anonymous scores not sealed; use --partial only for an explicitly incomplete metrics report")
        if (grading / "private-mapping.json").exists():
            check_packet(root)
        return {}, {}
    seal_value = read(grading / "seal.json")
    require(sha(grading / "scores.json") == seal_value["scores_sha256"] and sha(grading / "private-mapping.json") == seal_value["mapping_sha256"], "Sealed scores or mapping changed")
    for name, expected in seal_value["packet_hashes"].items():
        require(sha(inside(grading / "packet", name)) == expected, "Scoring packet changed after sealing")
    mapping, scores = check_packet(root), read(grading / "scores.json")
    gold = scoring_gold(root)
    rubric = validate_scores(scores, mapping, grading / "packet", gold)
    weights = scoring_weights(gold)
    if gold.get("scoring_version", 1) == 2:
        require(seal_value.get("scoring_version") == 2 and seal_value.get("scoring_weights") == weights, "Sealed scoring version/weights differ")
    by_result = {}
    for anon, relative in mapping.items():
        points = scores[anon]["points"]
        values = {dimension: sum(points[r["id"]] for r in rubric if r["dimension"] == dimension) for dimension in weights}
        by_result[relative] = values | {"quality": sum(values.values()), "anonymous_id": anon}
        if gold.get("scoring_version", 1) == 2:
            issues = scores[anon]["issues"]
            by_result[relative].update(critical_errors=sum(x["kind"] == "error" and x["severity"] == "critical" for x in issues),
                                      errors=sum(x["kind"] == "error" for x in issues), omissions=sum(x["kind"] == "omission" for x in issues))
    return by_result, scores


def summarize(root, partial=False):
    root = Path(root)
    p = check_inputs(root)
    scores, detailed_scores = score_data(root, partial)
    gold = scoring_gold(root)
    version, weights = gold.get("scoring_version", 1), scoring_weights(gold)
    tiered, metric_names = "service_tiers" in p, metric_keys(p)
    agentic = execution_identity(p)[0] == "project-readonly"
    if agentic:
        metric_names = list(dict.fromkeys([*metric_names, *AGENTIC_METRICS]))
    cost_key = "estimated_api_usd" if tiered else "estimated_standard_api_usd"
    attempts, chosen = all_attempts(root), chosen_attempts(root)
    rows, answers = [], []
    for r in attempts:
        row = {k: r.get(k) for k in ["slot_id", "attempt", "model", "effort", "repetition", "status", "valid", "actual_gateway_charge", *metric_names]}
        if agentic:
            row.update({k: r.get(k) for k in AGENTIC_FLAGS})
        if tiered:
            row.update(service_tier=r["service_tier"], speed_mode=speed_mode(r["service_tier"]))
        row["selected"] = chosen.get(r["slot_id"], {}).get("relative_result") == r["relative_result"]
        row.update(scores.get(r["relative_result"], {}))
        if version == 2:
            row.update(scoring_version=2, **{k + "_max": v for k, v in weights.items()})
        row["answer_link"] = (Path(r["relative_result"]).parent / "answer.txt").as_posix()
        rows.append(row)
        answers.append({"label": condition_label(row) + f" · r{r['repetition']} · attempt {r['attempt']} ({r['status']})", "text": inside(root, row["answer_link"]).read_text(encoding="utf-8"), "link": row["answer_link"]})
        if tiered:
            answers[-1].update({k: row[k] for k in ["model", "effort", "service_tier", "speed_mode"]})
    cells = []
    for model in p["models"]:
        for effort in p["efforts"]:
            for tier in protocol_tiers(p):
                all_rows = [r for r in rows if r["model"] == model and r["effort"] == effort and (not tiered or r["service_tier"] == tier)]
                selected = [r for r in all_rows if r["selected"]]
                good = [r for r in selected if r["valid"] and r["status"] == "completed"]
                cells.append({"model": model, "effort": effort, "planned": p["repetitions"], "settled": len(selected), "valid_completed": len(good), "attempts": len(all_rows), "failed_attempts": sum(not r["valid"] for r in all_rows),
                              "statistics": {k: distribution([r.get(k) for r in good]) for k in metric_names + list(weights) + ["quality"]}})
                if tiered:
                    cells[-1].update(service_tier=tier, speed_mode=speed_mode(tier), missing=p["repetitions"] - len(selected))
                if version == 2:
                    cells[-1].update(critical_errors=sum(r.get("critical_errors", 0) for r in good),
                                     error_answers=sum(r.get("errors", 0) > 0 for r in good), omission_answers=sum(r.get("omissions", 0) > 0 for r in good))
                if agentic:
                    observed = [r for r in good if isinstance(r.get("delegation_occurred"), bool)]
                    cells[-1].update(delegation_observed_answers=len(observed),
                                     delegated_answers=sum(r["delegation_occurred"] for r in observed),
                                     delegation_rate=sum(r["delegation_occurred"] for r in observed) / len(observed) if observed else None,
                                     accounting_complete_attempts=sum(r.get("accounting_complete") is True for r in all_rows),
                                     known_observed_cost_usd=sum(r["tree_observed_api_usd"] for r in all_rows if r.get("tree_observed_api_usd") is not None),
                                     observed_cost_coverage_attempts=sum(r.get("tree_observed_api_usd") is not None for r in all_rows))
    success = sum(c["valid_completed"] for c in cells)
    result = {"schema_version": 1, "generated_at": now(), "title": p["title"], "case_id": p["case_id"], "project": p["project"], "batch": root.name, "parent": p.get("parent"),
            "planned": len(p["schedule"]), "settled": len(chosen), "valid_completed": success, "attempt_count": len(attempts), "all_planned_successful": success == len(p["schedule"]),
            "scores_sealed": (root / "grading/seal.json").exists(), "scope": p["scope"], "conditions": cells, "rows": rows, "answers": answers,
            "known_cost_usd": sum(r[cost_key] for r in rows if r.get(cost_key) is not None), "cost_coverage_attempts": sum(r.get(cost_key) is not None for r in rows),
            "observed_total_elapsed_s": sum(r["elapsed_s"] for r in rows if r.get("elapsed_s") is not None)}
    if agentic:
        result.update(execution_mode="project-readonly", execution_profile_id=execution_identity(p, runtime_fingerprint(root))[1],
                      capability_verification=capability_summary(root),
                      accounting_complete_attempts=sum(r.get("accounting_complete") is True for r in rows),
                      known_observed_cost_usd=sum(r["tree_observed_api_usd"] for r in rows if r.get("tree_observed_api_usd") is not None),
                      observed_cost_coverage_attempts=sum(r.get("tree_observed_api_usd") is not None for r in rows))
    if tiered:
        result.update(schema_version=2, service_tiers=protocol_tiers(p), tier_comparisons=tier_comparisons(cells, weights))
    if version == 2:
        result.update(schema_version=2, scoring_version=2, scoring_weights=weights,
                      grading_details={row["answer_link"]: detailed_scores[row["anonymous_id"]] for row in rows if row.get("anonymous_id")})
        if p.get("offline_regrade"):
            manifest = read(root / "evaluation/manifest.json")
            previous = read(root / "evaluation/previous-summary.json")
            result["evaluation"] = {k: manifest[k] for k in ["mode", "source_batch", "post_hoc", "evaluator_previously_saw_model_identity", "note"]}
            if manifest.get("final_answer_repair"):
                result["evaluation"]["final_answer_repair"] = manifest["final_answer_repair"]
                result["evaluation"].update(new_inference_calls=0, costs_are_historical=True,
                                               historical_capability_verification=previous.get("capability_verification"))
            result["previous_evaluation"] = {k: previous.get(k) for k in ["batch", "scoring_version", "scoring_weights", "conditions", "rows"]}
            result["previous_evaluation"]["scoring_version"] = previous.get("scoring_version", 1)
            result["previous_evaluation"]["scoring_weights"] = previous.get("scoring_weights", {"accuracy": 70, "readability": 30})
            old_rows = {r["answer_link"]: r for r in previous["rows"]}
            require(set(old_rows) == {r["answer_link"] for r in rows}, "Regrade differs from source attempt set")
            repair_changes = {}
            if manifest.get("final_answer_repair"):
                from final_repair import FINAL_TEXT_METRICS
                provenance_name = manifest["final_answer_repair"]["provenance"]
                require(provenance_name in manifest["files_sha256"], "Final repair provenance is not sealed")
                provenance = read(inside(root, provenance_name))
                for entry in provenance["attempts"]:
                    name = str(Path(entry["relative_result"]).with_name("answer.txt")).replace("\\", "/")
                    require(name not in repair_changes and name in old_rows, "Final repair attempt set differs")
                    require(set(entry["changed_metrics"]) <= FINAL_TEXT_METRICS, "Final repair changed a historical metric")
                    repair_changes[name] = entry["changed_metrics"]
            for row in rows:
                old = old_rows[row["answer_link"]]
                condition_keys = ["slot_id", "attempt", "model", "effort", "repetition", "status", "valid", "selected", "actual_gateway_charge"] + (["service_tier", "speed_mode"] if tiered else [])
                changes = repair_changes.get(row["answer_link"], {})
                for key in metric_names + condition_keys:
                    if key in changes:
                        require(old.get(key) == changes[key]["before"] and row.get(key) == changes[key]["after"],
                                "Final repair metric differs from sealed provenance: " + key)
                    else:
                        require(row.get(key) == old.get(key), "Regrade metrics/conditions differ from original: " + key)
    return result


def markdown(text):
    """Small escaped renderer: evidence never becomes executable HTML."""
    def inline(s):
        s = html.escape(s)
        s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
        return re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
    output, code, table = [], None, False
    fence_char, fence_length = None, 0
    for line in text.splitlines():
        fence = re.match(r"^\s*(`{3,}|~{3,})(.*)$", line)
        if fence and code is None:
            code, fence_char, fence_length = [], fence[1][0], len(fence[1])
            continue
        if code is not None:
            if fence and fence[1][0] == fence_char and len(fence[1]) >= fence_length and not fence[2].strip():
                output.append("<pre><code>" + html.escape("\n".join(code)) + "</code></pre>")
                code = None
            else:
                code.append(line)
            continue
        if line.strip().startswith("|") and line.strip().endswith("|"):
            if not table:
                output.append('<div class="tablewrap"><table>')
                table = True
            if not re.fullmatch(r"[|\s:\-]+", line):
                output.append("<tr>" + "".join("<td>" + inline(x.strip()) + "</td>" for x in line.strip().strip("|").split("|")) + "</tr>")
            continue
        if table:
            output.append("</table></div>")
            table = False
        heading = re.match(r"^#{1,6}\s+(.+)", line)
        if heading:
            output.append("<h3>" + inline(heading[1]) + "</h3>")
        elif line.strip():
            output.append("<p>" + inline(line) + "</p>")
    if code is not None:
        output.append("<pre>" + html.escape("\n".join(code)) + "</pre>")
    if table:
        output.append("</table></div>")
    return "\n".join(output)


def fmt(x, digits=2):
    return "—" if x is None else f"{x:,.{digits}f}"


def condition_table(cells, weights=None):
    if weights is None:
        weights = {"accuracy": 50, "coverage": 20, "insight": 10, "readability": 20} if cells and "coverage" in cells[0]["statistics"] else {"accuracy": 70, "readability": 30}
    heads = ["组合", "有效/计划", "尝试/失败", *[DIMENSION_LABELS[k] + " /" + str(v) for k, v in weights.items()], "总分 /100", "耗时中位 s", "TTFT 中位 s", "可见 TPS 中位", "输出 token 均值", "费用均值 USD"]
    if "coverage" in weights:
        heads += ["关键错误数", "有错误/遗漏答案数"]
    if cells and "service_tier" in cells[0]:
        heads += ["缺失"]
    body = []
    for c in cells:
        s = c["statistics"]
        cost_key = "estimated_api_usd" if "service_tier" in c else "estimated_standard_api_usd"
        values = [html.escape(condition_label(c)), f"{c['valid_completed']}/{c['planned']}", f"{c['attempts']}/{c['failed_attempts']}", *[fmt(s[k]["mean"]) for k in list(weights) + ["quality"]], *[fmt(s[k]["median"]) for k in ["elapsed_s", "ttft_visible_s", "visible_decode_tps"]], fmt(s["output_tokens"]["mean"], 0), fmt(s[cost_key]["mean"], 6)]
        if "coverage" in weights:
            values += [str(c.get("critical_errors", 0)), f"{c.get('error_answers', 0)}/{c.get('omission_answers', 0)}"]
        if "service_tier" in c:
            values += [str(c["missing"])]
        body.append("<tr>" + "".join("<td>" + x + "</td>" for x in values) + "</tr>")
    return '<div class="tablewrap"><table><thead><tr>' + "".join("<th>"+h+"</th>" for h in heads) + "</tr></thead><tbody>" + "".join(body) + "</tbody></table></div>"


def render_csv(rows, version=1, service_tiers=None):
    output = io.StringIO(newline="")
    fields = CSV_V2_FIELDS if version == 2 else CSV_FIELDS
    if service_tiers is not None:
        fields = ["estimated_api_usd" if k == "estimated_standard_api_usd" else k for k in fields]
        fields = fields[:4] + ["service_tier", "speed_mode"] + fields[4:]
    if any("accounting_complete" in row for row in rows):
        fields = list(dict.fromkeys([*fields, *AGENTIC_METRICS, *AGENTIC_FLAGS]))
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def tier_comparison_html(data):
    pairs = data.get("tier_comparisons", [])
    if not pairs:
        return ""
    labels = {"quality": "总分", "elapsed_s": "耗时 s", "ttft_visible_s": "TTFT s", "visible_decode_tps": "可见 TPS", "output_tokens": "输出 token", "estimated_api_usd": "费用 USD"}
    rows = []
    for pair in pairs:
        for key, label in labels.items():
            metric = pair["metrics"][key]
            values = [condition_label(pair), label, str(metric["default"]["n"]), fmt(metric["default"]["median"], 6 if key == "estimated_api_usd" else 2),
                      str(metric["priority"]["n"]), fmt(metric["priority"]["median"], 6 if key == "estimated_api_usd" else 2),
                      fmt(metric["fast_minus_standard"], 6 if key == "estimated_api_usd" else 2), fmt(metric["fast_over_standard"])]
            rows.append("<tr>" + "".join("<td>" + html.escape(v) + "</td>" for v in values) + "</tr>")
    heads = ["模型 / 思考强度", "指标", "Standard n", "Standard 中位数", "Fast n", "Fast 中位数", "Fast − Standard", "Fast / Standard"]
    return '<section><h2>同模型同思考强度：Standard / Fast</h2><p class="note">比较各组中位数，分别展示实际 n；不是按轮次配对的因果估计。缺值或 Standard 中位数为零时，倍率为 —。</p><div class="tablewrap"><table><thead><tr>' + "".join("<th>" + x + "</th>" for x in heads) + "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div></section>"


def agentic_html(data):
    if data.get("execution_mode") != "project-readonly":
        return ""
    heads = ["组合", "委派答案/已观测", "子代理数中位", "答案就绪 s", "全任务 s", "首次进度 s", "正文首字 s",
             "主/子/全树输出 token 中位", "根/全树端到端输出 token/s 中位", "主/子/全树费用均值 USD", "计量完整尝试", "已观测费用合计 USD"]
    rows = []
    for cell in data["conditions"]:
        stats = cell["statistics"]
        median = lambda key: fmt(stats.get(key, {}).get("median"))
        values = [condition_label(cell), f"{cell['delegated_answers']}/{cell['delegation_observed_answers']}",
                  median("delegation_count"), *[median(k) for k in ("answer_ready_s", "workflow_elapsed_s", "first_progress_s", "final_ttft_s")],
                  " / ".join(fmt(stats.get(p + "output_tokens", {}).get("median"), 0) for p in ("root_", "child_", "tree_")),
                  " / ".join(median(k) for k in ("root_output_e2e_tps", "tree_output_e2e_tps")),
                  " / ".join(fmt(stats.get(p + "estimated_api_usd", {}).get("mean"), 6) for p in ("root_", "child_", "tree_")),
                  f"{cell['accounting_complete_attempts']}/{cell['attempts']}",
                  fmt(cell["known_observed_cost_usd"] if cell["observed_cost_coverage_attempts"] else None, 6)]
        rows.append("<tr>" + "".join("<td>" + html.escape(v) + "</td>" for v in values) + "</tr>")
    capability = data.get("capability_verification")
    note = ""
    if capability:
        shared = "复用同一能力验收记录；本批新增能力验收费用为零，下列费用属于历史验收。" if capability.get("paid_here") is False else "能力验收费用与正式样本分列。"
        note = '<p class="note">能力验收：本批 ' + html.escape(str(capability["attempts"])) + ' 次，留档 ' + html.escape(str(capability["archived_attempts"])) + ' 次。' + shared + '完整费用可估 ' + str(capability["complete_cost_attempts"]) + '/' + str(capability["archived_attempts"]) + '，已知合计 USD ' + fmt(capability["known_complete_cost_usd"], 6) + '；部分已观测费用 USD ' + fmt(capability["known_observed_cost_usd"], 6) + '。不计入正式样本统计；相同 capability_id 只计一次。</p>'
        if capability.get("replay_checks"):
            note += '<p class="note">恢复账本：历史付费尝试 ' + str(capability["historical_paid_attempts"]) + ' 次，经零调用离线复核 ' + str(capability["replay_checks"]) + ' 次；复核新增推理费用 USD 0。历史尝试的修复后完整费用估算 USD ' + fmt(capability["historical_known_complete_cost_usd"], 7) + '。原失败记录随档保留，不再计第二笔费用。本批新增付费验收 ' + str(capability["new_paid_attempts"]) + ' 次，本批已知完整费用估算 USD ' + fmt(capability["local_known_complete_cost_usd"], 7) + '；付费账本共 ' + str(capability["paid_ledger_attempts"]) + ' 次。实际网关扣费未知。</p>'
    return '<section><h2>原生代理工作流与全树消耗</h2><p class="note">零委派是有效观测。主代理与子孙代理分别计量；费用按各响应实际模型与速度档估算。已观测费用包含失败尝试，只是已知部分，不等于完整总费用。正文 TPS 不使用全树输出量。</p>' + note + '<div class="tablewrap"><table><thead><tr>' + "".join("<th>" + x + "</th>" for x in heads) + '</tr></thead><tbody>' + "".join(rows) + '</tbody></table></div></section>'


def report_context(root):
    root = Path(root)
    assessment = read(root / "assessment.json") if (root / "assessment.json").exists() else {"conclusion": "尚未填写本题的模型选择建议。请结合下表、事实错误与答案阅读质量评估。", "evidence": [], "limitations": []}
    return {"protocol": read(root / "protocol.json"), "prices": read(root / "prices.json"), "assessment": assessment,
            "parent": read(root / "parent-summary.json") if (root / "parent-summary.json").exists() else None,
            "fingerprint": read(root / "runtime.json").get("fingerprint") if (root / "runtime.json").exists() else None,
            "prompt": (root / "input/prompt.txt").read_text(encoding="utf-8"), "gold": read(root / "input/gold.json"), "scoring_gold": scoring_gold(root)}


def evaluation_html(data, context):
    """Versioned grading audit; original V1 rendering stays byte-compatible."""
    if data.get("scoring_version", 1) != 2:
        return "", "", ""
    intro = '<p class="note">评分 V2：准确性50 / 任务覆盖20 / 边界洞察10 / 可读性20。关键事实错误优先于综合分审查；广度不按篇幅、术语数量或推理token计分。</p>'
    if data.get("evaluation"):
        intro += '<p class="note">' + html.escape(data["evaluation"]["note"]) + '</p>'
        historical = data["evaluation"].get("historical_capability_verification")
        if historical:
            intro += '<p class="note">原批次历史能力验收：' + str(historical["archived_attempts"]) + ' 次，完整费用估算 USD ' + fmt(historical["known_complete_cost_usd"], 6) + '。本次离线修订新增模型采样 0 次、新增推理费用 USD 0；历史验收费用与正式样本费用分列，不重复计费。</p>'
    comparison = ""
    if data.get("previous_evaluation"):
        old = data["previous_evaluation"]
        if data.get("evaluation", {}).get("final_answer_repair"):
            comparison = '<h2>提取修复前评分（单独展示）</h2><p>本次依据原生 turn/completed 恢复最终正文，按修订目录封存的细则重新匿名评阅。分差可能来自文本恢复、评分细则与重新评阅，不能据此推断模型能力变化。原评分版本 ' + str(old["scoring_version"]) + '</p>'
        else:
            comparison = '<h2>原评分（版本不同，单独展示）</h2><p>同一批答案；分数变化来自评分口径，不是模型能力变化。原评分版本 ' + str(old["scoring_version"]) + '</p>'
        comparison += condition_table(old["conditions"], old["scoring_weights"])
        old_rows = {r["answer_link"]: r for r in old["rows"]}
        comparison += '<details><summary>逐份新旧总分对照</summary><div class="tablewrap"><table><tr><th>组合/轮次/尝试</th><th>原总分</th><th>V2总分</th></tr>'
        for r in data["rows"]:
            label = condition_label(r, "/") + f"/r{r['repetition']}/a{r['attempt']}"
            comparison += '<tr><td>'+html.escape(label)+'</td><td>'+fmt(old_rows[r["answer_link"]].get("quality"))+'</td><td>'+fmt(r.get("quality"))+'</td></tr>'
        comparison += '</table></div></details>'
    audit = '<details><summary>V2细则、逐项评分依据与错误/遗漏</summary><pre>' + html.escape(json.dumps(context["scoring_gold"], ensure_ascii=False, indent=2)) + '</pre>'
    for row in data["rows"]:
        score = data.get("grading_details", {}).get(row["answer_link"])
        if score is None:
            continue
        dimensions = " · ".join(f"{DIMENSION_LABELS[key]} {row[key]}/{weight}" for key, weight in data["scoring_weights"].items())
        audit += '<details><summary>' + html.escape(condition_label(row, "/") + f"/r{row['repetition']} · {dimensions} · 总分 {row['quality']}/100 · 关键错误 {row['critical_errors']}") + '</summary>'
        for item in context["scoring_gold"]["rubric"]:
            key = item["id"]
            audit += '<p><strong>' + html.escape(f"{key} · {DIMENSION_LABELS[item['dimension']]} {score['points'][key]}/{item['max']}") + '</strong> ' + html.escape(score["notes"][key]) + '</p>'
            for q in score["evidence"][key]:
                audit += '<blockquote>' + html.escape(q) + '</blockquote>'
        for issue in score["issues"]:
            audit += '<p>' + html.escape(f"{issue['kind']}/{issue['severity']} · {issue['criterion']}：{issue['note']}") + '</p>'
            if issue.get("quote"):
                audit += '<blockquote>' + html.escape(issue["quote"]) + '</blockquote>'
        audit += '</details>'
    return intro, comparison, audit + '</details>'


def render_html(data, context):
    """Render only supplied data; file and clock access belongs to the caller."""
    p, prices, assessment = context["protocol"], context["prices"], context["assessment"]
    require(isinstance(assessment.get("conclusion"), str) and isinstance(assessment.get("evidence", []), list) and isinstance(assessment.get("limitations", []), list), "Invalid assessment format")
    check_sensitive(json.dumps(assessment, ensure_ascii=False))
    detail = []
    for c in data["conditions"]:
        for name, stat in c["statistics"].items():
            detail.append("<tr>" + "".join("<td>"+html.escape(str(v))+"</td>" for v in [condition_label(c, "/"), name, stat["n"], *[fmt(stat[k], 6) for k in ["mean", "median", "sd", "min", "max"]]]) + "</tr>")
    comparison = ""
    if context["parent"] is not None:
        parent = context["parent"]
        comparison = "<h2>上一批次（单独展示，不合并）</h2><p>"+html.escape(parent["batch"])+"</p>"+condition_table(parent["conditions"])
        oldfp = parent.get("fingerprint")
        currentfp = context["fingerprint"]
        if oldfp != currentfp:
            comparison += '<p class="note">两批运行环境指纹不同，不能把全部变化归因于模型波动。</p>'
    payload = json.dumps({"answers": [{**a, "html": markdown(a["text"])} for a in data["answers"]], "cells": data["conditions"]}, ensure_ascii=False).replace("<", "\\u003c").replace("&", "\\u0026")
    evidence = "".join("<li>"+html.escape(str(x))+"</li>" for x in assessment.get("evidence", []))
    limits = ["单题、小样本；AI 评分，不是人工偏好研究。", "同一固定证据，不衡量搜索、工具使用、代码修改或自动委派。", "TPS = 可见输出 token / 首末非空可见 delta 间隔，受客户端缓冲影响，不是纯后端解码速率。", "总耗时从 turn/start 发出到 turn/completed 收到，含排队、网络、思考和输出；不含建会话和评分。", "费用按官方快照估算，不等于自建网关扣费；无缓存口径保留同次输出用量，仅是输入计费对照。", "缺失值为 —；统计 n 可能小于有效答案数，费用总计只覆盖已知项。"] + assessment.get("limitations", [])
    if data.get("execution_mode") == "project-readonly":
        limits[1:4] = ["在冻结工程源码及相同可用工具下进行只读分析，保留原生委派策略；不评价业务代码修改。",
                       "答案就绪与全任务结束分别计时；正文首字与首次进度分别报告。根输出 TPS 仅在用量可归属时提供，不能把全树输出除以根答案时长。",
                       "全树用量按唯一响应去重；未收齐响应或未知价格保留未知，已观测消耗单列。"]
    limit_html = "".join("<li>"+html.escape(str(x))+"</li>" for x in limits)
    raw = "".join('<details><summary>'+html.escape(a["label"])+'</summary><a href="'+quote(a["link"], safe="/")+'">原始答案</a><article>'+markdown(a["text"])+'</article></details>' for a in data["answers"])
    page = '''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>工程模型 Benchmark</title><style>
    :root{color-scheme:light dark;--bg:#f4f5f7;--fg:#24303a;--panel:#fff;--line:#cfd5db;--accent:#176d73}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.7 system-ui,"Microsoft YaHei",sans-serif}main{max-width:1400px;margin:auto;padding:30px 24px 70px}h1{font-size:29px}h2{font-size:22px;margin-top:32px}h3{font-size:18px}section,details{background:var(--panel);padding:20px;margin:18px 0;border-radius:10px}table{border-collapse:collapse;width:100%;font-size:13px;font-variant-numeric:tabular-nums}th,td{padding:10px;text-align:left;border-bottom:1px solid var(--line);vertical-align:top}.tablewrap{overflow:auto}th{white-space:nowrap}a{color:var(--accent)}.note{font-size:14px;color:#66717b}.compare{display:grid;grid-template-columns:1fr 1fr;gap:24px}.compare>div{min-width:0}select{width:100%;padding:10px;margin-bottom:12px;background:var(--panel);color:var(--fg);border:1px solid var(--line)}article{overflow-wrap:anywhere}article p{margin:9px 0}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:var(--bg);padding:14px;font-size:13px}summary{cursor:pointer;font-weight:600}code{font-family:Consolas,monospace}svg{width:100%;height:auto}#chart{overflow-x:auto}#chart svg{min-width:900px}svg text{fill:var(--fg);font:14px system-ui}.badge{display:inline-block;background:var(--panel);padding:5px 12px;border-radius:14px;margin:0 10px 10px 0}@media(prefers-color-scheme:dark){:root{--bg:#17202a;--fg:#e5e9ef;--panel:#222e3a;--line:#455563;--accent:#74c6cd}.note{color:#b2bdc8}}@media(max-width:850px){.compare{grid-template-columns:1fr}main{padding:20px 12px}h1{font-size:24px}}@media print{details{display:block}select{display:none}}
    </style></head><body><main><h1>@@TITLE@@</h1><p class="note">@@BATCH@@ · @@PROJECT@@</p><span class="badge">有效 @@VALID@@ / 计划 @@PLANNED@@</span><span class="badge">尝试 @@ATTEMPTS@@</span><span class="badge">@@STATUS@@</span><section><h2>本题选择建议</h2><p>@@CONCLUSION@@</p><ul>@@EVIDENCE@@</ul></section><h2>本批次结果</h2>@@TABLE@@<p class="note">观察总耗时 @@TIME@@ s；已知费用合计 $@@COST@@，覆盖 @@COVERAGE@@ 次尝试。价格日期 @@PRICE_DATE@@。</p><section><h2>耗时与范围</h2><div id="chart"></div><p class="note">点为中位数，横线为最小至最大；保留所有有效样本。</p></section>@@PARENT@@<section><h2>答案并排阅读</h2><div class="compare"><div><label for="left">左侧答案</label><select id="left"></select><article id="left-answer"></article></div><div><label for="right">右侧答案</label><select id="right"></select><article id="right-answer"></article></div></div></section><details><summary>每项指标分布与实际 n</summary><div class="tablewrap"><table><thead><tr><th>组合</th><th>指标</th><th>n</th><th>均值</th><th>中位数</th><th>标准差</th><th>最小</th><th>最大</th></tr></thead><tbody>@@DETAIL@@</tbody></table></div></details><details><summary>题面、证据和标准答案</summary>@@PROMPT@@<pre>@@GOLD@@</pre><pre>@@HASHES@@</pre></details><details><summary>逐次答案与失败记录</summary>@@RAW@@</details><section><h2>口径与限制</h2><ul>@@LIMITS@@</ul><p><a href="metrics.csv">CSV</a> · <a href="summary.json">完整 JSON</a> · <a href="validation.json">校验记录</a></p><pre>@@PRICES@@</pre></section></main><script id="data" type="application/json">@@DATA@@</script><script>
    const d=JSON.parse(document.getElementById('data').textContent);for(const side of ['left','right']){const s=document.getElementById(side);d.answers.forEach((a,i)=>{const o=document.createElement('option');o.value=i;o.textContent=a.label;s.append(o)});s.selectedIndex=side==='right'&&d.answers.length>1?1:0;function show(){document.getElementById(side+'-answer').innerHTML=d.answers[s.value]?.html||'无答案'}s.addEventListener('change',show);show()}
    const ns='http://www.w3.org/2000/svg',svg=document.createElementNS(ns,'svg');svg.setAttribute('viewBox',`0 0 1050 ${d.cells.length*42+42}`);svg.setAttribute('role','img');svg.setAttribute('aria-label','各组合耗时中位数与最小最大范围');const max=Math.max(1,...d.cells.map(c=>c.statistics.elapsed_s.max||0));function el(tag,attrs,text){const n=document.createElementNS(ns,tag);Object.entries(attrs).forEach(([k,v])=>n.setAttribute(k,v));if(text)n.textContent=text;svg.append(n)}d.cells.forEach((c,i)=>{const y=i*42+25,s=c.statistics.elapsed_s,x=v=>265+(v||0)/max*670;el('text',{x:0,y},c.model+'/'+c.effort);if(s.n){el('line',{x1:x(s.min),x2:x(s.max),y1:y-5,y2:y-5,stroke:'#57979b','stroke-width':3});el('circle',{cx:x(s.median),cy:y-5,r:5,fill:'#bd702b'});el('text',{x:950,y},s.median.toFixed(1)+' s')}});document.getElementById('chart').append(svg);
    </script></body></html>'''
    values = {"TITLE": html.escape(data["title"]), "BATCH": html.escape(data["batch"]), "PROJECT": html.escape(data["project"].get("name", data["project"]["root"])), "VALID": str(data["valid_completed"]), "PLANNED": str(data["planned"]), "ATTEMPTS": str(data["attempt_count"]), "STATUS": ("全量成功" if data["scores_sealed"] else "采样完成，评分未封存") if data["all_planned_successful"] else "有失败或缺口", "CONCLUSION": html.escape(assessment["conclusion"]), "EVIDENCE": evidence, "TABLE": condition_table(data["conditions"]), "TIME": fmt(data["observed_total_elapsed_s"]), "COST": fmt(data["known_cost_usd"] if data["cost_coverage_attempts"] else None, 6), "COVERAGE": str(data["cost_coverage_attempts"]), "PRICE_DATE": html.escape(str(prices.get("as_of") or "未知")), "PARENT": comparison, "DETAIL": "".join(detail), "PROMPT": markdown(context["prompt"]), "GOLD": html.escape(json.dumps(context["gold"], ensure_ascii=False, indent=2)), "HASHES": html.escape(json.dumps(p["input_hashes"], indent=2)), "RAW": raw, "LIMITS": limit_html, "PRICES": html.escape(json.dumps(prices, ensure_ascii=False, indent=2)), "DATA": payload}
    intro, old_evaluation, audit = evaluation_html(data, context)
    values["TABLE"] = intro + values["TABLE"] + audit + agentic_html(data)
    values["PARENT"] += old_evaluation
    if "service_tiers" in p:
        values["TABLE"] += tier_comparison_html(data)
        filters = '<div class="compare"><div><label for="model-filter">答案模型筛选</label><select id="model-filter"></select><label for="effort-filter">答案思考强度筛选</label><select id="effort-filter"></select></div><div><label for="tier-filter">答案速度模式筛选</label><select id="tier-filter"></select></div></div>'
        page = page.replace('<h2>答案并排阅读</h2>', '<h2>答案并排阅读</h2>' + filters)
        script_start = page.index('    const d=JSON.parse(')
        chart_start = page.index("    const ns='http://www.w3.org/2000/svg'", script_start)
        selection_script = '''    const d=JSON.parse(document.getElementById('data').textContent);
    const controls=[['model-filter','model','全部模型'],['effort-filter','effort','全部思考强度'],['tier-filter','service_tier','全部速度模式']];let visible=d.answers;
    function show(side){const s=document.getElementById(side);document.getElementById(side+'-answer').innerHTML=visible[s.value]?.html||'无匹配答案'}
    function filterAnswers(){visible=d.answers.filter(a=>controls.every(([id,key])=>!document.getElementById(id).value||a[key]===document.getElementById(id).value));for(const side of ['left','right']){const s=document.getElementById(side);s.replaceChildren();visible.forEach((a,i)=>{const o=document.createElement('option');o.value=i;o.textContent=a.label;s.append(o)});s.selectedIndex=side==='right'&&visible.length>1?1:0;show(side)}}
    for(const [id,key,label] of controls){const s=document.getElementById(id);const all=document.createElement('option');all.value='';all.textContent=label;s.append(all);for(const value of [...new Set(d.answers.map(a=>a[key]))]){const o=document.createElement('option');o.value=value;o.textContent=key==='service_tier'?(value==='priority'?'Fast':'Standard'):value;s.append(o)}s.selectedIndex=0;s.addEventListener('change',filterAnswers)}for(const side of ['left','right'])document.getElementById(side).addEventListener('change',()=>show(side));filterAnswers();
'''
        page = page[:script_start] + selection_script + page[chart_start:]
        page = page.replace("c.model+'/'+c.effort);", "c.model+'/'+c.effort+'/'+c.speed_mode);")
    return re.sub(r"@@([A-Z_]+)@@", lambda m: values[m[1]], page)


def build_report(root, partial=False):
    root = Path(root)
    idle(root)
    data = summarize(root, partial)
    page = render_html(data, report_context(root))
    table = render_csv(data["rows"], data.get("scoring_version", 1), data.get("service_tiers"))
    write(root / "summary.json", data)
    (root / "metrics.csv").write_text(table, encoding="utf-8-sig", newline="")
    (root / "report.html").write_text(page, encoding="utf-8")
    return data


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links, self.ids = [], []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.ids.append(attrs["id"])
        if tag == "a" and "href" in attrs:
            self.links.append(attrs["href"])


def validate_records(root):
    """Read-only transport, answer, usage and metric checks shared by exporters."""
    root = Path(root)
    p = check_inputs(root)
    agentic_mode = execution_identity(p)[0] == "project-readonly"
    attempts, tids = all_attempts(root), set()
    schedule = {s["id"]: s for s in p["schedule"]}
    for r in attempts:
        require(r["slot_id"] in schedule, "Unplanned slot")
        slot = schedule[r["slot_id"]]
        require(r["model"] == slot["model"] and r["effort"] == slot["effort"] and r["repetition"] == slot["repetition"], "Result condition differs from schedule")
        tier = r.get("service_tier") if "service_tiers" in p else "default"
        if "service_tiers" in p:
            require(r.get("service_tier") == slot["service_tier"], "Result service tier differs from schedule")
        directory = inside(root, r["relative_result"]).parent
        answer = (directory / "answer.txt").read_text(encoding="utf-8")
        require(sha(directory / "answer.txt") == r["answer_sha256"] and r["prompt_sha256"] == sha(root / "input/prompt.txt"), "Answer/prompt hash mismatch")
        if r.get("thread_id"):
            require(r["thread_id"] not in tids, "Thread reused between attempts")
            tids.add(r["thread_id"])
        if agentic_mode:
            from agentic import TreeLedger
            snapshot = r.get("agentic")
            if snapshot is None:
                require(not r.get("valid") and not r.get("usage") and not any(r.get(k) is not None for k in AGENTIC_METRICS),
                        "Missing agentic ledger for reported work")
                recalculated = metrics_for_protocol(p, None, r.get("elapsed_s"), answer=answer)
            else:
                require(isinstance(snapshot, dict) and snapshot.get("schema_version") == 1, "Malformed task-tree ledger")
                ledger = TreeLedger.from_snapshot(snapshot)
                require(digest(snapshot) == digest(ledger.snapshot()), "Task-tree ledger differs from event replay")
                if (directory / "events.jsonl").is_file():
                    events = [json.loads(line) for line in (directory / "events.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
                    require(events == snapshot["events"], "Task-tree events differ from stored event ledger")
                if (directory / "task-tree.json").is_file():
                    require(read(directory / "task-tree.json") == snapshot, "Task-tree export differs from result")
                require(snapshot["root_thread_id"] == r.get("thread_id"), "Task-tree root differs from result")
                require(ledger.final_answer.replace("\r\n", "\n") == answer.replace("\r\n", "\n"), "Final answer differs from task-tree ledger")
                descendant_ids = set(ledger.related_thread_ids) - {r.get("thread_id")}
                require(not descendant_ids.intersection(tids), "Descendant thread reused between attempts")
                tids.update(descendant_ids)
                if r.get("valid"):
                    e = r["effective"]
                    require(r["status"] == "completed" and ledger.root_status == "completed" and ledger.tree_terminal
                            and snapshot.get("discovery_complete") is True, "Valid answer has unfinished task tree")
                    require(e["model"] == r["model"] and e["reasoningEffort"] == r["effort"] and e["serviceTier"] == tier,
                            "Invalid effective settings/status")
                    require(bool(answer.strip()) and not ledger.errors and not r.get("protocol_issues")
                            and not r.get("protocol_violations"), "Invalid final answer/protocol")
                    require((directory / "events.jsonl").is_file(), "Missing events for valid result")
                recalculated = ledger.metrics(read(root / "prices.json"), p, r.get("elapsed_s"))
                for key in (*AGENTIC_METRICS, *AGENTIC_FLAGS):
                    actual, expected = r.get(key), recalculated.get(key)
                    require((actual is expected) if expected is None or isinstance(expected, bool)
                            else actual is not None and math.isclose(actual, expected, rel_tol=1e-8, abs_tol=1e-7),
                            "Task-tree metric inconsistent: " + key)
        elif r.get("valid"):
            e = r["effective"]
            require(r["status"] == "completed" and e["model"] == r["model"] and e["reasoningEffort"] == r["effort"] and e["serviceTier"] == tier and not r.get("tool_items"), "Invalid effective settings/status/tools")
            require(not usage_issues(r.get("usage")), "Invalid reported usage")
            events_file = directory / "events.jsonl"
            require(events_file.exists(), "Missing events for valid result")
            events = [json.loads(line) for line in events_file.read_text(encoding="utf-8").splitlines()]
            ds = [e for e in events if e["method"] == "item/agentMessage/delta" and e.get("delta")]
            if ds:
                require("".join(e["delta"] for e in ds).replace("\r\n", "\n") == answer.replace("\r\n", "\n"), "Stream differs from answer")
                require(abs(ds[0]["t_s"]-r["first_delta_s"]) < 1e-4 and abs(ds[-1]["t_s"]-r["last_delta_s"]) < 1e-4, "Visible timing inconsistent")
        if not agentic_mode:
            recalculated = metrics_for_protocol(p, r.get("usage"), r.get("elapsed_s"), r.get("first_delta_s"), r.get("last_delta_s"), answer, price_for(read(root / "prices.json"), r["model"], tier))
        for k in metric_keys(p):
            a, b = r.get(k), recalculated[k]
            require(a is None and b is None or a is not None and b is not None and math.isclose(a, b, rel_tol=1e-8, abs_tol=1e-7), "Metric inconsistent: " + k)
        check_sensitive(json.dumps(r, ensure_ascii=False))
        check_sensitive(answer)
    return attempts


def validate(root, partial=False):
    root = Path(root)
    p = check_inputs(root)
    score_data(root, partial)
    attempts = validate_records(root)
    exports = [root / name for name in ["summary.json", "metrics.csv", "report.html"]]
    if any(path.exists() for path in exports):
        require(all(path.is_file() for path in exports), "Incomplete report artifacts; regenerate with report")
        actual, expected = read(root / "summary.json"), summarize(root, partial)
        require(isinstance(actual, dict) and {k: v for k, v in actual.items() if k != "generated_at"} == {k: v for k, v in expected.items() if k != "generated_at"}, "Summary differs from raw results")
        with (root / "metrics.csv").open(encoding="utf-8-sig", newline="") as f:
            csv_rows = list(csv.reader(f))
        expected_csv = list(csv.reader(io.StringIO(render_csv(expected["rows"], expected.get("scoring_version", 1), expected.get("service_tiers")), newline="")))
        require(csv_rows == expected_csv, "CSV differs from raw results: headers or rows")
        page = (root / "report.html").read_text(encoding="utf-8")
        require(page == render_html(expected, report_context(root)), "HTML differs from raw results; regenerate with report")
        parser = Links()
        parser.feed(page)
        require(len(parser.ids) == len(set(parser.ids)), "Duplicate HTML IDs")
        for link in parser.links:
            if not urlparse(link).scheme and not link.startswith("#"):
                require(inside(root, unquote(link)).is_file() or link == "validation.json", "Broken local report link")
        node = shutil.which("node")
        if node:
            checked = subprocess.run([node, str(Path(__file__).with_name("check_report.cjs")), str(root / "report.html")], capture_output=True, text=True, encoding="utf-8", timeout=30)
            require(checked.returncode == 0, "Offline report interaction check failed")
            write(root / "ui-check.json", json.loads(checked.stdout))
        else:
            write(root / "ui-check.json", {"passed": None, "reason": "Optional Node.js unavailable; HTML links/IDs checked by Python", "browser_visual_check": "not performed"})
    value = {"at": now(), "passed": True, "attempts_checked": len(attempts), "planned_slots": len(p["schedule"]), "scores_sealed": (root / "grading/seal.json").exists(), "partial_report": partial, "no_new_inference": True}
    write(root / "validation.json", value)
    return value


def archive(root):
    root = Path(root)
    if execution_identity(check_inputs(root))[0] == "project-readonly":
        capability_summary(root)
    files = []
    names = ["protocol.json", "prices.json", "runtime.json", "preflight.json", "instruction-context.json", "preparation-seal.json", "progress.json", "assessment.json", "summary.json", "metrics.csv", "report.html", "validation.json", "ui-check.json", "parent-summary.json", "permission-probe.json", "permission-probe-seal.json"]
    files.extend(root / n for n in names if (root / n).is_file())
    files.extend(p for p in (root / "input").rglob("*") if p.is_file())
    if check_inputs(root).get("offline_regrade"):
        manifest = read(root / "evaluation/manifest.json")
        files.append(root / "evaluation/manifest.json")
        files.extend(inside(root, name) for name in manifest["files_sha256"] if name.startswith("evaluation/"))
    files.extend(p for p in (root / "runs").glob("*/attempt-*/*") if p.name in ["answer.txt", "result.json", "events.jsonl", "task-tree.json"] and p.is_file())
    files.extend(native_evidence_files(root))
    files.extend(root / "grading" / n for n in ["private-mapping.json", "scores.json", "seal.json"] if (root / "grading" / n).is_file())
    files.extend(p for p in (root / "grading/packet").rglob("*") if p.is_file() and p.name != "scores-template.json")
    # Retain superseded unsealed packets, including unfinished scorer notes and
    # templates, without adding unrelated files from the run directory.
    for previous in sorted((root / "grading-history").glob("*")):
        files.extend(previous / n for n in ["private-mapping.json", "scores.json"] if (previous / n).is_file())
        files.extend(p for p in (previous / "packet").rglob("*") if p.is_file())
    for path in files:
        require(path.resolve().is_relative_to(root.resolve()), "Archive entry escapes run")
        check_artifact_sensitive(path, path.is_relative_to(root / "input/source-snapshot") or "scratch" in path.relative_to(root).parts)
    temp = root / "benchmark-results.zip.tmp"
    with zipfile.ZipFile(temp, "w", zipfile.ZIP_DEFLATED) as z:
        for path in files:
            z.write(path, path.relative_to(root).as_posix())
    temp.replace(root / "benchmark-results.zip")
    with zipfile.ZipFile(root / "benchmark-results.zip") as z:
        require(z.testzip() is None, "Corrupt archive")
        for name in z.namelist():
            require(z.read(name) == inside(root, name).read_bytes(), "Archive differs from deliverable")
    return {"archive": str(root / "benchmark-results.zip"), "files": len(files), "sha256": sha(root / "benchmark-results.zip")}
