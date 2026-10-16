"""Read-only import/replay of the existing legacy benchmark format. No model calls."""
from __future__ import annotations

import argparse
import ast
import json
import math
from pathlib import Path
import shutil

from core import check_inputs, digest, metrics, now, read, require, sha, write
from reporting import archive, blind, build_report, seal, validate


def legacy_instructions(source):
    values = {}
    for node in ast.walk(ast.parse((source / "runner.py").read_text(encoding="utf-8-sig"))):
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if isinstance(k, ast.Constant) and k.value in ["baseInstructions", "developerInstructions"] and isinstance(v, ast.Constant):
                    values[k.value] = v.value
    require(set(values) == {"baseInstructions", "developerInstructions"}, "Legacy instruction literals unavailable; cannot claim an exact replay")
    return {"base": values["baseInstructions"], "developer": values["developerInstructions"]}


def import_cohort(source, root):
    source, root = Path(source).resolve(), Path(root).resolve()
    require(not root.exists(), "Historical replay output already exists")
    originals = [p for p in source.glob("*.json") if p.is_file()] + [source / "prompt.txt", source / "runner.py"] + list(source.glob("runs/*/result.json")) + list(source.glob("runs/*/answer.txt")) + list(source.glob("runs/*/events.jsonl"))
    before = {str(p): sha(p) for p in originals}
    oldp, oldg = read(source / "protocol.json"), read(source / "gold.json")
    results, mapping, scores = read(source / "results.json"), read(source / "blind-mapping.json"), read(source / "blind-scores.json")
    old_seal = read(source / "evaluation-seal.json")
    require(sha(source / "blind-scores.json") == old_seal["blind_scores_sha256"], "Legacy score seal mismatch")
    require(sha(source / "gold.json") == old_seal["gold_sha256"], "Legacy gold seal mismatch")
    prompt = (source / "prompt.txt").read_text(encoding="utf-8-sig")
    require(bool(prompt.strip()), "Empty legacy prompt")
    import hashlib
    require(hashlib.sha256(prompt.encode()).hexdigest() == oldp["prompt_sha256"], "Legacy transmitted prompt hash mismatch")
    bundle = root / "input"
    (bundle / "evidence").mkdir(parents=True)
    for name in ["prompt.txt", "question.txt", "evidence/S1.txt"]:
        (bundle / name).write_text(prompt, encoding="utf-8", newline="\n")
    shutil.copyfile(source / "gold.json", bundle / "legacy-gold.json")
    shutil.copyfile(source / "protocol.json", bundle / "legacy-protocol.json")
    for name in ["blind-scores.json", "blind-mapping.json", "evaluation-seal.json", "results.json"]:
        shutil.copyfile(source / name, bundle / ("legacy-" + name))
    if (source / "source-manifest.json").exists():
        shutil.copyfile(source / "source-manifest.json", bundle / "legacy-source-manifest.json")
    gold = dict(oldg)
    gold["verification"] = [{"method": "Original benchmark verification retained; no new inference or grading", "result": oldg.get("native_win32_verification", oldg.get("verification", "See legacy gold"))}]
    write(bundle / "gold.json", gold)
    write(bundle / "instructions.json", legacy_instructions(source))
    write(bundle / "case.json", {"title": "历史固定证据测试离线回放", "project": {"name": "历史工程（原路径未记录）", "root": "not recorded in legacy protocol"}, "candidates": [],
        "sources": [{"id": "S1", "file": "evidence/S1.txt", "logical_path": "legacy/frozen-prompt", "ranges": "complete", "source_sha256": sha(source / "prompt.txt")}], "historical_note": "Original topic selection retained; do not invent missing candidate history"})
    models = list(dict.fromkeys(r["model"] for r in results))
    efforts = list(dict.fromkeys(r.get("reasoning_effort", "max") for r in results))
    schedule = [{"id": f"{i:04d}", "model": r["model"], "effort": r.get("reasoning_effort", "max"), "repetition": r["repetition"]} for i, r in enumerate(results, 1)]
    write(bundle / "historical-schedule.json", schedule)
    write(root / "prices.json", {"as_of": oldp["pricing_date"], "sources": oldp["pricing_urls"], "models": oldp["prices"]})
    repetitions = len(results) // (len(models)*len(efforts))
    protocol = {"schema_version": 1, "historical_import": True, "created_at": now(), "title": "历史固定证据测试离线回放", "project": read(bundle / "case.json")["project"], "case_id": digest({"prompt": prompt, "gold": oldg}),
        "models": models, "efforts": efforts, "repetitions": repetitions, "schedule": schedule, "seed": None, "service_tier": "default", "concurrency": 1,
        "input_hashes": {p.relative_to(bundle).as_posix(): sha(p) for p in bundle.rglob("*") if p.is_file()}, "prices_sha256": sha(root / "prices.json"), "parent": None,
        "instructions_sha256": sha(bundle / "instructions.json"), "scope": "Historical offline replay: original cohort order, answers, usage and scores preserved. No new inference or regrading."}
    write(root / "protocol.json", protocol)
    write(root / "preparation-seal.json", {"protocol_sha256": sha(root / "protocol.json")})
    check_inputs(root)
    old_by_new = {}
    metric_checks = 0
    for slot, r in zip(schedule, results):
        directory = root / "runs" / slot["id"] / "attempt-001"
        directory.mkdir(parents=True)
        original = source / "runs" / r["run_id"]
        answer = (original / "answer.txt").read_text(encoding="utf-8")
        (directory / "answer.txt").write_text(answer, encoding="utf-8", newline="\n")
        shutil.copyfile(original / "events.jsonl", directory / "events.jsonl")
        first = r["ttft_visible_s"]
        last = first + r["decode_span_s"] if first is not None and r["decode_span_s"] is not None else None
        calculated = metrics(r.get("usage"), r["elapsed_s"], first, last, answer, oldp["prices"][r["model"]])
        old_keys = {"visible_decode_tps": "visible_decode_tps", "visible_e2e_tps": "visible_e2e_tps", "all_output_e2e_tps": "all_output_e2e_tps", "estimated_standard_api_usd": "estimated_standard_api_usd", "estimated_cold_input_api_usd": "estimated_cold_input_api_usd", "estimated_codex_credits": "estimated_codex_credits", "visible_tokens": "visible_tokens"}
        for new, old in old_keys.items():
            require(math.isclose(calculated[new], r[old], rel_tol=1e-8, abs_tol=1e-8), "Historical metric differs: " + new)
            metric_checks += 1
        value = {"slot_id": slot["id"], "attempt": 1, "legacy_run_id": r["run_id"], "model": slot["model"], "effort": slot["effort"], "repetition": slot["repetition"], "status": r["status"], "valid": r["status"] == "completed" and not r.get("tool_items"),
            "effective": r["effective"], "thread_id": r["thread_id"], "turn_id": r.get("turn_id"), "usage": r["usage"], "tool_items": r.get("tool_items", []), "warnings": r.get("warnings", []), "prompt_sha256": sha(bundle / "prompt.txt"),
            "first_delta_s": first, "last_delta_s": last, "answer_sha256": sha(directory / "answer.txt"), **calculated}
        write(directory / "result.json", value)
        old_by_new[(directory / "result.json").relative_to(root).as_posix()] = r["run_id"]
    blind(root)
    new_mapping = read(root / "grading/private-mapping.json")
    old_anon_by_run = {run_id: anon for anon, run_id in mapping.items()}
    replay_scores = {}
    for anon, relative in new_mapping.items():
        old_score = scores[old_anon_by_run[old_by_new[relative]]]
        replay_scores[anon] = {"points": old_score["points"], "notes": {item["id"]: "历史逐项分数原样保留；原整体备注：" + str(old_score["notes"]) for item in gold["rubric"]}}
    score_path = root / "historical-scores.json"
    write(score_path, replay_scores)
    seal(root, score_path)
    sealed = read(root / "grading/seal.json")
    sealed["evaluator"] = "Historical scores replayed unchanged; no fresh grading or new blind evaluation performed. Original notes retained."
    write(root / "grading/seal.json", sealed)
    write(root / "assessment.json", {"conclusion": "本报告用于验证通用工具能重算历史指标；未重新调用模型或重新评分。", "evidence": [f"{len(results)} 次原始输出、用量、评分及批次顺序保留"], "limitations": ["历史候选选题记录不足，不补造记录；不将不同批次默认合并。"]})
    build_report(root)
    checked = validate(root)
    delivery = archive(root)
    require(before == {str(p): sha(p) for p in originals}, "Source history modified")
    return {"source": str(source), "output": str(root), "runs": len(results), "metric_checks": metric_checks, "validation": checked, "archive": delivery,
            "known_cost_usd": read(root / "summary.json")["known_cost_usd"], "output_tokens": sum(r["usage"]["total"]["outputTokens"] for r in results), "source_files_unchanged": True}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cohorts", nargs="+", required=True)
    p.add_argument("--output", required=True)
    args = p.parse_args()
    output = Path(args.output).resolve()
    require(not output.exists(), "Replay output must be new")
    output.mkdir(parents=True)
    rows = [import_cohort(Path(source), output / f"cohort-{i:02d}") for i, source in enumerate(args.cohorts, 1)]
    result = {"at": now(), "inference_calls": 0, "regraded_answers": 0, "cohorts": rows, "runs": sum(r["runs"] for r in rows), "output_tokens": sum(r["output_tokens"] for r in rows), "known_cost_usd": sum(r["known_cost_usd"] for r in rows), "passed": True}
    write(output / "replay-validation.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
