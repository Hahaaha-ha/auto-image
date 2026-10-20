"""External-contract checks for production isolation, evidence, revisions and reuse."""

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from deploy_records import RecordError, RecordStore, compare_versions, describe_contract
from deploy_records.contract import SUMMARY_SECTIONS


ENV = {"os": "ubuntu", "os_version": "24.04", "architecture": "aarch64", "base_image": "ubuntu-image",
       "installation_method": "official-binary", "dependencies": {"libc": "2.39"}, "sources": ["target image metadata"]}


class RecordTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = RecordStore(self.root)

    def production(self, version="1.3.0", *, software="demo", **kwargs):
        return self.store.create(software, version, actual_version=version, major_source="https://example.test/releases", target_environment=ENV, **kwargs)

    def stage(self, production, kind, guide=None, *, status="passed", mode="initial", extra=None):
        revision = self.store.begin_stage(production["production_id"], kind, guide_revision=guide, mode=mode)
        paths = revision["paths"]
        for key, relative in paths.items():
            if key.endswith("_file") and key != "revision_file":
                (self.root / relative).write_text("> 验证契约: exit-code-v1\n\n## 检查\n" if key == "verify_file" else "actual execution evidence\n", encoding="utf-8")
        evidence_file = self.root / paths["evidence_dir"] / "commands.log"
        evidence_file.write_text("demo --version\n1.3.0\n", encoding="utf-8")
        data = {"status": status, "source": "controlled execution", "evidence": [str(evidence_file.relative_to(self.root))],
                "commands": [{"command": "demo --version", "exit_code": 0, "stdout": production["version"]["actual"], "stderr": ""}], "gaps": []}
        if kind == "install":
            data["environment"] = ENV
        if kind == "verify":
            data["actual_version"] = production["version"]["actual"]
            data["commands"][0]["stdout"] = "VERIFY_PASS: actual version " + production["version"]["actual"]
        if kind == "archive":
            data["images"] = [{"image_id": "img-" + production["production_id"], "status": "available"}]
        data.update(extra or {})
        return self.store.commit_stage(production["production_id"], revision["revision_id"], data)

    def completed(self, version="1.3.0", **kwargs):
        production = self.production(version, **kwargs)
        pid = production["production_id"]
        self.store.set_machine(pid, {"server_alias": "demo-" + pid, "instance_id": "ecs-" + pid, "auth_method": "key_pair"})
        guide = self.stage(production, "guide")
        stages = {"guide": guide}
        for kind in ("install", "verify", "archive", "pipeline"):
            stages[kind] = self.stage(production, kind, guide["revision_id"])
        return self.store.get(pid), stages

    def summary(self, production, stages=None, *, listed=True, gaps=None):
        draft = self.store.begin_summary(production["production_id"])
        body = self.root / draft["paths"]["summary_file"]
        body.write_text("# 版本总结\n\n" + "\n\n".join(f"## {section}\n\n实际内容和缺口" for section in SUMMARY_SECTIONS), encoding="utf-8")
        data = deepcopy(describe_contract()["summary"])
        data["source"] = "user explicitly requested summary"
        data["gaps"] = gaps or []
        data["review"] = {"sections_checked": list(SUMMARY_SECTIONS), "notes": "checked actual execution, configuration and version"}
        if stages:
            evidence = [stages["install"]["paths"]["install_result_file"], stages["verify"]["paths"]["verify_result_file"]]
            data["stage_revisions"] = {kind: value["revision_id"] for kind, value in stages.items()}
            data["evidence"] = evidence
            for value in data["coverage"].values():
                value.update(state="recorded", reason="actual commands are preserved", evidence=evidence)
            for value in data["materials"]:
                value.update(state="not_applicable", reason="no extra configuration or scripts required")
            data["effective_steps"] = [{"description": "install and run demo", "evidence": evidence}]
            image = stages["archive"]["data"]["images"][0]["image_id"]
            data["final_image_id"] = image
            if listed:
                data["listing"] = {"status": "listed", "image_id": image, "source": "user", "feedback": "the specified image is listed", "feedback_at": "2026-10-21T09:00:00+08:00"}
        return self.store.commit_summary(production["production_id"], draft["revision_id"], data)

    def test_unique_identity_persistent_sources_and_machine_survive_resume(self):
        first = self.production(source={"call_id": "call-one", "run_id": "session"})
        second = self.production(source={"call_id": "call-two", "run_id": "session"})
        self.assertNotEqual(first["production_id"], second["production_id"])
        pid = first["production_id"]
        self.store.set_machine(pid, {"instance_id": "ecs-one", "server_alias": "alias-one", "auth_method": "key_pair"})
        self.store.set_machine(pid, {"instance_id": None, "server_alias": "alias-one", "auth_method": None})
        self.store.update(pid, source={"call_id": "call-three", "run_id": "another-session"})
        resumed = RecordStore(self.root).list_productions(call_id="call-three")
        self.assertEqual(resumed[0]["machine"]["instance_id"], "ecs-one")
        self.assertEqual(resumed[0]["machine"]["auth_method"], "key_pair")
        with self.assertRaises(RecordError):
            self.store.set_machine(pid, {"instance_id": "different-ecs"})

    def test_no_history_and_unknown_target_use_official_docs(self):
        production = self.production()
        baseline = self.store.select_baseline(production["production_id"])
        self.assertIsNone(baseline["selected"])
        previous, stages = self.completed("1.2.3")
        self.summary(previous, stages)
        unknown = self.store.create("demo", "1.3.0", actual_version="1.3.0", major_source="official versioning")
        report = self.store.select_baseline(unknown["production_id"])
        self.assertIsNone(report["selected"])
        self.assertEqual(report["excluded"][0]["reason"], "target_environment_unknown")

    def test_eligible_summary_and_numeric_lower_version_selection(self):
        for version in ("1.9.0", "1.10.0", "1.20.0", "2.0.0"):
            production, stages = self.completed(version)
            summary = self.summary(production, stages)
            self.assertEqual(self.store.candidate_report(summary), {"eligible": True, "reasons": []})
        target = self.production("1.11.0")
        baseline = self.store.select_baseline(target["production_id"])
        self.assertEqual(baseline["selected"]["actual_version"], "1.10.0")
        self.assertEqual(baseline["reason"], "highest_lower_same_major")
        self.assertIsNone(self.store.select_baseline(self.production("3.0.0")["production_id"])["selected"])
        self.assertIsNone(self.store.select_baseline(self.production("1.1.0")["production_id"])["selected"])

    def test_same_version_latest_valid_commit_ignores_failed_new_attempt_and_draft(self):
        production, stages = self.completed()
        first = self.summary(production, stages)
        second = self.summary(production, stages)
        unfinished = self.summary(production, stages, gaps=["missing final configuration"])
        self.store.begin_summary(production["production_id"])
        target = self.production()
        chosen = self.store.select_baseline(target["production_id"])["selected"]
        self.assertEqual(chosen["summary_revision"], second["revision_id"])
        self.assertGreater(second["commit_sequence"], first["commit_sequence"])
        self.assertFalse(self.store.candidate_report(unfinished)["eligible"])

    def test_pending_summary_is_committable_but_never_reusable(self):
        production = self.production()
        summary = self.summary(production)
        self.assertEqual(summary["state"], "committed")
        self.assertFalse(self.store.candidate_report(summary)["eligible"])
        self.assertFalse(self.store.select_baseline(self.production()["production_id"])["selected"])
        production, stages = self.completed()
        summary = self.summary(production, stages, listed=False)
        self.assertFalse(self.store.candidate_report(summary)["eligible"])

    def test_revisions_do_not_overwrite_and_tampered_evidence_excludes_candidate(self):
        production, stages = self.completed()
        summary = self.summary(production, stages)
        with self.assertRaises(RecordError):
            self.store.commit_summary(production["production_id"], summary["revision_id"], summary["data"])
        (self.root / stages["install"]["paths"]["install_result_file"]).write_text("tampered", encoding="utf-8")
        self.assertFalse(self.store.candidate_report(summary)["eligible"])
        self.assertIsNone(self.store.select_baseline(self.production()["production_id"])["selected"])

    def test_missing_attachment_and_only_success_boolean_do_not_qualify(self):
        production = self.production()
        draft = self.store.begin_summary(production["production_id"])
        with self.assertRaises(RecordError):
            self.store.commit_summary(production["production_id"], draft["revision_id"], {"complete": True, "listing": {"status": "listed"}})
        production, stages = self.completed()
        summary = self.summary(production, stages)
        (self.root / stages["verify"]["paths"]["verify_result_file"]).unlink()
        self.assertFalse(self.store.candidate_report(summary)["eligible"])

    def test_latest_directory_uses_verified_identity_and_aliases_are_explicit(self):
        (self.root / "deploy.config.yaml").write_text("software_aliases:\n  demo-alias:\n    id: demo\n    source: https://official.test/demo\n", encoding="utf-8")
        self.store = RecordStore(self.root)
        production, stages = self.completed("1.2.3")
        summary = self.summary(production, stages)
        alias = self.store.create("demo-alias", "latest", actual_version="1.3.0", major_source="official", target_environment=ENV)
        self.assertEqual(alias["software"]["id"], "demo")
        self.assertEqual(self.store.select_baseline(alias["production_id"])["selected"]["summary_revision"], summary["revision_id"])
        self.assertNotEqual(self.store.software_identity("demo.other")["id"], "demo")
        self.assertEqual(self.store.software_identity("demo-alias")["original_directory"], "demo-alias")

    def test_versions_are_conservative(self):
        self.assertEqual(compare_versions("1.10.0", "1.9.0")["comparison"], 1)
        for left, right in (("1.3", "1.3.0"), ("1.3.0-rc1", "1.3.0"), ("latest", "1.3.0"), ("v1.2.3", "1.3.0")):
            self.assertIsNone(compare_versions(left, right)["comparison"])
        self.assertEqual(compare_versions("v1.2.3", "1.3.0", left_tag_confirmed=True)["comparison"], -1)

    def test_environment_mismatch_stops_reuse_but_dependency_differences_are_recorded(self):
        production, stages = self.completed("1.2.3")
        self.summary(production, stages)
        target = self.production()
        self.store.select_baseline(target["production_id"])
        changed = {**ENV, "architecture": "x86_64"}
        self.assertFalse(self.store.check_environment(target["production_id"], changed)["compatible"])
        with self.assertRaises(RecordError):
            self.stage(target, "install", self.stage(target, "guide")["revision_id"], extra={"environment": changed})
        changed = {**ENV, "dependencies": {"libc": "2.40"}, "base_image": "new-image"}
        report = self.store.check_environment(target["production_id"], changed)
        self.assertTrue(report["compatible"])
        self.assertEqual(report["adaptations"], ["base_image", "dependencies"])
        unknown = {**ENV, "incompatibilities": ["vendor prohibits this base image"]}
        self.assertFalse(self.store.check_environment(target["production_id"], unknown)["compatible"])

    def test_current_verification_gate_and_actual_version(self):
        production = self.production()
        guide = self.stage(production, "guide")
        install = self.stage(production, "install", guide["revision_id"])
        with self.assertRaises(RecordError):
            self.stage(production, "verify", guide["revision_id"], extra={"actual_version": "1.2.3"})
        self.stage(production, "verify", guide["revision_id"], status="failed")
        with self.assertRaises(RecordError):
            self.store.begin_stage(production["production_id"], "archive", guide_revision=guide["revision_id"])
        self.stage(production, "verify", guide["revision_id"])
        self.stage(production, "install", guide["revision_id"])
        with self.assertRaises(RecordError):
            self.store.begin_stage(production["production_id"], "archive", guide_revision=guide["revision_id"])
        self.assertTrue((self.root / install["paths"]["install_result_file"]).exists())

    def test_remediation_has_scope_and_preserves_machine_without_repeating_updates(self):
        production, stages = self.completed()
        scope = {"preconditions": ["demo already installed"], "commands": ["demo --version"],
                 "preserve": ["instance"], "skip": ["system update"], "verification": ["demo --version"], "system_update": False}
        guide = self.stage(production, "guide", mode="remediation", extra={"execution_scope": scope})
        remedied = self.stage(production, "install", guide["revision_id"], mode="remediation")
        self.assertEqual(remedied["mode"], "remediation")
        self.assertEqual(self.store.get(production["production_id"])["machine"]["instance_id"], production["machine"]["instance_id"])
        with self.assertRaises(RecordError):
            self.stage(production, "install", guide["revision_id"], mode="remediation", extra={"commands": [{"command": "apt-get update", "exit_code": 0}]})
        self.assertTrue((self.root / stages["install"]["paths"]["install_result_file"]).exists())

    def test_snapshots_are_redacted_stable_and_external_logs_are_rejected(self):
        production = self.production()
        draft = self.store.begin_stage(production["production_id"], "guide")
        source = self.root / "private.conf"
        source.write_text("PASSWORD=secret-one\napi_key: secret-two\ncurl --password secret-three\nport=8080\n", encoding="utf-8")
        saved = self.store.snapshot(production["production_id"], draft["revision_id"], source, "service.conf")
        text = (self.root / saved).read_text(encoding="utf-8")
        for secret in ("secret-one", "secret-two", "secret-three"):
            self.assertNotIn(secret, text)
        self.assertIn("port=8080", text)
        for key in ("install_file", "verify_file"):
            (self.root / draft["paths"][key]).write_text("> 验证契约: exit-code-v1\n", encoding="utf-8")
        with self.assertRaises(RecordError):
            self.store.commit_stage(production["production_id"], draft["revision_id"], {"source": "test", "status": "passed", "evidence": ["private.conf"]})

    def test_cli_initialization_and_version_comparison(self):
        script = Path(__file__).resolve().parents[2] / "scripts" / "deploy_record.py"
        result = subprocess.run([sys.executable, str(script), "--project-root", str(self.root), "create", "--software", "demo", "--version", "1.3.0"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["production_id"].startswith("prod-"))

    def test_failed_guide_and_pipeline_keep_their_own_reports(self):
        production = self.production()
        draft = self.store.begin_stage(production["production_id"], "guide")
        (self.root / draft["paths"]["stage_result_file"]).write_text("official docs unavailable", encoding="utf-8")
        guide = self.store.commit_stage(production["production_id"], draft["revision_id"], {"status": "failed", "source": "official documentation request", "gaps": ["documentation unavailable"]})
        pipeline = self.stage(production, "pipeline", guide["revision_id"], status="failed")
        self.assertEqual(pipeline["guide_revision"], guide["revision_id"])
        with self.assertRaises(RecordError):
            self.store.begin_stage(production["production_id"], "install", guide_revision=guide["revision_id"])

    def test_latest_directory_candidate_uses_commit_order_across_directories(self):
        production, stages = self.completed("1.2.3")
        earlier = self.summary(production, stages)
        latest = self.store.create("demo", "latest", actual_version="1.2.3", major_source="official", target_environment=ENV)
        guide = self.stage(latest, "guide")
        latest_stages = {"guide": guide}
        for kind in ("install", "verify", "archive", "pipeline"):
            latest_stages[kind] = self.stage(latest, kind, guide["revision_id"])
        newer = self.summary(self.store.get(latest["production_id"]), latest_stages)
        self.assertGreater(newer["commit_sequence"], earlier["commit_sequence"])
        target = self.production("1.2.3")
        self.assertEqual(self.store.select_baseline(target["production_id"])["selected"]["summary_revision"], newer["revision_id"])

    def test_interrupted_summary_commit_preserves_previous_candidate(self):
        production, stages = self.completed()
        previous = self.summary(production, stages)
        import deploy_records.store as store_module
        atomic = store_module._atomic_json

        def fail_revision(path, value):
            if value.get("record_type") == "summary" and value.get("state") == "committed":
                raise OSError("simulated interrupted final commit")
            return atomic(path, value)

        with patch.object(store_module, "_atomic_json", side_effect=fail_revision):
            with self.assertRaises(OSError):
                self.summary(production, stages)
        chosen = self.store.select_baseline(self.production()["production_id"])["selected"]
        self.assertEqual(chosen["summary_revision"], previous["revision_id"])
        self.assertEqual(len(self.store.summaries(production_id=production["production_id"])), 1)

    def test_remediation_cannot_fall_back_to_initial_mode(self):
        production, _ = self.completed()
        scope = {"preconditions": ["existing installation"], "commands": ["repair"], "preserve": [], "skip": [], "verification": ["check"], "system_update": False}
        guide = self.stage(production, "guide", mode="remediation", extra={"execution_scope": scope})
        with self.assertRaises(RecordError):
            self.store.begin_stage(production["production_id"], "install", guide_revision=guide["revision_id"], mode="initial")

    def test_redaction_keeps_non_secret_parameters(self):
        production = self.production()
        draft = self.store.begin_stage(production["production_id"], "guide")
        source = self.root / "runtime.json"
        source.write_text('{"password": "hidden-one", "task_id": "task-one", "disk_size": 100}\ncurl --disk-size 100 --token hidden-two\nAPP_PASSWORD=hidden-three\n', encoding="utf-8")
        relative = self.store.snapshot(production["production_id"], draft["revision_id"], source, "config.txt")
        text = (self.root / relative).read_text(encoding="utf-8")
        self.assertNotIn("hidden-", text)
        self.assertIn('"task_id": "task-one"', text)
        self.assertIn('"disk_size": 100', text)
        self.assertIn("--disk-size 100", text)
        self.assertEqual(json.loads(text.splitlines()[0])["password"], "<redacted>")

    def test_interrupted_archive_remains_discoverable_for_state_recovery(self):
        production, stages = self.completed()
        pending = self.store.begin_stage(production["production_id"], "archive", guide_revision=stages["guide"]["revision_id"])
        (self.root / pending["paths"]["evidence_dir"] / "operation.json").write_text('{"job_id": "job-already-submitted"}', encoding="utf-8")
        self.assertNotIn(pending["revision_id"], [item["revision_id"] for item in self.store.stages(production["production_id"])])
        drafts = self.store.stages(production["production_id"], include_drafts=True)
        self.assertIn(pending["revision_id"], [item["revision_id"] for item in drafts])
        resumed = self.store.begin_stage(production["production_id"], "archive", guide_revision=stages["guide"]["revision_id"])
        self.assertIn(pending["revision_id"], resumed["inputs"]["previous_archives"])

    def test_selected_baseline_is_accepted_as_stable_evidence(self):
        previous, stages = self.completed("1.2.3")
        baseline = self.summary(previous, stages)
        target = self.production("1.3.0")
        selected = self.store.select_baseline(target["production_id"])
        self.assertEqual(selected["selected"]["summary_revision"], baseline["revision_id"])
        guide = self.stage(target, "guide", extra={"evidence": [baseline["paths"]["summary_file"]]})
        self.assertIn(baseline["paths"]["summary_file"], guide["manifest"])
        other = self.production("1.4.0")
        with self.assertRaises(RecordError):
            self.stage(other, "guide", extra={"evidence": [baseline["paths"]["summary_file"]]})

    def test_lost_baseline_does_not_block_current_failure_evidence(self):
        previous, stages = self.completed("1.2.3")
        baseline = self.summary(previous, stages)
        target = self.production("1.3.0")
        self.store.select_baseline(target["production_id"])
        (self.root / baseline["paths"]["summary_file"]).unlink()
        failure = self.stage(target, "guide", status="failed", extra={"gaps": ["selected baseline lost"]})
        self.assertEqual(failure["state"], "committed")
        with self.assertRaises(RecordError):
            self.stage(target, "guide", status="failed", extra={"evidence": [baseline["paths"]["summary_file"]]})

    def test_postgresql_legacy_two_segment_major_is_not_reused_across_majors(self):
        previous, stages = self.completed("9.5.25", software="postgresql")
        self.assertEqual(previous["version"]["major"], "9.5")
        self.summary(previous, stages)
        same_major = self.production("9.5.26", software="postgresql")
        self.assertIsNotNone(self.store.select_baseline(same_major["production_id"])["selected"])
        next_major = self.production("9.6.24", software="postgresql")
        result = self.store.select_baseline(next_major["production_id"])
        self.assertIsNone(result["selected"])
        self.assertEqual(result["excluded"][0]["reason"], "major_version_differs")
        modern = self.production("16.2", software="postgresql")
        self.assertEqual(modern["version"]["major"], 16)

    def test_unsupported_major_rule_is_recorded_and_excluded(self):
        previous, stages = self.completed("1.2.3")
        self.summary(previous, stages)
        target = self.production("1.3.0", major_rule="unknown")
        self.assertIsNone(target["version"]["major"])
        target = self.store.update(target["production_id"], actual_version="1.3.0")
        self.assertEqual(target["version"]["major_rule"], "unknown")
        self.assertIsNotNone(target["version"]["major_source"])
        self.assertIsNone(self.store.select_baseline(target["production_id"])["selected"])

    def test_successful_install_requires_its_own_machine_snapshot(self):
        production = self.production()
        guide = self.stage(production, "guide")
        draft = self.store.begin_stage(production["production_id"], "install", guide_revision=guide["revision_id"])
        (self.root / draft["paths"]["install_result_file"]).write_text("actual installation result", encoding="utf-8")
        with self.assertRaisesRegex(RecordError, "install_meta_file"):
            self.store.commit_stage(production["production_id"], draft["revision_id"], {"status": "passed", "source": "controlled execution"})


if __name__ == "__main__":
    unittest.main()
