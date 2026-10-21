"""Latest deployment state per software/version. Used by the local CLI only."""

from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import tempfile

import yaml

from .contract import (DEFAULT_CONFIG, ENVIRONMENT_KEYS, LISTING_STATUSES, MAJOR_RULES, MODES,
                       SCHEMA_VERSION, STAGE_FILES, STAGE_ISSUES, STAGES, STATUSES, SUMMARY_SECTIONS, USER_LISTING_SOURCE)
from .versions import compare_versions, major_version, numeric_version


class RecordError(ValueError):
    """The current files or supplied values do not satisfy the deployment contract."""


def _require(condition, message):
    if not condition:
        raise RecordError(message)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _name(value):
    _require(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]*", value) and value not in (".", ".."), "invalid software or version path")
    return value


def _read(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        _require(isinstance(data, dict), "state must be an object")
        return data
    except (OSError, ValueError) as exc:
        raise RecordError(f"cannot read current state: {path.name}") from exc


def _atomic(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".deploy-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _environment(value=None):
    _require(value is None or isinstance(value, dict), "environment must be an object")
    result = {key: None for key in ENVIRONMENT_KEYS}
    result.update(value or {})
    for key in ENVIRONMENT_KEYS:
        _require(result[key] is None or isinstance(result[key], str), f"environment {key} must be text or null")
    _require(isinstance(result.get("sources", []), list), "environment sources must be a list")
    return result


def _redact(value):
    if isinstance(value, dict):
        return {key: ("<redacted>" if re.fullmatch(r"(?i)(?:password|passwd|admin_pass|token|secret|api_key|access_key|private_key|ak|sk)", key) and item else _redact(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        value = re.sub(r"-----BEGIN [^-]*PRIVATE KEY-----[\s\S]*?-----END [^-]*PRIVATE KEY-----", "<redacted private key>", value)
        value = re.sub(r"(?i)(https?://[^\s/:]+:)[^\s/@]+(@)", r"\1<redacted>\2", value)
        pattern = r'''(?ix)((?<![\w-])(?:--)?(?:[A-Za-z][A-Za-z0-9]*[_-])*(?:password|passwd|admin[_-]?pass|token|secret|api[_-]?key|access[_-]?key|private[_-]?key|ak|sk)(?![\w-])["']?\s*(?:=|:|\s)\s*)("[^"\n]*"|'[^'\n]*'|[^\s,;}]+)'''
        def replace(match):
            quote = match[2][0] if match[2][0] in ("'", '"') else ""
            return match[1] + quote + "<redacted>" + quote
        return re.sub(pattern, replace, value)
    return value


class VersionStore:
    def __init__(self, project_root, config_path=None):
        self.root = Path(project_root).resolve()
        self.config = deepcopy(DEFAULT_CONFIG)
        config = Path(config_path) if config_path else self.root / "deploy.config.yaml"
        if not config.is_absolute():
            config = self.root / config
        if config.exists():
            supplied = yaml.safe_load(config.read_text(encoding="utf-8")) or {}
            _require(isinstance(supplied, dict), "deploy config must be an object")
            self.config.update(supplied)
        output = self.config["output_dir"]
        _require("{{software}}" in output and "{{version}}" in output, "output_dir needs software and version")
        self.base = self._path(output.split("{{", 1)[0].rstrip("/"))

    def _path(self, relative):
        _require(isinstance(relative, str) and not Path(relative).is_absolute(), "paths must be project relative")
        path = (self.root / relative).resolve()
        _require(path.is_relative_to(self.root) and path != self.root, "path outside project")
        return path

    def software_identity(self, name):
        value = _name(name.strip().lower())
        alias = self.config.get("software_aliases", {}).get(value)
        if alias:
            _require(isinstance(alias, dict) and alias.get("source"), "alias needs official source")
        return {"id": _name(alias["id"] if alias else value), "input_name": name,
                "alias_source": alias["source"] if alias else None}

    def paths(self, software, version):
        software = self.software_identity(software)["id"]
        version = _name(version or self.config["unknown_version"])
        directory = self.config["output_dir"].replace("{{software}}", software).replace("{{version}}", version)
        self._path(directory)
        paths = {"output_dir": directory}
        for key, pattern in self.config.items():
            if key.endswith("_file") and isinstance(pattern, str):
                filename = pattern.replace("{{software}}", software).replace("{{version}}", version)
                _require(Path(filename).name == filename, "configured filenames must be basenames")
                paths[key] = (Path(directory) / filename).as_posix()
        return paths

    def load(self, software, version):
        paths = self.paths(software, version)
        state_path = self._path(paths["state_file"])
        if state_path.exists():
            state = _read(state_path)
            _require(state.get("schema_version") == SCHEMA_VERSION and state.get("paths") == paths, "unsupported or mismatched current state")
            return state
        state = {"schema_version": SCHEMA_VERSION, "software": self.software_identity(software),
                "version": {"requested": version, "actual": None, "major": None, "major_source": None,
                            "major_rule": None, "tag_prefix_confirmed": False},
                "target_environment": {}, "actual_environment": {}, "machine": {}, "cloud": {}, "baseline": None,
                "stages": {}, "summary": None, "paths": paths}
        meta = self._path(paths["install_meta_file"])
        if meta.is_file():
            machine = _read(meta)
            state["machine"] = {key: machine[key] for key in ("server_alias", "instance_id", "auth_method", "ip", "path") if machine.get(key)}
        return state

    def _write(self, state):
        state["updated_at"] = _now()
        state = _redact(state)
        _atomic(self._path(state["paths"]["state_file"]), json.dumps(state, ensure_ascii=False, indent=2) + "\n")
        return state

    def _version(self, software, actual, major_source, major_rule, tag_prefix_confirmed):
        _require(isinstance(actual, str) and actual.lower() not in ("latest", "unknown", ""), "actual version must be verified")
        configured = self.config.get("software_version_rules", {}).get(software)
        rule = major_rule or (configured["rule"] if configured else "unknown")
        _require(rule in MAJOR_RULES, "unsupported major rule; use unknown")
        if configured:
            _require(rule in (configured["rule"], "unknown"), "major rule conflicts with official software rule")
        return {"actual": actual, "major": major_version(actual, rule=rule, tag_prefix_confirmed=tag_prefix_confirmed) if major_source else None,
                "major_source": major_source, "major_rule": rule, "tag_prefix_confirmed": bool(tag_prefix_confirmed)}

    def update(self, software, version, *, actual_version=None, major_source=None, major_rule=None,
               tag_prefix_confirmed=None, target_environment=None, machine=None, cloud=None, baseline=None):
        state = self.load(software, version)
        if actual_version is not None:
            old = state["version"]
            identity = self._version(state["software"]["id"], actual_version, major_source or old.get("major_source"),
                                     major_rule or old.get("major_rule"), old.get("tag_prefix_confirmed", False) if tag_prefix_confirmed is None else tag_prefix_confirmed)
            state["version"].update(identity)
        if target_environment is not None:
            state["target_environment"] = _environment(target_environment)
        if machine is not None:
            _require(isinstance(machine, dict), "machine must be an object")
            allowed = ("server_alias", "instance_id", "auth_method", "ip", "path", "identity_evidence")
            _require(all(key in allowed for key in machine), "unsupported machine fields")
            state["machine"].update({key: val for key, val in machine.items() if val not in (None, "")})
            _atomic(self._path(state["paths"]["install_meta_file"]), json.dumps(_redact(state["machine"]), ensure_ascii=False, indent=2) + "\n")
        if baseline is not None:
            state["baseline"] = baseline
        if cloud is not None:
            _require(isinstance(cloud, dict) and all(key in ("action", "instance_id", "image_id", "job_id", "status") for key in cloud), "unsupported current cloud operation")
            state["cloud"] = {key: value for key, value in cloud.items() if value is not None}
        return self._write(state)

    def begin_stage(self, software, version, stage, *, mode="initial"):
        _require(stage in STAGES and mode in MODES, "invalid stage or mode")
        state = self.load(software, version)
        if stage == "guide" and mode == "initial":
            state.update(machine={}, cloud={}, actual_environment={}, target_environment=state.get("target_environment", {}), baseline=state.get("baseline"),
                         version={"requested": version, "actual": None, "major": None, "major_source": None, "major_rule": None, "tag_prefix_confirmed": False})
        if stage in ("install", "verify"):
            guide = state["stages"].get("guide", {})
            _require(guide.get("status") == "passed", "current guide required")
            if stage == "install":
                _require(guide.get("mode") == mode, "install mode must match current guide")
        if stage == "install" and mode == "remediation":
            guide = state["stages"].get("guide", {})
            _require(guide.get("mode") == mode and guide.get("execution_scope"), "remediation needs current scoped guide")
            _require(state["machine"].get("server_alias"), "remediation needs explicit existing machine")
        if stage == "archive":
            _require(state["stages"].get("verify", {}).get("status") == "passed", "verification gate blocks archive")
        invalidated = STAGES[STAGES.index(stage):]
        obsolete = []
        for name in invalidated:
            state["stages"].pop(name, None)
            for key in (*STAGE_FILES[name], STAGE_ISSUES.get(name)):
                if key and not (key == "install_meta_file" and (stage != "guide" or mode == "remediation")):
                    obsolete.append(self._path(state["paths"][key]))
        state["summary"] = None
        obsolete.append(self._path(state["paths"]["summary_file"]))
        state["mode"] = mode
        state = self._write(state)
        for path in obsolete:
            path.unlink(missing_ok=True)
        return state

    def commit_stage(self, software, version, stage, data):
        _require(stage in STAGES and isinstance(data, dict) and data.get("status") in STATUSES, "valid stage and status required")
        state = self.load(software, version)
        if data["status"] == "passed":
            for key in STAGE_FILES[stage]:
                _require(self._path(state["paths"][key]).is_file(), f"missing current output: {key}")
        if stage == "guide" and data["status"] == "passed":
            header = self._path(state["paths"]["verify_file"]).read_text(encoding="utf-8").split("\n## ", 1)[0]
            _require("> 验证契约: exit-code-v1" in header, "verification guide contract missing")
            if state.get("mode") == "remediation":
                scope = data.get("execution_scope", {})
                _require(all(isinstance(scope.get(key), list) and scope[key] for key in ("preconditions", "commands", "verification")) and type(scope.get("system_update")) is bool, "explicit remediation scope required")
        commands = data.get("commands", [])
        _require(isinstance(commands, list), "commands must be a list")
        for item in commands:
            _require(isinstance(item, dict) and isinstance(item.get("command"), str) and type(item.get("exit_code")) is int, "actual command and exit code required")
        if stage == "install":
            state["actual_environment"] = _environment(data.get("environment"))
            if data["status"] == "passed" and state.get("baseline"):
                _require(self._match_environment(state["actual_environment"], state["baseline"].get("environment", {}))["compatible"], "actual environment differs; revise guide")
            if state.get("mode") == "remediation":
                scope = state["stages"]["guide"]["execution_scope"]
                _require(all(item["command"] in scope["commands"] for item in commands), "execution exceeds remediation scope")
                if not scope["system_update"]:
                    _require(not any(re.search(r"\b(?:dist-upgrade|apt(?:-get)?\s+(?:-[^ ]+\s+)*update)\b", item["command"]) for item in commands), "system update not requested by remediation guide")
        if stage == "verify" and data["status"] == "passed":
            required = [item for item in commands if item.get("required", True)]
            _require(data.get("actual_version") and data["actual_version"] == state["version"]["actual"], "verification must confirm actual target version")
            _require(required and all(item["exit_code"] == 0 and "VERIFY_PASS:" in item.get("stdout", "") for item in required), "required verification lacks positive evidence")
        if stage == "archive" and data["status"] == "passed":
            _require(state["stages"].get("verify", {}).get("status") == "passed", "verification gate blocks archive")
            _require(data.get("images") and all(item.get("image_id") for item in data["images"]), "image identity required")
        state["stages"][stage] = {**data, "mode": state.get("mode", "initial"), "updated_at": _now()}
        return self._write(state)

    def save_summary(self, software, version, data):
        state = self.load(software, version)
        _require(isinstance(data, dict) and isinstance(data.get("body"), str), "summary body required")
        _require(isinstance(data.get("source"), str) and data["source"].strip(), "summary source required")
        for section in SUMMARY_SECTIONS:
            match = re.search(rf"^## {re.escape(section)}\s*$([\s\S]*?)(?=^## |\Z)", data["body"], re.M)
            _require(match and match.group(1).strip(), f"summary section missing or empty: {section}")
        listing = data.get("listing", {})
        _require(listing.get("status") in LISTING_STATUSES and listing.get("source") in (None, USER_LISTING_SOURCE), "listing must reflect explicit user feedback")
        _require(isinstance(data.get("gaps"), list), "explicit gaps required")
        evidence = data.get("evidence", [])
        _require(isinstance(evidence, list) and all(isinstance(item, str) for item in evidence), "evidence must be file paths")
        for relative in evidence:
            path = self._path(relative)
            _require(path.is_file() and path.is_relative_to(self._path(state["paths"]["output_dir"])), "summary evidence must be in current version directory")
        state["summary"] = None
        self._write(state)
        _atomic(self._path(state["paths"]["summary_file"]), _redact(data["body"]))
        state["summary"] = {key: val for key, val in data.items() if key != "body"}
        state["summary"]["updated_at"] = _now()
        self._write(state)
        return {"summary_file": state["paths"]["summary_file"], **self.candidate_report(state)}

    def candidate_report(self, state):
        try:
            summary = state.get("summary") or {}
            _require(summary and self._path(state["paths"]["summary_file"]).is_file(), "current_summary_missing")
            version = state["version"]
            _require(version.get("actual") and version.get("major") is not None and version.get("major_source"), "actual_version_or_major_unknown")
            env = _environment(state["actual_environment"])
            _require(all(env.get(key) for key in ENVIRONMENT_KEYS) and env.get("sources"), "actual_environment_unknown")
            _require(not env.get("incompatibilities"), "known_environment_incompatibility")
            _require(not summary["gaps"], "summary_has_gaps")
            _require(summary.get("evidence"), "summary_evidence_missing")
            listing = summary["listing"]
            image = summary.get("final_image_id")
            _require(listing["status"] == "listed" and listing.get("source") == USER_LISTING_SOURCE and listing.get("feedback") and listing.get("feedback_at"), "human_listing_feedback_missing")
            _require(image and listing.get("image_id") == image, "final_image_ambiguous")
            stages = state["stages"]
            _require(all(stages.get(stage, {}).get("status") == "passed" for stage in ("guide", "install", "verify", "archive")), "current_stages_not_passed")
            _require(stages["install"].get("commands"), "installation_steps_missing")
            _require(stages["verify"].get("actual_version") == version["actual"], "verified_version_mismatch")
            _require(any(item.get("image_id") == image for item in stages["archive"].get("images", [])), "final_image_not_current")
            for keys in STAGE_FILES.values():
                for key in keys:
                    if key != "pipeline_result_file":
                        _require(self._path(state["paths"][key]).is_file(), "current_output_missing")
            _require(all(self._path(path).is_file() for path in summary.get("evidence", [])), "summary_attachment_missing")
            return {"eligible": True, "reasons": []}
        except (RecordError, KeyError, TypeError, AttributeError) as exc:
            return {"eligible": False, "reasons": [str(exc)]}

    @staticmethod
    def _match_environment(target, baseline):
        if not all(target.get(key) for key in ENVIRONMENT_KEYS) or not target.get("sources"):
            return {"compatible": False, "reason": "target_environment_unknown"}
        if target.get("incompatibilities") or baseline.get("incompatibilities"):
            return {"compatible": False, "reason": "known_environment_incompatibility"}
        for key in ENVIRONMENT_KEYS:
            if target[key] != baseline.get(key):
                return {"compatible": False, "reason": "environment_mismatch:" + key}
        return {"compatible": True, "reason": "environment_matches", "adaptations": [key for key in ("base_image", "dependencies") if target.get(key) != baseline.get(key)]}

    def check_environment(self, software, version, environment):
        selected = self.load(software, version).get("baseline")
        return self._match_environment(_environment(environment), selected.get("environment", {})) if selected else {"compatible": True, "reason": "official_documentation"}

    def select_baseline(self, software, version, *, actual_version=None, major_source=None, major_rule=None,
                        tag_prefix_confirmed=False, target_environment=None):
        software_id = self.software_identity(software)["id"]
        target = self._version(software_id, actual_version, major_source, major_rule, tag_prefix_confirmed) if actual_version else {}
        usable, excluded = [], []
        if not target.get("actual") or target.get("major") is None:
            return {"selected": None, "reason": "target_version_or_major_unknown", "excluded": []}
        for path in self.base.glob("*/*/*"):
            if not path.is_file() or not path.name.endswith(self.config["state_file"].replace("{{software}}", "")):
                continue
            try:
                state = _read(path)
                if state.get("schema_version") != SCHEMA_VERSION or state.get("software", {}).get("id") != software_id:
                    continue
                report = self.candidate_report(state)
                reason = report["reasons"][0] if not report["eligible"] else None
                candidate = state["version"]
                comparison = compare_versions(candidate.get("actual"), target["actual"], left_tag_confirmed=candidate.get("tag_prefix_confirmed", False), right_tag_confirmed=tag_prefix_confirmed)
                exact = candidate.get("actual") == target["actual"] or comparison["comparison"] == 0
                if not reason and not exact:
                    reason = comparison["reason"] if comparison["comparison"] is None else "major_version_differs" if candidate.get("major") != target["major"] else "candidate_newer_than_target" if comparison["comparison"] > 0 else None
                match = self._match_environment(_environment(target_environment), state["actual_environment"])
                if not reason and not match["compatible"]:
                    reason = match["reason"]
                if reason:
                    excluded.append({"version": candidate.get("actual"), "reason": reason})
                else:
                    usable.append((exact, candidate, state, match))
            except (RecordError, KeyError, TypeError):
                continue
        if not usable:
            return {"selected": None, "reason": "official_documentation_required", "excluded": excluded}
        chosen = max(usable, key=lambda item: (item[0], numeric_version(item[1]["actual"], tag_prefix_confirmed=item[1].get("tag_prefix_confirmed", False)) or (), item[2]["summary"]["updated_at"]))
        state = chosen[2]
        return {"selected": {"software": software_id, "actual_version": chosen[1]["actual"], "summary_file": state["paths"]["summary_file"],
                             "environment": state["actual_environment"], "adaptations": chosen[3].get("adaptations", [])},
                "reason": "same_version" if chosen[0] else "highest_lower_same_major", "excluded": excluded}
