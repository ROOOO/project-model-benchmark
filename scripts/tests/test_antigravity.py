"""CLI transport, permission receipts and cycle recovery without paid inference."""
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import antigravity as agy
import study
from benchmark import parser
from billing import subscription_metrics
from core import BenchmarkError, build_schedule, digest, sha, write
import core


class FakeProcess:
    def __init__(self, events, code=0, diagnostic=""):
        self.pid = os.getpid()
        self.stdin = io.StringIO()
        self.stdout = io.StringIO("".join(json.dumps(e) + "\n" for e in events))
        self.stderr = io.StringIO(diagnostic)
        self.code = code

    def wait(self):
        return self.code


def native_events(model="gemini-4-neon-xhigh", response="完成", status="SUCCESS", permission_mode="proceed-in-sandbox", **extra):
    usage = {"input_tokens": 100, "output_tokens": 20, "thinking_tokens": 12, "cache_read_tokens": 30, "total_tokens": 120}
    return [{"event": "init", "conversation_id": "fresh-id", "init": {"model": model, "permission_mode": permission_mode, "cwd": "scratch", "tools": ["run_command"]}},
            {"event": "step_update", "step_update": {"step_type": "thinking", "text_delta": "PRIVATE_REASONING"}},
            {"event": "step_update", "step_update": {"step_type": "agent_response", "step_index": 2, "state": "DONE", "text_delta": "stale draft"}},
            {"event": "result", "result": {"status": status, "response": response, "conversation_id": "fresh-id",
                                             "num_turns": 1, "usage": usage, **extra}}]


class CliTransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / "batch"
        (self.root / "input").mkdir(parents=True)
        (self.root / "input/prompt.txt").write_text("题目", encoding="utf-8")
        write(self.root / "input/instructions.json", {"base": None, "developer": "Read sources only."})
        self.profile = {"readonly_root": str(self.base / "readonly"), "source_root": str(self.base / "readonly/source"),
                        "probe_root": str(self.base / "readonly/probe"), "forbidden_read_paths": [str(self.base / "private")],
                        "runtime_read_roots": [str(Path(sys.executable).parent)]}
        self.protocol = {"runtime_backend": "antigravity", "execution_profile": self.profile}
        self.slot = {"id": "0001", "model": "gemini-4-neon-xhigh", "effort": "xhigh", "repetition": 1}

    def tearDown(self):
        self.temp.cleanup()

    def run_fake(self, events, code=0):
        with patch.object(agy.subprocess, "Popen", return_value=FakeProcess(events, code)) as spawn:
            result = agy.run_trial(self.root, self.protocol, self.slot, 1, Path(sys.executable), {}, self.base / "scratch")
        args = spawn.call_args.args[0]
        if self.profile.get("mode") == agy.FULL_MODE:
            self.assertNotIn("--sandbox", args)
            self.assertIn("--dangerously-skip-permissions", args)
        else:
            self.assertIn("--sandbox", args)
            self.assertNotIn("--dangerously-skip-permissions", args)
        self.assertIn("--settings", args)
        self.assertNotIn("--continue", args)
        self.assertNotIn("--print-timeout", args)
        return result

    def test_terminal_response_is_authoritative_and_reasoning_is_not_archived(self):
        result = self.run_fake(native_events())
        self.assertTrue(result["valid"])
        directory = self.root / "runs/0001/attempt-001"
        answer = (directory / "answer.txt").read_text(encoding="utf-8")
        self.assertEqual(answer, "完成")
        self.assertNotIn("PRIVATE_REASONING", (directory / "events.jsonl").read_text(encoding="utf-8"))
        self.assertIsNone(result["visible_decode_tps"])
        self.assertIsNone(result["visible_tokens"])
        self.assertFalse((self.root / "inflight.json").exists())
        agy.validate_record(self.root, self.protocol, result, directory, answer)

    def test_wrong_model_or_empty_success_is_not_a_valid_sample(self):
        result = self.run_fake(native_events(model="fallback-model"))
        self.assertFalse(result["valid"])
        self.assertIn("effective_model_not_verified", result["protocol_issues"])

    def test_empty_success_is_not_valid(self):
        self.assertFalse(self.run_fake(native_events(response=""))["valid"])

    def test_quota_error_is_distinguished_and_not_retried(self):
        result = self.run_fake(native_events(response="", status="ERROR", error="Resource exhausted: quota exceeded"), 1)
        self.assertFalse(result["valid"])
        self.assertEqual(result["error_kind"], "quota_or_rate_limit")

    def test_missing_terminal_preserves_unresolved_inflight(self):
        result = self.run_fake(native_events()[:-1], 1)
        self.assertFalse(result["valid"])
        self.assertEqual(result["status"], "interrupted")
        self.assertTrue((self.root / "inflight.json").exists())

    def test_duplicate_terminal_receipts_are_rejected(self):
        events = native_events()
        self.assertFalse(self.run_fake(events + events[-1:])["valid"])

    def test_unknown_children_and_prices_do_not_become_zero(self):
        result = subscription_metrics({"runtime_backend": "antigravity", "usage": {"outputTokens": 20}})
        self.assertTrue(all(value is None for value in result.values()))

    def test_session_policy_uses_scoped_read_only_source_and_no_command_allow(self):
        settings = agy.session_settings(self.profile, self.base / "scratch")
        self.assertEqual(settings["toolPermission"], "proceed-in-sandbox")
        self.assertTrue(settings["enableTerminalSandbox"])
        self.assertIn("mcp(*)", settings["permissions"]["deny"])
        self.assertFalse(any(value.startswith("command(") for value in settings["permissions"]["allow"]))
        self.assertIn(f"read_file({Path(self.profile['readonly_root']).resolve().as_posix()})", settings["permissions"]["allow"])
        if os.name == "nt":
            self.assertTrue(any(rule.startswith("read_file(" + Path(self.profile["readonly_root"]).resolve().drive)
                                for rule in settings["permissions"]["allow"]))

    def test_permission_soft_denial_is_recognized(self):
        self.assertEqual(agy.error_kind('Print mode: soft-denying tool confirmation "ReadFile"'), "permission_required")

    def test_full_access_settings_and_effective_receipt(self):
        self.profile["mode"] = agy.FULL_MODE
        settings = agy.session_settings(self.profile, self.base / "scratch")
        self.assertFalse(settings["enableTerminalSandbox"])
        self.assertTrue(settings["allowNonWorkspaceAccess"])
        self.assertEqual(settings["toolPermission"], "always-proceed")
        self.assertFalse(settings["permissions"]["ask"])
        self.assertFalse(settings["permissions"]["deny"])
        result = self.run_fake(native_events(permission_mode="always-proceed"))
        self.assertTrue(result["valid"])
        self.assertEqual(result["effective"]["permissionMode"], "always-proceed")

    def test_full_access_cannot_accept_restricted_receipt(self):
        self.profile["mode"] = agy.FULL_MODE
        result = self.run_fake(native_events())
        self.assertFalse(result["valid"])
        self.assertIn("full_access_receipt_not_verified", result["protocol_issues"])

    def test_scoring_material_access_is_flagged_and_excluded(self):
        events = native_events()
        events.insert(-1, {"event": "step_update", "step_update": {"step_type": "tool", "tool_name": "run_command", "step_index": 3,
                          "tool_info": {"parameters": {"CommandLine": 'Get-Content C:\\Users\\lw\\.codex\\benchmarks\\old\\input\\gold.json'}}}})
        result = self.run_fake(events)
        self.assertTrue(result["transport_valid"])
        self.assertTrue(result["contaminated"])
        self.assertFalse(result["valid"])

    def test_source_tool_access_and_reasoning_are_not_contamination(self):
        events = native_events()
        events.insert(-1, {"event": "step_update", "step_update": {"step_type": "tool", "tool_name": "view_file", "step_index": 3,
                          "tool_info": {"parameters": {"AbsolutePath": 'C:\\frozen\\Logic\\GameMain.cpp'}}}})
        self.assertFalse(self.run_fake(events)["contaminated"])

    def test_permission_probe_requires_actual_tool_output(self):
        directory = self.base / "probe"
        directory.mkdir()
        command = "exact probe command"
        value = {"benchmark_permission_probe": 1, "source_read": "a" * 64, "scratch_write": True,
                 "source_write_blocked": True, "private_read_blocked": True}
        path = directory / "events.jsonl"
        path.write_text(json.dumps({"event": "result", "result": {"response": json.dumps(value)}}), encoding="utf-8")
        with self.assertRaises(BenchmarkError):
            study.probe_receipt(directory, command, "a" * 64)

        tool = {"event": "step_update", "step_update": {"tool_info": {
            "parameters": {"CommandLine": command}, "output": json.dumps(value)}}}
        path.write_text(json.dumps(tool), encoding="utf-8")
        self.assertEqual(study.probe_receipt(directory, command, "a" * 64), value)
        value["private_read_blocked"] = False
        tool["step_update"]["tool_info"]["output"] = json.dumps(value)
        path.write_text(json.dumps(tool), encoding="utf-8")
        with self.assertRaises(BenchmarkError):
            study.probe_receipt(directory, command, "a" * 64)

    def test_cli_truncated_command_retains_verifiable_probe_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            command = "python -c " + "a" * 300
            value = {"benchmark_permission_probe": 1, "source_read": "b" * 64, "scratch_write": True}
            event = {"event": "step_update", "step_update": {"tool_info": {
                "parameters": {"CommandLine": command[:150] + "…"}, "output": json.dumps(value)}}}
            (directory / "events.jsonl").write_text(json.dumps(event), encoding="utf-8")
            self.assertEqual(study.probe_receipt(directory, command, "b" * 64, full_access=True), value)
            with self.assertRaises(BenchmarkError):
                study.probe_receipt(directory, "different " + command, "b" * 64, full_access=True)


class CycleTests(unittest.TestCase):
    def test_round_robin_order_for_five_cycles(self):
        topics = ["preload", "module", "newjoy", "active-wall"]
        schedule = study.cycle_schedule(topics, 5)
        self.assertEqual(len(schedule), 20)
        self.assertEqual([s["topic"] for s in schedule], topics * 5)
        self.assertEqual([s["cycle"] for s in schedule], [c for c in range(1, 6) for _ in topics])
        self.assertEqual(len({(s["cycle"], s["topic"]) for s in schedule}), 20)

    def test_invalid_cycle_inputs(self):
        for topics, cycles in [([], 5), (["a", "a"], 5), (["a"], 0)]:
            with self.assertRaises(BenchmarkError):
                study.cycle_schedule(topics, cycles)

    def test_existing_cli_unchanged_and_new_cycle_commands_parse(self):
        self.assertEqual(parser().parse_args(["inventory"]).command, "inventory")
        args = parser().parse_args(["study-prepare", "--source-runs", "a", "b", "--study-dir", "new",
                                    "--model", "gemini-4-neon-xhigh", "--effort", "xhigh"])
        self.assertEqual(args.cycles, 5)
        full = parser().parse_args(["study-prepare", "--source-runs", "a", "--study-dir", "new",
                                   "--model", "gemini-4-neon-xhigh", "--effort", "xhigh", "--access-mode", "full-access"])
        self.assertEqual(full.access_mode, "full-access")

    def test_resume_skips_success_preserves_order_and_stops_on_quota(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            topics = ["a", "b", "c", "d"]
            manifest = {"topics": topics, "cycles": 5, "schedule": study.cycle_schedule(topics, 5),
                        "binary": sys.executable, "work_root": str(root / "work")}
            protocols = {}
            for topic in topics:
                batch = root / topic
                batch.mkdir()
                protocols[topic] = {"schedule": build_schedule(["gemini-4-neon-xhigh"], ["xhigh"], 5, 0), "execution_profile": {}}
            write(root / "capability.json", {"passed": True, "fingerprint": {"binary_sha256": sha(Path(sys.executable))}})
            success = {"slot_id": "0001", "attempt": 1, "status": "completed", "valid": True}
            write(root / "a/runs/0001/attempt-001/result.json", success)
            calls = []

            def run(batch, protocol, slot, attempt, *args):
                calls.append((batch.name, slot["repetition"]))
                record = {"slot_id": slot["id"], "attempt": attempt, "status": "failed", "valid": False,
                          "error_kind": "quota_or_rate_limit"} if batch.name == "b" and slot["repetition"] == 2 else {
                          "slot_id": slot["id"], "attempt": attempt, "status": "completed", "valid": True}
                write(batch / f"runs/{slot['id']}/attempt-{attempt:03d}/result.json", record)
                return record

            with patch.object(study, "check_study", return_value=manifest), \
                 patch.object(study, "check_inputs", side_effect=lambda p: protocols[Path(p).name]), \
                 patch.object(study, "preflight", return_value={"fingerprint": {"binary_sha256": sha(Path(sys.executable))}}), \
                 patch.object(agy, "check_profile"), patch.object(agy, "run_trial", side_effect=run):
                result = study.execute(root, resume=True)
                self.assertEqual(calls, [("b", 1), ("c", 1), ("d", 1), ("a", 2), ("b", 2)])
                self.assertEqual(result["complete_cycles"], 1)
                self.assertEqual(result["valid_completed"], 5)
                self.assertEqual(result["state"]["stage"], "quota_paused")
                self.assertEqual(result["next_missing"], {"cycle": 2, "topic": "b", "slot_id": "0002"})
                calls.clear()
                study.execute(root, resume=True)
                self.assertFalse(calls)


class CliExportTests(unittest.TestCase):
    def test_frozen_import_scoring_reports_and_tamper_detection(self):
        self.export_fixture("restricted")

    def test_full_access_import_scoring_reports_and_tamper_detection(self):
        self.export_fixture("full-access")

    def export_fixture(self, access_mode):
        from reporting import archive, blind, build_report, seal, validate, validate_records
        from test_benchmark import fixture
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            case, _ = fixture(base)
            old = core.prepare(case, base / "historical", ["old-model"], ["max"], 1)
            source = old / "input/source-snapshot"
            source.mkdir()
            (source / "file.cpp").write_text("int value = 1;\n", encoding="utf-8")
            hashes = agy.source_hashes(source)
            write(old / "input/execution-manifest.json", {"files": {k: {"sha256": v} for k, v in hashes.items()}})
            write(old / "input/instructions.json", {"base": None, "developer": core.PROJECT_INSTRUCTIONS})
            profile = {"mode": agy.MODE, "policy": agy.POLICY, "run_root": str(old),
                       "source_content_sha256": digest(hashes), "manifest_sha256": sha(old / "input/execution-manifest.json")}
            profile["comparison_fingerprint"] = digest({"test": True})
            profile["profile_sha256"] = agy.profile_hash(profile)
            protocol = core.read(old / "protocol.json")
            protocol.update(execution_profile=profile, input_hashes={p.relative_to(old / "input").as_posix(): sha(p)
                            for p in (old / "input").rglob("*") if p.is_file()},
                            instructions_sha256=sha(old / "input/instructions.json"))
            write(old / "protocol.json", protocol)
            write(old / "preparation-seal.json", {"protocol_sha256": sha(old / "protocol.json")})
            write(old / "grading/seal.json", {})
            new = base / "study"
            with patch.dict(os.environ, {"LOCALAPPDATA": str(base / "local")}):
                study.prepare([old], new, "gemini-4-neon-xhigh", "xhigh", 1, sys.executable, access_mode=access_mode)
            study.check_study(new, runtime=True)
            batch = new / "historical"
            p = core.check_inputs(batch)
            answer = "None 使用默认值；空字符串保持原值。建议分别断言这两种输入。"
            permission_mode = "always-proceed" if access_mode == "full-access" else "proceed-in-sandbox"
            with patch.object(agy.subprocess, "Popen", return_value=FakeProcess(native_events(response=answer, permission_mode=permission_mode))):
                agy.run_trial(batch, p, p["schedule"][0], 1, Path(sys.executable), {}, base / "one-scratch")
            validate_records(batch)
            blind(batch)
            scores = core.read(batch / "grading/packet/scores-template.json")
            for value in scores.values():
                value.update(points={"A1": 70, "R1": 30}, notes={"A1": "Fixture answer covers the two inputs.", "R1": "Clear fixture answer."})
            write(base / "scores.json", scores)
            seal(batch, base / "scores.json")
            build_report(batch)
            self.assertTrue(validate(batch)["passed"])
            archive(batch)
            record_path = batch / "runs/0001/attempt-001/result.json"
            record = core.read(record_path)
            record["usage"]["inputTokens"] += 1
            write(record_path, record)
            with self.assertRaises(BenchmarkError):
                validate_records(batch)


class EphemeralProjectCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.projects = self.base / "agy-projects"
        self.projects.mkdir()
        self.root = self.base / "batch"
        (self.root / "input").mkdir(parents=True)
        (self.root / "input/prompt.txt").write_text("题目", encoding="utf-8")
        write(self.root / "input/instructions.json", {"base": None, "developer": "Read sources only."})
        self.profile = {"mode": agy.FULL_MODE}
        self.protocol = {"runtime_backend": "antigravity", "execution_profile": self.profile}
        self.slot = {"id": "0001", "model": "gemini-4-neon-xhigh", "effort": "xhigh", "repetition": 1}

    def tearDown(self):
        self.temp.cleanup()

    def write_project(self, filename, payload):
        path = self.projects / filename
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def test_folder_uri_parsing_handles_windows_and_rejects_unc(self):
        target = self.base / "scratch/r4-t1-m1-b-001"
        posix = target.resolve().as_posix()
        if os.name == "nt":
            drive, rest = posix[:2], posix[2:]
            two_slash = f"file://{drive}{rest}"
            three_slash_encoded = f"file:///{drive[0].lower()}%3A{rest}"
            self.assertEqual(agy._canonical_path(agy.folder_uri_to_path(two_slash)), agy._canonical_path(target))
            self.assertEqual(agy._canonical_path(agy.folder_uri_to_path(three_slash_encoded)), agy._canonical_path(target))
        else:
            self.assertEqual(agy._canonical_path(agy.folder_uri_to_path("file://" + posix)), agy._canonical_path(target))
        self.assertIsNone(agy.folder_uri_to_path("file://remote-host/share/dir"))
        self.assertIsNone(agy.folder_uri_to_path("https://example.com/dir"))

    def test_new_project_cleans_only_matching_scratch_and_protects_other_projects(self):
        scratch = self.base / "scratch/r1-t1-m1-b-001"
        sibling = self.base / "scratch/r1-t2-m1-b-001"
        default_proj = self.write_project("default-cli-project.json", {"id": "default-cli-project", "name": "CLI Project", "projectResources": {}})
        sidecar_proj = self.write_project("sidecar.json", {"id": "sidecar", "name": "swarm/swarm_board", "sidecar": {"sidecarId": "swarm/swarm_board"}, "projectResources": {"resources": [{"folderUri": "file://" + scratch.resolve().as_posix()}]}})
        git_proj = self.write_project("repo.json", {"id": "repo", "name": "repo", "projectResources": {"resources": [{"gitFolder": {"folderUri": "file://" + scratch.resolve().as_posix()}}]}})
        preexisting = self.write_project("preexisting.json", {"id": "preexisting", "name": "pre", "projectResources": {"resources": [{"folderUri": "file://" + scratch.resolve().as_posix()}]}})
        sibling_proj = self.write_project("sibling.json", {"id": "sibling", "name": "r1-t2-m1-b-001", "projectResources": {"resources": [{"folderUri": "file://" + sibling.resolve().as_posix()}]}})
        created = []

        def fake_popen(args, **kwargs):
            cwd = Path(kwargs["cwd"]).resolve()
            created.append(self.write_project("new-trial-uuid.json", {
                "id": "new-trial-uuid", "name": cwd.name,
                "projectResources": {"resources": [{"folderUri": "file://" + cwd.as_posix()}]},
            }))
            return FakeProcess(native_events(permission_mode="always-proceed"))

        with patch.object(agy, "projects_config_dir", return_value=self.projects), \
             patch.object(agy.subprocess, "Popen", side_effect=fake_popen):
            record = agy.run_trial(self.root, self.protocol, self.slot, 1, Path(sys.executable), {}, scratch,
                                   prompt_override="q", new_project=True)
        self.assertTrue(record["valid"])
        self.assertFalse(created[0].exists())
        self.assertEqual([item["id"] for item in record["ephemeral_project_cleanup"]["removed"]], ["new-trial-uuid"])
        for protected in (default_proj, sidecar_proj, git_proj, preexisting, sibling_proj):
            self.assertTrue(protected.exists(), f"{protected.name} must not be deleted")

    def test_interrupted_trial_preserves_ephemeral_project(self):
        scratch = self.base / "scratch/r1-t3-m1-b-001"
        created = []

        def fake_popen(args, **kwargs):
            cwd = Path(kwargs["cwd"]).resolve()
            created.append(self.write_project("inflight-uuid.json", {
                "id": "inflight-uuid", "name": cwd.name,
                "projectResources": {"resources": [{"folderUri": "file://" + cwd.as_posix()}]},
            }))
            return FakeProcess(native_events(permission_mode="always-proceed")[:-1], code=1)

        with patch.object(agy, "projects_config_dir", return_value=self.projects), \
             patch.object(agy.subprocess, "Popen", side_effect=fake_popen):
            record = agy.run_trial(self.root, self.protocol, self.slot, 1, Path(sys.executable), {}, scratch,
                                   prompt_override="q", new_project=True)
        self.assertEqual(record["status"], "interrupted")
        self.assertTrue(created[0].exists())
        self.assertNotIn("ephemeral_project_cleanup", record)

    def test_explicit_project_passes_flag_is_exclusive_and_never_deleted(self):
        scratch = self.base / "scratch/explicit-slot"
        fixed = self.write_project("slot-1.json", {
            "id": "slot-1", "name": "slot-1",
            "projectResources": {"resources": [{"folderUri": "file://" + scratch.resolve().as_posix()}]},
        })
        with self.assertRaises(BenchmarkError):
            agy.run_trial(self.root, self.protocol, self.slot, 1, Path(sys.executable), {}, scratch,
                          prompt_override="q", new_project=True, project="slot-1")
        with patch.object(agy, "projects_config_dir", return_value=self.projects), \
             patch.object(agy.subprocess, "Popen", return_value=FakeProcess(native_events(permission_mode="always-proceed"))) as spawn:
            record = agy.run_trial(self.root, self.protocol, self.slot, 1, Path(sys.executable), {}, scratch,
                                   prompt_override="q", project="slot-1")
        args = spawn.call_args.args[0]
        self.assertIn("--project", args)
        self.assertEqual(args[args.index("--project") + 1], "slot-1")
        self.assertNotIn("--new-project", args)
        self.assertEqual(record["project"], "slot-1")
        self.assertTrue(fixed.exists())

    def test_cleanup_stale_projects_respects_active_and_unverified_and_archives(self):
        work = self.base / "agy-benchmark-work"
        bench = self.base / "benchmarks"
        done_scratch = work / "batchA/scratch/r1-t1-m1-b-001"
        active_scratch = work / "batchA/scratch/r1-t2-m1-b-001"
        peer_done_input = bench / "paired1/units/u1/peer-state/runs/run-done/input"
        peer_active_input = bench / "paired1/units/u2/peer-state/runs/run-act/input"
        peer_unverified_input = bench / "paired1/units/u3/peer-state/runs/run-unv/input"
        for d in (done_scratch, active_scratch, peer_done_input, peer_active_input, peer_unverified_input):
            d.mkdir(parents=True)
        write(bench / "indep1/units/r1-t2-m1-b/state.json", {"status": "running", "scratch": str(active_scratch)})
        write(peer_done_input.parent / "run.json", {"status": "completed", "terminal_verified": True})
        write(peer_active_input.parent / "run.json", {"status": "running", "terminal_verified": False})
        write(bench / "paired1/units/u2/peer-state/active.json", {"run_id": "run-act"})
        write(peer_unverified_input.parent / "run.json", {"status": "unknown-inflight", "terminal_verified": False})

        p_done = self.write_project("p-done.json", {"id": "p-done", "name": "r1-t1-m1-b-001", "projectResources": {"resources": [{"folderUri": "file://" + done_scratch.resolve().as_posix()}]}})
        p_act = self.write_project("p-act.json", {"id": "p-act", "name": "r1-t2-m1-b-001", "projectResources": {"resources": [{"folderUri": "file://" + active_scratch.resolve().as_posix()}]}})
        p_peer_done = self.write_project("p-peer-done.json", {"id": "p-peer-done", "name": "input", "projectResources": {"resources": [{"folderUri": "file://" + peer_done_input.resolve().as_posix()}]}})
        p_peer_act = self.write_project("p-peer-act.json", {"id": "p-peer-act", "name": "input", "projectResources": {"resources": [{"folderUri": "file://" + peer_active_input.resolve().as_posix()}]}})
        p_peer_unv = self.write_project("p-peer-unv.json", {"id": "p-peer-unv", "name": "input", "projectResources": {"resources": [{"folderUri": "file://" + peer_unverified_input.resolve().as_posix()}]}})
        p_user = self.write_project("user.json", {"id": "user", "name": ".codex", "projectResources": {"resources": [{"folderUri": "file://" + (self.base / "user-dir").resolve().as_posix()}]}})

        with patch.object(agy, "projects_config_dir", return_value=self.projects):
            with self.assertRaises(BenchmarkError):
                agy.cleanup_stale_projects(benchmark_root=bench, work_root=work, dry_run=False, archive_dir=self.projects / "sub")
            preview = agy.cleanup_stale_projects(benchmark_root=bench, work_root=work, dry_run=True)
            self.assertEqual({c["id"] for c in preview["candidates"]}, {"p-done", "p-peer-done"})
            self.assertEqual({s["id"] for s in preview["skipped_active"]}, {"p-act", "p-peer-act"})
            self.assertEqual({s["id"] for s in preview["skipped_unverified"]}, {"p-peer-unv"})
            self.assertTrue(p_done.exists() and p_peer_done.exists())

            archive_dir = self.base / "archive-backup"
            applied = agy.cleanup_stale_projects(benchmark_root=bench, work_root=work, dry_run=False, archive_dir=archive_dir)
            self.assertEqual(applied["removed_count"], 2)
            self.assertFalse(p_done.exists())
            self.assertFalse(p_peer_done.exists())
            self.assertTrue((archive_dir / "p-done.json").is_file())
            self.assertTrue((archive_dir / "p-peer-done.json").is_file())
            self.assertTrue((peer_done_input.parent / "ephemeral-project.json").is_file())
            for kept in (p_act, p_peer_act, p_peer_unv, p_user):
                self.assertTrue(kept.exists())

        parsed = parser().parse_args(["cleanup-agy-projects", "--apply", "--archive-dir", str(self.base / "bak")])
        self.assertEqual(parsed.command, "cleanup-agy-projects")
        self.assertTrue(parsed.apply)


if __name__ == "__main__":
    unittest.main()
