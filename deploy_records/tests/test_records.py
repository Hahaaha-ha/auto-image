"""Current-version overwrite and reuse behavior through the public local interface."""

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from deploy_records import RecordError, VersionStore, compare_versions, describe_contract
from deploy_records.contract import STAGE_FILES, SUMMARY_SECTIONS

ENV = {"os": "ubuntu", "os_version": "24.04", "architecture": "aarch64", "installation_method": "official-binary",
       "sources": ["resolved target image"], "base_image": "ubuntu-24-arm", "dependencies": {"libc": "2.39"}}


class VersionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = VersionStore(self.root)

    def identify(self, version="1.3.0", software="demo", **kwargs):
        rule = None if software == "postgresql" else "numeric"
        return self.store.update(software, version, actual_version=version, major_source="official release policy", major_rule=rule, target_environment=ENV, **kwargs)

    def stage(self, stage, version="1.3.0", software="demo", mode="initial", status="passed", extra=None):
        state = self.store.begin_stage(software, version, stage, mode=mode)
        if stage == "guide" and mode == "initial":
            state = self.identify(version, software)
        for key in STAGE_FILES[stage]:
            target = self.root / state["paths"][key]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(state["machine"]) if key == "install_meta_file" else "> 验证契约: exit-code-v1\n\n## 检查\n" if key == "verify_file" else f"current {stage} for {version}\n", encoding="utf-8")
        data = {"status": status, "source": "actual controlled command", "commands": [{"command": "demo --version", "exit_code": 0, "stdout": "VERIFY_PASS: " + version, "stderr": ""}], "gaps": []}
        if stage == "install":
            data["environment"] = ENV
        if stage == "verify":
            data["actual_version"] = version
        if stage == "archive":
            data["images"] = [{"image_id": "image-" + version}]
        data.update(extra or {})
        return self.store.commit_stage(software, version, stage, data)

    def completed(self, version="1.3.0", software="demo"):
        self.stage("guide", version, software)
        self.store.update(software, version, machine={"server_alias": "target", "instance_id": "ecs-one", "auth_method": "key_pair"})
        for stage in ("install", "verify", "archive", "pipeline"):
            self.stage(stage, version, software)
        return self.store.load(software, version)

    def summary(self, version="1.3.0", software="demo", listed=True, body_label="latest summary"):
        data = deepcopy(describe_contract()["summary"])
        data["body"] = "# Current summary\n\n" + "\n\n".join(f"## {section}\n\n{body_label}" for section in SUMMARY_SECTIONS)
        data["source"] = "manual user request"
        data["evidence"] = [self.store.paths(software, version)["install_file"], self.store.paths(software, version)["verify_result_file"]]
        data["final_image_id"] = "image-" + version
        if listed:
            data["listing"].update(status="listed", image_id="image-" + version, feedback="specified image is listed", feedback_at="2026-10-22T09:00:00+08:00")
        return self.store.save_summary(software, version, data)

    def select(self, version="1.3.0", software="demo", **kwargs):
        rule = kwargs.pop("major_rule", None if software == "postgresql" else "numeric")
        return self.store.select_baseline(software, version, actual_version=version, major_source="official release policy", major_rule=rule, target_environment=ENV, **kwargs)

    def test_flat_paths_have_no_generated_identity_or_nested_directories(self):
        state = self.completed()
        self.summary()
        paths = state["paths"]
        self.assertTrue(all(Path(relative).parent.as_posix() == paths["output_dir"] for key, relative in paths.items() if key != "output_dir"))
        self.assertEqual(paths["summary_file"], "deploy/demo/1.3.0/demo-summary.md")
        self.assertFalse(any(part in {"runs", "stages", "summaries"} for path in self.root.rglob("*") for part in path.parts))
        content = json.dumps(state)
        self.assertNotIn("production_id", content)
        self.assertNotIn("revision_id", content)
        self.assertNotIn("call_id", content)

    def test_summary_overwrites_current_body_and_metadata(self):
        self.completed()
        first = self.summary(body_label="old body")
        second = self.summary(body_label="new body")
        self.assertEqual(first["summary_file"], second["summary_file"])
        text = (self.root / second["summary_file"]).read_text()
        self.assertIn("new body", text)
        self.assertNotIn("old body", text)
        self.assertEqual(len(list(self.root.rglob("*-summary.md"))), 1)

    def test_new_same_version_install_discards_old_success_and_machine(self):
        self.completed()
        self.summary()
        self.assertTrue(self.select()["selected"])
        state = self.store.begin_stage("demo", "1.3.0", "guide")
        self.assertFalse(state["stages"])
        self.assertFalse(state["machine"])
        self.assertIsNone(state["summary"])
        self.assertIsNone(state["version"]["actual"])
        self.assertFalse((self.root / state["paths"]["summary_file"]).exists())
        self.assertFalse((self.root / state["paths"]["install_meta_file"]).exists())
        self.assertIsNone(self.select()["selected"])

    def test_begin_guide_preserves_selected_baseline_and_target_environment(self):
        self.identify("1.3.0")
        baseline = {"selected": {"actual_version": "1.2.3", "summary_file": "deploy/demo/1.2.3/demo-summary.md"}, "reason": "highest_lower_same_major"}
        self.store.update("demo", "1.3.0", baseline=baseline, target_environment=ENV)
        state = self.store.begin_stage("demo", "1.3.0", "guide", mode="initial")
        self.assertEqual(state["baseline"], baseline)
        self.assertEqual(state["target_environment"], ENV)

    def test_pipeline_restart_invalidates_current_summary(self):
        self.completed()
        self.summary()
        self.assertTrue(self.store.load("demo", "1.3.0")["summary"])
        state = self.store.begin_stage("demo", "1.3.0", "pipeline")
        self.assertIsNone(state["summary"])
        self.assertFalse((self.root / state["paths"]["summary_file"]).exists())

    def test_failure_replaces_current_status_without_history_or_special_report(self):
        self.completed()
        self.summary()
        self.store.begin_stage("demo", "1.3.0", "install")
        state = self.store.commit_stage("demo", "1.3.0", "install", {"status": "failed", "source": "network timeout"})
        self.assertEqual(state["stages"]["install"]["status"], "failed")
        self.assertNotIn("verify", state["stages"])
        self.assertNotIn("archive", state["stages"])
        self.assertIsNone(self.select()["selected"])

    def test_stage_restart_clears_downstream_files_and_old_issues(self):
        state = self.completed()
        for key in ("install_issues_file", "verify_issues_file", "archive_issues_file"):
            (self.root / state["paths"][key]).write_text("outdated issue")
        self.store.begin_stage("demo", "1.3.0", "verify")
        self.assertTrue((self.root / state["paths"]["install_result_file"]).exists())
        self.assertFalse((self.root / state["paths"]["verify_result_file"]).exists())
        self.assertFalse((self.root / state["paths"]["archive_result_file"]).exists())
        self.assertFalse((self.root / state["paths"]["verify_issues_file"]).exists())

    def test_current_cloud_operation_survives_archive_restart(self):
        self.completed()
        operation = {"action": "create-image", "instance_id": "ecs-one", "image_id": "image-pending", "job_id": "job-pending", "status": "running"}
        self.store.update("demo", "1.3.0", cloud=operation)
        state = self.store.begin_stage("demo", "1.3.0", "archive")
        self.assertEqual(state["cloud"], operation)
        self.assertNotIn("archive", state["stages"])
        self.store.update("demo", "1.3.0", cloud={"action": "change-os", "instance_id": "ecs-one", "image_id": "image-pending", "status": "submitted"})
        self.assertNotIn("job_id", self.store.load("demo", "1.3.0")["cloud"])

    def test_no_history_and_unlisted_summary_use_official_docs(self):
        self.assertIsNone(self.select()["selected"])
        self.completed("1.2.3")
        self.summary("1.2.3", listed=False)
        self.assertIsNone(self.select()["selected"])

    def test_same_version_first_then_highest_numeric_lower_same_major(self):
        for version in ("1.9.0", "1.10.0", "1.20.0", "2.0.0"):
            self.completed(version)
            self.summary(version)
        self.assertEqual(self.select("1.11.0")["selected"]["actual_version"], "1.10.0")
        self.assertEqual(self.select("1.9.0")["reason"], "same_version")
        self.assertIsNone(self.select("1.1.0")["selected"])
        self.assertIsNone(self.select("3.0.0")["selected"])

    def test_postgresql_major_policy_and_unknown_rule(self):
        self.completed("9.5.25", "postgresql")
        self.summary("9.5.25", "postgresql")
        self.assertTrue(self.select("9.5.26", "postgresql")["selected"])
        self.assertIsNone(self.select("9.6.24", "postgresql")["selected"])
        self.assertIsNone(self.select("9.5.26", "postgresql", major_rule="unknown")["selected"])

    def test_version_comparison_remains_conservative(self):
        self.assertEqual(compare_versions("1.10.0", "1.9.0")["comparison"], 1)
        for value in ("1.3.0-rc1", "1.3", "latest", "v1.2.3"):
            self.assertIsNone(compare_versions(value, "1.3.0")["comparison"])
        self.assertEqual(compare_versions("v1.2.3", "1.3.0", left_tag_confirmed=True)["comparison"], -1)

    def test_target_environment_unknown_or_different_excludes_baseline(self):
        self.completed("1.2.3")
        self.summary("1.2.3")
        for environment in ({}, {**ENV, "architecture": "x86_64"}):
            result = self.store.select_baseline("demo", "1.3.0", actual_version="1.3.0", major_source="official", target_environment=environment)
            self.assertIsNone(result["selected"])
        self.identify()
        self.store.update("demo", "1.3.0", baseline=self.select()["selected"])
        self.assertFalse(self.store.check_environment("demo", "1.3.0", {**ENV, "architecture": "x86_64"})["compatible"])
        self.assertTrue(self.store.check_environment("demo", "1.3.0", {**ENV, "base_image": "new base"})["compatible"])

    def test_existing_machine_and_auth_survive_remediation(self):
        self.completed()
        state = self.store.begin_stage("demo", "1.3.0", "guide", mode="remediation")
        self.assertEqual(state["machine"]["instance_id"], "ecs-one")
        scope = {"preconditions": ["already installed"], "commands": ["demo --version"], "verification": ["demo --version"], "system_update": False}
        for key in STAGE_FILES["guide"]:
            (self.root / state["paths"][key]).write_text("> 验证契约: exit-code-v1\n")
        self.store.commit_stage("demo", "1.3.0", "guide", {"status": "passed", "execution_scope": scope})
        self.store.update("demo", "1.3.0", machine={"server_alias": "target", "instance_id": None, "auth_method": None})
        with self.assertRaises(RecordError):
            self.store.begin_stage("demo", "1.3.0", "install", mode="initial")
        state = self.stage("install", mode="remediation")
        self.assertEqual(state["machine"]["auth_method"], "key_pair")
        with self.assertRaisesRegex(RecordError, "scope"):
            self.stage("install", mode="remediation", extra={"commands": [{"command": "apt-get update", "exit_code": 0}]})

    def test_actual_version_and_positive_verification_gate(self):
        self.stage("guide")
        self.stage("install")
        with self.assertRaisesRegex(RecordError, "version"):
            self.stage("verify", extra={"actual_version": "1.2.3"})
        self.stage("verify", status="failed")
        with self.assertRaises(RecordError):
            self.store.begin_stage("demo", "1.3.0", "archive")
        with self.assertRaisesRegex(RecordError, "positive"):
            self.stage("verify", extra={"commands": [{"command": "check", "exit_code": 0, "stdout": "empty"}]})

    def test_summary_checks_image_listing_and_required_current_materials(self):
        state = self.completed()
        self.assertTrue(self.summary()["eligible"])
        (self.root / state["paths"]["install_file"]).unlink()
        self.assertFalse(self.store.candidate_report(self.store.load("demo", "1.3.0"))["eligible"])
        state = self.completed()
        self.summary()
        state = self.store.load("demo", "1.3.0")
        state["summary"]["listing"]["image_id"] = "different-image"
        self.assertFalse(self.store.candidate_report(state)["eligible"])

    def test_empty_summary_sections_and_missing_evidence_are_not_candidates(self):
        self.completed()
        data = deepcopy(describe_contract()["summary"])
        data["body"] = "# x\n\n" + "\n\n".join(f"## {section}\n" for section in SUMMARY_SECTIONS)
        data["source"] = "manual user request"
        data["listing"].update(status="listed", image_id="image-1.3.0", feedback="listed", feedback_at="2026-10-22T09:00:00+08:00")
        data["final_image_id"] = "image-1.3.0"
        with self.assertRaises(RecordError):
            self.store.save_summary("demo", "1.3.0", data)

    def test_summary_requires_manual_source(self):
        self.completed()
        data = deepcopy(describe_contract()["summary"])
        data["body"] = "# x\n\n" + "\n\n".join(f"## {section}\n\ncurrent material" for section in SUMMARY_SECTIONS)
        data["evidence"] = [self.store.paths("demo", "1.3.0")["install_file"]]
        data.pop("source", None)
        with self.assertRaisesRegex(RecordError, "source"):
            self.store.save_summary("demo", "1.3.0", data)

    def test_interrupted_summary_overwrite_is_not_eligible(self):
        self.completed()
        self.summary()
        import deploy_records.store as module
        atomic = module._atomic
        def fail_body(path, text):
            if path.name.endswith("-summary.md"):
                raise OSError("write interrupted")
            atomic(path, text)
        with patch.object(module, "_atomic", side_effect=fail_body):
            with self.assertRaises(OSError):
                self.summary(body_label="replacement")
        self.assertFalse(self.store.candidate_report(self.store.load("demo", "1.3.0"))["eligible"])

    def test_verified_aliases_share_one_current_directory(self):
        (self.root / "deploy.config.yaml").write_text("software_aliases:\n  demo-alias:\n    id: demo\n    source: https://official.test/project\n")
        self.store = VersionStore(self.root)
        self.assertEqual(self.store.paths("demo", "1.3.0"), self.store.paths("demo-alias", "1.3.0"))
        self.assertNotEqual(self.store.paths("demo.other", "1.3.0"), self.store.paths("demo", "1.3.0"))

    def test_secrets_are_redacted_without_changing_non_secret_parameters(self):
        state = self.identify()
        self.store.commit_stage("demo", "1.3.0", "install", {"status": "failed", "commands": [{"command": "curl --token hidden-value --disk-size 100", "exit_code": 1, "stderr": 'password="hidden-other"'}]})
        text = (self.root / state["paths"]["state_file"]).read_text()
        self.assertNotIn("hidden-", text)
        self.assertIn("--disk-size 100", text)
        json.loads(text)

    def test_machine_metadata_redacts_secret_fields(self):
        self.store.update("demo", "1.3.0", machine={"server_alias": "target", "auth_method": "key_pair", "identity_evidence": "private_key=hidden-key"})
        state = self.store.load("demo", "1.3.0")
        meta = (self.root / state["paths"]["install_meta_file"]).read_text()
        self.assertNotIn("hidden-key", meta)

    def test_cli_exposes_only_software_and_version_identity(self):
        script = Path(__file__).resolve().parents[2] / "scripts" / "deploy_record.py"
        result = subprocess.run([sys.executable, str(script), "--project-root", str(self.root), "paths", "--software", "demo", "--version", "1.3.0"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["output_dir"], "deploy/demo/1.3.0")


if __name__ == "__main__":
    unittest.main()
