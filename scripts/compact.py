"""Static compact comparison views derived from sealed condition statistics."""
from __future__ import annotations

import csv
import html
import io
import json

from core import require
from billing import SUBSCRIPTION_LABEL, format_usd


DIMENSIONS = (("accuracy", "准确"), ("coverage", "覆盖"),
              ("insight", "洞察"), ("readability", "表达"))
STAT_FIELDS = ("n", "mean", "median", "min", "max")


def display_topics(groups):
    """Join only display lists; condition identities and statistics stay intact."""
    topics = {}
    for group in groups:
        key = tuple(group.get(k) for k in ("case_id", "rubric_sha256", "scoring_version", "execution_mode"))
        maxima = group.get("dimension_maxima", group.get("weights", {}))
        if key not in topics:
            topics[key] = {k: group.get(k) for k in ("case_id", "title", "label", "rubric_sha256", "scoring_version", "execution_mode")}
            topics[key].update(id=f"topic-{len(topics) + 1}", dimension_maxima=maxima,
                               member_case_ids=[], conditions=[], sources=[])
        topic = topics[key]
        require(topic["dimension_maxima"] == maxima, "Same-topic dimension maxima differ")
        topic["member_case_ids"].append(group["id"])
        topic["conditions"].extend(group.get("conditions", []))
        known = {s["id"] for s in topic["sources"]}
        topic["sources"].extend(s for s in group.get("sources", []) if s["id"] not in known)
    return list(topics.values())


def _metrics(topic):
    if topic["execution_mode"] == "project-readonly":
        return (("answer_ready_s", "答案交付（秒）", "median", 1),
                ("workflow_elapsed_s", "全部任务结束（秒）", "median", 1)), "tree_"
    return (("elapsed_s", "完成（秒）", "median", 1),), ""


def _fields(topic):
    times, prefix = _metrics(topic)
    return ["quality", *(k for k, _ in DIMENSIONS), *(k for k, *_ in times),
            prefix + "input_tokens", prefix + "output_tokens", prefix + "estimated_api_usd", "estimated_subscription_credits", "estimated_subscription_usd"]


def _ordered(conditions):
    order = {"low": 0, "medium": 1, "high": 2, "xhigh": 3, "max": 4, "ultra": 5}
    return sorted(conditions, key=lambda c: (order.get(c["effort"], 99), c["effort"],
                  c.get("service_tier", "default") != "default", c["model"],
                  c.get("batch", ""), c["id"]))


def compact_data(data, source_sha256, cli_versions=None):
    cli_versions = cli_versions or {}
    sources = {(s["case"], s["origin_batch_id"]): s for s in data["source_batches"]}
    require(len(sources) == len(data["source_batches"]), "Duplicate compact source identity")
    result = {"schema_version": 1, "generated_at": data.get("generated_at"),
              "source": "comparison-data.json", "source_sha256": source_sha256,
              "topics": [], "rows": [], "billing_context": data.get("billing_context")}
    seen = set()
    for topic in display_topics(data["cases"]):
        result["topics"].append({k: v for k, v in topic.items() if k != "conditions"})
        for c in _ordered(topic["conditions"]):
            require(c["id"] not in seen, "Duplicate compact condition identity")
            seen.add(c["id"])
            src = sources[(c["case"], c["origin_batch_id"])]
            require(src["execution_profile_id"] == c["execution_profile_id"], "Compact source profile mismatch")
            row = {"topic_id": topic["id"], "case_id": topic["case_id"],
                   "case_title": topic.get("title") or topic.get("label"), "condition_id": c["id"],
                   "case": c["case"], "rubric_sha256": topic["rubric_sha256"],
                   "scoring_version": topic["scoring_version"], "execution_mode": c["execution_mode"],
                   "execution_profile_id": c["execution_profile_id"], "source_id": src["id"],
                   "source_batch": src["batch"], "origin_batch_id": c["origin_batch_id"],
                   "price_as_of": src.get("price_as_of"), "cli_version": cli_versions.get(src["id"]),
                    "billing_basis": "included_subscription_estimate", "billing_unit": "USD",
                    "billing_rules_as_of": data.get("billing_context", {}).get("as_of"),
                   "model": c["model"], "effort": c["effort"],
                   "service_tier": c.get("service_tier", "default"),
                   "speed": c.get("speed_mode") or ("Fast" if c.get("service_tier") == "priority" else "Standard"),
                   "valid_n": c["valid_completed"], "planned_n": c["planned"],
                   "critical_errors": c.get("critical_errors"), "error_answers": c.get("error_answers")}
            for key, _ in DIMENSIONS:
                row[key + "_maximum"] = topic["dimension_maxima"].get(key)
            for key in _fields(topic):
                stat = c["statistics"].get(key, {})
                for field in STAT_FIELDS:
                    row[key + "_" + field] = stat.get(field, 0 if field == "n" else None)
            result["rows"].append(row)
    result.update(topic_count=len(result["topics"]), condition_count=len(result["rows"]), additional_model_calls=0)
    return result


def _number(value, places=1, comma=False):
    if value is None:
        return "—"
    return format(value, f",.{places}f" if comma else f".{places}f")


def _coverage(row, metric):
    n = row.get(metric + "_n", 0)
    return f" [{n}/{row['valid_n']}]" if n != row["valid_n"] else ""


def compact_cells(row, topic):
    times, prefix = _metrics(topic)
    def metric(key, center="mean", places=1, comma=False):
        return _number(row.get(key + "_" + center), places, comma) + _coverage(row, key)
    quality = metric("quality")
    if row.get("quality_mean") is not None:
        quality = (f"{_number(row['quality_mean'])} [{_number(row['quality_min'])}–{_number(row['quality_max'])}]"
                   + _coverage(row, "quality"))
    return [row["effort"], row["speed"], row["model"], row.get("cli_version") or "—",
            f"{row['valid_n']}/{row['planned_n']}", quality,
            " / ".join(metric(k) for k, _ in DIMENSIONS),
            " / ".join(_number(row.get(k), 0) for k in ("critical_errors", "error_answers")),
            *(metric(k, center, places) for k, _, center, places in times),
            " / ".join(metric(prefix + k, places=0, comma=True) for k in ("input_tokens", "output_tokens")),
            format_usd(row.get("estimated_subscription_usd_mean")) + _coverage(row, "estimated_subscription_usd")]


def render_compact(compact, data):
    esc = lambda v: html.escape(str(v), quote=True)
    parts = ['''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>模型紧凑对照</title>
<style>body{font:15px/1.55 system-ui,"Segoe UI",sans-serif;color:#17243b;background:#f4f7fb;margin:0}main{max-width:1800px;margin:auto;padding:28px}h1{font-size:30px;margin:0 0 12px}h2{margin:30px 0 8px;font-size:21px}.note{background:#e8f1ff;border-left:4px solid #2966d3;padding:12px 16px}.scroll{overflow:auto;background:white;border:1px solid #dce5f0;border-radius:8px}table{border-collapse:collapse;width:100%;white-space:nowrap;font-variant-numeric:tabular-nums}th,td{padding:8px 10px;border-bottom:1px solid #e4eaf3;text-align:right}th{background:#eaf0f8}th:nth-child(-n+4),td:nth-child(-n+4){text-align:left}small,details{color:#53657b}.links a{display:inline-block;margin-right:16px}details{font-size:13px;margin-top:8px;overflow-wrap:anywhere}@media(max-width:700px){main{padding:16px}h1{font-size:24px}}</style></head><body><main><h1>模型紧凑对照</h1>
<p>按题目、评分细则和执行模式并列各批配置，每行保留原统计，不合并跨批样本。</p>
<p class="links"><a href="report.html">完整综合报告</a><a href="compact.csv">下载本表 CSV</a><a href="compact-data.json">下载本表 JSON</a><a href="comparison.zip">完整结果包</a></p>''']
    limits = list(dict.fromkeys([*(data.get("limitations") or []), *(data.get("assessment", {}).get("limitations") or [])]))
    parts.append('<div class="note">' + ''.join(f'<p>{esc(x)}</p>' for x in limits) + '</div>')
    billing = data.get("billing_context") or {}
    parts.append('<p class="note">' + SUBSCRIPTION_LABEL + '展示均值；Standard 1×，Fast 2.5×。规则核对日期 ' + esc(billing.get("as_of") or "未知") + '。' + esc(billing.get("note") or "美元等值估算未知；不回退 API 美元。") + '</p>')
    parts.append('<p><small>n 为有效／计划样本。指标覆盖完整时只显示一次 n；覆盖不足时在该值后标注 [已知数/有效数]，缺失显示“—”。质量为均值和范围；四维括号内为各项满分，旧评分缺少的维度保持未知。两种工程耗时都从请求发出计时，展示中位数：答案交付是最终正文完整返回，全部任务结束是所有已启动工作达到终态；固定材料仅展示完成耗时。用量为均值，工程模式使用全树计数。API 历史费用仅保留在下载数据中。</small></p>')
    for topic in compact["topics"]:
        times, prefix = _metrics(topic)
        rows = [r for r in compact["rows"] if r["topic_id"] == topic["id"]]
        dimensions = " / ".join(f"{label}（{_number(topic['dimension_maxima'].get(k), 0)}）" for k, label in DIMENSIONS)
        headers = ["档位", "速度", "模型", "CLI", "n", "质量均值 [范围]", dimensions,
                   "关键错误 / 错误答案", *(label for _, label, *_ in times),
                   ("全树输入 / 输出 token" if prefix else "输入 / 输出 token"), SUBSCRIPTION_LABEL]
        title = topic.get("title") or topic.get("label") or topic["case_id"]
        mode = "工程只读" if prefix else "固定材料"
        parts.append(f'<section><h2>{esc(title)}</h2><p><small>评分 V{esc(topic["scoring_version"])} · {esc(mode)} · rubric {esc(topic["rubric_sha256"][:12])} · {len(rows)} 个配置。环境差异见完整报告。</small></p><div class="scroll" tabindex="0" role="region" aria-label="{esc(title)}对照表"><table><thead><tr>')
        parts.extend(f'<th scope="col">{esc(h)}</th>' for h in headers)
        parts.append('</tr></thead><tbody>')
        for row in rows:
            provenance = f"{row['source_batch']} · {row['source_id']} · {row['execution_profile_id']}"
            parts.append(f'<tr data-condition="{esc(row["condition_id"])}" title="{esc(provenance)}">')
            parts.extend(f'<td>{esc(value)}</td>' for value in compact_cells(row, topic))
            parts.append('</tr>')
        parts.append('</tbody></table></div><details><summary>来源与运行环境</summary><ul>')
        for source in topic["sources"]:
            parts.append(f'<li><a href="sources/{esc(source["id"])}/summary.json">{esc(source["batch"])}</a> · {esc(source["execution_profile_id"])} · 价格日期 {esc(source.get("price_as_of") or "未知")}</li>')
        parts.append('</ul></details></section>')
    parts.append('<footer><p><small>本表与完整报告共用封存统计，无新增模型调用；完整来源、答案和逐项评分见完整报告与数据文件。</small></p></footer></main></body></html>')
    return ''.join(parts)


def compact_artifacts(data, source_sha256, cli_versions=None):
    compact = compact_data(data, source_sha256, cli_versions)
    fields = list(dict.fromkeys(k for r in compact["rows"] for k in r))
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows(compact["rows"])
    return {"compact.html": render_compact(compact, data), "compact.csv": stream.getvalue(),
            "compact-data.json": json.dumps(compact, ensure_ascii=False, indent=2, allow_nan=False) + "\n"}
