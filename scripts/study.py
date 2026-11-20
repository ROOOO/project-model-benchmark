"""Frozen multi-topic round-robin studies using the existing scoring pipeline."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import secrets
import shutil
import sys

import antigravity as agy
from core import (build_schedule, check_inputs, check_sensitive, chosen_attempts, digest, home,
                  inside, load_prices, now, read, require, sha, validate_gold, write)
from runtime import RunLock, still_running


def cycle_schedule(topics, cycles):
    require(topics and len(set(topics)) == len(topics) and cycles > 0, "Use unique topics and positive cycles")
    return [{"cycle": cycle, "topic": topic, "slot_id": f"{cycle:04d}"}
            for cycle in range(1, cycles + 1) for topic in topics]


def check_study(root, runtime=False):
    root = Path(root).resolve()
    study = read(root / "study.json")
    require(sha(root / "study.json") == read(root / "study-seal.json")["study_sha256"], "Frozen study changed")
    require(study["schedule"] == cycle_schedule(study["topics"], study["cycles"]), "Cycle order changed")
    require(study.get("runtime_backend") == agy.BACKEND, "Unsupported study backend")
    for topic in study["topics"]:
        batch = inside(root, topic)
        protocol = check_inputs(batch)
        require(sha(batch / "protocol.json") == study["protocol_sha256"][topic], "Study topic protocol changed")
        require(protocol["models"] == [study["model"]] and protocol["efforts"] == [study["effort"]]
                and protocol["repetitions"] == study["cycles"], "Study conditions differ between topics")
        if runtime:
            agy.check_profile(protocol["execution_profile"], runtime=True)
    return study


def prepare(source_runs, output, model, effort, cycles=5, binary=None, runtime_read_roots=None, *, access_mode="restricted"):
    """Reuse verified sealed material; no inference and no old answers are copied."""
    root = Path(output).resolve()
    require(access_mode in ("restricted", "full-access"), "Unsupported access mode")
    mode = agy.FULL_MODE if access_mode == "full-access" else agy.MODE
    policy = agy.FULL_POLICY if access_mode == "full-access" else agy.POLICY
    require(not root.exists(), "Study directory already exists; use study-resume")
    sources = [Path(path).resolve() for path in source_runs]
    require(len(set(sources)) == len(sources) and sources, "Use unique source batches")
    require(all(not root.is_relative_to(p) and not p.is_relative_to(root) for p in sources), "Study overlaps old batches")
    protocols = [check_inputs(path) for path in sources]
    require(all((path / "grading/seal.json").is_file() for path in sources), "Reuse completed, sealed source batches")
    source_ids = {p.get("execution_profile", {}).get("source_content_sha256") for p in protocols}
    require(len(source_ids) == 1 and None not in source_ids, "Topics need the same frozen source union")
    require(len({p["instructions_sha256"] for p in protocols}) == 1, "Source topic instructions differ")
    for path in sources:
        validate_gold(read(path / "input/gold.json"))
    topics = [path.name for path in sources]
    require(len(set(topics)) == len(topics), "Source topic directory names must be unique")
    work = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "agy-benchmark-work" / secrets.token_hex(12)
    require(not work.exists() and not work.is_relative_to(root), "Work area must be new and separate")
    root.mkdir(parents=True)
    readonly, scratch = work / "readonly", work / "scratch"
    readonly.mkdir(parents=True)
    scratch.mkdir()
    live_source = readonly / "source"
    shutil.copytree(sources[0] / "input/source-snapshot", live_source)
    probe = readonly / "probe"
    probe.mkdir()
    (probe / "readonly.txt").write_text("benchmark readonly control\n", encoding="utf-8")
    canary = root / "permission-canary.txt"
    canary.write_text("private-control-" + secrets.token_hex(20), encoding="utf-8")
    runtime_read_roots = [str(Path(p).resolve()) for p in (runtime_read_roots or [Path(sys.executable).parent])]
    forbidden = [str(home() / name) for name in ("benchmarks", "sessions", "memories", "skills")]
    forbidden += [str(Path.home() / ".gemini/antigravity"), str(Path.home() / ".gemini/antigravity-cli")]
    seals = {}
    for source, original, topic in zip(sources, protocols, topics):
        batch = root / topic
        batch.mkdir()
        for relative in original["input_hashes"]:
            old = inside(source / "input", relative)
            target = inside(batch / "input", relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(old, target)
            require(sha(target) == original["input_hashes"][relative], "Source changed while copying")
        profile = {"schema_version": 1, "mode": mode, "policy": policy,
                   "run_root": str(batch), "model_work_root": str(work), "readonly_root": str(readonly),
                   "source_root": str(live_source), "scratch_root": str(scratch), "probe_root": str(probe),
                   "manifest_sha256": sha(batch / "input/execution-manifest.json"),
                   "source_content_sha256": original["execution_profile"]["source_content_sha256"],
                   "runtime_read_roots": runtime_read_roots, "forbidden_read_paths": forbidden}
        profile["comparison_fingerprint"] = digest({"mode": mode, "policy": policy,
                                                    "source": profile["source_content_sha256"]})
        profile["profile_sha256"] = agy.profile_hash(profile)
        pricing = load_prices()
        write(batch / "prices.json", pricing)
        protocol = {"schema_version": 4, "created_at": now(), "title": original["title"], "project": original["project"],
                    "case_id": original["case_id"], "models": [model], "efforts": [effort], "repetitions": cycles,
                    "service_tier": "default", "concurrency": 1, "seed": 0,
                    "schedule": build_schedule([model], [effort], cycles, 0),
                    "input_hashes": original["input_hashes"], "instructions_sha256": original["instructions_sha256"],
                    "prices_sha256": sha(batch / "prices.json"), "parent": None, "execution_profile": profile,
                    "runtime_backend": agy.BACKEND, "source_batch": str(source),
                    "source_protocol_sha256": sha(source / "protocol.json"), "study_root": str(root),
                    "scope": ("Frozen-project engineering analysis through Antigravity CLI Full Access; no OS source/grade isolation. "
                              if access_mode == "full-access" else
                              "Frozen-project engineering analysis through Antigravity CLI; source read-only, scratch writable. ") +
                             "Same historical questions, source bytes and rubric. Native CLI tools/instructions differ; "
                             "whole-tree accounting, final-text TPS, prices and quota charges unknown. AI scoring with known requested model."}
        write(batch / "protocol.json", protocol)
        write(batch / "preparation-seal.json", {"protocol_sha256": sha(batch / "protocol.json")})
        check_inputs(batch)
        seals[topic] = sha(batch / "protocol.json")
    study = {"schema_version": 1, "created_at": now(), "runtime_backend": agy.BACKEND,
             "access_mode": access_mode,
             "model": model, "effort": effort, "cycles": cycles, "topics": topics,
             "schedule": cycle_schedule(topics, cycles), "protocol_sha256": seals,
             "binary": str(agy.discover_binary(binary)), "work_root": str(work),
             "canary_path": str(canary), "limitations": agy.LIMITATIONS}
    write(root / "study.json", study)
    write(root / "study-seal.json", {"study_sha256": sha(root / "study.json")})
    check_study(root, runtime=True)
    write(root / "study-state.json", {"stage": "prepared", "at": now(), "inference_calls": 0})
    return {"study_dir": str(root), "planned_samples": len(study["schedule"]), "inference_started": False}


def preflight(root, binary=None):
    root = Path(root).resolve()
    study = check_study(root, runtime=True)
    binary = agy.discover_binary(binary or study["binary"])
    info = agy.catalog(binary)
    require(study["model"] in info["models"], "Requested model is absent; no model fallback applied")
    require(study["model"].endswith("-" + study["effort"]), "Pin both model slug and matching effort")
    hashes = {p.name: sha(p) for p in Path(__file__).parent.glob("*.py")}
    inherited = {}
    for relative in (".gemini/GEMINI.md", ".gemini/AGENTS.md", ".gemini/antigravity-cli/settings.json",
                     ".gemini/config/hooks.json", ".gemini/config/mcp_config.json"):
        path = Path.home() / relative
        inherited[relative] = sha(path) if path.is_file() else None
    profile = check_inputs(root / study["topics"][0])["execution_profile"]
    fingerprint = {"binary_sha256": info["binary_sha256"], "version": info["version"],
                   "provider": "google-antigravity-managed", "endpoint_host": None,
                   "model_catalog_sha256": digest(info["models"]), "runner_sha256": digest(hashes),
                   "inherited_files_sha256": inherited, "model": study["model"], "effort": study["effort"],
                   "policy": profile["policy"], "global_instructions_sha256": digest(inherited)}
    result = {"at": now(), "inference_calls": 0, "configuration_passed": True, "fingerprint": fingerprint,
              "permissions_verified": False, "permission_probe": "Requires the separate explicit study-smoke invocation",
              "requested_model_available": True, "limitations": agy.LIMITATIONS}
    runtime_file = root / "runtime.json"
    if runtime_file.exists():
        require(read(runtime_file)["fingerprint"] == fingerprint, "Runtime changed; create a separate study")
    write(root / "preflight.json", result)
    for topic in study["topics"]:
        write(root / topic / "preflight.json", result)
    return result


def probe_command(root, profile):
    source = Path(profile["source_root"])
    selected = next(p for p in sorted(source.rglob("*.cpp")) if p.is_file())
    script = f"""import json, pathlib, hashlib
result = {{'benchmark_permission_probe': 1}}
source = pathlib.Path({str(selected)!r})
result['source_read'] = hashlib.sha256(source.read_bytes()).hexdigest()
scratch = pathlib.Path.cwd() / 'permission-write.txt'
scratch.write_text('scratch-control', encoding='utf-8')
result['scratch_write'] = scratch.read_text(encoding='utf-8') == 'scratch-control'
"""
    if profile.get("mode") != agy.FULL_MODE:
        script += f"""
try:
    pathlib.Path({str(Path(profile['probe_root']) / 'readonly.txt')!r}).write_text('should-be-denied', encoding='utf-8')
    result['source_write_blocked'] = False
except OSError:
    result['source_write_blocked'] = True
try:
    pathlib.Path({str(Path(root) / 'permission-canary.txt')!r}).read_bytes()
    result['private_read_blocked'] = False
except OSError:
    result['private_read_blocked'] = True
"""
    script += "print(json.dumps(result))\n"
    payload = base64.b64encode(script.encode()).decode("ascii")
    command = f'& "{sys.executable}" -B -c "exec(__import__(\'base64\').b64decode(\'{payload}\'))"'
    return command, sha(selected), str(selected)


def probe_receipt(directory, command, expected_source_hash, full_access=False):
    events = [json.loads(line) for line in (Path(directory) / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    found = []
    for event in events:
        tool = event.get("step_update", {}).get("tool_info", {})
        parameters = tool.get("parameters", {})
        if command not in json.dumps(parameters, ensure_ascii=False):
            # JSON escapes backslashes and quotes; compare values as strings instead.
            values = list(parameters.values()) if isinstance(parameters, dict) else []
            matches = any(isinstance(value, str) and (value == command or
                          (value.endswith("…") and len(value) >= 100 and command.startswith(value[:-1]))) for value in values)
            if not matches:
                continue
        output = tool.get("output", "")
        if isinstance(output, str):
            for line in output.splitlines():
                try:
                    value = json.loads(line)
                except ValueError:
                    continue
                if isinstance(value, dict) and value.get("benchmark_permission_probe") == 1:
                    found.append(value)
    require(found, "Native command permission probe output missing; model claims do not verify isolation")
    value = found[-1]
    required = ("scratch_write",) if full_access else ("scratch_write", "source_write_blocked", "private_read_blocked")
    require(value.get("source_read") == expected_source_hash and all(value.get(k) is True for k in required),
            "Native CLI source/command capability check failed" if full_access else "Native CLI isolation probe failed")
    return value


def recover_smoke(root, source, binary=None):
    """Revalidate complete archived native evidence in a fresh revision, zero calls."""
    root, source = Path(root).resolve(), Path(source).resolve()
    require(root != source and not root.is_relative_to(source) and not source.is_relative_to(root), "Use an independent study revision")
    current, original = check_study(root, runtime=True), check_study(source, runtime=True)
    require(current.get("access_mode") == original.get("access_mode") == "full-access", "Recovery requires matching Full Access policies")
    require(not (root / "capability.json").exists(), "Target capability already exists")
    with RunLock(root):
        pre = preflight(root, binary)
        previous = read(source / "preflight.json")
        a, b = pre["fingerprint"], previous["fingerprint"]
        require({k: v for k, v in a.items() if k != "runner_sha256"} ==
                {k: v for k, v in b.items() if k != "runner_sha256"}, "Only local reducer code may differ")
        require(current["model"] == original["model"] and current["effort"] == original["effort"], "Requested settings differ")
        old_batch, new_batch = source / original["topics"][0], root / current["topics"][0]
        p, q = check_inputs(old_batch), check_inputs(new_batch)
        require(p["case_id"] == q["case_id"] and p["input_hashes"] == q["input_hashes"] and
                p["execution_profile"]["source_content_sha256"] == q["execution_profile"]["source_content_sha256"], "Recovery inputs differ")
        require(not (old_batch / "inflight.json").exists(), "Source capability may still be active")
        prior = read(source / "capability.json")
        original_record_path = inside(source, prior["record"])
        record = read(original_record_path)
        answer = original_record_path.with_name("answer.txt").read_text(encoding="utf-8")
        agy.validate_record(old_batch, p, record, original_record_path.parent, answer)
        require(record.get("valid") is True, "Only a valid completed transport can be recovered offline")
        command, expected, _ = probe_command(source, p["execution_profile"])
        proof = probe_receipt(original_record_path.parent, command, expected, full_access=True)
        destination = new_batch / "capability/runs/0001/attempt-001"
        destination.mkdir(parents=True, exist_ok=False)
        hashes = {}
        for name in ("answer.txt", "result.json", "events.jsonl", "session-settings.json"):
            path = original_record_path.with_name(name)
            hashes[name] = sha(path)
            shutil.copyfile(path, destination / name)
            require(sha(destination / name) == hashes[name], "Recovery copy differs")
        receipt = {"passed": True, "at": now(), "formal_samples": 0, "inference_calls": 0,
                   "fingerprint": pre["fingerprint"], "source_fingerprint": record["fingerprint"],
                   "record": str((destination / "result.json").relative_to(root)), "native_permission_probe": proof,
                   "recovery": {"source_study": str(source), "source_files_sha256": hashes,
                                "reason": "Native CLI truncates long command parameters; complete tool output permits offline verification",
                                "historical_paid_calls": 1, "new_paid_calls": 0}}
        write(root / "capability.json", receipt)
        write(root / "capability-history/offline-recovery.json", receipt)
        write(root / "runtime.json", {"fingerprint": pre["fingerprint"], "at": now()})
        for topic in current["topics"]:
            write(root / topic / "runtime.json", {"fingerprint": pre["fingerprint"], "at": now()})
            write(root / topic / "cli-capability.json", receipt)
        write(root / "study-state.json", {"stage": "ready", "at": now(), "next": current["schedule"][0]})
        return receipt


def smoke(root, binary=None, retry_failed=False):
    root = Path(root).resolve()
    study = check_study(root, runtime=True)
    prior = read(root / "capability.json") if (root / "capability.json").exists() else None
    require(prior is None or (not prior.get("passed") and retry_failed),
            "Capability attempt already exists; --retry-failed preserves the failure before retrying")
    with RunLock(root):
        pre = preflight(root, binary)
        topic = study["topics"][0]
        batch = root / topic
        protocol = check_inputs(batch)
        require(not (batch / "inflight.json").exists(), "Check unresolved inflight capability before retrying")
        full = protocol["execution_profile"].get("mode") == agy.FULL_MODE
        attempt = max((int(p.name.split("-")[-1]) for p in (batch / "capability/runs/0001").glob("attempt-*") if p.is_dir()), default=0) + 1
        command, expected, source = probe_command(root, protocol["execution_profile"])
        prompt = ("This is a minimal paid capability check, separate from benchmark answers. "
                  "Use a native file-read tool to read the frozen source file below. Then execute exactly the command below "
                  "once using the native terminal tool. " +
                  ("It checks source read access and scratch writes. Use Full Access without asking for permission. "
                   "Do not read historical answers, grading files, sessions or memories. Do not use Computer Use. "
                   if full else
                  "It checks source read access, scratch writes, a harmless readonly "
                  "control write and private-canary read. The canary is test-only; do not read any other private file. "
                  "Do not change permissions or run outside the sandbox. ") +
                  "Do not replace these checks with an explanation. "
                  "Return READY after the command, or explain the actual failure.\n"
                  f"Source file: {source}\nCommand:\n{command}")
        slot = {"id": "0001", "model": study["model"], "effort": study["effort"], "repetition": 0}
        record = agy.run_trial(batch, protocol, slot, attempt, agy.discover_binary(binary or study["binary"]), pre["fingerprint"],
                               Path(study["work_root"]) / f"scratch/capability-{attempt:03d}", prompt_override=prompt, capability=True)
        directory = batch / f"capability/runs/0001/attempt-{attempt:03d}"
        receipt = {"passed": False, "at": now(), "formal_samples": 0, "inference_calls": 1,
                   "fingerprint": pre["fingerprint"], "record": str(directory.relative_to(root) / "result.json")}
        write(root / "capability.json", receipt)
        write(root / f"capability-history/attempt-{attempt:03d}.json", receipt)
        require(record["valid"], "CLI capability check did not produce a valid terminal answer")
        receipt["native_permission_probe"] = probe_receipt(directory, command, expected, full_access=full)
        check_study(root, runtime=True)
        receipt["passed"] = True
        write(root / "capability.json", receipt)
        write(root / f"capability-history/attempt-{attempt:03d}.json", receipt)
        write(root / "runtime.json", {"fingerprint": pre["fingerprint"], "at": now()})
        for name in study["topics"]:
            write(root / name / "runtime.json", {"fingerprint": pre["fingerprint"], "at": now()})
            write(root / name / "cli-capability.json", receipt)
        write(root / "study-state.json", {"stage": "ready", "at": now(), "next": study["schedule"][0]})
        return receipt


def status(root):
    root = Path(root).resolve()
    study = check_study(root)
    observed = {topic: chosen_attempts(root / topic) for topic in study["topics"]}
    valid = lambda value: bool(value and value.get("valid") and value.get("status") == "completed")
    counts = {topic: sum(valid(value) for value in records.values()) for topic, records in observed.items()}
    pending = [slot for slot in study["schedule"] if not valid(observed[slot["topic"]].get(slot["slot_id"]))]
    return {"at": now(), "valid_completed": sum(counts.values()), "planned": len(study["schedule"]),
            "by_topic": counts, "next_missing": pending[0] if pending else None,
            "complete_cycles": sum(all(valid(observed[t].get(f"{c:04d}")) for t in study["topics"])
                                   for c in range(1, study["cycles"] + 1)),
            "state": read(root / "study-state.json") if (root / "study-state.json").exists() else None}


def execute(root, binary=None, resume=False, retry_failed=False):
    root = Path(root).resolve()
    study = check_study(root, runtime=True)
    with RunLock(root):
        pre = preflight(root, binary)
        capability = read(root / "capability.json")
        require(capability.get("passed") and capability.get("fingerprint") == pre["fingerprint"],
                "Passing capability evidence for this exact runtime is required")
        require(not (root / "inflight.json").exists(), "Unresolved study inflight record")
        existing = {topic: chosen_attempts(root / topic) for topic in study["topics"]}
        require(resume or not any(existing.values()), "Samples already exist; use study-resume")
        for position in study["schedule"]:
            topic, sid = position["topic"], position["slot_id"]
            batch = root / topic
            require(not (batch / "grading/seal.json").exists(), "Scores sealed; prepare a separate study")
            inflight = batch / "inflight.json"
            if inflight.exists():
                active = read(inflight)
                require(not still_running(active) and not still_running(active, "server_"), "Existing agy process is active; wait")
                require(False, "Local process ended without a terminal receipt; remote completion is unknown. Reconcile before retrying")
            previous = existing[topic].get(sid)
            if previous and (previous.get("valid") or previous.get("contaminated")):
                continue
            if previous and not retry_failed:
                # Keep failed positions visible; do not silently advance past an exhausted quota.
                write(root / "study-state.json", {"stage": "needs_retry_decision", "at": now(), "position": position})
                return status(root)
            require(sha(agy.discover_binary(binary or study["binary"])) == pre["fingerprint"]["binary_sha256"],
                    "agy binary changed during study; no new request sent")
            protocol = check_inputs(batch)
            agy.check_profile(protocol["execution_profile"], runtime=True)
            slot = next(item for item in protocol["schedule"] if item["id"] == sid)
            attempt = max((int(p.name.split("-")[-1]) for p in (batch / "runs" / sid).glob("attempt-*") if p.is_dir()), default=0) + 1
            write(root / "study-state.json", {"stage": "sampling", "at": now(), "position": position, "attempt": attempt})
            record = agy.run_trial(batch, protocol, slot, attempt, agy.discover_binary(binary or study["binary"]),
                                   pre["fingerprint"], Path(study["work_root"]) / f"scratch/{topic}-{sid}-{attempt:03d}")
            existing[topic][sid] = record
            check_study(root, runtime=True)
            if not record["valid"] and not record.get("contaminated"):
                reason = record.get("error_kind") or "invalid_terminal_receipt"
                stage = "quota_paused" if reason == "quota_or_rate_limit" else "failed"
                write(root / "study-state.json", {"stage": stage, "at": now(), "position": position, "reason": reason})
                snapshot = status(root)
                write(root / "progress.json", snapshot)
                print(json.dumps(snapshot, ensure_ascii=False), flush=True)
                return snapshot
            snapshot = status(root)
            write(root / "progress.json", snapshot)
            write(batch / "progress.json", {"at": now(), "valid_completed_slots": snapshot["by_topic"][topic],
                                           "planned_slots": study["cycles"]})
            print(json.dumps({k: snapshot[k] for k in ("valid_completed", "planned", "by_topic", "complete_cycles", "next_missing")}, ensure_ascii=False), flush=True)
        write(root / "study-state.json", {"stage": "sampling_complete", "at": now(), "next": "blind and seal each topic"})
        return status(root)
