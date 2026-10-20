"""The persisted record format and its vocabulary, shared by all consumers."""

SCHEMA_VERSION = 1
STAGES = ("guide", "install", "verify", "archive", "pipeline")
STATUSES = ("passed", "failed", "partial", "unknown")
MODES = ("initial", "remediation")
LISTING_STATUSES = ("listed", "not_listed", "unknown")
USER_LISTING_SOURCE = "user"
LISTING_SOURCES = (USER_LISTING_SOURCE,)
COVERAGE_STATES = ("recorded", "not_applicable", "missing")
ENVIRONMENT_KEYS = ("os", "os_version", "architecture", "installation_method")
ENVIRONMENT_FIELDS = (*ENVIRONMENT_KEYS, "base_image", "dependencies", "sources", "incompatibilities")
SUMMARY_SECTIONS = (
    "软件与环境", "制作来源", "复用基准", "最终有效步骤", "安装详情",
    "版本差异", "修正经验", "验证与缺口", "修订历史",
)
COVERAGE_AREAS = ("preparation", "dependencies", "installation", "configuration", "startup", "verification", "packaging")
MATERIAL_KINDS = ("config", "script")
MAJOR_RULES = ("numeric", "postgresql", "unknown")
DEFAULT_CONFIG = {
    "output_dir": "deploy/{{software}}/{{version}}",
    "unknown_version": "latest",
    "records": {"runs_dir": "runs", "stages_dir": "stages", "summaries_dir": "summaries",
                "production_file": "production.json", "revision_file": "revision.json", "summary_file": "summary.md"},
    "install_file": "{{software}}-install.md", "verify_file": "{{software}}-verify.md",
    "install_result_file": "{{software}}-install-result.md", "install_issues_file": "{{software}}-install-issues.md",
    "install_meta_file": "{{software}}-install-meta.json",
    "verify_result_file": "{{software}}-verify-result.md", "verify_issues_file": "{{software}}-verify-issues.md",
    "archive_result_file": "{{software}}-archive-result.md", "archive_issues_file": "{{software}}-archive-issues.md",
    "deploy_list_file": "{{software}}-deploy-list.md", "pipeline_result_file": "{{software}}-pipeline-result.md",
    "software_version_rules": {"postgresql": {"rule": "postgresql", "source": "https://www.postgresql.org/support/versioning/"}},
}
STAGE_FILES = {
    "guide": ("install_file", "verify_file"),
    "install": ("install_result_file", "install_meta_file"),
    "verify": ("verify_result_file",),
    "archive": ("archive_result_file", "deploy_list_file"),
    "pipeline": ("pipeline_result_file",),
}


def is_control_file(name):
    return name in (".records.lock", ".commit-sequence.json") or (name.startswith(".record-") and name.endswith(".tmp"))


def describe_contract():
    """Return executable examples, rather than maintaining separate enum copies in prompts."""
    return {
        "schema_version": SCHEMA_VERSION, "stages": STAGES, "statuses": STATUSES, "modes": MODES,
        "listing_statuses": LISTING_STATUSES, "listing_sources": LISTING_SOURCES, "coverage_states": COVERAGE_STATES,
        "environment_fields": ENVIRONMENT_FIELDS, "required_environment": ENVIRONMENT_KEYS,
        "coverage_areas": COVERAGE_AREAS, "material_kinds": MATERIAL_KINDS, "summary_sections": SUMMARY_SECTIONS,
        "major_rules": MAJOR_RULES,
        "major_rule_usage": "Configured software rules take precedence. Confirm numeric first-segment versioning from official sources; use unknown for unsupported special rules and generate from official docs.",
        "path_rule": "All paths are relative to project_root; use returned paths and inputs verbatim.",
        "source": {"run_id": None, "call_id": None, "task_id": None},
        "create": {"target_environment": {"os": "ubuntu", "os_version": "24.04", "architecture": "aarch64",
                   "installation_method": "official-binary", "sources": ["user requirements and resolved target image"]},
                   "source": {"run_id": None, "call_id": None, "task_id": None}},
        "identity": {"actual_version": "1.3.0", "major_source": "official release versioning URL", "major_rule": None, "tag_prefix_confirmed": False},
        "update": {"actual_version": "1.3.0", "major_source": "official release versioning URL", "major_rule": None, "tag_prefix_confirmed": False,
                   "source": {"run_id": None, "call_id": None, "task_id": None}, "target_environment": {}},
        "machine": {"server_alias": None, "instance_id": None, "auth_method": None, "identity_evidence": None},
        "stage": {"status": "passed", "source": "agent execution", "evidence": [],
                  "commands": [{"command": "actual command with secrets replaced", "exit_code": 0, "stdout": "actual result (required verification: VERIFY_PASS: ...)", "stderr": "", "required": True}],
                  "gaps": [], "environment": {}, "actual_version": None, "images": [], "operations": []},
        "execution_scope": {"preconditions": ["observed state and read-only check"], "commands": ["exact corrective command"],
                            "preserve": ["existing installation"], "skip": ["completed steps"],
                            "verification": ["read-only rechecks"], "system_update": False},
        "summary": {"source": "explicit user summary request", "evidence": [], "gaps": [],
                    "stage_revisions": {"guide": None, "install": None, "verify": None, "archive": None, "pipeline": None},
                    "final_image_id": None,
                    "listing": {"status": "unknown", "image_id": None, "feedback": None, "feedback_at": None, "source": USER_LISTING_SOURCE},
                    "coverage": {key: {"state": "missing", "reason": "describe the actual evidence or gap", "evidence": []} for key in COVERAGE_AREAS},
                    "materials": [{"kind": kind, "state": "missing", "reason": "capture necessary files or explain inapplicability", "evidence": []} for kind in MATERIAL_KINDS],
                    "effective_steps": [{"description": "actual final command/configuration", "evidence": []}],
                    "review": {"sections_checked": [], "notes": "check body against execution; separate suggestions from actual work"}},
    }
