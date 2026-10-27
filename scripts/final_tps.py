"""Optional, offline estimate for the authoritative final answer stream.

This proxy is separate from native response usage and never starts inference.
The vocabulary is checked before tiktoken loads it, so comparison stays offline.
"""
from __future__ import annotations

import hashlib
from importlib import metadata
import os
from pathlib import Path
import sys

from agentic import CANONICAL_FINAL_POLICY, TreeLedger
from core import BenchmarkError, numeric, require, sha


ENCODING = "o200k_base"
TIKTOKEN_VERSION = "0.14.0"
VOCAB_URL = "https://openaipublic.blob.core.windows.net/encodings/o200k_base.tiktoken"
VOCAB_SHA256 = "446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d"
ESTIMATE_FIELDS = (
    "estimated_final_text_tps", "estimated_final_text_tokens", "estimated_final_stream_s",
    "estimated_final_first_delta_s", "estimated_final_last_delta_s", "estimated_final_item_id",
    "estimated_final_tps_method", "estimated_final_tps_reason",
)


def load_proxy_estimator():
    """Load the pinned encoding from a verified local cache; never download it."""
    try:
        import tiktoken
        version = metadata.version("tiktoken")
        regex_version = metadata.version("regex")
    except (ImportError, metadata.PackageNotFoundError) as exc:
        raise BenchmarkError("Final TPS estimate requires the isolated tiktoken 0.14.0 runtime") from exc
    require(version == TIKTOKEN_VERSION, "Final TPS estimate requires tiktoken 0.14.0")
    cache = os.environ.get("TIKTOKEN_CACHE_DIR")
    require(bool(cache), "Set TIKTOKEN_CACHE_DIR to the preloaded local o200k_base vocabulary")
    cache_key = hashlib.sha1(VOCAB_URL.encode()).hexdigest()
    vocabulary = Path(cache) / cache_key
    require(vocabulary.is_file() and sha(vocabulary) == VOCAB_SHA256,
            "Local o200k_base vocabulary is missing or has the wrong SHA256")
    encoding = tiktoken.get_encoding(ENCODING)
    require(encoding.name == ENCODING and sha(vocabulary) == VOCAB_SHA256,
            "Local o200k_base vocabulary changed while loading")
    details = {"method": "local_proxy_tokenization", "tokenizer": "tiktoken", "version": version,
               "regex_version": regex_version, "python_version": sys.version.split()[0],
               "encoding": ENCODING, "vocabulary_sha256": VOCAB_SHA256,
               "model_tokenizer_mapping": "unverified", "implementation_sha256": sha(Path(__file__)),
               "definition": "Full authoritative final text encoded with o200k_base divided by the interval between its first and last nonempty received deltas.",
               "limitation": "Proxy encoding is not verified as any model's actual tokenizer; client buffering and the first chunk affect the observed rate."}
    return encoding, details


def estimate_final_tps(record, row, answer, encoding):
    """Calculate only when the archived completed final matches its full stream."""
    result = {key: None for key in ESTIMATE_FIELDS}

    def unknown(reason):
        result["estimated_final_tps_reason"] = reason
        return result

    if not (row.get("selected") and row.get("valid") and row.get("status") == "completed"):
        return unknown("not_selected_valid_completion")
    # A validated native visible TPS already belongs in its original field.
    if numeric(row.get("visible_decode_tps")) and numeric(row.get("visible_tokens")):
        result["estimated_final_tps_method"] = "native_available"
        return unknown("native_visible_tps_available")
    snapshot = record.get("agentic")
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("events"), list):
        return unknown("native_final_evidence_unavailable")
    try:
        ledger = TreeLedger.from_snapshot(snapshot, final_answer_policy=CANONICAL_FINAL_POLICY)
    except (KeyError, TypeError, ValueError):
        return unknown("native_final_evidence_invalid")
    if ledger.errors or ledger.root_status != "completed":
        return unknown("native_final_evidence_conflicting")
    items = ledger.final_messages
    if len(items) != 1:
        return unknown("authoritative_final_item_count_not_one")
    item = items[0]
    text, deltas = item.get("text"), item.get("deltas")
    if not isinstance(text, str) or not isinstance(deltas, list) or not all(isinstance(x, str) for x in deltas):
        return unknown("final_stream_unavailable")
    normalize = lambda value: value.replace("\r\n", "\n")
    if not text.strip() or normalize(text) != normalize(answer):
        return unknown("authoritative_final_differs_from_answer")
    first, last = item.get("first_delta_s"), item.get("last_delta_s")
    if not numeric(first) or not numeric(last):
        return unknown("final_stream_timestamps_missing")
    if last <= first:
        return unknown("final_stream_interval_not_positive")
    if normalize("".join(deltas)) != normalize(text):
        return unknown("final_stream_incomplete_or_differs")
    try:
        tokens = len(encoding.encode_ordinary(text))
    except (UnicodeError, ValueError):
        return unknown("proxy_tokenization_failed")
    if not tokens:
        return unknown("proxy_token_count_empty")
    span = last - first
    rate = tokens / span
    if not numeric(rate):
        return unknown("proxy_rate_not_finite")
    result.update(estimated_final_text_tps=rate, estimated_final_text_tokens=tokens,
                  estimated_final_stream_s=span, estimated_final_first_delta_s=first,
                  estimated_final_last_delta_s=last, estimated_final_item_id=item["id"],
                  estimated_final_tps_method="o200k_base_proxy", estimated_final_tps_reason=None)
    return result
