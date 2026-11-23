"""Project model benchmark CLI. Preparation/analysis never starts inference."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from core import BenchmarkError, EFFORTS, MODELS, check_inputs, home, prepare, read, require, require_sampling_allowed, select_project, write
from evaluation import regrade
from inventory import history as topic_history, inventory
from comparison import compare
from reporting import archive, blind, build_report, seal, validate
from runtime import Server, execute, capability_smoke


def retest(parent, output=None, models=None, efforts=None, repetitions=5, prices=None, service_tiers=None):
    parent = Path(parent).resolve()
    p = require_sampling_allowed(parent)
    require(not p.get("execution_profile"), "Project-readonly retests require a new prepare with a fresh source workspace and execution specification")
    require((parent / "summary.json").exists() and (parent / "grading/seal.json").exists(), "Finish scoring/reporting the parent batch before retesting")
    validate(parent)
    tiers = service_tiers if service_tiers is not None else p.get("service_tiers")
    root = prepare(parent / "input", output, models or p["models"], efforts or p["efforts"], repetitions, prices, parent, tiers)
    q = check_inputs(root)
    require(q["case_id"] == p["case_id"] and q["input_hashes"] == p["input_hashes"], "Retest inputs differ from parent")
    summary = read(parent / "summary.json")
    write(root / "parent-summary.json", {k: summary[k] for k in ["batch", "case_id", "conditions", "planned", "valid_completed", "generated_at"]} | {"fingerprint": read(parent / "runtime.json").get("fingerprint") if (parent / "runtime.json").exists() else None})
    return root


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    discover = sub.add_parser("discover", help="Resolve current/specified/frequently used local project")
    discover.add_argument("--project")
    discover.add_argument("--current", default=os.getcwd())
    discover.add_argument("--no-current", action="store_true")
    discover.add_argument("--projects-json")
    discover.add_argument("--threads-json")
    discover.add_argument("--output")
    history = sub.add_parser("history", help="List prior topics without answer/quality data")
    history.add_argument("--root", default=str(home() / "benchmarks"))
    listing = sub.add_parser("inventory", help="Read execution coverage of existing batches; no inference or quality data")
    scope = listing.add_mutually_exclusive_group()
    scope.add_argument("--root", help="Scan nested batches; defaults to the benchmark root")
    scope.add_argument("--run-dirs", nargs="+", help="Inspect only these exact batch roots")
    capture = sub.add_parser("capture-app-answer", help="Archive one completed App final verbatim; no inference")
    capture.add_argument("--message-json", required=True, help="Allowlisted full read_thread final-message receipt")
    capture.add_argument("--completion-json", required=True, help="Allowlisted wait_threads completion metadata")
    capture.add_argument("--thread-id", required=True)
    capture.add_argument("--turn-id", required=True)
    capture.add_argument("--output-dir", required=True)
    paired_prepare = sub.add_parser("paired-prepare", help="Freeze an authorized matched App review batch; no inference")
    paired_prepare.add_argument("--selection", required=True)
    paired_prepare.add_argument("--comparison", required=True)
    paired_prepare.add_argument("--batch-dir", required=True)
    paired_prepare.add_argument("--pilot")
    paired_prepare.add_argument("--design", choices=["matched-ab", "independent-reviewers"], default="matched-ab")
    paired_prepare.add_argument("--source-model")
    paired_prepare.add_argument("--reviewers", help="JSON configurations for two independent reviewers")
    paired_prepare.add_argument("--previous-batch", help="Read-only prior paired report to retain alongside the new group")
    for command in ("paired-status", "paired-next", "paired-register", "paired-collect", "paired-blind", "paired-grade-next", "paired-grade-complete", "paired-verify", "paired-report", "paired-run-cli", "paired-reconcile-cli", "paired-retry"):
        q = sub.add_parser(command)
        q.add_argument("--batch-dir", required=True)
        if command in ("paired-register", "paired-collect", "paired-grade-complete", "paired-run-cli", "paired-reconcile-cli", "paired-retry"):
            q.add_argument("--unit", required=True)
        if command == "paired-register":
            q.add_argument("--thread-id", required=True)
            q.add_argument("--host-id", default="local")
            q.add_argument("--grading", action="store_true")
        if command == "paired-collect":
            q.add_argument("--message-json", required=True)
            q.add_argument("--completion-json", required=True)
    revision = sub.add_parser("regrade", help="Prepare an isolated offline scoring revision; never starts inference")
    revision.add_argument("--run-dir", required=True)
    revision.add_argument("--rubric", required=True)
    revision.add_argument("--output", required=True)
    revision.add_argument("--condition-filter", help="JSON list selecting existing model/effort/service_tier conditions for offline grading")
    revision.add_argument("--scores-from", help="Adopt sealed 70/20/10 judgments unchanged into formal V3; compare original sampling grades, no inference")
    revision.add_argument("--review-from", help="Archive the pre-review formal V3 judgments for a fresh review of the same answers")
    revision.add_argument("--review-revision", help="Frozen review revision ID; requires --review-from")
    revision.add_argument("--repair-final-answers", action="store_true", help="Re-extract canonical completed finals from archived native events; no model calls")
    topic = sub.add_parser("blind-topic", help="Prepare a clean anonymous topic packet from existing answers; no inference")
    topic.add_argument("--entries-json", required=True)
    topic.add_argument("--rubric", required=True)
    topic.add_argument("--packet", required=True)
    topic.add_argument("--private-mapping", required=True)
    topic_check = sub.add_parser("validate-topic", help="Validate a sealed independent anonymous topic review")
    topic_check.add_argument("--packet", required=True)
    topic_check.add_argument("--expected-input-seal-sha256")
    topic_check.add_argument("--private-mapping", help="Coordinator-only original/capture validation; required for new App captures")
    comparison = sub.add_parser("compare", help="Create a separate offline comparison from sealed runs; never starts inference")
    comparison.add_argument("--run-dirs", nargs="+", required=True)
    comparison.add_argument("--output", required=True)
    comparison.add_argument("--models", nargs="+")
    comparison.add_argument("--condition-filter", help="JSON list of exact model/effort/service_tier selections; offline only")
    comparison.add_argument("--assessment", help="JSON with conclusion, evidence and limitations; omitted uses neutral notes")
    comparison.add_argument("--estimate-answer-tps", action="store_true", help="Estimate final text TPS with a pinned, preloaded local o200k_base proxy")
    adoption = sub.add_parser("adopt-smoke", help="Reuse verified capability checks across identical study environments; no inference")
    adoption.add_argument("--run-dir", required=True)
    adoption.add_argument("--source", required=True)
    recovery = sub.add_parser("recover-smoke", help="Replay an archived capability check into a fresh revision; no inference")
    recovery.add_argument("--run-dir", required=True)
    recovery.add_argument("--source", required=True)
    cleanup_agy = sub.add_parser("cleanup-agy-projects", help="Inspect or remove stale ephemeral Antigravity projects from completed benchmark runs; never starts inference")
    cleanup_agy.add_argument("--apply", action="store_true", help="Remove matched ephemeral project JSON files (default is dry-run)")
    cleanup_agy.add_argument("--archive-dir", help="Optional backup directory outside ~/.gemini/config/projects")
    cleanup_agy.add_argument("--benchmark-root", help="Override benchmarks directory (defaults to ~/.codex/benchmarks)")
    cleanup_agy.add_argument("--work-root", help="Override agy-benchmark-work directory (defaults to LOCALAPPDATA/agy-benchmark-work)")
    study_prepare = sub.add_parser("study-prepare", help="Prepare an Antigravity multi-topic cycle study; no inference")
    study_prepare.add_argument("--source-runs", nargs="+", required=True, help="Sealed historical batches, in topic order")
    study_prepare.add_argument("--study-dir", required=True)
    study_prepare.add_argument("--model", required=True)
    study_prepare.add_argument("--effort", required=True)
    study_prepare.add_argument("--cycles", type=int, default=5)
    study_prepare.add_argument("--agy")
    study_prepare.add_argument("--runtime-read-roots", nargs="+")
    study_prepare.add_argument("--access-mode", choices=["restricted", "full-access"], default="restricted")
    for command in ("study-preflight", "study-smoke", "study-run", "study-resume", "study-status", "study-recover-smoke"):
        q = sub.add_parser(command)
        q.add_argument("--study-dir", required=True)
        if command != "study-status":
            q.add_argument("--agy")
        if command == "study-resume":
            q.add_argument("--retry-failed", action="store_true")
        if command == "study-smoke":
            q.add_argument("--retry-failed", action="store_true")
        if command == "study-recover-smoke":
            q.add_argument("--source", required=True)
    for command in ["prepare", "retest"]:
        q = sub.add_parser(command, help="Prepare a new batch; never starts inference")
        q.add_argument("--case-dir" if command == "prepare" else "--parent", required=True)
        q.add_argument("--run-dir")
        q.add_argument("--models", nargs="+", default=MODELS if command == "prepare" else None)
        q.add_argument("--efforts", nargs="+", default=EFFORTS if command == "prepare" else None)
        q.add_argument("--repetitions", type=int, default=5)
        q.add_argument("--prices")
        q.add_argument("--service-tiers", nargs="+", choices=["default", "fast", "priority"], help="Speed modes; Fast is frozen as priority. Prepare defaults to Standard; retest inherits the parent.")
        if command == "prepare":
            q.add_argument("--execution-mode", choices=["project-readonly", "fixed-evidence"], default="project-readonly")
            q.add_argument("--execution-spec", help="JSON source_roots specification for a frozen read-only project workspace")
    for command in ["preflight", "smoke", "run", "resume", "blind", "seal", "report", "validate"]:
        q = sub.add_parser(command)
        q.add_argument("--run-dir", required=True)
        if command in ["preflight", "smoke", "run", "resume"]:
            q.add_argument("--codex")
        if command == "resume":
            q.add_argument("--retry-failed", action="store_true")
        if command == "smoke":
            q.add_argument("--continue", dest="continue_existing", action="store_true", help="Run only untouched capability positions after verified offline recovery; never retry failed attempts")
        if command == "seal":
            q.add_argument("--scores", required=True)
        if command == "blind":
            q.add_argument("--refresh", action="store_true", help="Archive unsealed grading and create fresh anonymous IDs")
        if command in ["report", "validate"]:
            q.add_argument("--partial", action="store_true")
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    if args.command.startswith("paired-"):
        import paired
        command = args.command
        if command == "paired-prepare":
            if args.design == "independent-reviewers":
                from independent_review import prepare as prepare_independent
                value = prepare_independent(args.selection, args.comparison, args.batch_dir, args.source_model, args.reviewers, args.previous_batch)
            else:
                require(args.pilot, "The matched A/B design requires --pilot")
                value = paired.prepare(args.selection, args.comparison, args.batch_dir, args.pilot)
        elif command == "paired-status":
            value = paired.status(args.batch_dir)
        elif command == "paired-next":
            value = paired.dispatch(args.batch_dir)
        elif command == "paired-register":
            value = paired.register(args.batch_dir, args.unit, args.thread_id, args.host_id, args.grading)
        elif command == "paired-collect":
            value = paired.collect(args.batch_dir, args.unit, args.message_json, args.completion_json)
        elif command == "paired-blind":
            value = paired.blind(args.batch_dir)
        elif command == "paired-grade-next":
            value = paired.grade_dispatch(args.batch_dir)
        elif command == "paired-grade-complete":
            value = paired.grade_complete(args.batch_dir, args.unit)
        elif command == "paired-report":
            from paired_report import export
            value = export(args.batch_dir)
        elif command in ("paired-run-cli", "paired-reconcile-cli", "paired-retry"):
            import independent_review
            action = {"paired-run-cli": independent_review.run_cli,
                      "paired-reconcile-cli": independent_review.reconcile_cli,
                      "paired-retry": independent_review.retry}[command]
            value = action(args.batch_dir, args.unit)
        else:
            value = paired.verify(args.batch_dir)
    elif args.command.startswith("study-"):
        import study
        if args.command == "study-prepare":
            value = study.prepare(args.source_runs, args.study_dir, args.model, args.effort, args.cycles,
                                  args.agy, args.runtime_read_roots, access_mode=args.access_mode)
        elif args.command == "study-preflight":
            value = study.preflight(args.study_dir, args.agy)
        elif args.command == "study-smoke":
            value = study.smoke(args.study_dir, args.agy, retry_failed=args.retry_failed)
        elif args.command == "study-status":
            value = study.status(args.study_dir)
        elif args.command == "study-recover-smoke":
            value = study.recover_smoke(args.study_dir, args.source, args.agy)
        else:
            value = study.execute(args.study_dir, args.agy, args.command == "study-resume", getattr(args, "retry_failed", False))
    elif args.command == "discover":
        value = select_project(args.project, None if args.no_current else args.current, read(args.projects_json) if args.projects_json else None, read(args.threads_json) if args.threads_json else None)
        if args.output:
            write(args.output, value)
    elif args.command == "history":
        value = topic_history(args.root)
    elif args.command == "inventory":
        value = inventory(root=args.root, run_dirs=args.run_dirs)
    elif args.command == "capture-app-answer":
        from app_capture import capture_app_answer
        value = capture_app_answer(args.message_json, args.completion_json, args.output_dir,
                                   args.thread_id, args.turn_id)
    elif args.command == "regrade":
        options = {"condition_filter": args.condition_filter} if args.condition_filter else {}
        if args.scores_from:
            options["scores_from"] = args.scores_from
        if args.review_from or args.review_revision:
            options.update(review_from=args.review_from, review_revision=args.review_revision)
        value = regrade(args.run_dir, args.rubric, args.output, repair_final_answers=args.repair_final_answers, **options)
    elif args.command == "blind-topic":
        from fresh_blind import prepare_topic
        value = prepare_topic(read(args.entries_json), args.rubric, args.packet, args.private_mapping)
    elif args.command == "validate-topic":
        from fresh_blind import check_topic_seal
        value = check_topic_seal(args.packet, args.expected_input_seal_sha256, args.private_mapping)
    elif args.command == "compare":
        options = {}
        if args.estimate_answer_tps:
            options["estimate_answer_tps"] = True
        if args.condition_filter:
            options["condition_filter"] = args.condition_filter
        value = compare(args.run_dirs, args.output, args.models, args.assessment, **options)
    elif args.command == "adopt-smoke":
        from agentic_runtime import adopt_capability
        value = adopt_capability(args.run_dir, args.source)
    elif args.command == "recover-smoke":
        from agentic_runtime import recover_capability
        value = recover_capability(args.run_dir, args.source)
    elif args.command == "cleanup-agy-projects":
        import antigravity as agy
        value = agy.cleanup_stale_projects(
            benchmark_root=args.benchmark_root,
            work_root=args.work_root,
            dry_run=not args.apply,
            archive_dir=args.archive_dir,
        )
    elif args.command in ["prepare", "retest"]:
        if args.command == "prepare":
            require(args.execution_mode != "project-readonly" or args.execution_spec, "Project-readonly preparation requires --execution-spec with source_roots")
            require(args.execution_mode != "fixed-evidence" or not args.execution_spec, "Fixed-evidence does not accept an execution specification")
            require(args.execution_mode != "fixed-evidence" or "ultra" not in args.efforts, "New Ultra measurements require native tools/delegation; use project-readonly")
            root = prepare(args.case_dir, args.run_dir, args.models, args.efforts, args.repetitions, args.prices, service_tiers=args.service_tiers,
                           execution_profile=read(args.execution_spec) if args.execution_spec else None)
        else:
            root = retest(args.parent, args.run_dir, args.models, args.efforts, args.repetitions, args.prices, args.service_tiers)
        p = check_inputs(root)
        value = {"run_dir": str(root), "title": p["title"], "planned_calls": len(p["schedule"]), "inference_started": False}
    else:
        root = Path(args.run_dir).resolve()
        if args.command in ("preflight", "smoke", "run", "resume"):
            require(check_inputs(root).get("runtime_backend") != "antigravity",
                    "Antigravity batches use study-preflight/study-smoke/study-run/study-resume with --study-dir")
        if args.command == "preflight":
            p = check_inputs(root)
            server = Server(root, args.codex)
            try:
                value = server.preflight(p)
            finally:
                server.close()
        elif args.command == "smoke":
            value = capability_smoke(root, args.codex, continue_existing=args.continue_existing)
        elif args.command in ["run", "resume"]:
            execute(root, args.codex, args.command == "resume", getattr(args, "retry_failed", False))
            value = {"sampling_finished": True, "run_dir": str(root), "next": "blind"}
        elif args.command == "blind":
            value = blind(root, args.refresh)
        elif args.command == "seal":
            value = seal(root, args.scores)
        elif args.command == "report":
            data = build_report(root, args.partial)
            checked = validate(root, args.partial, write_receipt=True)
            value = archive(root) | {"report": str(root / "report.html"), "validation": checked, "valid_completed": data["valid_completed"], "planned": data["planned"]}
        else:
            value = validate(root, args.partial)
    print(json.dumps(value, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BenchmarkError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
    except (ValueError, ConnectionError, OSError, KeyError, TypeError):
        # Never print arbitrary server/config exceptions: they can include signed URLs.
        print("Benchmark stopped. Inspect durable state and sanitized diagnostics; check inputs, active processes, requested capabilities and sealed files. No model fallback was applied.", file=sys.stderr)
        raise SystemExit(1)
