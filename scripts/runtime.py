"""Local Codex app-server transport. No auth, raw stderr, or reasoning is logged."""
from __future__ import annotations

import collections
import ctypes
import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import threading
import time
import tomllib
from urllib.parse import urlparse

from core import BenchmarkError, check_inputs, chosen_attempts, digest, home, inside, metrics_for_protocol, now, price_for, read, require, require_sampling_allowed, sha, usage_issues, write


class RpcRejected(BenchmarkError):
    """A matching JSON-RPC error proves that the request was rejected."""
    def __init__(self, method):
        super().__init__(f"RPC rejected: {method}; raw error omitted to avoid leaking URLs or credentials")


def effective_configuration_snapshot(config_response, requirements_response, protocol):
    """Hash all effective layers; expose only typed native controls, never config bodies."""
    require(isinstance(config_response, dict) and isinstance(config_response.get("config"), dict)
            and isinstance(config_response.get("layers"), list), "Effective configuration/layers unavailable")
    require(isinstance(requirements_response, dict) and "requirements" in requirements_response
            and (requirements_response["requirements"] is None or isinstance(requirements_response["requirements"], dict)),
            "Effective requirements unavailable")
    profile = protocol["execution_profile"]
    replacements = {}
    for key, token in (("model_work_root", "<benchmark-work>"), ("run_root", "<benchmark-private-run>"),
                       ("source_root", "<benchmark-source>"), ("scratch_root", "<benchmark-scratch>"),
                       ("probe_root", "<benchmark-probe>"), ("work_cwd", "<benchmark-cwd>")):
        if profile.get(key):
            replacements.setdefault(str(profile[key]), token)
    permission_id = profile.get("permission_profile_id")

    def normalize(value):
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                normalized_key = normalize(key)
                require(normalized_key not in result, "Configuration path normalization collision")
                result[normalized_key] = normalize(item)
            return result
        if isinstance(value, list):
            return [normalize(item) for item in value]
        if not isinstance(value, str):
            return value
        if permission_id and value == permission_id:
            return "<benchmark-permission>"
        for original, token in sorted(replacements.items(), key=lambda item: -len(item[0])):
            # Normalize known physical roots, including embedded paths, with
            # boundaries; unrelated strings and sibling paths retain their meaning.
            for variant in {original, original.replace("\\", "/"), original.replace("/", "\\")}:
                pattern = r"(?<![\w.\\/:-])" + re.escape(variant) + r"(?=$|[\\/\s\"'])"
                value = re.sub(pattern, lambda match: token, value, flags=re.IGNORECASE if os.name == "nt" else 0)
        return value

    config, layers = config_response["config"], config_response["layers"]
    semantic_layers = []
    for layer in layers:
        require(isinstance(layer, dict) and isinstance(layer.get("name"), dict) and isinstance(layer.get("config"), dict),
                "Malformed effective configuration layer")
        # Version is the layer's raw-byte hash; keep that in the raw seal, then
        # recompute semantics after replacing only benchmark-generated paths.
        semantic_layers.append(normalize({key: value for key, value in layer.items() if key != "version"}))
    sources, seen = {}, set()

    def referenced_files(settings, base=None):
        if not isinstance(settings, dict):
            return
        references = [(settings[key], False) for key in ("model_instructions_file", "experimental_compact_prompt_file",
                       "developer_instructions_file", "model_catalog_json") if isinstance(settings.get(key), str)]
        agents = settings.get("agents", {})
        if isinstance(agents, dict):
            references += [(role["config_file"], True) for role in agents.values()
                           if isinstance(role, dict) and isinstance(role.get("config_file"), str)]
        for raw, recurse in references:
            path = Path(raw).expanduser()
            if not path.is_absolute() and base is not None:
                path = base / path
            require(path.is_absolute() and path.is_file(), "Referenced effective configuration source unavailable")
            path = path.resolve()
            identity = normalize(str(path))
            before = sha(path)
            require(identity not in sources or sources[identity] == before, "Conflicting effective configuration sources")
            sources[identity] = before
            if recurse and path not in seen:
                seen.add(path)
                try:
                    nested = tomllib.loads(path.read_text(encoding="utf-8-sig"))
                except (ValueError, OSError):
                    raise BenchmarkError("Referenced agent configuration could not be inspected") from None
                referenced_files(nested, path.parent)
                require(sha(path) == before, "Agent configuration changed during preflight")

    referenced_files(config)
    agents, features = config.get("agents", {}), config.get("features", {})
    # config/read serializes absent optional tables as null on some binaries.
    agents = {} if agents is None else agents
    features = {} if features is None else features
    require(isinstance(agents, dict) and isinstance(features, dict), "Effective native settings malformed")
    safe_agents = {}
    for key in ("enabled", "expose_model_overrides"):
        if isinstance(agents.get(key), bool):
            safe_agents[key] = agents[key]
    for key in ("max_threads", "max_depth", "max_concurrent_threads_per_session"):
        if isinstance(agents.get(key), int) and not isinstance(agents[key], bool):
            safe_agents[key] = agents[key]
    effort = agents.get("default_subagent_reasoning_effort")
    if effort in ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"):
        safe_agents["default_subagent_reasoning_effort"] = effort
    if isinstance(agents.get("default_subagent_model"), str):
        model = agents["default_subagent_model"]
        safe_agents["default_subagent_model"] = model if model in protocol["models"] else {"sha256": digest(model)}
    feature_names = ("multi_agent", "multi_agent_v2", "shell_tool", "exec_tool", "unified_exec", "js_repl", "code_mode",
                     "apps", "plugins", "skills", "memories", "apply_patch", "parallel", "imagegen", "computer_use", "browser_use")
    safe_features = {key: features[key] for key in feature_names if isinstance(features.get(key), bool)}
    v2 = features.get("multi_agent_v2", {})
    if isinstance(v2, dict):
        safe_features["multi_agent_v2"] = {key: value for key, value in v2.items()
            if (key in ("enabled", "disable_native_tools", "wait_agent_enabled", "disable_direct_message",
                        "expose_spawn_agent_model_overrides", "hide_spawn_agent_metadata", "preserve_fork_prefix",
                        "non_code_mode_only") and isinstance(value, bool)) or (key in ("max_concurrent_threads_per_session",
                "min_wait_timeout_ms", "max_wait_timeout_ms", "default_wait_timeout_ms")
                and isinstance(value, int) and not isinstance(value, bool))}
    safe = {"agents": safe_agents, "features": safe_features,
            "default_scope": "Configured effective values shown; omitted values retain the frozen binary/model native defaults."}
    if config.get("web_search") in ("disabled", "cached", "live"):
        safe["web_search"] = config["web_search"]
    semantic = {"config": normalize(config), "layers": semantic_layers,
                "requirements": normalize(requirements_response), "source_files": sources}
    return {"effective_config_sha256": digest(config), "effective_layers_sha256": digest(layers),
            "effective_requirements_sha256": digest(requirements_response),
            "effective_configuration_fingerprint": digest(semantic), "effective_native_settings": safe,
            "effective_source_files_sha256": digest(sources), "effective_source_file_count": len(sources)}


def check_effective_native_configuration(snapshot, models):
    """Reject explicit loss of required native capabilities before any paid turn."""
    settings = snapshot["effective_native_settings"]
    agents, features = settings["agents"], settings["features"]
    require(agents.get("enabled") is not False, "Effective configuration disables native agents")
    require(all(agents.get(key, 1) > 0 for key in ("max_threads", "max_depth", "max_concurrent_threads_per_session")),
            "Effective agent limits prevent delegation")
    require(features.get("shell_tool") is not False and features.get("exec_tool") is not False,
            "Effective configuration disables native source verification tools")
    versions = {model.get("multiAgentVersion", model.get("multi_agent_version")) for model in models}
    if "v1" in versions:
        require(features.get("multi_agent") is not False, "Effective configuration disables V1 delegation")
    if "v2" in versions:
        v2 = features.get("multi_agent_v2", {})
        require(v2 is not False, "Effective configuration disables V2 delegation")
        if isinstance(v2, dict):
            require(v2.get("enabled") is not False and v2.get("disable_native_tools") is not True,
                    "Effective configuration disables V2 native delegation tools")
            require(v2.get("max_concurrent_threads_per_session", 2) > 1,
                    "Effective V2 concurrency leaves no child-agent capacity")


def process_identity(pid):
    """None means gone; unknown is conservatively treated as still running."""
    if not pid:
        return None
    if os.name == "nt":
        from ctypes import wintypes
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.OpenProcess.restype = wintypes.HANDLE
        h = api.OpenProcess(0x1000, False, int(pid))
        if not h:
            return None if ctypes.get_last_error() in (87, 1168) else "unknown"
        try:
            code = wintypes.DWORD()
            api.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            if api.GetExitCodeProcess(h, ctypes.byref(code)) and code.value != 259:
                return None
            times = [wintypes.FILETIME() for _ in range(4)]
            api.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
            if api.GetProcessTimes(h, *(ctypes.byref(t) for t in times)):
                return str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
            return "unknown"
        finally:
            api.CloseHandle.argtypes = [wintypes.HANDLE]
            api.CloseHandle(h)
    try:
        os.kill(int(pid), 0)
        stat = Path(f"/proc/{pid}/stat")
        return stat.read_text().rsplit(")", 1)[1].split()[19] if stat.exists() else "unknown"
    except ProcessLookupError:
        return None
    except PermissionError:
        return "unknown"


def still_running(info, prefix=""):
    current = process_identity(info.get(prefix + "pid"))
    return current is not None and (current == "unknown" or info.get(prefix + "identity") in (None, "unknown", current))


class RunLock:
    def __init__(self, root):
        self.path = Path(root) / "runner-lock.json"

    def __enter__(self):
        if self.path.exists():
            require(not still_running(read(self.path)), "Existing runner is active; keep waiting instead of launching a duplicate")
            self.path.unlink()
        try:
            with self.path.open("x", encoding="utf-8") as f:
                json.dump({"pid": os.getpid(), "identity": process_identity(os.getpid()), "created_at": now()}, f)
        except FileExistsError:
            raise ValueError("Another runner acquired this run") from None
        return self

    def __exit__(self, *args):
        if self.path.exists() and read(self.path).get("pid") == os.getpid():
            self.path.unlink()


def discover_binary(explicit=None):
    if explicit:
        p = Path(explicit).expanduser().resolve()
        require(p.is_file(), "Explicit Codex binary unavailable")
        return p
    candidates = []
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        base = Path(os.environ["LOCALAPPDATA"]) / "OpenAI/Codex/bin"
        # Prefer a currently running app binary over older installed versions.
        command = "Get-CimInstance Win32_Process -Filter \"Name='codex.exe'\" | Select-Object -ExpandProperty ExecutablePath -Unique"
        result = subprocess.run(["powershell", "-NoProfile", "-Command", command], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20)
        for line in result.stdout.splitlines():
            p = Path(line.strip())
            if p.is_file() and p.is_relative_to(base):
                candidates.append(p)
        if base.exists():
            candidates.extend(sorted(base.glob("*/codex.exe"), key=lambda p: p.stat().st_mtime, reverse=True))
    for name in (["codex.exe"] if os.name == "nt" else ["codex"]):
        p = shutil.which(name)
        if p:
            candidates.append(Path(p))
    require(bool(candidates), "Codex executable not found; provide --codex with the app-server capable binary")
    return candidates[0].resolve()


class Server:
    def __init__(self, root, binary=None):
        self.root = Path(root)
        protocol = require_sampling_allowed(self.root)
        self.binary = discover_binary(binary)
        config_file = home() / "config.toml"
        conf = tomllib.loads(config_file.read_text(encoding="utf-8-sig")) if config_file.exists() else {}
        self.provider = conf.get("model_provider", "openai")
        self.host = urlparse(conf.get("model_providers", {}).get(self.provider, {}).get("base_url", "")).hostname
        version = subprocess.run([str(self.binary), "--version"], capture_output=True, text=True, encoding="utf-8", timeout=20)
        require(version.returncode == 0, "Codex version probe failed")
        self.fingerprint = {"binary_sha256": sha(self.binary), "version": version.stdout.strip(), "provider": self.provider, "endpoint_host": self.host,
                            "global_instructions_sha256": sha(home() / "AGENTS.md") if (home() / "AGENTS.md").exists() else None}
        overrides = {"service_tier": "default", "model_reasoning_effort": "max", "web_search": "disabled", "features.shell_tool": False, "features.exec_tool": False,
                     "features.apps": False, "features.plugins": False, "features.multi_agent": False, "features.js_repl": False, "features.code_mode": False}
        project_mode = protocol.get("execution_profile", {}).get("mode") == "project-readonly"
        if project_mode:
            from execution import launch_overrides, check_profile
            from agentic_runtime import environment_fingerprint
            check_profile(protocol["execution_profile"])
            overrides = {"service_tier": "default", "web_search": "disabled", "features.apps": False, "features.plugins": False,
                         "features.memories": False, "memories.use_memories": False, "memories.generate_memories": False}
            overrides.update(launch_overrides(protocol["execution_profile"]))
            overrides.update({"shell_environment_policy.inherit": "all",
                "shell_environment_policy.include_only": ["PATH", "PATHEXT", "SystemRoot", "WINDIR", "COMSPEC", "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS"],
                "shell_environment_policy.set": {"TEMP": protocol["execution_profile"]["scratch_root"], "TMP": protocol["execution_profile"]["scratch_root"], "TMPDIR": protocol["execution_profile"]["scratch_root"]}})
            self.fingerprint["execution"] = environment_fingerprint(conf, protocol)
            self.fingerprint["execution"]["inherited_config_sha256"] = sha(config_file) if config_file.exists() else None
            self.fingerprint["execution"]["binary_companions_sha256"] = {
                path.name: sha(path) for path in sorted(self.binary.parent.glob("codex*.exe")) if path.is_file()}
        if "priority" in protocol.get("service_tiers", []):
            overrides["features.fast_mode"] = True
        for name in conf.get("mcp_servers", {}):
            # The Codex override parser splits dotted keys literally; TOML-quoted
            # segments create a different server name instead of quoting it.
            require(bool(re.fullmatch(r"[A-Za-z0-9_-]+", name)), "Cannot disable an MCP name with dotted/quoted segments using this runtime override parser")
            overrides[f"mcp_servers.{name}.enabled"] = False
        if project_mode:
            self.fingerprint["execution"]["launch_overrides_sha256"] = digest(overrides)
        args = [str(self.binary), "app-server", "--stdio"]
        for k, v in overrides.items():
            if project_mode:
                from execution import toml_value
                value = toml_value(v)
            else:
                value = json.dumps(v)
            args += ["-c", k + "=" + value]
        self.launch_overrides = overrides
        work = Path(protocol["execution_profile"]["work_cwd"]) if project_mode else self.root / "work"
        work.mkdir(exist_ok=True)
        self.proc = subprocess.Popen(args, cwd=work, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True, encoding="utf-8", errors="replace", bufsize=1, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.q = queue.Queue()
        self.seq = 0
        self.pending = []
        self.counts = collections.Counter()
        self.active = False
        threading.Thread(target=self._stdout, daemon=True).start()
        threading.Thread(target=self._stderr, daemon=True).start()
        try:
            self.initialized = self.rpc("initialize", {"clientInfo": {"name": "project_model_benchmark", "title": "Project Model Benchmark", "version": "1.0"}, "capabilities": {"experimentalApi": True}})
            self.send({"method": "initialized", "params": {}})
        except BaseException:
            self.close()
            raise

    def _stdout(self):
        for line in self.proc.stdout:
            try:
                self.q.put((time.perf_counter(), json.loads(line)))
            except json.JSONDecodeError:
                self.counts["non_json_stdout"] += 1
        self.q.put((time.perf_counter(), {"_eof": True}))

    def _stderr(self):
        for line in self.proc.stderr:
            if "ERROR" in line:
                self.counts["stderr_error_lines"] += 1
            elif "WARN" in line:
                self.counts["stderr_warning_lines"] += 1

    def send(self, value):
        self.proc.stdin.write(json.dumps(value, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def event(self, *, poll=False):
        last_notice = time.perf_counter()
        while True:
            try:
                t, msg = self.q.get(timeout=2)
            except queue.Empty:
                if self.proc.poll() is not None:
                    raise ConnectionError("app-server exited")
                if poll:
                    return None
                if time.perf_counter() - last_notice >= 30:
                    print(json.dumps({"status": "waiting_for_server", "at": now()}), flush=True)
                    last_notice = time.perf_counter()
                continue
            if msg.get("_eof"):
                raise ConnectionError("app-server transport closed; backend completion unknown")
            if "id" in msg and "method" in msg:
                self.send({"id": msg["id"], "error": {"code": -32601, "message": "Benchmark does not authorize interactive approval or client dynamic tools"}})
                self.counts["unexpected_server_request"] += 1
                continue
            return t, msg

    def poll_event(self):
        """A bounded polling chunk, never an overall workflow deadline."""
        return self.event(poll=True)

    def rpc(self, method, params):
        self.seq += 1
        request_id = self.seq
        self.send({"id": request_id, "method": method, "params": params})
        while True:
            t, msg = self.event()
            if msg.get("id") == request_id:
                if "error" in msg:
                    raise RpcRejected(method)
                return msg.get("result", {})
            self.pending.append((t, msg))

    def preflight(self, protocol):
        project_mode = protocol.get("execution_profile", {}).get("mode") == "project-readonly"
        require(project_mode or "ultra" not in protocol["efforts"], "Restricted fixed-evidence mode cannot measure native Ultra; prepare a new project-readonly batch")
        models, cursor, seen = [], None, set()
        while True:
            params = {"limit": 100, "includeHidden": True}
            if cursor:
                params["cursor"] = cursor
            page = self.rpc("model/list", params)
            models.extend(page.get("data", []))
            cursor = page.get("nextCursor")
            if not cursor:
                break
            require(cursor not in seen, "Repeated model-list cursor")
            seen.add(cursor)
        available = {m.get("model", m.get("id")): m for m in models}
        missing = []
        for model in protocol["models"]:
            supported = [e["reasoningEffort"] for e in available.get(model, {}).get("supportedReasoningEfforts", [])]
            missing.extend(f"{model}/{e}" for e in protocol["efforts"] if e not in supported)
            if "service_tiers" in protocol:
                tiers = {t.get("id") for t in available.get(model, {}).get("serviceTiers", [])}
                missing.extend(f"{model}/service_tier={tier}" for tier in protocol["service_tiers"] if tier != "default" and tier not in tiers)
        fields = ["id", "model", "displayName", "supportedReasoningEfforts"]
        if project_mode:
            fields += ["multiAgentVersion"]
        if "service_tiers" in protocol:
            fields += ["serviceTiers", "additionalSpeedTiers", "defaultServiceTier"]
        selected = [{k: m.get(k) for k in fields} for m in models if m.get("model", m.get("id")) in protocol["models"]]
        result = {"at": now(), "fingerprint": self.fingerprint, "models": selected, "unsupported": missing, "stderr_counts": dict(self.counts), "inference_calls": 0}
        write(self.root / "preflight.json", result)
        require(not missing, "Requested model/effort unsupported: " + ", ".join(missing))
        if project_mode:
            from execution import permission_probe
            from agentic_runtime import check_native_configuration, project_thread_probe
            configuration = self.rpc("config/read", {"includeLayers": True, "cwd": protocol["execution_profile"]["work_cwd"]})
            requirements = self.rpc("configRequirements/read", {})
            configuration_seal = effective_configuration_snapshot(configuration, requirements, protocol)
            self.fingerprint["execution"].update(configuration_seal)
            check_effective_native_configuration(configuration_seal, selected)
            check_native_configuration(self, protocol, selected)
            result["thread_permission_probe"] = project_thread_probe(self, protocol)
            result["permission_probe"] = permission_probe(self, protocol["execution_profile"], self.fingerprint)
            write(self.root / "preflight.json", result)
        return result

    def run_trial(self, item, attempt):
        root = self.root
        protocol = check_inputs(root)
        if protocol.get("execution_profile", {}).get("mode") == "project-readonly":
            from agentic_runtime import run_trial
            return run_trial(self, item, attempt)
        tier = item.get("service_tier", "default") if "service_tiers" in protocol else "default"
        directory = root / "runs" / item["id"] / f"attempt-{attempt:03d}"
        directory.mkdir(parents=True, exist_ok=False)
        prompt = (root / "input/prompt.txt").read_text(encoding="utf-8")
        instructions = read(root / "input/instructions.json")
        result = {"slot_id": item["id"], "attempt": attempt, "model": item["model"], "effort": item["effort"], "repetition": item["repetition"],
                  "started_at": now(), "status": "starting", "valid": False, "usage": None, "effective": {}, "tool_items": [], "warnings": [],
                  "prompt_sha256": sha(root / "input/prompt.txt"), "fingerprint": self.fingerprint, "actual_gateway_charge": None}
        if "service_tiers" in protocol:
            result["service_tier"] = tier
        inflight = {"slot_id": item["id"], "attempt": attempt, "relative_dir": directory.relative_to(root).as_posix(), "pid": os.getpid(),
                    "identity": process_identity(os.getpid()), "server_pid": self.proc.pid, "server_identity": process_identity(self.proc.pid), "created_at": now()}
        write(root / "inflight.json", inflight)
        messages, deltas = [], []
        first = last = start = None
        elapsed = None
        requests_before = self.counts["unexpected_server_request"]
        try:
            require(self.fingerprint["global_instructions_sha256"] == (sha(home() / "AGENTS.md") if (home() / "AGENTS.md").exists() else None), "Global instructions changed during this batch")
            thread = self.rpc("thread/start", {"model": item["model"], "modelProvider": self.provider, "allowProviderModelFallback": False,
                "serviceTier": tier, "ephemeral": True, "cwd": str(root / "work"), "approvalPolicy": "never", "sandbox": "read-only", "dynamicTools": [],
                "baseInstructions": instructions["base"], "developerInstructions": instructions["developer"],
                "config": {"model_reasoning_effort": item["effort"], "model_reasoning_summary": "none", "service_tier": tier, "project_doc_max_bytes": 0}, "serviceName": "project_model_benchmark"})
            effective = {k: thread.get(k) for k in ["model", "modelProvider", "reasoningEffort", "serviceTier"]}
            effective["instruction_sources_sha256"] = digest(thread.get("instructionSources"))
            result["effective"] = effective
            require(effective["model"] == item["model"] and effective["reasoningEffort"] == item["effort"] and effective["serviceTier"] == tier and effective["modelProvider"] == self.provider, "Effective settings differ from requested settings")
            sources_file = root / "instruction-context.json"
            if sources_file.exists():
                require(read(sources_file)["sha256"] == effective["instruction_sources_sha256"], "Inherited instruction context changed; start a separate batch")
            else:
                write(sources_file, {"sha256": effective["instruction_sources_sha256"]})
            tid = thread["thread"]["id"]
            result["thread_id"] = tid
            self.pending.clear()
            start = time.perf_counter()
            self.active = True
            inflight.update(thread_id=tid, submitted_at=now())
            write(root / "inflight.json", inflight)
            response = self.rpc("turn/start", {"threadId": tid, "model": item["model"], "effort": item["effort"], "summary": "none", "serviceTierForTurn": tier, "input": [{"type": "text", "text": prompt}]})
            turn_id = response["turn"]["id"]
            result["turn_id"] = turn_id
            pending = collections.deque(self.pending)
            self.pending.clear()
            last_notice = time.perf_counter()
            with (directory / "events.jsonl").open("w", encoding="utf-8") as log:
                while True:
                    t, msg = pending.popleft() if pending else self.event()
                    method, params = msg.get("method", "response"), msg.get("params", {})
                    if params.get("threadId") not in (None, tid) or params.get("turnId") not in (None, turn_id):
                        continue
                    safe = {"t_s": t-start, "method": method}
                    if params.get("model") and params["model"] != item["model"]:
                        result["warnings"].append("Observed model differs from requested model")
                    if "service_tiers" in protocol:
                        observed_tier = params.get("serviceTier")
                        if observed_tier is None and isinstance(params.get("turn"), dict):
                            observed_tier = params["turn"].get("serviceTier")
                        if observed_tier is not None:
                            safe["serviceTier"] = observed_tier
                            if observed_tier != tier:
                                result["warnings"].append("Observed service tier differs from requested service tier")
                    if method == "thread/tokenUsage/updated":
                        result["usage"] = params.get("tokenUsage")
                        safe["tokenUsage"] = result["usage"]
                    elif method == "item/agentMessage/delta":
                        delta = params.get("delta", "")
                        if delta:
                            first = t-start if first is None else first
                            last = t-start
                            deltas.append(delta)
                        safe.update(itemId=params.get("itemId"), delta=delta)
                    elif method in ("item/started", "item/completed"):
                        entry = params.get("item", {})
                        kind = entry.get("type")
                        safe.update(item_type=kind, item_id=entry.get("id"), phase=entry.get("phase"))
                        if kind == "agentMessage" and method == "item/completed":
                            messages.append(entry.get("text", ""))
                        elif method == "item/started" and kind not in ("agentMessage", "reasoning", "userMessage"):
                            result["tool_items"].append(kind)
                    elif method == "error":
                        safe["willRetry"] = params.get("willRetry")
                        result.setdefault("runtime_error_events", 0)
                        result["runtime_error_events"] += 1
                    elif method == "turn/completed":
                        result["status"] = params.get("turn", {}).get("status", "unknown")
                        safe["status"] = result["status"]
                        elapsed = t-start
                        self.active = False
                    # Only the allowlisted fields above are persisted; no reasoning payloads/raw errors.
                    log.write(json.dumps(safe, ensure_ascii=False) + "\n")
                    log.flush()
                    if not self.active:
                        break
                    if time.perf_counter() - last_notice >= 30:
                        print(json.dumps({"status": "running", "elapsed_s": time.perf_counter()-start, "visible_characters": sum(map(len, deltas))}), flush=True)
                        last_notice = time.perf_counter()
        except (Exception, KeyboardInterrupt) as exc:
            rejected = isinstance(exc, RpcRejected)
            if rejected:
                self.active = False  # An explicit rejection did not admit a turn.
            result["status"] = "interrupted" if self.active or isinstance(exc, (KeyboardInterrupt, ConnectionError)) else "failed"
            result["error_kind"] = type(exc).__name__
            if isinstance(exc, BenchmarkError):
                result["error_detail"] = str(exc)
            result["warnings"].append("Request explicitly rejected; failure retained and retry requires --retry-failed." if rejected else "Transport/platform interruption; raw error omitted. A previous remote completion may be unknown.")
            if start is not None:
                elapsed = time.perf_counter()-start
        answer = "".join(messages) or "".join(deltas)
        stream = "".join(deltas)
        if stream and stream.replace("\r\n", "\n") != answer.replace("\r\n", "\n"):
            result["warnings"].append("Stream/final answer mismatch")
        if self.counts["unexpected_server_request"] != requests_before:
            result["warnings"].append("Unexpected tool/approval request")
        result["warnings"].extend(usage_issues(result["usage"]))
        (directory / "answer.txt").write_text(answer, encoding="utf-8", newline="\n")
        rates = price_for(read(root / "prices.json"), item["model"], tier)
        result.update(metrics_for_protocol(protocol, result["usage"], elapsed, first, last, answer, rates))
        result.update(first_delta_s=first, last_delta_s=last, completed_at=now(), answer_sha256=sha(directory / "answer.txt"), stderr_counts=dict(self.counts))
        result["valid"] = result["status"] == "completed" and bool(answer.strip()) and not result["tool_items"] and not result["warnings"]
        write(directory / "result.json", result)
        if not self.active:
            (root / "inflight.json").unlink(missing_ok=True)
        return result

    def close(self):
        if self.active and self.proc.poll() is None:
            return  # A live inference is never killed by a local timeout/cleanup rule.
        if self.proc.poll() is None:
            self.proc.stdin.close()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.terminate()  # Only an idle server is stopped.
                self.proc.wait()
        self.active = False


def recover_inflight(root):
    path = Path(root) / "inflight.json"
    if not path.exists():
        return
    item = read(path)
    require(not still_running(item) and not still_running(item, "server_"), "Existing runner/app-server may still be active; wait and inspect its durable state before resuming")
    directory = inside(root, item["relative_dir"])
    if not (directory / "result.json").exists():
        protocol = check_inputs(root)
        slot = ({"id": item["slot_id"], **{k: item[k] for k in ("model", "effort", "service_tier", "repetition")}}
                if item.get("purpose") == "capability" else next(s for s in protocol["schedule"] if s["id"] == item["slot_id"]))
        answer_file = directory / "answer.txt"
        if not answer_file.exists():
            answer_file.write_text("", encoding="utf-8")
        result = {"slot_id": slot["id"], "attempt": item["attempt"], "model": slot["model"], "effort": slot["effort"], "repetition": slot["repetition"], "status": "interrupted", "valid": False,
              "prompt_sha256": sha(Path(root) / "input/prompt.txt"), "answer_sha256": sha(answer_file), "warnings": ["Previous local processes ended; remote completion unknown; original attempt retained"],
              "usage": None, **metrics_for_protocol(protocol, None, None, answer=answer_file.read_text(encoding="utf-8"))}
        if "service_tiers" in protocol:
            result["service_tier"] = slot.get("service_tier", "default")
        if item.get("purpose"):
            result["purpose"] = item["purpose"]
        tree_path = directory / "task-tree.json"
        if protocol.get("execution_profile") and tree_path.exists():
            from agentic import TreeLedger
            ledger = TreeLedger.from_snapshot(read(tree_path))
            ledger.mark_transport_incomplete()
            answer_file.write_text(ledger.final_answer, encoding="utf-8", newline="\n")
            result["answer_sha256"] = sha(answer_file)
            result["agentic"] = ledger.snapshot()
            result["fingerprint"] = item.get("fingerprint")
            result["thread_id"] = item.get("thread_id")
            result.update(ledger.metrics(read(Path(root) / "prices.json"), protocol, None))
            write(tree_path, result["agentic"])
            (directory / "events.jsonl").write_text("".join(json.dumps(e, ensure_ascii=False)+"\n" for e in result["agentic"]["events"]), encoding="utf-8")
        write(directory / "result.json", result)
    path.unlink()


def execute(root, binary=None, resume=False, retry_failed=False, server_factory=Server):
    root = Path(root).resolve()
    protocol = require_sampling_allowed(root)
    require(not protocol.get("historical_import"), "Historical replay is offline-only; prepare a separate retest batch to start new inference")
    require(not (root / "grading/seal.json").exists(), "Scores already sealed; use a separate retest batch")
    with RunLock(root):
        recover_inflight(root)
        existing = chosen_attempts(root)
        require(resume or not existing, "Results already exist; use resume")
        server = server_factory(root, binary)
        try:
            preflight = server.preflight(protocol)
            if protocol.get("execution_profile", {}).get("mode") == "project-readonly":
                from agentic_runtime import require_capability
                require_capability(root, preflight["fingerprint"])
            runtime_file = root / "runtime.json"
            if runtime_file.exists():
                require(read(runtime_file)["fingerprint"] == preflight["fingerprint"], "Runtime changed; start a separate batch instead of mixing environments")
            else:
                write(runtime_file, {"fingerprint": preflight["fingerprint"], "at": now()})
            for slot in protocol["schedule"]:
                previous = existing.get(slot["id"])
                if previous and (previous.get("valid") or not retry_failed):
                    continue
                n = max((int(p.name.split("-")[-1]) for p in (root / "runs" / slot["id"]).glob("attempt-*") if p.is_dir()), default=0) + 1
                result = server.run_trial(slot, n)
                existing[slot["id"]] = result
                progress = {"at": now(), "settled_slots": len(existing), "planned_slots": len(protocol["schedule"]), "valid_completed_slots": sum(r.get("valid", False) for r in existing.values())}
                write(root / "progress.json", progress)
                print(json.dumps(progress), flush=True)
                if server.active or server.proc.poll() is not None:
                    raise ConnectionError("Run interrupted; retained inflight state must be checked before resume")
        finally:
            server.close()


def capability_smoke(root, binary=None, *, continue_existing=False):
    from agentic_runtime import capability_smoke as run_smoke
    return run_smoke(root, binary, continue_existing=continue_existing)


def recover_capability(root, source):
    from agentic_runtime import recover_capability as recover
    return recover(root, source)
