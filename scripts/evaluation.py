"""Create a self-contained, immutable offline evaluation of existing samples."""
from pathlib import Path
import copy
import re
import shutil

from core import check_inputs, check_sensitive, condition_filter_keys, digest, inside, now, read, require, require_current_gold, score_scale, scoring_gold, sha, three_dimension_scoring, validate_gold, write


def formal_v3_gold(gold):
    """Normalize a sealed three-dimensional draft without changing any score scale."""
    validate_gold(gold)
    require(three_dimension_scoring(gold), "Formal V3 import needs already scored 70/20/10 judgments")
    if gold.get("scoring_version") == 3:
        return copy.deepcopy(gold)
    result = copy.deepcopy(gold)
    result.pop("reweight", None)
    for item in result["rubric"]:
        item["criterion"] = item["criterion"].replace(
            " V4: retain the original criterion and anchors; multiply original-scale points and maximum by 1.4.",
            " 正式V3：准确性按70分量表计分，原量表判据锚点乘1.4。")
    result.update(scoring_version=3, revision_label="formal-v3-reliability",
                  revision_note="Formal V3: accuracy70/coverage20/insight10, unscored presentation. Existing scores and judgments unchanged; direct comparison with original sampling scores. Post-hoc revision, no new review or inference.",
                  normalization={"method":"formal-v3", "source_rubric_sha256":digest(gold), "scores_unchanged":True})
    result["grading_rules"] = [
        "Formal V3 weights are accuracy70/coverage20/insight10. Presentation notes never add or deduct points.",
        "Reuse the sealed factual judgments, required checkpoints, evidence and independent issue groups; this is not a fresh subjective review.",
        "Errors belong to accuracy and required omissions to coverage. Apply the frozen additional principle deduction and lowest cap once per causal group.",
        "Optional insight absence is not an omission. Diagnostic error/omission ratios use the frozen checkpoint denominator, not answer length.",
        "Source/fixture inference, actual SDK execution and production evidence remain distinct. Claims must not exceed the evidence.",
        "The previous comparison version is the original sampling grade, matched by protocol, slot, result and answer hashes. Experimental intermediates are internal provenance only.",
        *[rule for rule in gold.get("grading_rules", []) if rule.startswith("Calibration:")]]
    validate_gold(result)
    return result


def formal_v3_score(score, gold):
    result = copy.deepcopy(score)
    if gold.get("scoring_version") == 4:
        for item in gold["rubric"]:
            if item["dimension"] == "accuracy":
                note = re.sub(r"^V3 original-scale [^;]+; V4 x1\.4\. ", "", result["notes"][item["id"]])
                result["notes"][item["id"]] = "准确性已按70分量表计分；沿用的原量表判断说明：" + note
    return result


def validate_imported_scores(root, mapping, scores, gold):
    base = Path(root) / "evaluation/derivation-source"
    source_gold = read(base / "rubric.json")
    require(gold == formal_v3_gold(source_gold), "Formal V3 rubric differs from its sealed judgment source")
    source_scores, source_mapping = read(base / "grading/scores.json"), read(base / "grading/private-mapping.json")
    by_result = {relative:source_scores[anon] for anon,relative in source_mapping.items()}
    for anon, relative in mapping.items():
        require(relative in by_result and scores[anon] == formal_v3_score(by_result[relative], source_gold),
                "Formal V3 import must preserve the source points, issues and checkpoints")


def v4_gold_from_v3(gold):
    """Read-only historical migration validation; not a current creation format."""
    validate_gold(gold)
    require(gold.get("scoring_version") == 3 and not three_dimension_scoring(gold),
            "Historical weight migration requires four-dimensional trial judgments")
    result = copy.deepcopy(gold)
    result["rubric"] = [r for r in result["rubric"] if r["dimension"] != "readability"]
    for item in result["rubric"]:
        if item["dimension"] == "accuracy":
            item["max"] = score_scale(item["max"], "1.4")
            item["criterion"] += " V4: retain the original criterion and anchors; multiply original-scale points and maximum by 1.4."
    result.update(scoring_version=4, revision_label="principle-v4-weight-migration",
                  revision_note="Post-hoc deterministic V3 weight migration, not a new subjective review. Accuracy x1.4; coverage/insight unchanged; presentation unscored. No new sampling.",
                  reweight={"source_version": 3, "source_rubric_sha256": digest(gold), "accuracy_factor": "1.4"})
    old_rules = result.get("grading_rules", [])
    result["grading_rules"] = [
        "V4 weights: accuracy70/coverage20/insight10. Presentation notes do not contribute points or penalties.",
        "This revision mechanically reuses sealed V3 judgments/evidence/issues/checkpoints; no fresh blind or subjective review is claimed.",
        *old_rules[2:8], *old_rules[9:],
        "Historical V3 readability points/notes remain in the source archive; do not turn them into new factual or omission issues."]
    homes = result.get("scoring_scope", {}).get("unique_homes", {})
    for key in homes:
        if "presentation" in key:
            homes[key] = "presentation_notes, unscored"
    validate_gold(result)
    return result


def v4_score_from_v3(score, gold):
    kept = {r["id"]: r for r in gold["rubric"] if r["dimension"] != "readability"}
    result = {key: {cid: copy.deepcopy(value) for cid, value in score[key].items() if cid in kept}
              for key in ("points", "notes", "evidence")}
    for cid, item in kept.items():
        if item["dimension"] == "accuracy":
            result["points"][cid] = score_scale(score["points"][cid], "1.4")
            result["notes"][cid] = f'V3 original-scale {score["points"][cid]}/{item["max"]}; V4 x1.4. ' + score["notes"][cid]
    result.update(issues=copy.deepcopy(score["issues"]), checks=copy.deepcopy(score["checks"]), presentation_notes=[])
    return result


def validate_reweighted_scores(root, mapping, scores, gold):
    """Recheck sealed derivation from archived V3 evidence, without the live source."""
    base = Path(root) / "evaluation/reweight-source"
    source_gold = read(base / "rubric.json")
    require(gold == v4_gold_from_v3(source_gold), "V4 rubric differs from its declared weight migration")
    source_scores, source_mapping = read(base / "grading/scores.json"), read(base / "grading/private-mapping.json")
    by_result = {relative: source_scores[anon] for anon, relative in source_mapping.items()}
    for anon, relative in mapping.items():
        require(relative in by_result and scores[anon] == v4_score_from_v3(by_result[relative], source_gold),
                "V4 scores differ from the frozen V3 weight migration")


def regrade(source, rubric_path, output, *, repair_final_answers=False, condition_filter=None, scores_from=None,
            review_from=None, review_revision=None):
    # Import here: report generation also reads this module's manifest as data.
    from reporting import check_artifact_sensitive, idle, native_evidence_files, score_data, summarize

    source, output = Path(source).resolve(), Path(output).resolve()
    protocol = check_inputs(source)
    idle(source)
    require(not protocol.get("offline_regrade"), "Regrade the original sampling batch, not an evaluation revision")
    require(not output.exists() and not output.is_relative_to(source) and not source.is_relative_to(output), "Use a new sibling output directory")
    require((source / "grading/seal.json").is_file(), "Source scoring must be sealed")
    score_data(source)
    original = read(source / "summary.json")
    expected = summarize(source)
    require({k: v for k, v in original.items() if k != "generated_at"} == {k: v for k, v in expected.items() if k != "generated_at"}, "Source summary differs from source records")
    gold = read(rubric_path)
    validate_gold(gold)
    require_current_gold(gold)
    require(gold.get("scoring_version") in (2, 3, 4), "Regrade requires an explicit V2 or formal V3 rubric")
    frozen_gold = read(source / "input/gold.json")
    require(gold["reference_facts"] == frozen_gold["reference_facts"] and gold["verification"] == frozen_gold["verification"], "Regrade must preserve original reference facts and verification")
    check_sensitive(Path(rubric_path).read_text(encoding="utf-8-sig"))
    selected_conditions = condition_filter_keys(condition_filter)
    keys = {(s["model"], s["effort"], s.get("service_tier", "default")) for s in protocol["schedule"]}
    require(selected_conditions is None or selected_conditions <= keys, "Selected regrade condition is absent from source")
    selected_slots = {s["id"] for s in protocol["schedule"]
                      if selected_conditions is None or (s["model"], s["effort"], s.get("service_tier", "default")) in selected_conditions}
    require(not repair_final_answers or selected_conditions is None, "Final-text repair and selected regrade require separate revisions")
    require((review_from is None) == (review_revision is None), "Use --review-from and --review-revision together")
    require(review_from is None or (scores_from is None and not repair_final_answers), "Fresh review cannot import unchanged scores or repair answer text")
    neutral_review, neutral_files = None, {}
    if review_from is not None:
        require(isinstance(review_revision, str) and review_revision.strip()
                and gold.get("review_revision") == review_revision, "Review revision must match the frozen rubric")
        from review import prepare_review_source
        neutral_review, neutral_files = prepare_review_source(source, review_from, gold, selected_slots)
        require(neutral_review != source and not output.is_relative_to(neutral_review)
                and not neutral_review.is_relative_to(output), "Review source must be a distinct sealed run")

    review, review_gold, review_scores, review_mapping, review_files = None, None, None, None, {}
    adopt_scores = gold.get("scoring_version") == 3 and three_dimension_scoring(gold)
    if scores_from is not None:
        require((adopt_scores or gold.get("scoring_version") == 4) and not repair_final_answers, "--scores-from imports sealed judgments without text repair")
        review = Path(scores_from).resolve()
        require(review != source and not output.is_relative_to(review) and not review.is_relative_to(output), "Weight source must be a distinct sealed run")
        review_protocol = check_inputs(review)
        idle(review)
        sampling = read(review / "evaluation/source-protocol.json") if review_protocol.get("offline_regrade") else review_protocol
        require(sampling == protocol, "V3 scores belong to a different original sampling protocol")
        score_data(review)
        review_gold = scoring_gold(review)
        expected_gold = formal_v3_gold(review_gold) if adopt_scores else v4_gold_from_v3(review_gold)
        require(gold == expected_gold, "Rubric must be the exact declared sealed-score derivation")
        review_scores, review_mapping = read(review / "grading/scores.json"), read(review / "grading/private-mapping.json")
        review_summary = summarize(review)
        require({k:v for k,v in review_summary.items() if k != "generated_at"}
                == {k:v for k,v in read(review / "summary.json").items() if k != "generated_at"}, "V3 source summary differs from its sealed judgments")
        originals = {r["answer_link"].replace("answer.txt", "result.json") for r in original["rows"]
                     if r["selected"] and r["valid"] and r["slot_id"] in selected_slots}
        require(originals and originals <= set(review_mapping.values()), "V3 judgment source does not cover the selected valid answers")
        for relative in originals:
            for name in (relative, str(Path(relative).with_name("answer.txt")).replace("\\", "/")):
                require(sha(inside(source, name)) == sha(inside(review, name)), "V3 identity/slot/answer or execution record differs from sampling source")
        archive_prefix = "evaluation/derivation-source/" if adopt_scores else "evaluation/reweight-source/"
        for name in ("protocol.json", "preparation-seal.json", "summary.json", "grading/scores.json", "grading/seal.json", "grading/private-mapping.json"):
            review_files[name] = archive_prefix + name
        rubric_source = "evaluation/rubric.json" if review_protocol.get("offline_regrade") else "input/gold.json"
        review_files[rubric_source] = archive_prefix + "rubric.json"

    repair = None
    if repair_final_answers:
        from final_repair import prepare_final_answer_repair
        from reporting import validate_records
        validate_records(source)
        repair = prepare_final_answer_repair(source, protocol)

    copies = {f.relative_to(source).as_posix(): f.relative_to(source).as_posix()
              for f in (source / "input").rglob("*") if f.is_file()}
    for f in (source / "runs").glob("*/attempt-*/*"):
        if f.is_file() and f.relative_to(source).parts[1] in selected_slots and f.name in {"answer.txt", "result.json", "events.jsonl", "task-tree.json", "session-settings.json"}:
            copies[f.relative_to(source).as_posix()] = f.relative_to(source).as_posix()
    for f in native_evidence_files(source):
        relative = f.relative_to(source)
        # A native capability seal names its audited scratch artifacts too.
        # Omitting them makes the copied verification receipt unverifiable.
        if (relative.parts[0] == "capability" or "scratch" not in relative.parts) and (relative.parts[0] != "runs" or relative.parts[1] in selected_slots):
            copies[relative.as_posix()] = relative.as_posix()
    copies.update({"prices.json": "prices.json", "protocol.json": "evaluation/source-protocol.json",
                   "preparation-seal.json": "evaluation/source-preparation-seal.json",
                   "summary.json": "evaluation/previous-summary.json"})
    for name in ["runtime.json", "instruction-context.json", "preflight.json"]:
        if (source / name).is_file():
            copies[name] = name
    for name in ["scores.json", "seal.json", "private-mapping.json"]:
        copies["grading/" + name] = "evaluation/previous-grading/" + name
    # Read and inspect every allowlisted file before creating the destination.
    before = {}
    for name in copies:
        f = inside(source, name)
        check_artifact_sensitive(f, f.is_relative_to(source / "input/source-snapshot") or "scratch" in f.relative_to(source).parts)
        before[name] = sha(f)
    review_before = {}
    for name in review_files:
        check_artifact_sensitive(inside(review, name))
        review_before[name] = sha(inside(review, name))
    neutral_before = {}
    for name in neutral_files:
        check_artifact_sensitive(inside(neutral_review, name))
        neutral_before[name] = sha(inside(neutral_review, name))
    if repair is not None:
        require(all(before.get(name) == expected
                    for attempt in repair["provenance"]["attempts"]
                    for name, expected in attempt["source_files_sha256"].items()),
                "Source changed after final-answer repair analysis")
    output.mkdir(parents=True)
    for name, target in copies.items():
        destination = inside(output, target)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(inside(source, name), destination)
        require(sha(destination) == before[name], "Source changed during snapshot")
    for name, target in review_files.items():
        destination = inside(output, target)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(inside(review, name), destination)
        require(sha(destination) == review_before[name], "V3 grading source changed during snapshot")
    for name, target in neutral_files.items():
        destination = inside(output, target)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(inside(neutral_review, name), destination)
        require(sha(destination) == neutral_before[name], "Review source changed during snapshot")
    extra_files = []
    if repair is not None:
        from final_repair import PROVENANCE_PATH
        for name, target in repair["source_copies"].items():
            destination = inside(output, target)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(inside(source, name), destination)
            require(sha(destination) == before[name], "Source changed during repair evidence snapshot")
            extra_files.append(target)
        for name, data in {**repair["code_files"], **repair["replacements"]}.items():
            destination = inside(output, name)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
            extra_files.append(name)
        write(output / PROVENANCE_PATH, repair["provenance"])
        extra_files.append(PROVENANCE_PATH)
    write(output / "evaluation/rubric.json", gold)
    files = {target: sha(inside(output, target)) for target in [*copies.values(), *extra_files]}
    files.update({target: review_before[name] for name,target in review_files.items()})
    files.update({target: neutral_before[name] for name,target in neutral_files.items()})
    files["evaluation/rubric.json"] = sha(output / "evaluation/rubric.json")
    manifest = {"schema_version": 1, "mode": "offline-regrade", "created_at": now(),
                "source_batch": source.name, "source_root": str(source), "source_files_sha256": before,
                "files_sha256": files, "scoring_version": gold["scoring_version"], "post_hoc": True,
                "evaluator_previously_saw_model_identity": True,
                "note": "事后新增细则；评分者此前见过模型身份。重新匿名并隐藏性能，不是完全独立盲评。No new inference."}
    if selected_conditions is not None:
        manifest.update(selected_slots=[s["id"] for s in protocol["schedule"] if s["id"] in selected_slots],
                        condition_filter=read(condition_filter))
    if review is not None:
        if adopt_scores:
            manifest.update(score_import={"source_root":str(review), "source_files_sha256":review_before,
                                           "scores_unchanged":True, "new_subjective_review":False},
                            note="正式V3：准确性70/覆盖20/洞察10，表达不计分；沿用封存三维判断和分数，直接对比原采样评分。事后修订，评分者已见过身份，非独立盲评；零新增采样。")
        else:
            manifest.update(reweight={"source_root": str(review), "source_files_sha256": review_before,
                                      "source_version": 3, "accuracy_factor": "1.4", "new_subjective_review": False},
                            note="事后权重调整：沿用封存V3逐点判断，准确性×1.4，覆盖/洞察不变，表达退出计分。评分者此前见过身份；不是新的独立盲评。零新增采样。")
    if repair is not None:
        manifest["final_answer_repair"] = repair["metadata"]
        manifest["note"] = "事后修复完成文本提取并按修订目录封存的细则评分；评分者此前见过模型身份。重新匿名并隐藏性能，不是完全独立盲评。零新增模型调用，费用和执行数据均来自原批次。"
    if neutral_review is not None:
        manifest.update(review_revision=review_revision,
            review_source={"source_root":str(neutral_review),"source_files_sha256":neutral_before},
            note="正式V3中立复核：权重、核验点和原则处罚不变；全篇复核后逐项对照旧判断。评分者已见过历史身份，非独立盲评；零新增采样，主对照仍为原V2。")
    write(output / "evaluation/manifest.json", manifest)
    protocol = protocol | {"offline_regrade": True, "evaluation_manifest_sha256": digest(manifest)}
    write(output / "protocol.json", protocol)
    write(output / "preparation-seal.json", {"protocol_sha256": sha(output / "protocol.json")})
    check_inputs(output)
    require(all(sha(inside(source, name)) == expected_sha for name, expected_sha in before.items()), "Source changed during regrade preparation")
    result = {"run_dir": str(output), "source_batch": source.name, "scoring_version": gold["scoring_version"],
            "inference_started": False, "next": "blind", "snapshot_files": len(files),
            **({"final_answer_repair": repair["metadata"]} if repair is not None else {})}
    if review is not None:
        from reporting import blind, seal
        blind(output)
        mapping = read(output / "grading/private-mapping.json")
        by_result = {relative: review_scores[anon] for anon,relative in review_mapping.items()}
        convert = formal_v3_score if adopt_scores else v4_score_from_v3
        migrated = {anon: convert(by_result[relative], review_gold) for anon,relative in mapping.items()}
        validator = validate_imported_scores if adopt_scores else validate_reweighted_scores
        validator(output, mapping, migrated, gold)
        write(output / "evaluation/derived-scores.json", migrated)
        result.update(seal(output, output / "evaluation/derived-scores.json"), next="report", deterministic_reweight=not adopt_scores,
                      scores_imported_unchanged=adopt_scores)
        require(all(sha(inside(review, name)) == expected for name,expected in review_before.items()), "V3 source changed during weight migration")
    return result
