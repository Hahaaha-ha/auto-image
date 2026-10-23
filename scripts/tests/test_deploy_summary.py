"""Public save/find CLI behavior with isolated configuration and deployment files.

Run: python -m unittest discover -s scripts/tests -v
"""

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
import importlib.util
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
SPEC = importlib.util.spec_from_file_location("deploy_summary", SCRIPT)
deploy_summary = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(deploy_summary)


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
            "software_aliases": {
                "anything-llm": {"id": "anythingllm", "source": "https://github.com/Mintplex-Labs/anything-llm"},
                "anythingllm": {"id": "anythingllm", "source": "https://github.com/Mintplex-Labs/anything-llm"},
            },
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

    def test_alias_lookup_preserves_existing_directory_and_record_spelling(self):
        data = self.data(software="anythingllm", path="deploy/anything-llm/1.0/anything-llm-summary.md")
        saved = self.invoke("save", data)
        self.assertEqual(saved["path"], data["path"])
        self.assertFalse((self.root / "deploy/anythingllm").exists())
        for query in ("anythingllm", "anything-llm", "AnythingLLM"):
            result = self.invoke("find", {"software": query})
            self.assertEqual([match["path"] for match in result["matches"]], [data["path"]])
            self.assertEqual(result["matches"][0]["record"]["software"], "anythingllm")

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

    def test_configured_filename_with_alias_and_special_version_is_findable(self):
        self.config["summary_file"] = "{{software}}-{{version}}-summary.md"
        self.write_config()
        data = self.data("v1.2.0-rc1", software="anythingllm",
                         path="deploy/anything-llm/v1.2.0-rc1/anythingllm-v1.2.0-rc1-summary.md")
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
