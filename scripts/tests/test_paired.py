"""Durable paired dispatch must not duplicate costly App or AGY work."""
from pathlib import Path
from unittest.mock import patch
import sys,tempfile,unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import core,paired
from paired_report import aggregate,performance_summary,checkpoint_transitions,external_source_files,historical_performance,peer_run_counts,render


class PairedBatchTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.units=["r1-t1-m1-b","r1-t1-m1-a","r1-t1-m2-a","r1-t1-m2-b"]
        for unit in self.units:
            d=self.root/"units"/unit;d.mkdir(parents=True);(d/"prompt.txt").write_text("frozen",encoding="utf-8")
            paired.save_state(self.root,unit,{"unit":unit,"stage":"answer","pair_id":unit[:-2],"arm":unit[-1],
                "model":"gpt-6.2-sol","effort":"max","status":"pending","thread_id":None,
                "prompt_sha256":core.sha(d/"prompt.txt")})
        core.write(self.root/"protocol.json",{"units":self.units})
        core.write(self.root/"preparation-seal.json",{"protocol_sha256":core.sha(self.root/"protocol.json")})
        sys.path.insert(0,str(core.home()/"skills/peer-analysis/scripts"))
        import peer
        self.peer=peer

    def tearDown(self):self.temp.cleanup()

    def dispatch(self,eligible=True):
        with patch.object(self.peer,"home",return_value=self.root/"peer-state"),patch.object(self.peer,"quota",return_value={"eligible":eligible}),patch.object(self.peer,"binary_at",return_value=Path("no-call")):
            return paired.dispatch(self.root)

    def test_one_per_arm_and_durable_intent_prevent_duplicate_dispatch(self):
        result=self.dispatch()
        self.assertEqual({r["unit"] for r in result["ready"]},set(self.units[:2]))
        with self.assertRaisesRegex(ValueError,"ambiguous"):self.dispatch()
        for r in result["ready"]:paired.register(self.root,r["unit"],"thread-"+r["unit"])
        self.assertEqual(self.dispatch()["ready"],[])
        done=paired.state(self.root,self.units[1]);done["status"]="completed";paired.save_state(self.root,self.units[1],done)
        # B is still active on the front pair, so a later A must wait for its
        # matching pair instead of advancing the independent A queue.
        self.assertEqual(self.dispatch()["ready"],[])
        done=paired.state(self.root,self.units[0]);done["status"]="completed";paired.save_state(self.root,self.units[0],done)
        self.assertEqual([r["unit"] for r in self.dispatch()["ready"]],["r1-t1-m2-a","r1-t1-m2-b"])

    def test_reserve_holds_peer_but_allows_independent_a(self):
        result=self.dispatch(False)
        self.assertEqual([r["unit"] for r in result["ready"]],["r1-t1-m1-a"])
        self.assertEqual(paired.state(self.root,"r1-t1-m1-b")["status"],"pending")
        paired.register(self.root,"r1-t1-m1-a","thread-a1")
        done=paired.state(self.root,"r1-t1-m1-a");done["status"]="completed";paired.save_state(self.root,"r1-t1-m1-a",done)
        self.assertEqual([r["unit"] for r in self.dispatch(False)["ready"]],["r1-t1-m2-a"])

    def test_authorized_quota_override_changes_only_selected_b_invocation(self):
        core.write(self.root/"quota-overrides.json",{"overrides":[{"id":"user-zero-r1",
            "authorization":"User explicitly requested zero reserve for this batch.","reserve_fraction":"0",
            "units":[self.units[0]]}]})
        result=self.dispatch();b=result["ready"][0];a=result["ready"][1]
        self.assertIn("--reserve-fraction 0",b["prompt"])
        self.assertNotIn("--reserve-fraction",a["prompt"])
        self.assertEqual(paired.state(self.root,b["unit"])["peer_reserve_fraction"],"0")
        self.assertEqual((self.root/"units"/b["unit"]/"prompt.txt").read_text(encoding="utf-8"),"frozen")
        self.assertIsNone(paired.quota_override(self.root,self.units[3]))

    def test_pair_arm_order_alternates_when_both_slots_are_free(self):
        result=self.dispatch(True)
        self.assertEqual([r["unit"] for r in result["ready"]],self.units[:2])

    def test_explicit_parallel_limits_fill_independent_slots_without_relaunch(self):
        core.write(self.root/"concurrency-override.json",{"id":"faster-r1","authorization":"User requested parallel tasks.",
            "max_active_per_arm":{"a":2,"b":5}})
        ready=self.dispatch()["ready"]
        self.assertEqual({r["unit"] for r in ready},set(self.units))
        for job in ready:
            paired.register(self.root,job["unit"],"thread-"+job["unit"])
            if job["unit"].endswith("-b"):self.assertIn("peer-busy",job["prompt"])
        self.assertEqual(self.dispatch()["ready"],[])

    def test_authorized_grader_parallelism_caps_independent_fresh_packets(self):
        core.write(self.root/"concurrency-override.json",{"id":"faster-r1","authorization":"User requested parallel tasks.",
            "max_active_per_arm":{"a":2,"b":3},"max_active_graders":8})
        for i in range(1,10):
            unit=f"grade-t{(i-1)//5+1}-m{(i-1)%5+1}";d=self.root/"grades"/unit;d.mkdir(parents=True)
            (d/"prompt.txt").write_text("anonymous",encoding="utf-8")
            core.write(d/"state.json",{"unit":unit,"status":"pending","model":"gpt-6.1-sol",
                "effort":"max","prompt_sha256":core.sha(d/"prompt.txt")})
        jobs=paired.grade_dispatch(self.root)["ready"];self.assertEqual(len(jobs),8)
        for job in jobs:paired.register(self.root,job["unit"],"judge-"+job["unit"],grading=True)
        self.assertEqual(paired.grade_dispatch(self.root)["ready"],[])
        d=self.root/"grades"/jobs[0]["unit"];s=core.read(d/"state.json");s["status"]="completed";core.write(d/"state.json",s)
        self.assertEqual(len(paired.grade_dispatch(self.root)["ready"]),1)

    def test_parallel_preparation_may_queue_only_behind_known_own_running_peer(self):
        core.write(self.root/"concurrency-override.json",{"id":"faster-r1","authorization":"User requested parallel tasks.",
            "max_active_per_arm":{"a":2,"b":3}})
        peer_root=self.root/"peer-state";rid="20261123T060000Z-1234abcd"
        core.write(peer_root/"active.json",{"run_id":rid})
        core.write(peer_root/"runs"/rid/"run.json",{"task":"paired-ab/"+self.root.name+"/case"})
        with patch.object(self.peer,"active_status",return_value={"status":"unknown-inflight","run_id":rid}):
            jobs=self.dispatch()["ready"]
        self.assertEqual({j["unit"][-1] for j in jobs},{"a"})
        for job in jobs:paired.register(self.root,job["unit"],"thread-"+job["unit"])
        with patch.object(self.peer,"active_status",return_value={"status":"running","run_id":rid}):
            jobs=self.dispatch()["ready"]
        self.assertEqual({j["unit"][-1] for j in jobs},{"b"})

    def test_user_isolated_peer_tasks_do_not_wait_for_another_task_global_lock(self):
        core.write(self.root/"concurrency-override.json",{"id":"unlocked-r1","authorization":"User explicitly removed cross-task locking.",
            "max_active_per_arm":{"a":2,"b":3},"isolated_peer_units":[self.units[0]]})
        peer_root=self.root/"peer-state";rid="20261123T060000Z-1234abcd"
        core.write(peer_root/"active.json",{"run_id":rid})
        with patch.object(self.peer,"active_status",return_value={"status":"unknown-inflight","run_id":rid}):
            jobs=self.dispatch()["ready"]
        self.assertEqual({j["unit"] for j in jobs},{self.units[0],self.units[1],self.units[2]})
        own=paired.state(self.root,self.units[0])
        self.assertEqual(Path(own["peer_state_override"]),self.root/"units"/self.units[0]/"peer-state")
        self.assertTrue((peer_root/"active.json").is_file())
        self.assertIn("--state-dir",next(j["prompt"] for j in jobs if j["unit"]==self.units[0]))

    def test_five_round_model_rotation_keeps_strict_alternation_by_pair_index(self):
        pairs=[];units=[]
        for cycle in range(1,6):
            order=list(range(1,6));order=order[cycle-1:]+order[:cycle-1]
            for topic in range(1,5):
                for model in order:
                    pair=f"r{cycle}-t{topic}-m{model}";pairs.append({"pair_id":pair})
                    old_first="a" if (cycle+topic-1+model-1)%2==0 else "b"
                    for arm in (old_first,"b" if old_first=="a" else "a"):
                        unit=pair+"-"+arm;units.append(unit);d=self.root/"units"/unit;d.mkdir(parents=True,exist_ok=True)
                        (d/"prompt.txt").write_text("frozen",encoding="utf-8")
                        paired.save_state(self.root,unit,{"unit":unit,"pair_id":pair,"arm":arm,"status":"pending",
                            "model":"gpt-6.2-sol","effort":"max","thread_id":None,"prompt_sha256":core.sha(d/"prompt.txt")})
        core.write(self.root/"protocol.json",{"units":units,"pairs":pairs})
        core.write(self.root/"preparation-seal.json",{"protocol_sha256":core.sha(self.root/"protocol.json")})
        starts=[]
        for index,pair in enumerate(pairs):
            jobs=self.dispatch()["ready"]
            self.assertEqual(len(jobs),2)
            first="b" if index%2==0 else "a";starts.append(first)
            self.assertEqual(jobs[0]["unit"],pair["pair_id"]+"-"+first)
            for job in jobs:
                paired.register(self.root,job["unit"],"thread-"+job["unit"])
                s=paired.state(self.root,job["unit"]);s["status"]="completed";paired.save_state(self.root,job["unit"],s)
        self.assertEqual((starts.count("a"),starts.count("b")),(50,50))

    def test_completed_front_pair_allows_next_pair_in_recorded_order(self):
        for arm in self.units[:2]:
            s=paired.state(self.root,arm);s["status"]="completed";paired.save_state(self.root,arm,s)
        result=self.dispatch(True)
        self.assertEqual([r["unit"] for r in result["ready"]],self.units[2:])

    def test_registration_cannot_replace_running_thread(self):
        self.dispatch()
        paired.register(self.root,self.units[0],"first")
        with self.assertRaisesRegex(ValueError,"launch intent"):paired.register(self.root,self.units[0],"second")
        self.assertEqual(paired.state(self.root,self.units[0])["thread_id"],"first")

    def test_model_or_input_state_cannot_be_silently_changed(self):
        (self.root/"protocol.json").write_text("{}",encoding="utf-8")
        with self.assertRaisesRegex(ValueError,"protocol changed"):paired.status(self.root)

    def test_grading_requires_all_answers_and_rejects_unit_path_escape(self):
        with self.assertRaisesRegex(ValueError,"Finish all"):paired.blind(self.root)
        with self.assertRaisesRegex(ValueError,"Invalid paired unit"):paired.state(self.root,"../outside")

    def test_unknown_inflight_blocks_new_paid_dispatch(self):
        s=paired.state(self.root,self.units[0]);s["status"]="unknown-inflight";paired.save_state(self.root,self.units[0],s)
        with self.assertRaisesRegex(ValueError,"in-flight"):self.dispatch()

    def test_peer_resume_keeps_same_chat_and_preserves_old_capture(self):
        unit=self.units[0];s=paired.state(self.root,unit)
        s.update(status="waiting-peer",thread_id="original-chat",last_turn_id="old-turn",capture="old-capture")
        paired.save_state(self.root,unit,s)
        result=self.dispatch();job=next(j for j in result["ready"] if j["unit"]==unit)
        self.assertEqual(job["resume_thread_id"],"original-chat")
        self.assertEqual(paired.state(self.root,unit)["collection_attempt"],2)
        with self.assertRaisesRegex(ValueError,"retain"):paired.register(self.root,unit,"replacement")
        paired.register(self.root,unit,"original-chat")
        self.assertEqual(paired.state(self.root,unit)["capture"],"old-capture")

    def test_missing_audit_resumes_same_chat_without_replacing_technical_final(self):
        unit=self.units[1];d=self.root/"units"/unit
        s=paired.state(self.root,unit);s.update(status="running",thread_id="thread-1");paired.save_state(self.root,unit,s)
        def receipt(turn,text):
            message={"thread":{"id":"thread-1","hostId":"local"},"turns":[{"id":turn,"status":"completed","error":None,
                "items":[{"type":"agentMessage","id":"msg-"+turn,"phase":"final_answer","text":text,"truncated":False}]}]}
            completion={"polls":[{"thread":message["thread"],"latestTurn":{"id":turn,"status":"completed","error":None,
                "startedAt":1,"completedAt":2,"durationMs":1000},"latestAssistantMessageId":"msg-"+turn,
                "latestAssistantMessage":{"id":"msg-"+turn,"turnId":turn,"phase":"final_answer"}}]}
            mp=d/(turn+"-message.json");cp=d/(turn+"-completion.json");core.write(mp,message);core.write(cp,completion)
            return mp,cp
        mp,cp=receipt("technical","First exact technical body")
        first=paired.collect(self.root,unit,mp,cp)
        self.assertEqual(first["status"],"awaiting-audit")
        self.assertIn("不修改",first["repair_prompt"])
        self.assertEqual(paired.status(self.root)["active"][0]["status"],"awaiting-audit")
        self.assertIsNone(paired.collect(self.root,unit,mp,cp)["repair_prompt"])
        self.assertEqual(self.dispatch()["ready"][0]["unit"],self.units[0])
        ap,ac=receipt("audit","Public audit completed")
        with self.assertRaisesRegex(ValueError,"preserve"):paired.collect(self.root,unit,ap,ac)
        paired.capture_app_answer(ap,ac,d/"captured-audit-001","thread-1","audit")
        core.write(d/"audit.json",{"peer_invoked":False,"peer_run_ids":[]})
        with patch.object(paired,"execution_receipt",return_value={"visible_peer_call_mentions":0}):
            self.assertEqual(paired.collect(self.root,unit,mp,cp)["status"],"completed")
        done=paired.state(self.root,unit)
        self.assertEqual((Path(done["capture"])/"answer.txt").read_text(encoding="utf-8"),"First exact technical body")
        self.assertEqual(done["elapsed_ms_total"],2000)
        self.assertEqual(core.read(self.root/"audit-corrections.json")["corrections"][0]["audit_turn_duration_ms"],1000)

    def test_incomplete_peer_metadata_requests_audit_repair_without_new_assistance(self):
        unit=self.units[0];d=self.root/"units"/unit
        s=paired.state(self.root,unit);s.update(status="running",thread_id="thread-b");paired.save_state(self.root,unit,s)
        core.write(d/"message.json",{"thread":{"id":"thread-b"},"turns":[{"id":"technical"}]})
        core.write(d/"completion.json",{})
        core.write(d/"audit.json",{"peer_invoked":True})
        with patch.object(paired,"capture_app_answer",return_value={"answer_sha256":"unchanged"}):
            result=paired.collect(self.root,unit,d/"message.json",d/"completion.json")
        self.assertEqual(result["status"],"awaiting-audit")
        self.assertIn("不发起新的AGY",result["repair_prompt"])
        self.assertEqual(paired.state(self.root,unit)["audit_repair_reason"],"incomplete-peer-audit-references")

    def test_archive_retains_adopted_raw_capture_and_historical_performance_bytes(self):
        unit=self.units[1];pilot=self.root/"pilot";capture=pilot/"captured";capture.mkdir(parents=True)
        raw=b"Exact original answer\r\n"
        (capture/"answer.txt").write_bytes(raw)
        core.write(capture/"answer-receipt.json",{"fixture":"source receipt"})
        core.write(pilot/"audit.json",{"fixture":"audit"});core.write(pilot/"execution.json",{"fixture":"execution"})
        result=self.root/"old-result.json";result.write_bytes(b'{"unknown_cost":null}\r\n')
        summary=self.root/"old-summary.json";core.write(summary,{"fixture":"old score"})
        s=paired.state(self.root,unit);s.update(status="adopted",reused_from=str(pilot),capture=str(capture),
            audit=str(pilot/"audit.json"),execution_receipt=str(pilot/"execution.json"));paired.save_state(self.root,unit,s)
        files=external_source_files(self.root,{"units":self.units,"pairs":[{"pair_id":unit[:-2],"result":str(result),
            "published_source":{"summary":str(summary),"summary_sha256":core.sha(summary)}}]})
        self.assertEqual(files["adopted-pilot/"+unit+"/captured/answer.txt"].read_bytes(),raw)
        self.assertEqual(files["historical-results/"+unit[:-2]+"/result.json"].read_bytes(),result.read_bytes())
        self.assertIn("historical-summaries/"+core.sha(summary)+".json",files)

    def test_historical_performance_keeps_metrics_without_embedding_large_traces(self):
        path=self.root/"old-result.json"
        record={"elapsed_s":12.34,"input_tokens":100,"usage":None,"actual_gateway_charge":None,
            "permission_receipt":{"verified":True},"agentic":{"tree_terminal":True,"events":["trace"*10000],
            "messages":["old answer"],"tools":[{"type":"read"}]}}
        core.write(path,record);original=path.read_bytes()
        performance=historical_performance({"result":str(path),"pair_id":"r1-t1-m1"})
        self.assertEqual(performance["elapsed_s"],12.34)
        self.assertIsNone(performance["usage"]);self.assertIsNone(performance["actual_gateway_charge"])
        self.assertEqual(performance["permission_receipt"],record["permission_receipt"])
        self.assertNotIn("agentic",performance)
        self.assertEqual(performance["agentic_summary"]["trace_item_counts"]["events"],1)
        self.assertEqual(performance["source_result"]["sha256"],core.sha(path))
        self.assertEqual(path.read_bytes(),original)

    def test_public_grader_paths_and_titles_do_not_encode_source_model_order(self):
        unit="grade-t1-m4";d=self.root/"grades"/unit;d.mkdir(parents=True)
        token="a93b7fd0152b8c5a7664d1ef";relative="blind-packets/"+token
        (d/"prompt.txt").write_text("Read only "+str(self.root/relative),encoding="utf-8")
        s={"unit":unit,"status":"pending","model":"gpt-6.1-sol","effort":"max",
            "original_model":"gpt-6-astra","anonymous_packet_id":token,"packet":relative,
            "prompt_sha256":core.sha(d/"prompt.txt")}
        core.write(d/"state.json",s)
        job=paired.grade_dispatch(self.root)["ready"][0]
        self.assertNotIn("m4",job["directory_name"])
        self.assertNotIn("gpt-6-astra",job["prompt"]+job["title"]+job["directory_name"])
        self.assertEqual(paired.grade_packet(self.root,s),self.root/relative)
        self.assertEqual(paired.grade_packet(self.root,{"unit":unit}),d/"packet")


class PairedAggregateTests(unittest.TestCase):
    def samples(self):
        rows=[]
        for topic in paired.TOPICS:
            for model in paired.MODELS:
                for cycle in range(1,6):
                    for version,quality in (("original",90),("a",91),("b",91+cycle-3)):
                        rows.append({"pair_id":topic+model+str(cycle),"topic":topic,"model":model,"cycle":cycle,
                            "version":version,"quality":quality,"accuracy":70,"coverage":20,"insight":quality-90,
                            "errors":0,"omissions":0})
        return rows

    def test_three_hundred_texts_remain_one_hundred_matched_pairs(self):
        result=aggregate(self.samples())
        self.assertEqual(len(result["pairs"]),100)
        self.assertEqual(len(result["cells"]),60)
        self.assertTrue(all(c["n"]==5 for c in result["cells"]))
        for row in result["model_equal_topic_means"]:
            self.assertEqual((row["wins"],row["ties"],row["losses"]),(8,4,8))
            self.assertEqual(row["b_minus_a"],0)
            self.assertEqual(row["a_minus_original"],1)

    def test_missing_or_duplicated_arm_cannot_create_an_uplift(self):
        rows=self.samples()
        with self.assertRaisesRegex(ValueError,"missing"):aggregate(rows[:-1])
        with self.assertRaisesRegex(ValueError,"Duplicate"):aggregate(rows+[rows[0]])

    def test_decimal_pair_differences_are_not_rounded_early(self):
        rows=self.samples()
        for row in rows:row["quality"]={"original":90,"a":90.1,"b":90.2}[row["version"]]
        result=aggregate(rows)
        self.assertTrue(all(row["b_minus_a"]==0.1 for row in result["pairs"]))
        self.assertTrue(all(row["b_minus_a"]==0.1 for row in result["model_equal_topic_means"]))

    def test_task_times_are_paired_and_unknown_duration_is_not_zero(self):
        rows=self.samples()
        for row in rows:
            if row["version"]!="original":row["incremental_minutes"]={"a":1.25,"b":4.75}[row["version"]]
        result=performance_summary(rows)
        self.assertIn("not summed",result["scope"])
        for row in result["models"]:
            self.assertEqual((row["a_observed"],row["b_observed"],row["paired_observed"]),(20,20,20))
            self.assertEqual(row["b_minus_a_mean_minutes"],3.5)
        next(r for r in rows if r["version"]=="b")["incremental_minutes"]=None
        row=performance_summary(rows)["models"][0]
        self.assertEqual((row["a_observed"],row["b_observed"],row["paired_observed"]),(20,19,19))
        self.assertEqual(row["b_mean_minutes"],4.75)

    def test_checkpoint_changes_distinguish_new_errors_from_net_issue_counts(self):
        rows=[]
        for version,statuses in (("original",("error","correct")),("a",("correct","error")),("b",("correct","omission"))):
            rows.append({"pair_id":"one","version":version,"topic":"preload","model":paired.MODELS[0],
                "quality_details":{"checks":{str(i):{"status":status,"evidence":["quoted source"]}
                    for i,status in enumerate(statuses)}}})
        changes=checkpoint_transitions(rows)["rows"]
        a=next(r for r in changes if r["comparison"]=="original_to_a")
        self.assertEqual((a["corrected_errors"],a["newly_marked_errors"]),(1,1))
        self.assertEqual(len(a["changed"]),2)
        b=next(r for r in changes if r["comparison"]=="a_to_b")
        self.assertEqual((b["corrected_errors"],b["newly_marked_omissions"]),(0,1))
        rows[-1]["quality_details"]["checks"].pop("0")
        with self.assertRaisesRegex(ValueError,"IDs differ"):checkpoint_transitions(rows)

    def test_static_report_keeps_three_hundred_details_and_escapes_answer_text(self):
        with tempfile.TemporaryDirectory() as temp:
            answer=Path(temp)/"answer.txt";answer.write_text("<script>untrusted answer</script>",encoding="utf-8")
            rows=self.samples()
            for row in rows:
                row.update(anonymous_id=row["pair_id"]+row["version"],answer_path=str(answer),raw_quality=row["quality"],
                    principle_penalty=0,quality_cap=100,incremental_minutes=2 if row["version"]=="a" else 3,
                    quality_details={"points":{"one":1},"notes":{"one":"Evidence note"},"evidence":{"one":["Exact quote"]},
                        "checks":{"point":{"status":"correct","evidence":["Exact quote"]}},"issues":[]})
            results={"summary":aggregate(rows),"samples":rows,"rubrics":{t:{"rubric":[{"id":"one","max":1,"criterion":"Fact"}]} for t in paired.TOPICS},
                "overall":{"b_minus_a":0,"wins":40,"ties":20,"losses":40,"unique_assisting_peer_runs":1,"verified_zero_turn_failures":0},
                "limitations":["Sealed condition"],"performance":performance_summary(rows),"checkpoint_transitions":checkpoint_transitions(rows)}
            document=render(results)
            self.assertEqual(document.count("data-sample='"),300)
            matrix=document.split("<table id='matrix'>",1)[1].split("</table>",1)[0]
            self.assertEqual(matrix.count("<tr>"),6)
            self.assertIn('name="mode" checked',document)
            self.assertIn("#compare:checked~main .compare",document)
            self.assertIn("&lt;script&gt;untrusted answer&lt;/script&gt;",document)
            self.assertNotIn("<script>",document)
            self.assertIn("checkpoint-transitions.csv",document)


class PairedPeerReceiptTests(unittest.TestCase):
    def test_citation_signal_requires_terminal_run_and_complete_dispositions(self):
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp);run_id="20261122T080145Z-398ccf78dfa6";directory=base/"runs"/run_id
            record={"terminal_verified":True,"transport_complete":True,"status":"partial",
                "config":{"model":"gemini-4-neon-xhigh","effort":"xhigh","reserve_fraction":"0.10"},
                "validation_issues":["citation_outside_evidence","citation_quote_mismatch"]}
            core.write(directory/"run.json",record)
            core.write(directory/"answer.json",{"findings":[{"id":"F1"}]})
            sample={"peer_state_override":str(base)};audit={"peer_run_ids":[run_id]}
            with self.assertRaisesRegex(ValueError,"dispositions incomplete"):paired.validate_peer(base,sample,audit)
            core.write(directory/"assessments"/"one.json",{"decisions":[{"finding_id":"F1",
                "status":"verified-adopted","reason":"Verified frozen source","verification":"source.cpp:12"}]})
            receipt=paired.validate_peer(base,sample,audit)[0]
            self.assertEqual(receipt["validation_issues"],record["validation_issues"])
            record["validation_issues"].append("transport_completion_unverified");core.write(directory/"run.json",record)
            with self.assertRaisesRegex(ValueError,"transport validation failed"):paired.validate_peer(base,sample,audit)
            record["validation_issues"]=[];record["terminal_verified"]=False;core.write(directory/"run.json",record)
            with self.assertRaisesRegex(ValueError,"terminal unverified"):paired.validate_peer(base,sample,audit)

    def test_peer_run_references_accept_sealed_metadata_objects(self):
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp);run_id="20261122T080145Z-398ccf78dfa6";directory=base/"runs"/run_id
            core.write(directory/"run.json",{"terminal_verified":True,"transport_complete":True,"status":"completed",
                "config":{"model":"gemini-4-neon-xhigh","effort":"xhigh","reserve_fraction":"0.10"},"validation_issues":[]})
            core.write(directory/"answer.json",{"findings":[]})
            sample={"peer_state_override":str(base)}
            audit={"peer_run_ids":[{"run_id":run_id,"status":"completed","quota_after":{"eligible":True}}]}
            result=paired.validate_peer(base,sample,audit)
            self.assertEqual(result[0]["run_id"],run_id)
            audit={"peer_runs":[{"run_id":run_id,"status":"completed"}]}
            self.assertEqual(paired.validate_peer(base,sample,audit)[0]["run_id"],run_id)
            audit["peer_run_ids"]=[run_id]
            self.assertEqual(len(paired.validate_peer(base,sample,audit)),1)
            audit["peer_runs"].insert(0,{"run_id":None,"status":"skipped","generation_started":False,
                "reason":"Material preflight rejected before a model call"})
            self.assertEqual(len(paired.validate_peer(base,sample,audit)),1)
            audit["peer_run_ids"]=[{}]
            with self.assertRaisesRegex(ValueError,"identifier"):paired.validate_peer(base,sample,audit)

    def test_peer_attempt_counts_do_not_call_new_failures_prior_reuse(self):
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp);records=[]
            for rid,status,accepted,date,turns in (("old","completed",True,"2026-11-21T00:00:00+00:00",1),
                    ("new","completed",True,"2026-11-23T00:00:00+00:00",1),
                    ("zero","failed",False,"2026-11-23T00:00:00+00:00",0),
                    ("stopped","resolved-stopped",False,"2026-11-23T00:00:00+00:00",None)):
                core.write(base/"runs"/rid/"run.json",{"started_at":date})
                records.append({"run_id":rid,"state_dir":str(base),"status":status,
                    "assistance_accepted":accepted,"reported_turns":turns})
            counts=peer_run_counts(records+[records[1]],"2026-11-22T00:00:00+00:00")
            self.assertEqual(counts["new_peer_runs_in_batch"],3)
            self.assertEqual(counts["prior_peer_runs_reused"],1)
            self.assertEqual(counts["new_generation_runs_in_batch"],1)
            self.assertEqual(counts["verified_zero_turn_failures"],1)
            self.assertEqual(counts["verified_stopped_failures"],1)
            self.assertEqual(counts["duplicate_run_references"],1)

    def test_confirmed_stop_is_retained_without_claiming_usage_or_assistance(self):
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp);rid="20261123T005848Z-dd3ceeae7a29";directory=base/"runs"/rid
            core.write(directory/"run.json",{"status":"resolved-stopped","terminal_verified":False,
                "config":{"model":"gemini-4-neon-xhigh","effort":"xhigh","reserve_fraction":"0.10"}})
            s={"peer_state_override":str(base)};audit={"peer_run_ids":[rid]}
            with self.assertRaisesRegex(ValueError,"confirmation missing"):
                paired.validate_peer(base,s,audit,allow_unassisted=True)
            confirmation={"run_id":"wrong","conversation_id":None,"confirmed_stopped":True,"basis":"Service rejected startup."}
            core.write(directory/"stop-confirmation.json",confirmation)
            with self.assertRaisesRegex(ValueError,"not bound"):
                paired.validate_peer(base,s,audit,allow_unassisted=True)
            confirmation["run_id"]=rid;core.write(directory/"stop-confirmation.json",confirmation)
            result=paired.validate_peer(base,s,audit,allow_unassisted=True)[0]
            self.assertIsNone(result["usage"]);self.assertIsNone(result["reported_turns"])
            self.assertFalse(result["assistance_accepted"]);self.assertFalse(result["terminal_verified"])
            with self.assertRaisesRegex(ValueError,"No verified AGY analysis"):
                paired.validate_peer(base,s,audit)

    def test_zero_turn_failures_are_preserved_but_do_not_supply_assistance(self):
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp);rid="20261122T183000Z-36e124734f92";directory=base/"runs"/rid
            record={"terminal_verified":True,"transport_complete":True,"status":"failed",
                "config":{"model":"gemini-4-neon-xhigh","effort":"xhigh","reserve_fraction":"0.10"},
                "terminal":{"num_turns":0,"response":""},"native_usage":{k:0 for k in
                    ("input_tokens","output_tokens","thinking_tokens","cache_read_tokens","total_tokens")}}
            core.write(directory/"run.json",record);s={"peer_state_override":str(base)};a={"peer_run_ids":[rid]}
            with self.assertRaisesRegex(ValueError,"No verified AGY analysis"):paired.validate_peer(base,s,a)
            failed=paired.validate_peer(base,s,a,allow_unassisted=True)[0]
            self.assertFalse(failed["assistance_accepted"])
            self.assertEqual(failed["reported_turns"],0)
            s={"peer_state_override":str(base/"isolated"),"peer_state_fallback":str(base)}
            self.assertEqual(paired.validate_peer(base,s,a,allow_unassisted=True)[0]["state_dir"],str(base))
            record["native_usage"]=None;core.write(directory/"run.json",record)
            with self.assertRaisesRegex(ValueError,"may include generation"):paired.validate_peer(base,s,a,allow_unassisted=True)
            record["native_usage"]={"input_tokens":1};core.write(directory/"run.json",record)
            with self.assertRaisesRegex(ValueError,"may include generation"):paired.validate_peer(base,s,a,allow_unassisted=True)

    def test_empty_verified_terminal_retains_nonzero_usage_without_becoming_analysis(self):
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp);rid="20261123T073819Z-f129f2d1645c";directory=base/"runs"/rid
            record={"status":"partial","terminal_verified":True,"transport_complete":True,
                "config":{"model":"gemini-4-neon-xhigh","effort":"xhigh","reserve_fraction":"0.10"},
                "terminal":{"status":"SUCCESS","num_turns":1,"response":""},
                "native_usage":{"input_tokens":10,"output_tokens":2,"total_tokens":12},
                "validation_issues":["missing_structured_answer","out_of_scope_file_target_observed"],
                "started_at":"2026-11-23T00:00:00+00:00"}
            core.write(directory/"run.json",record);s={"peer_state_override":str(base)};a={"peer_run_ids":[rid]}
            item=paired.validate_peer(base,s,a,allow_unassisted=True)[0]
            self.assertFalse(item["assistance_accepted"]);self.assertEqual(item["usage"]["total_tokens"],12)
            counts=peer_run_counts([item],"2026-11-22T00:00:00+00:00")
            self.assertEqual(counts["unusable_terminal_attempts"],1)
            self.assertEqual(counts["new_generation_runs_in_batch"],1)
            self.assertEqual(counts["new_accepted_analysis_runs_in_batch"],0)
            with self.assertRaisesRegex(ValueError,"No verified AGY analysis"):paired.validate_peer(base,s,a)
            record["terminal"]["response"]="Unstructured text exists";core.write(directory/"run.json",record)
            with self.assertRaisesRegex(ValueError,"validation failed"):paired.validate_peer(base,s,a,allow_unassisted=True)


if __name__=="__main__":unittest.main()

