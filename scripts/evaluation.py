"""Create a self-contained, immutable offline evaluation of existing samples."""
from pathlib import Path
import shutil

from core import check_inputs, check_sensitive, digest, inside, now, read, require, sha, validate_gold, write


def regrade(source, rubric_path, output, *, repair_final_answers=False):
    # Import here: report generation also reads this module's manifest as data.
    from reporting import idle, score_data, summarize

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
    require(gold.get("scoring_version") == 2, "Regrade requires an explicit V2 rubric")
    frozen_gold = read(source / "input/gold.json")
    require(gold["reference_facts"] == frozen_gold["reference_facts"] and gold["verification"] == frozen_gold["verification"], "Regrade must preserve original reference facts and verification")
    check_sensitive(Path(rubric_path).read_text(encoding="utf-8-sig"))

    repair = None
    if repair_final_answers:
        from final_repair import prepare_final_answer_repair
        from reporting import validate_records
        validate_records(source)
        repair = prepare_final_answer_repair(source, protocol)

    copies = {f.relative_to(source).as_posix(): f.relative_to(source).as_posix()
              for f in (source / "input").rglob("*") if f.is_file()}
    for f in (source / "runs").glob("*/attempt-*/*"):
        if f.is_file() and f.name in {"answer.txt", "result.json", "events.jsonl", "task-tree.json"}:
            copies[f.relative_to(source).as_posix()] = f.relative_to(source).as_posix()
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
        check_sensitive(f.read_text(encoding="utf-8-sig"))
        before[name] = sha(f)
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
    files["evaluation/rubric.json"] = sha(output / "evaluation/rubric.json")
    manifest = {"schema_version": 1, "mode": "offline-regrade", "created_at": now(),
                "source_batch": source.name, "source_root": str(source), "source_files_sha256": before,
                "files_sha256": files, "scoring_version": 2, "post_hoc": True,
                "evaluator_previously_saw_model_identity": True,
                "note": "事后新增细则；评分者此前见过模型身份。重新匿名并隐藏性能，不是完全独立盲评。No new inference."}
    if repair is not None:
        manifest["final_answer_repair"] = repair["metadata"]
        manifest["note"] = "事后修复完成文本提取并按修订目录封存的细则评分；评分者此前见过模型身份。重新匿名并隐藏性能，不是完全独立盲评。零新增模型调用，费用和执行数据均来自原批次。"
    write(output / "evaluation/manifest.json", manifest)
    protocol = protocol | {"offline_regrade": True, "evaluation_manifest_sha256": digest(manifest)}
    write(output / "protocol.json", protocol)
    write(output / "preparation-seal.json", {"protocol_sha256": sha(output / "protocol.json")})
    check_inputs(output)
    require(all(sha(inside(source, name)) == expected_sha for name, expected_sha in before.items()), "Source changed during regrade preparation")
    return {"run_dir": str(output), "source_batch": source.name, "scoring_version": 2,
            "inference_started": False, "next": "blind", "snapshot_files": len(files),
            **({"final_answer_repair": repair["metadata"]} if repair is not None else {})}
