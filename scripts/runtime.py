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
        if "priority" in protocol.get("service_tiers", []):
            overrides["features.fast_mode"] = True
        for name in conf.get("mcp_servers", {}):
            # The Codex override parser splits dotted keys literally; TOML-quoted
            # segments create a different server name instead of quoting it.
            require(bool(re.fullmatch(r"[A-Za-z0-9_-]+", name)), "Cannot disable an MCP name with dotted/quoted segments using this runtime override parser")
            overrides[f"mcp_servers.{name}.enabled"] = False
        args = [str(self.binary), "app-server", "--stdio"]
        for k, v in overrides.items():
            args += ["-c", k + "=" + json.dumps(v)]
        work = self.root / "work"
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

    def event(self):
        last_notice = time.perf_counter()
        while True:
            try:
                t, msg = self.q.get(timeout=2)
            except queue.Empty:
                if self.proc.poll() is not None:
                    raise ConnectionError("app-server exited")
                if time.perf_counter() - last_notice >= 30:
                    print(json.dumps({"status": "waiting_for_server", "at": now()}), flush=True)
                    last_notice = time.perf_counter()
                continue
            if msg.get("_eof"):
                raise ConnectionError("app-server transport closed; backend completion unknown")
            if "id" in msg and "method" in msg:
                self.send({"id": msg["id"], "error": {"code": -32601, "message": "Closed-context benchmark: tools/approval requests disabled"}})
                self.counts["unexpected_server_request"] += 1
                continue
            return t, msg

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
        if "service_tiers" in protocol:
            fields += ["serviceTiers", "additionalSpeedTiers", "defaultServiceTier"]
        selected = [{k: m.get(k) for k in fields} for m in models if m.get("model", m.get("id")) in protocol["models"]]
        result = {"at": now(), "fingerprint": self.fingerprint, "models": selected, "unsupported": missing, "stderr_counts": dict(self.counts), "inference_calls": 0}
        write(self.root / "preflight.json", result)
        require(not missing, "Requested model/effort unsupported: " + ", ".join(missing))
        return result

    def run_trial(self, item, attempt):
        root = self.root
        protocol = check_inputs(root)
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
        slot = next(s for s in protocol["schedule"] if s["id"] == item["slot_id"])
        answer_file = directory / "answer.txt"
        if not answer_file.exists():
            answer_file.write_text("", encoding="utf-8")
        result = {"slot_id": slot["id"], "attempt": item["attempt"], "model": slot["model"], "effort": slot["effort"], "repetition": slot["repetition"], "status": "interrupted", "valid": False,
              "prompt_sha256": sha(Path(root) / "input/prompt.txt"), "answer_sha256": sha(answer_file), "warnings": ["Previous local processes ended; remote completion unknown; original attempt retained"],
              "usage": None, **metrics_for_protocol(protocol, None, None, answer=answer_file.read_text(encoding="utf-8"))}
        if "service_tiers" in protocol:
            result["service_tier"] = slot.get("service_tier", "default")
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
