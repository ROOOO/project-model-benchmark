"""Read-only effective config seals, without model calls or config body exports."""
import collections
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import BenchmarkError, digest, read
from reporting import execution_identity
from runtime import Server, check_effective_native_configuration, effective_configuration_snapshot


class EffectiveConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="effective-config-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.protocol = self.protocol_for("first")
        self.configuration, self.requirements = self.responses(self.protocol)

    def protocol_for(self, name):
        work = self.base / name
        return {"models": ["gpt-6.1-sol"], "efforts": ["ultra"], "service_tiers": ["default"],
                "instructions_sha256": "same-instructions", "execution_profile": {
                    "mode": "project-readonly", "comparison_fingerprint": "a" * 64,
                    "permission_profile_id": "permission-" + name,
                    "model_work_root": str(work), "source_root": str(work / "source"), "work_cwd": str(work / "source"),
                    "scratch_root": str(work / "scratch"), "probe_root": str(work / "probe"), "run_root": str(self.base / (name + "-run"))}}

    def responses(self, protocol):
        profile = protocol["execution_profile"]
        config = {"agents": {"max_depth": 3, "max_threads": 8, "default_subagent_reasoning_effort": "xhigh",
                             "default_subagent_model": "gpt-6.1-sol", "authorization": "not-an-exported-value"},
                  "features": {"multi_agent": True, "multi_agent_v2": {"enabled": True,
                      "max_concurrent_threads_per_session": 5, "private": "do-not-export"}, "shell_tool": True},
                  "default_permissions": profile["permission_profile_id"], "web_search": "disabled",
                  "permissions": {profile["permission_profile_id"]: {"filesystem": {
                      ":minimal": "read", profile["source_root"]: "read", profile["scratch_root"]: "write"},
                      "network": {"enabled": False}}},
                  "provider_credential": "secret-value-must-remain-in-memory"}
        layer = {"name": {"type": "sessionFlags"}, "version": digest(config), "config": copy.deepcopy(config)}
        return {"config": config, "layers": [layer], "origins": {}}, {"requirements": None, "supportsIndependentSpeedModes": True}

    def snapshot(self, config=None, requirements=None, protocol=None):
        return effective_configuration_snapshot(config or self.configuration,
            requirements if requirements is not None else self.requirements, protocol or self.protocol)

    def test_effective_controls_are_typed_and_secrets_never_exported(self):
        result = self.snapshot()
        text = json.dumps(result)
        for secret in ("not-an-exported-value", "do-not-export", "secret-value-must-remain-in-memory"):
            self.assertNotIn(secret, text)
        settings = result["effective_native_settings"]
        self.assertEqual(settings["agents"]["max_threads"], 8)
        self.assertEqual(settings["features"]["multi_agent_v2"]["max_concurrent_threads_per_session"], 5)
        self.assertEqual(result["effective_config_sha256"], digest(self.configuration["config"]))
        self.assertEqual(result["effective_layers_sha256"], digest(self.configuration["layers"]))
        self.assertEqual(result["effective_requirements_sha256"], digest(self.requirements))

    def test_equivalent_workspaces_and_generated_permission_ids_share_semantics(self):
        second = self.protocol_for("second")
        config, requirements = self.responses(second)
        before, after = self.snapshot(), self.snapshot(config, requirements, second)
        self.assertNotEqual(before["effective_config_sha256"], after["effective_config_sha256"])
        self.assertNotEqual(before["effective_layers_sha256"], after["effective_layers_sha256"])
        self.assertEqual(before["effective_configuration_fingerprint"], after["effective_configuration_fingerprint"])
        first_fp = {"execution": before}
        second_fp = {"execution": after}
        self.assertEqual(execution_identity(self.protocol, first_fp), execution_identity(second, second_fp))

    def test_real_permissions_concurrency_requirements_and_other_paths_remain_semantic(self):
        initial = self.snapshot()["effective_configuration_fingerprint"]
        changes = [lambda config: config["agents"].update(max_threads=9),
                   lambda config: config["features"]["multi_agent_v2"].update(max_concurrent_threads_per_session=7),
                   lambda config: config["permissions"][self.protocol["execution_profile"]["permission_profile_id"]]["filesystem"].update({":minimal": "write"}),
                   lambda config: config.update(unrelated_file=str(self.base / "external-a"))]
        for mutate in changes:
            changed = copy.deepcopy(self.configuration)
            mutate(changed["config"])
            self.assertNotEqual(initial, self.snapshot(changed)["effective_configuration_fingerprint"])
        requirements = {"requirements": {"allowed_permission_profiles": {"other": True}}}
        self.assertNotEqual(initial, self.snapshot(requirements=requirements)["effective_configuration_fingerprint"])

    def test_path_normalization_preserves_sibling_and_non_path_meaning(self):
        first, second = self.protocol_for("first"), self.protocol_for("second")
        config_a, requirements = self.responses(first)
        config_b, _ = self.responses(second)
        # These are sibling paths, not children of the benchmark work roots.
        config_a["config"]["literal"] = first["execution_profile"]["model_work_root"] + "-unrelated"
        config_b["config"]["literal"] = second["execution_profile"]["model_work_root"] + "-unrelated"
        self.assertNotEqual(self.snapshot(config_a)["effective_configuration_fingerprint"],
                            self.snapshot(config_b, requirements, second)["effective_configuration_fingerprint"])

    def test_layer_order_and_disabled_content_are_not_dropped(self):
        initial = self.snapshot()["effective_configuration_fingerprint"]
        changed = copy.deepcopy(self.configuration)
        changed["layers"].append({"name": {"type": "system", "file": str(self.base / "system.toml")},
                                   "version": "different", "config": {"agents": {"max_threads": 2}}, "disabledReason": "untrusted"})
        forward = self.snapshot(changed)["effective_configuration_fingerprint"]
        self.assertNotEqual(initial, forward)
        changed["layers"].reverse()
        self.assertNotEqual(forward, self.snapshot(changed)["effective_configuration_fingerprint"])

    def test_role_and_referenced_instruction_content_are_hashed_only(self):
        instructions = self.base / "instructions.txt"
        instructions.write_text("PRIVATE-INSTRUCTION-CONTENT", encoding="utf-8")
        role = self.base / "role.toml"
        role.write_text('model_instructions_file="instructions.txt"\n', encoding="utf-8")
        config = copy.deepcopy(self.configuration)
        config["config"]["agents"]["reviewer"] = {"config_file": str(role)}
        result = self.snapshot(config)
        self.assertEqual(result["effective_source_file_count"], 2)
        self.assertNotIn("PRIVATE-INSTRUCTION-CONTENT", json.dumps(result))
        instructions.write_text("CHANGED-INSTRUCTION-CONTENT", encoding="utf-8")
        self.assertNotEqual(result["effective_configuration_fingerprint"], self.snapshot(config)["effective_configuration_fingerprint"])

    def test_missing_sources_and_incomplete_rpc_shapes_fail_closed(self):
        for bad in ({}, {"config": {}, "layers": None}, {"config": {}, "layers": [None]}):
            with self.subTest(response=bad), self.assertRaises(BenchmarkError):
                effective_configuration_snapshot(bad, self.requirements, self.protocol)
        with self.assertRaises(BenchmarkError):
            self.snapshot(requirements={})
        changed = copy.deepcopy(self.configuration)
        changed["config"]["agents"]["reviewer"] = {"config_file": str(self.base / "missing.toml")}
        with self.assertRaisesRegex(BenchmarkError, "source unavailable"):
            self.snapshot(changed)

    def test_native_preflight_reads_layers_and_requirements_without_turns(self):
        root = self.base / "preflight"
        root.mkdir()
        server = Server.__new__(Server)
        server.root, server.fingerprint = root, {"execution": {}}
        server.counts = collections.Counter()
        calls = []

        def rpc(method, params):
            calls.append((method, params))
            if method == "model/list":
                return {"data": [{"model": self.protocol["models"][0], "supportedReasoningEfforts": [{"reasoningEffort": "ultra"}]}]}
            if method == "config/read":
                self.assertEqual(params, {"includeLayers": True, "cwd": self.protocol["execution_profile"]["work_cwd"]})
                return copy.deepcopy(self.configuration)
            if method == "configRequirements/read":
                self.assertEqual(params, {})
                return copy.deepcopy(self.requirements)
            raise AssertionError("Unexpected RPC " + method)

        server.rpc = rpc
        with patch("agentic_runtime.check_native_configuration"), patch("agentic_runtime.project_thread_probe", return_value={"inference_calls": 0}), patch("execution.permission_probe", return_value={"passed": True}):
            result = server.preflight(self.protocol)
        self.assertEqual([method for method, _ in calls], ["model/list", "config/read", "configRequirements/read"])
        self.assertEqual(result["inference_calls"], 0)
        saved = read(root / "preflight.json")
        self.assertIn("effective_configuration_fingerprint", saved["fingerprint"]["execution"])
        self.assertNotIn("secret-value-must-remain-in-memory", json.dumps(saved))

    def test_explicit_native_tool_and_capacity_disables_block_before_paid_smoke(self):
        variants = [{"agents": {"enabled": False}}, {"agents": {"max_threads": 0}}, {"agents": {"max_depth": 0}},
                    {"features": {"shell_tool": False}}, {"features": {"exec_tool": False}},
                    {"features": {"multi_agent_v2": False}}, {"features": {"multi_agent_v2": {"enabled": False}}},
                    {"features": {"multi_agent_v2": {"disable_native_tools": True}}},
                    {"features": {"multi_agent_v2": {"max_concurrent_threads_per_session": 1}}}]
        for override in variants:
            with self.subTest(override=override):
                changed = copy.deepcopy(self.configuration)
                changed["config"].update(override)
                with self.assertRaises(BenchmarkError):
                    check_effective_native_configuration(self.snapshot(changed), [{"multiAgentVersion": "v2"}])
        v1_disabled = copy.deepcopy(self.configuration)
        v1_disabled["config"]["features"]["multi_agent"] = False
        with self.assertRaisesRegex(BenchmarkError, "V1 delegation"):
            check_effective_native_configuration(self.snapshot(v1_disabled), [{"multiAgentVersion": "v1"}])
        # Disabling an unused V1 backend does not disable a V2 model.
        check_effective_native_configuration(self.snapshot(v1_disabled), [{"multiAgentVersion": "v2"}])
        check_effective_native_configuration(self.snapshot({"config": {}, "layers": []}), [{"multiAgentVersion": "v2"}])

    def test_null_optional_native_tables_keep_defaults_without_hiding_bad_types(self):
        result = self.snapshot({"config": {"agents": None, "features": None}, "layers": []})
        check_effective_native_configuration(result, [{"multiAgentVersion": "v2"}])
        self.assertEqual(result["effective_native_settings"]["agents"], {})
        for value in (False, 2, "disabled"):
            with self.subTest(value=value), self.assertRaisesRegex(BenchmarkError, "settings malformed"):
                self.snapshot({"config": {"agents": value}, "layers": []})


if __name__ == "__main__":
    unittest.main()
