"""Read-only cross-run comparisons with explicit scoring and sampling provenance."""
from __future__ import annotations

import csv
import io
import json
from pathlib import Path
import shutil
import zipfile

from core import (METRICS, check_inputs, check_sensitive, digest,
                  distribution, inside, now, numeric, read, require, scoring_gold,
                  scoring_weights, sha, write)
from final_tps import ESTIMATE_FIELDS, estimate_final_tps, load_proxy_estimator
from reporting import AGENTIC_FLAGS, AGENTIC_METRICS, check_artifact_sensitive, execution_identity, idle, native_evidence_files, runtime_fingerprint, summarize, validate_records


DIMENSIONS = ("accuracy", "coverage", "readability", "insight")
CANONICAL_METRICS = tuple(dict.fromkeys(
    "estimated_api_usd" if x == "estimated_standard_api_usd" else x for x in METRICS))
STAT_FIELDS = tuple(dict.fromkeys(CANONICAL_METRICS + AGENTIC_METRICS)) + DIMENSIONS + ("quality",)
IDENTITY_FIELDS = ("case", "batch", "origin_batch_id", "execution_mode", "execution_profile_id", "model", "effort", "service_tier", "speed_mode")
PAIR_FIELDS = ("case", "model", "effort", "kind", "standard_condition", "fast_condition",
               "standard_batch", "fast_batch", "standard_valid", "fast_valid",
               "standard_elapsed_median_s", "fast_elapsed_median_s", "elapsed_reduction_pct",
               "speedup", "ttft_reduction_pct", "decode_tps_gain_pct", "cost_ratio")
DEFAULT_LIMITATIONS = [
    "只比较已封存答案，不新增模型采样；单题及小样本差异不能直接推广到其他任务。",
    "质量按题目和评分细则分别展示，不合并不同题目或细则的分数；旧分值保留原尺度，缺失维度为未知。",
    "执行模式、工具策略或源码指纹不同的批次分别展示，禁止将封闭材料与工程代理结果直接合并或配对。",
    "评分属于 AI 辅助评阅；事后重评与先前身份暴露等限制沿用各来源记录。",
    "耗时与首字等待、TPS 均为客户端观测，受排队、网络、思考、输出及缓冲影响。",
    "Fast 收益比较组中位数，不是逐次配对实验；跨批次仅为历史观察，环境与价格可能不同。",
    "费用使用采样时封存的价格和已知用量估算，不是实际网关扣款；未知费用不视为零。",
]


def fingerprint(root):
    """Include every input file, even artifacts outside the runner allowlist."""
    root = Path(root).resolve()
    require(root.is_dir(), "Comparison input must be an existing run directory")
    paths = list(root.rglob("*"))
    require(not any(p.is_symlink() for p in paths), "Comparison inputs must not contain symbolic links")
    return {p.relative_to(root).as_posix(): sha(p) for p in sorted(paths) if p.is_file()}


def canonical_cost(row):
    new, old = row.get("estimated_api_usd"), row.get("estimated_standard_api_usd")
    require(new is None or old is None or new == old, "Conflicting legacy and current cost fields")
    value = new if new is not None else old
    require(value is None or numeric(value), "Invalid estimated cost")
    return value


def assessment_value(value=None):
    if value is None:
        value = {"conclusion": "请按当前题目分别查看质量、耗时与输出表现；本报告不预设模型排名。",
                 "evidence": [], "limitations": []}
    elif isinstance(value, (str, Path)):
        value = read(value)
    require(isinstance(value, dict) and isinstance(value.get("conclusion"), str)
            and all(isinstance(value.get(k, []), list) and all(isinstance(x, str) for x in value.get(k, []))
                    for k in ("evidence", "limitations")), "Invalid assessment format")
    result = {"conclusion": value["conclusion"], "evidence": value.get("evidence", []),
              "limitations": value.get("limitations", [])}
    check_sensitive(json.dumps(result, ensure_ascii=False))
    return result


def _median(condition, key):
    return condition["statistics"].get(key, {}).get("median")


def _ratio(numerator, denominator):
    return numerator / denominator if numerator is not None and denominator not in (None, 0) else None


def _pair(case, standard, fast, kind):
    elapsed_a, elapsed_b = (_median(x, "elapsed_s") for x in (standard, fast))
    ttft_a, ttft_b = (_median(x, "ttft_visible_s") for x in (standard, fast))
    tps_a, tps_b = (_median(x, "visible_decode_tps") for x in (standard, fast))
    elapsed_ratio, ttft_ratio, tps_ratio = (_ratio(elapsed_b, elapsed_a), _ratio(ttft_b, ttft_a), _ratio(tps_b, tps_a))
    return {"case": case["id"], "model": standard["model"], "effort": standard["effort"], "kind": kind,
            "standard_condition": standard["id"], "fast_condition": fast["id"],
            "standard_batch": standard["batch"], "fast_batch": fast["batch"],
            "standard_valid": standard["valid_completed"], "fast_valid": fast["valid_completed"],
            "standard_elapsed_median_s": elapsed_a, "fast_elapsed_median_s": elapsed_b,
            "elapsed_reduction_pct": 100 * (1 - elapsed_ratio) if elapsed_ratio is not None else None,
            "speedup": _ratio(elapsed_a, elapsed_b),
            "ttft_reduction_pct": 100 * (1 - ttft_ratio) if ttft_ratio is not None else None,
            "decode_tps_gain_pct": 100 * (tps_ratio - 1) if tps_ratio is not None else None,
            "cost_ratio": _ratio(_median(fast, "estimated_api_usd"), _median(standard, "estimated_api_usd"))}


def fast_observations(cases):
    """Use same sampling batch first; never resolve historical ambiguity silently."""
    pairs, notes = [], []
    for case in cases:
        keys = dict.fromkeys((c["model"], c["effort"]) for c in case["conditions"])
        for model, effort in keys:
            conditions = [c for c in case["conditions"] if (c["model"], c["effort"]) == (model, effort)]
            used = set()
            for origin in dict.fromkeys(c["origin_batch_id"] for c in conditions):
                a = [c for c in conditions if c["origin_batch_id"] == origin and c["service_tier"] == "default"]
                b = [c for c in conditions if c["origin_batch_id"] == origin and c["service_tier"] == "priority"]
                if len(a) == len(b) == 1:
                    pairs.append(_pair(case, a[0], b[0], "same-batch"))
                    used.update((a[0]["id"], b[0]["id"]))
            a = [c for c in conditions if c["id"] not in used and c["service_tier"] == "default"]
            b = [c for c in conditions if c["id"] not in used and c["service_tier"] == "priority"]
            if len(a) == len(b) == 1:
                pairs.append(_pair(case, a[0], b[0], "historical-observation"))
            elif a and b:
                notes.append({"case": case["id"], "model": model, "effort": effort,
                              "reason": "ambiguous-cross-batch", "standard_candidates": [c["id"] for c in a],
                              "fast_candidates": [c["id"] for c in b],
                              "note": "多个跨批候选保留独立展示；未自动配对。"})
    return pairs, notes


def _paths(run_dirs):
    roots = [Path(p).expanduser().resolve() for p in run_dirs]
    require(bool(roots) and len(set(roots)) == len(roots), "Supply distinct run directories")
    require(not any(a != b and (a.is_relative_to(b) or b.is_relative_to(a)) for a in roots for b in roots),
            "Comparison input directories must not overlap")
    return roots


def _source_id(index, root):
    return f"source-{index:03d}-{digest(str(root))[:8]}"


def build_data(run_dirs, models=None, assessment=None, *, final_tps=None):
    """Reconstruct sealed summaries without source validation/report side effects."""
    roots = _paths(run_dirs)
    selected_models = set(models) if models is not None else None
    require(selected_models is None or bool(selected_models), "Model filter must not be empty")
    cases, sources, rows, failures, details = {}, [], [], [], {}
    identities, found_models = {}, set()
    for source_index, root in enumerate(roots, 1):
        protocol = check_inputs(root)
        idle(root)
        records = validate_records(root)
        expected = summarize(root)
        saved = read(root / "summary.json")
        require({k: v for k, v in saved.items() if k != "generated_at"}
                == {k: v for k, v in expected.items() if k != "generated_at"},
                "Source summary differs from sealed records: " + root.name)
        gold = scoring_gold(root)
        weights = scoring_weights(gold)
        # Group on the complete frozen scoring document: identical labels/weights
        # alone do not establish compatible criteria or reference facts.
        rubric_hash = digest(gold)
        execution_mode, execution_id = execution_identity(protocol, runtime_fingerprint(root))
        case_key = digest({"case_id": protocol["case_id"], "rubric_sha256": rubric_hash,
                           "execution_profile_id": execution_id})
        original_protocol = read(root / "evaluation/source-protocol.json") if protocol.get("offline_regrade") else protocol
        origin = digest(original_protocol)
        source_id = _source_id(source_index, root)
        raw = {r["relative_result"]: r for r in records}
        source_rows = []
        for row in expected["rows"]:
            if selected_models is not None and row["model"] not in selected_models:
                continue
            found_models.add(row["model"])
            relative_result = str(Path(row["answer_link"]).with_name("result.json").as_posix())
            record = raw[relative_result]
            identity = digest({"sampling_protocol": origin, "result": relative_result})
            require(identity not in identities,
                    "Duplicate original attempt across scoring sources; choose one primary scoring version: "
                    + root.name + " / " + identities.get(identity, ""))
            identities[identity] = root.name
            tier = row.get("service_tier", record.get("service_tier", "default"))
            require(tier in ("default", "priority"), "Unknown source service tier")
            require(all(row.get(k) is None or numeric(row[k]) for k in CANONICAL_METRICS if k != "estimated_api_usd"),
                    "Invalid source metric")
            archive_answer = f"answers/{source_id}/{Path(relative_result).parent.as_posix()}/answer.txt"
            item = {k: row.get(k) for k in (*CANONICAL_METRICS, *AGENTIC_METRICS, *AGENTIC_FLAGS, *DIMENSIONS, "quality", "slot_id", "attempt",
                                         "repetition", "status", "valid", "selected", "anonymous_id", "critical_errors", "errors", "omissions")}
            item.update(case=case_key, batch=root.name, source_id=source_id, origin_batch_id=origin,
                        execution_mode=execution_mode, execution_profile_id=execution_id,
                        model=row["model"], effort=row["effort"], service_tier=tier,
                        speed_mode="Fast" if tier == "priority" else "Standard",
                        estimated_api_usd=canonical_cost(row), attempt_identity=identity,
                        answer_link=archive_answer, source_answer_url=inside(root, row["answer_link"]).as_uri(),
                        answer_sha256=sha(inside(root, row["answer_link"])),
                        result_sha256=sha(inside(root, relative_result)),
                        relative_result=relative_result, source_answer_relative=row["answer_link"],
                        remote_completion="unknown" if row["status"] != "completed" else "completed",
                        actual_gateway_charge=row.get("actual_gateway_charge"),
                        dimension_maxima=weights)
            if final_tps is not None:
                answer = inside(root, row["answer_link"]).read_bytes().decode("utf-8-sig") if row.get("selected") and row.get("valid") and row.get("status") == "completed" else ""
                item.update(estimate_final_tps(record, row, answer, final_tps[0]))
            source_rows.append(item)
            rows.append(item)
            if not (item["valid"] and item["status"] == "completed"):
                failures.append(item)
            if row.get("anonymous_id"):
                details[identity] = {"batch": root.name, "anonymous_id": row["anonymous_id"],
                                     "answer_link": archive_answer, "rubric_sha256": rubric_hash,
                                     "scores": read(root / "grading/scores.json")[row["anonymous_id"]]}
        if not source_rows:
            continue
        source = {"id": source_id, "batch": root.name, "label": root.name, "origin_batch_id": origin,
                  "case": case_key, "url": (root / "report.html").as_uri(),
                  "execution_mode": execution_mode, "execution_profile_id": execution_id,
                  "summary_url": (root / "summary.json").as_uri(), "scoring_version": gold.get("scoring_version", 1),
                  "rubric_sha256": rubric_hash, "weights": weights, "evaluation": expected.get("evaluation"),
                  "protocol_sha256": sha(root / "protocol.json"), "scores_sha256": sha(root / "grading/scores.json"),
                  "prices_sha256": sha(root / "prices.json"), "price_as_of": read(root / "prices.json").get("as_of"),
                  "planned_slots": sum(selected_models is None or s["model"] in selected_models
                                       for s in protocol["schedule"]),
                  "valid_answers": sum(r["selected"] and r["valid"] and r["status"] == "completed" for r in source_rows),
                  "attempts": len(source_rows)}
        if expected.get("capability_verification") is not None:
            source["capability_verification"] = expected["capability_verification"]
        sources.append(source)
        case = cases.setdefault(case_key, {"id": case_key, "case_id": protocol["case_id"],
                                            "label": protocol["title"], "title": protocol["title"],
                                            "rubric_sha256": rubric_hash, "scoring_version": gold.get("scoring_version", 1),
                                            "execution_mode": execution_mode, "execution_profile_id": execution_id,
                                            "weights": weights, "dimension_maxima": weights,
                                            "conditions": [], "sources": []})
        case["sources"].append(source)
        for condition in expected["conditions"]:
            if selected_models is not None and condition["model"] not in selected_models:
                continue
            tier = condition.get("service_tier", "default")
            condition_rows = [r for r in source_rows if (r["model"], r["effort"], r["service_tier"])
                              == (condition["model"], condition["effort"], tier)]
            good = [r for r in condition_rows if r["selected"] and r["valid"] and r["status"] == "completed"]
            maxima = {k: weights.get(k) for k in DIMENSIONS}
            fields = (*STAT_FIELDS, "estimated_final_text_tps", "estimated_final_text_tokens", "estimated_final_stream_s") if final_tps is not None else STAT_FIELDS
            statistics = {k: distribution([r.get(k) for r in good]) for k in fields}
            item = {"id": digest({"source": source_id, "model": condition["model"], "effort": condition["effort"], "tier": tier}),
                    "case": case_key, "batch": root.name, "origin_batch_id": origin,
                    "execution_mode": execution_mode, "execution_profile_id": execution_id,
                    "model": condition["model"], "effort": condition["effort"], "service_tier": tier,
                    "speed_mode": "Fast" if tier == "priority" else "Standard", "planned": condition["planned"],
                    "settled": sum(r["selected"] for r in condition_rows), "valid_completed": len(good),
                    "attempts": len(condition_rows), "failed_attempts": sum(not r["valid"] for r in condition_rows),
                    "statistics": statistics, "rows": good, "dimension_maxima": maxima,
                    "critical_errors": sum(r.get("critical_errors") or 0 for r in good) if gold.get("scoring_version", 1) == 2 else None,
                    "error_answers": sum((r.get("errors") or 0) > 0 for r in good) if gold.get("scoring_version", 1) == 2 else None,
                    "omission_answers": sum((r.get("omissions") or 0) > 0 for r in good) if gold.get("scoring_version", 1) == 2 else None,
                    "coverage_deduction_answers": sum(r["coverage"] < weights["coverage"] for r in good) if "coverage" in weights else None,
                    "accuracy_deduction_answers": sum(r["accuracy"] < weights["accuracy"] for r in good),
                    "ceiling_counts": {k: sum(r.get(k) == maximum for r in good) for k, maximum in weights.items()},
                    "known_cost_usd": sum(r["estimated_api_usd"] for r in condition_rows if r["estimated_api_usd"] is not None),
                    "cost_coverage_attempts": sum(r["estimated_api_usd"] is not None for r in condition_rows)}
            observed_delegation = [r for r in good if isinstance(r.get("delegation_occurred"), bool)]
            item.update(delegation_observed_answers=len(observed_delegation),
                        delegated_answers=sum(r["delegation_occurred"] for r in observed_delegation),
                        delegation_rate=sum(r["delegation_occurred"] for r in observed_delegation) / len(observed_delegation) if observed_delegation else None,
                        accounting_complete_attempts=sum(r.get("accounting_complete") is True for r in condition_rows),
                        known_observed_cost_usd=sum(r["tree_observed_api_usd"] for r in condition_rows if r.get("tree_observed_api_usd") is not None),
                        observed_cost_coverage_attempts=sum(r.get("tree_observed_api_usd") is not None for r in condition_rows))
            require(item["valid_completed"] == condition["valid_completed"], "Condition valid count differs from sealed summary")
            case["conditions"].append(item)
    require(bool(rows), "No attempts match the selected models")
    require(selected_models is None or selected_models <= found_models, "Some selected models are absent from all source runs")
    values = list(cases.values())
    pairs, pairing_notes = fast_observations(values)
    result = {"schema_version": 1, "generated_at": now(), "title": "工程模型综合评测",
              "counts": {"independent_questions": len({c["case_id"] for c in values}), "scoring_groups": len(values),
                         "conditions": sum(len(c["conditions"]) for c in values),
                         "valid_answers": sum(r["selected"] and r["valid"] and r["status"] == "completed" for r in rows),
                         "planned_slots": sum(s["planned_slots"] for s in sources), "attempts": len(rows),
                         "invalid_attempts": len(failures), "additional_model_samples_this_analysis": 0},
              "cases": values, "source_batches": sources, "samples": rows, "failures": failures,
              "paired_speed_observations": pairs, "pairing_notes": pairing_notes,
              "assessment": assessment_value(assessment), "limitations": DEFAULT_LIMITATIONS,
              "definitions": {"quality": "各题各评分细则独立展示，不汇总跨题质量或费用加权总分。",
                              "elapsed_s": "客户端完成耗时，单位秒；旧模式到根 turn 完成，工程代理模式到任务树终态；不同执行模式不合并。",
                              "ttft_visible_s": "首个非空可见 delta 的客户端等待时间，单位秒。",
                              "visible_decode_tps": "可见输出 token / 首末非空可见 delta 间隔；受客户端缓冲影响。",
                              "estimated_api_usd": "已封存价格快照估算；兼容旧 estimated_standard_api_usd，未知不计为零。",
                              "output_tokens": "包含推理 token 的用量输出计数；表中均值与图中统计口径分别标注。",
                              "omission_answers": "原评分记录中的遗漏标记；不等于正式覆盖度扣分答案数。",
                              "fast_observations": "同批优先；跨批仅在一对候选唯一时显示历史观察；不逐次配对。"},
              "grading_details": details}
    if final_tps is not None:
        result["final_tps_estimation"] = {**final_tps[1],
            "available_samples": sum(numeric(r.get("estimated_final_text_tps")) for r in rows),
            "selected_valid_samples": sum(r["selected"] and r["valid"] and r["status"] == "completed" for r in rows)}
        result["definitions"]["estimated_final_text_tps"] = "权威最终正文以 o200k_base 代理分词的 token 数除以首末非空片段的客户端到达间隔；模型 tokenizer 映射未核实，非原生计量。"
        result["limitations"] = [*DEFAULT_LIMITATIONS, "正文估算 TPS 使用 o200k_base 代理；模型 tokenizer 映射未核实，首个片段及客户端缓冲可能影响结果，缺失时间戳不估算。"]
    if any(c["execution_mode"] == "project-readonly" for c in values):
        result["definitions"].update(
            answer_ready_s="根最终答案交付完成；可能早于所有子任务结束。",
            root_turn_completed_s="根 turn 结束通知；与最终答案消息交付、全树终态分别记录。",
            workflow_elapsed_s="根请求发出至所有已启动子孙工作终态；idle 线程不必关闭。",
            first_progress_s="首次可见根进度或正文；与最终正文首字分别展示。",
            final_ttft_s="根 final 消息首次非空 delta 等待。",
            delegation_count="观测到的唯一子孙线程数；零委派是有效观测。",
            tree_estimated_api_usd="收齐响应与价格后按实际模型及速度档汇总的全树估算费用。",
            tree_observed_api_usd="已知响应的可计价部分，包含失败尝试；不是完整总费用。",
            visible_decode_tps="工程代理模式仅在根输出可归属时提供；不可用全树输出除根答案流时长。")
    check_sensitive(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return result


def _csv(rows, fields):
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def export_tables(data):
    conditions = []
    for case in data["cases"]:
        for cell in case["conditions"]:
            row = {k: cell.get(k) for k in ("id", *IDENTITY_FIELDS, "planned", "settled", "valid_completed", "attempts", "failed_attempts")}
            row.update({k: cell.get(k) for k in ("delegation_observed_answers", "delegated_answers", "delegation_rate",
                                                "accounting_complete_attempts", "known_cost_usd", "cost_coverage_attempts",
                                                "known_observed_cost_usd", "observed_cost_coverage_attempts")})
            row.update(scoring_version=case["scoring_version"], rubric_sha256=case["rubric_sha256"])
            row.update({k + "_max": case["weights"].get(k) for k in DIMENSIONS})
            for key, statistics in cell["statistics"].items():
                row.update({key + "_" + stat: value for stat, value in statistics.items()})
            conditions.append(row)
    condition_fields = list(dict.fromkeys(k for row in conditions for k in row))
    sample_fields = [*IDENTITY_FIELDS, "source_id", "slot_id", "attempt", "repetition", "status", "valid", "selected",
                     "anonymous_id", "attempt_identity", *DIMENSIONS, "quality", *CANONICAL_METRICS,
                     *AGENTIC_METRICS, *AGENTIC_FLAGS,
                     "critical_errors", "errors", "omissions", "actual_gateway_charge", "remote_completion",
                     "answer_sha256", "result_sha256", "answer_link", "source_answer_url"]
    if data.get("final_tps_estimation"):
        sample_fields += list(ESTIMATE_FIELDS)
    return {"conditions.csv": _csv(conditions, condition_fields),
            "samples.csv": _csv(data["samples"], sample_fields),
            "fast-observations.csv": _csv(data["paired_speed_observations"], PAIR_FIELDS)}


def compare(run_dirs, output, models=None, assessment=None, *, estimate_answer_tps=False):
    roots, output = _paths(run_dirs), Path(output).expanduser().resolve()
    require(not output.exists() and all(not output.is_relative_to(p) and not p.is_relative_to(output) for p in roots),
            "Use a new comparison output directory outside all source runs")
    final_tps = load_proxy_estimator() if estimate_answer_tps else None
    before = {str(p): fingerprint(p) for p in roots}
    data = build_data(roots, models, assessment, final_tps=final_tps)
    template_path = Path(__file__).resolve().parent.parent / "assets/comparison.html"
    template = template_path.read_text(encoding="utf-8")
    require(template.count("__COMPARISON_DATA__") == 1, "Comparison template must contain one data marker")
    output.mkdir(parents=True)
    write(output / "comparison-data.json", data)
    write(output / "assessment.json", data["assessment"])
    write(output / "grading-details.json", data["grading_details"])
    write(output / "source-manifest-before.json", before)
    for name, content in export_tables(data).items():
        (output / name).write_text(content, encoding="utf-8", newline="")
    source_by_id = {_source_id(index, root): root for index, root in enumerate(roots, 1)}
    for source in data["source_batches"]:
        root = source_by_id[source["id"]]
        # Preserve the source material needed to review this comparison offline,
        # without copying unrelated reports, caches or full event streams.
        source_files = ["protocol.json", "preparation-seal.json", "prices.json", "summary.json",
                        "grading/scores.json", "grading/seal.json", "grading/private-mapping.json"]
        source_files += [p.relative_to(root).as_posix() for p in (root / "input").rglob("*") if p.is_file()]
        source_files += [name for name in ("evaluation/rubric.json", "evaluation/manifest.json", "evaluation/source-protocol.json", "permission-probe.json", "permission-probe-seal.json", "runtime.json", "preflight.json", "instruction-context.json")
                         if (root / name).is_file()]
        source_files += [p.relative_to(root).as_posix() for p in native_evidence_files(root)]
        for name in source_files:
            original = inside(root, name)
            check_artifact_sensitive(original, original.is_relative_to(root / "input/source-snapshot") or "scratch" in original.relative_to(root).parts)
            destination = inside(output, f"sources/{source['id']}/{name}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(original, destination)
            require(sha(destination) == before[str(root)][name], "Copied source evidence differs")
    for row in data["samples"]:
        source = inside(source_by_id[row["source_id"]], row["source_answer_relative"])
        check_sensitive(source.read_text(encoding="utf-8-sig"))
        destination = inside(output, row["answer_link"])
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        require(sha(destination) == row["answer_sha256"], "Copied answer differs from source")
        record_source = inside(source_by_id[row["source_id"]], row["relative_result"])
        check_sensitive(record_source.read_text(encoding="utf-8-sig"))
        record_destination = destination.with_name("result.json")
        shutil.copyfile(record_source, record_destination)
        require(sha(record_destination) == row["result_sha256"], "Copied attempt record differs from source")
        for name in ("events.jsonl", "task-tree.json"):
            companion = record_source.with_name(name)
            if companion.is_file() and row["execution_mode"] == "project-readonly":
                check_artifact_sensitive(companion)
                target = record_destination.with_name(name)
                shutil.copyfile(companion, target)
                require(sha(target) == before[str(source_by_id[row["source_id"]])][companion.relative_to(source_by_id[row["source_id"]]).as_posix()],
                        "Copied task-tree ledger differs from source")
    payload = json.dumps(data, ensure_ascii=False, allow_nan=False).replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    (output / "report.html").write_text(template.replace("__COMPARISON_DATA__", payload), encoding="utf-8")
    after = {str(p): fingerprint(p) for p in roots}
    write(output / "source-manifest-after.json", after)
    require(before == after, "Source file set or hashes changed during comparison")
    # Re-read actual bytes rather than trusting in-memory exports.
    require(read(output / "comparison-data.json") == data, "Comparison JSON differs after export")
    for name, content in export_tables(data).items():
        require((output / name).read_bytes() == content.encode("utf-8"), "Comparison CSV differs after export")
    require(payload in (output / "report.html").read_text(encoding="utf-8"), "Comparison HTML data differs after export")
    validation = {"ok": True, "source_files_unchanged": True, "json_csv_consistent": True,
                  "html_embedded_data_consistent": True, "portable_answer_hashes_verified": len(data["samples"]),
                  "counts": data["counts"], "no_new_model_sampling": True,
                  "layout_check": "Not run by Python exporter; use check_comparison.cjs and record any visual review separately.",
                  "archive_bytes_verified": True}
    archive_path = output / "comparison.zip"
    export_stage = "manifest"
    try:
        # The success record must be in the final archive. If any remaining
        # export/check fails, replace it with a failure record before re-raising.
        write(output / "validation.json", validation)
        manifest = {"schema_version": 1, "generated_at": now(), "files_sha256": fingerprint(output),
                    "archive": "comparison.zip", "excludes": ["manifest.json", "comparison.zip"]}
        write(output / "manifest.json", manifest)
        members = {p.relative_to(output).as_posix(): p for p in output.rglob("*") if p.is_file() and p != archive_path}
        export_stage = "archive-write"
        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, path in sorted(members.items()):
                archive.write(path, name)
        export_stage = "archive-verification"
        with zipfile.ZipFile(archive_path) as archive:
            require(set(archive.namelist()) == set(members), "Comparison archive member set differs")
            require(all(archive.read(name) == path.read_bytes() for name, path in members.items()), "Comparison archive bytes differ")
        export_stage = "artifact-manifest-verification"
        require(all(sha(inside(output, name)) == expected_sha for name, expected_sha in manifest["files_sha256"].items()),
                "Comparison artifact changed after manifest creation")
        export_stage = "source-preservation-verification"
        require(before == {str(p): fingerprint(p) for p in roots}, "Source changed while creating comparison archive")
        export_stage = "archive-hash"
        archive_digest = sha(archive_path)
    except BaseException as error:
        validation.update(ok=False, archive_bytes_verified=False, failure_stage=export_stage,
                          failure_type=type(error).__name__)
        if export_stage == "source-preservation-verification":
            validation["source_files_unchanged"] = False
        # An incomplete archive could still contain the optimistic receipt.
        # It is newly created output, so remove it rather than offering it.
        try:
            archive_path.unlink(missing_ok=True)
        except OSError:
            pass
        # Remove the optimistic record first: even a disk-full error while
        # saving diagnostics must not leave a standalone success claim.
        try:
            (output / "validation.json").unlink(missing_ok=True)
            write(output / "validation.json", validation)
        except OSError:
            pass
        raise
    return {"output": str(output), "counts": data["counts"], "source_files_unchanged": True,
            "archive": str(archive_path), "archive_sha256": archive_digest, "inference_started": False}
