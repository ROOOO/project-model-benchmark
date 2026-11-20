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
EXECUTION_LABELS = {"project-readonly": "工程只读 · 原生代理",
                    "project-full-access-cli": "工程 CLI · Full Access",
                    "project-readonly-cli": "工程 CLI · 受限", "fixed-evidence": "固定材料 · 禁工具"}


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
<p class="links"><a href="question-overview.html">跨执行模式逐题对照</a><a href="report.html">完整综合报告</a><a href="compact.csv">下载本表 CSV</a><a href="compact-data.json">下载本表 JSON</a><a href="comparison.zip">完整结果包</a></p>''']
    limits = list(dict.fromkeys([*(data.get("limitations") or []), *(data.get("assessment", {}).get("limitations") or [])]))
    parts.append('<div class="note">' + ''.join(f'<p>{esc(x)}</p>' for x in limits) + '</div>')
    billing = data.get("billing_context") or {}
    parts.append('<p class="note">' + SUBSCRIPTION_LABEL + '展示均值；Standard 1×，Fast 2.5×。规则核对日期 ' + esc(billing.get("as_of") or "未知") + '。' + esc(billing.get("note") or "美元等值估算未知；不回退 API 美元。") + '</p>')
    parts.append('<p><small>n 为有效／计划样本。指标覆盖完整时只显示一次 n；覆盖不足时在该值后标注 [已知数/有效数]，缺失显示“—”。质量为均值和范围；四维括号内为各项满分，旧评分缺少的维度保持未知。耗时展示中位数：Codex 工程模式从请求发出分别计答案交付和全部任务结束；CLI 从进程启动到退出计完成耗时，包含启动开销。用量为均值，Codex 工程模式使用全树计数，CLI 使用原生累计计数，未核验完整任务树。未知模型的费用不套用 Codex 权重。API 历史费用仅保留在下载数据中。</small></p>')
    for topic in compact["topics"]:
        times, prefix = _metrics(topic)
        rows = [r for r in compact["rows"] if r["topic_id"] == topic["id"]]
        dimensions = " / ".join(f"{label}（{_number(topic['dimension_maxima'].get(k), 0)}）" for k, label in DIMENSIONS)
        headers = ["档位", "速度", "模型", "CLI", "n", "质量均值 [范围]", dimensions,
                   "关键错误 / 错误答案", *(label for _, label, *_ in times),
                   ("全树输入 / 输出 token" if prefix else "输入 / 输出 token"), SUBSCRIPTION_LABEL]
        title = topic.get("title") or topic.get("label") or topic["case_id"]
        mode = EXECUTION_LABELS.get(topic["execution_mode"], "未知执行模式")
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


def question_overview_artifacts(compact, data):
    """Place modes side by side without merging samples or their statistics."""
    topics, rows = {}, []
    for row in compact["rows"]:
        key = tuple(row[k] for k in ("case_id", "rubric_sha256", "scoring_version"))
        if key not in topics:
            topics[key] = {"id": f"question-{len(topics) + 1}", "case_id": row["case_id"],
                           "title": row["case_title"], "rubric_sha256": row["rubric_sha256"],
                           "scoring_version": row["scoring_version"],
                           "dimension_maxima": next(t["dimension_maxima"] for t in compact["topics"] if t["id"] == row["topic_id"])}
        native = row["execution_mode"] == "project-readonly"
        delivery = "answer_ready_s" if native else "elapsed_s"
        prefix = "tree_" if native else ""
        value = {**row, "question_id": topics[key]["id"],
                 "execution_label": EXECUTION_LABELS.get(row["execution_mode"], "未知执行模式"),
                 "delivery_metric": delivery,
                 "token_basis": "Codex 全树" if native else "CLI 原生累计" if row["execution_mode"].endswith("-cli") else "固定材料单线程"}
        for target, source in (("delivery_s", delivery), ("workflow_s", "workflow_elapsed_s"),
                               ("observed_input_tokens", prefix + "input_tokens"),
                               ("observed_output_tokens", prefix + "output_tokens")):
            for field in STAT_FIELDS:
                value[target + "_" + field] = row.get(source + "_" + field, 0 if field == "n" else None)
        rows.append(value)
    payload = {"schema_version": 1, "source": "comparison-data.json",
               "source_sha256": compact["source_sha256"], "topics": list(topics.values()),
               "rows": rows, "statistics_merged": False, "additional_model_calls": 0}
    esc = lambda v: html.escape(str(v), quote=True)
    parts = ['''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>逐题模型对照</title><style>
body{margin:0;background:#f4f7fb;color:#17243b;font:15px/1.6 system-ui,"Microsoft YaHei",sans-serif}main{max-width:1800px;margin:auto;padding:26px}h1{margin:0}h2{font-size:22px;margin-top:32px}.note{padding:14px 18px;background:#e8f1ff;border-left:4px solid #2966d3}.scroll{overflow:auto;background:white;border:1px solid #dce5f0}table{border-collapse:collapse;width:100%;white-space:nowrap;font-variant-numeric:tabular-nums}th,td{padding:9px 11px;border-bottom:1px solid #dce5f0;text-align:right}th{background:#eaf0f8}th:nth-child(-n+3),td:nth-child(-n+3){text-align:left}small{color:#53657b}a{color:#175dbc}.current{background:#eef8f3}@media(max-width:700px){main{padding:14px}h1{font-size:25px}}</style></head><body><main><h1>逐题模型对照</h1>
<p><a href="report.html">完整报告与原文</a> · <a href="compact.html">按执行模式分表</a> · <a href="question-overview.csv">CSV</a> · <a href="question-overview.json">JSON</a> · <a href="comparison.zip">结果包</a></p>''']
    assessment = data.get("assessment") or {}
    parts.append('<div class="note"><p>' + esc(assessment.get("conclusion") or "各行保留独立来源与原统计。") + '</p>')
    for text in [*(assessment.get("evidence") or []), *(data.get("limitations") or []), *(assessment.get("limitations") or [])]:
        parts.append('<p>' + esc(text) + '</p>')
    parts.append('</div><p><small>质量为均值 [范围]。V2计准确 / 覆盖 / 洞察 / 表达（50 / 20 / 10 / 20）。耗时为中位数：Codex 从请求发出计答案交付及全树结束，CLI 从进程启动至退出，含启动开销；两者不可作为相同计时口径排名。用量为均值，各行单列计数范围，原生计数语义可能不同。缺失为 —；覆盖不足显示 [已知/有效]。费用仅对有可靠规则和完整用量的模型估算，不代表现金扣费。各行统计独立，未合并跨模式样本。</small></p>')
    headers = ["模型 / 档位", "执行条件", "用量范围", "n", "质量均值 [范围]", "准确 / 覆盖 / 洞察 / 表达",
               "关键错误 / 错误答案", "交付／CLI完成（秒）", "全树结束（秒）", "输入 / 输出 token", SUBSCRIPTION_LABEL]
    for topic in topics.values():
        topic_headers = [h.replace("准确 / 覆盖 / 洞察 / 表达", "准确 / 覆盖 / 洞察") if False else h for h in headers]
        parts.append(f'<section><h2>{esc(topic["title"])}</h2><p><small>评分 V{esc(topic["scoring_version"])} · rubric {esc(topic["rubric_sha256"][:12])}</small></p><div class="scroll"><table><thead><tr>')
        parts.extend('<th>' + esc(h) + '</th>' for h in topic_headers)
        parts.append('</tr></thead><tbody>')
        for row in (r for r in rows if r["question_id"] == topic["id"]):
            def metric(k, center="mean", places=1):
                return _number(row.get(k + "_" + center), places, places == 0) + _coverage(row, k)
            quality = f'{metric("quality")} [{_number(row["quality_min"])}–{_number(row["quality_max"])}]'
            cells = [f'{row["model"]} / {row["effort"]}', row["execution_label"], row["token_basis"],
                     f'{row["valid_n"]}/{row["planned_n"]}', quality,
                     " / ".join(metric(k) for k, _ in DIMENSIONS),
                     " / ".join(_number(row.get(k), 0) for k in ("critical_errors", "error_answers")),
                     metric("delivery_s", "median"), metric("workflow_s", "median"),
                     " / ".join(metric("observed_" + k, places=0) for k in ("input_tokens", "output_tokens")),
                     format_usd(row.get("estimated_subscription_usd_mean")) + _coverage(row, "estimated_subscription_usd")]
            css = ' class="current"' if row["execution_mode"] == "project-full-access-cli" else ""
            parts.append(f'<tr{css} data-condition="{esc(row["condition_id"])}" title="{esc(row["source_batch"])} · {esc(row["execution_profile_id"])}">')
            parts.extend('<td>' + esc(c) + '</td>' for c in cells)
            parts.append('</tr>')
        parts.append('</tbody></table></div></section>')
    parts.append('</main></body></html>')
    fields = list(dict.fromkeys(k for row in rows for k in row))
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return {"question-overview.html": ''.join(parts), "question-overview.csv": stream.getvalue(),
            "question-overview.json": json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"}



def compact_artifacts(data, source_sha256, cli_versions=None):
    compact = compact_data(data, source_sha256, cli_versions)
    fields = list(dict.fromkeys(k for r in compact["rows"] for k in r))
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows(compact["rows"])
    return {"compact.html": render_compact(compact, data), "compact.csv": stream.getvalue(),
            "compact-data.json": json.dumps(compact, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
            **question_overview_artifacts(compact, data)}
