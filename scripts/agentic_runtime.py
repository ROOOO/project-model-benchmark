"""Native engineering workflow transport; no raw reasoning or credentials persisted."""
from __future__ import annotations

import collections
import json
from pathlib import Path
import time

from core import BenchmarkError, check_inputs, digest, home, now, read, require, sha, write


def environment_fingerprint(config, protocol):
    """Seal only safe configuration fields and hashes of instruction/catalog files."""
    agents = config.get("agents", {})
    safe_agents = {k: agents.get(k) for k in ("enabled", "max_threads", "max_depth", "max_concurrent_threads_per_session",
                  "default_subagent_model", "default_subagent_reasoning_effort", "expose_model_overrides") if k in agents}
    roles = {}
    for name, role in agents.items():
        if isinstance(role, dict) and role.get("config_file"):
            path = Path(role["config_file"]).expanduser()
            if not path.is_absolute():
                path = home() / path
            require(path.is_file(), "Configured agent role file unavailable")
            roles[name] = sha(path)
    cache = home() / "models_cache.json"
    data = read(cache) if cache.exists() else {}
    selected = []
    for model in data.get("models", []):
        if model.get("slug") in protocol["models"]:
            levels = [x.get("effort") for x in model.get("supported_reasoning_levels", [])]
            resolved = model.get("multi_agent_reasoning_effort") or ("max" if "max" in levels else next((x for x in reversed(levels) if x != "ultra"), None))
            selected.append({"model": model["slug"], "multi_agent_version": model.get("multi_agent_version"),
                             "ultra_resolved_effort": resolved, "model_metadata_sha256": digest(model)})
    return {"profile_sha256": protocol["execution_profile"]["profile_sha256"],
            "agent_settings": safe_agents, "agent_roles_sha256": roles,
            "feature_overrides": {k: v for k, v in config.get("features", {}).items() if isinstance(v, bool)},
            "models": selected, "catalog_client_version": data.get("client_version"),
            "model_catalog_sha256": digest(sorted(data.get("models", []), key=lambda model: model.get("slug", ""))),
            "native_defaults": "Selected binary defaults; no benchmark-specific agent model, effort or concurrency overrides"}


def check_native_configuration(server, protocol, models):
    state = server.fingerprint["execution"]
    require(state["agent_settings"].get("enabled", True), "Native agents are disabled by inherited configuration")
    require(server.launch_overrides.get("features.multi_agent") is not False, "Benchmark launch disables delegation")
    versions = {m.get("model", m.get("id")): m.get("multiAgentVersion") for m in models}
    catalog = {m["model"]: m for m in state["models"]}
    for model in protocol["models"]:
        require(model in catalog, "Model catalog metadata missing; cannot freeze native Ultra resolution")
        if "ultra" in protocol["efforts"]:
            require(catalog[model]["ultra_resolved_effort"], "Ultra reasoning resolution unavailable")
        version = versions.get(model) or catalog[model].get("multi_agent_version")
        require(version in ("v1", "v2"), "Native multi-agent backend metadata missing")
        if version == "v1":
            require(state["feature_overrides"].get("multi_agent", True), "Inherited configuration disables V1 delegation")
        if version == "v2":
            require(state["feature_overrides"].get("multi_agent_v2", True), "Inherited configuration disables V2 delegation")


def capability_identity(protocol, fingerprint):
    """Exact reusable capability scope; workspace paths are deliberately excluded."""
    require(isinstance(fingerprint, dict), "Capability runtime identity is missing; repeat non-inference preflight")
    runtime_keys = ("binary_sha256", "version", "provider", "endpoint_host", "global_instructions_sha256")
    execution_keys = ("agent_settings", "agent_roles_sha256", "feature_overrides", "models", "catalog_client_version", "native_defaults")
    execution = fingerprint.get("execution", {})
    profile = protocol.get("execution_profile", {})
    require(profile.get("mode") == "project-readonly", "Capability sharing requires project-readonly execution")
    require(isinstance(execution, dict) and all(k in fingerprint for k in runtime_keys) and all(k in execution for k in execution_keys),
            "Capability runtime identity is incomplete; repeat non-inference preflight")
    require(isinstance(execution["models"], list) and all(isinstance(model, dict) for model in execution["models"]),
            "Capability model identity is incomplete; repeat non-inference preflight")
    require(bool(profile.get("comparison_fingerprint")) and bool(protocol.get("instructions_sha256")),
            "Capability source/instruction identity is missing")
    stable_execution = {k: execution[k] for k in execution_keys}
    for key in ("instruction_sources_sha256", "inherited_config_sha256", "model_catalog_sha256", "binary_companions_sha256",
                "effective_configuration_fingerprint", "effective_native_settings"):
        if key in execution:
            stable_execution[key] = execution[key]
    stable_execution["models"] = sorted(execution["models"], key=lambda x: x.get("model", ""))
    return digest({"schema": 1, "runtime": {k: fingerprint[k] for k in runtime_keys},
                   "execution": stable_execution, "instructions_sha256": protocol["instructions_sha256"],
                   "comparison_fingerprint": profile["comparison_fingerprint"],
                   "supported_matrix": {"models": sorted(protocol["models"]),
                                        "service_tiers": sorted(protocol.get("service_tiers", ["default"]))}})


def _capability_artifact(root, relative):
    """Allow only sanitized attempt receipts and already audited scratch archives."""
    import re
    from core import inside
    require(isinstance(relative, str) and "\\" not in relative and ":" not in relative
            and all(p not in ("", ".", "..") for p in relative.split("/")), "Invalid capability artifact path")
    require(bool(re.fullmatch(r"(?:runs/[0-9]{4}/attempt-[0-9]{3}/(?:answer\.txt|result\.json|events\.jsonl|task-tree\.json|scratch/.+)|recovery/(?:receipt\.json|source/(?:answer\.txt|result\.json|events\.jsonl|task-tree\.json|protocol\.json|preparation-seal\.json|prices\.json|runtime\.json|preflight\.json|permission-probe\.json|permission-probe-seal\.json|instruction-context\.json)))", relative)),
            "Capability artifact is outside the receipt allowlist")
    base = Path(root) / "capability"
    require(base.is_dir() and not base.is_symlink() and not (getattr(base.lstat(), "st_file_attributes", 0) & 0x400),
            "Capability directory must not be a link or reparse point")
    path = inside(base, relative)
    entry = base
    for part in relative.split("/"):
        entry = entry / part
        require(entry.exists() and not entry.is_symlink() and not (getattr(entry.lstat(), "st_file_attributes", 0) & 0x400),
                "Capability artifacts must not contain links or reparse points")
    require(path.is_file() and path.stat().st_nlink == 1, "Capability artifact must be a regular, unlinked file")
    return path


def _capability_id(compatibility_id, origin_fingerprint_sha256, files):
    return digest({"schema": 1, "compatibility_id": compatibility_id,
                   "origin_fingerprint_sha256": origin_fingerprint_sha256, "files_sha256": files})


def _capability_prompt():
    return ("这是运行能力验收，不是正式评测。请创建一个原生子代理，让它读取当前冻结源码目录中的一个文件，"
            "返回文件相对路径和首个非空行；主代理独立读取同一文件核对，等待子代理完成后用中文简短报告一致性。"
            "只读源码，任何临时文件仅写 scratch。不要访问评分或历史资料。必须实际执行，失败如实报告。")


RECOVERY_CONTEXT = ("protocol.json", "preparation-seal.json", "prices.json", "runtime.json", "preflight.json",
                    "permission-probe.json", "permission-probe-seal.json", "instruction-context.json")


def _capability_records(root, files=None):
    base = Path(root) / "capability"
    names = files if files is not None else (p.relative_to(base).as_posix() for p in base.glob("runs/*/attempt-*/result.json"))
    return {name: read(_capability_artifact(root, name)) for name in names
            if name.startswith("runs/") and name.endswith("/result.json")}


def _recovery_receipt(root, fingerprint, *, require_repaired=True):
    """Validate a derived replay without substituting its historical runtime identity."""
    from agentic import TreeLedger
    receipt = read(_capability_artifact(root, "recovery/receipt.json"))
    require(receipt.get("schema_version") == 1 and receipt.get("inference_calls") == 0
            and receipt.get("historical_paid_attempts") == 1 and receipt.get("replay_checks") == 1,
            "Capability recovery provenance is incomplete")
    if require_repaired:
        require(receipt.get("repair_fingerprint_sha256") == digest(fingerprint), "Recovered capability belongs to a different repair runtime")
        require(receipt.get("repair_runner_sha256") == {name: sha(Path(__file__).parent / name) for name in ("agentic.py", "agentic_runtime.py", "runtime.py")},
                "Recovery reducer or runner changed; preserve the replay and prepare a fresh revision")
    files = receipt.get("source_files_sha256", {})
    require(set(files) == {"answer.txt", "result.json", "events.jsonl", "task-tree.json"}, "Recovery source receipt set is incomplete")
    for name, expected in files.items():
        require(sha(_capability_artifact(root, "recovery/source/" + name)) == expected, "Recovery source receipt changed")
    original = read(_capability_artifact(root, "recovery/source/result.json"))
    context = receipt.get("source_context_sha256", {})
    require(set(context) == set(RECOVERY_CONTEXT), "Recovery source context is incomplete")
    collection = receipt.get("source_collection_sha256", {})
    require(isinstance(collection, dict) and all(collection.get(name) == expected for name, expected in context.items())
            and all(collection.get("capability/runs/0001/attempt-001/" + name) == expected for name, expected in files.items()),
            "Recovery copied evidence differs from the original source collection")
    for name, expected in context.items():
        require(sha(_capability_artifact(root, "recovery/source/" + name)) == expected, "Recovery source context changed")
    source_protocol = read(_capability_artifact(root, "recovery/source/protocol.json"))
    require(read(_capability_artifact(root, "recovery/source/preparation-seal.json")).get("protocol_sha256") == context["protocol.json"]
            and source_protocol.get("prices_sha256") == context["prices.json"], "Recovery frozen protocol or price identity differs")
    source_profile = source_protocol.get("execution_profile", {})
    source_fingerprint = original.get("fingerprint")
    for name in ("preflight.json", "runtime.json", "permission-probe.json"):
        require(read(_capability_artifact(root, "recovery/source/" + name)).get("fingerprint") == source_fingerprint,
                "Recovery archived runtime identities differ")
    permission_seal = read(_capability_artifact(root, "recovery/source/permission-probe-seal.json"))
    permission_report = read(_capability_artifact(root, "recovery/source/permission-probe.json"))
    require(permission_seal.get("report_sha256") == context["permission-probe.json"]
            and permission_seal.get("fingerprint_sha256") == digest(source_fingerprint)
            and permission_seal.get("profile_sha256") == source_profile.get("profile_sha256")
            and permission_report.get("passed") is True and permission_report.get("backend") == "mxc" and permission_report.get("inference_calls") == 0,
            "Recovery archived permission proof differs")
    require(receipt.get("source_prices") == read(_capability_artifact(root, "recovery/source/prices.json"))
            and receipt.get("source_prices_sha256") == context["prices.json"], "Recovery original price snapshot differs")
    instructions = read(_capability_artifact(root, "recovery/source/instruction-context.json"))
    expected_instructions = source_fingerprint.get("execution", {}).get("instruction_sources_sha256")
    if expected_instructions is not None:
        require(instructions.get("sha256") == expected_instructions, "Recovery original instruction chain differs")
    require(receipt.get("compatibility_id") == capability_identity(source_protocol, source_fingerprint)
            == capability_identity(check_inputs(root), fingerprint), "Recovery archived capability identity differs")
    snapshot = read(_capability_artifact(root, "recovery/source/task-tree.json"))
    require(original.get("agentic") == snapshot, "Recovery source result and task tree differ")
    event_path = _capability_artifact(root, "recovery/source/events.jsonl")
    require([json.loads(line) for line in event_path.read_text(encoding="utf-8").splitlines() if line.strip()] == snapshot.get("events"),
            "Recovery events differ from the original archived order")
    original_answer = _capability_artifact(root, "recovery/source/answer.txt")
    require(original.get("answer_sha256") == sha(original_answer), "Recovery original answer digest differs")
    derived_name = receipt.get("derived_result")
    require(derived_name == "runs/0001/attempt-001/result.json", "Recovery must cover the original first capability position only")
    derived = read(_capability_artifact(root, derived_name))
    require(derived.get("fingerprint") == original.get("fingerprint") and derived.get("execution_profile_sha256") == original.get("execution_profile_sha256"),
            "Recovered result must retain its original execution fingerprint")
    provenance = derived.get("replay_provenance", {})
    require(provenance.get("kind") == "historical-paid-offline-replay" and provenance.get("inference_calls") == 0
            and provenance.get("source_result_sha256") == files["result.json"] and provenance.get("receipt") == "recovery/receipt.json",
            "Recovered result provenance differs")
    require(sha(_capability_artifact(root, "runs/0001/attempt-001/answer.txt")) == files["answer.txt"]
            and sha(_capability_artifact(root, "runs/0001/attempt-001/events.jsonl")) == files["events.jsonl"],
            "Recovery changed the answer or original event archive")
    replay = TreeLedger.from_snapshot(snapshot)
    require(replay.snapshot() == derived.get("agentic") == read(_capability_artifact(root, "runs/0001/attempt-001/task-tree.json")),
            "Recovered ledger differs from a fresh archived-order replay")
    require(replay.accounting_complete and derived.get("accounting_complete") is True and derived.get("valid") is True,
            "Recovered ledger did not pass accounting and terminal validation")
    require(receipt.get("source_fingerprint_sha256") == digest(original.get("fingerprint")), "Recovery source runtime identity differs")
    require(receipt.get("response_usage_sha256") == digest({r["response_id"]: r["usage"] for r in snapshot.get("responses", [])})
            == digest({r["response_id"]: r["usage"] for r in replay.snapshot()["responses"]}), "Recovery changed per-response usage")
    require(receipt.get("answer_sha256") == files["answer.txt"], "Recovery answer provenance differs")
    expected_metrics = replay.metrics(receipt.get("source_prices", {}), source_protocol, original.get("workflow_elapsed_s"))
    require(all(derived.get(name) == value for name, value in expected_metrics.items()), "Recovery metrics differ from original sealed prices and replay usage")
    require(receipt.get("estimated_api_usd") == expected_metrics.get("tree_estimated_api_usd") and receipt.get("actual_gateway_charge") is None,
            "Recovery fee attribution differs")
    return receipt


def require_capability(root, fingerprint):
    root = Path(root).resolve()
    path = root / "capability/seal.json"
    require(path.is_file(), "Native delegation capability smoke is required before formal sampling: use smoke or adopt-smoke")
    require(not path.is_symlink() and path.stat().st_nlink == 1, "Capability seal must be a regular, unlinked file")
    seal = read(path)
    protocol = check_inputs(root)
    identity = capability_identity(protocol, fingerprint)
    require(seal.get("fingerprint") == fingerprint and seal.get("passed") is True,
            "Capability smoke missing, failed, or belongs to a different runtime")
    require(seal.get("compatibility_id") == identity, "Capability source, instructions, matrix or runtime identity differs")
    files = seal.get("files_sha256")
    require(isinstance(files, dict) and bool(files), "Capability artifact manifest is missing")
    for relative, expected in files.items():
        require(sha(_capability_artifact(root, relative)) == expected, "Capability smoke artifacts changed")
    actual = {p.relative_to(root / "capability").as_posix() for p in (root / "capability").rglob("*") if p.is_file() and p != path}
    require(actual == set(files), "Capability artifact collection differs from its seal")
    require(seal.get("capability_id") == _capability_id(identity, seal.get("origin_fingerprint_sha256"), files),
            "Capability shared identity does not match its sealed artifacts")
    expected_matrix = {(model, tier) for model in protocol["models"] for tier in protocol.get("service_tiers", ["default"])}
    named_records = _capability_records(root, files)
    records = list(named_records.values())
    require(len(records) == len(expected_matrix) and seal.get("verified_attempts") == len(records), "Capability verification matrix is incomplete")
    require(all(isinstance(r, dict) and isinstance(r.get("model"), str)
                and isinstance(r.get("service_tier", "default"), str) for r in records), "Capability receipts have invalid model/speed identities")
    require({(r.get("model"), r.get("service_tier", "default")) for r in records} == expected_matrix,
            "Capability verification model/speed matrix differs")
    require(all(r.get("purpose") == "capability" and r.get("effort") == "ultra" and r.get("valid") is True
                and r.get("accounting_complete") is True and r.get("delegation_count", 0) > 0 for r in records),
            "Capability verification receipts are incomplete or unsuccessful")
    require(seal.get("formal_samples") == 0, "Capability verification must not count as formal samples")
    if seal.get("schema_version", 1) == 2:
        origins = seal.get("attempt_origins", {})
        require(set(origins) == set(named_records) and seal.get("origin_fingerprint_sha256") == digest(origins),
                "Capability per-attempt origin manifest differs")
        for name, record in named_records.items():
            origin = origins[name]
            require(isinstance(origin, dict) and origin.get("fingerprint_sha256") == digest(record.get("fingerprint"))
                    and origin.get("kind") in ("historical-paid-offline-replay", "new-paid"), "Capability attempt origin differs")
            if origin["kind"] == "historical-paid-offline-replay":
                receipt = _recovery_receipt(root, fingerprint, require_repaired=False)
                require(origin.get("source_result_sha256") == receipt["source_files_sha256"]["result.json"]
                        and origin.get("receipt") == "recovery/receipt.json" and name == receipt["derived_result"],
                        "Historical capability attempt provenance differs")
            else:
                require(not record.get("replay_provenance"), "New paid capability attempt cannot be a replay")
                if seal.get("paid_here") is True:
                    require(record.get("fingerprint") == fingerprint, "New paid capability attempt uses a different runtime")
        historical = sum(value["kind"] == "historical-paid-offline-replay" for value in origins.values())
        new = len(records) - historical
        require(historical == 1 and seal.get("historical_paid_attempts") == historical and seal.get("replay_checks") == historical
                and seal.get("origin_new_paid_attempts") == new and seal.get("paid_ledger_attempts") == len(records),
                "Recovered capability charge counts differ")
        if seal.get("paid_here") is True:
            require(seal.get("attempts") == seal.get("new_paid_attempts") == new and not seal.get("shared_from"),
                    "Recovered capability local paid accounting differs")
        else:
            source = seal.get("shared_from", {})
            require(seal.get("paid_here") is False and seal.get("attempts") == seal.get("new_paid_attempts") == 0
                    and seal.get("shared_attempts") == len(records) and isinstance(source, dict)
                    and source.get("capability_id") == seal["capability_id"] and source.get("compatibility_id") == identity
                    and source.get("seal_sha256") and source.get("root"), "Adopted capability accounting/provenance is incomplete")
        return seal
    require(seal.get("schema_version", 1) == 1
            and all(digest(r.get("fingerprint")) == seal.get("origin_fingerprint_sha256") for r in records),
            "Capability execution origin differs")
    if seal.get("paid_here") is True:
        require(seal.get("attempts") == len(records) and not seal.get("shared_from")
                and seal.get("origin_fingerprint_sha256") == digest(fingerprint), "Original capability accounting/provenance differs")
    else:
        source = seal.get("shared_from", {})
        require(seal.get("paid_here") is False and seal.get("attempts") == 0
                and seal.get("shared_attempts") == len(records) and isinstance(source, dict) and source.get("capability_id") == seal["capability_id"]
                and source.get("compatibility_id") == identity and source.get("seal_sha256") and source.get("root"),
                "Adopted capability accounting/provenance is incomplete")
    return seal


def adopt_capability(target_root, source_root):
    """Reuse an explicitly selected, compatible paid verification without inference."""
    import shutil
    from execution import require_permission_probe
    from runtime import RunLock
    target, source = Path(target_root).resolve(), Path(source_root).resolve()
    require(target != source, "Capability source and target must be distinct batches")
    require(target.is_dir() and source.is_dir(), "Capability source and target batch directories must exist")
    require(not (target / "capability").exists(), "Target capability already exists; preserve its records")
    with RunLock(target):
        require(not (target / "capability").exists(), "Target capability already exists; preserve its records")
        protocol, source_protocol = check_inputs(target), check_inputs(source)
        preflight_path = target / "preflight.json"
        require(preflight_path.is_file(), "Target non-inference preflight is required before capability adoption")
        preflight = read(preflight_path)
        require(preflight.get("inference_calls") == 0 and preflight.get("unsupported") == []
                and isinstance(preflight.get("fingerprint"), dict), "Target non-inference preflight did not pass")
        fingerprint = preflight["fingerprint"]
        require_permission_probe(protocol["execution_profile"], fingerprint)
        source_path = source / "capability/seal.json"
        require(source_path.is_file(), "Selected source has no sealed capability verification")
        source_seal = read(source_path)
        validated = require_capability(source, source_seal.get("fingerprint", {}))
        identity = capability_identity(protocol, fingerprint)
        require(identity == capability_identity(source_protocol, validated["fingerprint"]) == validated["compatibility_id"],
                "Capability cannot be shared: frozen source, instructions, model matrix or native runtime configuration differs")
        source_seal_sha256 = sha(source_path)
        for relative, expected in validated["files_sha256"].items():
            original = _capability_artifact(source, relative)
            destination = target / "capability" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(original, destination)
            require(sha(destination) == expected and sha(original) == expected, "Capability artifact changed during adoption")
        require(sha(source_path) == source_seal_sha256, "Source capability seal changed during adoption")
        seal = {"passed": True, "at": now(), "fingerprint": fingerprint, "compatibility_id": identity,
                "capability_id": validated["capability_id"], "origin_fingerprint_sha256": validated["origin_fingerprint_sha256"],
                "files_sha256": validated["files_sha256"], "attempts": 0, "verified_attempts": validated["verified_attempts"],
                "shared_attempts": validated["verified_attempts"], "formal_samples": 0, "paid_here": False,
                "shared_from": {"root": str(source), "seal_sha256": source_seal_sha256,
                                "fingerprint_sha256": digest(validated["fingerprint"]), "compatibility_id": identity,
                                "capability_id": validated["capability_id"]},
                "cost_scope": "Shared paid capability verification; zero new attempts or inference calls in this batch. Original charges belong to the source capability_id."}
        if validated.get("schema_version") == 2:
            seal.update({key: validated[key] for key in ("schema_version", "attempt_origins", "historical_paid_attempts", "replay_checks",
                                                        "origin_new_paid_attempts", "paid_ledger_attempts")})
            seal["new_paid_attempts"] = 0
        write(target / "capability/seal.json", seal)
        require_capability(target, fingerprint)
        return {"passed": True, "capability_attempts": 0, "shared_capability_attempts": validated["verified_attempts"],
                "formal_samples": 0, "inference_calls": 0, "paid_here": False, "capability_id": validated["capability_id"]}


def _collection_hashes(root):
    """Read-only whole-source evidence, rejecting links rather than following them."""
    root = Path(root)
    require(root.is_dir() and not root.is_symlink() and not (getattr(root.lstat(), "st_file_attributes", 0) & 0x400),
            "Recovery source must be a regular directory")
    values = {}
    for path in root.rglob("*"):
        require(not path.is_symlink() and not (getattr(path.lstat(), "st_file_attributes", 0) & 0x400),
                "Recovery source contains a link or reparse point")
        if path.is_file():
            require(path.stat().st_nlink == 1, "Recovery source contains a hard-linked file")
            values[path.relative_to(root).as_posix()] = sha(path)
    return values


def recover_capability(target_root, source_root):
    """Zero-RPC, zero-inference replay of one archived failed accounting check."""
    import copy
    import shutil
    from agentic import TreeLedger, TOKEN_METRICS
    from execution import require_permission_probe
    from runtime import RunLock

    target, source = Path(target_root).resolve(), Path(source_root).resolve()
    require(target != source and target.is_dir() and source.is_dir(), "Recovery requires distinct existing batches")
    require(not target.is_relative_to(source) and not source.is_relative_to(target), "Recovery batches must not be nested")
    require(not (target / "capability").exists(), "Target capability already exists; preserve its records")
    with RunLock(target):
        require(not (target / "capability").exists(), "Target capability already exists; preserve its records")
        before = _collection_hashes(source)
        protocol, source_protocol = check_inputs(target), check_inputs(source)
        preflight = read(target / "preflight.json")
        original_preflight = read(source / "preflight.json")
        require(preflight.get("inference_calls") == original_preflight.get("inference_calls") == 0
                and preflight.get("unsupported") == original_preflight.get("unsupported") == [], "Recovery requires passed non-inference preflights")
        fingerprint, old_fingerprint = preflight.get("fingerprint"), original_preflight.get("fingerprint")
        identity = capability_identity(protocol, fingerprint)
        require(identity == capability_identity(source_protocol, old_fingerprint),
                "Recovery source, instructions, matrix, permissions or runtime identity differs")
        question_hashes = lambda value: {name: expected for name, expected in value.get("input_hashes", {}).items()
                                         if name != "execution-manifest.json"}
        require(protocol.get("case_id") == source_protocol.get("case_id") and question_hashes(protocol) == question_hashes(source_protocol),
                "Recovery target must preserve the original frozen question and scoring inputs")
        require_permission_probe(protocol["execution_profile"], fingerprint)
        require_permission_probe(source_protocol["execution_profile"], old_fingerprint)
        require(read(source / "runtime.json").get("fingerprint") == old_fingerprint, "Recovery source runtime receipt differs")
        require(not (source / "inflight.json").exists() and not (source / "capability/seal.json").exists(),
                "Recovery source is active or already has a completed capability seal")
        records = _capability_records(source)
        original_name = "runs/0001/attempt-001/result.json"
        require(set(records) == {original_name}, "Recovery supports exactly one original attempted capability position")
        original = records[original_name]
        require(original.get("fingerprint") == old_fingerprint and original.get("execution_profile_sha256") == source_protocol["execution_profile"]["profile_sha256"],
                "Recovery source execution fingerprint differs")
        require(original.get("purpose") == "capability" and original.get("slot_id") == "0001" and original.get("attempt") == 1
                and original.get("model") == protocol["models"][0] and original.get("service_tier", "default") == protocol.get("service_tiers", ["default"])[0]
                and original.get("effort") == "ultra" and original.get("prompt_sha256") == digest(_capability_prompt()),
                "Recovery source request does not match the original first capability position")
        require(original.get("status") == "completed" and original.get("valid") is True and original.get("protocol_issues") == []
                and original.get("accounting_complete") is False, "Recovery only repairs an otherwise valid completed accounting failure")
        snapshot = read(_capability_artifact(source, "runs/0001/attempt-001/task-tree.json"))
        require(original.get("agentic") == snapshot and bool(snapshot.get("accounting_issues"))
                and set(original.get("warnings", [])) <= set(snapshot.get("accounting_issues", [])), "Recovery source failure is not isolated to accounting")
        archived = _capability_artifact(source, "runs/0001/attempt-001/events.jsonl")
        require([json.loads(line) for line in archived.read_text(encoding="utf-8").splitlines() if line.strip()] == snapshot.get("events"),
                "Recovery events differ from the original archived order")
        answer_path = _capability_artifact(source, "runs/0001/attempt-001/answer.txt")
        require(original.get("answer_sha256") == sha(answer_path), "Recovery original answer digest differs")
        require(snapshot.get("root_thread_id") == original.get("thread_id") and snapshot.get("root_turn_id") == original.get("turn_id"),
                "Recovery source task identity differs")
        _require_permission_receipt(original.get("permission_receipt", {}), source_protocol["execution_profile"])
        for node in snapshot.get("nodes", []):
            _require_permission_receipt(node.get("settings", {}), source_protocol["execution_profile"])
            for change in node.get("settings_history", []):
                settings = change.get("settings", {})
                if "activePermissionProfile" in settings or "approvalPolicy" in settings:
                    _require_permission_receipt(settings, source_protocol["execution_profile"])
        replay = TreeLedger.from_snapshot(snapshot)  # Original archive order, intentionally not sorted.
        replayed = replay.snapshot()
        original_usage = {record["response_id"]: record["usage"] for record in snapshot.get("responses", [])}
        require(len(original_usage) == len(snapshot.get("responses", []))
                and original_usage == {record["response_id"]: record["usage"] for record in replayed["responses"]},
                "Recovery changed per-response usage or response identities")
        require(replay.final_answer == answer_path.read_text(encoding="utf-8") and replay.root_status == "completed"
                and replay.tree_terminal and replay.accounting_complete and not replay.errors, "Archived-order replay did not pass unchanged-answer accounting verification")
        metrics = replay.metrics(read(source / "prices.json"), source_protocol, original.get("workflow_elapsed_s"))
        require(metrics.get("delegation_count", 0) > 0, "Recovery native delegation receipt is incomplete")
        independent = {label: sum(record[key] for record in original_usage.values()) for label, key in TOKEN_METRICS.items()}
        require(all(metrics.get("tree_" + label) == total for label, total in independent.items()), "Recovery independent response sum differs")
        result = copy.deepcopy(original)
        result.update(metrics, agentic=replayed, warnings=[], replay_provenance={"kind": "historical-paid-offline-replay", "source_root": str(source),
                      "source_attempt_relative": "capability/runs/0001/attempt-001", "source_result_sha256": before["capability/" + original_name],
                      "receipt": "recovery/receipt.json", "inference_calls": 0})
        destination = target / "capability/runs/0001/attempt-001"
        destination.mkdir(parents=True)
        source_files = {}
        for name in ("answer.txt", "result.json", "events.jsonl", "task-tree.json"):
            original_file = _capability_artifact(source, "runs/0001/attempt-001/" + name)
            source_files[name] = sha(original_file)
            archived_file = target / "capability/recovery/source" / name
            archived_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(original_file, archived_file)
        source_context = {}
        for name in RECOVERY_CONTEXT:
            original_file = source / name
            require(original_file.is_file(), "Recovery source context is incomplete: " + name)
            source_context[name] = sha(original_file)
            shutil.copyfile(original_file, target / "capability/recovery/source" / name)
        shutil.copyfile(answer_path, destination / "answer.txt")
        shutil.copyfile(archived, destination / "events.jsonl")
        write(destination / "task-tree.json", replayed)
        write(destination / "result.json", result)
        receipt = {"schema_version": 1, "at": now(), "inference_calls": 0, "historical_paid_attempts": 1, "replay_checks": 1,
                   "source_root": str(source), "source_files_sha256": source_files, "source_context_sha256": source_context, "source_collection_sha256": before,
                   "source_fingerprint_sha256": digest(old_fingerprint), "repair_fingerprint_sha256": digest(fingerprint),
                   "compatibility_id": identity, "derived_result": original_name,
                   "repair_runner_sha256": {name: sha(Path(__file__).parent / name) for name in ("agentic.py", "agentic_runtime.py", "runtime.py")},
                   "source_prices": read(source / "prices.json"), "source_prices_sha256": source_protocol["prices_sha256"],
                   "answer_sha256": sha(answer_path), "response_usage_sha256": digest(original_usage), "unique_responses": len(original_usage),
                   "delegation_count": metrics["delegation_count"], "independent_token_totals": independent,
                   "original_accounting_issues": snapshot["accounting_issues"], "replay_accounting_issues": replay.accounting_issues(),
                   "estimated_api_usd": metrics["tree_estimated_api_usd"], "actual_gateway_charge": None,
                   "cost_scope": "One historical paid attempt, one zero-inference replay; the replay adds no model charge. Gateway charge remains unknown."}
        write(target / "capability/recovery/receipt.json", receipt)
        require(before == _collection_hashes(source), "Recovery source collection changed during read-only replay")
        _recovery_receipt(target, fingerprint)
        return {"passed": True, "inference_calls": 0, "historical_paid_attempts": 1, "replay_checks": 1, "new_paid_attempts": 0,
                "remaining_capability_attempts": len(protocol["models"]) * len(protocol.get("service_tiers", ["default"])) - 1,
                "unique_responses": len(original_usage), "tree_input_tokens": metrics["tree_input_tokens"], "tree_output_tokens": metrics["tree_output_tokens"],
                "estimated_api_usd": metrics["tree_estimated_api_usd"], "actual_gateway_charge": None, "formal_samples": 0}


def _descendants(server, ledger, elapsed):
    """Discover unloaded descendants too; process RPC interleavings before declaring idle."""
    cursor, seen = None, set()
    while True:
        params = {"ancestorThreadId": ledger.root_thread_id, "limit": 100, "sourceKinds": []}
        if cursor:
            params["cursor"] = cursor
        page = server.rpc("thread/list", params)
        for thread in page.get("data", []):
            ledger.reconcile_thread(thread, elapsed())
        cursor = page.get("nextCursor")
        if not cursor:
            break
        require(cursor not in seen, "Repeated descendant cursor")
        seen.add(cursor)
    server.rpc("thread/loaded/list", {})
    background = []
    for tid in list(ledger.related_thread_ids):
        thread = server.rpc("thread/read", {"threadId": tid, "includeTurns": False}).get("thread", {})
        ledger.reconcile_thread(thread, elapsed())
        cursor, seen = None, set()
        while True:
            params = {"threadId": tid, "limit": 100}
            if cursor:
                params["cursor"] = cursor
            page = server.rpc("thread/backgroundTerminals/list", params)
            background.extend({"thread_id": tid, "process_id": x.get("processId"), "item_id": x.get("itemId")}
                              for x in page.get("data", []))
            cursor = page.get("nextCursor")
            if not cursor:
                break
            require(cursor not in seen, "Repeated background-terminal cursor")
            seen.add(cursor)
    return background


def _require_permission_receipt(settings, profile, *, root=False):
    receipt = settings.get("activePermissionProfile")
    require(isinstance(receipt, dict) and receipt.get("id") == profile["permission_profile_id"],
            "Native thread permission receipt missing or differs from the frozen profile")
    require(settings.get("approvalPolicy") == "never", "Native thread approval policy differs from the frozen profile")
    if root:
        require(isinstance(settings.get("cwd"), str) and Path(settings["cwd"]).resolve() == Path(profile["work_cwd"]).resolve(),
                "Native root cwd differs from the frozen workspace")


def _capture_child_receipts(server, ledger, elapsed, *, profile=None):
    """Rejoin only an observed loaded child, without settings overrides or a turn.

    Initial settings are a notification baseline, not necessarily a notification.
    A warm metadata-only resume exposes the actual live configuration. Unloaded
    children remain unknown; never cold-resume them to manufacture a receipt.
    """
    captured = {event.get("params", {}).get("threadId") for event in ledger.events
                if event.get("method") == "ledger/threadSettingsObserved"}
    pending = sorted(set(ledger.related_thread_ids) - {ledger.root_thread_id} - captured)
    result = {"captured": [], "unloaded": [], "unmaterialized": []}
    if not pending:
        return result
    if profile is None:
        profile = check_inputs(server.root)["execution_profile"]
    for tid in pending:
        loaded = server.rpc("thread/loaded/list", {}).get("data")
        require(isinstance(loaded, list) and all(isinstance(value, str) for value in loaded),
                "Native loaded-thread receipt is malformed")
        if tid not in loaded:
            result["unloaded"].append(tid)
            continue
        metadata = server.rpc("thread/read", {"threadId": tid, "includeTurns": False}).get("thread")
        require(isinstance(metadata, dict) and metadata.get("id") == tid,
                "Native child materialization metadata belongs to a different thread")
        rollout = metadata.get("path")
        # A loaded fresh thread can have a future rollout path yet no storage.
        # Stat only this owned thread's native path; never read session content.
        if not isinstance(rollout, str) or not Path(rollout).is_file():
            result["unmaterialized"].append(tid)
            continue
        # No path/history/config/model/provider/permissions or other overrides.
        response = server.rpc("thread/resume", {"threadId": tid, "excludeTurns": True})
        thread = response.get("thread")
        require(isinstance(thread, dict) and thread.get("id") == tid,
                "Native warm child receipt belongs to a different thread")
        observed = elapsed() if callable(elapsed) else elapsed
        ledger.reconcile_thread(thread, observed)
        ledger.reconcile_settings(tid, response, observed)
        require(tid in ledger.related_thread_ids, "Native warm receipt is outside the trial's descendant tree")
        _require_permission_receipt(response, profile)
        result["captured"].append(tid)
    return result


def project_thread_probe(server, protocol):
    """Inspect native thread permissions/instructions without submitting a turn."""
    from execution import instruction_fingerprint, thread_permissions
    profile = protocol["execution_profile"]
    response = server.rpc("thread/start", {"model": protocol["models"][0], "modelProvider": server.provider,
        "allowProviderModelFallback": False, "serviceTier": "default", "cwd": profile["work_cwd"],
        "approvalPolicy": "never", "ephemeral": False, "experimentalRawEvents": True,
        "dynamicTools": [], "baseInstructions": None, "developerInstructions": read(server.root / "input/instructions.json")["developer"],
        "config": {"model_reasoning_effort": protocol["efforts"][0], "model_reasoning_summary": "none"},
        **thread_permissions(profile)})
    tid = response["thread"]["id"]
    try:
        _require_permission_receipt(response, profile, root=True)
        sources = instruction_fingerprint(response.get("instructionSources", []))
        context = {"sha256": digest(sources), "sources": sources}
        path = server.root / "instruction-context.json"
        if path.exists():
            require(read(path) == context, "Inherited instruction content changed; use a new batch")
        else:
            write(path, context)
        server.fingerprint["execution"]["instruction_sources_sha256"] = context["sha256"]
        return {"inference_calls": 0, "activePermissionProfile": response["activePermissionProfile"],
                "approvalPolicy": response["approvalPolicy"], "instruction_sources_sha256": context["sha256"]}
    finally:
        server.rpc("thread/archive", {"threadId": tid})


def run_trial(server, item, attempt, *, capability=False, prompt_override=None):
    from agentic import TreeLedger
    from execution import check_profile, thread_permissions, require_permission_probe, reset_scratch, instruction_fingerprint
    from runtime import RpcRejected, process_identity
    import os

    root = server.root
    protocol = check_inputs(root)
    profile = protocol["execution_profile"]
    check_profile(profile)
    require_permission_probe(profile, server.fingerprint)
    if not capability:
        require_capability(root, server.fingerprint)
    # A fresh thread alone does not isolate files left by earlier samples.
    reset_scratch(profile)
    tier = item.get("service_tier", "default")
    directory = root / ("capability/runs" if capability else "runs") / item["id"] / f"attempt-{attempt:03d}"
    directory.mkdir(parents=True, exist_ok=False)
    prompt = prompt_override or (root / "input/prompt.txt").read_text(encoding="utf-8")
    instructions = read(root / "input/instructions.json")
    result = {"slot_id": item["id"], "attempt": attempt, "model": item["model"], "effort": item["effort"],
              "repetition": item["repetition"], "service_tier": tier, "started_at": now(), "status": "starting", "valid": False,
              "usage": None, "tool_items": [], "warnings": [], "protocol_issues": [], "effective": {}, "actual_gateway_charge": None,
              "prompt_sha256": digest(prompt) if capability else sha(root / "input/prompt.txt"), "fingerprint": server.fingerprint,
              "execution_profile_sha256": profile["profile_sha256"], "purpose": "capability" if capability else "formal"}
    inflight = {"slot_id": item["id"], "attempt": attempt, "relative_dir": directory.relative_to(root).as_posix(),
                "pid": os.getpid(), "identity": process_identity(os.getpid()), "server_pid": server.proc.pid,
                "server_identity": process_identity(server.proc.pid), "created_at": now(), "purpose": result["purpose"],
                "model": item["model"], "effort": item["effort"], "service_tier": tier, "repetition": item["repetition"],
                "fingerprint": server.fingerprint}
    write(root / "inflight.json", inflight)
    ledger, start, elapsed_s = None, None, None
    pending = collections.deque()
    before = server.counts["unexpected_server_request"]
    try:
        require(server.fingerprint["global_instructions_sha256"] == (sha(home() / "AGENTS.md") if (home() / "AGENTS.md").exists() else None), "Global instructions changed")
        params = {"model": item["model"], "modelProvider": server.provider, "allowProviderModelFallback": False,
                  "serviceTier": tier, "ephemeral": False, "cwd": profile["work_cwd"], "approvalPolicy": "never",
                  "experimentalRawEvents": True, "dynamicTools": [], "baseInstructions": instructions["base"],
                  "developerInstructions": instructions["developer"], "serviceName": "project_model_benchmark",
                  "config": {"model_reasoning_effort": item["effort"], "model_reasoning_summary": "none", "service_tier": tier},
                  **thread_permissions(profile)}
        response = server.rpc("thread/start", params)
        effective = {k: response.get(k) for k in ("model", "modelProvider", "reasoningEffort", "serviceTier")}
        result["effective"] = effective
        require(effective == {"model": item["model"], "modelProvider": server.provider, "reasoningEffort": item["effort"], "serviceTier": tier}, "Effective settings differ from requested settings")
        _require_permission_receipt(response, profile, root=True)
        result["permission_receipt"] = {"activePermissionProfile": response["activePermissionProfile"],
                                        "approvalPolicy": response["approvalPolicy"]}
        tid = response["thread"]["id"]
        result["thread_id"] = tid
        context_sources = instruction_fingerprint(response.get("instructionSources", []))
        context_hash = digest(context_sources)
        context = root / "instruction-context.json"
        if context.exists():
            require(read(context)["sha256"] == context_hash, "Inherited instructions changed; use a new batch")
        else:
            write(context, {"sha256": context_hash, "sources": context_sources})
        start = time.perf_counter()
        ledger = TreeLedger(tid, None, effective | result["permission_receipt"])
        server.active = True
        inflight.update(thread_id=tid, submitted_at=now())
        write(root / "inflight.json", inflight)
        turn = server.rpc("turn/start", {"threadId": tid, "model": item["model"], "effort": item["effort"],
                          "summary": "none", "serviceTierForTurn": tier, "input": [{"type": "text", "text": prompt}]})
        result["turn_id"] = turn["turn"]["id"]
        ledger.bind_root_turn(result["turn_id"])
        pending = collections.deque(server.pending)
        server.pending.clear()
        last_notice, discovery_rounds = start, 0
        with (directory / "events.jsonl").open("w", encoding="utf-8") as log:
            while True:
                # RPCs (including child receipt reads) receive notifications while
                # waiting for their acknowledgement. Consume those before asking
                # the transport for a newer event, preserving receive order.
                pending.extend(server.pending)
                server.pending.clear()
                if pending:
                    stamp, message = pending.popleft()
                elif ledger.root_completed:
                    background = _descendants(server, ledger, lambda: time.perf_counter() - start)
                    _capture_child_receipts(server, ledger, lambda: time.perf_counter() - start, profile=profile)
                    result["background_terminal_check"] = {"elapsed_s": time.perf_counter()-start,
                                                           "active": background}
                    pending.extend(server.pending)
                    server.pending.clear()
                    if pending:
                        discovery_rounds = 0
                        continue
                    if not ledger.active_threads and not background:
                        discovery_rounds += 1
                        if discovery_rounds >= 2:
                            ledger.mark_discovery_complete(time.perf_counter()-start)
                            server.active = False
                            break
                        continue
                    discovery_rounds = 0
                    polled = server.poll_event()
                    if polled is None:
                        continue
                    stamp, message = polled
                else:
                    stamp, message = server.event()
                elapsed_s = max(0, stamp - start)
                prior_threads = set(ledger.related_thread_ids)
                normalized = ledger.observe(message, elapsed_s)
                if set(ledger.related_thread_ids) != prior_threads or message.get("method") in (
                        "turn/started", "thread/started", "thread/settings/updated", "rawResponse/completed"):
                    _capture_child_receipts(server, ledger, lambda: time.perf_counter() - start, profile=profile)
                if normalized:
                    log.write(json.dumps(normalized, ensure_ascii=False) + "\n")
                    log.flush()
                    write(directory / "task-tree.json", ledger.snapshot())
                    inflight.update(thread_ids=sorted(ledger.related_thread_ids), event_count=len(ledger.snapshot().get("events", [])))
                    write(root / "inflight.json", inflight)
                if time.perf_counter() - last_notice >= 30:
                    print(json.dumps({"status": "running", "elapsed_s": time.perf_counter()-start,
                                      "known_threads": len(ledger.related_thread_ids), "active_threads": len(ledger.active_threads)}), flush=True)
                    last_notice = time.perf_counter()
        result["status"] = ledger.root_status or "unknown"
    except (Exception, KeyboardInterrupt) as exc:
        if isinstance(exc, RpcRejected) and not result.get("turn_id"):
            server.active = False
        result["status"] = "interrupted" if server.active or isinstance(exc, (ConnectionError, KeyboardInterrupt)) else "failed"
        result["error_kind"] = type(exc).__name__
        if isinstance(exc, BenchmarkError):
            result["error_detail"] = str(exc)
        result["protocol_issues"].append("Execution did not reach a verified task-tree terminal state")
        result["warnings"].append("Failure retained; remote completion and additional charges may be unknown. No automatic retry.")
        if ledger:
            # RPC notifications can arrive before an acknowledgement is lost.
            queued = list(pending) + list(server.pending)
            for stamp, message in queued:
                ledger.observe(message, max(0, stamp-start))
            server.pending.clear()
            ledger.mark_transport_incomplete()
    elapsed_s = time.perf_counter()-start if start is not None else None
    answer = ledger.final_answer if ledger else ""
    (directory / "answer.txt").write_text(answer, encoding="utf-8", newline="\n")
    if ledger:
        snapshot = ledger.snapshot()
        # Reconciliation and discovery records must be replayable along with streamed events.
        (directory / "events.jsonl").write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in snapshot.get("events", [])), encoding="utf-8")
        result["agentic"] = snapshot
        write(directory / "task-tree.json", snapshot)
        result.update(ledger.metrics(read(root / "prices.json"), protocol, elapsed_s))
        result["warnings"].extend(ledger.accounting_issues())
        result["protocol_issues"].extend(ledger.errors)
        for node in snapshot["nodes"]:
            try:
                _require_permission_receipt(node["settings"], profile)
                for change in node.get("settings_history", []):
                    settings = change.get("settings", {})
                    if "activePermissionProfile" in settings or "approvalPolicy" in settings:
                        _require_permission_receipt(settings, profile)
            except BenchmarkError as exc:
                result["protocol_issues"].append(str(exc) + ": " + node["thread_id"])
        if ledger.root_status == "completed" and not answer.strip():
            result["protocol_issues"].append("No explicit root final-answer phase was observed")
        result["tool_items"] = sorted({e.get("type") for e in snapshot.get("tools", []) if e.get("type")})
    else:
        from core import metrics_for_protocol
        result.update(metrics_for_protocol(protocol, None, elapsed_s, answer=answer))
    if server.counts["unexpected_server_request"] != before:
        result["protocol_issues"].append("Unexpected interactive or client-tool request")
    result.update(completed_at=now(), answer_sha256=sha(directory / "answer.txt"), stderr_counts=dict(server.counts))
    if not server.active:
        try:
            reset_scratch(profile, directory / "scratch")
        except BenchmarkError:
            result["protocol_issues"].append("Scratch artifact validation failed; remaining files retained for inspection")
    result["valid"] = result["status"] == "completed" and bool(answer.strip()) and not result["protocol_issues"]
    write(directory / "result.json", result)
    if not server.active:
        (root / "inflight.json").unlink(missing_ok=True)
    return result


def capability_smoke(root, binary=None, *, continue_existing=False):
    """Explicit paid check, separately archived; never called by non-inference preflight."""
    from execution import require_permission_probe
    from runtime import RunLock, Server, recover_inflight
    root = Path(root).resolve()
    protocol = check_inputs(root)
    require(protocol.get("execution_profile", {}).get("mode") == "project-readonly", "Smoke requires a project-readonly batch")
    require("ultra" in protocol["efforts"], "Native Ultra capability check requires Ultra in the requested matrix")
    exists = (root / "capability").exists()
    require(continue_existing or not exists, "Capability attempts already exist; preserve failures and use smoke --continue only after recover-smoke")
    require(not continue_existing or exists, "Smoke continuation requires an existing recovered capability")
    require(not (root / "capability/seal.json").exists(), "Capability is already sealed; no additional paid smoke is allowed")
    results = []
    with RunLock(root):
        recover_inflight(root)
        existing = _capability_records(root) if exists else {}
        # Inspect attempts before starting the server: failures are retained, never retried.
        if continue_existing:
            require(bool(existing), "Smoke continuation has no recovered or completed capability positions")
            matrix = [(model, tier) for model in protocol["models"] for tier in protocol.get("service_tiers", ["default"])]
            expected_names = {f"runs/{index:04d}/attempt-001/result.json": (model, tier)
                              for index, (model, tier) in enumerate(matrix, 1)}
            require(set(existing) <= set(expected_names), "Existing capability positions differ from the frozen matrix")
            for name, result in existing.items():
                model, tier = expected_names[name]
                require(result.get("valid") is True and result.get("accounting_complete") is True
                        and result.get("delegation_count", 0) > 0 and result.get("status") == "completed",
                        "A prior capability position failed; no automatic retry or further sampling is allowed")
                require(result.get("model") == model and result.get("service_tier", "default") == tier
                        and result.get("effort") == "ultra" and result.get("purpose") == "capability" and result.get("attempt") == 1,
                        "Existing capability position differs from the frozen matrix")
            directories = {p.relative_to(root / "capability").as_posix() for p in (root / "capability/runs").glob("*/attempt-*") if p.is_dir()}
            require(directories == {str(Path(name).parent).replace("\\", "/") for name in existing},
                    "A capability position has an incomplete retained attempt; no retry is allowed")
            preflight = read(root / "preflight.json")
            receipt = _recovery_receipt(root, preflight.get("fingerprint"))
            require(receipt.get("compatibility_id") == capability_identity(protocol, preflight.get("fingerprint")),
                    "Recovered capability identity differs from the current preflight")
        server = Server(root, binary)
        try:
            preflight = server.preflight(protocol)
            require_permission_probe(protocol["execution_profile"], server.fingerprint)
            identity = capability_identity(protocol, server.fingerprint)
            write(root / "runtime.json", {"fingerprint": preflight["fingerprint"], "at": now()})
            if continue_existing:
                _recovery_receipt(root, server.fingerprint)
                require(all(record.get("fingerprint") == server.fingerprint or record.get("replay_provenance") for record in existing.values()),
                        "Existing paid capability runtime differs; prepare a new batch")
            prompt = _capability_prompt()
            position = 0
            for model in protocol["models"]:
                for tier in protocol.get("service_tiers", ["default"]):
                    position += 1
                    item = {"id": f"{position:04d}", "model": model, "effort": "ultra", "service_tier": tier, "repetition": 1}
                    name = f"runs/{item['id']}/attempt-001/result.json"
                    if name in existing:
                        result = existing[name]
                        require(result.get("model") == model and result.get("service_tier", "default") == tier
                                and result.get("effort") == "ultra" and result.get("purpose") == "capability" and result.get("attempt") == 1,
                                "Existing capability position differs from the frozen matrix")
                        continue
                    require(not (root / "capability/runs" / item["id"]).exists(), "Existing capability attempt cannot be retried")
                    result = run_trial(server, item, 1, capability=True, prompt_override=prompt)
                    results.append(result)
                    require(result.get("valid") and result.get("delegation_count", 0) > 0 and result.get("accounting_complete"),
                            "Native delegation/terminal/usage capability check failed; formal sampling blocked")
                    require(not server.active, "Capability workflow still running")
            files = {p.relative_to(root / "capability").as_posix(): sha(p) for p in (root / "capability").rglob("*") if p.is_file()}
            for name in files:
                _capability_artifact(root, name)
            named_records = _capability_records(root, files)
            origin = digest(server.fingerprint)
            extra = {}
            if continue_existing:
                origins = {}
                for name, record in named_records.items():
                    provenance = record.get("replay_provenance")
                    origins[name] = {"kind": "historical-paid-offline-replay" if provenance else "new-paid",
                                     "fingerprint_sha256": digest(record.get("fingerprint"))}
                    if provenance:
                        origins[name].update(source_result_sha256=provenance["source_result_sha256"], receipt=provenance["receipt"])
                origin = digest(origins)
                new_count = sum(value["kind"] == "new-paid" for value in origins.values())
                extra = {"schema_version": 2, "attempt_origins": origins, "historical_paid_attempts": 1, "replay_checks": 1,
                         "origin_new_paid_attempts": new_count, "new_paid_attempts": new_count,
                         "paid_ledger_attempts": len(named_records)}
            capability_id = _capability_id(identity, origin, files)
            write(root / "capability/seal.json", {"passed": True, "at": now(), "fingerprint": server.fingerprint,
                  "compatibility_id": identity, "capability_id": capability_id, "origin_fingerprint_sha256": origin,
                  "files_sha256": files, "attempts": extra.get("new_paid_attempts", len(results)), "verified_attempts": len(named_records), "paid_here": True,
                  "formal_samples": 0, **extra,
                  "cost_scope": ("One historical paid capability attempt replayed with zero inference, plus new paid capability attempts; canonical records count each charge once."
                                 if continue_existing else "Separate paid capability verification; see individual whole-tree usage and estimates")})
            require_capability(root, server.fingerprint)
            return {"passed": True, "capability_attempts": len(results), "formal_samples": 0,
                    "paid_here": True, "capability_id": capability_id, "historical_paid_attempts": extra.get("historical_paid_attempts", 0),
                    "replay_checks": extra.get("replay_checks", 0), "verified_attempts": len(named_records)}
        finally:
            server.close()
