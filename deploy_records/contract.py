"""Current version files and the vocabulary used by deployment prompts."""

SCHEMA_VERSION = 2
STAGES = ("guide", "install", "verify", "archive", "pipeline")
STATUSES = ("passed", "failed", "partial", "unknown")
MODES = ("initial", "remediation")
LISTING_STATUSES = ("listed", "not_listed", "unknown")
USER_LISTING_SOURCE = "user"
ENVIRONMENT_KEYS = ("os", "os_version", "architecture", "installation_method")
MAJOR_RULES = ("numeric", "postgresql", "unknown")
SUMMARY_SECTIONS = ("软件与环境", "制作来源", "复用基准", "最终有效步骤", "安装详情", "版本差异", "修正经验", "验证与缺口", "更新说明")
DEFAULT_CONFIG = {
    "output_dir": "deploy/{{software}}/{{version}}", "unknown_version": "latest",
    "state_file": "{{software}}-state.json", "summary_file": "{{software}}-summary.md",
    "install_file": "{{software}}-install.md", "verify_file": "{{software}}-verify.md",
    "install_result_file": "{{software}}-install-result.md", "install_issues_file": "{{software}}-install-issues.md",
    "install_meta_file": "{{software}}-install-meta.json",
    "verify_result_file": "{{software}}-verify-result.md", "verify_issues_file": "{{software}}-verify-issues.md",
    "archive_result_file": "{{software}}-archive-result.md", "archive_issues_file": "{{software}}-archive-issues.md",
    "deploy_list_file": "{{software}}-deploy-list.md", "pipeline_result_file": "{{software}}-pipeline-result.md",
    "software_version_rules": {"postgresql": {"rule": "postgresql", "source": "https://www.postgresql.org/support/versioning/"}},
}
STAGE_FILES = {
    "guide": ("install_file", "verify_file"), "install": ("install_result_file", "install_meta_file"),
    "verify": ("verify_result_file",), "archive": ("archive_result_file", "deploy_list_file"), "pipeline": ("pipeline_result_file",),
}
STAGE_ISSUES = {stage: stage + "_issues_file" for stage in ("install", "verify", "archive")}


def describe_contract():
    return {
        "schema_version": SCHEMA_VERSION, "stages": STAGES, "statuses": STATUSES, "modes": MODES,
        "listing_statuses": LISTING_STATUSES, "major_rules": MAJOR_RULES,
        "required_environment": ENVIRONMENT_KEYS, "summary_sections": SUMMARY_SECTIONS,
        "path_rule": "One directory per software/version; returned files are overwritten in place. No history or extra persistent IDs.",
        "update": {"actual_version": "1.3.0", "major_source": "official versioning URL", "major_rule": None,
                   "tag_prefix_confirmed": False, "target_environment": {"os": "ubuntu", "os_version": "24.04", "architecture": "aarch64",
                   "installation_method": "official-binary", "sources": ["target machine or resolved image"]}},
        "machine": {"server_alias": None, "instance_id": None, "auth_method": None},
        "cloud": {"action": None, "instance_id": None, "image_id": None, "job_id": None, "status": None},
        "stage": {"status": "passed", "source": "actual execution", "commands": [
            {"command": "actual command", "exit_code": 0, "stdout": "actual output; required verify needs VERIFY_PASS:", "stderr": "", "required": True}],
            "environment": {}, "actual_version": None, "images": [], "operations": [], "gaps": []},
        "execution_scope": {"preconditions": ["read-only checks of current state"], "commands": ["exact corrective command"],
                            "preserve": [], "skip": [], "verification": ["read-only checks"], "system_update": False},
        "summary": {"body": "Markdown using summary_sections", "source": "manual user request", "gaps": [], "evidence": [],
                    "final_image_id": None, "listing": {"status": "unknown", "source": USER_LISTING_SOURCE,
                    "image_id": None, "feedback": None, "feedback_at": None}},
    }
