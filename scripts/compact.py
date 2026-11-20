"""Static compact comparison views derived from sealed condition statistics."""
from __future__ import annotations

import csv
from decimal import Decimal, ROUND_HALF_UP
import html
import io
import json

from core import V3_METRICS, require, three_dimension_scoring
from billing import SUBSCRIPTION_LABEL, format_usd


DIMENSIONS = (("accuracy", "准确"), ("coverage", "覆盖"),
              ("insight", "洞察"), ("readability", "表达"))


def dimensions(topic):
    return tuple((k, label) for k, label in DIMENSIONS if not three_dimension_scoring(topic) or k != "readability")
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
        if group.get("review_revision"):
            topic["review_revision"] = group["review_revision"]
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
    return ["quality", *(k for k, _ in dimensions(topic)), *(k for k, *_ in times),
            prefix + "input_tokens", prefix + "output_tokens", prefix + "estimated_api_usd", "estimated_subscription_credits", "estimated_subscription_usd",
            *([*V3_METRICS, "previous_quality", "quality_delta"] if topic.get("scoring_version") in (3, 4) else []),
            *(["pre_review_quality", "review_quality_delta"] if topic.get("review_revision") else [])]


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
            if topic.get("scoring_version") in (3, 4):
                row.update(critical_omissions=c.get("critical_omissions"), omission_answers=c.get("omission_answers"),
                           critical_error_answers=c.get("critical_error_answers"), critical_omission_answers=c.get("critical_omission_answers"))
            if "previous_scoring_version" in c:
                row["previous_scoring_version"] = c["previous_scoring_version"]
            if c.get("review_revision"):
                row["review_revision"] = c["review_revision"]
            for key, _ in dimensions(topic):
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
    rounded = Decimal(str(value)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    return format(rounded, f",.{places}f" if comma else f".{places}f")


def _previous(row):
    label = f'V{row["previous_scoring_version"]}' if row.get("previous_scoring_version") else "原分"
    delta = row.get("quality_delta_mean")
    rounded_delta = Decimal(str(delta)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if delta is not None else None
    change = f' · Δ{rounded_delta:+.2f}' if rounded_delta is not None else ""
    return f'{label} {_number(row.get("previous_quality_mean"), 2)}{change}'


def _review_previous(row):
    if not row.get("review_revision"):
        return ""
    delta = row.get("review_quality_delta_mean")
    change = f' · Δ{Decimal(str(delta)):+.2f}' if delta is not None else ""
    return '<br><small class="review-previous">复核前 V3 ' + _number(row.get("pre_review_quality_mean"), 2) + change + '</small>'


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
            " / ".join(metric(k) for k, _ in dimensions(topic)),
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
    parts.append('<p><small>n 为有效／计划样本。指标覆盖完整时只显示一次 n；覆盖不足时在该值后标注 [已知数/有效数]，缺失显示“—”。质量为均值和范围；按冻结细则列出计分维度及各项满分，旧评分缺少的维度保持未知。耗时展示中位数：Codex 工程模式从请求发出分别计答案交付和全部任务结束；CLI 从进程启动到退出计完成耗时，包含启动开销。用量为均值，Codex 工程模式使用全树计数，CLI 使用原生累计计数，未核验完整任务树。未知模型的费用不套用 Codex 权重。API 历史费用仅保留在下载数据中。</small></p>')
    for topic in compact["topics"]:
        times, prefix = _metrics(topic)
        rows = [r for r in compact["rows"] if r["topic_id"] == topic["id"]]
        dimension_text = " / ".join(f"{label}（{_number(topic['dimension_maxima'].get(k), 0)}）" for k, label in dimensions(topic))
        headers = ["档位", "速度", "模型", "CLI", "n", "质量均值 [范围]", dimension_text,
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
    parts.append('</div><p><small>质量为均值 [范围]。正式V3计准确 / 覆盖 / 洞察（70 / 20 / 10），表达不计分；历史V2计准确 / 覆盖 / 洞察 / 表达（50 / 20 / 10 / 20）。耗时为中位数：Codex 从请求发出计答案交付及全树结束，CLI 从进程启动至退出，含启动开销；两者不可作为相同计时口径排名。用量为均值，各行单列计数范围，原生计数语义可能不同。缺失为 —；覆盖不足显示 [已知/有效]。费用仅对有可靠规则和完整用量的模型估算，不代表现金扣费。各行统计独立，未合并跨模式样本。</small></p>')
    headers = ["模型 / 档位", "执行条件", "用量范围", "n", "质量均值 [范围]", "准确 / 覆盖 / 洞察 / 表达",
               "关键错误 / 错误答案", "交付／CLI完成（秒）", "全树结束（秒）", "输入 / 输出 token", SUBSCRIPTION_LABEL]
    for topic in topics.values():
        topic_headers = [h.replace("准确 / 覆盖 / 洞察 / 表达", "准确 / 覆盖 / 洞察") if three_dimension_scoring(topic) else h for h in headers]
        parts.append(f'<section><h2>{esc(topic["title"])}</h2><p><small>评分 V{esc(topic["scoring_version"])} · rubric {esc(topic["rubric_sha256"][:12])}</small></p><div class="scroll"><table><thead><tr>')
        parts.extend('<th>' + esc(h) + '</th>' for h in topic_headers)
        parts.append('</tr></thead><tbody>')
        for row in (r for r in rows if r["question_id"] == topic["id"]):
            def metric(k, center="mean", places=1):
                return _number(row.get(k + "_" + center), places, places == 0) + _coverage(row, k)
            quality = f'{metric("quality")} [{_number(row["quality_min"])}–{_number(row["quality_max"])}]'
            cells = [f'{row["model"]} / {row["effort"]}', row["execution_label"], row["token_basis"],
                     f'{row["valid_n"]}/{row["planned_n"]}', quality,
                     " / ".join(metric(k) for k, _ in dimensions(topic)),
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
    overview = question_overview_artifacts(compact, data)
    result = {"compact.html": render_compact(compact, data), "compact.csv": stream.getvalue(),
              "compact-data.json": json.dumps(compact, ensure_ascii=False, indent=2, allow_nan=False) + "\n", **overview}
    if compact["topics"] and all(t["scoring_version"] in (3, 4) for t in compact["topics"]):
        result["matrix.html"] = render_principle_matrix(json.loads(overview["question-overview.json"]), data)
    return result


def render_principle_matrix(overview, data):
    """Static first view: no script is needed to see scores, time or issues."""
    esc = lambda v: html.escape(str(v), quote=True)
    version = 4 if all(t["scoring_version"] == 4 for t in overview["topics"]) else 3
    three_dimensions = all(three_dimension_scoring(t) for t in overview["topics"])
    key = lambda r: (r["model"], r["effort"], r.get("service_tier", "default"))
    columns = list(dict.fromkeys(key(c) for topic in data["cases"] for c in topic["conditions"]))
    names = {"gpt-6.2-sol": "6.2 Sol", "gpt-6.2-astra": "6.2 Astra", "gpt-6.1-sol": "旧 6.1 Sol",
             "gpt-6-astra": "旧 6 Astra", "gpt-6.1-terra": "旧 6.1 Terra", "gemini-4-neon-xhigh": "Neon Extra"}
    short = lambda s: next((name for word, name in (("共享", "共享预载"), ("模块", "模块生命周期"),
                                                   ("NewJoy", "NewJoy"), ("活动墙", "活动墙")) if s.startswith(word)), s)
    conditions = {c["id"]: c for t in data["cases"] for c in t["conditions"]}
    parts = ['''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>V3 四题紧凑评分报告</title><style>
*{box-sizing:border-box}body{margin:0;background:#11151d;color:#edf1f8;font:15px/1.6 system-ui,"Microsoft YaHei",sans-serif}main{max-width:1300px;margin:auto;padding:28px 22px 50px}h1{font-size:27px;margin:0 0 7px}h2{font-size:20px;margin:24px 0 9px}p{margin:8px 0}small,.muted{color:#a4afc2}a{color:#9dc5ff}.panel{background:#1b212c;border:1px solid #333d4c;border-radius:11px;padding:16px 18px}.scroll{overflow:auto}table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}th,td{border-bottom:1px solid #333d4c;text-align:center;padding:11px 13px;vertical-align:middle}th{background:#222a37}th:first-child,td:first-child{text-align:left}th small{display:block;font-size:11px;font-weight:400}.matrix{min-width:1040px}.score{font-size:21px;font-weight:650}.time{font-size:14px;color:#a4afc2;margin-left:5px}.badge{display:block;font-size:11px;margin-top:4px}.risk{background:rgba(236,134,72,.10)}.danger{color:#ffbd91}.summary{background:#222a37;font-weight:650}.note{border-left:3px solid #ffbd91;padding:10px 14px;background:#29251f;margin:16px 0;font-size:14px}details{border-top:1px solid #333d4c;margin-top:13px;padding-top:11px}summary{cursor:pointer;color:#9dc5ff}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}blockquote{font-size:13px;color:#c7d1e2;border-left:2px solid #526174;padding-left:12px}.audit td,.audit th{font-size:12px;padding:7px 10px}.footer{font-size:12px;color:#a4afc2;margin-top:18px}@media(max-width:700px){main{padding:18px 12px}h1{font-size:23px}}</style></head><body><main><h1>V3 四题紧凑评分报告</h1>
<p class="muted">每格：最终分 / 分钟。每题每配置 n=5；先逐份应用原则扣分与封顶，再统计均分。分数取均值，时长取中位数。</p><p><a href="question-overview.csv">CSV</a> · <a href="comparison-data.json">完整 JSON</a> · <a href="report.html">完整综合报告</a> · <a href="comparison.zip">结果包</a></p><div class="panel scroll"><table class="matrix"><thead><tr><th>题目</th>''']
    for model, effort, tier in columns:
        mode = "Full Access" if model.startswith("gemini-") else ("Fast" if tier == "priority" else "Standard")
        parts.append(f'<th>{esc(names.get(model, model))}<small>{esc(effort)} · {esc(mode)}</small></th>')
    parts.append('</tr></thead><tbody>')
    for topic in overview["topics"]:
        rows = {key(r): r for r in overview["rows"] if r["question_id"] == topic["id"]}
        parts.append(f'<tr data-question="{esc(topic["id"])}"><th>{esc(short(topic["title"]))}</th>')
        for column in columns:
            r = rows.get(column)
            if r is None:
                parts.append('<td>—</td>')
                continue
            errors, omissions = r.get("error_answers", 0), r.get("omission_answers", 0)
            css = ' class="risk"' if errors or omissions else ''
            parts.append(f'<td{css} data-condition="{esc(r["condition_id"])}"><span class="score">{_number(r["quality_mean"], 2)}</span><span class="time">/ {_number(r["delivery_s_median"] / 60 if r["delivery_s_median"] is not None else None)}</span><span class="badge danger">错 {errors}/{r["valid_n"]} · 漏 {omissions}/{r["valid_n"]}</span><small class="previous">{esc(_previous(r))}</small>{_review_previous(r)}</td>')
        parts.append('</tr>')
    parts.append('<tr class="summary" data-summary="mean"><th>四题等权均值</th>')
    for column in columns:
        rs = [r for r in overview["rows"] if key(r) == column]
        complete = len(rs) == len(overview["topics"]) and all(r["quality_mean"] is not None and r["delivery_s_median"] is not None for r in rs)
        if complete:
            mean_quality = (sum(Decimal(str(r["quality_mean"])) for r in rs) / len(rs)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            mean_minutes = sum(r["delivery_s_median"] for r in rs) / len(rs) / 60
            previous = ""
            if all(r.get("previous_quality_mean") is not None for r in rs):
                old = sum(Decimal(str(r["previous_quality_mean"])) for r in rs) / len(rs)
                current = sum(Decimal(str(r["quality_mean"])) for r in rs) / len(rs)
                previous = '<br><small class="previous">' + esc(_previous({**rs[0], "previous_quality_mean":old, "quality_delta_mean":current - old})) + '</small>'
            if all(r.get("pre_review_quality_mean") is not None for r in rs):
                pre = sum(Decimal(str(r["pre_review_quality_mean"])) for r in rs) / len(rs)
                current = sum(Decimal(str(r["quality_mean"])) for r in rs) / len(rs)
                previous += _review_previous({**rs[0], "pre_review_quality_mean":pre, "review_quality_delta_mean":current-pre})
            parts.append(f'<td><span class="score">{mean_quality:.2f}</span><span class="time"> / {mean_minutes:.1f}</span>{previous}</td>')
        else:
            parts.append('<td>—</td>')
    parts.append('</tr></tbody></table></div><p class="muted"><small>错/漏统计含相应问题的答案数；同一份可同时出现不同问题。原分是同一答案的历史评分，仅作对照。四题均值只描述本组样本，时长列为四题中位数的均值。</small></p>')
    assessment = data.get("assessment") or {}
    parts.append('<div class="note">' + esc(assessment.get("conclusion") or "先看错误与遗漏，再比较最终分和时间；不同执行条件保持独立。") + '</div>')
    parts.append('<section class="panel"><h2>原则规则与分数构成</h2><p>基础分：准确性50 + 覆盖20 + 洞察10 + 表达20。最终分 = max(0, min(基础分 − 原则扣分, 最低适用上限))。</p><div class="scroll"><table><tr><th>影响</th><th>错误：额外扣分 / 上限</th><th>遗漏：额外扣分 / 上限</th></tr><tr><td>核心结论</td><td>25 / 59</td><td>15 / 69</td></tr><tr><td>关键子结论</td><td>10 / 79</td><td>6 / 84</td></tr><tr><td>一般实质问题</td><td>3 / 89</td><td>2 / 100</td></tr></table></div><p class="muted"><small>同因问题只罚一次；同因的错误和下游遗漏不叠罚。核验点重要性权重3/2/1只计算辅助错漏比例，不能用多写正确句子稀释处罚。未提出可选洞察不算遗漏。</small></p>')
    for topic in overview["topics"]:
        parts.append('<details><summary>' + esc(short(topic["title"])) + '：逐份基础分、扣分、封顶和证据</summary>')
        for r in (r for r in overview["rows"] if r["question_id"] == topic["id"]):
            c = conditions[r["condition_id"]]
            old_label = f'V{r["previous_scoring_version"]}分' if r.get("previous_scoring_version") else "原分"
            parts.append('<details><summary>' + esc(names.get(r["model"], r["model"])) + '</summary><div class="scroll"><table class="audit"><tr><th>样本</th><th>' + esc(old_label) + '</th><th>基础分</th><th>原则扣分</th><th>上限</th><th>最终分</th><th>差值</th><th>错误率 / 遗漏率（辅助）</th></tr>')
            for s in c["rows"]:
                values = [s["anonymous_id"], _number(s.get("previous_quality")), _number(s["raw_quality"]),
                          _number(s["principle_penalty"]), _number(s["quality_cap"]), _number(s["quality"]), _number(s.get("quality_delta"), 2),
                          f'{s["weighted_error_rate"]:.1%} / {s["weighted_omission_rate"]:.1%}']
                parts.append('<tr>' + ''.join('<td>' + esc(v) + '</td>' for v in values) + '</tr>')
            parts.append('</table></div>')
            for s in c["rows"]:
                detail = data["grading_details"][s["attempt_identity"]]["scores"]
                parts.append('<details><summary>' + esc(s["anonymous_id"]) + ' · 评分依据</summary><p><a href="' + esc(s["answer_link"]) + '">原始答案</a></p>')
                audit = {"points": detail["points"], "notes": detail["notes"], "principle_groups": s["principle_groups"], "checks": detail["checks"]}
                if three_dimension_scoring(topic):
                    audit["presentation_notes"] = detail["presentation_notes"]
                if detail.get("review_audit"):
                    audit["review_audit"] = detail["review_audit"]
                    audit["review_notes"] = detail.get("review_notes", [])
                parts.append('<pre>' + esc(json.dumps(audit, ensure_ascii=False, indent=2)) + '</pre>')
                for issue in detail["issues"]:
                    parts.append('<p>' + esc(f'{issue["kind"]} / {issue["impact_level"]} / {issue["group_id"]}：{issue["note"]}；正确依据：{issue["reference"]}；影响：{issue["impact"]}') + '</p>')
                    if issue.get("quote"):
                        parts.append('<blockquote>' + esc(issue["quote"]) + '</blockquote>')
                parts.append('</details>')
            parts.append('</details>')
        parts.append('</details>')
    parts.append('</section><footer class="footer">')
    for text in [*(assessment.get("limitations") or []), "AI事后重评；评分者此前见过模型身份，非独立人工盲评。零新增采样，费用与执行用量沿用原记录，未知仍未知。", "历史Codex为max/工程只读，计请求至答案交付；Neon为Extra/xhigh/Full Access，计CLI启动至退出，包含启动开销。"]:
        parts.append('<p>' + esc(text) + '</p>')
    parts.append('</footer></main></body></html>')
    result = ''.join(parts)
    if version == 4:
        result = result.replace("V3 四题紧凑评分报告", "V4 四题紧凑评分报告")
    if three_dimensions:
        result = result.replace("基础分：准确性50 + 覆盖20 + 洞察10 + 表达20。", "基础分：准确性70 + 覆盖20 + 洞察10。表达仅作非计分备注。")
    if version == 3 and three_dimensions:
        result = result.replace("原分是同一答案的历史评分，仅作对照。", "V2为同一答案的原始评分，Δ为V3−V2；分差反映事后规则修订，不代表模型能力变化。")
        result = result.replace("AI事后重评；", "正式V3沿用已封存判断与当前分数，直接对照原始V2；")
    return result
