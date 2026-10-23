"""Native thread admission preflight without model turns or external processes."""
import collections
import copy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentic_runtime import capability_identity, environment_fingerprint, project_thread_probe
from core import BenchmarkError, digest, read, sha, write
from execution import instruction_fingerprint, prepare_profile
from runtime import Server


class ProbeServer:
    def __init__(self, root, protocol, instructions):
        self.root, self.protocol = root, protocol
        self.provider = "test"
        self.calls, self.archived = [], []
        self.counts = collections.Counter()
        self.active = False
        self.launch_overrides = {}
        self.fingerprint = {"binary_sha256": "offline-only", "execution": {
            "agent_settings": {}, "feature_overrides": {}, "models": [
                {"model": model, "multi_agent_version": "v2", "ultra_resolved_effort": "xhigh"}
                for model in protocol["models"]]}}
        profile = protocol["execution_profile"]
        self.response = {"thread": {"id": "probe-root", "status": {"type": "idle"}},
                         "activePermissionProfile": {"id": profile["permission_profile_id"]},
                         "approvalPolicy": "never", "cwd": profile["work_cwd"], "instructionSources": instructions}
        self.models = [{"id": model, "model": model, "multiAgentVersion": "v2",
                        "supportedReasoningEfforts": [{"reasoningEffort": effort} for effort in protocol["efforts"]],
                        "serviceTiers": [{"id": tier} for tier in protocol["service_tiers"]]}
                       for model in protocol["models"]]

    def rpc(self, method, params):
        self.calls.append((method, copy.deepcopy(params)))
        if method == "model/list":
            return {"data": self.models, "nextCursor": None}
        if method == "config/read":
            return {"config": {"features": {"multi_agent": True, "multi_agent_v2": True, "shell_tool": True}},
                    "layers": [], "origins": {}}
        if method == "configRequirements/read":
            return {"requirements": None}
        if method == "thread/start":
            return copy.deepcopy(self.response)
        if method == "thread/archive":
            self.archived.append(params["threadId"])
            return {}
        raise AssertionError("Preflight must not invoke inference or execution: " + method)


class ThreadProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="thread-probe-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.business = self.base / "business"
        self.business.mkdir()
        (self.business / "wall.cpp").write_text("void MoveWall();\n", encoding="utf-8")
        self.root = self.base / "run"
        self.root.mkdir()
        write(self.root / "input/instructions.json", {"developer": "Frozen developer instruction for this batch."})
        profile = prepare_profile({"source_roots": [{"path": str(self.business), "logical_path": "UE/Source"}],
                                   "model_work_root": str(self.base / "model-work")}, self.root)
        self.protocol = {"execution_profile": profile, "models": ["gpt-6.1-sol", "gpt-6-astra"],
                         "efforts": ["xhigh", "ultra"], "service_tiers": ["default", "priority"]}
        self.instruction = self.base / "AGENTS.md"
        self.instruction.write_text("PRIVATE-INSTRUCTION-BODY: never archive this body.\n", encoding="utf-8")
        self.server = ProbeServer(self.root, self.protocol, [str(self.instruction)])

    def assert_no_inference(self):
        self.assertFalse(self.server.active)
        self.assertFalse(any(method.startswith("turn/") or method == "command/exec" for method, _ in self.server.calls))

    def test_probe_seals_content_hash_and_archives_idle_thread_without_turn(self):
        result = project_thread_probe(self.server, self.protocol)
        self.assert_no_inference()
        self.assertEqual([method for method, _ in self.server.calls], ["thread/start", "thread/archive"])
        self.assertEqual(self.server.archived, ["probe-root"])
        params = self.server.calls[0][1]
        profile = self.protocol["execution_profile"]
        self.assertEqual(params["permissions"], profile["permission_profile_id"])
        self.assertEqual(params["cwd"], profile["work_cwd"])
        self.assertEqual(params["approvalPolicy"], "never")
        self.assertFalse(params["ephemeral"])
        self.assertTrue(params["experimentalRawEvents"])
        self.assertIsNone(params["baseInstructions"])
        self.assertEqual(params["developerInstructions"], read(self.root / "input/instructions.json")["developer"])
        self.assertNotIn("sandbox", params)
        expected = instruction_fingerprint([str(self.instruction)])
        self.assertEqual(read(self.root / "instruction-context.json"), {"sha256": digest(expected), "sources": expected})
        self.assertEqual(self.server.fingerprint["execution"]["instruction_sources_sha256"], digest(expected))
        self.assertEqual(result["inference_calls"], 0)
        self.assertEqual(result["instruction_sources_sha256"], digest(expected))
        self.assertNotIn("PRIVATE-INSTRUCTION-BODY", (self.root / "instruction-context.json").read_text(encoding="utf-8"))

    def test_frozen_profile_is_validated_before_admitting_any_thread(self):
        frozen = Path(self.protocol["execution_profile"]["source_root"]) / "UE/Source/wall.cpp"
        frozen.write_text("tampered frozen source", encoding="utf-8")
        with self.assertRaisesRegex(BenchmarkError, "Frozen source"):
            project_thread_probe(self.server, self.protocol)
        self.assertEqual(self.server.calls, [])
        self.assertFalse((self.root / "instruction-context.json").exists())

    def test_receipt_failure_archives_thread_and_never_seals_instructions(self):
        for key, value in (("activePermissionProfile", {"id": ":read-only"}), ("approvalPolicy", "on-request"),
                           ("cwd", str(self.base / "unexpected-cwd"))):
            with self.subTest(field=key):
                server = ProbeServer(self.root, self.protocol, [str(self.instruction)])
                server.response[key] = value
                with self.assertRaisesRegex(BenchmarkError, "permission receipt|approval policy|cwd|working directory"):
                    project_thread_probe(server, self.protocol)
                self.assertEqual(server.archived, ["probe-root"])
                self.assertNotIn("instruction_sources_sha256", server.fingerprint["execution"])
                self.assertFalse((self.root / "instruction-context.json").exists())
                self.assertEqual([method for method, _ in server.calls], ["thread/start", "thread/archive"])

    def test_same_instruction_path_content_mutation_fails_without_overwriting_seal(self):
        project_thread_probe(self.server, self.protocol)
        before = sha(self.root / "instruction-context.json")
        fingerprint = copy.deepcopy(self.server.fingerprint)
        self.instruction.write_text("Changed contents at exactly the same path.\n", encoding="utf-8")
        with self.assertRaisesRegex(BenchmarkError, "instruction content changed"):
            project_thread_probe(self.server, self.protocol)
        self.assertEqual(sha(self.root / "instruction-context.json"), before)
        self.assertEqual(self.server.fingerprint, fingerprint)
        self.assertEqual(self.server.archived, ["probe-root", "probe-root"])
        self.assert_no_inference()

    def test_instruction_order_and_duplicates_do_not_create_false_context_drift(self):
        other = self.base / "developer.md"
        other.write_text("Other instruction", encoding="utf-8")
        self.server.response["instructionSources"] = [str(other), str(self.instruction), str(other)]
        project_thread_probe(self.server, self.protocol)
        before = sha(self.root / "instruction-context.json")
        self.server.response["instructionSources"] = [str(self.instruction), str(other)]
        project_thread_probe(self.server, self.protocol)
        self.assertEqual(sha(self.root / "instruction-context.json"), before)
        self.assert_no_inference()

    def test_missing_or_relative_instruction_source_fails_closed_and_archives(self):
        for path in (str(self.base / "missing-AGENTS.md"), "relative-AGENTS.md"):
            with self.subTest(path=path):
                server = ProbeServer(self.root, self.protocol, [path])
                with self.assertRaises(BenchmarkError):
                    project_thread_probe(server, self.protocol)
                self.assertEqual(server.archived, ["probe-root"])
                self.assertFalse((self.root / "instruction-context.json").exists())

    def test_server_preflight_passes_completed_thread_probe_fingerprint_to_permission_proof(self):
        observed = []

        def permission(server, profile, fingerprint):
            self.assertEqual(server.archived, ["probe-root"])
            self.assertTrue((self.root / "instruction-context.json").is_file())
            self.assertEqual(fingerprint["execution"]["instruction_sources_sha256"], read(self.root / "instruction-context.json")["sha256"])
            observed.append(copy.deepcopy(fingerprint))
            return {"passed": True, "inference_calls": 0}

        with patch("execution.permission_probe", side_effect=permission) as proof:
            result = Server.preflight(self.server, self.protocol)
        self.assertEqual(proof.call_count, 1)
        self.assertEqual(result["fingerprint"], observed[0])
        self.assertEqual(result["thread_permission_probe"]["inference_calls"], 0)
        self.assertTrue(result["permission_probe"]["passed"])
        self.assertEqual(read(self.root / "preflight.json"), result)
        self.assert_no_inference()

    def test_repeated_server_preflight_content_drift_blocks_permission_proof(self):
        with patch("execution.permission_probe", return_value={"passed": True, "inference_calls": 0}) as proof:
            Server.preflight(self.server, self.protocol)
            before = sha(self.root / "instruction-context.json")
            self.instruction.write_text("Changed inherited instruction body.\n", encoding="utf-8")
            with self.assertRaisesRegex(BenchmarkError, "instruction content changed"):
                Server.preflight(self.server, self.protocol)
        self.assertEqual(proof.call_count, 1)
        self.assertEqual(self.server.archived, ["probe-root", "probe-root"])
        self.assertEqual(sha(self.root / "instruction-context.json"), before)
        self.assertNotIn("permission_probe", read(self.root / "preflight.json"))
        self.assert_no_inference()

    def test_unsupported_matrix_stops_before_thread_admission_or_permission_proof(self):
        self.server.models[0]["supportedReasoningEfforts"] = []
        with patch("execution.permission_probe") as proof:
            with self.assertRaisesRegex(BenchmarkError, "unsupported"):
                Server.preflight(self.server, self.protocol)
        proof.assert_not_called()
        self.assertEqual([method for method, _ in self.server.calls], ["model/list"])
        self.assertTrue(read(self.root / "preflight.json")["unsupported"])
        self.assertEqual(read(self.root / "preflight.json")["inference_calls"], 0)

    def test_effective_config_hash_changes_shared_capability_identity(self):
        protocol = copy.deepcopy(self.protocol)
        protocol["instructions_sha256"] = "same-developer-instruction"
        fingerprint = copy.deepcopy(self.server.fingerprint)
        fingerprint.update(version="offline", provider="test", endpoint_host=None, global_instructions_sha256=None)
        fingerprint["execution"].update(agent_roles_sha256={}, catalog_client_version="offline", native_defaults="native",
                                        inherited_config_sha256="sealed-config-bytes")
        before = capability_identity(protocol, fingerprint)
        fingerprint["execution"]["inherited_config_sha256"] = "changed-config-bytes"
        self.assertNotEqual(before, capability_identity(protocol, fingerprint))
        self.assertEqual(self.server.calls, [])

    def test_unselected_catalog_metadata_is_frozen_but_catalog_order_is_immaterial(self):
        protocol = copy.deepcopy(self.protocol)
        protocol["instructions_sha256"] = "same-developer-instruction"
        codex_home = self.base / "catalog-home"
        codex_home.mkdir()
        catalog = {"client_version": "offline", "models": [
            {"slug": model, "multi_agent_version": "v2", "supported_reasoning_levels": [{"effort": "xhigh"}],
             "context_window": 272000} for model in [*protocol["models"], "unselected-child-model"]]}
        runtime = {"binary_sha256": "offline", "version": "offline", "provider": "test", "endpoint_host": None,
                   "global_instructions_sha256": None}
        with patch("agentic_runtime.home", return_value=codex_home):
            write(codex_home / "models_cache.json", catalog)
            before = environment_fingerprint({}, protocol)
            before_id = capability_identity(protocol, {**runtime, "execution": before})
            catalog["models"][-1]["context_window"] = 500000
            write(codex_home / "models_cache.json", catalog)
            changed = environment_fingerprint({}, protocol)
            changed_id = capability_identity(protocol, {**runtime, "execution": changed})
            self.assertEqual(before["models"], changed["models"])
            self.assertNotEqual(before["model_catalog_sha256"], changed["model_catalog_sha256"])
            self.assertNotEqual(before_id, changed_id)
            catalog["models"].reverse()
            write(codex_home / "models_cache.json", catalog)
            reordered = environment_fingerprint({}, protocol)
        self.assertEqual(changed["model_catalog_sha256"], reordered["model_catalog_sha256"])
        self.assertEqual(changed_id, capability_identity(protocol, {**runtime, "execution": reordered}))
        self.assertEqual(self.server.calls, [])


if __name__ == "__main__":
    unittest.main()
