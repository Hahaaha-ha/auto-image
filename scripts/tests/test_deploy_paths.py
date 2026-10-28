"""Deployment path resolution through the public, read-only CLI.

Run: python -m unittest scripts.tests.test_deploy_paths -v
"""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml


SCRIPT = Path(__file__).resolve().parents[1] / "deploy_paths.py"
ANYTHING_URL = "https://github.com/Mintplex-Labs/anything-llm"


class DeployPathsCliTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = {
            "output_dir": "deploy/{{software}}/{{version}}",
            "unknown_version": "latest",
            "install_file": "{{software}}-install.md",
            "verify_file": "{{software}}-verify.md",
            "install_result_file": "{{software}}-install-result.md",
            "install_meta_file": "{{software}}-install-meta.json",
            "verify_result_file": "{{software}}-verify-result.md",
            "archive_result_file": "{{software}}-archive-result.md",
            "pipeline_result_file": "{{software}}-pipeline-result.md",
            "summary_file": "{{software}}-summary.md",
        }
        self.write_config()

    def write_config(self):
        (self.root / "deploy.config.yaml").write_text(yaml.safe_dump(self.config), encoding="utf-8")

    def invoke(self, data, *, success=True, cwd=None):
        process = subprocess.run(
            [sys.executable, str(SCRIPT), "resolve", "--data", "-", "--root", str(self.root)],
            input=json.dumps(data), text=True, capture_output=True, cwd=cwd, check=False,
        )
        self.assertEqual(process.returncode, 0 if success else 1, process.stderr)
        self.assertEqual(process.stderr if success else process.stdout, "")
        response = json.loads(process.stdout if success else process.stderr)
        if not success:
            self.assertIsInstance(response.get("error"), str)
            self.assertTrue(response["error"])
        return response

    def data(self, software="anythingllm", version="1.19.0", **extra):
        return {"software": software, "version": version, "project_url": ANYTHING_URL, **extra}

    def raw(self, relative, content):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def summary(self, relative, software, *, project_url=ANYTHING_URL, version="1.19.0", **extra):
        record = {"software": software, "version": version, "project_url": project_url, **extra}
        return self.raw(relative, "---\n" + yaml.safe_dump(record) + "---\n\n# Existing summary\n")

    def snapshot(self):
        return {path.relative_to(self.root).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns)
                for path in self.root.rglob("*") if path.is_file()}

    def test_three_inputs_use_one_official_name_for_all_new_artifacts(self):
        for software in ("AnythingLLM", "anythingllm", "anything-llm"):
            with self.subTest(software=software):
                result = self.invoke(self.data(software))
                self.assertEqual(result["software"], "anything-llm")
                self.assertEqual(result["version"], "1.19.0")
                self.assertEqual(result["output_dir"], "deploy/anything-llm/1.19.0")
                expected = {
                    key: "deploy/anything-llm/1.19.0/" + template.replace("{{software}}", "anything-llm")
                    for key, template in self.config.items() if key.endswith("_file")
                }
                self.assertEqual(result["files"], expected)
        self.assertFalse((self.root / "deploy").exists())

    def test_unregistered_new_software_retains_repository_hyphens(self):
        result = self.invoke(self.data("FreshApp", "2.0", project_url="https://github.com/Example/Fresh-App"))
        self.assertEqual(result["software"], "fresh-app")
        self.assertEqual(result["output_dir"], "deploy/fresh-app/2.0")
        self.assertEqual(result["files"]["summary_file"], "deploy/fresh-app/2.0/fresh-app-summary.md")

    def test_display_name_with_spaces_uses_confirmed_repository_name(self):
        result = self.invoke(self.data("Anything LLM"))
        self.assertEqual(result["software"], "anything-llm")
        self.assertEqual(result["output_dir"], "deploy/anything-llm/1.19.0")

    def test_same_actual_version_preserves_a_placeholder_directory(self):
        self.summary("deploy/anythingllm/latest/anythingllm-summary.md", "anythingllm")
        result = self.invoke(self.data())
        self.assertEqual(result["version"], "1.19.0")
        self.assertEqual(result["output_dir"], "deploy/anythingllm/latest")
        self.assertEqual(result["files"]["summary_file"], "deploy/anythingllm/latest/anythingllm-summary.md")

    def test_repository_url_syntax_variants_find_the_same_existing_version(self):
        self.summary("deploy/old-spelling/1.19.0/old-spelling-summary.md", "old-spelling",
                     project_url="https://github.com/Example/Fresh-App")
        for url in ("https://github.com/example/fresh-app", "https://GITHUB.COM/Example/Fresh-App.git/"):
            with self.subTest(url=url):
                result = self.invoke(self.data("FreshApp", project_url=url))
                self.assertEqual(result["software"], "fresh-app")
                self.assertEqual(result["output_dir"], "deploy/old-spelling/1.19.0")

    def test_new_version_does_not_inherit_the_old_directory_spelling(self):
        self.summary("deploy/anythingllm/1.19.0/anythingllm-summary.md", "anythingllm")
        before = self.snapshot()
        result = self.invoke(self.data(version="1.20.0"))
        self.assertEqual(result["output_dir"], "deploy/anything-llm/1.20.0")
        self.assertEqual(result["files"]["install_file"], "deploy/anything-llm/1.20.0/anything-llm-install.md")
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.root / result["output_dir"]).exists())

    def test_same_version_preserves_existing_directory_and_filename_spelling(self):
        self.summary("deploy/anythingllm/1.19.0/anythingllm-summary.md", "anythingllm")
        self.raw("deploy/anythingllm/1.19.0/anythingllm-install.md", "Existing guide")
        result = self.invoke(self.data())
        self.assertEqual(result["software"], "anything-llm")
        self.assertEqual(result["output_dir"], "deploy/anythingllm/1.19.0")
        self.assertEqual(result["files"]["install_file"], "deploy/anythingllm/1.19.0/anythingllm-install.md")
        self.assertEqual(result["files"]["summary_file"], "deploy/anythingllm/1.19.0/anythingllm-summary.md")

    def test_existing_mixed_directory_and_filename_are_preserved(self):
        self.summary("deploy/anythingllm/1.19.0/anything-llm-summary.md", "anything-llm")
        result = self.invoke(self.data())
        self.assertEqual(result["output_dir"], "deploy/anythingllm/1.19.0")
        self.assertEqual(result["files"]["summary_file"], "deploy/anythingllm/1.19.0/anything-llm-summary.md")

    def test_known_guide_path_preserves_legacy_prefix_without_a_summary(self):
        guide = self.raw("deploy/anythingllm/1.19.0/anythingllm-install.md", "Existing guide")
        result = self.invoke(self.data(files={"install_file": str(guide)}))
        self.assertEqual(result["output_dir"], "deploy/anythingllm/1.19.0")
        self.assertEqual(result["files"]["install_file"], guide.relative_to(self.root).as_posix())
        self.assertEqual(result["files"]["verify_file"], "deploy/anythingllm/1.19.0/anythingllm-verify.md")

    def test_multiple_same_version_directories_require_an_explicit_choice(self):
        self.summary("deploy/anythingllm/1.19.0/anythingllm-summary.md", "anythingllm")
        self.summary("deploy/anything-llm/1.19.0/anything-llm-summary.md", "anything-llm")
        self.invoke(self.data(), success=False)
        result = self.invoke(self.data(output_dir="deploy/anything-llm/1.19.0"))
        self.assertEqual(result["output_dir"], "deploy/anything-llm/1.19.0")

    def test_different_owner_with_same_repository_name_cannot_overwrite(self):
        self.summary("deploy/server/1.0/server-summary.md", "server", version="1.0",
                     project_url="https://github.com/First/server")
        before = self.snapshot()
        for version in ("1.0", "2.0"):
            with self.subTest(version=version):
                self.invoke(self.data("server", version, project_url="https://github.com/Second/server"), success=False)
        self.assertEqual(self.snapshot(), before)

    def test_explicit_directory_does_not_override_a_known_source_conflict(self):
        self.summary("deploy/server/1.0/server-summary.md", "server", version="1.0",
                     project_url="https://github.com/First/server")
        self.invoke(self.data("server", "1.0", project_url="https://github.com/Second/server",
                              output_dir="deploy/server/1.0"), success=False)

    def test_stage_source_protects_software_directory_without_a_summary(self):
        self.raw("deploy/server/1.0/server-install.md",
                 "# Deployment guide\n\n> 软件：server | 官方项目：https://github.com/First/server | 目录：deploy/server/1.0\n")
        for version in ("1.0", "2.0"):
            with self.subTest(version=version):
                self.invoke(self.data("server", version, project_url="https://github.com/Second/server"), success=False)
        result = self.invoke(self.data("server", "1.0", project_url="https://github.com/First/server"))
        self.assertEqual(result["output_dir"], "deploy/server/1.0")

    def test_unknown_existing_stage_directory_requires_confirmed_paths(self):
        relative = "deploy/anything-llm/1.19.0/anything-llm-install.md"
        self.raw(relative, "Guide with no project identity")
        self.invoke(self.data(), success=False)
        result = self.invoke(self.data(files={"install_file": relative}))
        self.assertEqual(result["files"]["install_file"], relative)

    def test_internal_symlink_cannot_redirect_output_to_another_version(self):
        self.summary("deploy/anything-llm/1.19.0/anything-llm-summary.md", "anything-llm")
        other = self.raw("deploy/anything-llm/1.20.0/anything-llm-install.md", "Other version guide")
        (self.root / "deploy/anything-llm/1.19.0/anything-llm-install.md").symlink_to(other)
        self.invoke(self.data(), success=False)
        self.assertEqual(other.read_text(), "Other version guide")

    def test_unrelated_unsafe_summary_does_not_block_new_software(self):
        with tempfile.TemporaryDirectory() as outside:
            target = Path(outside) / "unrelated-summary.md"
            target.write_text("Unrelated outside data")
            directory = self.root / "deploy/unrelated/1.0"
            directory.mkdir(parents=True)
            (directory / "unrelated-summary.md").symlink_to(target)
            result = self.invoke(self.data())
            self.assertEqual(result["output_dir"], "deploy/anything-llm/1.19.0")

    def test_monorepo_components_keep_distinct_sources_and_names(self):
        first = "https://github.com/Example/Suite/tree/main/apps/editor"
        second = "https://github.com/Example/Suite/tree/main/apps/worker"
        self.summary("deploy/editor/1.0/editor-summary.md", "editor", version="1.0", project_url=first,
                     canonical_name="editor")
        result = self.invoke(self.data("SuiteWorker", "1.0", project_url=second, canonical_name="suite-worker"))
        self.assertEqual(result["software"], "suite-worker")
        self.assertEqual(result["canonical_name"], "suite-worker")
        self.assertEqual(result["project_url"], second)
        self.assertEqual(result["output_dir"], "deploy/suite-worker/1.0")

    def test_non_repository_source_uses_confirmed_product_name(self):
        result = self.invoke(self.data("ExampleProduct", "3.2", project_url="https://example.com/products/editor",
                                      canonical_name="example-editor"))
        self.assertEqual(result["software"], "example-editor")
        self.assertEqual(result["project_url"], "https://example.com/products/editor")
        self.assertEqual(result["output_dir"], "deploy/example-editor/3.2")

    def test_no_source_preserves_exact_name_instead_of_removing_punctuation(self):
        self.summary("deploy/sample-app/1.0/sample-app-summary.md", "sample-app", version="1.0", project_url=None)
        result = self.invoke(self.data("SampleApp", "1.0", project_url=None))
        self.assertEqual(result["software"], "sampleapp")
        self.assertEqual(result["output_dir"], "deploy/sampleapp/1.0")
        exact = self.invoke(self.data("SAMPLE-APP", "1.0", project_url=None))
        self.assertEqual(exact["output_dir"], "deploy/sample-app/1.0")

    def test_unknown_version_uses_configured_placeholder_without_inventing_version(self):
        self.config["unknown_version"] = "unconfirmed"
        self.write_config()
        result = self.invoke(self.data(version=None))
        self.assertIsNone(result["version"])
        self.assertEqual(result["output_dir"], "deploy/anything-llm/unconfirmed")

    def test_custom_templates_and_root_are_used_from_another_working_directory(self):
        self.config.update(output_dir="artifacts/packages/{{software}}/releases/{{version}}",
                           install_file="install-{{software}}-{{version}}.md",
                           summary_file="experience-{{software}}.md")
        self.write_config()
        with tempfile.TemporaryDirectory() as elsewhere:
            result = self.invoke(self.data(version="v2.0-rc1"), cwd=elsewhere)
        self.assertEqual(result["output_dir"], "artifacts/packages/anything-llm/releases/v2.0-rc1")
        self.assertEqual(result["files"]["install_file"],
                         "artifacts/packages/anything-llm/releases/v2.0-rc1/install-anything-llm-v2.0-rc1.md")

    def test_paths_for_different_version_directories_cannot_be_combined(self):
        guide = self.raw("deploy/anything-llm/1.19.0/anything-llm-install.md", "Install")
        verify = self.raw("deploy/anything-llm/1.20.0/anything-llm-verify.md", "Verify")
        self.invoke(self.data(files={"install_file": str(guide), "verify_file": str(verify)}), success=False)

    def test_unknown_file_keys_and_stage_path_collisions_are_rejected(self):
        self.invoke(self.data(files={"typo_file": "deploy/anything-llm/1.19.0/notes.md"}), success=False)
        self.config["summary_file"] = self.config["install_file"]
        self.write_config()
        self.invoke(self.data(), success=False)

    def test_path_traversal_and_external_overrides_are_rejected(self):
        cases = [
            self.data(version="../../outside"),
            self.data(canonical_name="../outside"),
            self.data(output_dir="../outside/1.19.0"),
            self.data(files={"install_file": "../outside/install.md"}),
        ]
        for data in cases:
            with self.subTest(data=data):
                self.invoke(data, success=False)

    def test_symlinked_output_cannot_escape_the_artifact_root(self):
        with tempfile.TemporaryDirectory() as outside:
            artifact_root = self.root / "deploy"
            artifact_root.mkdir()
            (artifact_root / "anything-llm").symlink_to(outside, target_is_directory=True)
            self.invoke(self.data(), success=False)
            self.assertEqual(list(Path(outside).iterdir()), [])

    def test_resolving_existing_artifacts_does_not_change_content_or_mtime(self):
        self.summary("deploy/anything-llm/1.19.0/anything-llm-summary.md", "anything-llm")
        self.raw("deploy/anything-llm/1.19.0/anything-llm-install.md", "Existing guide")
        before = self.snapshot()
        self.invoke(self.data())
        self.assertEqual(self.snapshot(), before)

    def test_invalid_input_is_a_structured_cli_error(self):
        for data in ([], {}, self.data(version=19), self.data(project_url=17)):
            with self.subTest(data=data):
                self.invoke(data, success=False)


if __name__ == "__main__":
    unittest.main()
