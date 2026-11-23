"""Official agy headless transport and frozen, session-only execution policy.

CLI receipts are kept distinct from Codex task-tree receipts. Native cumulative
usage is preserved without inventing child accounting or final-text token TPS.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import threading
import time
from core import check_sensitive, digest, metrics, now, read, require, sha, write
from runtime import process_identity


BACKEND = "antigravity"
MODE = "project-readonly-cli"
POLICY = {"backend": "antigravity-native", "source": "read-only", "workspace": "scratch-only",
          "terminal": "native-sandbox", "outside-sandbox": "review-required",
          "web-and-mcp": "denied", "delegation": "native"}
FULL_MODE = "project-full-access-cli"
FULL_POLICY = {"backend": "antigravity-native", "source": "instruction-only-read-only",
               "workspace": "full-access", "terminal": "unsandboxed", "outside-sandbox": "always-proceed",
               "web-and-mcp": "native-full-access", "delegation": "native"}
LIMITATIONS = [
    "Antigravity CLI native instructions and permission backend differ from historical Codex runs.",
    "CLI cumulative usage is retained as reported; descendant coverage and whole-tree terminal state are not proven.",
    "Final-text token counts, decoding TPS, prices and actual quota charges are unknown.",
    "The evaluator knows the requested model; anonymous answer IDs do not establish independent blind evaluation.",
]


def discover_binary(explicit=None):
    candidates = [Path(explicit)] if explicit else [
        Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "agy/bin/agy.exe",
        Path(shutil.which("agy") or "__missing_agy__"),
    ]
    for path in candidates:
        if path.is_file():
            return path.resolve()
    require(False, "Official agy CLI unavailable; install it or provide --agy")


def catalog(binary):
    version = subprocess.run([str(binary), "--version"], capture_output=True, text=True, encoding="utf-8", check=True)
    models = subprocess.run([str(binary), "models"], capture_output=True, text=True, encoding="utf-8", check=True)
    values = {line.split("\t", 1)[0].strip(): line.split("\t", 1)[1].strip()
              for line in models.stdout.splitlines() if "\t" in line}
    require(values, "agy did not return a model catalog; check login and connectivity")
    return {"version": version.stdout.strip(), "binary_sha256": sha(binary), "models": values}


def profile_hash(profile):
    return digest({k: v for k, v in profile.items() if k != "profile_sha256"})


def source_hashes(directory):
    from execution import _not_link
    result = {}
    directory = Path(directory)
    _not_link(directory)
    for path in sorted(directory.rglob("*")):
        _not_link(path)
        if path.is_file():
            require(path.stat().st_nlink == 1, "Source hardlink aliases are not permitted")
            result[path.relative_to(directory).as_posix()] = sha(path)
    return result


def check_profile(profile, runtime=True, runroot=None):
    expected = FULL_POLICY if profile.get("mode") == FULL_MODE else POLICY
    require(profile.get("mode") in (MODE, FULL_MODE) and profile.get("policy") == expected,
            "Unsupported Antigravity execution policy")
    require(profile.get("profile_sha256") == profile_hash(profile), "Antigravity profile changed")
    root = Path(runroot or profile["run_root"])
    manifest = root / "input/execution-manifest.json"
    require(sha(manifest) == profile["manifest_sha256"], "Frozen source manifest changed")
    expected = {k: v["sha256"] for k, v in read(manifest)["files"].items()}
    require(source_hashes(root / "input/source-snapshot") == expected, "Archived source changed")
    require(digest(expected) == profile["source_content_sha256"], "Source identity changed")
    if runtime:
        require(source_hashes(profile["source_root"]) == expected, "Live source snapshot changed")
    return profile


def normalized_path(path):
    # Matching normalizes drives internally, but native sandbox mounts still
    # need a complete Windows absolute path in the permission declaration.
    return Path(path).resolve().as_posix()



def session_settings(profile, scratch):
    if profile.get("mode") == FULL_MODE:
        return {"agentMode": "accept-edits", "toolPermission": "always-proceed",
                "enableTerminalSandbox": False, "allowNonWorkspaceAccess": True,
                "artifactReviewPolicy": "always-proceed",
                "permissions": {"allow": [], "ask": [], "deny": []}}
    source = normalized_path(profile["readonly_root"])
    denied = [f"read_file({normalized_path(p)})" for p in profile["forbidden_read_paths"]]
    denied += [f"write_file({source})",
               "read_url(*)", "execute_url(*)", "mcp(*)"]
    allowed = [f"read_file({source})",
               f"write_file({normalized_path(scratch)})"]
    allowed += [f"read_file({normalized_path(p)})" for p in profile["runtime_read_roots"]]
    return {"agentMode": "accept-edits", "toolPermission": "proceed-in-sandbox",
            "enableTerminalSandbox": True, "permissions": {"allow": allowed, "ask": [], "deny": denied}}


def safe_event(event):
    """Keep public output/tool evidence, excluding reasoning and arbitrary metadata."""
    kind = event.get("event")
    if kind == "init":
        payload = event.get("init", {})
        return {"event": kind, "conversation_id": event.get("conversation_id"), "init": {
            k: payload[k] for k in ("cwd", "tools", "permission_mode", "model", "agent") if k in payload}}
    if kind == "result":
        value = event.get("result", {})
        return {"event": kind, "result": {k: value[k] for k in
                ("conversation_id", "status", "response", "duration_seconds", "num_turns", "usage") if k in value},
                "error_kind": error_kind(value.get("error", "")) if value.get("error") else None}
    if kind != "step_update":
        return None
    value = event.get("step_update", {})
    step = {k: value[k] for k in ("conversation_id", "step_index", "state", "step_type", "tool_name",
                                 "duration_seconds", "usage") if k in value}
    if value.get("step_type") == "agent_response" and isinstance(value.get("text_delta"), str):
        step["text_delta"] = value["text_delta"]
    tool = value.get("tool_info")
    if isinstance(tool, dict):
        step["tool_info"] = {k: tool[k] for k in ("name", "parameters", "output") if k in tool}
        if tool.get("error"):
            step["tool_info"]["error_kind"] = error_kind(str(tool["error"]))
    info = value.get("subagent_info")
    if isinstance(info, dict):
        step["subagent_info"] = {"subagents": [
            {k: sub[k] for k in ("type_name", "role", "conversation_id") if k in sub}
            for sub in info.get("subagents", []) if isinstance(sub, dict)]}
    return {"event": kind, "step_update": step}


def error_kind(message):
    if re.search(r"quota|resource.?exhaust|rate.?limit|credits?.*(?:exhaust|insufficient)|capacity", str(message), re.I):
        return "quota_or_rate_limit"
    if re.search(r"authenticat|sign.?in|log.?in|unauthorized", str(message), re.I):
        return "authentication_required"
    if re.search(r"soft.den|permission|approval", str(message), re.I):
        return "permission_required"
    return "cli_error"


def canonical_usage(native):
    if not isinstance(native, dict):
        return None
    keys = {"inputTokens": "input_tokens", "cachedInputTokens": "cache_read_tokens",
            "outputTokens": "output_tokens", "reasoningOutputTokens": "thinking_tokens", "totalTokens": "total_tokens"}
    return {key: native.get(value) for key, value in keys.items()}


def cli_metrics(protocol, usage, elapsed, first=None, last=None, answer="", rates=None):
    result = metrics(usage, elapsed, first, last, answer)
    # Cumulative output includes tool/reasoning turns; it cannot price or count
    # the terminal answer's tokens, even when a final text interval is observed.
    for key in ("visible_tokens", "visible_decode_tps", "visible_e2e_tps",
                "estimated_standard_api_usd", "estimated_cold_input_api_usd", "estimated_codex_credits"):
        result[key] = None
    if "service_tiers" in protocol:
        result["estimated_api_usd"] = result.pop("estimated_standard_api_usd")
    return result


def access_audit(events):
    """Record observable tool access; only explicit grading/history paths flag contamination."""
    tools, flagged = {}, []
    private = re.compile(r"(?:[\\/]\.codex[\\/](?:benchmarks|sessions|memories))|(?:[\\/](?:gold\.json|scores(?:-template)?\.json))|(?:[\\/]grading[\\/])", re.I)
    for event in events:
        step = event.get("step_update", {})
        tool = step.get("tool_info", {})
        name = step.get("tool_name", tool.get("name"))
        if not name:
            continue
        key = str(step.get("conversation_id", "")) + ":" + str(step.get("step_index", ""))
        tools[key] = name
        params = json.dumps(tool.get("parameters", {}), ensure_ascii=False).replace("\\\\", "\\")
        if private.search(params) and key not in {x["step"] for x in flagged}:
            flagged.append({"step": key, "tool": name, "reason": "grading_or_historical_data_path_in_tool_arguments"})
    return {"tools": sorted(set(tools.values())), "tool_calls_observed": len(tools),
            "grading_material_access_detected": bool(flagged), "flagged_steps": flagged,
            "coverage": "CLI-exposed tool arguments only; unreported descendant reads are unknown"}


def run_trial(root, protocol, slot, attempt, binary, fingerprint, scratch, *, prompt_override=None, capability=False, new_project=False, delegation_guard=False):
    """Run exactly one fresh conversation. Never impose a generation timeout."""
    root, scratch = Path(root), Path(scratch)
    require(not scratch.exists(), "Every sample needs a new scratch workspace")
    scratch.mkdir(parents=True)
    relative = ("capability/" if capability else "") + f"runs/{slot['id']}/attempt-{attempt:03d}"
    directory = root / relative
    directory.mkdir(parents=True, exist_ok=False)
    profile = protocol["execution_profile"]
    settings = session_settings(profile, scratch)
    write(directory / "session-settings.json", settings)
    instructions = read(root / "input/instructions.json")
    question = (root / "input/prompt.txt").read_text(encoding="utf-8")
    body = prompt_override if prompt_override is not None else "\n\n".join(
        x for x in [instructions.get("base"), instructions.get("developer"),
                    "Use only the frozen source and supplied question. Do not read historical benchmark answers, gold, scoring files, sessions or memories. Do not use Computer Use or browser automation. Do not modify business source; write verification artifacts only in scratch.",
                    f"Frozen source workspace: {profile['source_root']}\nWritable scratch: {scratch}", question] if x)
    check_sensitive(body)
    args = [str(binary), "--model", slot["model"], "--effort", slot["effort"],
            "--mode", "accept-edits", "--settings", str(directory / "session-settings.json"),
            "--input-format", "stream-json", "--output-format", "stream-json", "--disable-slash-commands"]
    args.append("--dangerously-skip-permissions" if profile.get("mode") == FULL_MODE else "--sandbox")
    if new_project:
        args.append("--new-project")
    started = time.monotonic()
    environment = None
    if delegation_guard:
        environment = {**os.environ, "CODEX_PEER_ACTIVE": "1", "PEER_ANALYSIS_ACTIVE": "1"}
    proc = subprocess.Popen(args, cwd=scratch, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", bufsize=1, env=environment)
    inflight = {"pid": os.getpid(), "identity": process_identity(os.getpid()),
                "server_pid": proc.pid, "server_identity": process_identity(proc.pid), "slot_id": slot["id"],
                "attempt": attempt, "relative_dir": relative, "started_at": now(), "purpose": "capability" if capability else "formal"}
    write(root / "inflight.json", inflight)
    lines = queue.Queue()
    diagnostics = set()

    def stdout_reader():
        for line in proc.stdout:
            lines.put((time.monotonic() - started, line))
        lines.put(None)

    def stderr_reader():
        for line in proc.stderr:
            # Diagnostics can contain signed URLs or credentials; retain categories only.
            if re.search(r"error|fail|quota|denied|permission|sandbox|authenticat", line, re.I):
                diagnostics.add(error_kind(line))

    readers = [threading.Thread(target=stdout_reader, daemon=True), threading.Thread(target=stderr_reader, daemon=True)]
    for reader in readers:
        reader.start()
    proc.stdin.write(json.dumps({"event": "user", "message": {"content": body}}, ensure_ascii=False) + "\n")
    proc.stdin.close()
    events, terminal, init = [], None, None
    issues = []
    with (directory / "events.jsonl").open("w", encoding="utf-8", newline="\n") as archive:
        while True:
            try:
                item = lines.get(timeout=1)
            except queue.Empty:
                continue
            if item is None:
                break
            elapsed, line = item
            try:
                event = json.loads(line)
                safe = safe_event(event)
                if safe is None:
                    continue
                check_sensitive(json.dumps(safe, ensure_ascii=False))
                safe["t_s"] = elapsed
                if safe["event"] == "init":
                    if init is not None:
                        issues.append("duplicate_init")
                    init = safe
                if safe["event"] == "result":
                    if terminal is not None:
                        issues.append("multiple_terminal_results")
                    terminal = safe
                events.append(safe)
                archive.write(json.dumps(safe, ensure_ascii=False) + "\n")
                archive.flush()
                inflight.update(conversation_id=(init or {}).get("conversation_id"), last_event_at=now(),
                                last_event=safe["event"], elapsed_s=elapsed)
                write(root / "inflight.json", inflight)
            except (ValueError, TypeError):
                issues.append("unusable_or_sensitive_event_omitted")
    exit_code = proc.wait()
    for reader in readers:
        reader.join()
    elapsed = time.monotonic() - started
    native = (terminal or {}).get("result", {})
    answer = native.get("response", "")
    answer = answer if isinstance(answer, str) else ""
    check_sensitive(answer)
    (directory / "answer.txt").write_text(answer, encoding="utf-8", newline="\n")
    effective_model = (init or {}).get("init", {}).get("model")
    if effective_model != slot["model"]:
        issues.append("effective_model_not_verified")
    if native.get("num_turns") != 1:
        issues.append("fresh_single_turn_not_verified")
    permission_mode = (init or {}).get("init", {}).get("permission_mode")
    if profile.get("mode") == FULL_MODE and permission_mode != "always-proceed":
        issues.append("full_access_receipt_not_verified")
    terminal_status = native.get("status")
    if terminal_status == "SUCCESS" and not answer.strip():
        issues.append("empty_terminal_response")
    valid = exit_code == 0 and terminal_status == "SUCCESS" and bool(answer.strip()) and not issues
    audit = access_audit(events)
    contaminated = audit["grading_material_access_detected"]
    clean = valid and not contaminated
    usage = canonical_usage(native.get("usage"))
    status = "completed" if valid else ("interrupted" if terminal is None else "failed")
    record = {"slot_id": slot["id"], "attempt": attempt, "model": slot["model"], "effort": slot["effort"],
              "service_tier": "default", "repetition": slot["repetition"], "status": status, "valid": clean,
              "transport_valid": valid, "contaminated": contaminated, "tool_access": audit,
              "runtime_backend": BACKEND, "thread_id": native.get("conversation_id"),
              "effective": {"model": effective_model, "reasoningEffort": slot["effort"], "serviceTier": "default", "permissionMode": permission_mode},
              "prompt_sha256": sha(root / "input/prompt.txt"), "answer_sha256": sha(directory / "answer.txt"),
              "submitted_prompt_sha256": digest(body), "fingerprint": fingerprint,
              "usage": usage, "native_usage": native.get("usage"), "usage_scope": "cli-native-cumulative",
              "first_delta_s": None, "last_delta_s": None, "cli_status": terminal_status,
              "cli_exit_code": exit_code, "protocol_issues": issues, "diagnostic_categories": sorted(diagnostics),
              "error_kind": (terminal or {}).get("error_kind") or
                            ("permission_required" if not valid and "permission_required" in diagnostics else None),
              "actual_gateway_charge": None,
              "cli_duration_seconds": native.get("duration_seconds"),
              "answer_ready_s": (terminal or {}).get("t_s"), "limitations": LIMITATIONS,
              "session_settings_sha256": sha(directory / "session-settings.json"),
              "new_project": new_project,
              "delegation_guard": {"CODEX_PEER_ACTIVE": "1", "PEER_ANALYSIS_ACTIVE": "1"} if delegation_guard else {},
              "cli_invocation": {"model": slot["model"], "effort": slot["effort"], "new_project": new_project,
                  "access_mode": "full-access" if profile.get("mode") == FULL_MODE else "restricted"},
              **cli_metrics(protocol, usage, elapsed, answer=answer)}
    if capability:
        record["purpose"] = "capability"
    write(directory / "result.json", record)
    # A missing terminal receipt is not proof that remote inference stopped.
    if terminal is not None:
        (root / "inflight.json").unlink()
    return record


def validate_record(root, protocol, record, directory, answer):
    events = [json.loads(line) for line in (directory / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    terminal = [e for e in events if e.get("event") == "result"]
    initial = [e for e in events if e.get("event") == "init"]
    require(record.get("runtime_backend") == BACKEND, "Missing CLI backend identity")
    if record.get("valid") or record.get("transport_valid"):
        require(len(terminal) == len(initial) == 1, "CLI receipt missing or duplicated")
        native = terminal[0]["result"]
        require(native.get("status") == "SUCCESS" and native.get("num_turns") == 1 and record["cli_exit_code"] == 0,
                "Invalid CLI terminal state")
        require(native.get("response") == answer and canonical_usage(native.get("usage")) == record.get("usage"),
                "CLI terminal answer/usage differs from archived receipt")
        require(initial[0]["init"].get("model") == record["model"] and not record.get("protocol_issues"),
                "CLI model identity or protocol failed")
        if "cli_invocation" in record:
            require(record["cli_invocation"].get("model") == record["model"]
                    and record["cli_invocation"].get("effort") == record["effort"], "CLI invocation model/effort differs")
        require(native.get("conversation_id") == record.get("thread_id"), "CLI conversation identity differs")
        if protocol.get("execution_profile", {}).get("mode") == FULL_MODE:
            require(initial[0]["init"].get("permission_mode") == "always-proceed", "Full Access receipt differs")
    if "tool_access" in record:
        require(access_audit(events) == record["tool_access"], "Tool access audit differs from native events")
        require(record.get("contaminated") == record["tool_access"]["grading_material_access_detected"], "Contamination flag differs")
        require(not record.get("valid") or not record.get("contaminated"), "Contaminated sample cannot be clean")
    return cli_metrics(protocol, record.get("usage"), record.get("elapsed_s"), answer=answer)


CLI_ANSWER_POLICY = "antigravity-terminal-v1"
CLI_RECEIPT = "cli-answer-receipt.json"


def capture_terminal(root, directory):
    """Bind a full native CLI final; no model call or App-receipt substitution."""
    root, directory = Path(root).resolve(), Path(directory).resolve()
    require(directory.parent.parent.parent == root, "CLI attempt must be inside its transport root")
    record, protocol = read(directory / "result.json"), read(root / "protocol.json")
    require(record.get("transport_valid") and (directory / "answer.txt").read_text(encoding="utf-8").strip(),
            "CLI final needs a successful, nonempty native terminal")
    # Do not normalize line endings when comparing the selected final.
    answer = (directory / "answer.txt").read_bytes().decode("utf-8")
    validate_record(root, protocol, record, directory, answer)
    receipt = {"schema_version": 1, "answer_source": CLI_ANSWER_POLICY,
        "captured_at": now(), "status": "captured", "model": record["model"], "effort": record["effort"],
        "conversation_id": record["thread_id"], "answer_sha256": sha(directory / "answer.txt"),
        "protocol_sha256": sha(root / "protocol.json"), "contaminated": record.get("contaminated", False),
        "files_sha256": {name: sha(directory / name) for name in
            ("answer.txt", "result.json", "events.jsonl", "session-settings.json")}, "new_model_calls": 0}
    path = directory / CLI_RECEIPT
    if path.exists():
        existing = validate_terminal_capture(path)
        require(all(existing[k] == v for k, v in receipt.items() if k != "captured_at"), "CLI capture conflicts with its native final")
    else:
        write(path, receipt)
    return {"answer": str(directory / "answer.txt"), "answer_sha256": receipt["answer_sha256"],
        "answer_source": CLI_ANSWER_POLICY, "cli_answer_receipt": str(path), "cli_answer_receipt_sha256": sha(path)}


def validate_terminal_capture(receipt_path, answer_path=None):
    path = Path(receipt_path).resolve(); directory = path.parent
    root = directory.parent.parent.parent
    receipt = read(path)
    require(receipt.get("schema_version") == 1 and receipt.get("answer_source") == CLI_ANSWER_POLICY
            and receipt.get("status") == "captured", "Unsupported CLI final capture")
    require(set(receipt.get("files_sha256", {})) == {"answer.txt", "result.json", "events.jsonl", "session-settings.json"},
            "CLI capture file set differs")
    for name, expected in receipt["files_sha256"].items():
        require(sha(directory / name) == expected, "CLI final source bytes changed: " + name)
    require(sha(root / "protocol.json") == receipt["protocol_sha256"], "CLI capture protocol changed")
    protocol, record = read(root / "protocol.json"), read(directory / "result.json")
    answer = (directory / "answer.txt").read_bytes().decode("utf-8")
    require(record.get("transport_valid") and answer.strip(), "CLI capture lacks successful native final")
    validate_record(root, protocol, record, directory, answer)
    require(receipt["model"] == record["model"] and receipt["effort"] == record["effort"]
            and receipt["conversation_id"] == record["thread_id"]
            and receipt["answer_sha256"] == sha(directory / "answer.txt")
            and receipt["contaminated"] == record.get("contaminated", False), "CLI capture identity differs")
    if answer_path is not None:
        require(Path(answer_path).read_bytes() == answer.encode("utf-8"), "Selected answer differs from CLI terminal")
    return receipt
