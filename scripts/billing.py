"""Presentation-only subscription estimates; never rewrite sealed API costs."""
from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
import math
from pathlib import Path

from core import distribution, numeric, read, usage_issues


SUBSCRIPTION_CREDIT_METRICS = ("estimated_subscription_credits", "observed_subscription_credits", *(
    prefix + key for prefix in ("root_", "child_", "tree_")
    for key in ("estimated_subscription_credits", "observed_subscription_credits")))
SUBSCRIPTION_USD_METRICS = tuple(k.removesuffix("_credits") + "_usd" for k in SUBSCRIPTION_CREDIT_METRICS)
SUBSCRIPTION_METRICS = SUBSCRIPTION_CREDIT_METRICS + SUBSCRIPTION_USD_METRICS
SUBSCRIPTION_LABEL = "估算费用（美元）"


@lru_cache(maxsize=1)
def _pricing():
    return read(Path(__file__).resolve().parent.parent / "assets/subscription-pricing.json")


def billing_context():
    return deepcopy(_pricing())


def format_usd(value, digits=4):
    return f"${value:,.{digits}f}" if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else "—"


def _dollar_equivalents(result, pricing):
    factor = pricing.get("usd_equivalent", {}).get("credits_per_usd")
    for credit_key, usd_key in zip(SUBSCRIPTION_CREDIT_METRICS, SUBSCRIPTION_USD_METRICS):
        value = result[credit_key]
        result[usd_key] = value / factor if numeric(value) and numeric(factor) and factor > 0 else None
    return result


def response_credits(usage, model, tier, pricing=None):
    """Apply the observed response's model/tier to published base token weights."""
    pricing = pricing if pricing is not None else _pricing()
    tier = "priority" if tier == "fast" else tier
    rates = pricing.get("models", {}).get(model)
    factor = pricing.get("speed_multipliers", {}).get(tier)
    if not rates or not numeric(factor) or usage_issues(usage):
        return None
    counts = usage.get("total", usage) if isinstance(usage, dict) else None
    if not isinstance(counts, dict):
        return None
    values = [counts.get(k) for k in ("inputTokens", "cachedInputTokens", "outputTokens")]
    if not all(numeric(v) and int(v) == v for v in values):
        return None
    i, c, o = values
    if c > i or not all(numeric(rates.get(k)) for k in ("input_credits", "cached_credits", "output_credits")):
        return None
    # Output already includes reasoning. Cache writes have no separate Codex fee.
    return ((i - c) * rates["input_credits"] + c * rates["cached_credits"]
            + o * rates["output_credits"]) * factor / 1e6


def subscription_metrics(record, pricing=None):
    """Price known responses individually, including mixed child models/speeds."""
    pricing = pricing if pricing is not None else _pricing()
    result = dict.fromkeys(SUBSCRIPTION_METRICS)
    if record.get("runtime_backend") == "antigravity":
        # OpenAI subscription weights and zero-child assumptions do not apply
        # to native cumulative Antigravity usage.
        return result
    snapshot = record.get("agentic")
    if not isinstance(snapshot, dict):
        tier = record.get("service_tier", record.get("effective", {}).get("serviceTier", "default"))
        value = response_credits(record.get("usage"), record.get("model"), tier, pricing)
        for prefix in ("", "root_", "tree_"):
            for kind in ("estimated", "observed"):
                result[prefix + kind + "_subscription_credits"] = value
        result["child_estimated_subscription_credits"] = 0
        result["child_observed_subscription_credits"] = 0
        return _dollar_equivalents(result, pricing)

    root = snapshot.get("root_thread_id")
    related = {root} if root else set()
    nodes = snapshot.get("nodes", [])
    nodes = list(nodes.values()) if isinstance(nodes, dict) else nodes
    while True:
        expanded = related | {n.get("thread_id") for n in nodes if n.get("parent_thread_id") in related}
        if expanded == related:
            break
        related = expanded
    records = snapshot.get("responses", [])
    records = list(records.values()) if isinstance(records, dict) else records
    records = [r for r in records if r.get("thread_id") in related]
    complete = snapshot.get("accounting_complete") is True
    groups = {"root_": [r for r in records if r.get("thread_id") == root],
              "child_": [r for r in records if r.get("thread_id") != root], "tree_": records}
    for prefix, responses in groups.items():
        values = [response_credits(r.get("usage"), r.get("settings", {}).get("model"),
                                   r.get("settings", {}).get("serviceTier"), pricing) for r in responses]
        known = [v for v in values if v is not None]
        empty_child = prefix == "child_" and len(related) == 1 and snapshot.get("discovery_complete") is True
        result[prefix + "observed_subscription_credits"] = sum(known) if known else (0 if empty_child else None)
        if complete and ((values and len(known) == len(values)) or empty_child):
            result[prefix + "estimated_subscription_credits"] = sum(known)
    result["estimated_subscription_credits"] = result["tree_estimated_subscription_credits"]
    result["observed_subscription_credits"] = result["tree_observed_subscription_credits"]
    return _dollar_equivalents(result, pricing)


def subscription_view(data, records, pricing=None):
    """Add display metrics to a copy, preserving canonical historical summaries."""
    pricing = billing_context() if pricing is None else pricing
    result = deepcopy(data)
    prices = {(r["slot_id"], r["attempt"]): subscription_metrics(r, pricing) for r in records}
    for row in result["rows"]:
        row.update(prices.get((row["slot_id"], row["attempt"]), dict.fromkeys(SUBSCRIPTION_METRICS)))
        row.update(billing_basis="included_subscription_estimate", billing_unit="USD",
                   billing_rules_as_of=pricing["as_of"])
    for cell in result["conditions"]:
        good = [r for r in result["rows"] if r["model"] == cell["model"] and r["effort"] == cell["effort"]
                and r.get("service_tier", "default") == cell.get("service_tier", "default")
                and r["selected"] and r["valid"] and r["status"] == "completed"]
        cell["statistics"].update({k: distribution([r.get(k) for r in good]) for k in SUBSCRIPTION_METRICS})
    if result.get("tier_comparisons"):
        cells = {(c["model"], c["effort"], c["service_tier"]): c for c in result["conditions"]}
        for pair in result["tier_comparisons"]:
            for key in ("estimated_subscription_credits", "estimated_subscription_usd"):
                a, b = [cells[(pair["model"], pair["effort"], t)]["statistics"][key]
                        for t in ("default", "priority")]
                known = a["median"] is not None and b["median"] is not None
                pair["metrics"][key] = {
                    "default": {"n": a["n"], "median": a["median"]},
                    "priority": {"n": b["n"], "median": b["median"]},
                    "fast_minus_standard": b["median"] - a["median"] if known else None,
                    "fast_over_standard": b["median"] / a["median"] if known and a["median"] else None}
    known = [r["estimated_subscription_credits"] for r in result["rows"] if r["estimated_subscription_credits"] is not None]
    known_usd = [r["estimated_subscription_usd"] for r in result["rows"] if r["estimated_subscription_usd"] is not None]
    result.update(billing_context=deepcopy(pricing), known_subscription_credits=sum(known) if known else None,
                  known_subscription_usd=sum(known_usd) if known_usd else None,
                  subscription_coverage_attempts=len(known), subscription_usd_coverage_attempts=len(known_usd))
    return result


def subscription_export(view):
    """A reproducible sidecar beside the unchanged canonical source summary."""
    identity = ("slot_id", "attempt", "model", "effort", "service_tier", "status", "valid", "selected",
                "billing_basis", "billing_unit", "billing_rules_as_of")
    return {"billing_context": view["billing_context"],
            "known_subscription_credits": view["known_subscription_credits"],
            "known_subscription_usd": view["known_subscription_usd"],
            "subscription_coverage_attempts": view["subscription_coverage_attempts"],
            "subscription_usd_coverage_attempts": view["subscription_usd_coverage_attempts"],
            "rows": [{k: r.get(k) for k in (*identity, *SUBSCRIPTION_METRICS)} for r in view["rows"]]}
