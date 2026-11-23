"""Compact offline exports for independent reviewers of the same draft cohort."""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from pathlib import Path
import csv
import hashlib
import html
import io
import json
import shutil
import statistics
import zipfile

import paired
from app_capture import validate_answer_entry
from core import check_sensitive, now, principle_score, read, require, sha, write
from fresh_blind import check_topic_seal, original_score
from paired_report import checkpoint_transitions, historical_performance, mean

D = lambda value: Decimal(str(value))


def aggregate(samples, topics):
    groups, by_pair = defaultdict(list), defaultdict(dict)
    for sample in samples:
        groups[sample["topic"], sample["version"]].append(sample)
        require(sample["version"] not in by_pair[sample["pair_id"]], "Duplicate review version")
        by_pair[sample["pair_id"]][sample["version"]] = sample
    cells = []
    for (topic, version), items in sorted(groups.items()):
        clean = [s for s in items if not s.get("contaminated")]
        row = {"topic": topic, "version": version, "n": len(items), "clean_n": len(clean),
               "contaminated": len(items) - len(clean), "all_quality": mean(s["quality"] for s in items)}
        for key in ("quality", "accuracy", "coverage", "insight", "errors", "omissions"):
            row[key] = mean(s[key] for s in clean) if clean else None
        values = [s["quality"] for s in clean]
        row.update(median=float(statistics.median(map(D, values))) if values else None,
                   minimum=min(values) if values else None, maximum=max(values) if values else None,
                   sample_stdev=statistics.stdev(values) if len(values) > 1 else None, actual_cost=None)
        cells.append(row)
    pairs = []
    for pair_id, versions in sorted(by_pair.items()):
        require(set(versions) == {"original", "a", "b"}, "Independent comparison needs all three versions")
        base = versions["original"]
        row = {"pair_id": pair_id, "topic": base["topic"], "cycle": base["cycle"],
               "clean": not any(s.get("contaminated") for s in versions.values()),
               **{v + "_quality": versions[v]["quality"] for v in ("original", "a", "b")}}
        for left, right in (("a", "original"), ("b", "original"), ("a", "b")):
            row[left + "_minus_" + right] = float(D(versions[left]["quality"]) - D(versions[right]["quality"]))
        for v in ("a", "b"):
            row[v + "_error_count_delta"] = versions[v]["errors"] - base["errors"]
            row[v + "_omission_count_delta"] = versions[v]["omissions"] - base["omissions"]
        pairs.append(row)
    comparisons = []
    for topic in topics:
        items = [p for p in pairs if p["topic"] == topic and p["clean"]]
        ds = [p["a_minus_b"] for p in items]
        comparisons.append({"topic": topic, "clean_pairs": len(items),
            **{k: mean(p[k] for p in items) if items else None for k in ("a_minus_original", "b_minus_original", "a_minus_b")},
            "wins": sum(D(x) > 0 for x in ds), "ties": sum(D(x) == 0 for x in ds), "losses": sum(D(x) < 0 for x in ds)})
    averages = []
    for version in ("original", "a", "b"):
        rows = [r for r in cells if r["version"] == version]
        complete = len(rows) == len(topics) and all(r["clean_n"] for r in rows)
        averages.append({"version": version, "topics": len(rows), "n": sum(r["n"] for r in rows),
            "clean_n": sum(r["clean_n"] for r in rows),
            **{key: mean(r[key] for r in rows) if complete else None
               for key in ("quality", "accuracy", "coverage", "insight", "errors", "omissions")}})
    clean = [p for p in pairs if p["clean"]]
    complete = len(comparisons) == len(topics) and all(c["clean_pairs"] for c in comparisons)
    overall = {"pairs": len(pairs), "clean_pairs": len(clean), "graded_texts": len(samples),
        "contaminated_texts": sum(bool(s.get("contaminated")) for s in samples),
        **{k: mean(c[k] for c in comparisons) if complete else None for k in ("a_minus_original", "b_minus_original", "a_minus_b")},
        "wins": sum(p["a_minus_b"] > 0 for p in clean), "ties": sum(p["a_minus_b"] == 0 for p in clean),
        "losses": sum(p["a_minus_b"] < 0 for p in clean),
        "errors": {v: sum(s["errors"] for s in samples if s["version"] == v and not s.get("contaminated")) for v in ("original", "a", "b")},
        "omissions": {v: sum(s["omissions"] for s in samples if s["version"] == v and not s.get("contaminated")) for v in ("original", "a", "b")}}
    return {"cells": cells, "pairs": pairs, "comparisons": comparisons, "equal_topic_means": averages, "overall": overall}


def export(directory):
    root, p = paired.load(directory); checks = paired.verify(root)
    require(p.get("design") == "independent-reviewers", "Wrong report design")
    require(paired.status(root)["completed_revisions"] == p["new_revisions"], "Independent revisions are incomplete")
    packets = []
    for path in sorted((root / "grades").glob("*/state.json")):
        state = read(path); require(state["status"] == "completed", "Anonymous grading incomplete")
        packet = paired.grade_packet(root, state)
        seal = check_topic_seal(packet, state["input_seal_sha256"], state["mapping"])
        require(sha(packet / "seal.json") == state["seal_sha256"], "Anonymous grade changed")
        if state.get("external_packet"):
            target = root / state["archive_packet"]
            for source in packet.rglob("*"):
                if not source.is_file() or "__pycache__" in source.parts:
                    continue
                destination = target / source.relative_to(packet); destination.parent.mkdir(parents=True, exist_ok=True)
                if not destination.exists():
                    shutil.copyfile(source, destination)
                require(sha(destination) == sha(source), "Archived anonymous input/judgment differs")
        packets.append((path, state, packet, seal))
    require(len(packets) == p["grading_packets"], "Missing grading packet")
    # No mapping is exported or history revealed until all four independent
    # packets have been validated and sealed.
    all_seals = {s["unit"]: sha(packet / "seal.json") for _, s, packet, _ in packets}
    reveal = root / "identity-reveal.json"
    if reveal.exists():
        require(read(reveal)["all_topic_seals"] == all_seals, "Identity reveal seal differs")
    else:
        write(reveal, {"revealed_at": now(), "all_topic_seals": all_seals, "all_sealed_before_reveal": True})
    samples, grading, rubrics = [], [], {}
    for path, state, packet, seal in packets:
        gold = read(packet / "rubric.json"); rubrics[state["topic"]] = gold
        mapping, scores = read(state["mapping"])["entries"], read(packet / "scores.json")
        grading.append({"unit": state["unit"], "seal": seal, "execution": read(path.parent / "execution.json")})
        for label, entry in mapping.items():
            require(sha(entry["answer"]) == entry["answer_sha256"], "Scored source bytes changed")
            raw = Path(entry["answer"]).read_text(encoding="utf-8-sig")
            view = (packet / "answers" / (label + ".txt")).read_text(encoding="utf-8")
            score = original_score(scores[label], view, raw, entry["edits"])
            dimensions = {key: float(sum((D(score["points"][r["id"]]) for r in gold["rubric"] if r["dimension"] == key), Decimal(0)))
                          for key in ("accuracy", "coverage", "insight")}
            sample = {"pair_id": entry["pair_id"], "identity": entry["identity"], "topic": entry["topic"],
                "model": entry["model"], "cycle": entry["cycle"], "version": entry["version"], "anonymous_id": state["unit"] + "/" + label,
                "answer_sha256": entry["answer_sha256"], "answer_path": entry["answer"], "slot_id": entry["slot_id"],
                "quality_details": score, "anonymous_judgment_sha256": digest_score(scores[label]),
                **dimensions, **principle_score(score, gold), "actual_cost": None, "contaminated": False}
            if entry["version"] == "original":
                sample.update(published_quality=entry["published_quality"], performance_record=historical_performance(entry))
            else:
                run = paired.state(root, entry["unit"]); capture = validate_answer_entry(run["capture"])
                sample.update(reviewer_model=run["model"], reviewer_effort=run["effort"], reviewer_backend=run["backend"],
                    answer_source=capture["answer_source"], capture=capture, contaminated=run.get("contaminated", False),
                    audit=read(run["audit"]) if run.get("audit") else None)
                if run["backend"] == "app":
                    sample["execution"] = read(run["execution_receipt"])
                    terminal = read(Path(capture["answer_receipt"]).parent / "turn-completion.json")["polls"][0]["latestTurn"]
                    sample["incremental_minutes"] = terminal["durationMs"] / 60000
                else:
                    native = read(run["native_result"])
                    from independent_review import audit_interference
                    independence = audit_interference(Path(run["native_result"]).parent)
                    require(not independence["affected"], "Affected independent review must be invalidated before export")
                    sample.update(native_result=native, incremental_minutes=float(D(native["elapsed_s"]) / 60), independence_audit=independence)
            samples.append(sample)
    require(len(samples) == p["texts_to_grade"], "Scored text count differs")
    require(sum(len(s["quality_details"]["points"]) for s in samples) == p["rubric_items"], "Scoring item coverage differs")
    require(sum(len(s["quality_details"]["checks"]) for s in samples) == p["checkpoint_records"], "Checkpoint coverage differs")
    summary = aggregate(samples, p["topics"])
    require(len(summary["cells"]) == 12 and all(r["n"] == 5 for r in summary["cells"]), "Require twelve five-answer cells")
    prior = None
    if p.get("previous_batch"):
        previous = p["previous_batch"]; old = Path(previous["batch"])
        require(sha(old / "delivery/results.json") == previous["results_sha256"], "Previous report changed")
        source = read(old / "delivery/results.json")
        prior = {**previous, "model_equal_topic_means": source["summary"]["model_equal_topic_means"], "overall": source["overall"]}
    result = {"schema_version": 1, "design": p["design"], "scoring_version": 3, "source_model": p["source_model"],
        "reviewers": p["reviewers"], "summary": summary, "samples": samples, "grading": grading, "rubrics": rubrics,
        "checkpoint_transitions": checkpoint_transitions(samples), "previous_group": prior, "actual_cost": None,
        "limitations": p["limitations"], "concurrency": p["max_answer_concurrency"], "cli_shared_generation_lock": False}
    if (root / "independence-override.json").is_file():
        result["independence_override"] = read(root / "independence-override.json")
        result["limitations"] = [*p["limitations"],
            "用户在首批5个Neon会话启动后提示新增codex-peer技能，随后暂停新Neon启动、核对技能及公开工具调用。技能本身排除模型benchmark；后续15个会话增加独立任务禁令与CLI进程防递归标记。此标记仅利用已有脚本保护，不证明后台终端继承环境或技能目录不被发现。全部正式Neon原生调用逐份复查；发现技能激活或外部委派的尝试作废保留。"]
    if (root / "concurrency-activation.json").is_file():
        result["concurrency_activation"] = read(root / "concurrency-activation.json")
        result["limitations"] = [*result["limitations"], "按用户后续选择，Neon的20份全部完成后，将剩余Astra的并发上限由2提高至5；原40个位置、成功答案及评分标准不变。实际启动/完成和重叠条件变化已封存，不把任务耗时相加视为批次墙钟时间。"]
    dest = root / "delivery"; dest.mkdir(exist_ok=True)
    write(dest / "results.json", result)
    for name, rows in (("summary.csv", summary["cells"]), ("pairs.csv", summary["pairs"]),
                       ("means.csv", summary["equal_topic_means"]), ("checkpoint-transitions.csv", result["checkpoint_transitions"]["rows"])):
        csv_file(dest / name, rows)
    csv_file(dest / "samples.csv", samples, ["pair_id", "topic", "cycle", "version", "reviewer_model", "reviewer_effort", "reviewer_backend",
        "quality", "accuracy", "coverage", "insight", "raw_quality", "principle_penalty", "quality_cap", "errors", "omissions",
        "incremental_minutes", "published_quality", "contaminated", "answer_source", "answer_sha256", "actual_cost"])
    document = render(result); check_sensitive(document)
    (dest / "report.html").write_bytes(document.encode("utf-8"))
    if p.get("previous_batch"):
        shutil.copyfile(Path(p["previous_batch"]["batch"]) / "delivery/results.json", dest / "previous-results.json")
    checks.update(answers=len(samples), pairs=len(summary["pairs"]), grading_packets=len(packets),
        scoring_items=p["rubric_items"], checkpoint_records=p["checkpoint_records"], report_sha256=sha(dest / "report.html"),
        all_grades_sealed_before_reveal=True, csv_json_consistent=True)
    browser = dest / "headless-validation.json"
    if browser.exists():
        b = read(browser); require(b["passed"] and b["report_sha256"] == checks["report_sha256"], "Stale headless validation")
        checks["headless_browser_passed"] = True
    write(dest / "validation.json", checks)
    files = {}
    for folder in ("cases", "baselines", "private", "units", "grades", "blind-packets"):
        for path in (root / folder).rglob("*"):
            if path.is_file() and "__pycache__" not in path.parts and path.suffix not in (".lock", ".stderr", ".raw"):
                files[path.relative_to(root).as_posix()] = path
    for pair in p["pairs"]:
        target = root / "historical-results" / pair["pair_id"] / "result.json"; target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            shutil.copyfile(pair["result"], target)
        require(sha(target) == pair["result_sha256"], "Historical performance archive changed")
        files[target.relative_to(root).as_posix()] = target
    for path in Path(p["work_root"]).rglob("*"):
        if path.is_file() and "__pycache__" not in path.parts and path.suffix not in (".stderr", ".raw"):
            files["runtime/" + path.relative_to(p["work_root"]).as_posix()] = path
    for path in list(root.glob("*.json")) + list(dest.iterdir()):
        if path.is_file() and path.name not in ("result-package.zip", "package-manifest.json"):
            files[path.relative_to(root).as_posix()] = path
    hashes = {name: sha(path) for name, path in sorted(files.items())}
    archive = dest / "result-package.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        for name, path in sorted(files.items()):
            z.write(path, name)
    with zipfile.ZipFile(archive) as z:
        require(set(z.namelist()) == set(hashes), "ZIP member count differs")
        require(all(hashlib.sha256(z.read(k)).hexdigest() == v for k, v in hashes.items()), "ZIP bytes differ")
    write(dest / "package-manifest.json", {"archive": archive.name, "sha256": sha(archive), "entries": len(hashes), "files_sha256": hashes, "verified": True})
    return {"delivery": str(dest), "overall": summary["overall"], "means": summary["equal_topic_means"], "validation": checks}


def digest_score(score):
    from core import digest
    return digest(score)


def csv_file(path, rows, fields=None):
    require(rows, "Cannot export empty CSV")
    stream = io.StringIO(newline=""); writer = csv.DictWriter(stream, fieldnames=fields or list(rows[0]), extrasaction="ignore", lineterminator="\n")
    writer.writeheader(); writer.writerows(rows); path.write_bytes(b"\xef\xbb\xbf" + stream.getvalue().encode("utf-8"))
    with path.open(encoding="utf-8-sig", newline="") as f:
        require(len(list(csv.DictReader(f))) == len(rows), "CSV count differs")


def render(result):
    e = lambda value: html.escape(str(value), quote=True)
    n = lambda value: "未知" if value is None else f"{D(value):.2f}"
    signed = lambda value: "未知" if value is None else f"{D(value):+.2f}"
    summary = result["summary"]; index = {(r["topic"], r["version"]): r for r in summary["cells"]}
    labels = {"original": "Neon 原稿", **{r["arm"]: r["label"] for r in result["reviewers"]}}
    def dimensions(row):
        return "<small class='dimensions current'>" + "".join("<span>" + label + "<strong>" + n(row[k]) + "/" + str(maximum) + "</strong></span>"
            for label, k, maximum in (("准确", "accuracy", 70), ("覆盖", "coverage", 20), ("洞察", "insight", 10))) + "</small>"
    def cell(row, original=None):
        delta = float(D(row["quality"]) - D(original["quality"])) if original and row["quality"] is not None and original["quality"] is not None else None
        comparison = "相对原稿 " + signed(delta) if original else "本轮原稿评分"
        return ("<b>" + n(row["quality"]) + "</b><small>错 " + n(row["errors"]) + " / 漏 " + n(row["omissions"]) + "</small>" + dimensions(row) +
            "<small class='history'>" + comparison + "</small>" + ("<small>干净样本 " + str(row["clean_n"]) + "/" + str(row["n"]) + "</small>" if row["clean_n"] != row["n"] else ""))
    rows = []
    for topic, title in zip(paired.TOPICS, paired.TITLES):
        original = index[topic, "original"]; comparison = next(c for c in summary["comparisons"] if c["topic"] == topic)
        rows.append("<tr data-topic='" + topic + "'><th>" + title + "</th><td class='baseline'>" + cell(original) + "</td>" +
            "".join("<td data-version='" + v + "'>" + cell(index[topic, v], original) + "</td>" for v in ("a", "b")) +
            "<td>" + signed(comparison["a_minus_b"]) + "</td></tr>")
    averages = {r["version"]: r for r in summary["equal_topic_means"]}
    rows.append("<tr data-topic='mean'><th>四题等权均分</th><td class='baseline'>" + cell(averages["original"]) + "</td>" +
        "".join("<td data-version='" + v + "'>" + cell(averages[v], averages["original"]) + "</td>" for v in ("a", "b")) +
        "<td>" + signed(summary["overall"]["a_minus_b"]) + "</td></tr>")
    o = summary["overall"]
    doc = """<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Neon 原稿 · 双模型独立复查</title><style>
:root{color-scheme:light dark;font:14px/1.5 system-ui,'Microsoft YaHei',sans-serif}body{margin:0;background:#f4f6f8;color:#182532}main{max-width:1100px;margin:auto;padding:18px}h1{font-size:23px}h2{font-size:18px}table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}th,td{padding:10px;border-bottom:1px solid #d9e1e9;text-align:center}th:first-child{text-align:left}small{display:block;font-size:11px;color:#657688}td b{font-size:18px}.card,details{background:white;border:1px solid #d5dfe8;border-radius:9px;padding:12px;margin:12px 0}.scroll{overflow-x:auto}.dimensions{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:5px;max-width:210px;margin:4px auto}.dimensions strong{display:block;font-size:11px;font-weight:500}.notice{background:#fff2cf;border-left:4px solid #ad7600;padding:10px}summary,label{cursor:pointer}summary{font-weight:600}pre{white-space:pre-wrap;overflow-wrap:anywhere}blockquote{border-left:3px solid #9db8cf;margin:5px 0;padding:8px;background:#eef4f8}.evidence td{text-align:left;vertical-align:top}.evidence th{width:90px}.baseline,.history{display:none}#history:checked~main .baseline{display:table-cell}#history:checked~main .history{display:block}#history:checked~main .current{display:none}input[name=mode]{margin:20px 6px 0 18px}a{color:#145da3}@media(max-width:760px){main{padding:10px}.matrix{min-width:760px}}@media(prefers-color-scheme:dark){body{background:#161c23;color:#e0e8ef}.card,details{background:#202932;border-color:#405261}small{color:#afbdca}blockquote{background:#293744}.notice{background:#3b301a}a{color:#91c8ff}}
</style><input id="current" name="mode" type="radio" checked><label for="current">仅看修订</label><input id="history" name="mode" type="radio"><label for="history">显示原稿对照</label><main>
<h1>Neon 原稿 · Astra 与 Neon 独立复查</h1><p>20份历史原稿 · 40份独立修订 · 60份同轮匿名评分 · V3准确70／覆盖20／洞察10</p>"""
    doc += "<div class='notice'>Astra−Neon 四题等权均值 " + signed(o["a_minus_b"]) + "分；Astra胜／平／负 " + str(o["wins"]) + "/" + str(o["ties"]) + "/" + str(o["losses"]) + "，干净配对 " + str(o["clean_pairs"]) + "/20。两组独立修订同一底稿，均未读取另一组结果。</div>"
    doc += "<div class='card scroll'><table id='matrix' class='matrix'><thead><tr><th>题目</th><th class='baseline'>Neon 原稿</th>" + "".join("<th>" + e(r["label"]) + "<small>" + e(r["model"]) + " / " + e(r["effort"]) + "</small></th>" for r in result["reviewers"]) + "<th>Astra−Neon</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table></div><small>每格每版本n=5；基础三项之和可能高于原则扣分、封顶后的最终分。原稿分数也来自本轮评分，旧发布分仅作溯源。时长见逐份详情。</small>"
    doc += "<p>" + " · ".join("<a href='" + f + "'>" + label + "</a>" for f, label in (("summary.csv", "分组CSV"), ("pairs.csv", "配对CSV"), ("means.csv", "均分CSV"), ("samples.csv", "逐份CSV"), ("results.json", "JSON"), ("result-package.zip", "完整ZIP"))) + "</p>"
    prior = result.get("previous_group")
    if prior:
        body = "".join("<tr><th>" + e(r["model"]) + "</th>" + "".join("<td>" + n(r[k]) + "</td>" for k in ("original", "a", "b", "b_minus_a")) + "</tr>" for r in prior["model_equal_topic_means"])
        doc += "<details id='previous-group'><summary>上一组：Codex 自查／Codex＋AGY 辅助（原300份评分未改）</summary><div class='scroll'><table><thead><tr><th>来源模型</th><th>原稿</th><th>Codex自查</th><th>Codex＋AGY</th><th>辅助−自查</th></tr></thead><tbody>" + body + "</tbody></table></div><p>本组输入来源和比较机制不同，分开解释，不合并为统一排名。<a href='" + e((Path(prior["batch"]) / "delivery/report.html").as_uri()) + "'>上一组完整报告（本机原交付）</a> · <a href='previous-results.json'>原始结果JSON副本</a></p></details>"
    doc += "<details><summary>实验条件与限制</summary><ul>" + "".join("<li>" + e(x) + "</li>" for x in result["limitations"]) + "</ul><p>2路Astra、5路Neon并行；Neon CLI使用会话级Full Access，逐份独立project与scratch，无共享生成锁，自身在途仍受保护。用量和时长按各后端原生回执保留，费用未知。污染样本保留评分证据，但排除干净统计。</p></details><h2>逐份评分、证据与性能</h2>"
    for s in sorted(result["samples"], key=lambda s: (s["topic"], s["cycle"], s["version"])):
        gold, score = result["rubrics"][s["topic"]], s["quality_details"]
        items = "".join("<tr><th>" + e(r["id"]) + "<small>" + n(score["points"][r["id"]]) + "/" + n(r["max"]) + "</small></th><td>" + e(r["criterion"]) + "<p>" + e(score["notes"][r["id"]]) + "</p><blockquote>" + e("\n".join(score["evidence"][r["id"]])) + "</blockquote></td></tr>" for r in gold["rubric"])
        doc += "<details data-sample='" + e(s["anonymous_id"]) + "'><summary>" + e(s["pair_id"]) + " · " + labels[s["version"]] + " · " + n(s["quality"]) + "分 · 错" + str(s["errors"]) + "/漏" + str(s["omissions"]) + (" · 污染标记" if s.get("contaminated") else "") + "</summary><p>基础 " + n(s["raw_quality"]) + "；原则扣分 " + n(s["principle_penalty"]) + "；封顶 " + n(s["quality_cap"]) + "</p><table class='evidence'>" + items + "</table><details><summary>全部核验点与问题</summary><pre>" + e(json.dumps(score, ensure_ascii=False, indent=2)) + "</pre></details><details><summary>原始技术答案</summary><pre>" + e(Path(s["answer_path"]).read_text(encoding="utf-8-sig")) + "</pre></details><details><summary>性能及审计</summary><pre>" + e(json.dumps({k: s.get(k) for k in ("incremental_minutes", "execution", "native_result", "performance_record", "audit", "independence_audit", "published_quality", "actual_cost")}, ensure_ascii=False, indent=2)) + "</pre></details></details>"
    return doc + "</main></html>"
