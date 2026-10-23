"""Project model benchmark CLI. Preparation/analysis never starts inference."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from core import BenchmarkError, EFFORTS, MODELS, check_inputs, home, prepare, read, require, require_sampling_allowed, select_project, write
from evaluation import regrade
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
    revision = sub.add_parser("regrade", help="Prepare an isolated offline scoring revision; never starts inference")
    revision.add_argument("--run-dir", required=True)
    revision.add_argument("--rubric", required=True)
    revision.add_argument("--output", required=True)
    comparison = sub.add_parser("compare", help="Create a separate offline comparison from sealed runs; never starts inference")
    comparison.add_argument("--run-dirs", nargs="+", required=True)
    comparison.add_argument("--output", required=True)
    comparison.add_argument("--models", nargs="+")
    comparison.add_argument("--assessment", help="JSON with conclusion, evidence and limitations; omitted uses neutral notes")
    adoption = sub.add_parser("adopt-smoke", help="Reuse verified capability checks across identical study environments; no inference")
    adoption.add_argument("--run-dir", required=True)
    adoption.add_argument("--source", required=True)
    recovery = sub.add_parser("recover-smoke", help="Replay an archived capability check into a fresh revision; no inference")
    recovery.add_argument("--run-dir", required=True)
    recovery.add_argument("--source", required=True)
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
    if args.command == "discover":
        value = select_project(args.project, None if args.no_current else args.current, read(args.projects_json) if args.projects_json else None, read(args.threads_json) if args.threads_json else None)
        if args.output:
            write(args.output, value)
    elif args.command == "history":
        value = [{k: p.get(k) for k in ["title", "project", "case_id", "created_at"]} for f in sorted(Path(args.root).glob("*/protocol.json")) if (p := read(f))]
    elif args.command == "regrade":
        value = regrade(args.run_dir, args.rubric, args.output)
    elif args.command == "compare":
        value = compare(args.run_dirs, args.output, args.models, args.assessment)
    elif args.command == "adopt-smoke":
        from agentic_runtime import adopt_capability
        value = adopt_capability(args.run_dir, args.source)
    elif args.command == "recover-smoke":
        from agentic_runtime import recover_capability
        value = recover_capability(args.run_dir, args.source)
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
            checked = validate(root, args.partial)
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
