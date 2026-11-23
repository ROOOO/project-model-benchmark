"""Static paired-review export; exact decimal aggregates and sealed evidence."""
from collections import defaultdict,Counter
from decimal import Decimal
from pathlib import Path
import csv,hashlib,html,io,json,statistics,zipfile
from core import read,write,sha,require,principle_score,check_sensitive
from fresh_blind import check_topic_seal
import paired

D=lambda x:Decimal(str(x))
def mean(values):
    values=list(values);require(bool(values),"Cannot aggregate an empty paired cell")
    return float(sum(map(D,values),Decimal(0))/len(values))


def peer_run_counts(all_runs,created_at):
    """Count failed attempts separately without misclassifying them as reused runs."""
    from datetime import datetime
    records={r["run_id"]:r for r in all_runs}
    assisting={rid for rid,r in records.items() if r["assistance_accepted"]}
    generated=assisting|{rid for rid,r in records.items() if r.get("reported_turns") and r["reported_turns"]>0}
    new=set()
    for run_id,record in records.items():
        run=read(Path(record["state_dir"])/"runs"/run_id/"run.json")
        if datetime.fromisoformat(run["started_at"])>=datetime.fromisoformat(created_at):new.add(run_id)
    return {"peer_run_references":len(all_runs),"unique_peer_runs":len(records),
        "unique_assisting_peer_runs":len(assisting),
        "verified_zero_turn_failures":sum(r["status"]=="failed" and r.get("reported_turns")==0 for r in records.values()),
        "verified_stopped_failures":sum(r["status"]=="resolved-stopped" for r in records.values()),
        "unusable_terminal_attempts":sum(r["status"]=="unusable-terminal" for r in records.values()),
        "peer_run_count_scope":"Run attempts; verified zero-turn failures are not generation calls. "
            "Stopped attempts without native usage retain unknown counters; known generation includes unusable nonzero-turn attempts.",
        "new_generation_runs_in_batch":len(new&generated),"new_accepted_analysis_runs_in_batch":len(new&assisting),"new_peer_runs_in_batch":len(new),
        "prior_peer_runs_reused":len(records.keys()-new),"duplicate_run_references":len(all_runs)-len(records)}


def performance_summary(samples):
    """Task durations can overlap; never label their sum as batch wall time."""
    revisions=[s for s in samples if s["version"] in {"a","b"}]
    pairs=defaultdict(dict)
    for sample in revisions:
        require(sample["version"] not in pairs[sample["pair_id"]],"Duplicate paired performance")
        pairs[sample["pair_id"]][sample["version"]]=sample
    rows=[]
    for model in paired.MODELS:
        items=[s for s in revisions if s["model"]==model]
        if not items:continue
        durations={arm:[s["incremental_minutes"] for s in items if s["version"]==arm
            and s.get("incremental_minutes") is not None] for arm in ("a","b")}
        differences=[float(D(arms["b"]["incremental_minutes"])-D(arms["a"]["incremental_minutes"]))
            for arms in pairs.values() if set(arms)=={"a","b"} and arms["a"]["model"]==model
            and all(s.get("incremental_minutes") is not None for s in arms.values())]
        rows.append({"model":model,"a_observed":len(durations["a"]),"b_observed":len(durations["b"]),
            "paired_observed":len(differences),
            **{arm+"_mean_minutes":mean(values) if values else None for arm,values in durations.items()},
            **{arm+"_median_minutes":float(statistics.median(map(D,values))) if values else None
                for arm,values in durations.items()},
            "b_minus_a_mean_minutes":mean(differences) if differences else None,"actual_cost":None})
    return {"scope":"Per-answer task elapsed time; overlapping tasks are not summed as batch wall time. "
            "B includes peer waiting and verification; adopted pilot durations are retained.","models":rows,"actual_cost":None}


def checkpoint_transitions(samples):
    """Compare frozen checkpoint labels without inventing causal issue matches."""
    pairs=defaultdict(dict)
    for sample in samples:pairs[sample["pair_id"]][sample["version"]]=sample
    rows=[]
    for pair_id,versions in sorted(pairs.items()):
        require(set(versions)=={"original","a","b"},"Checkpoint comparison needs all three texts")
        for left,right in (("original","a"),("original","b"),("a","b")):
            before=versions[left]["quality_details"]["checks"];after=versions[right]["quality_details"]["checks"]
            require(set(before)==set(after),"Frozen checkpoint IDs differ across paired texts")
            changed=[{"check_id":cid,"before":before[cid],"after":after[cid]} for cid in before
                if before[cid]["status"]!=after[cid]["status"]]
            rows.append({"pair_id":pair_id,"topic":versions[right]["topic"],"model":versions[right]["model"],
                "comparison":left+"_to_"+right,"checkpoints":len(before),"changed":changed,
                **{name:sum(before[cid]["status"]!=target and after[cid]["status"]==target for cid in before)
                    for name,target in (("newly_marked_errors","error"),("newly_marked_omissions","omission"))},
                **{name:sum(before[cid]["status"]==target and after[cid]["status"]=="correct" for cid in before)
                    for name,target in (("corrected_errors","error"),("filled_omissions","omission"))}})
    return {"scope":"Frozen-checkpoint label transitions; a newly marked error is not a new causal issue count. "
            "Extension claims and distinct issues under one checkpoint remain in the full issue evidence.","rows":rows}


def external_source_files(root,protocol):
    """Include original performance bytes and adopted capture proofs in the ZIP."""
    files={}
    for pair in protocol["pairs"]:
        files["historical-results/"+pair["pair_id"]+"/result.json"]=Path(pair["result"])
        published=pair["published_source"]
        files["historical-summaries/"+published["summary_sha256"]+".json"]=Path(published["summary"])
    for unit in protocol["units"]:
        record=paired.state(root,unit)
        if not record.get("reused_from"):continue
        capture=Path(record["capture"])
        for path in capture.rglob("*"):
            if path.is_file():files["adopted-pilot/"+unit+"/captured/"+path.relative_to(capture).as_posix()]=path
        files["adopted-pilot/"+unit+"/audit.json"]=Path(record["audit"])
        files["adopted-pilot/"+unit+"/execution.json"]=Path(record["execution_receipt"])
    return files


def aggregate(samples):
    groups=defaultdict(list);by_pair=defaultdict(dict)
    for s in samples:
        groups[s["topic"],s["model"],s["version"]].append(s)
        require(s["version"] not in by_pair[s["pair_id"]],"Duplicate paired score")
        by_pair[s["pair_id"]][s["version"]]=s
    rows=[]
    for (topic,model,version),items in sorted(groups.items()):
        values=[s["quality"] for s in items]
        rows.append({"topic":topic,"model":model,"version":version,"n":len(items),
            **{k:mean(s[k] for s in items) for k in ("quality","accuracy","coverage","insight","errors","omissions")},
            "median":float(statistics.median(map(D,values))),"minimum":min(values),"maximum":max(values),
            "sample_stdev":statistics.stdev(values) if len(values)>1 else None,"actual_cost":None})
    pairs=[]
    for pair_id,versions in sorted(by_pair.items()):
        require(set(versions)=={"original","a","b"},"Pair missing one score version")
        b=versions["original"]
        delta=lambda x,y:float(D(versions[x]["quality"])-D(versions[y]["quality"]))
        pairs.append({"pair_id":pair_id,"topic":b["topic"],"model":b["model"],"cycle":b["cycle"],
            **{v+"_quality":versions[v]["quality"] for v in ("original","a","b")},
            "a_minus_original":delta("a","original"),"b_minus_original":delta("b","original"),"b_minus_a":delta("b","a"),
            "a_error_count_delta":versions["a"]["errors"]-b["errors"],"b_error_count_delta":versions["b"]["errors"]-b["errors"],
            "a_omission_count_delta":versions["a"]["omissions"]-b["omissions"],"b_omission_count_delta":versions["b"]["omissions"]-b["omissions"]})
    comparisons=[]
    for topic in paired.TOPICS:
        for model in paired.MODELS:
            items=[p for p in pairs if p["topic"]==topic and p["model"]==model]
            if not items:continue
            ds=[p["b_minus_a"] for p in items]
            comparisons.append({"topic":topic,"model":model,"n":len(items),"b_minus_a_mean":mean(ds),
                "b_minus_a_median":float(statistics.median(map(D,ds))),"minimum":min(ds),"maximum":max(ds),
                "wins":sum(D(x)>0 for x in ds),"ties":sum(D(x)==0 for x in ds),"losses":sum(D(x)<0 for x in ds),
                "a_minus_original":mean(p["a_minus_original"] for p in items),
                "b_minus_original":mean(p["b_minus_original"] for p in items)})
    averages=[]
    for model in paired.MODELS:
        cells=[c for c in comparisons if c["model"]==model]
        if not cells:continue
        require(len(cells)==4,"Equal-topic model average needs all four topics")
        averages.append({"model":model,"topics":4,"pairs":sum(c["n"] for c in cells),
            "b_minus_a":mean(c["b_minus_a_mean"] for c in cells),
            "a_minus_original":mean(c["a_minus_original"] for c in cells),
            "b_minus_original":mean(c["b_minus_original"] for c in cells),
            **{v:mean(r["quality"] for r in rows if r["model"]==model and r["version"]==v) for v in ("original","a","b")},
            "wins":sum(c["wins"] for c in cells),"ties":sum(c["ties"] for c in cells),"losses":sum(c["losses"] for c in cells)})
    return {"cells":rows,"pairs":pairs,"comparisons":comparisons,"model_equal_topic_means":averages}


def historical_performance(pair):
    """Keep native metrics, linking exact archival traces instead of duplicating them."""
    source=Path(pair["result"]);record=read(source)
    performance={k:v for k,v in record.items() if k!="agentic"}
    agentic=record.get("agentic")
    if isinstance(agentic,dict):
        performance["agentic_summary"]={k:agentic[k] for k in ("schema_version","root_thread_id","root_turn_id",
            "initial_settings","initial_root_turn_id","root_turn_order","started_at","tree_terminal",
            "accounting_complete","accounting_issues","discovery_complete","final_answer_policy") if k in agentic}
        performance["agentic_summary"]["trace_item_counts"]={k:len(agentic[k]) if isinstance(agentic.get(k),list) else None
            for k in ("nodes","responses","messages","tools","errors","events")}
    performance["source_result"]={"path":str(source),"sha256":sha(source),
        "report_relative_path":"../historical-results/"+pair["pair_id"]+"/result.json",
        "scope":"Native performance fields retained; full original result is preserved byte-for-byte in the archive."}
    return performance


def export(directory):
    root,p=paired.load(directory);verification=paired.verify(root)
    require(paired.status(root)["completed_revisions"]==200,"Cannot export incomplete paired revisions")
    samples=[];grading=[];rubrics={}
    for path in sorted((root/"grades").glob("*/state.json")):
        state=read(path);require(state["status"]=="completed","Grading is not completed")
        packet=paired.grade_packet(root,state)
        seal=check_topic_seal(packet,state["input_seal_sha256"],state["mapping"])
        require(sha(packet/"seal.json")==state["seal_sha256"],"Grading seal changed")
        gold=read(packet/"rubric.json");rubrics[state["topic"]]=gold
        mapping=read(state["mapping"])["entries"];scores=read(packet/"scores.json")
        grading.append({"unit":state["unit"],"seal":seal,"execution":read(path.parent/"execution.json")})
        for label,e in mapping.items():
            require(sha(e["answer"])==e["answer_sha256"],"Scored original bytes changed")
            score=scores[label]
            dimensions={key:float(sum((D(score["points"][r["id"]]) for r in gold["rubric"] if r["dimension"]==key),Decimal(0)))
                for key in ("accuracy","coverage","insight")}
            s={"pair_id":e["pair_id"],"identity":e["identity"],"topic":e["topic"],"model":e["model"],"cycle":e["cycle"],
                "version":e["version"],"anonymous_id":state["unit"]+"/"+label,"answer_sha256":e["answer_sha256"],
                "answer_path":e["answer"],"slot_id":e["slot_id"],"published_quality":e.get("published_quality") if e["version"]=="original" else None,
                "quality_details":score,**dimensions,**principle_score(score,gold),"actual_cost":None}
            if e["version"]!="original":
                run=paired.state(root,e["unit"]);execution=read(run["execution_receipt"]);audit=read(run["audit"])
                capture=Path(run["capture"]);proof=paired.validate_capture(capture/"answer-receipt.json")
                terminal=read(capture/"turn-completion.json")["polls"][0]["latestTurn"]
                s.update(execution=execution,audit=audit,answer_source=proof["answer_source"],
                    answer_turn_minutes=terminal["durationMs"]/60000,
                    incremental_minutes=run.get("elapsed_ms_total",terminal["durationMs"])/60000,reused_from=run.get("reused_from"))
                if e["version"]=="b":s["peer_analysis"]=paired.validate_peer(root,run,audit)
            else:s["performance_record"]=historical_performance(e)
            samples.append(s)
    require(len(grading)==20 and len(samples)==300,"Expected 20 grading packets and 300 texts")
    require(sum(len(s["quality_details"]["points"]) for s in samples)==3225,"Scoring-item coverage differs")
    require(sum(len(s["quality_details"]["checks"]) for s in samples)==5100,"Checkpoint coverage differs")
    summary=aggregate(samples);require(len(summary["pairs"])==100 and len(summary["cells"])==60,"Paired aggregate coverage differs")
    require(all(r["n"]==5 for r in summary["cells"]),"Each score cell needs five answers")
    deltas=[r["b_minus_a"] for r in summary["pairs"]]
    all_runs=[r for s in samples if s["version"]=="b" for r in s["peer_analysis"]]
    run_ids={r["run_id"] for r in all_runs}
    scheduling=read(root/"scheduling-update.json") if (root/"scheduling-update.json").is_file() else None
    audit_corrections=read(root/"audit-corrections.json") if (root/"audit-corrections.json").is_file() else None
    recovery=read(root/"recovery-deviations.json") if (root/"recovery-deviations.json").is_file() else None
    quota_changes=read(root/"quota-overrides.json") if (root/"quota-overrides.json").is_file() else None
    concurrency=read(root/"concurrency-override.json") if (root/"concurrency-override.json").is_file() else None
    limitations=list(p["limitations"])
    limitations.append("历史性能字段原样展示；大型原始事件轨迹不重复嵌入页面或汇总JSON，原始result.json按原字节保存在结果包和历史记录入口中。")
    limitations.append("原稿来自历史采样，A与B在新的App会话中复核；原稿到修订稿的分差同时包含重新审阅与运行条件差异。评估AGY辅助增益主要看同模型的B−A；B还使用额外计算，并非等算力对照。")
    if scheduling:limitations.append(scheduling["report_disclosure"])
    if audit_corrections:limitations.append("部分会话在原对话补齐公开审计；保留首次技术正文，补正指令不授权新AGY请求，补正任务耗时已计入。逐项记录见audit_corrections，实际外部调用仍以各份回执为准。")
    if recovery:limitations.append(recovery["report_disclosure"])
    if quota_changes:limitations.append(quota_changes["report_disclosure"])
    if concurrency:limitations.append(concurrency["report_disclosure"])
    results={"schema_version":1,"scoring_version":3,"protocol":p,"summary":summary,"samples":samples,"graders":grading,
        "overall":{"pairs":100,"b_minus_a":mean(deltas),"wins":sum(x>0 for x in deltas),
            "ties":sum(x==0 for x in deltas),"losses":sum(x<0 for x in deltas),
            **peer_run_counts(all_runs,p["created_at"]),"adopted_answer_texts":2,"actual_cost":None},
        "limitations":limitations,"scheduling_update":scheduling,"audit_corrections":audit_corrections,
        "recovery_deviations":recovery,"quota_overrides":quota_changes,"concurrency_override":concurrency,
        "performance":performance_summary(samples),"checkpoint_transitions":checkpoint_transitions(samples),
        "rubrics":rubrics,"actual_cost":None}
    dest=root/"delivery";dest.mkdir(exist_ok=True);write(dest/"results.json",results)
    def csv_file(name,rows,fields):
        stream=io.StringIO(newline="");writer=csv.DictWriter(stream,fieldnames=fields,extrasaction="ignore",lineterminator="\n")
        writer.writeheader();writer.writerows(rows);(dest/name).write_bytes(b"\xef\xbb\xbf"+stream.getvalue().encode("utf-8"))
        with (dest/name).open(encoding="utf-8-sig",newline="") as f:require(len(list(csv.DictReader(f)))==len(rows),"CSV count mismatch")
    csv_file("summary.csv",summary["cells"],list(summary["cells"][0]))
    csv_file("pairs.csv",summary["pairs"],list(summary["pairs"][0]))
    csv_file("models.csv",summary["model_equal_topic_means"],list(summary["model_equal_topic_means"][0]))
    csv_file("performance.csv",results["performance"]["models"],list(results["performance"]["models"][0]))
    csv_file("checkpoint-transitions.csv",results["checkpoint_transitions"]["rows"],
        ["pair_id","topic","model","comparison","checkpoints","newly_marked_errors","newly_marked_omissions","corrected_errors","filled_omissions"])
    csv_file("samples.csv",samples,["pair_id","topic","model","cycle","version","quality","accuracy","coverage","insight",
        "raw_quality","principle_penalty","quality_cap","errors","omissions","answer_turn_minutes","incremental_minutes","published_quality","answer_sha256","actual_cost"])
    document=render(results);check_sensitive(document);(dest/"report.html").write_bytes(document.encode("utf-8"))
    checks={**verification,"answers":300,"pairs":100,"grading_packets":20,"scoring_items":3225,"checkpoint_records":5100,
        "unique_peer_runs":len(run_ids),"report_sha256":sha(dest/"report.html"),"csv_json_consistent":True}
    browser=dest/"headless-validation.json"
    if browser.exists():
        b=read(browser);require(b["passed"] and b["report_sha256"]==checks["report_sha256"],"Headless receipt is stale")
        checks["headless_browser_passed"]=True
    write(dest/"validation.json",checks)
    files={}
    for folder in ("cases","baselines","private","units","grades","blind-packets"):
        for path in (root/folder).rglob("*"):
            if path.is_file() and "__pycache__" not in path.parts:files[folder+"/"+path.relative_to(root/folder).as_posix()]=path
    for path in root.glob("*.json"):files[path.name]=path
    for path in dest.iterdir():
        if path.is_file() and path.name not in ("result-package.zip","package-manifest.json"):files["delivery/"+path.name]=path
    files.update(external_source_files(root,p))
    for name,source in files.copy().items():
        if name.startswith("historical-results/"):
            destination=root/name;destination.parent.mkdir(parents=True,exist_ok=True)
            if not destination.exists():destination.write_bytes(source.read_bytes())
            require(sha(destination)==sha(source),"Local historical archive differs")
    # Global peer receipts are public run artifacts, never session/auth/config caches.
    for run_id in sorted(run_ids):
        run=next(r for r in all_runs if r["run_id"]==run_id);base=Path(run["state_dir"])/"runs"/run_id
        for path in base.rglob("*"):
            if path.is_file() and "__pycache__" not in path.parts and path.suffix not in (".stderr",".raw"):
                files["peer-runs/"+run_id+"/"+path.relative_to(base).as_posix()]=path
    hashes={name:sha(path) for name,path in sorted(files.items())}
    archive=dest/"result-package.zip"
    with zipfile.ZipFile(archive,"w",zipfile.ZIP_DEFLATED) as z:
        for name,path in sorted(files.items()):z.write(path,name)
    with zipfile.ZipFile(archive) as z:
        require(set(z.namelist())==set(hashes),"ZIP members mismatch")
        require(all(hashlib.sha256(z.read(k)).hexdigest()==v for k,v in hashes.items()),"ZIP bytes mismatch")
    write(dest/"package-manifest.json",{"archive":archive.name,"sha256":sha(archive),"entries":len(hashes),"files_sha256":hashes,"verified":True})
    return {"delivery":str(dest),"overall":results["overall"],"model_means":summary["model_equal_topic_means"],"validation":checks}


def render(results):
    e=lambda x:html.escape(str(x),quote=True)
    n=lambda x:f"{x:.2f}"
    summary=results["summary"];index={(r["topic"],r["model"],r["version"]):r for r in summary["cells"]}
    differences={(r["topic"],r["model"]):r for r in summary["comparisons"]}
    def dimensions(row):
        return "<small class='dimensions'>"+"".join("<span>"+label+"<strong>"+n(row[key])+"/"+str(maximum)+
            "</strong></span>" for key,label,maximum in (("accuracy","准确",70),("coverage","覆盖",20),("insight","洞察",10)))+"</small>"
    def cell(topic,model):
        stages={v:index[topic,model,v] for v in ("original","a","b")};b=stages["b"];d=differences[topic,model]
        return ("<td><span class='current'><b>"+n(b["quality"])+"</b>"+dimensions(b)+"<small>错误 "+n(b["errors"])+" · 遗漏 "+n(b["omissions"])+
            "</small></span><span class='compare'><b>"+" → ".join(n(stages[v]["quality"]) for v in ("original","a","b"))+
            "</b><small>原稿 → A自查 → B辅助</small><small>B−A "+f"{d['b_minus_a_mean']:+.2f}"+
            " · 胜/平/负 "+str(d["wins"])+"/"+str(d["ties"])+"/"+str(d["losses"])+"</small></span></td>")
    rows=[]
    for topic,title in zip(paired.TOPICS,paired.TITLES):
        rows.append("<tr><th>"+title+"</th>"+"".join(cell(topic,m) for m in paired.MODELS)+"</tr>")
    avg={r["model"]:r for r in summary["model_equal_topic_means"]}
    cells=[]
    for model in paired.MODELS:
        r=avg[model];dims={key:mean(index[topic,model,"b"][key] for topic in paired.TOPICS)
            for key in ("accuracy","coverage","insight")}
        cells.append("<td><span class='current'><b>"+n(r["b"])+"</b>"+dimensions(dims)+"<small>四题等权</small></span>"+
            "<span class='compare'><b>"+n(r["original"])+" → "+n(r["a"])+" → "+n(r["b"])+"</b><small>B−A "+f"{r['b_minus_a']:+.2f}"+
            "</small></span></td>")
    rows.append("<tr><th>四题等权均分</th>"+"".join(cells)+"</tr>")
    detail=[]
    for sample in sorted(results["samples"],key=lambda s:(s["pair_id"],s["version"])):
        s=sample;score=s["quality_details"];gold=results["rubrics"][s["topic"]]
        source=s.get("performance_record",{}).get("source_result")
        historical_link=("<p><a href='"+e(source["report_relative_path"])+"'>原始性能记录（完整原字节）</a></p>") if source else ""
        items="".join("<tr><th>"+e(r["id"])+"<small>"+n(score["points"][r["id"]])+"/"+n(r["max"])+"</small></th><td>"+
            e(r["criterion"])+"<p>"+e(score["notes"][r["id"]])+"</p><blockquote>"+e("\n".join(score["evidence"][r["id"]]))+"</blockquote></td></tr>" for r in gold["rubric"])
        detail.append("<details data-sample='"+e(s["anonymous_id"])+"'><summary>"+e(s["pair_id"])+" · "+e(s["model"])+" · "+
            e(s["version"])+" · "+n(s["quality"])+"分 · 错"+str(s["errors"])+"/漏"+str(s["omissions"])+"</summary><p>基础 "+
            n(s["raw_quality"])+"；原则扣分 "+n(s["principle_penalty"])+"；封顶 "+n(s["quality_cap"])+"</p><table class='evidence'>"+
            items+"</table><details><summary>全部核验点和问题</summary><pre>"+e(json.dumps(score,ensure_ascii=False,indent=2))+
            "</pre></details><details><summary>原始技术答案</summary><pre>"+e(Path(s["answer_path"]).read_text(encoding="utf-8-sig"))+
            "</pre></details><details><summary>性能、审计和AGY处置</summary>"+historical_link+"<pre>"+e(json.dumps({k:s.get(k) for k in
                ("answer_turn_minutes","incremental_minutes","execution","performance_record","audit","peer_analysis","actual_cost")},ensure_ascii=False,indent=2))+"</pre></details></details>")
    overall=results["overall"]
    doc="""<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>五模型四题 A/B 复核</title><style>
:root{color-scheme:light dark;font:14px/1.5 system-ui,'Microsoft YaHei',sans-serif}body{margin:0;background:#f4f6f8;color:#17212b}
main,.controls{max-width:1480px;margin:auto;padding:16px}h1{font-size:24px}h2{font-size:18px}small{display:block;font-size:11px;color:#667787}
.card,details{background:white;border:1px solid #d6dfe8;border-radius:9px;padding:10px;margin:10px 0}.scroll{overflow-x:auto}
table{width:100%;border-collapse:collapse;white-space:nowrap}th,td{padding:10px;border-bottom:1px solid #dce3ea;text-align:center;font-variant-numeric:tabular-nums}
th:first-child{text-align:left}td b{font-size:17px}summary{cursor:pointer;font-weight:600}pre{white-space:pre-wrap;overflow-wrap:anywhere}
.dimensions{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:4px;margin:2px auto;max-width:200px}.dimensions strong{display:block;font-size:11px;font-weight:500}
.evidence{white-space:normal}.evidence th{width:90px;vertical-align:top}.evidence td{text-align:left}blockquote{border-left:3px solid #aac1d7;padding:8px;background:#f0f4f8}
.notice{background:#fff4d9;border-left:4px solid #bc7800;padding:12px}.compare{display:none}#compare:checked~main .current{display:none}#compare:checked~main .compare{display:inline}
input[name=mode]{margin:20px 6px 0 20px}label{cursor:pointer}a{color:#125ca1}
@media(max-width:800px){main{padding:10px}.scroll table{min-width:1250px}}
@media(prefers-color-scheme:dark){body{background:#151b22;color:#e2e8ef}.card,details{background:#202832;border-color:#3d4b5a}blockquote{background:#293541}.notice{background:#382f1c}small{color:#a4b3c1}a{color:#8ac1f0}}
</style><input id="current" type="radio" name="mode" checked><label for="current">仅看B辅助</label><input id="compare" type="radio" name="mode"><label for="compare">原稿／A／B对照</label><main>
<h1>五个Codex配置 · 四题五轮 A/B复核</h1><p>100份历史底稿 · 200份修订稿 · 300份统一匿名评分 · V3准确70／覆盖20／洞察10</p>"""
    doc+=("<div class='notice'>B−A平均 "+f"{overall['b_minus_a']:+.2f}"+"分；胜/平/负 "+
        str(overall["wins"])+"/"+str(overall["ties"])+"/"+str(overall["losses"])+"。B使用额外AGY计算，此表比较工作流，不是等算力测试。</div>")
    doc+=("<div class='card scroll'><table id='matrix'><thead><tr><th>题目</th>"+"".join("<th>"+e(m)+" / max</th>" for m in paired.MODELS)+
        "</tr></thead><tbody>"+"".join(rows)+"</tbody></table></div><small>每格每版本n=5；三项为基础维度，最终分另受原则扣分与封顶影响。主表无时间；展开详情可查原始性能。</small>")
    doc+=("<p><a href='summary.csv'>分组CSV</a> · <a href='pairs.csv'>成对差值CSV</a> · <a href='models.csv'>等权均分CSV</a> · "+
        "<a href='samples.csv'>逐份CSV</a> · <a href='performance.csv'>任务耗时CSV</a> · <a href='checkpoint-transitions.csv'>核验点变化CSV</a> · <a href='results.json'>JSON</a> · <a href='result-package.zip'>结果包</a></p>")
    def duration(value):return "未知" if value is None else n(value)
    performance_rows="".join("<tr><th>"+e(r["model"])+"</th><td>"+duration(r["a_mean_minutes"])+
        "</td><td>"+duration(r["b_mean_minutes"])+"</td><td>"+duration(r["b_minus_a_mean_minutes"])+
        "</td><td>"+str(r["paired_observed"])+"</td></tr>" for r in results["performance"]["models"])
    doc+=("<details><summary>增量任务耗时（分钟）</summary><div class='scroll'><table><thead><tr><th>模型</th>"+
        "<th>A自查均值</th><th>B辅助均值</th><th>成对 B−A 均值</th><th>有效配对</th></tr></thead><tbody>"+
        performance_rows+"</tbody></table></div><p>B含外部请求等待、源码核验和整合；保留试跑原时长。"+
        "并行任务的时长不能相加当作整批墙钟时间。原稿生成性能仍保留在逐份详情；费用未知。</p></details>")
    transition_rows=[]
    for comparison,label in (("original_to_a","原稿→A"),("original_to_b","原稿→B"),("a_to_b","A→B")):
        items=[r for r in results["checkpoint_transitions"]["rows"] if r["comparison"]==comparison]
        transition_rows.append("<tr><th>"+label+"</th>"+"".join("<td>"+str(sum(r[k] for r in items))+"</td>"
            for k in ("corrected_errors","filled_omissions","newly_marked_errors","newly_marked_omissions"))+"</tr>")
    doc+=("<details><summary>固定核验点的错漏变化</summary><div class='scroll'><table><thead><tr><th>比较</th>"+
        "<th>错误→正确</th><th>遗漏→正确</th><th>新标为错误</th><th>新标为遗漏</th></tr></thead><tbody>"+
        "".join(transition_rows)+"</tbody></table></div><p>按相同冻结核验点比较本轮判断，不把错误数净变化称作新增错误数；"+
        "同一点内的不同因果问题及扩展内容仍需看完整证据。所有变化的前后原文与判断保存在JSON中。</p></details>")
    doc+=("<details><summary>解释与条件差异</summary><ul>"+"".join("<li>"+e(x)+"</li>" for x in results["limitations"])+
        "</ul><p>AGY有效分析run "+str(overall["unique_assisting_peer_runs"])+"，已核实零轮失败 "+str(overall["verified_zero_turn_failures"])+
        "，确认停止但无用量回执 "+str(overall.get("verified_stopped_failures",0))+
        "，已生成但无可用回答 "+str(overall.get("unusable_terminal_attempts",0))+
        "；失败记录保留，不当作有效分析；缺少回执的用量保持未知。历史发布分保留，本表分差仅使用本轮同包盲评分。费用未知，不将额度差值冒充精确收费。评分者已无法完全排除文风线索及共同偏差。</p></details>")
    doc+="<h2>逐份评分、原文及审计</h2>"+"".join(detail)+"</main></html>"
    return doc

