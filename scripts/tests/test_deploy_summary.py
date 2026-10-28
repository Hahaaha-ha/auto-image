"""Public save/find CLI behavior with isolated configuration and deployment files.

Run: python -m unittest discover -s scripts/tests -v
"""

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml


SCRIPT = Path(__file__).resolve().parents[1] / "deploy_summary.py"
sys.path.insert(0, str(SCRIPT.parent))
import deploy_summary



class SummaryCliTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = {
            "output_dir": "deploy/{{software}}/{{version}}",
            "summary_file": "{{software}}-summary.md",
            "install_result_file": "{{software}}-install-result.md",
            "install_meta_file": "{{software}}-install-meta.json",
        }
        self.write_config()

    def write_config(self):
        (self.root / "deploy.config.yaml").write_text(yaml.safe_dump(self.config), encoding="utf-8")

    def invoke(self, command, data, *, success=True):
        request = self.root / "request.json"
        request.write_text(json.dumps(data), encoding="utf-8")
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = deploy_summary.main([command, "--data", str(request), "--root", str(self.root)])
        self.assertEqual(status, 0 if success else 1, stderr.getvalue())
        self.assertEqual(stderr.getvalue() if success else stdout.getvalue(), "")
        return json.loads(stdout.getvalue() if success else stderr.getvalue())

    def data(self, version="1.24.0", *, directory="latest", software="nginx", **extra):
        return {"path": f"deploy/{software}/{directory}/{software}-summary.md", "software": software,
                "version": version, "body": "# 总结\n\n已执行安装；镜像未上架。", **extra}

    def raw(self, relative, content):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def test_save_find_returns_actual_path_and_header_values(self):
        data = self.data(os="ubuntu", os_version="22.04", architecture=None,
                         project_url="https://nginx.org/", custom={"attempt": "failed", "reviewed": False})
        data["path"] = str(self.root / "deploy/nginx/unused/../latest/nginx-summary.md")
        saved = self.invoke("save", data)
        self.assertEqual(saved["path"], "deploy/nginx/latest/nginx-summary.md")
        self.assertNotIn("path", saved["record"])
        self.assertNotIn("body", saved["record"])
        self.assertIsNotNone(datetime.fromisoformat(saved["record"]["updated_at"]).tzinfo)
        found = self.invoke("find", {"software": "nginx", "version": "1.24.0", "os": "ubuntu", "os_version": "24.04", "architecture": "aarch64"})
        self.assertEqual(found["warnings"], [])
        match, = found["matches"]
        self.assertEqual(match["record"], saved["record"])
        self.assertEqual(match["path"], saved["path"])
        self.assertEqual(match["version_match"]["relation"], "same")
        self.assertEqual(match["environment"]["os"]["relation"], "same")
        self.assertEqual(match["environment"]["os_version"]["relation"], "different")
        self.assertEqual(match["environment"]["architecture"]["relation"], "unknown")
        self.assertNotIn("eligible", json.dumps(found))
        self.assertIn(data["body"], (self.root / saved["path"]).read_text(encoding="utf-8"))

    def test_repeat_save_overwrites_only_one_markdown(self):
        initial = self.data(custom="old")
        self.invoke("save", initial)
        replacement = self.data(body="# 重试结果\n\n安装失败，待确认。")
        self.invoke("save", replacement)
        found = self.invoke("find", {"software": "nginx"})
        self.assertEqual(len(found["matches"]), 1)
        self.assertNotIn("custom", found["matches"][0]["record"])
        summary = (self.root / replacement["path"]).read_text(encoding="utf-8")
        self.assertIn(replacement["body"], summary)
        self.assertNotIn(initial["body"], summary)
        files = [path for path in (self.root / "deploy").rglob("*") if path.is_file()]
        self.assertEqual(files, [self.root / replacement["path"]])

    def test_invalid_input_keeps_existing_summary(self):
        data = self.data()
        self.invoke("save", data)
        path = self.root / data["path"]
        original = path.read_bytes()
        invalid = [{**data, "body": " "}, {**data, "version": 1.24}, {**data, "software": "../nginx"},
                   {**data, "os": ["ubuntu"]}, {key: value for key, value in data.items() if key != "version"}]
        for request in invalid:
            with self.subTest(request=request):
                self.assertIn("error", self.invoke("save", request, success=False))
                self.assertEqual(path.read_bytes(), original)

    def test_write_failure_keeps_old_summary_and_cleans_temporary_file(self):
        data = self.data()
        self.invoke("save", data)
        path = self.root / data["path"]
        original = path.read_bytes()
        with patch.object(deploy_summary.os, "replace", side_effect=OSError("disk replacement failed")):
            result = self.invoke("save", {**data, "body": "new content"}, success=False)
        self.assertIn("disk replacement failed", result["error"])
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(list(path.parent.glob(".deploy-*.tmp")), [])

    def test_stage_meta_attachments_and_outside_paths_are_rejected(self):
        paths = ["deploy/nginx/latest/nginx-install-result.md", "deploy/nginx/latest/nginx-install-meta.json",
                 "deploy/nginx/latest/notes.md", "deploy/nginx/latest/attachments/nginx-summary.md",
                 "deploy/redis/latest/redis-summary.md", "reports/nginx/latest/nginx-summary.md"]
        for relative in paths:
            with self.subTest(path=relative):
                file = self.raw(relative, "existing artifact")
                self.invoke("save", self.data(path=relative), success=False)
                self.assertEqual(file.read_text(encoding="utf-8"), "existing artifact")
        with tempfile.TemporaryDirectory() as outside:
            path = Path(outside) / "nginx-summary.md"
            path.write_text("outside", encoding="utf-8")
            self.invoke("save", self.data(path=str(path)), success=False)
            self.assertEqual(path.read_text(encoding="utf-8"), "outside")

    def test_file_and_directory_symlink_escapes_are_rejected(self):
        with tempfile.TemporaryDirectory() as outside:
            external = Path(outside)
            target = external / "nginx-summary.md"
            target.write_text("outside", encoding="utf-8")
            directory = self.root / "deploy/nginx/latest"
            directory.mkdir(parents=True)
            (directory / "nginx-summary.md").symlink_to(target)
            self.invoke("save", self.data(), success=False)
            found = self.invoke("find", {"software": "nginx"})
            self.assertEqual(found["matches"], [])
            self.assertEqual(found["warnings"][0]["path"], "deploy/nginx/latest/nginx-summary.md")
            self.assertEqual(found["warnings"][0]["code"], "unsafe_summary_path")
            (self.root / "deploy/nginx/other").symlink_to(external, target_is_directory=True)
            self.invoke("save", self.data(directory="other"), success=False)
            self.assertEqual(target.read_text(encoding="utf-8"), "outside")

    def test_summary_symlink_cannot_overwrite_a_stage_file_inside_artifact_root(self):
        stage = self.raw("deploy/nginx/latest/nginx-install-result.md", "installation result")
        (stage.parent / "nginx-summary.md").symlink_to(stage)
        self.invoke("save", self.data(), success=False)
        self.assertEqual(stage.read_text(encoding="utf-8"), "installation result")

    def test_official_source_canonicalizes_new_summary_without_alias_config(self):
        source = "https://github.com/Mintplex-Labs/anything-llm"
        data = self.data(software="anythingllm", path="deploy/anything-llm/1.0/anything-llm-summary.md",
                         project_url=source)
        saved = self.invoke("save", data)
        self.assertEqual(saved["path"], data["path"])
        self.assertFalse((self.root / "deploy/anythingllm").exists())
        for query in ("anythingllm", "anything-llm", "AnythingLLM"):
            result = self.invoke("find", {"software": query, "project_url": source})
            self.assertEqual([match["path"] for match in result["matches"]], [data["path"]])
            self.assertEqual(result["matches"][0]["record"]["software"], "anything-llm")

    def test_legacy_source_lookup_and_update_preserve_actual_path(self):
        source = "https://github.com/Mintplex-Labs/anything-llm"
        relative = "deploy/anythingllm/1.19.0/anything-llm-summary.md"
        old = {"software": "anythingllm", "version": "1.19.0", "project_url": source}
        self.raw(relative, "---\n" + yaml.safe_dump(old) + "---\nOld body")
        result = self.invoke("find", {"software": "AnythingLLM", "project_url": source})
        self.assertEqual(result["warnings"], [])
        self.assertEqual(result["matches"][0]["record"], old)
        self.assertEqual(result["matches"][0]["path"], relative)
        saved = self.invoke("save", {**old, "path": relative, "body": "Updated body"})
        self.assertEqual(saved["path"], relative)
        self.assertEqual(saved["record"]["software"], "anything-llm")
        self.assertFalse((self.root / "deploy/anything-llm").exists())

    def test_same_name_different_source_is_not_found_or_overwritten(self):
        original = self.data(project_url="https://nginx.org/")
        self.invoke("save", original)
        query = {"software": "nginx", "project_url": "https://example.org/nginx"}
        self.assertEqual(self.invoke("find", query)["matches"], [])
        file = self.root / original["path"]
        before = file.read_bytes()
        self.invoke("save", {**original, **query, "body": "Wrong product"}, success=False)
        self.assertEqual(file.read_bytes(), before)

    def test_first_summary_can_use_confirmed_existing_stage_directory(self):
        self.config["install_file"] = "{{software}}-install.md"
        self.write_config()
        self.raw("deploy/anythingllm/1.19.0/anything-llm-install.md", "Existing guide")
        data = self.data("1.19.0", software="AnythingLLM",
                         path="deploy/anythingllm/1.19.0/anything-llm-summary.md",
                         project_url="https://github.com/Mintplex-Labs/anything-llm")
        saved = self.invoke("save", data)
        self.assertEqual(saved["path"], data["path"])
        self.assertEqual(saved["record"]["software"], "anything-llm")
        found = self.invoke("find", {"software": "anything-llm"})
        self.assertEqual(found["matches"][0]["path"], data["path"])

    def test_first_summary_cannot_conflict_with_existing_stage_source(self):
        self.config["install_file"] = "{{software}}-install.md"
        self.write_config()
        guide = self.raw("deploy/server/1.0/server-install.md",
                         "# Guide\n\n> 官方项目：https://github.com/First/server\n")
        data = self.data("1.0", directory="1.0", software="server", project_url="https://github.com/Second/server")
        self.invoke("save", data, success=False)
        self.assertFalse((self.root / data["path"]).exists())
        self.assertIn("First/server", guide.read_text())

    def test_updating_legacy_summary_keeps_known_identity_when_not_resupplied(self):
        relative = "deploy/anythingllm/1.19.0/anythingllm-summary.md"
        old = {"software": "anything-llm", "version": "1.19.0",
               "project_url": "https://github.com/Mintplex-Labs/anything-llm"}
        self.raw(relative, "---\n" + yaml.safe_dump(old) + "---\nOld body")
        saved = self.invoke("save", self.data("1.19.0", software="anything-llm", path=relative))
        self.assertEqual(saved["record"]["project_url"], old["project_url"])
        found = self.invoke("find", {"software": "anything-llm"})
        self.assertEqual(found["matches"][0]["path"], relative)

    def test_component_override_is_inherited_before_identity_validation(self):
        data = self.data("1.0", software="editor", canonical_name="editor",
                         project_url="https://github.com/example/suite")
        self.invoke("save", data)
        update = {key: value for key, value in data.items() if key != "canonical_name"}
        saved = self.invoke("save", {**update, "body": "Updated component"})
        self.assertEqual(saved["record"]["canonical_name"], "editor")
        self.assertEqual(saved["record"]["software"], "editor")

    def test_component_sources_are_not_collapsed_to_repository(self):
        self.invoke("save", self.data(software="component-a", canonical_name="component-a",
                                     project_url="https://github.com/vendor/suite/tree/main/component-a"))
        query = {"software": "component-a", "canonical_name": "component-b",
                 "project_url": "https://github.com/vendor/suite/tree/main/component-b"}
        self.assertEqual(self.invoke("find", query)["matches"], [])

    def test_missing_source_does_not_guess_punctuation_alias(self):
        self.invoke("save", self.data(software="new-app"))
        self.assertEqual(self.invoke("find", {"software": "newapp"})["matches"], [])
        self.assertEqual(len(self.invoke("find", {"software": "new-app"})["matches"]), 1)

    def test_source_unknown_legacy_layout_supports_exact_lookup_and_update(self):
        relative = "deploy/anything-llm/1.0/anythingllm-summary.md"
        header = {"software": "anythingllm", "version": "1.0"}
        self.raw(relative, "---\n" + yaml.safe_dump(header) + "---\nLegacy body")
        found = self.invoke("find", {"software": "anythingllm"})
        self.assertEqual(found["warnings"], [])
        self.assertEqual(found["matches"][0]["path"], relative)
        self.assertEqual(self.invoke("find", {"software": "anything-llm"})["matches"], [])
        updated = self.invoke("save", {**header, "path": relative, "body": "Updated"})
        self.assertEqual(updated["path"], relative)
        self.assertNotIn("project_url", updated["record"])

    def test_explicit_legacy_summary_can_use_current_confirmed_name_and_source(self):
        relative = "deploy/anythingllm/1.0/anythingllm-summary.md"
        header = {"software": "anythingllm", "version": "1.0"}
        self.raw(relative, "---\n" + yaml.safe_dump(header) + "---\nLegacy body")
        saved = self.invoke("save", self.data("1.0", software="anything-llm", path=relative,
                                             project_url="https://github.com/Mintplex-Labs/anything-llm"))
        self.assertEqual(saved["path"], relative)
        self.assertEqual(saved["record"]["software"], "anything-llm")
        self.assertTrue(any(item["path"] == relative for item in saved["warnings"]))
        self.assertEqual(self.invoke("find", {"software": "anything-llm"})["matches"][0]["path"], relative)

    def test_explicit_summary_update_can_repair_missing_or_broken_metadata(self):
        data = self.data()
        for previous in ("# Legacy summary without a header", "---\nsoftware: [\n---\nOld body"):
            with self.subTest(previous=previous):
                self.raw(data["path"], previous)
                saved = self.invoke("save", data)
                self.assertEqual(saved["path"], data["path"])
                self.assertTrue(saved["warnings"])
                self.assertEqual(self.invoke("find", {"software": data["software"]})["warnings"], [])

    def test_confirming_source_for_an_exact_legacy_name_can_normalize_its_header(self):
        relative = "deploy/anythingllm/1.0/anythingllm-summary.md"
        header = {"software": "anythingllm", "version": "1.0"}
        self.raw(relative, "---\n" + yaml.safe_dump(header) + "---\nLegacy body")
        saved = self.invoke("save", {**header, "path": relative, "body": "Confirmed source",
                                     "project_url": "https://github.com/Mintplex-Labs/anything-llm"})
        self.assertEqual(saved["record"]["software"], "anything-llm")
        self.assertEqual(self.invoke("find", {"software": "anything-llm"})["matches"][0]["path"], relative)

    def test_punctuation_does_not_establish_product_identity(self):
        self.invoke("save", self.data(software="foo-bar"))
        self.invoke("save", self.data(software="foobar"))
        result = self.invoke("find", {"software": "foobar"})
        self.assertEqual([match["record"]["software"] for match in result["matches"]], ["foobar"])

    def test_versions_sort_conservatively_and_keep_special_strings(self):
        versions = ["1.9.0", "1.10.0", "1.11.0", "2.0.0", "v1.10.0", "1.10.0-rc1", "1.10.0-1ubuntu1", "1.10", None]
        for index, version in enumerate(versions):
            self.invoke("save", self.data(version, directory=str(index)))
        result = self.invoke("find", {"software": "nginx", "version": "1.11.0"})
        self.assertEqual([match["record"]["version"] for match in result["matches"][:3]], ["1.11.0", "1.10.0", "1.9.0"])
        relations = {match["record"]["version"]: match["version_match"]["relation"] for match in result["matches"]}
        self.assertEqual(relations["2.0.0"], "newer")
        for version in versions[4:-1]:
            self.assertEqual(relations[version], "incomparable")
        self.assertEqual(relations[None], "unknown")
        exact = self.invoke("find", {"software": "nginx", "version": "v1.10.0"})
        self.assertEqual(exact["matches"][0]["record"]["version"], "v1.10.0")
        self.assertEqual(exact["matches"][0]["version_match"]["relation"], "same")

    def test_unknown_version_and_fields_are_not_inferred_from_directory(self):
        self.invoke("save", self.data(None, directory="9.99", notes={"unverified": True}))
        self.raw("deploy/nginx/4.0/nginx-summary.md", "---\nsoftware: nginx\ncustom: unfinished\n---\nA partial record.")
        result = self.invoke("find", {"software": "nginx", "version": "9.99", "os": "ubuntu"})
        self.assertEqual(len(result["matches"]), 2)
        for match in result["matches"]:
            self.assertIsNone(match["record"].get("version"))
            self.assertEqual(match["version_match"]["relation"], "unknown")
            self.assertEqual(match["environment"]["os"]["relation"], "unknown")

    def test_partial_corruption_warns_with_paths_without_invented_records(self):
        self.invoke("save", self.data())
        invalid = {
            "old": "# Old summary without a header",
            "broken": "---\nsoftware: [\n---\nBody",
            "unclosed": "---\nsoftware: nginx\nBody",
            "unconfirmed": "---\nversion: '1.0'\n---\nBody",
            "ambiguous": "---\nsoftware: redis\nsoftware: nginx\n---\nBody",
        }
        for directory, content in invalid.items():
            self.raw(f"deploy/nginx/{directory}/nginx-summary.md", content)
        unreadable = self.root / "deploy/nginx/unreadable/nginx-summary.md"
        unreadable.parent.mkdir(parents=True)
        unreadable.write_bytes(b"\xff\xfe")
        result = self.invoke("find", {"software": "nginx"})
        self.assertEqual(len(result["matches"]), 1)
        self.assertEqual(len(result["warnings"]), 6)
        self.assertTrue(all(warning["path"].startswith("deploy/nginx/") and warning["message"] for warning in result["warnings"]))
        self.assertTrue(all("record" not in warning for warning in result["warnings"]))

    def test_invalid_source_with_canonical_override_does_not_hide_valid_summaries(self):
        self.invoke("save", self.data())
        bad = {"software": "nginx", "canonical_name": "nginx", "version": "1.0", "project_url": "not-a-url"}
        self.raw("deploy/nginx/bad/nginx-summary.md", "---\n" + yaml.safe_dump(bad) + "---\nBody")
        result = self.invoke("find", {"software": "nginx"})
        self.assertEqual(len(result["matches"]), 1)
        self.assertEqual(result["warnings"][0]["code"], "invalid_summary")

    def test_unquoted_timestamp_keeps_original_string(self):
        self.raw("deploy/nginx/1/nginx-summary.md", "---\nsoftware: nginx\nversion: '1'\nupdated_at: 2026-10-23T10:00:00+08:00\n---\nBody")
        result = self.invoke("find", {"software": "nginx"})
        self.assertEqual(result["matches"][0]["record"]["updated_at"], "2026-10-23T10:00:00+08:00")

    def test_custom_path_config_is_shared_by_save_and_find(self):
        self.config.update(output_dir="artifacts/packages/{{software}}/releases/{{version}}", summary_file="experience-{{software}}.md")
        self.write_config()
        data = self.data(path="artifacts/packages/nginx/releases/old-label/experience-nginx.md")
        saved = self.invoke("save", data)
        result = self.invoke("find", {"software": "nginx"})
        self.assertEqual(result["matches"][0]["path"], saved["path"])
        self.invoke("save", self.data(), success=False)

    def test_other_deployment_artifacts_and_state_stay_unchanged(self):
        artifacts = ["nginx-install-result.md", "nginx-verify-result.md", "nginx-archive-result.md",
                     "nginx-install-meta.json", "nginx-state.json", "install.sh"]
        paths = [self.raw(f"deploy/nginx/latest/{name}", f"original {name}") for name in artifacts]
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}
        self.invoke("save", self.data())
        self.invoke("save", self.data(body="本次未完成。"))
        self.invoke("find", {"software": "nginx"})
        after = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}
        self.assertEqual(after, before)

    def test_legacy_configured_filename_and_special_version_are_findable(self):
        self.config["summary_file"] = "{{software}}-{{version}}-summary.md"
        self.write_config()
        data = self.data("v1.2.0-rc1", software="anythingllm",
                         project_url="https://github.com/Mintplex-Labs/anything-llm",
                         path="deploy/anything-llm/v1.2.0-rc1/anythingllm-v1.2.0-rc1-summary.md")
        header = {key: value for key, value in data.items() if key not in ("path", "body")}
        self.raw(data["path"], "---\n" + yaml.safe_dump(header) + "---\nLegacy summary")
        saved = self.invoke("save", data)
        result = self.invoke("find", {"software": "anything-llm", "version": "v1.2.0-rc1"})
        self.assertEqual(result["warnings"], [])
        self.assertEqual(result["matches"][0]["path"], saved["path"])
        self.assertEqual(result["matches"][0]["version_match"]["relation"], "same")

    def test_expanded_summary_pattern_cannot_overwrite_stage_files(self):
        self.config["summary_file"] = "{{software}}-{{version}}.md"
        self.write_config()
        stage = self.raw("deploy/nginx/install-result/nginx-install-result.md", "existing stage result")
        self.invoke("save", self.data(path=str(stage)), success=False)
        self.assertEqual(stage.read_text(encoding="utf-8"), "existing stage result")
        result = self.invoke("find", {"software": "nginx"})
        self.assertEqual(result["matches"], [])
        self.assertIn("collides", result["warnings"][0]["message"])

    def test_empty_result_and_invalid_cli_input(self):
        self.assertEqual(self.invoke("find", {"software": "nginx"}), {"matches": [], "warnings": []})
        self.assertIn("JSON object", self.invoke("find", [], success=False)["error"])
        self.assertIn("software", self.invoke("find", {}, success=False)["error"])

    def test_script_entrypoint_and_stdin(self):
        result = subprocess.run([sys.executable, str(SCRIPT), "find", "--root", str(self.root), "--data", "-"],
                                input='{"software":"nginx"}', text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"matches": [], "warnings": []})
        help_result = subprocess.run([sys.executable, str(SCRIPT), "--help"], text=True, capture_output=True, check=False)
        self.assertEqual(help_result.returncode, 0)
        self.assertIn("save input:", help_result.stdout)


if __name__ == "__main__":
    unittest.main()
