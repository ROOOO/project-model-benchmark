"""Anonymous scoring, portable reports, validation and allowlisted archives."""
from __future__ import annotations

import csv
import html
from html.parser import HTMLParser
import io
import json
import math
from pathlib import Path
import random
import re
import secrets
import shutil
import subprocess
from urllib.parse import quote, unquote, urlparse
import zipfile

from core import DIMENSION_LABELS, METRICS, all_attempts, check_inputs, check_sensitive, chosen_attempts, digest, distribution, inside, metrics, now, read, require, scoring_gold, scoring_weights, sha, usage_issues, validate_scores, write
from runtime import still_running


CSV_FIELDS = list(dict.fromkeys(["slot_id", "attempt", "model", "effort", "repetition", "status", "valid", "selected", "accuracy", "readability", "quality", "anonymous_id", "actual_gateway_charge", *METRICS, "answer_link"]))
CSV_V2_FIELDS = CSV_FIELDS[:9] + ["coverage", "insight"] + CSV_FIELDS[9:] + ["scoring_version", "accuracy_max", "coverage_max", "insight_max", "readability_max", "critical_errors", "errors", "omissions"]


def idle(root):
    root = Path(root)
    lock = root / "runner-lock.json"
    require(not lock.exists() or not still_running(read(lock)), "Runner is still active")
    active = root / "inflight.json"
    require(not active.exists(), "Unresolved inflight attempt; inspect/recover it with resume before grading/reporting")


def settled_valid_answers(root, protocol):
    chosen = chosen_attempts(root)
    require(set(chosen) == {s["id"] for s in protocol["schedule"]}, "Sampling not settled; finish/resume before anonymizing")
    valid = {r["relative_result"]: r for r in chosen.values() if r.get("valid") and r.get("status") == "completed"}
    require(bool(valid), "No valid completed answers to grade")
    return valid


def check_packet(root):
    """Bind anonymous grading to the frozen inputs and every selected valid answer."""
    root = Path(root)
    protocol = check_inputs(root)
    valid = settled_valid_answers(root, protocol)
    packet = root / "grading/packet"
    mapping = read(root / "grading/private-mapping.json")
    require(isinstance(mapping, dict) and bool(mapping), "Invalid scoring mapping")
    require(all(isinstance(anon, str) and re.fullmatch(r"Q(?:[0-9a-f]{8}-)?[0-9]{3,}", anon) and isinstance(relative, str)
                for anon, relative in mapping.items()), "Invalid scoring mapping IDs/paths")
    require(len(set(mapping.values())) == len(mapping), "Duplicate answer in scoring mapping")
    require(set(mapping.values()) == set(valid), "Scoring mapping differs from current valid answers; use blind --refresh before scoring again")
    for name in ["prompt.txt", "gold.json"]:
        source = inside(root / "input", name)
        destination = inside(packet, name)
        require(destination.is_file() and sha(destination) == sha(source), "Scoring input differs from frozen input: " + name)
    if protocol.get("offline_regrade"):
        require((packet / "rubric.json").is_file() and sha(packet / "rubric.json") == sha(root / "evaluation/rubric.json"), "Scoring rubric differs from frozen revision")
    else:
        require(not (packet / "rubric.json").exists(), "Unexpected scoring rubric override")
    answers = packet / "answers"
    require(answers.is_dir() and {p.name for p in answers.iterdir()} == {anon + ".txt" for anon in mapping}, "Anonymous answer files differ from scoring mapping")
    for anon, relative in mapping.items():
        record = valid[relative]
        source = inside(root, relative).parent / "answer.txt"
        actual = sha(source)
        require(actual == record.get("answer_sha256"), "Raw answer differs from recorded hash")
        require(record.get("prompt_sha256") == protocol["input_hashes"]["prompt.txt"], "Recorded prompt differs from frozen input")
        destination = inside(answers, anon + ".txt")
        require(destination.is_file() and sha(destination) == actual, "Anonymous answer differs from original answer")
    return mapping


def blind(root, refresh=False):
    root = Path(root).resolve()
    p = check_inputs(root)
    idle(root)
    valid = settled_valid_answers(root, p)
    grading = root / "grading"
    packet = grading / "packet"
    mapping_path = grading / "private-mapping.json"
    archived = None
    if refresh:
        require(not (grading / "seal.json").exists(), "Scores already sealed; refresh cannot replace sealed grading")
        if grading.exists():
            # Both resolved endpoints must remain in this run before moving the
            # complete, unsealed packet; user grading work is never deleted.
            source = inside(root, "grading")
            stamp = re.sub(r"[^0-9TZ]", "", now())
            archived = inside(root, f"grading-history/{stamp}-{secrets.token_hex(4)}")
            archived.parent.mkdir(parents=True, exist_ok=True)
            require(not archived.exists(), "Archived grading destination already exists")
            source.rename(archived)
    if mapping_path.exists():
        mapping = check_packet(root)
        if (grading / "seal.json").exists():
            score_data(root)
        return {"packet": str(packet), "answers": len(mapping), "mapping_not_displayed": True, "archived_grading": None}
    else:
        require(not grading.exists() or not any(grading.iterdir()), "Incomplete scoring mapping/packet; use blind --refresh to retain it and create a fresh packet")
        order = list(valid)
        random.SystemRandom().shuffle(order)
        packet_id = secrets.token_hex(4)
        mapping = {f"Q{packet_id}-{i:03d}": relative for i, relative in enumerate(order, 1)}
        write(mapping_path, mapping)
    (packet / "answers").mkdir(parents=True, exist_ok=True)
    for name in ["prompt.txt", "gold.json"]:
        source, destination = root / "input" / name, packet / name
        if destination.exists():
            require(sha(source) == sha(destination), "Scoring input changed")
        else:
            shutil.copyfile(source, destination)
    gold = scoring_gold(root)
    rubric = gold["rubric"]
    if p.get("offline_regrade"):
        shutil.copyfile(root / "evaluation/rubric.json", packet / "rubric.json")
    template = {}
    for anon, relative in mapping.items():
        source = inside(root, relative).parent / "answer.txt"
        destination = packet / "answers" / f"{anon}.txt"
        if destination.exists():
            require(sha(source) == sha(destination), "Anonymous answer changed")
        else:
            shutil.copyfile(source, destination)
        template[anon] = {"points": {r["id"]: None for r in rubric}, "notes": {r["id"]: "" for r in rubric}}
        if gold.get("scoring_version", 1) == 2:
            template[anon].update(evidence={r["id"]: [] for r in rubric}, issues=[])
    if not (packet / "scores-template.json").exists():
        write(packet / "scores-template.json", template)
    (packet / "instructions.txt").write_text("只读取本 packet 的题面、gold 与匿名答案。逐项填写 points 和 notes；null 不是分数，不存在默认分数。评分前不读模型、性能、映射或运行结果。完成后用 seal 封存。这是 AI 辅助评分，不是人工偏好研究。\n", encoding="utf-8")
    if gold.get("scoring_version", 1) == 2:
        with (packet / "instructions.txt").open("a", encoding="utf-8") as f:
            f.write("V2：准确性50/任务覆盖20/边界洞察10/可读性20。若有rubric.json，以其中细则评分，gold保留原始事实。每项填写evidence原文摘录；遗漏可留空摘录但需注明。issues逐项区分error/omission及critical/minor，错误需原文。错误扣准确性、遗漏扣覆盖，不重复扣；不以篇幅或术语数量奖励广度。\n")
            if p.get("offline_regrade"):
                f.write("本次是事后补评，评分者此前见过模型身份。新编号与性能隐藏不能消除先验印象，不宣称完全独立盲评。\n")
    check_packet(root)
    return {"packet": str(packet), "answers": len(mapping), "mapping_not_displayed": True, "archived_grading": str(archived) if archived else None}


def seal(root, scores_path):
    root = Path(root)
    check_inputs(root)
    idle(root)
    grading = root / "grading"
    require(not (grading / "seal.json").exists(), "Scores already sealed")
    mapping = check_packet(root)
    scores = read(scores_path)
    gold = scoring_gold(root)
    validate_scores(scores, mapping, grading / "packet", gold)
    check_sensitive(json.dumps(scores, ensure_ascii=False))
    write(grading / "scores.json", scores)
    hashes = {p.relative_to(grading / "packet").as_posix(): sha(p) for p in (grading / "packet").rglob("*") if p.is_file() and p.name != "scores-template.json"}
    value = {"at": now(), "evaluator": "AI-assisted rubric scoring; model/effort and performance labels hidden. Not independent human preference research.",
             "scores_sha256": sha(grading / "scores.json"), "mapping_sha256": sha(grading / "private-mapping.json"), "packet_hashes": hashes, "answers_scored": len(scores)}
    if gold.get("scoring_version", 1) == 2:
        value.update(scoring_version=2, scoring_weights=scoring_weights(gold))
        if check_inputs(root).get("offline_regrade"):
            value["evaluator"] = "事后新增细则；评分者此前见过模型身份。重新匿名并隐藏性能，不是完全独立盲评。AI-assisted, no new inference."
    write(grading / "seal.json", value)
    return {"sealed_answers": len(scores)}


def score_data(root, partial=False):
    root = Path(root)
    grading = root / "grading"
    if not (grading / "seal.json").exists():
        require(partial, "Anonymous scores not sealed; use --partial only for an explicitly incomplete metrics report")
        if (grading / "private-mapping.json").exists():
            check_packet(root)
        return {}, {}
    seal_value = read(grading / "seal.json")
    require(sha(grading / "scores.json") == seal_value["scores_sha256"] and sha(grading / "private-mapping.json") == seal_value["mapping_sha256"], "Sealed scores or mapping changed")
    for name, expected in seal_value["packet_hashes"].items():
        require(sha(inside(grading / "packet", name)) == expected, "Scoring packet changed after sealing")
    mapping, scores = check_packet(root), read(grading / "scores.json")
    gold = scoring_gold(root)
    rubric = validate_scores(scores, mapping, grading / "packet", gold)
    weights = scoring_weights(gold)
    if gold.get("scoring_version", 1) == 2:
        require(seal_value.get("scoring_version") == 2 and seal_value.get("scoring_weights") == weights, "Sealed scoring version/weights differ")
    by_result = {}
    for anon, relative in mapping.items():
        points = scores[anon]["points"]
        values = {dimension: sum(points[r["id"]] for r in rubric if r["dimension"] == dimension) for dimension in weights}
        by_result[relative] = values | {"quality": sum(values.values()), "anonymous_id": anon}
        if gold.get("scoring_version", 1) == 2:
            issues = scores[anon]["issues"]
            by_result[relative].update(critical_errors=sum(x["kind"] == "error" and x["severity"] == "critical" for x in issues),
                                      errors=sum(x["kind"] == "error" for x in issues), omissions=sum(x["kind"] == "omission" for x in issues))
    return by_result, scores


def summarize(root, partial=False):
    root = Path(root)
    p = check_inputs(root)
    scores, detailed_scores = score_data(root, partial)
    gold = scoring_gold(root)
    version, weights = gold.get("scoring_version", 1), scoring_weights(gold)
    attempts, chosen = all_attempts(root), chosen_attempts(root)
    rows, answers = [], []
    for r in attempts:
        row = {k: r.get(k) for k in ["slot_id", "attempt", "model", "effort", "repetition", "status", "valid", "actual_gateway_charge", *METRICS]}
        row["selected"] = chosen.get(r["slot_id"], {}).get("relative_result") == r["relative_result"]
        row.update(scores.get(r["relative_result"], {}))
        if version == 2:
            row.update(scoring_version=2, **{k + "_max": v for k, v in weights.items()})
        row["answer_link"] = (Path(r["relative_result"]).parent / "answer.txt").as_posix()
        rows.append(row)
        answers.append({"label": f"{r['model']} · {r['effort']} · r{r['repetition']} · attempt {r['attempt']} ({r['status']})", "text": inside(root, row["answer_link"]).read_text(encoding="utf-8"), "link": row["answer_link"]})
    cells = []
    for model in p["models"]:
        for effort in p["efforts"]:
            all_rows = [r for r in rows if r["model"] == model and r["effort"] == effort]
            selected = [r for r in all_rows if r["selected"]]
            good = [r for r in selected if r["valid"] and r["status"] == "completed"]
            cells.append({"model": model, "effort": effort, "planned": p["repetitions"], "settled": len(selected), "valid_completed": len(good), "attempts": len(all_rows), "failed_attempts": sum(not r["valid"] for r in all_rows),
                          "statistics": {k: distribution([r.get(k) for r in good]) for k in METRICS + list(weights) + ["quality"]}})
            if version == 2:
                cells[-1].update(critical_errors=sum(r.get("critical_errors", 0) for r in good),
                                 error_answers=sum(r.get("errors", 0) > 0 for r in good), omission_answers=sum(r.get("omissions", 0) > 0 for r in good))
    success = sum(c["valid_completed"] for c in cells)
    result = {"schema_version": 1, "generated_at": now(), "title": p["title"], "case_id": p["case_id"], "project": p["project"], "batch": root.name, "parent": p.get("parent"),
            "planned": len(p["schedule"]), "settled": len(chosen), "valid_completed": success, "attempt_count": len(attempts), "all_planned_successful": success == len(p["schedule"]),
            "scores_sealed": (root / "grading/seal.json").exists(), "scope": p["scope"], "conditions": cells, "rows": rows, "answers": answers,
            "known_cost_usd": sum(r["estimated_standard_api_usd"] for r in rows if r.get("estimated_standard_api_usd") is not None), "cost_coverage_attempts": sum(r.get("estimated_standard_api_usd") is not None for r in rows),
            "observed_total_elapsed_s": sum(r["elapsed_s"] for r in rows if r.get("elapsed_s") is not None)}
    if version == 2:
        result.update(schema_version=2, scoring_version=2, scoring_weights=weights,
                      grading_details={row["answer_link"]: detailed_scores[row["anonymous_id"]] for row in rows if row.get("anonymous_id")})
        if p.get("offline_regrade"):
            manifest = read(root / "evaluation/manifest.json")
            previous = read(root / "evaluation/previous-summary.json")
            result["evaluation"] = {k: manifest[k] for k in ["mode", "source_batch", "post_hoc", "evaluator_previously_saw_model_identity", "note"]}
            result["previous_evaluation"] = {k: previous.get(k) for k in ["batch", "scoring_version", "scoring_weights", "conditions", "rows"]}
            result["previous_evaluation"]["scoring_version"] = previous.get("scoring_version", 1)
            result["previous_evaluation"]["scoring_weights"] = previous.get("scoring_weights", {"accuracy": 70, "readability": 30})
            old_rows = {r["answer_link"]: r for r in previous["rows"]}
            require(set(old_rows) == {r["answer_link"] for r in rows}, "Regrade differs from source attempt set")
            for row in rows:
                old = old_rows[row["answer_link"]]
                require(all(row.get(k) == old.get(k) for k in METRICS + ["slot_id", "attempt", "model", "effort", "repetition", "status", "valid", "selected", "actual_gateway_charge"]), "Regrade metrics/conditions differ from original")
    return result


def markdown(text):
    """Small escaped renderer: evidence never becomes executable HTML."""
    def inline(s):
        s = html.escape(s)
        s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
        return re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
    output, code, table = [], None, False
    fence_char, fence_length = None, 0
    for line in text.splitlines():
        fence = re.match(r"^\s*(`{3,}|~{3,})(.*)$", line)
        if fence and code is None:
            code, fence_char, fence_length = [], fence[1][0], len(fence[1])
            continue
        if code is not None:
            if fence and fence[1][0] == fence_char and len(fence[1]) >= fence_length and not fence[2].strip():
                output.append("<pre><code>" + html.escape("\n".join(code)) + "</code></pre>")
                code = None
            else:
                code.append(line)
            continue
        if line.strip().startswith("|") and line.strip().endswith("|"):
            if not table:
                output.append('<div class="tablewrap"><table>')
                table = True
            if not re.fullmatch(r"[|\s:\-]+", line):
                output.append("<tr>" + "".join("<td>" + inline(x.strip()) + "</td>" for x in line.strip().strip("|").split("|")) + "</tr>")
            continue
        if table:
            output.append("</table></div>")
            table = False
        heading = re.match(r"^#{1,6}\s+(.+)", line)
        if heading:
            output.append("<h3>" + inline(heading[1]) + "</h3>")
        elif line.strip():
            output.append("<p>" + inline(line) + "</p>")
    if code is not None:
        output.append("<pre>" + html.escape("\n".join(code)) + "</pre>")
    if table:
        output.append("</table></div>")
    return "\n".join(output)


def fmt(x, digits=2):
    return "—" if x is None else f"{x:,.{digits}f}"


def condition_table(cells, weights=None):
    if weights is None:
        weights = {"accuracy": 50, "coverage": 20, "insight": 10, "readability": 20} if cells and "coverage" in cells[0]["statistics"] else {"accuracy": 70, "readability": 30}
    heads = ["组合", "有效/计划", "尝试/失败", *[DIMENSION_LABELS[k] + " /" + str(v) for k, v in weights.items()], "总分 /100", "耗时中位 s", "TTFT 中位 s", "可见 TPS 中位", "输出 token 均值", "费用均值 USD"]
    if "coverage" in weights:
        heads += ["关键错误数", "有错误/遗漏答案数"]
    body = []
    for c in cells:
        s = c["statistics"]
        values = [html.escape(c["model"] + " · " + c["effort"]), f"{c['valid_completed']}/{c['planned']}", f"{c['attempts']}/{c['failed_attempts']}", *[fmt(s[k]["mean"]) for k in list(weights) + ["quality"]], *[fmt(s[k]["median"]) for k in ["elapsed_s", "ttft_visible_s", "visible_decode_tps"]], fmt(s["output_tokens"]["mean"], 0), fmt(s["estimated_standard_api_usd"]["mean"], 6)]
        if "coverage" in weights:
            values += [str(c.get("critical_errors", 0)), f"{c.get('error_answers', 0)}/{c.get('omission_answers', 0)}"]
        body.append("<tr>" + "".join("<td>" + x + "</td>" for x in values) + "</tr>")
    return '<div class="tablewrap"><table><thead><tr>' + "".join("<th>"+h+"</th>" for h in heads) + "</tr></thead><tbody>" + "".join(body) + "</tbody></table></div>"


def render_csv(rows, version=1):
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=CSV_V2_FIELDS if version == 2 else CSV_FIELDS)
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def report_context(root):
    root = Path(root)
    assessment = read(root / "assessment.json") if (root / "assessment.json").exists() else {"conclusion": "尚未填写本题的模型选择建议。请结合下表、事实错误与答案阅读质量评估。", "evidence": [], "limitations": []}
    return {"protocol": read(root / "protocol.json"), "prices": read(root / "prices.json"), "assessment": assessment,
            "parent": read(root / "parent-summary.json") if (root / "parent-summary.json").exists() else None,
            "fingerprint": read(root / "runtime.json").get("fingerprint") if (root / "runtime.json").exists() else None,
            "prompt": (root / "input/prompt.txt").read_text(encoding="utf-8"), "gold": read(root / "input/gold.json"), "scoring_gold": scoring_gold(root)}


def evaluation_html(data, context):
    """Versioned grading audit; original V1 rendering stays byte-compatible."""
    if data.get("scoring_version", 1) != 2:
        return "", "", ""
    intro = '<p class="note">评分 V2：准确性50 / 任务覆盖20 / 边界洞察10 / 可读性20。关键事实错误优先于综合分审查；广度不按篇幅、术语数量或推理token计分。</p>'
    if data.get("evaluation"):
        intro += '<p class="note">' + html.escape(data["evaluation"]["note"]) + '</p>'
    comparison = ""
    if data.get("previous_evaluation"):
        old = data["previous_evaluation"]
        comparison = '<h2>原评分（版本不同，单独展示）</h2><p>同一批答案；分数变化来自评分口径，不是模型能力变化。原评分版本 ' + str(old["scoring_version"]) + '</p>' + condition_table(old["conditions"], old["scoring_weights"])
        old_rows = {r["answer_link"]: r for r in old["rows"]}
        comparison += '<details><summary>逐份新旧总分对照</summary><div class="tablewrap"><table><tr><th>组合/轮次/尝试</th><th>原总分</th><th>V2总分</th></tr>'
        for r in data["rows"]:
            label = f"{r['model']}/{r['effort']}/r{r['repetition']}/a{r['attempt']}"
            comparison += '<tr><td>'+html.escape(label)+'</td><td>'+fmt(old_rows[r["answer_link"]].get("quality"))+'</td><td>'+fmt(r.get("quality"))+'</td></tr>'
        comparison += '</table></div></details>'
    audit = '<details><summary>V2细则、逐项评分依据与错误/遗漏</summary><pre>' + html.escape(json.dumps(context["scoring_gold"], ensure_ascii=False, indent=2)) + '</pre>'
    for row in data["rows"]:
        score = data.get("grading_details", {}).get(row["answer_link"])
        if score is None:
            continue
        dimensions = " · ".join(f"{DIMENSION_LABELS[key]} {row[key]}/{weight}" for key, weight in data["scoring_weights"].items())
        audit += '<details><summary>' + html.escape(f"{row['model']}/{row['effort']}/r{row['repetition']} · {dimensions} · 总分 {row['quality']}/100 · 关键错误 {row['critical_errors']}") + '</summary>'
        for item in context["scoring_gold"]["rubric"]:
            key = item["id"]
            audit += '<p><strong>' + html.escape(f"{key} · {DIMENSION_LABELS[item['dimension']]} {score['points'][key]}/{item['max']}") + '</strong> ' + html.escape(score["notes"][key]) + '</p>'
            for q in score["evidence"][key]:
                audit += '<blockquote>' + html.escape(q) + '</blockquote>'
        for issue in score["issues"]:
            audit += '<p>' + html.escape(f"{issue['kind']}/{issue['severity']} · {issue['criterion']}：{issue['note']}") + '</p>'
            if issue.get("quote"):
                audit += '<blockquote>' + html.escape(issue["quote"]) + '</blockquote>'
        audit += '</details>'
    return intro, comparison, audit + '</details>'


def render_html(data, context):
    """Render only supplied data; file and clock access belongs to the caller."""
    p, prices, assessment = context["protocol"], context["prices"], context["assessment"]
    require(isinstance(assessment.get("conclusion"), str) and isinstance(assessment.get("evidence", []), list) and isinstance(assessment.get("limitations", []), list), "Invalid assessment format")
    check_sensitive(json.dumps(assessment, ensure_ascii=False))
    detail = []
    for c in data["conditions"]:
        for name, stat in c["statistics"].items():
            detail.append("<tr>" + "".join("<td>"+html.escape(str(v))+"</td>" for v in [c["model"]+"/"+c["effort"], name, stat["n"], *[fmt(stat[k], 6) for k in ["mean", "median", "sd", "min", "max"]]]) + "</tr>")
    comparison = ""
    if context["parent"] is not None:
        parent = context["parent"]
        comparison = "<h2>上一批次（单独展示，不合并）</h2><p>"+html.escape(parent["batch"])+"</p>"+condition_table(parent["conditions"])
        oldfp = parent.get("fingerprint")
        currentfp = context["fingerprint"]
        if oldfp != currentfp:
            comparison += '<p class="note">两批运行环境指纹不同，不能把全部变化归因于模型波动。</p>'
    payload = json.dumps({"answers": [{**a, "html": markdown(a["text"])} for a in data["answers"]], "cells": data["conditions"]}, ensure_ascii=False).replace("<", "\\u003c").replace("&", "\\u0026")
    evidence = "".join("<li>"+html.escape(str(x))+"</li>" for x in assessment.get("evidence", []))
    limits = ["单题、小样本；AI 评分，不是人工偏好研究。", "同一固定证据，不衡量搜索、工具使用、代码修改或自动委派。", "TPS = 可见输出 token / 首末非空可见 delta 间隔，受客户端缓冲影响，不是纯后端解码速率。", "总耗时从 turn/start 发出到 turn/completed 收到，含排队、网络、思考和输出；不含建会话和评分。", "费用按官方快照估算，不等于自建网关扣费；无缓存口径保留同次输出用量，仅是输入计费对照。", "缺失值为 —；统计 n 可能小于有效答案数，费用总计只覆盖已知项。"] + assessment.get("limitations", [])
    limit_html = "".join("<li>"+html.escape(str(x))+"</li>" for x in limits)
    raw = "".join('<details><summary>'+html.escape(a["label"])+'</summary><a href="'+quote(a["link"], safe="/")+'">原始答案</a><article>'+markdown(a["text"])+'</article></details>' for a in data["answers"])
    page = '''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>工程模型 Benchmark</title><style>
    :root{color-scheme:light dark;--bg:#f4f5f7;--fg:#24303a;--panel:#fff;--line:#cfd5db;--accent:#176d73}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.7 system-ui,"Microsoft YaHei",sans-serif}main{max-width:1400px;margin:auto;padding:30px 24px 70px}h1{font-size:29px}h2{font-size:22px;margin-top:32px}h3{font-size:18px}section,details{background:var(--panel);padding:20px;margin:18px 0;border-radius:10px}table{border-collapse:collapse;width:100%;font-size:13px;font-variant-numeric:tabular-nums}th,td{padding:10px;text-align:left;border-bottom:1px solid var(--line);vertical-align:top}.tablewrap{overflow:auto}th{white-space:nowrap}a{color:var(--accent)}.note{font-size:14px;color:#66717b}.compare{display:grid;grid-template-columns:1fr 1fr;gap:24px}.compare>div{min-width:0}select{width:100%;padding:10px;margin-bottom:12px;background:var(--panel);color:var(--fg);border:1px solid var(--line)}article{overflow-wrap:anywhere}article p{margin:9px 0}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:var(--bg);padding:14px;font-size:13px}summary{cursor:pointer;font-weight:600}code{font-family:Consolas,monospace}svg{width:100%;height:auto}#chart{overflow-x:auto}#chart svg{min-width:900px}svg text{fill:var(--fg);font:14px system-ui}.badge{display:inline-block;background:var(--panel);padding:5px 12px;border-radius:14px;margin:0 10px 10px 0}@media(prefers-color-scheme:dark){:root{--bg:#17202a;--fg:#e5e9ef;--panel:#222e3a;--line:#455563;--accent:#74c6cd}.note{color:#b2bdc8}}@media(max-width:850px){.compare{grid-template-columns:1fr}main{padding:20px 12px}h1{font-size:24px}}@media print{details{display:block}select{display:none}}
    </style></head><body><main><h1>@@TITLE@@</h1><p class="note">@@BATCH@@ · @@PROJECT@@</p><span class="badge">有效 @@VALID@@ / 计划 @@PLANNED@@</span><span class="badge">尝试 @@ATTEMPTS@@</span><span class="badge">@@STATUS@@</span><section><h2>本题选择建议</h2><p>@@CONCLUSION@@</p><ul>@@EVIDENCE@@</ul></section><h2>本批次结果</h2>@@TABLE@@<p class="note">观察总耗时 @@TIME@@ s；已知费用合计 $@@COST@@，覆盖 @@COVERAGE@@ 次尝试。价格日期 @@PRICE_DATE@@。</p><section><h2>耗时与范围</h2><div id="chart"></div><p class="note">点为中位数，横线为最小至最大；保留所有有效样本。</p></section>@@PARENT@@<section><h2>答案并排阅读</h2><div class="compare"><div><label for="left">左侧答案</label><select id="left"></select><article id="left-answer"></article></div><div><label for="right">右侧答案</label><select id="right"></select><article id="right-answer"></article></div></div></section><details><summary>每项指标分布与实际 n</summary><div class="tablewrap"><table><thead><tr><th>组合</th><th>指标</th><th>n</th><th>均值</th><th>中位数</th><th>标准差</th><th>最小</th><th>最大</th></tr></thead><tbody>@@DETAIL@@</tbody></table></div></details><details><summary>题面、证据和标准答案</summary>@@PROMPT@@<pre>@@GOLD@@</pre><pre>@@HASHES@@</pre></details><details><summary>逐次答案与失败记录</summary>@@RAW@@</details><section><h2>口径与限制</h2><ul>@@LIMITS@@</ul><p><a href="metrics.csv">CSV</a> · <a href="summary.json">完整 JSON</a> · <a href="validation.json">校验记录</a></p><pre>@@PRICES@@</pre></section></main><script id="data" type="application/json">@@DATA@@</script><script>
    const d=JSON.parse(document.getElementById('data').textContent);for(const side of ['left','right']){const s=document.getElementById(side);d.answers.forEach((a,i)=>{const o=document.createElement('option');o.value=i;o.textContent=a.label;s.append(o)});s.selectedIndex=side==='right'&&d.answers.length>1?1:0;function show(){document.getElementById(side+'-answer').innerHTML=d.answers[s.value]?.html||'无答案'}s.addEventListener('change',show);show()}
    const ns='http://www.w3.org/2000/svg',svg=document.createElementNS(ns,'svg');svg.setAttribute('viewBox',`0 0 1050 ${d.cells.length*42+42}`);svg.setAttribute('role','img');svg.setAttribute('aria-label','各组合耗时中位数与最小最大范围');const max=Math.max(1,...d.cells.map(c=>c.statistics.elapsed_s.max||0));function el(tag,attrs,text){const n=document.createElementNS(ns,tag);Object.entries(attrs).forEach(([k,v])=>n.setAttribute(k,v));if(text)n.textContent=text;svg.append(n)}d.cells.forEach((c,i)=>{const y=i*42+25,s=c.statistics.elapsed_s,x=v=>265+(v||0)/max*670;el('text',{x:0,y},c.model+'/'+c.effort);if(s.n){el('line',{x1:x(s.min),x2:x(s.max),y1:y-5,y2:y-5,stroke:'#57979b','stroke-width':3});el('circle',{cx:x(s.median),cy:y-5,r:5,fill:'#bd702b'});el('text',{x:950,y},s.median.toFixed(1)+' s')}});document.getElementById('chart').append(svg);
    </script></body></html>'''
    values = {"TITLE": html.escape(data["title"]), "BATCH": html.escape(data["batch"]), "PROJECT": html.escape(data["project"].get("name", data["project"]["root"])), "VALID": str(data["valid_completed"]), "PLANNED": str(data["planned"]), "ATTEMPTS": str(data["attempt_count"]), "STATUS": ("全量成功" if data["scores_sealed"] else "采样完成，评分未封存") if data["all_planned_successful"] else "有失败或缺口", "CONCLUSION": html.escape(assessment["conclusion"]), "EVIDENCE": evidence, "TABLE": condition_table(data["conditions"]), "TIME": fmt(data["observed_total_elapsed_s"]), "COST": fmt(data["known_cost_usd"] if data["cost_coverage_attempts"] else None, 6), "COVERAGE": str(data["cost_coverage_attempts"]), "PRICE_DATE": html.escape(str(prices.get("as_of") or "未知")), "PARENT": comparison, "DETAIL": "".join(detail), "PROMPT": markdown(context["prompt"]), "GOLD": html.escape(json.dumps(context["gold"], ensure_ascii=False, indent=2)), "HASHES": html.escape(json.dumps(p["input_hashes"], indent=2)), "RAW": raw, "LIMITS": limit_html, "PRICES": html.escape(json.dumps(prices, ensure_ascii=False, indent=2)), "DATA": payload}
    intro, old_evaluation, audit = evaluation_html(data, context)
    values["TABLE"] = intro + values["TABLE"] + audit
    values["PARENT"] += old_evaluation
    return re.sub(r"@@([A-Z_]+)@@", lambda m: values[m[1]], page)


def build_report(root, partial=False):
    root = Path(root)
    idle(root)
    data = summarize(root, partial)
    page = render_html(data, report_context(root))
    table = render_csv(data["rows"], data.get("scoring_version", 1))
    write(root / "summary.json", data)
    (root / "metrics.csv").write_text(table, encoding="utf-8-sig", newline="")
    (root / "report.html").write_text(page, encoding="utf-8")
    return data


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links, self.ids = [], []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.ids.append(attrs["id"])
        if tag == "a" and "href" in attrs:
            self.links.append(attrs["href"])


def validate(root, partial=False):
    root = Path(root)
    p = check_inputs(root)
    score_data(root, partial)
    attempts, tids = all_attempts(root), set()
    schedule = {s["id"]: s for s in p["schedule"]}
    for r in attempts:
        require(r["slot_id"] in schedule, "Unplanned slot")
        slot = schedule[r["slot_id"]]
        require(r["model"] == slot["model"] and r["effort"] == slot["effort"] and r["repetition"] == slot["repetition"], "Result condition differs from schedule")
        directory = inside(root, r["relative_result"]).parent
        answer = (directory / "answer.txt").read_text(encoding="utf-8")
        require(sha(directory / "answer.txt") == r["answer_sha256"] and r["prompt_sha256"] == sha(root / "input/prompt.txt"), "Answer/prompt hash mismatch")
        if r.get("thread_id"):
            require(r["thread_id"] not in tids, "Thread reused between attempts")
            tids.add(r["thread_id"])
        if r.get("valid"):
            e = r["effective"]
            require(r["status"] == "completed" and e["model"] == r["model"] and e["reasoningEffort"] == r["effort"] and e["serviceTier"] == "default" and not r.get("tool_items"), "Invalid effective settings/status/tools")
            require(not usage_issues(r.get("usage")), "Invalid reported usage")
            events_file = directory / "events.jsonl"
            require(events_file.exists(), "Missing events for valid result")
            events = [json.loads(line) for line in events_file.read_text(encoding="utf-8").splitlines()]
            ds = [e for e in events if e["method"] == "item/agentMessage/delta" and e.get("delta")]
            if ds:
                require("".join(e["delta"] for e in ds).replace("\r\n", "\n") == answer.replace("\r\n", "\n"), "Stream differs from answer")
                require(abs(ds[0]["t_s"]-r["first_delta_s"]) < 1e-4 and abs(ds[-1]["t_s"]-r["last_delta_s"]) < 1e-4, "Visible timing inconsistent")
        recalculated = metrics(r.get("usage"), r.get("elapsed_s"), r.get("first_delta_s"), r.get("last_delta_s"), answer, read(root / "prices.json").get("models", {}).get(r["model"]))
        for k in METRICS:
            a, b = r.get(k), recalculated[k]
            require(a is None and b is None or a is not None and b is not None and math.isclose(a, b, rel_tol=1e-8, abs_tol=1e-7), "Metric inconsistent: " + k)
        check_sensitive(json.dumps(r, ensure_ascii=False))
        check_sensitive(answer)
    exports = [root / name for name in ["summary.json", "metrics.csv", "report.html"]]
    if any(path.exists() for path in exports):
        require(all(path.is_file() for path in exports), "Incomplete report artifacts; regenerate with report")
        actual, expected = read(root / "summary.json"), summarize(root, partial)
        require(isinstance(actual, dict) and {k: v for k, v in actual.items() if k != "generated_at"} == {k: v for k, v in expected.items() if k != "generated_at"}, "Summary differs from raw results")
        with (root / "metrics.csv").open(encoding="utf-8-sig", newline="") as f:
            csv_rows = list(csv.reader(f))
        expected_csv = list(csv.reader(io.StringIO(render_csv(expected["rows"], expected.get("scoring_version", 1)), newline="")))
        require(csv_rows == expected_csv, "CSV differs from raw results: headers or rows")
        page = (root / "report.html").read_text(encoding="utf-8")
        require(page == render_html(expected, report_context(root)), "HTML differs from raw results; regenerate with report")
        parser = Links()
        parser.feed(page)
        require(len(parser.ids) == len(set(parser.ids)), "Duplicate HTML IDs")
        for link in parser.links:
            if not urlparse(link).scheme and not link.startswith("#"):
                require(inside(root, unquote(link)).is_file() or link == "validation.json", "Broken local report link")
        node = shutil.which("node")
        if node:
            checked = subprocess.run([node, str(Path(__file__).with_name("check_report.cjs")), str(root / "report.html")], capture_output=True, text=True, encoding="utf-8", timeout=30)
            require(checked.returncode == 0, "Offline report interaction check failed")
            write(root / "ui-check.json", json.loads(checked.stdout))
        else:
            write(root / "ui-check.json", {"passed": None, "reason": "Optional Node.js unavailable; HTML links/IDs checked by Python", "browser_visual_check": "not performed"})
    value = {"at": now(), "passed": True, "attempts_checked": len(attempts), "planned_slots": len(p["schedule"]), "scores_sealed": (root / "grading/seal.json").exists(), "partial_report": partial, "no_new_inference": True}
    write(root / "validation.json", value)
    return value


def archive(root):
    root = Path(root)
    files = []
    names = ["protocol.json", "prices.json", "runtime.json", "preflight.json", "instruction-context.json", "preparation-seal.json", "progress.json", "assessment.json", "summary.json", "metrics.csv", "report.html", "validation.json", "ui-check.json", "parent-summary.json"]
    files.extend(root / n for n in names if (root / n).is_file())
    files.extend(p for p in (root / "input").rglob("*") if p.is_file())
    if check_inputs(root).get("offline_regrade"):
        manifest = read(root / "evaluation/manifest.json")
        files.append(root / "evaluation/manifest.json")
        files.extend(inside(root, name) for name in manifest["files_sha256"] if name.startswith("evaluation/"))
    files.extend(p for p in (root / "runs").glob("*/attempt-*/*") if p.name in ["answer.txt", "result.json", "events.jsonl"] and p.is_file())
    files.extend(root / "grading" / n for n in ["private-mapping.json", "scores.json", "seal.json"] if (root / "grading" / n).is_file())
    files.extend(p for p in (root / "grading/packet").rglob("*") if p.is_file() and p.name != "scores-template.json")
    # Retain superseded unsealed packets, including unfinished scorer notes and
    # templates, without adding unrelated files from the run directory.
    for previous in sorted((root / "grading-history").glob("*")):
        files.extend(previous / n for n in ["private-mapping.json", "scores.json"] if (previous / n).is_file())
        files.extend(p for p in (previous / "packet").rglob("*") if p.is_file())
    for path in files:
        require(path.resolve().is_relative_to(root.resolve()), "Archive entry escapes run")
        check_sensitive(path.read_text(encoding="utf-8-sig"))
    temp = root / "benchmark-results.zip.tmp"
    with zipfile.ZipFile(temp, "w", zipfile.ZIP_DEFLATED) as z:
        for path in files:
            z.write(path, path.relative_to(root).as_posix())
    temp.replace(root / "benchmark-results.zip")
    with zipfile.ZipFile(root / "benchmark-results.zip") as z:
        require(z.testzip() is None, "Corrupt archive")
        for name in z.namelist():
            require(z.read(name) == inside(root, name).read_bytes(), "Archive differs from deliverable")
    return {"archive": str(root / "benchmark-results.zip"), "files": len(files), "sha256": sha(root / "benchmark-results.zip")}
