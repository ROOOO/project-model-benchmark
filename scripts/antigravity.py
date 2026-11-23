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
import urllib.parse

from core import check_sensitive, digest, home, metrics, now, read, require, sha, write
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


def projects_config_dir():
    return Path.home() / ".gemini/config/projects"


def _canonical_path(path):
    return os.path.normcase(os.path.normpath(str(Path(path).resolve(strict=False))))


def _is_under(child, parent):
    c, p = _canonical_path(child), _canonical_path(parent)
    prefix = p if p.endswith(os.sep) else p + os.sep
    return c != p and c.startswith(prefix)


def folder_uri_to_path(uri):
    """Resolve local file:// URIs (both file://C:/... and file:///c%3A/...) safely."""
    if not isinstance(uri, str) or not uri.lower().startswith("file:"):
        return None
    parsed = urllib.parse.urlparse(uri)
    if parsed.scheme.lower() != "file":
        return None
    netloc = urllib.parse.unquote(parsed.netloc or "")
    path = urllib.parse.unquote(parsed.path or "")
    if re.fullmatch(r"[A-Za-z]:", netloc):
        raw = netloc + path
    elif netloc in ("", "localhost"):
        raw = path[1:] if re.match(r"^/[A-Za-z]:(?:[\\/]|$)", path) else path
    else:
        return None
    if not raw:
        return None
    return Path(raw).resolve(strict=False)


def ephemeral_project_target(path):
    """Return metadata only for non-default, non-sidecar, single local-folder CLI projects."""
    path = Path(path)
    if path.name.lower() == "default-cli-project.json":
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    project_id = data.get("id")
    if not isinstance(project_id, str) or not project_id.strip() or project_id == "default-cli-project":
        return None
    if "sidecar" in data:
        return None
    resources = (data.get("projectResources") or {}).get("resources")
    if not isinstance(resources, list) or len(resources) != 1:
        return None
    entry = resources[0]
    if not isinstance(entry, dict) or "gitFolder" in entry:
        return None
    folder = folder_uri_to_path(entry.get("folderUri"))
    if folder is None:
        return None
    return {"id": project_id, "name": data.get("name"), "folder": folder, "path": path}


def snapshot_projects_dir():
    directory = projects_config_dir()
    if not directory.is_dir():
        return set()
    try:
        return {_canonical_path(p) for p in directory.glob("*.json")}
    except OSError:
        return set()


def _remove_project_file(path, archive_dir=None):
    path = Path(path)
    if archive_dir is not None:
        archive_dir = Path(archive_dir).resolve(strict=False)
        config_dir = projects_config_dir().resolve(strict=False)
        require(_canonical_path(archive_dir) != _canonical_path(config_dir) and not _is_under(archive_dir, config_dir),
                "Archive directory must be outside Antigravity projects config directory")
        try:
            archive_dir.mkdir(parents=True, exist_ok=True)
            dest = archive_dir / path.name
            if not dest.exists():
                shutil.copy2(path, dest)
        except OSError as exc:
            return f"{type(exc).__name__}: {exc}"
    last_error = None
    for delay in (0.0, 0.1, 0.2):
        if delay:
            time.sleep(delay)
        try:
            path.unlink()
            return None
        except FileNotFoundError:
            return None
        except OSError as exc:
            last_error = exc
    return f"{type(last_error).__name__}: {last_error}"


def cleanup_ephemeral_projects_for_dir(target_dir, *, pre_existing=None, archive_dir=None):
    """Remove only ephemeral CLI project JSON files matching target_dir exactly."""
    directory = projects_config_dir()
    if not directory.is_dir():
        return {"removed": [], "errors": []}
    expected = _canonical_path(target_dir)
    pre = set(pre_existing) if pre_existing is not None else None
    removed, errors = [], []
    try:
        candidates = sorted(directory.glob("*.json"))
    except OSError as exc:
        return {"removed": [], "errors": [f"{type(exc).__name__}: {exc}"]}
    for path in candidates:
        if pre is not None and _canonical_path(path) in pre:
            continue
        info = ephemeral_project_target(path)
        if info is None or _canonical_path(info["folder"]) != expected:
            continue
        err = _remove_project_file(path, archive_dir=archive_dir)
        if err:
            errors.append({"id": info["id"], "file": path.name, "error": err})
        else:
            removed.append({"id": info["id"], "name": info["name"], "file": path.name})
    return {"removed": removed, "errors": errors}


def _active_benchmark_targets(benchmark_root):
    active_scratches, active_work_roots, active_peer_runs, active_peer_roots = set(), set(), set(), set()
    unscoped_inflight = False
    if not benchmark_root.is_dir():
        return active_scratches, active_work_roots, active_peer_runs, active_peer_roots, unscoped_inflight
    for inflight in benchmark_root.rglob("inflight.json"):
        scoped = False
        for candidate in (inflight.parent / "protocol.json", inflight.parent.parent / "study.json",
                          inflight.parent.parent.parent.parent / "protocol.json"):
            if not candidate.is_file():
                continue
            try:
                data = json.loads(candidate.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                continue
            profile = data.get("execution_profile") if isinstance(data, dict) else None
            for root_value in (
                data.get("work_root") if isinstance(data, dict) else None,
                profile.get("model_work_root") if isinstance(profile, dict) else None,
                profile.get("scratch_root") if isinstance(profile, dict) else None,
            ):
                if isinstance(root_value, str) and root_value.strip():
                    active_work_roots.add(_canonical_path(root_value))
                    scoped = True
        if not scoped:
            unscoped_inflight = True
    for state_file in benchmark_root.glob("*/units/*/state.json"):
        try:
            state = json.loads(state_file.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            unscoped_inflight = True
            continue
        if isinstance(state, dict) and state.get("status") in {"launching", "running", "collecting", "awaiting-audit", "unknown-inflight"}:
            if isinstance(state.get("scratch"), str) and state["scratch"].strip():
                active_scratches.add(_canonical_path(state["scratch"]))
            else:
                proto_file = state_file.parents[2] / "protocol.json"
                if proto_file.is_file():
                    try:
                        proto = json.loads(proto_file.read_text(encoding="utf-8-sig"))
                        if isinstance(proto.get("work_root"), str) and proto["work_root"].strip():
                            active_work_roots.add(_canonical_path(proto["work_root"]))
                        else:
                            unscoped_inflight = True
                    except (OSError, ValueError):
                        unscoped_inflight = True
                else:
                    unscoped_inflight = True
    for active_file in benchmark_root.glob("*/units/*/peer-state/active.json"):
        try:
            active = json.loads(active_file.read_text(encoding="utf-8-sig"))
            run_id = active.get("run_id") if isinstance(active, dict) else None
            if isinstance(run_id, str) and run_id.strip():
                active_peer_runs.add(_canonical_path(active_file.parent / "runs" / run_id))
            else:
                active_peer_roots.add(_canonical_path(active_file.parent))
        except (OSError, ValueError):
            active_peer_roots.add(_canonical_path(active_file.parent))
    return active_scratches, active_work_roots, active_peer_runs, active_peer_roots, unscoped_inflight


def cleanup_stale_projects(*, benchmark_root=None, work_root=None, dry_run=True, archive_dir=None):
    """Inspect or clean leftover ephemeral Antigravity projects from completed benchmark runs."""
    directory = projects_config_dir()
    bench = Path(benchmark_root).resolve(strict=False) if benchmark_root else (home() / "benchmarks").resolve(strict=False)
    work = Path(work_root).resolve(strict=False) if work_root else (
        Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "agy-benchmark-work"
    ).resolve(strict=False)
    if archive_dir is not None:
        archive_dir = Path(archive_dir).resolve(strict=False)
        config_dir = directory.resolve(strict=False)
        require(_canonical_path(archive_dir) != _canonical_path(config_dir) and not _is_under(archive_dir, config_dir),
                "Archive directory must be outside Antigravity projects config directory")
    active_scratches, active_work_roots, active_peer_runs, active_peer_roots, unscoped_inflight = _active_benchmark_targets(bench)
    candidates, removed, skipped_active, skipped_unverified, errors = [], [], [], [], []
    if directory.is_dir():
        for path in sorted(directory.glob("*.json")):
            info = ephemeral_project_target(path)
            if info is None:
                continue
            folder = info["folder"]
            kind = None
            run_dir = None
            if _is_under(folder, work):
                rel = Path(_canonical_path(folder)).relative_to(_canonical_path(work))
                if len(rel.parts) == 3 and rel.parts[1].lower() == "scratch":
                    if (unscoped_inflight or _canonical_path(folder) in active_scratches
                            or any(_is_under(folder, Path(aw)) or _canonical_path(folder) == aw for aw in active_work_roots)):
                        skipped_active.append({"id": info["id"], "name": info["name"], "file": path.name, "folder": str(folder), "reason": "active-or-inflight-benchmark"})
                        continue
                    kind = "benchmark-scratch"
            elif _is_under(folder, bench):
                rel = Path(_canonical_path(folder)).relative_to(_canonical_path(bench))
                if (len(rel.parts) == 7 and rel.parts[1].lower() == "units"
                        and rel.parts[3].lower() == "peer-state" and rel.parts[4].lower() == "runs"
                        and rel.parts[6].lower() == "input"):
                    run_dir = folder.parent
                    peer_state = run_dir.parent.parent
                    if _canonical_path(run_dir) in active_peer_runs or _canonical_path(peer_state) in active_peer_roots:
                        skipped_active.append({"id": info["id"], "name": info["name"], "file": path.name, "folder": str(folder), "reason": "active-peer-run"})
                        continue
                    run_file = run_dir / "run.json"
                    try:
                        record = json.loads(run_file.read_text(encoding="utf-8-sig"))
                    except (OSError, ValueError):
                        skipped_unverified.append({"id": info["id"], "name": info["name"], "file": path.name, "folder": str(folder), "reason": "missing-or-unreadable-run-record"})
                        continue
                    terminal_ok = isinstance(record, dict) and (
                        record.get("terminal_verified") is True
                        or record.get("status") in {"resolved-stopped", "failed-before-launch"}
                    )
                    if not terminal_ok:
                        skipped_unverified.append({"id": info["id"], "name": info["name"], "file": path.name, "folder": str(folder), "reason": "unverified-peer-run"})
                        continue
                    kind = "benchmark-peer-input"
            if kind is None:
                continue
            item = {"id": info["id"], "name": info["name"], "file": path.name, "kind": kind, "folder": str(folder)}
            candidates.append(item)
            if not dry_run:
                if kind == "benchmark-peer-input" and run_dir is not None and run_dir.is_dir():
                    archived = run_dir / "ephemeral-project.json"
                    if not archived.exists():
                        try:
                            shutil.copy2(path, archived)
                        except OSError as exc:
                            errors.append({**item, "error": f"{type(exc).__name__}: {exc}"})
                            continue
                err = _remove_project_file(path, archive_dir=archive_dir)
                if err:
                    errors.append({**item, "error": err})
                else:
                    removed.append(item)
    return {
        "projects_dir": str(directory),
        "dry_run": dry_run,
        "archive_dir": str(archive_dir) if archive_dir is not None else None,
        "matched": len(candidates),
        "removed_count": len(removed),
        "candidates": candidates,
        "removed": removed,
        "skipped_active": skipped_active,
        "skipped_unverified": skipped_unverified,
        "errors": errors,
    }


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


def run_trial(root, protocol, slot, attempt, binary, fingerprint, scratch, *, prompt_override=None, capability=False, new_project=False, project=None, delegation_guard=False, cleanup_new_project=True):
    """Run exactly one fresh conversation. Never impose a generation timeout."""
    root, scratch = Path(root), Path(scratch)
    require(not (new_project and project), "Cannot combine new_project and explicit project")
    if project is not None:
        require(isinstance(project, str) and bool(project.strip()), "Explicit project must be a non-empty string")
        project = project.strip()
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
    before_projects = snapshot_projects_dir() if (new_project and cleanup_new_project) else None
    if new_project:
        args.append("--new-project")
    elif project:
        args += ["--project", project]
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
    project_cleanup = None
    if new_project and cleanup_new_project and terminal is not None:
        project_cleanup = cleanup_ephemeral_projects_for_dir(scratch, pre_existing=before_projects)
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
                  **({"project": project} if project else {}),
                  "access_mode": "full-access" if profile.get("mode") == FULL_MODE else "restricted"},
              **({"project": project} if project else {}),
              **({"ephemeral_project_cleanup": project_cleanup} if project_cleanup is not None else {}),
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
