"""Shared native capability receipts: offline only, never starts an app server."""
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agentic_runtime as native
from core import BenchmarkError, digest, read, sha, write


class CapabilitySharingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="capability-share-")
        self.base = Path(self.temp.name).resolve()
        self.protocols, self.fingerprints = {}, {}
        self.source = self.batch("source")
        self.target = self.batch("target")
        self.inputs = patch.object(native, "check_inputs", side_effect=lambda root: self.protocols[Path(root).resolve()])
        self.inputs.start()
        # Exercise the real permission seal checker, with source snapshot validation
        # outside this unit's scope. No permission RPC is issued during adoption.
        self.profile_check = patch("execution.check_profile")
        self.profile_check.start()
        self.seal_source()

    def tearDown(self):
        self.profile_check.stop()
        self.inputs.stop()
        self.temp.cleanup()

    def batch(self, name):
        root = self.base / name
        root.mkdir()
        protocol = {"models": ["gpt-6.1-sol", "gpt-6-astra"], "efforts": ["xhigh", "ultra"],
                    "service_tiers": ["default", "priority"], "instructions_sha256": "frozen-instructions",
                    "execution_profile": {"mode": "project-readonly", "run_root": str(root),
                                          "profile_sha256": "profile-" + name,
                                          "comparison_fingerprint": "same-source-and-permission-policy"}}
        fingerprint = {"binary_sha256": "same-binary", "version": "codex-test", "provider": "openai", "endpoint_host": None,
                       "global_instructions_sha256": "same-global-instructions", "execution": {
                           "profile_sha256": "profile-" + name, "launch_overrides_sha256": "paths-" + name,
                           "agent_settings": {"enabled": True, "max_threads": 6}, "agent_roles_sha256": {"worker": "role-hash"},
                           "feature_overrides": {"multi_agent": True, "multi_agent_v2": True},
                           "models": [{"model": m, "multi_agent_version": "v2", "ultra_resolved_effort": "xhigh", "model_metadata_sha256": m + "-metadata"} for m in protocol["models"]],
                           "catalog_client_version": "test-version", "native_defaults": "unchanged native defaults"}}
        self.protocols[root], self.fingerprints[root] = protocol, fingerprint
        self.preflight(root)
        return root

    def preflight(self, root):
        fingerprint, protocol = self.fingerprints[root], self.protocols[root]
        write(root / "preflight.json", {"fingerprint": fingerprint, "unsupported": [], "inference_calls": 0})
        report = root / "permission-probe.json"
        write(report, {"passed": True, "inference_calls": 0, "backend": "mxc", "fingerprint": fingerprint})
        write(root / "permission-probe-seal.json", {"report_sha256": sha(report),
              "profile_sha256": protocol["execution_profile"]["profile_sha256"], "fingerprint_sha256": digest(fingerprint)})

    def attempt(self, root, item):
        directory = root / "capability/runs" / item["id"] / "attempt-001"
        directory.mkdir(parents=True)
        (directory / "answer.txt").write_text("原生子代理与主代理读取结果一致。", encoding="utf-8")
        (directory / "events.jsonl").write_text('{"method":"ledger/discoveryComplete"}\n', encoding="utf-8")
        write(directory / "task-tree.json", {"accounting_complete": True})
        write(directory / "scratch/manifest.json", {"files": {}})
        result = {**item, "slot_id": item["id"], "attempt": 1, "purpose": "capability", "valid": True,
                  "accounting_complete": True, "delegation_count": 1, "status": "completed",
                  "fingerprint": self.fingerprints[root], "tree_estimated_api_usd": .01}
        write(directory / "result.json", result)
        return result

    def seal_source(self):
        root = self.source
        protocol, fp = self.protocols[root], self.fingerprints[root]
        for model in protocol["models"]:
            for tier in protocol["service_tiers"]:
                count = len(list((root / "capability/runs").glob("*"))) if (root / "capability/runs").exists() else 0
                self.attempt(root, {"id": f"{count+1:04d}", "model": model, "service_tier": tier, "effort": "ultra"})
        files = {p.relative_to(root / "capability").as_posix(): sha(p) for p in (root / "capability").rglob("*") if p.is_file()}
        identity, origin = native.capability_identity(protocol, fp), digest(fp)
        write(root / "capability/seal.json", {"passed": True, "fingerprint": fp, "compatibility_id": identity,
              "capability_id": native._capability_id(identity, origin, files), "origin_fingerprint_sha256": origin,
              "files_sha256": files, "attempts": 4, "verified_attempts": 4, "formal_samples": 0, "paid_here": True})

    def test_compatible_workpaths_share_without_any_server_or_new_attempt(self):
        before = {str(p): sha(p) for p in self.source.rglob("*") if p.is_file()}
        with patch("runtime.Server", side_effect=AssertionError("adoption must not start RPC")):
            result = native.adopt_capability(self.target, self.source)
        original = read(self.source / "capability/seal.json")
        adopted = native.require_capability(self.target, self.fingerprints[self.target])
        self.assertNotEqual(original["fingerprint"], adopted["fingerprint"])
        self.assertEqual(original["compatibility_id"], adopted["compatibility_id"])
        self.assertEqual(original["capability_id"], adopted["capability_id"])
        self.assertEqual((result["inference_calls"], result["capability_attempts"], adopted["attempts"]), (0, 0, 0))
        self.assertFalse(adopted["paid_here"])
        self.assertEqual(adopted["shared_attempts"], 4)
        self.assertEqual(adopted["shared_from"]["seal_sha256"], sha(self.source / "capability/seal.json"))
        self.assertEqual(before, {str(p): sha(p) for p in self.source.rglob("*") if p.is_file()})
        for name, expected in original["files_sha256"].items():
            self.assertEqual(sha(self.target / "capability" / name), expected)

    def test_exact_own_fingerprint_is_required_even_when_compatible(self):
        native.adopt_capability(self.target, self.source)
        with self.assertRaisesRegex(BenchmarkError, "different runtime"):
            native.require_capability(self.target, self.fingerprints[self.source])
        with self.assertRaisesRegex(BenchmarkError, "different runtime"):
            native.require_capability(self.source, self.fingerprints[self.target])

    def test_missing_runtime_identity_does_not_infer_compatibility(self):
        with self.assertRaisesRegex(BenchmarkError, "identity is missing"):
            native.capability_identity(self.protocols[self.target], None)
        fp = self.fingerprints[self.target]
        fp["execution"].pop("agent_roles_sha256")
        self.preflight(self.target)
        with self.assertRaisesRegex(BenchmarkError, "identity is incomplete"):
            native.adopt_capability(self.target, self.source)
        self.assertFalse((self.target / "capability").exists())

    def test_effective_instruction_chain_hash_is_stable_when_present(self):
        self.source = self.batch("sealed-instruction-source")
        self.fingerprints[self.source]["execution"]["instruction_sources_sha256"] = "same-effective-chain"
        self.preflight(self.source)
        self.seal_source()
        self.fingerprints[self.target]["execution"]["instruction_sources_sha256"] = "same-effective-chain"
        self.preflight(self.target)
        native.adopt_capability(self.target, self.source)
        changed = self.batch("changed-instruction-chain")
        self.fingerprints[changed]["execution"]["instruction_sources_sha256"] = "different-effective-chain"
        self.preflight(changed)
        with self.assertRaisesRegex(BenchmarkError, "cannot be shared"):
            native.adopt_capability(changed, self.source)
        self.assertFalse((changed / "capability").exists())

    def test_reject_source_instruction_matrix_and_native_configuration_differences(self):
        changes = [lambda p, f: p["execution_profile"].update(comparison_fingerprint="other-source"),
                   lambda p, f: p.update(instructions_sha256="other-instructions"),
                   lambda p, f: p.update(models=["gpt-6.1-sol"]),
                   lambda p, f: p.update(service_tiers=["default"]),
                   lambda p, f: f["execution"]["agent_settings"].update(max_threads=8),
                   lambda p, f: f["execution"]["models"][0].update(ultra_resolved_effort="max"),
                   lambda p, f: f["execution"].update(agent_roles_sha256={"worker": "changed"}),
                   lambda p, f: f["execution"]["feature_overrides"].update(multi_agent=False),
                   lambda p, f: f.update(binary_sha256="other-binary"),
                   lambda p, f: f.update(global_instructions_sha256="other-global")]
        for index, change in enumerate(changes):
            with self.subTest(change=index):
                target = self.batch("different-" + str(index))
                change(self.protocols[target], self.fingerprints[target])
                self.preflight(target)
                with self.assertRaisesRegex(BenchmarkError, "cannot be shared"):
                    native.adopt_capability(target, self.source)
                self.assertFalse((target / "capability").exists())

    def test_target_requires_passed_current_non_inference_permission_proof(self):
        for index, kind in enumerate(("missing-preflight", "unsupported", "missing-permission", "stale-permission", "failed-permission")):
            with self.subTest(kind=kind):
                target = self.batch("unproved-" + str(index))
                if kind == "missing-preflight":
                    (target / "preflight.json").unlink()
                elif kind == "unsupported":
                    report = read(target / "preflight.json")
                    report["unsupported"] = ["ultra"]
                    write(target / "preflight.json", report)
                elif kind == "missing-permission":
                    (target / "permission-probe-seal.json").unlink()
                elif kind == "stale-permission":
                    self.fingerprints[target]["execution"]["launch_overrides_sha256"] = "changed-paths"
                    report = read(target / "preflight.json")
                    report["fingerprint"] = self.fingerprints[target]
                    write(target / "preflight.json", report)
                else:
                    report = read(target / "permission-probe.json")
                    report["passed"] = False
                    write(target / "permission-probe.json", report)
                    seal = read(target / "permission-probe-seal.json")
                    seal["report_sha256"] = sha(target / "permission-probe.json")
                    write(target / "permission-probe-seal.json", seal)
                with self.assertRaises(BenchmarkError):
                    native.adopt_capability(target, self.source)
                self.assertFalse((target / "capability").exists())

    def test_tampered_source_or_copy_is_rejected(self):
        native.adopt_capability(self.target, self.source)
        answer = self.target / "capability/runs/0001/attempt-001/answer.txt"
        answer.write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(BenchmarkError, "artifacts changed"):
            native.require_capability(self.target, self.fingerprints[self.target])
        original = self.source / "capability/runs/0001/attempt-001/result.json"
        original.write_text("{}", encoding="utf-8")
        other = self.batch("source-tamper-target")
        with self.assertRaisesRegex(BenchmarkError, "artifacts changed"):
            native.adopt_capability(other, self.source)
        self.assertFalse((other / "capability").exists())

    def test_extra_unsealed_files_and_forged_identity_are_rejected(self):
        native.adopt_capability(self.target, self.source)
        (self.target / "capability/unsealed.txt").write_text("extra", encoding="utf-8")
        with self.assertRaisesRegex(BenchmarkError, "collection differs"):
            native.require_capability(self.target, self.fingerprints[self.target])
        (self.target / "capability/unsealed.txt").unlink()
        seal = read(self.target / "capability/seal.json")
        seal["capability_id"] = "invented"
        write(self.target / "capability/seal.json", seal)
        with self.assertRaisesRegex(BenchmarkError, "shared identity"):
            native.require_capability(self.target, self.fingerprints[self.target])

    def test_allowlist_blocks_extra_paths_even_if_manifest_is_resealed(self):
        path = self.source / "capability/private-config.json"
        write(path, {"irrelevant": "not a receipt"})
        seal = read(self.source / "capability/seal.json")
        seal["files_sha256"]["private-config.json"] = sha(path)
        seal["capability_id"] = native._capability_id(seal["compatibility_id"], seal["origin_fingerprint_sha256"], seal["files_sha256"])
        write(self.source / "capability/seal.json", seal)
        with self.assertRaisesRegex(BenchmarkError, "allowlist"):
            native.adopt_capability(self.target, self.source)
        self.assertFalse((self.target / "capability").exists())

    def test_existing_target_is_preserved_and_shared_chain_retains_charge_identity(self):
        native.adopt_capability(self.target, self.source)
        with self.assertRaisesRegex(BenchmarkError, "already exists"):
            native.adopt_capability(self.target, self.source)
        next_target = self.batch("third-case")
        native.adopt_capability(next_target, self.target)
        seal = native.require_capability(next_target, self.fingerprints[next_target])
        self.assertEqual(seal["capability_id"], read(self.source / "capability/seal.json")["capability_id"])
        self.assertEqual(seal["attempts"], 0)
        self.assertFalse(seal["paid_here"])

    def test_four_smokes_then_three_adoptions_issue_only_four_paid_attempts(self):
        source = self.batch("fresh-smoke")
        fake = SimpleNamespace(fingerprint=self.fingerprints[source], active=False, close=lambda: None,
                               preflight=lambda protocol: read(source / "preflight.json"))
        calls = []

        def trial(server, item, attempt, **kwargs):
            self.assertTrue(kwargs["capability"])
            self.assertEqual(attempt, 1)
            calls.append((item["model"], item["service_tier"]))
            return self.attempt(source, item)

        with patch("runtime.Server", return_value=fake) as server, patch("runtime.recover_inflight"), patch.object(native, "run_trial", side_effect=trial):
            result = native.capability_smoke(source)
            for index in range(3):
                native.adopt_capability(self.batch("case-" + str(index)), source)
        self.assertEqual(server.call_count, 1)
        self.assertEqual(len(calls), 4)
        self.assertEqual(len(set(calls)), 4)
        self.assertEqual(result["capability_attempts"], 4)
        self.assertTrue(read(source / "capability/seal.json")["paid_here"])


if __name__ == "__main__":
    unittest.main()
