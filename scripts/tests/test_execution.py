"""Native project execution boundaries; inference is never used by these tests."""
import copy
import base64
import json
import os
from pathlib import Path
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import BenchmarkError, digest, read, sha, write
from execution import (check_profile, execution_mode, launch_overrides, permission_probe,
                       prepare_profile, require_permission_probe, reset_scratch, thread_permissions,
                       toml_value, instruction_fingerprint)


class ExecutionProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / "business"
        self.source.mkdir()
        (self.source / "physics.cpp").write_text("const int expected = 2;\n", encoding="utf-8")
        (self.source / "detail").mkdir()
        (self.source / "detail/wall.h").write_text("void move();\n", encoding="utf-8")
        self.run = self.base / "run"
        self.run.mkdir()

    def make(self, **options):
        spec = {"source_roots": [{"path": str(self.source), "logical_path": "UE/Source"}],
                "model_work_root": str(self.base / "model-work")}
        spec.update(options)
        return prepare_profile(spec, self.run)

    def test_snapshot_is_copy_and_frozen_with_provenance(self):
        profile = self.make()
        copied = Path(profile["source_root"]) / "UE/Source/physics.cpp"
        self.assertEqual(copied.read_bytes(), (self.source / "physics.cpp").read_bytes())
        self.assertNotEqual(copied.stat().st_ino, (self.source / "physics.cpp").stat().st_ino)
        manifest = read(profile["manifest_path"])
        self.assertEqual(manifest["files"]["UE/Source/physics.cpp"]["source_path"], str(self.source / "physics.cpp"))
        (self.source / "physics.cpp").write_text("new live source", encoding="utf-8")
        check_profile(profile)  # The model sees the sealed copy, not future edits.
        copied.write_text("changed snapshot", encoding="utf-8")
        with self.assertRaisesRegex(BenchmarkError, "Frozen source"):
            check_profile(profile)

    def test_extra_file_and_profile_tampering_rejected(self):
        profile = self.make()
        altered = copy.deepcopy(profile)
        altered["scratch_root"] = str(self.base)
        with self.assertRaisesRegex(BenchmarkError, "profile changed"):
            check_profile(altered)
        (Path(profile["source_root"]) / "gold.json").write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(BenchmarkError, "file set"):
            check_profile(profile)

    def test_scratch_changes_do_not_invalidate_source_fingerprint(self):
        profile = self.make()
        (Path(profile["scratch_root"]) / "calculation.txt").write_text("3", encoding="utf-8")
        self.assertEqual(check_profile(profile), profile)

    def test_source_root_and_private_work_overlap_rejected(self):
        for path in [self.run, self.source, self.base]:
            with self.subTest(path=path), self.assertRaises(BenchmarkError):
                self.make(model_work_root=str(path))
        with self.assertRaisesRegex(BenchmarkError, "private"):
            self.make(source_roots=[{"path": str(self.base), "logical_path": "all"}])

    def test_logical_paths_cannot_escape_or_overlap(self):
        for logical in ["../private", "/root", "C:/root", "bad\\name"]:
            with self.subTest(logical=logical), self.assertRaises(BenchmarkError):
                self.make(source_roots=[{"path": str(self.source), "logical_path": logical}])
        with self.assertRaisesRegex(BenchmarkError, "overlap"):
            self.make(source_roots=[{"path": str(self.source), "logical_path": "UE"}, {"path": str(self.source), "logical_path": "UE/sub"}])

    def test_runtime_root_cannot_expose_private_run(self):
        with self.assertRaisesRegex(BenchmarkError, "narrow"):
            self.make(runtime_read_roots=[str(self.base)])

    def test_runtime_root_cannot_be_a_user_or_platform_container(self):
        container = self.base / "platform-container"
        container.mkdir()
        (container / "runtime.exe").write_bytes(b"test-binary")
        with patch.dict(os.environ, {"PROGRAMDATA": str(container)}), self.assertRaisesRegex(BenchmarkError, "narrow"):
            self.make(runtime_read_roots=[str(container)])

    def test_runtime_root_cannot_reexpose_live_source(self):
        for path in [self.source, self.source / "physics.cpp"]:
            with self.subTest(path=path), self.assertRaisesRegex(BenchmarkError, "original source"):
                self.make(runtime_read_roots=[str(path)])

    def test_legacy_mode_is_explicit_and_backward_compatible(self):
        self.assertEqual(execution_mode({}), "fixed-evidence")
        self.assertEqual(execution_mode({"execution_profile": {"mode": "project-readonly"}}), "project-readonly")

    def test_instruction_fingerprint_detects_same_path_content_changes(self):
        path = self.base / "AGENTS.md"
        path.write_text("Frozen instruction one.\n", encoding="utf-8")
        initial = instruction_fingerprint([str(path), str(path.parent / "." / path.name)])
        self.assertEqual(initial, [{"path": str(path.resolve()), "sha256": sha(path)}])
        self.assertNotIn("Frozen instruction", str(initial))
        path.write_text("Changed instruction two.\n", encoding="utf-8")
        self.assertNotEqual(instruction_fingerprint([str(path)]), initial)

    def test_instruction_fingerprint_requires_existing_absolute_files(self):
        for paths in [["relative/AGENTS.md"], [str(self.base / "missing")], [str(self.base)], None, [4]]:
            with self.subTest(paths=paths), self.assertRaises(BenchmarkError):
                instruction_fingerprint(paths)

    def test_native_profile_has_no_full_read_and_no_legacy_sandbox(self):
        profile = self.make()
        overrides = launch_overrides(profile)
        self.assertEqual(overrides["windows.sandbox"], "mxc")
        permission = overrides["permissions"][profile["permission_profile_id"]]
        self.assertNotIn(":root", permission["filesystem"])
        self.assertEqual(permission["filesystem"][":minimal"], "read")
        self.assertEqual(permission["filesystem"][profile["source_root"]], "read")
        self.assertEqual(permission["filesystem"][profile["scratch_root"]], "write")
        self.assertFalse(permission["network"]["enabled"])
        self.assertEqual(thread_permissions(profile), {"permissions": profile["permission_profile_id"]})
        # Real CLI parser consumes TOML, not a JSON object with colons.
        parsed = tomllib.loads("permissions=" + toml_value(overrides["permissions"]))
        self.assertEqual(parsed["permissions"], overrides["permissions"])

    def test_comparison_hash_ignores_workspace_locations(self):
        first = self.make()
        other = self.base / "other-run"
        other.mkdir()
        second = prepare_profile({"source_roots": [{"path": str(self.source), "logical_path": "UE/Source"}], "model_work_root": str(self.base / "other-work")}, other)
        self.assertNotEqual(first["profile_sha256"], second["profile_sha256"])
        self.assertEqual(first["comparison_fingerprint"], second["comparison_fingerprint"])

    def test_archival_copy_can_be_checked_without_model_workspace(self):
        profile = self.make()
        import shutil
        exported = self.base / "export"
        shutil.copytree(self.run, exported)
        shutil.rmtree(profile["model_work_root"])
        check_profile(profile, runtime=False, runroot=exported)
        with self.assertRaises((BenchmarkError, FileNotFoundError)):
            check_profile(profile, runroot=exported)

    def test_runtime_file_changes_block_execution_but_not_offline_review(self):
        runtime = self.base / "runtime"
        runtime.mkdir()
        (runtime / "calculator.exe").write_bytes(b"test-binary")
        profile = self.make(runtime_read_roots=[str(runtime)])
        (runtime / "calculator.exe").write_bytes(b"updated-binary")
        check_profile(profile, runtime=False)
        with self.assertRaisesRegex(BenchmarkError, "runtime executable"):
            check_profile(profile)

    def test_scratch_archived_then_cleared_without_exposing_prior_answers(self):
        profile = self.make()
        scratch = Path(profile["scratch_root"])
        (scratch / "nested").mkdir()
        (scratch / "nested/result.txt").write_text("independent probe", encoding="utf-8")
        archive = self.run / "scratch-archive"
        result = reset_scratch(profile, archive)
        self.assertEqual((archive / "nested/result.txt").read_text(encoding="utf-8"), "independent probe")
        self.assertEqual(result["files_sha256"], read(archive / "scratch-manifest.json")["files_sha256"])
        self.assertEqual(list(scratch.iterdir()), [])
        self.assertTrue((self.source / "physics.cpp").exists())

    def test_scratch_auth_material_is_never_archived_or_deleted(self):
        profile = self.make()
        scratch = Path(profile["scratch_root"])
        secret_file = scratch / "credential.txt"
        secret_file.write_text("Bearer " + "x" * 30, encoding="utf-16")
        with self.assertRaisesRegex(BenchmarkError, "authentication"):
            reset_scratch(profile, self.run / "archive")
        self.assertTrue(secret_file.exists())
        self.assertFalse((self.run / "archive").exists())

    def test_hardlinks_rejected(self):
        try:
            os.link(self.source / "physics.cpp", self.source / "alias.cpp")
        except OSError:
            self.skipTest("Host does not support hardlinks")
        with self.assertRaisesRegex(BenchmarkError, "hardlink"):
            self.make()

    def test_symlink_rejected(self):
        try:
            (self.source / "alias.cpp").symlink_to(self.source / "physics.cpp")
        except OSError:
            self.skipTest("Host does not allow symlinks")
        with self.assertRaisesRegex(BenchmarkError, "links or reparse"):
            self.make()

    def test_missing_permission_proof_blocks_sampling(self):
        profile = self.make()
        with self.assertRaisesRegex(BenchmarkError, "no-model"):
            require_permission_probe(profile, {"binary_sha256": "frozen"})

    def test_gate_is_bound_to_fingerprint_and_report_bytes(self):
        profile = self.make()
        fingerprint = {"binary_sha256": "frozen", "version": "1"}
        report = {"passed": True, "inference_calls": 0, "backend": "mxc", "fingerprint": fingerprint}
        write(self.run / "permission-probe.json", report)
        write(self.run / "permission-probe-seal.json", {"report_sha256": sha(self.run / "permission-probe.json"), "profile_sha256": profile["profile_sha256"], "fingerprint_sha256": digest(fingerprint)})
        require_permission_probe(profile, fingerprint)
        with self.assertRaisesRegex(BenchmarkError, "does not match"):
            require_permission_probe(profile, {"binary_sha256": "changed"})
        report["passed"] = False
        write(self.run / "permission-probe.json", report)
        with self.assertRaises(BenchmarkError):
            require_permission_probe(profile, fingerprint)

    def test_failed_native_probe_cannot_make_gate(self):
        profile = self.make()
        class RejectingServer:
            def rpc(self, method, params):
                raise BenchmarkError("unsupported native backend")
        # On Windows the real RPC fails; elsewhere platform gating fails earlier.
        with self.assertRaises(BenchmarkError):
            permission_probe(RejectingServer(), profile, {"binary_sha256": "frozen"})
        report = read(self.run / "permission-probe.json")
        self.assertFalse(report["passed"])
        self.assertEqual(report["inference_calls"], 0)
        self.assertFalse((self.run / "permission-probe-seal.json").exists())

    @unittest.skipUnless(os.name == "nt", "Native probe command construction is Windows-specific")
    def test_probe_uses_sandboxed_command_api_and_exact_check_set(self):
        profile = self.make()
        calls = []
        class MockServer:
            def rpc(self, method, params):
                calls.append((method, params))
                script = base64.b64decode(params["command"][-1]).decode("utf-16-le")
                checks = {"read_control": True, "source_read": True, "scratch_write": True,
                          "source_write_denied": True,
                          "forbidden_reads_denied": [True] * script.count("@{path=")}
                return {"exitCode": 0, "stdout": json.dumps(checks)}
        report = permission_probe(MockServer(), profile, {"binary_sha256": "mocked"})
        self.assertTrue(report["passed"])
        self.assertEqual(report["inference_calls"], 0)
        self.assertTrue(calls)
        for method, params in calls:
            self.assertEqual(method, "command/exec")
            self.assertEqual(params["permissionProfile"], profile["permission_profile_id"])
            self.assertNotIn("sandboxPolicy", params)
        require_permission_probe(profile, {"binary_sha256": "mocked"})
        self.assertFalse((Path(profile["scratch_root"]) / "permission-junction-control").exists())

    @unittest.skipUnless(os.name == "nt", "Native probe command construction is Windows-specific")
    def test_native_permission_check_failure_is_preserved(self):
        profile = self.make()
        class MockServer:
            def rpc(self, method, params):
                script = base64.b64decode(params["command"][-1]).decode("utf-16-le")
                return {"exitCode": 0, "stdout": json.dumps({"read_control": True, "source_read": True,
                    "scratch_write": True, "source_write_denied": False,
                    "forbidden_reads_denied": [True] * script.count("@{path=")})}
        with self.assertRaisesRegex(BenchmarkError, "restrictions failed"):
            permission_probe(MockServer(), profile, {"binary_sha256": "mocked"})
        report = read(self.run / "permission-probe.json")
        self.assertFalse(report["passed"])
        self.assertFalse(report["shells"][0]["checks"]["source_write_denied"])
        self.assertFalse((self.run / "permission-probe-seal.json").exists())


if __name__ == "__main__":
    unittest.main()
