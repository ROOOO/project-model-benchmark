"""Allowlisted app-server task-tree ledger; never persist raw responses or reasoning.

Usage is counted only from unique ``rawResponse/completed`` notifications.  Thread
token counters are cumulative (and forks may inherit them), so they are evidence
for cross-checking, never additional billable usage.  A terminal root turn alone
does not prove that its descendants have stopped.
"""
from __future__ import annotations

import copy
import re

from core import metrics as legacy_metrics, numeric, price_for, usage_issues


TOKEN_KEYS = ("inputTokens", "cachedInputTokens", "cacheWriteInputTokens",
              "outputTokens", "reasoningOutputTokens", "totalTokens")
TOKEN_METRICS = {"input_tokens": "inputTokens", "cached_input_tokens": "cachedInputTokens",
                 "cache_write_input_tokens": "cacheWriteInputTokens", "output_tokens": "outputTokens",
                 "reasoning_tokens": "reasoningOutputTokens"}
TERMINAL_TURNS = {"completed", "failed", "interrupted", "cancelled", "terminalObservedIdle"}
IDLE_STATES = {"idle", "notLoaded", "systemError", "completed", "interrupted", "errored", "shutdown", "notFound"}
PLAIN_ITEMS = {"agentMessage", "reasoning", "userMessage", "plan"}
CANONICAL_FINAL_POLICY = "turn-completed-v1"
LEGACY_FINAL_POLICY = "legacy"


def _id(value):
    """Identifiers, unlike free-form error/metadata strings, are safe to archive."""
    return value if isinstance(value, str) and re.fullmatch(r"[\w./:@+-]{1,240}", value) else None


def _status(value):
    value = value.get("type") if isinstance(value, dict) else value
    return _id(value)


def _usage(value):
    if not isinstance(value, dict):
        return None
    # The wire schema explicitly defaults this counter to zero for older senders.
    result = {key: value.get(key, 0 if key == "cacheWriteInputTokens" else None) for key in TOKEN_KEYS}
    return {key: counter if numeric(counter) and int(counter) == counter else None for key, counter in result.items()}


def _full_usage(value):
    return isinstance(value, dict) and all(value.get(key) is not None for key in TOKEN_KEYS) and not usage_issues(value)


def _response_rates(rates, usage):
    """Choose the context band per API response, never from summed tree input."""
    if not isinstance(rates, dict) or not _full_usage(usage):
        return None
    selected = dict(rates)
    if "context_threshold_tokens" in selected:
        threshold = selected["context_threshold_tokens"]
        if not numeric(threshold) or threshold <= 0:
            return None
        if usage["inputTokens"] > threshold:
            for key in ("input_usd", "cached_usd", "output_usd"):
                value = selected.get("long_" + key)
                if not numeric(value):
                    return None
                selected[key] = value
    # An unknown cache-write duration/rate cannot be inferred from input price.
    # With no writes its contribution is exactly zero regardless of duration.
    if usage["cacheWriteInputTokens"] == 0 and "write_usd" not in selected:
        selected["write_usd"] = 0
    return selected


def _settings(value):
    if not isinstance(value, dict):
        return {}
    result = {}
    for dest, names in {"model": ("model",), "modelProvider": ("modelProvider",),
                        "reasoningEffort": ("reasoningEffort", "effort"),
                        "serviceTier": ("serviceTier", "service_tier")}.items():
        for name in names:
            if name in value:
                result[dest] = _id(value[name])
                break
    if result.get("serviceTier") == "fast":
        result["serviceTier"] = "priority"
    if "activePermissionProfile" in value:
        profile = value["activePermissionProfile"]
        result["activePermissionProfile"] = ({"id": _id(profile.get("id")),
            **({"extends": _id(profile.get("extends"))} if "extends" in profile else {})} if isinstance(profile, dict) else None)
    if "approvalPolicy" in value:
        policy = value["approvalPolicy"]
        if isinstance(policy, str) and policy in ("never", "untrusted", "on-request"):
            result["approvalPolicy"] = policy
        elif isinstance(policy, dict) and isinstance(policy.get("granular"), dict):
            result["approvalPolicy"] = {"granular": {key: flag for key, flag in policy["granular"].items()
                if key in ("sandbox_approval", "rules", "skill_approval", "request_permissions", "mcp_elicitations") and isinstance(flag, bool)}}
        else:
            result["approvalPolicy"] = None
    if "cwd" in value:
        cwd = value["cwd"]
        result["cwd"] = cwd if isinstance(cwd, str) and 0 < len(cwd) <= 4096 and not any(ord(char) < 32 for char in cwd) else None
    return result


class TreeLedger:
    """Reduce one fresh trial, including child events arriving before relations.

    ``started_at`` is recorded for audit only; ``observe`` takes elapsed seconds.
    Once root completion is observed, the caller must list/reconcile descendants
    and call ``mark_discovery_complete``.  This class cannot discover threads by
    itself and cannot prove a missing upstream billing receipt.
    """
    def __init__(self, root_thread_id, root_turn_id, root_settings, started_at=0,
                 final_answer_policy=CANONICAL_FINAL_POLICY):
        if final_answer_policy not in (CANONICAL_FINAL_POLICY, LEGACY_FINAL_POLICY):
            raise ValueError("Unknown final answer policy")
        self.final_answer_policy = final_answer_policy
        self.root_final_completions = {}
        self.root_turn_evidence_s = {}
        self.root_thread_id = root_thread_id
        self.root_turn_id = root_turn_id
        self.initial_root_turn_id = root_turn_id
        self.root_turn_order = [root_turn_id] if root_turn_id else []
        self.initial_settings = _settings(root_settings)
        self.started_at = started_at
        self.nodes, self.responses, self.messages, self.tools = {}, {}, {}, {}
        self.events, self.errors = [], []
        self.discovery_complete = False
        self.transport_complete = True
        self.answer_ready_s = None
        self.root_turn_completed_s = None
        self.workflow_finished_s = None
        node = self._node(root_thread_id)
        node["settings"].update(self.initial_settings)
        node["status"] = "active"
        if root_turn_id:
            node["turns"][root_turn_id] = "inProgress"

    def _node(self, tid):
        if tid not in self.nodes:
            self.nodes[tid] = {"thread_id": tid, "parent_thread_id": None, "session_id": None,
                               "parent_source": None, "forked_from_id": None, "status": "unknown", "turns": {}, "settings": {},
                               "settings_history": [], "latest_cumulative_usage": None,
                               "latest_cumulative_usage_t_s": None, "status_observed_s": None,
                               "turn_observed_s": {}, "reconciled_idle_s": None,
                               "activity_seen": False, "reconciled": False}
        return self.nodes[tid]

    def _error(self, message):
        if message not in self.errors:
            self.errors.append(message)

    def _admit_root_turn(self, turn_id, elapsed_s=None):
        node = self._node(self.root_thread_id)
        if (self.final_answer_policy == CANONICAL_FINAL_POLICY and turn_id and numeric(elapsed_s)
                and node["turns"].get(turn_id) not in TERMINAL_TURNS):
            self.root_turn_evidence_s[turn_id] = max(self.root_turn_evidence_s.get(turn_id, elapsed_s), elapsed_s)
        if turn_id and turn_id not in self.root_turn_order:
            if self.root_turn_id is None:
                self.root_turn_id = turn_id
            latest_turn_evidence = (self.root_turn_evidence_s.get(self.root_turn_order[-1])
                                    if self.final_answer_policy == CANONICAL_FINAL_POLICY and self.root_turn_order else None)
            # Late orphan items and a later idle reconciliation do not move the
            # endpoint of an already completed turn past a newer queued turn.
            evidence = ((latest_turn_evidence,) if self.final_answer_policy == CANONICAL_FINAL_POLICY
                        else (self.root_turn_completed_s, node["reconciled_idle_s"]))
            settled_times = [value for value in evidence if numeric(value)]
            if self.root_turn_order and numeric(elapsed_s) and settled_times and elapsed_s <= max(settled_times):
                # A previously unknown old turn can arrive from an RPC queue
                # after a newer answer. Admit it as history, preserving the
                # current answer turn and its delivery/discovery observations.
                self.root_turn_order.insert(len(self.root_turn_order)-1, turn_id)
                node["turns"].setdefault(turn_id, "terminalObservedIdle")
                node["turn_observed_s"].setdefault(turn_id, elapsed_s)
                return
            self.root_turn_order.append(turn_id)
            node["turns"].setdefault(turn_id, "inProgress")
            self.discovery_complete = False
            self.answer_ready_s = None
            self.root_turn_completed_s = None

    def bind_root_turn(self, turn_id):
        """Bind the turn/start receipt, retaining pre-receipt queued observations."""
        turn_id = _id(turn_id)
        if not turn_id:
            self._error("Turn admission receipt missing turn ID")
        elif self.root_turn_id is not None and self.root_turn_id != turn_id:
            self._error("Turn admission receipt conflicts with observed root turn")
        else:
            self._admit_root_turn(turn_id)
        self.events.append({"method": "ledger/rootTurnBound", "t_s": 0, "params": {"turnId": turn_id}})

    @property
    def related_thread_ids(self):
        related = {self.root_thread_id}
        while True:
            expanded = related | {tid for tid, node in self.nodes.items() if node["parent_thread_id"] in related}
            if expanded == related:
                return sorted(related)
            related = expanded

    def _relation(self, child, parent, authoritative=False):
        if not child or not parent or child == parent:
            if child and child == parent:
                self._error("Self-parent thread relationship")
            return
        node = self._node(child)
        self._node(parent)
        if node["parent_thread_id"] not in (None, parent):
            if not authoritative and node["parent_source"] == "thread_metadata":
                return
            if not authoritative or node["parent_source"] == "thread_metadata":
                self._error("Conflicting parent thread relationship")
                return
        cursor, visited = parent, {child}
        while cursor:
            if cursor in visited:
                self._error("Cyclic thread relationship")
                return
            visited.add(cursor)
            cursor = self._node(cursor)["parent_thread_id"]
        node["parent_thread_id"] = parent
        node["parent_source"] = "thread_metadata" if authoritative else (node["parent_source"] or "activity")
        self.discovery_complete = False

    def _apply_settings(self, tid, settings, elapsed, creation=False):
        node = self._node(tid)
        settings = _settings(settings)
        if not settings:
            return
        for entry in node["settings_history"]:
            if entry["t_s"] == elapsed and any(key in entry["settings"] and entry["settings"][key] != value
                                               for key, value in settings.items()):
                self._error("Conflicting settings at same observation time: " + tid)
        node["settings_history"].append({"t_s": elapsed, "settings": settings, "creation": creation})
        node["settings"] = self._settings_at(tid, float("inf"))
        # An earlier notification may be delivered after a response. Recompute
        # its historical identity; a later RPC receipt still cannot fill it.
        for response in self.responses.values():
            if response["thread_id"] == tid:
                response["settings"] = self._settings_at(tid, response["t_s"])

    def _settings_at(self, tid, elapsed):
        """RPC snapshots observed later cannot price notifications queued earlier."""
        result = copy.deepcopy(self.initial_settings) if tid == self.root_thread_id else {}
        same_time, seen = None, {}
        for entry in sorted(self._node(tid)["settings_history"], key=lambda value: value["t_s"]):
            if entry["t_s"] <= elapsed:
                if same_time != entry["t_s"]:
                    same_time, seen = entry["t_s"], {}
                for key, value in entry["settings"].items():
                    if key in seen and seen[key] != value:
                        # Conflicting simultaneous observations are unknown,
                        # rather than dependent on notification arrival order.
                        result[key] = None
                    elif key not in seen:
                        result[key] = value
                    seen.setdefault(key, value)
            elif entry["creation"]:
                # Native creation metadata can fill a missing initial identity.
                for key, value in entry["settings"].items():
                    result.setdefault(key, value)
        return result

    def _apply_status(self, node, status, elapsed):
        if node["status_observed_s"] is None or elapsed >= node["status_observed_s"]:
            node["status"] = status or "unknown"
            node["status_observed_s"] = elapsed

    def _apply_turn(self, node, turn_id, status, elapsed, started=False):
        previous = node["turns"].get(turn_id)
        observed = node["turn_observed_s"].get(turn_id)
        idle_at = node["reconciled_idle_s"]
        if started and (previous in TERMINAL_TURNS or idle_at is not None and elapsed <= idle_at):
            node["turns"].setdefault(turn_id, "terminalObservedIdle")
            node["turn_observed_s"].setdefault(turn_id, elapsed)
            return
        if observed is not None and elapsed < observed:
            return
        node["turns"][turn_id] = status
        node["turn_observed_s"][turn_id] = elapsed
        self._apply_status(node, "active" if status not in TERMINAL_TURNS else "idle", elapsed)

    def _thread_data(self, thread):
        if not isinstance(thread, dict) or not _id(thread.get("id")):
            return None
        safe = {"id": thread["id"]}
        for name in ("parentThreadId", "sessionId", "forkedFromId"):
            if name in thread:
                safe[name] = _id(thread[name])
        if "status" in thread:
            safe["status"] = _status(thread["status"])
        safe.update(_settings(thread))
        return safe

    def _apply_thread(self, thread, elapsed, reconciled=False, creation=False):
        safe = self._thread_data(thread)
        if safe is None:
            return
        tid = safe["id"]
        node = self._node(tid)
        if tid == self.root_thread_id and safe.get("forkedFromId"):
            self._error("Trial root must be a fresh thread, not a fork")
        if safe.get("parentThreadId"):
            self._relation(tid, safe["parentThreadId"], authoritative=True)
        for source, target in (("sessionId", "session_id"), ("forkedFromId", "forked_from_id")):
            if source in safe and safe[source] is not None:
                node[target] = safe[source]
        if safe.get("status"):
            self._apply_status(node, safe["status"], elapsed)
        self._apply_settings(tid, safe, elapsed, creation=creation)
        if reconciled:
            node["reconciled"] = True
            # Metadata status is an authoritative observation of current work.
            # Do not import fork-copied turns/items/usage as newly executed work.
            if safe.get("status") in IDLE_STATES:
                node["reconciled_idle_s"] = max(node["reconciled_idle_s"] or elapsed, elapsed)
                for turn, status in node["turns"].items():
                    if status not in TERMINAL_TURNS and node["turn_observed_s"].get(turn, 0) <= elapsed:
                        node["turns"][turn] = "terminalObservedIdle"
                        node["turn_observed_s"][turn] = elapsed

    def observe(self, message, elapsed_s):
        """Return the archived allowlisted event, or None for unsupported input."""
        if not isinstance(message, dict) or not numeric(elapsed_s):
            return None
        method = message.get("method")
        params = message.get("params", {})
        if not isinstance(params, dict) or not _id(method):
            return None
        safe = {"method": method, "t_s": elapsed_s, "params": {}}
        out = safe["params"]
        tid = _id(params.get("threadId"))
        turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
        turn_id = _id(params.get("turnId")) or _id(turn.get("id"))
        if tid:
            out["threadId"] = tid
            self._node(tid)
        if turn_id:
            out["turnId"] = turn_id
        node = self.nodes.get(tid)
        if method == "thread/started":
            thread = self._thread_data(params.get("thread"))
            if thread is None:
                return None
            out["thread"] = thread
            self._apply_thread(thread, elapsed_s, creation=True)
        elif method == "thread/settings/updated" and node is not None:
            settings = _settings(params.get("threadSettings", {}))
            out["threadSettings"] = settings
            self._apply_settings(tid, settings, elapsed_s)
        elif method == "thread/status/changed" and node is not None:
            out["status"] = _status(params.get("status"))
            self._apply_status(node, out["status"], elapsed_s)
        elif method == "thread/tokenUsage/updated" and node is not None:
            usage = params.get("tokenUsage")
            out["tokenUsage"] = {key: _usage(usage.get(key)) for key in ("total", "last")} if isinstance(usage, dict) else None
            if any(event["method"] == method and event["t_s"] == elapsed_s and
                   event["params"].get("threadId") == tid and event["params"].get("tokenUsage") != out["tokenUsage"]
                   for event in self.events):
                self._error("Conflicting cumulative usage at same observation time: " + tid)
            observed = node["latest_cumulative_usage_t_s"]
            if observed is None or elapsed_s > observed:
                node["latest_cumulative_usage"] = out["tokenUsage"]
                node["latest_cumulative_usage_t_s"] = elapsed_s
        elif method == "rawResponse/completed" and node is not None:
            response_id = _id(params.get("responseId"))
            out.update(responseId=response_id, usage=_usage(params.get("usage")))
            if response_id is None:
                self._error("Response completion missing response ID")
            else:
                if tid == self.root_thread_id:
                    self._admit_root_turn(turn_id, elapsed_s)
                node["activity_seen"] = True
                response = {"response_id": response_id, "thread_id": tid, "turn_id": turn_id,
                            "t_s": elapsed_s, "usage": out["usage"], "settings": self._settings_at(tid, elapsed_s)}
                previous = self.responses.get(response_id)
                if previous is None:
                    self.responses[response_id] = response
                elif any(previous.get(key) != response.get(key) for key in ("thread_id", "turn_id", "usage")):
                    self._error("Conflicting duplicate response ID")
        elif method in ("turn/started", "turn/completed") and node is not None:
            if not turn_id:
                self._error("Turn lifecycle missing turn ID")
            else:
                status = _status(turn.get("status")) or ("inProgress" if method == "turn/started" else "unknown")
                out["turn"] = {"id": turn_id, "status": status}
                if tid:
                    if tid == self.root_thread_id:
                        self._admit_root_turn(turn_id, elapsed_s)
                    node["activity_seen"] = True
                    self._apply_turn(node, turn_id, status, elapsed_s, started=method == "turn/started")
                    if tid == self.root_thread_id and turn_id == self.root_turn_order[-1] and method == "turn/completed":
                        self.root_turn_completed_s = max(self.root_turn_completed_s or elapsed_s, elapsed_s)
                    for item in turn.get("items", []):
                        if isinstance(item, dict):
                            self._item(tid, turn_id, item, "item/completed", elapsed_s)
                    if turn.get("items"):
                        out["turn"]["items"] = [self._safe_item(tid, item) for item in turn["items"] if isinstance(item, dict)]
                    if tid == self.root_thread_id and method == "turn/completed" and status == "completed":
                        self._capture_final_completion(turn_id, turn.get("items", []), elapsed_s)
                    if tid == self.root_thread_id and turn_id == self.root_turn_order[-1] and method == "turn/completed":
                        self.answer_ready_s = max((entry["completed_s"] for entry in self.final_messages if entry["completed_s"] is not None), default=None)
        elif method in ("item/started", "item/completed") and node is not None:
            item = params.get("item")
            if not isinstance(item, dict):
                return None
            out["item"] = self._safe_item(tid, item)
            self._item(tid, turn_id, item, method, elapsed_s)
        elif method == "item/agentMessage/delta" and node is not None:
            item_id = _id(params.get("itemId"))
            out["itemId"] = item_id
            if tid == self.root_thread_id and turn_id and item_id:
                self._admit_root_turn(turn_id, elapsed_s)
                delta = params.get("delta") if isinstance(params.get("delta"), str) else ""
                out["delta"] = delta
                entry = self._message(item_id, turn_id)
                entry["deltas"].append(delta)
                if delta:
                    if entry["first_delta_s"] is None:
                        entry["first_delta_s"] = elapsed_s
                    entry["last_delta_s"] = elapsed_s
            # Child text and tool output are deliberately absent from the ledger.
        elif method == "error":
            out["willRetry"] = params.get("willRetry") if isinstance(params.get("willRetry"), bool) else None
        else:
            # Never archive raw response items, tool arguments/results, errors,
            # instruction bodies, usage_metadata, or private reasoning payloads.
            return None
        self.events.append(safe)
        return copy.deepcopy(safe)

    def _safe_item(self, tid, item):
        safe = {key: _id(item.get(key)) for key in ("type", "id", "phase", "status", "tool", "kind", "agentThreadId", "senderThreadId") if key in item}
        if item.get("type") == "agentMessage" and tid == self.root_thread_id:
            safe["text"] = item.get("text", "") if isinstance(item.get("text"), str) else ""
        for key in ("model", "reasoningEffort"):
            if key in item:
                safe[key] = _id(item[key])
        if item.get("type") == "collabAgentToolCall":
            safe["receiverThreadIds"] = [value for value in (_id(x) for x in item.get("receiverThreadIds", [])) if value]
            safe["agentsStates"] = {key: {"status": _status(value.get("status"))} for key, value in item.get("agentsStates", {}).items() if _id(key) and isinstance(value, dict)}
        return safe

    def _message(self, item_id, turn_id):
        return self.messages.setdefault(item_id, {"id": item_id, "turn_id": turn_id, "phase": None,
                    "text": None, "completed": False, "deltas": [], "first_delta_s": None,
                    "last_delta_s": None, "completed_s": None})

    def _capture_final_completion(self, turn_id, items, elapsed):
        """Only the completed turn confirms which retry stream became its answer."""
        if self.final_answer_policy == LEGACY_FINAL_POLICY:
            return
        previous = self.root_final_completions.get(turn_id)
        accepted, seen = [], set()
        for item in items:
            if not isinstance(item, dict) or item.get("type") != "agentMessage" or item.get("phase") != "final_answer":
                continue
            text, item_id = item.get("text"), _id(item.get("id"))
            if not isinstance(text, str) or not text.strip():
                continue
            if item_id is None or item_id in seen:
                self._error("Invalid or duplicate authoritative final item ID")
                continue
            seen.add(item_id)
            entry = copy.deepcopy(self._message(item_id, turn_id))
            entry.update(phase="final_answer", text=text, completed=True)
            stream = "".join(entry["deltas"])
            if stream and stream.replace("\r\n", "\n") != text.replace("\r\n", "\n"):
                self._error("Final answer stream disagrees with completed message")
            accepted.append(entry)
        if previous is not None and previous["items"]:
            before = [(item["id"], item["text"].replace("\r\n", "\n")) for item in previous["items"]]
            after = [(item["id"], item["text"].replace("\r\n", "\n")) for item in accepted]
            if after and before != after:
                self._error("Conflicting authoritative final completion")
            return
        if previous is not None and not accepted:
            return
        self.root_final_completions[turn_id] = {"t_s": elapsed, "items": accepted}

    def _item(self, tid, turn_id, item, method, elapsed):
        kind, item_id = item.get("type"), _id(item.get("id"))
        node = self._node(tid)
        if kind not in ("reasoning", "userMessage"):
            node["activity_seen"] = True
        if kind == "agentMessage" and tid == self.root_thread_id and turn_id and item_id:
            self._admit_root_turn(turn_id, elapsed)
            entry = self._message(item_id, turn_id)
            phase = item.get("phase")
            if phase in ("commentary", "partial_answer", "final_answer"):
                entry["phase"] = phase
            if method == "item/completed":
                entry["text"] = item.get("text", "") if isinstance(item.get("text"), str) else ""
                if not entry["completed"]:
                    entry["completed_s"] = elapsed
                entry["completed"] = True
                stream = "".join(entry["deltas"])
                if self.final_answer_policy == LEGACY_FINAL_POLICY and entry["phase"] == "final_answer" and stream and stream.replace("\r\n", "\n") != entry["text"].replace("\r\n", "\n"):
                    self._error("Final answer stream disagrees with completed message")
        elif kind not in PLAIN_ITEMS and item_id:
            tool = self.tools.setdefault((tid, item_id), {"thread_id": tid, "item_id": item_id, "type": _id(kind), "status": None})
            if item.get("status") is not None:
                tool["status"] = _status(item["status"])
        if kind == "subAgentActivity":
            child = _id(item.get("agentThreadId"))
            if child:
                self._node(child)
                if item.get("kind") == "started":
                    self._relation(child, tid)
                    self._node(child)["activity_seen"] = True
                    self._apply_settings(child, item, elapsed, creation=True)
                elif item.get("kind") in ("completed", "interrupted"):
                    # Activity on the parent may summarize a previous child turn.
                    # Reconcile the child before treating it as currently idle.
                    self._node(child)["reported_activity"] = item.get("kind")
        elif kind == "collabAgentToolCall":
            sender = _id(item.get("senderThreadId")) or tid
            for value in item.get("receiverThreadIds", []):
                child = _id(value)
                if not child:
                    continue
                if item.get("tool") == "spawnAgent":
                    self._relation(child, sender)
                    self._node(child)["activity_seen"] = True
                    self._apply_settings(child, item, elapsed, creation=True)
                self._node(child)

    def reconcile_thread(self, thread, elapsed_s=None):
        elapsed = elapsed_s if numeric(elapsed_s) else max((event["t_s"] for event in self.events), default=0)
        safe = self._thread_data(thread)
        if safe is None:
            self._error("Reconciliation missing thread ID")
            return None
        self._apply_thread(safe, elapsed, reconciled=True)
        event = {"method": "ledger/threadReconciled", "t_s": elapsed, "params": {"thread": safe}}
        self.events.append(event)
        return copy.deepcopy(event)

    def reconcile_settings(self, thread_id, settings, elapsed_s):
        """Archive an actual warm-resume receipt, with explicit RPC provenance."""
        if not _id(thread_id) or not numeric(elapsed_s):
            self._error("Settings receipt missing thread ID or observation time")
            return None
        safe = _settings(settings)
        self._apply_settings(thread_id, safe, elapsed_s)
        if safe:
            self._node(thread_id)["settings_history"][-1]["source"] = "thread/resume"
        event = {"method": "ledger/threadSettingsObserved", "t_s": elapsed_s,
                 "params": {"threadId": thread_id, "threadSettings": safe, "source": "thread/resume"}}
        self.events.append(event)
        return copy.deepcopy(event)

    def mark_discovery_complete(self, elapsed_s=None):
        """Caller has checked persisted descendants, not merely loaded threads."""
        self.discovery_complete = True
        if self.tree_terminal and numeric(elapsed_s):
            self.workflow_finished_s = elapsed_s
        event = {"method": "ledger/discoveryComplete", "t_s": elapsed_s if numeric(elapsed_s) else 0, "params": {}}
        self.events.append(event)

    def mark_transport_incomplete(self):
        self.transport_complete = False
        self.events.append({"method": "ledger/transportIncomplete", "t_s": 0, "params": {}})

    @property
    def root_status(self):
        return self._node(self.root_thread_id)["turns"].get(self.root_turn_order[-1], "unknown") if self.root_turn_order else "unknown"

    @property
    def root_completed(self):
        turns = self._node(self.root_thread_id)["turns"]
        return bool(self.root_turn_order) and all(turns.get(turn) in TERMINAL_TURNS for turn in self.root_turn_order)

    @property
    def active_threads(self):
        active = []
        for tid in self.related_thread_ids:
            node = self.nodes[tid]
            if node["status"] not in IDLE_STATES or any(status not in TERMINAL_TURNS for status in node["turns"].values()):
                active.append(tid)
        return active

    @property
    def tree_terminal(self):
        return self.root_completed and not self.active_threads

    @property
    def final_messages(self):
        if self.final_answer_policy == CANONICAL_FINAL_POLICY:
            completion = self.root_final_completions.get(self.root_turn_order[-1]) if self.root_turn_order else None
            return completion["items"] if completion is not None else []
        return [entry for entry in self.messages.values() if entry["phase"] == "final_answer" and entry["turn_id"] == self.root_turn_order[-1]] if self.root_turn_order else []

    @property
    def final_answer(self):
        return "\n\n".join(entry["text"] if entry["completed"] else "".join(entry["deltas"]) for entry in self.final_messages)

    @property
    def candidate_answer(self):
        """Unclassified last message for audit; never silently scored as final."""
        unknown = [entry for entry in self.messages.values() if entry["phase"] is None and entry["completed"]]
        return unknown[-1]["text"] if unknown else ""

    def accounting_issues(self):
        issues = list(self.errors)
        if self.final_answer_policy == CANONICAL_FINAL_POLICY and self.root_status == "completed" and not self.final_messages:
            issues.append("Missing authoritative root final-answer completion")
        if not self.transport_complete:
            issues.append("Transport continuity unknown")
        if not self.discovery_complete:
            issues.append("Descendant discovery not reconciled")
        if not self.tree_terminal:
            issues.append("Task tree not terminal")
        for tid in self.related_thread_ids:
            node = self.nodes[tid]
            if not node["reconciled"]:
                issues.append("Thread metadata not reconciled: " + tid)
            responses = [r for r in self.responses.values() if r["thread_id"] == tid]
            if node["activity_seen"] and not responses:
                issues.append("No per-response usage observed for active thread: " + tid)
            if any(not _full_usage(r["usage"]) for r in responses):
                issues.append("Missing or invalid per-response usage: " + tid)
            # A fresh root has no fork baseline; this detects lost completions.
            cumulative = node.get("latest_cumulative_usage")
            total = cumulative.get("total") if isinstance(cumulative, dict) else None
            if tid == self.root_thread_id and responses and _full_usage(total) and all(_full_usage(r["usage"]) for r in responses):
                if any(sum(r["usage"][key] for r in responses) != total[key] for key in TOKEN_KEYS):
                    issues.append("Root response usage disagrees with cumulative counters")
            last = cumulative.get("last") if isinstance(cumulative, dict) else None
            if responses and _full_usage(last):
                latest = max(responses, key=lambda value: value["t_s"])
                if _full_usage(latest["usage"]) and last != latest["usage"]:
                    issues.append("Latest response usage disagrees with last counters: " + tid)
        return issues

    @property
    def accounting_complete(self):
        return not self.accounting_issues()

    def snapshot(self):
        related = set(self.related_thread_ids)
        snapshot = {"schema_version": 1, "root_thread_id": self.root_thread_id,
                "root_turn_id": self.root_turn_id, "initial_settings": self.initial_settings,
                "initial_root_turn_id": self.initial_root_turn_id,
                "root_turn_order": self.root_turn_order,
                "started_at": self.started_at, "nodes": [self.nodes[tid] for tid in sorted(related)],
                "responses": [value for value in self.responses.values() if value["thread_id"] in related],
                "messages": list(self.messages.values()),
                "tools": [value for value in self.tools.values() if value["thread_id"] in related],
                "errors": self.errors, "tree_terminal": self.tree_terminal,
                "accounting_complete": self.accounting_complete, "accounting_issues": self.accounting_issues(),
                "discovery_complete": self.discovery_complete, "events": self.events}
        if self.final_answer_policy != LEGACY_FINAL_POLICY:
            snapshot["final_answer_policy"] = self.final_answer_policy
        return copy.deepcopy(snapshot)

    @classmethod
    def from_snapshot(cls, snapshot, final_answer_policy=None):
        policy = final_answer_policy if final_answer_policy is not None else snapshot.get("final_answer_policy", LEGACY_FINAL_POLICY)
        ledger = cls(snapshot["root_thread_id"], snapshot.get("initial_root_turn_id", snapshot["root_turn_id"]),
                     snapshot["initial_settings"], snapshot.get("started_at", 0), final_answer_policy=policy)
        for event in snapshot.get("events", []):
            method, elapsed = event.get("method"), event.get("t_s", 0)
            if method == "ledger/threadReconciled":
                ledger.reconcile_thread(event.get("params", {}).get("thread"), elapsed)
            elif method == "ledger/threadSettingsObserved":
                ledger.reconcile_settings(event.get("params", {}).get("threadId"), event.get("params", {}).get("threadSettings"), elapsed)
            elif method == "ledger/discoveryComplete":
                ledger.mark_discovery_complete(elapsed)
            elif method == "ledger/transportIncomplete":
                ledger.mark_transport_incomplete()
            elif method == "ledger/rootTurnBound":
                ledger.bind_root_turn(event.get("params", {}).get("turnId"))
            else:
                ledger.observe(event, elapsed)
        return ledger

    def metrics(self, pricing, protocol, elapsed):
        related = set(self.related_thread_ids)
        records = [r for r in self.responses.values() if r["thread_id"] in related]
        groups = {"root": [r for r in records if r["thread_id"] == self.root_thread_id],
                  "child": [r for r in records if r["thread_id"] != self.root_thread_id], "tree": records}
        complete = self.accounting_complete
        result = legacy_metrics(None, elapsed, answer=self.final_answer)
        if "service_tiers" in protocol:
            result["estimated_api_usd"] = result.pop("estimated_standard_api_usd")
        for prefix, values in groups.items():
            missing_usage = any(not _full_usage(value["usage"]) for value in values)
            known_empty = prefix == "child" and len(related) == 1 and self.discovery_complete
            for label, key in TOKEN_METRICS.items():
                counters = [value["usage"][key] for value in values if isinstance(value["usage"], dict) and value["usage"].get(key) is not None]
                observed = sum(counters) if counters else (0 if known_empty else None)
                result[f"{prefix}_observed_{label}"] = observed
                result[f"{prefix}_{label}"] = (observed if observed is not None else 0) if complete and not missing_usage else None
            fees = []
            for value in values:
                settings = value["settings"]
                tier = settings.get("serviceTier")
                rates = price_for(pricing, settings.get("model"), tier) if settings.get("model") and tier else None
                rates = _response_rates(rates, value["usage"])
                fee = legacy_metrics(value["usage"], None, rates=rates)["estimated_standard_api_usd"] if rates else None
                fees.append(fee)
            known_fees = [value for value in fees if value is not None]
            result[f"{prefix}_observed_api_usd"] = sum(known_fees) if known_fees else (0 if known_empty else None)
            result[f"{prefix}_estimated_api_usd"] = sum(fees) if complete and all(value is not None for value in fees) else None
        final_first = min((entry["first_delta_s"] for entry in self.final_messages if entry["first_delta_s"] is not None), default=None)
        progress_first = min((entry["first_delta_s"] for entry in self.messages.values() if entry["phase"] in ("commentary", "partial_answer") and entry["first_delta_s"] is not None), default=None)
        result.update(answer_ready_s=self.answer_ready_s, workflow_elapsed_s=elapsed,
                      root_turn_completed_s=self.root_turn_completed_s,
                      first_progress_s=progress_first, final_ttft_s=final_first,
                      ttft_visible_s=final_first, delegation_count=len(related)-1,
                      delegation_occurred=len(related) > 1, accounting_complete=complete,
                      pricing_complete=complete and result["tree_estimated_api_usd"] is not None,
                      root_output_e2e_tps=None, tree_output_e2e_tps=None)
        if self.answer_ready_s and result["root_output_tokens"] is not None:
            result["root_output_e2e_tps"] = result["root_output_tokens"] / self.answer_ready_s
        if elapsed and result["tree_output_tokens"] is not None:
            result["tree_output_e2e_tps"] = result["tree_output_tokens"] / elapsed
        # Legacy unprefixed tokens/cost refer to the entire workflow in this mode.
        # A multi-response root does not expose final-text-only token attribution;
        # leave decode TPS unknown instead of dividing whole-tree output by it.
        for label in TOKEN_METRICS:
            result[label] = result["tree_" + label]
        result["all_output_e2e_tps"] = result["tree_output_e2e_tps"]
        result["estimated_api_usd" if "service_tiers" in protocol else "estimated_standard_api_usd"] = result["tree_estimated_api_usd"]
        return result
