"""One local entry point for identity, immutable revisions, evidence and reuse."""

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import hashlib
import json
from pathlib import Path
import re
import os
import tempfile
import uuid

import yaml

from .contract import (
    COVERAGE_AREAS, COVERAGE_STATES, DEFAULT_CONFIG, ENVIRONMENT_FIELDS, ENVIRONMENT_KEYS,
    LISTING_SOURCES, LISTING_STATUSES, MAJOR_RULES, MATERIAL_KINDS, MODES, SCHEMA_VERSION, STAGE_FILES, STAGES, STATUSES, SUMMARY_SECTIONS, USER_LISTING_SOURCE,
)
from .versions import compare_versions, major_version, numeric_version


class RecordError(ValueError):
    """A record is ambiguous, incomplete, stale, or violates the persisted contract."""


def _now():
    return datetime.now(timezone.utc).isoformat()


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]*", value) or value in (".", ".."):
        raise RecordError("invalid path identity")
    return value


def _require(condition, message):
    if not condition:
        raise RecordError(message)


def _read(path):
    try:
        with path.open(encoding="utf-8") as stream:
            value = json.load(stream)
        _require(isinstance(value, dict), f"not an object: {path.name}")
        return value
    except (OSError, ValueError) as exc:
        raise RecordError(f"cannot read record: {path.name}") from exc


def _atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".record-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


_SECRET_NAME = r"(?:password|passwd|admin[_-]?pass|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|ak|sk)"


def _redact_text(value):
    value = re.sub(r"-----BEGIN [^-]*PRIVATE KEY-----[\s\S]*?-----END [^-]*PRIVATE KEY-----", "<redacted private key>", value)
    value = re.sub(r"(?i)(https?://[^\s/:]+:)[^\s/@]+(@)", r"\1<redacted>\2", value)
    name = rf"(?<![\w-])(?:--)?(?:[A-Za-z][A-Za-z0-9]*[_-])*{_SECRET_NAME}(?![\w-])"
    def replacement(match):
        original = match.group(2)
        quote = original[0] if original[0] in ("'", '"') else ""
        return match.group(1) + quote + "<redacted>" + quote

    value = re.sub(rf'''(?ix)({name}["']?\s*(?:=|:|\s)\s*)("[^"\n]*"|'[^'\n]*'|[^\s,;}}]+)''', replacement, value)
    value = re.sub(r"(?i)(Authorization\s*:\s*(?:Bearer|Basic)\s+)\S+", r"\1<redacted>", value)
    return value


def _redact(value):
    if isinstance(value, dict):
        return {key: ("<redacted>" if re.fullmatch(_SECRET_NAME, str(key), re.I) and item else _redact(item))
                for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return _redact_text(value) if isinstance(value, str) else value


def _environment(value=None):
    _require(value is None or isinstance(value, dict), "environment must be an object")
    value = value or {}
    result = {key: value.get(key) for key in ENVIRONMENT_FIELDS}
    for key in ("sources", "incompatibilities"):
        result[key] = result[key] or []
        _require(isinstance(result[key], list), f"environment {key} must be a list")
    result["dependencies"] = result["dependencies"] or {}
    _require(isinstance(result["dependencies"], dict), "environment dependencies must be an object")
    for key in ENVIRONMENT_KEYS:
        _require(result[key] is None or isinstance(result[key], str), f"environment {key} must be text or null")
    return result


class RecordStore:
    def __init__(self, project_root, config_path=None):
        self.root = Path(project_root).resolve()
        self.config = deepcopy(DEFAULT_CONFIG)
        path = Path(config_path) if config_path else self.root / "deploy.config.yaml"
        if not path.is_absolute():
            path = self.root / path
        if path.exists():
            with path.open(encoding="utf-8") as stream:
                supplied = yaml.safe_load(stream) or {}
            _require(isinstance(supplied, dict), "deploy config must be an object")
            self.config.update({key: val for key, val in supplied.items() if key != "records"})
            self.config["records"].update(supplied.get("records") or {})
        output = self.config["output_dir"]
        _require("{{software}}" in output and "{{version}}" in output, "output_dir needs software and version")
        prefix = output.split("{{", 1)[0].rstrip("/")
        _require(bool(prefix), "output_dir needs a fixed root")
        self.base = self._path(prefix)
        for name in self.config["records"].values():
            _identifier(name)

    def _path(self, relative):
        path = Path(relative)
        _require(not path.is_absolute(), "record paths must be relative to project root")
        resolved = (self.root / path).resolve()
        _require(resolved.is_relative_to(self.root) and resolved != self.root, "path outside project root")
        return resolved

    def _relative(self, path):
        return path.relative_to(self.root).as_posix()

    @contextmanager
    def _locked(self):
        self.base.mkdir(parents=True, exist_ok=True)
        with (self.base / ".records.lock").open("a") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def _production_paths(self):
        name = self.config["records"]["production_file"]
        runs_dir = self.config["records"]["runs_dir"]
        return self.base.glob(f"**/{runs_dir}/*/{name}") if self.base.exists() else []

    def list_productions(self, software=None, version=None, call_id=None):
        software_id = self.software_identity(software)["id"] if software else None
        records = []
        for path in self._production_paths():
            try:
                record = _read(path)
                self._validate_production(record, path)
            except RecordError:
                continue
            if software_id and record["software"]["id"] != software_id:
                continue
            if version and record["version"]["actual"] != version:
                continue
            if call_id and not any(source.get("call_id") == call_id for source in record["sources"]):
                continue
            records.append(record)
        return sorted(records, key=lambda record: (record["created_at"], record["production_id"]))

    def _validate_production(self, record, path):
        try:
            _require(record["schema_version"] == SCHEMA_VERSION and record["record_type"] == "production", "unsupported production record")
            _require(record["production_id"] == path.parent.name, "production identity mismatch")
            _require(self._path(record["paths"]["production_file"]) == path.resolve(), "production location mismatch")
            _require(isinstance(record["software"]["id"], str) and isinstance(record["version"], dict), "invalid production identity")
            _require(isinstance(record["machine"], dict) and isinstance(record["sources"], list), "invalid production linkage")
            _require(all(isinstance(item, dict) and all(item.get(key) is None or isinstance(item.get(key), str) for key in ("call_id", "run_id", "task_id")) for item in record["sources"]), "invalid production sources")
            _require(isinstance(record["created_at"], str), "production creation time missing")
            for key in ("actual", "requested", "major_source"):
                _require(record["version"].get(key) is None or isinstance(record["version"][key], str), "invalid version identity")
            _require(type(record["version"].get("tag_prefix_confirmed")) is bool, "invalid version normalization")
            _environment(record["target_environment"])
            _environment(record["actual_environment"])
        except (KeyError, TypeError) as exc:
            raise RecordError("invalid production structure") from exc

    def get(self, production_id):
        _identifier(production_id)
        matches = [item for item in self.list_productions() if item["production_id"] == production_id]
        _require(len(matches) == 1, "production missing or ambiguous; specify an existing production_id")
        return matches[0]

    def software_identity(self, name):
        _require(isinstance(name, str) and bool(name.strip()), "software name required")
        normalized = name.strip().lower()
        alias = (self.config.get("software_aliases") or {}).get(normalized)
        if alias:
            _require(isinstance(alias, dict) and alias.get("source"), "software alias requires official source")
            canonical = alias["id"]
        else:
            canonical = normalized
        _identifier(canonical)
        return {"id": canonical, "input_name": name, "original_directory": normalized,
                "alias_source": alias["source"] if alias else None}

    def create(self, software, version=None, *, target_environment=None, source=None, actual_version=None,
               major_source=None, major_rule=None, tag_prefix_confirmed=False, derived_from=None):
        identity = self.software_identity(software)
        requested = version or self.config["unknown_version"]
        _identifier(requested)
        production_id = "prod-" + uuid.uuid4().hex
        version_dir = self._path(self.config["output_dir"].replace("{{software}}", identity["id"]).replace("{{version}}", requested))
        run_dir = version_dir / self.config["records"]["runs_dir"] / production_id
        record = {
            "schema_version": SCHEMA_VERSION, "record_type": "production", "production_id": production_id,
            "created_at": _now(), "software": identity,
            "version": {"requested": requested, "actual": None, "major": None, "major_source": None, "tag_prefix_confirmed": False},
            "target_environment": _environment(target_environment), "actual_environment": _environment(),
            "machine": {"server_alias": None, "instance_id": None, "auth_method": None}, "sources": [],
            "baseline": {"selected": None, "reason": "not_selected", "excluded": []}, "derived_from": derived_from,
            "paths": {"version_dir": self._relative(version_dir), "run_dir": self._relative(run_dir),
                      "production_file": self._relative(run_dir / self.config["records"]["production_file"])},
        }
        if actual_version:
            self._set_version(record, actual_version, major_source, tag_prefix_confirmed, major_rule)
        if source:
            record["sources"].append(self._source(source))
        if derived_from:
            self.get(derived_from)
        with self._locked():
            run_dir.mkdir(parents=True, exist_ok=False)
            _atomic_json(self._path(record["paths"]["production_file"]), _redact(record))
        return self.get(production_id)

    @staticmethod
    def _source(source):
        _require(isinstance(source, dict), "source must be an object")
        result = {key: source.get(key) for key in ("run_id", "call_id", "task_id")}
        _require(all(value is None or isinstance(value, str) for value in result.values()), "source IDs must be text or null")
        return result

    def _set_version(self, record, actual, major_source, tag_prefix_confirmed, major_rule=None):
        _require(isinstance(actual, str) and bool(actual.strip()), "actual version must be explicit")
        _require(actual.lower() not in ("latest", "unknown", "unknown_version"), "actual version cannot be a placeholder")
        configured = (self.config.get("software_version_rules") or {}).get(record["software"]["id"])
        previous = record["version"]
        if major_source is None:
            major_source = previous.get("major_source")
        if tag_prefix_confirmed is None:
            tag_prefix_confirmed = previous.get("tag_prefix_confirmed", False)
        rule = major_rule or previous.get("major_rule") or (configured["rule"] if configured else "numeric")
        if configured:
            _require(isinstance(configured, dict) and configured.get("source"), "software version rule needs an official source")
            _require(rule in ("unknown", configured.get("rule")), "major rule conflicts with verified software rule")
        _require(rule in MAJOR_RULES, "unsupported major rule; use unknown until verified")
        major = major_version(actual, rule=rule, tag_prefix_confirmed=tag_prefix_confirmed) if major_source else None
        record["version"].update(actual=actual, major=major, major_rule=rule,
                                 major_source=major_source, tag_prefix_confirmed=bool(tag_prefix_confirmed))

    def update(self, production_id, *, source=None, target_environment=None, actual_version=None,
               major_source=None, major_rule=None, tag_prefix_confirmed=None):
        with self._locked():
            record = self.get(production_id)
            if source:
                item = self._source(source)
                if item not in record["sources"]:
                    record["sources"].append(item)
            if target_environment is not None:
                record["target_environment"] = _environment(target_environment)
            if actual_version is not None:
                previous = record["version"]["actual"]
                _require(previous in (None, actual_version), "actual version changed; start a linked new production")
                self._set_version(record, actual_version, major_source, tag_prefix_confirmed, major_rule)
            _atomic_json(self._path(record["paths"]["production_file"]), _redact(record))
            return record

    def set_machine(self, production_id, machine):
        _require(isinstance(machine, dict), "machine must be an object")
        with self._locked():
            record = self.get(production_id)
            for key, value in machine.items():
                _require(key in ("server_alias", "instance_id", "auth_method", "ip", "path", "identity_evidence"), "unsupported machine field")
                if value is None or value == "":
                    continue
                old = record["machine"].get(key)
                if key in ("instance_id", "server_alias"):
                    _require(not old or old == value, "machine identity changed; start a linked new production")
                record["machine"][key] = value
            _atomic_json(self._path(record["paths"]["production_file"]), _redact(record))
            return record["machine"]

    def _revision_path(self, production, revision_id, *, summary=False):
        _identifier(revision_id)
        if summary:
            parent = self._path(production["paths"]["version_dir"]) / self.config["records"]["summaries_dir"]
        else:
            parent = self._path(production["paths"]["run_dir"]) / self.config["records"]["stages_dir"]
        return parent / revision_id / self.config["records"]["revision_file"]

    def revision(self, production_id, revision_id, *, summary=False, committed=True):
        production = self.get(production_id)
        result = _read(self._revision_path(production, revision_id, summary=summary))
        _require(result.get("schema_version") == SCHEMA_VERSION and result.get("production_id") == production_id and result.get("revision_id") == revision_id, "revision identity mismatch")
        _require(result.get("record_type") == ("summary" if summary else "stage"), "revision type mismatch")
        self._validate_revision(result)
        if committed:
            _require(result.get("state") == "committed", "revision not committed")
            self._check_manifest(result)
        return result

    @staticmethod
    def _validate_revision(revision):
        _require(revision.get("state") in ("draft", "committed") and isinstance(revision.get("paths"), dict), "invalid revision state or paths")
        _require(isinstance(revision.get("created_at"), str), "revision creation time missing")
        if revision.get("record_type") == "stage":
            _require(revision.get("stage") in STAGES and revision.get("mode") in MODES and isinstance(revision.get("inputs"), dict), "invalid stage revision")
        if revision["state"] == "committed":
            _require(type(revision.get("commit_sequence")) is int and revision["commit_sequence"] > 0 and isinstance(revision.get("committed_at"), str), "revision commit identity missing")
            _require(isinstance(revision.get("data"), dict) and isinstance(revision.get("identity"), dict), "revision data missing")

    def stages(self, production_id, *, include_drafts=False):
        production = self.get(production_id)
        parent = self._path(production["paths"]["run_dir"]) / self.config["records"]["stages_dir"]
        result = []
        for path in parent.glob(f"*/{self.config['records']['revision_file']}"):
            try:
                revision = self.revision(production_id, path.parent.name, committed=not include_drafts)
                if revision["state"] == "committed" and include_drafts:
                    self._check_manifest(revision)
                result.append(revision)
            except RecordError:
                continue
        return sorted(result, key=lambda item: (item["state"] == "draft", item.get("commit_sequence", 0), item["created_at"]))

    def _stage_paths(self, production, directory):
        values = {"revision_dir": self._relative(directory), "evidence_dir": self._relative(directory / "evidence"),
                  "stage_result_file": self._relative(directory / "stage-result.md"),
                  "revision_file": self._relative(directory / self.config["records"]["revision_file"])}
        for key in DEFAULT_CONFIG:
            if key.endswith("_file") and key in self.config:
                name = self.config[key].replace("{{software}}", production["software"]["id"]).replace("{{version}}", production["version"]["requested"])
                _require(Path(name).name == name and name not in (".", ".."), "stage filenames must be basenames")
                values[key] = self._relative(directory / name)
        return values

    def begin_stage(self, production_id, stage, *, guide_revision=None, mode="initial", inputs=None):
        _require(stage in STAGES, "invalid stage")
        _require(mode in MODES, "invalid execution mode")
        production = self.get(production_id)
        stages = self.stages(production_id)
        latest = {item["stage"]: item for item in stages}
        links = dict(inputs or {})
        if stage != "guide":
            guide_revision = guide_revision or links.get("guide_revision")
            _require(bool(guide_revision), "explicit guide_revision required")
            guide = self.revision(production_id, guide_revision)
            _require(guide["stage"] == "guide" and (stage == "pipeline" or guide["data"]["status"] == "passed"), "valid committed guide required")
            links["guide_revision"] = guide_revision
            links["install_file"] = guide["paths"]["install_file"]
            links["verify_file"] = guide["paths"]["verify_file"]
            if stage == "install":
                _require(mode == guide["mode"], "install mode must match the current guide; no fallback to full installation")
            for upstream in ("install", "verify"):
                match = [item for item in stages if item["stage"] == upstream and item.get("guide_revision") == guide_revision]
                chosen = links.get(upstream + "_revision")
                if chosen:
                    result = self.revision(production_id, chosen)
                    _require(result["stage"] == upstream and result.get("guide_revision") == guide_revision, "upstream revision mismatch")
                else:
                    result = match[-1] if match else None
                if result:
                    links[upstream + "_revision"] = result["revision_id"]
                    for key in STAGE_FILES[upstream]:
                        links[key] = result["paths"][key]
            if stage == "pipeline":
                archives = [item for item in stages if item["stage"] == "archive" and item.get("guide_revision") == guide_revision]
                archive_id = links.get("archive_revision") or (archives[-1]["revision_id"] if archives else None)
                if archive_id:
                    archive = self.revision(production_id, archive_id)
                    _require(archive["stage"] == "archive" and archive.get("guide_revision") == guide_revision, "pipeline archive revision mismatch")
                    links["archive_revision"] = archive_id
                    for key in STAGE_FILES["archive"]:
                        links[key] = archive["paths"][key]
            if stage == "archive":
                _require(links.get("install_revision") and links.get("verify_revision"), "archive needs current install and verify revisions")
                verify = self.revision(production_id, links["verify_revision"])
                _require(verify["data"]["status"] == "passed", "verification gate blocks archive")
                _require(verify["inputs"].get("install_revision") == links["install_revision"], "verification does not describe current install")
                links["previous_archives"] = [item["revision_id"] for item in self.stages(production_id, include_drafts=True) if item["stage"] == "archive"]
            if mode == "remediation" and stage == "install":
                _require(guide.get("mode") == "remediation" and guide["data"].get("execution_scope"), "remediation needs scoped guide")
                _require(production["machine"].get("server_alias") and production["machine"].get("instance_id"), "remediation needs explicit existing machine identity")
        revision_id = stage + "-" + uuid.uuid4().hex
        directory = self._revision_path(production, revision_id).parent
        result = {"schema_version": SCHEMA_VERSION, "record_type": "stage", "state": "draft",
                  "production_id": production_id, "revision_id": revision_id, "stage": stage,
                  "mode": mode, "guide_revision": guide_revision, "created_at": _now(),
                  "previous_revision": latest[stage]["revision_id"] if stage in latest else None,
                  "inputs": links, "paths": self._stage_paths(production, directory)}
        with self._locked():
            (directory / "evidence").mkdir(parents=True, exist_ok=False)
            _atomic_json(directory / self.config["records"]["revision_file"], result)
        return result

    def _validate_scope(self, scope):
        _require(isinstance(scope, dict), "remediation execution_scope required")
        for key in ("preconditions", "commands", "preserve", "skip", "verification"):
            _require(isinstance(scope.get(key), list), f"execution_scope.{key} must be a list")
        _require(scope["preconditions"] and scope["commands"] and scope["verification"], "remediation needs preconditions, commands and rechecks")
        _require(isinstance(scope.get("system_update"), bool), "execution_scope.system_update must be explicit")

    def _freeze_files(self, revision, evidence):
        directory = self._path(revision["paths"]["revision_dir"])
        own = []
        for path in directory.rglob("*"):
            if not path.is_file() or path.name == self.config["records"]["revision_file"] or path.name.startswith(".record-"):
                continue
            _require(not path.is_symlink() and path.resolve().is_relative_to(directory), "evidence symlink not allowed")
            try:
                original = path.read_text(encoding="utf-8")
            except UnicodeDecodeError as exc:
                raise RecordError("execution evidence must be text; snapshot configuration and scripts") from exc
            redacted = _redact_text(original)
            if redacted != original:
                path.write_text(redacted, encoding="utf-8")
            with path.open("rb") as stream:
                os.fsync(stream.fileno())
            own.append(self._relative(path))
        stable = {}
        for previous in self.stages(revision["production_id"]):
            stable.update(previous["manifest"])
        for previous in self.summaries(production_id=revision["production_id"], validate=False):
            try:
                self._check_manifest(previous)
                stable.update(previous["manifest"])
            except RecordError:
                continue
        selected = self.get(revision["production_id"])["baseline"].get("selected")
        if selected:
            try:
                baseline = self.revision(selected["production_id"], selected["summary_revision"], summary=True)
                if self.candidate_report(baseline)["eligible"]:
                    stable.update(baseline["manifest"])
            except RecordError:
                pass  # Own failure evidence remains committable when the old baseline is lost.
        _require(isinstance(evidence, list) and all(isinstance(item, str) for item in evidence), "evidence must be paths")
        for relative in evidence:
            _require(relative in own or relative in stable, "evidence must be a stable snapshot in a committed revision or the current draft")
            if relative in stable:
                _require(self._hash(relative) == stable[relative], "referenced evidence changed")
        return {relative: self._hash(relative) for relative in sorted(set(own + evidence))}

    def _hash(self, relative):
        path = self._path(relative)
        _require(path.is_file(), f"referenced file missing: {relative}")
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _check_manifest(self, revision):
        manifest = revision.get("manifest")
        _require(isinstance(manifest, dict) and bool(manifest), "revision has no evidence manifest")
        for relative, digest in manifest.items():
            _require(self._hash(relative) == digest, f"evidence modified after commit: {relative}")

    def _commit(self, production, revision, data, *, summary=False):
        _require(revision["state"] == "draft", "committed revisions are immutable; begin a new revision")
        sequence_file = self.base / ".commit-sequence.json"
        sequence = _read(sequence_file)["value"] if sequence_file.exists() else 0
        _atomic_json(sequence_file, {"value": sequence + 1})
        revision.update(state="committed", committed_at=_now(), commit_sequence=sequence + 1, data=_redact(data))
        revision["identity"] = {"software": deepcopy(production["software"]), "version": deepcopy(production["version"])}
        revision["environment"] = deepcopy(production["actual_environment"])
        revision["baseline"] = deepcopy(production["baseline"])
        _atomic_json(self._revision_path(production, revision["revision_id"], summary=summary), revision)
        return revision

    def commit_stage(self, production_id, revision_id, data):
        _require(isinstance(data, dict) and data.get("status") in STATUSES, "stage status required")
        _require(isinstance(data.get("source"), str) and data["source"].strip(), "stage source required")
        with self._locked():
            production = self.get(production_id)
            revision = self.revision(production_id, revision_id, committed=False)
            _require(revision["state"] == "draft", "committed revisions are immutable")
            stage = revision["stage"]
            if data["status"] == "passed":
                for key in STAGE_FILES[stage]:
                    _require(self._path(revision["paths"][key]).is_file(), f"missing required output: {key}")
            else:
                outputs = (*STAGE_FILES[stage], "stage_result_file")
                _require(any(self._path(revision["paths"][key]).is_file() for key in outputs), "failed stage still needs its own evidence/report")
            if stage == "guide" and data["status"] == "passed":
                text = self._path(revision["paths"]["verify_file"]).read_text(encoding="utf-8")
                header = text.split("\n## ", 1)[0]
                _require("> 验证契约: exit-code-v1" in header, "verification guide missing exit-code-v1 contract")
                if revision["mode"] == "remediation":
                    self._validate_scope(data.get("execution_scope"))
            commands = data.get("commands", [])
            _require(isinstance(commands, list), "commands must be a list")
            for item in commands:
                _require(isinstance(item, dict) and isinstance(item.get("command"), str) and type(item.get("exit_code")) is int, "actual commands need text and exit code")
                _require(type(item.get("required", True)) is bool, "command required must be boolean")
            if stage == "install":
                if data.get("environment"):
                    production["actual_environment"] = _environment(data["environment"])
                if production["baseline"]["selected"] and data["status"] == "passed":
                    compatible = self.check_environment(production_id, production["actual_environment"])
                    _require(compatible["compatible"], "actual environment no longer matches baseline; revise guide")
                if revision["mode"] == "remediation":
                    guide = self.revision(production_id, revision["guide_revision"])
                    scope = guide["data"]["execution_scope"]
                    allowed = set(scope["commands"])
                    _require(all(item["command"] in allowed for item in commands), "remediation executed outside guide scope")
                    if not scope["system_update"]:
                        _require(not any(re.search(r"\b(?:dist-upgrade|apt(?:-get)?\s+(?:-[^ ]+\s+)*update)\b", item["command"]) for item in commands), "system update not authorized by remediation guide")
            if stage == "verify" and data["status"] == "passed":
                _require(data.get("actual_version") == production["version"]["actual"] and data.get("actual_version"), "verification must confirm the actual target version")
                required = [item for item in commands if item.get("required", True)]
                _require(required and all(item["exit_code"] == 0 and "VERIFY_PASS:" in item.get("stdout", "") for item in required), "passing verification needs successful required checks with VERIFY_PASS evidence")
            if stage == "archive":
                images = data.get("images", [])
                _require(isinstance(images, list) and all(isinstance(item, dict) and item.get("image_id") for item in images), "images need explicit identities")
                if data["status"] == "passed":
                    _require(bool(images), "archive success needs image identity")
            evidence = data.get("evidence", [])
            revision["manifest"] = self._freeze_files(revision, evidence)
            _atomic_json(self._path(production["paths"]["production_file"]), _redact(production))
            return self._commit(production, revision, data)

    def snapshot(self, production_id, revision_id, source, name, *, summary=False):
        with self._locked():
            revision = self.revision(production_id, revision_id, summary=summary, committed=False)
            _require(revision["state"] == "draft", "snapshot requires a draft")
            _identifier(name)
            destination = self._path(revision["paths"]["evidence_dir"]) / name
            _require(not destination.exists(), "snapshot already exists")
            text = Path(source).read_text(encoding="utf-8")
            destination.write_text(_redact_text(text), encoding="utf-8")
            return self._relative(destination)

    def begin_summary(self, production_id):
        production = self.get(production_id)
        revision_id = "summary-" + uuid.uuid4().hex
        previous = self.summaries(production_id=production_id)
        directory = self._revision_path(production, revision_id, summary=True).parent
        result = {"schema_version": SCHEMA_VERSION, "record_type": "summary", "state": "draft",
                  "production_id": production_id, "revision_id": revision_id, "created_at": _now(),
                  "previous_revision": previous[-1]["revision_id"] if previous else None,
                  "paths": {"revision_dir": self._relative(directory), "evidence_dir": self._relative(directory / "evidence"),
                            "revision_file": self._relative(directory / self.config["records"]["revision_file"]),
                            "summary_file": self._relative(directory / self.config["records"]["summary_file"])}}
        with self._locked():
            (directory / "evidence").mkdir(parents=True, exist_ok=False)
            _atomic_json(directory / self.config["records"]["revision_file"], result)
        return result

    def summaries(self, software=None, production_id=None, *, validate=True):
        productions = [self.get(production_id)] if production_id else self.list_productions(software=software)
        result, seen = [], set()
        for production in productions:
            directory = self._path(production["paths"]["version_dir"]) / self.config["records"]["summaries_dir"]
            for path in directory.glob(f"*/{self.config['records']['revision_file']}"):
                if path in seen:
                    continue
                seen.add(path)
                try:
                    item = _read(path)
                    if item.get("record_type") != "summary" or item.get("state") != "committed" or item.get("schema_version") != SCHEMA_VERSION:
                        continue
                    self._validate_revision(item)
                    if production_id and item.get("production_id") != production_id:
                        continue
                    if validate:
                        self._check_manifest(item)
                    result.append(item)
                except RecordError:
                    continue
        return sorted(result, key=lambda item: item.get("commit_sequence", 0))

    def _summary_structure(self, data):
        _require(isinstance(data, dict), "summary data must be an object")
        _require(isinstance(data.get("source"), str) and data["source"].strip(), "manual summary source required")
        listing = data.get("listing", {})
        _require(isinstance(listing, dict) and listing.get("status") in LISTING_STATUSES, "explicit listing status required")
        _require(listing.get("source") is None or listing.get("source") in LISTING_SOURCES, "listing source must use schema listing_sources; preserve the actual feedback in feedback")
        _require(isinstance(data.get("gaps"), list), "explicit gaps list required")
        _require(isinstance(data.get("stage_revisions"), dict), "stage_revisions must be explicit")
        for area in COVERAGE_AREAS:
            value = data.get("coverage", {}).get(area, {})
            _require(isinstance(value, dict) and value.get("state") in COVERAGE_STATES and value.get("reason"), f"coverage.{area} requires a state and explanation")
            _require(isinstance(value.get("evidence"), list), "coverage evidence must be paths")
        materials = data.get("materials")
        _require(isinstance(materials, list), "materials must be explicit")
        for kind in MATERIAL_KINDS:
            _require(any(isinstance(item, dict) and item.get("kind") == kind for item in materials), "capture or explain each material kind")
        for item in materials:
            _require(item.get("kind") in MATERIAL_KINDS and item.get("state") in COVERAGE_STATES and item.get("reason") and isinstance(item.get("evidence"), list), "invalid material evidence")
        steps = data.get("effective_steps")
        _require(isinstance(steps, list) and all(isinstance(item, dict) and item.get("description") and isinstance(item.get("evidence"), list) for item in steps), "effective steps must cite actual evidence")
        review = data.get("review", {})
        _require(isinstance(review, dict) and set(review.get("sections_checked", [])) == set(SUMMARY_SECTIONS) and review.get("notes"), "check all summary sections against the evidence")

    def commit_summary(self, production_id, revision_id, data):
        self._summary_structure(data)
        with self._locked():
            production = self.get(production_id)
            revision = self.revision(production_id, revision_id, summary=True, committed=False)
            _require(revision["state"] == "draft", "committed revisions are immutable")
            body = self._path(revision["paths"]["summary_file"])
            _require(body.is_file(), "summary body missing")
            text = body.read_text(encoding="utf-8")
            for section in SUMMARY_SECTIONS:
                _require(re.search(rf"^## {re.escape(section)}\s*\n(?:(?!^## ).)*\S", text, re.M | re.S), f"summary section missing or empty: {section}")
            evidence = list(data.get("evidence", []))
            for match in re.finditer(r"\[[^\]]*\]\((?:<([^>]+)>|([^\s)]+))\)", text):
                target = match.group(1) or match.group(2)
                if target.startswith("#") or re.match(r"[A-Za-z][A-Za-z0-9+.-]*:", target):
                    continue
                target = target.split("#", 1)[0]
                _require(not Path(target).is_absolute(), "summary local links must be relative and stable")
                linked = (body.parent / target).resolve()
                _require(linked.is_relative_to(self.root), "summary link outside project root")
                evidence.append(self._relative(linked))
            for value in data["coverage"].values():
                evidence.extend(value["evidence"])
            for item in data["materials"] + data["effective_steps"]:
                evidence.extend(item["evidence"])
            for stage, linked_id in data["stage_revisions"].items():
                _require(stage in STAGES, "unknown linked stage")
                if linked_id:
                    linked = self.revision(production_id, linked_id)
                    _require(linked["stage"] == stage, "linked stage type mismatch")
            revision["manifest"] = self._freeze_files(revision, evidence)
            # An unfinished but honest summary is committable; reuse is a separate check.
            return self._commit(production, revision, data, summary=True)

    def candidate_report(self, summary):
        reasons = []
        try:
            _require(summary.get("state") == "committed", "summary_uncommitted")
            self._check_manifest(summary)
            data = summary["data"]
            self._summary_structure(data)
            production = self.get(summary["production_id"])
            identity = summary["identity"]
            version = identity["version"]
            _require(bool(version.get("actual")), "actual_version_unknown")
            _require(bool(version.get("major_source")), "major_version_basis_unknown")
            _require(version.get("major") is not None, "major_version_rule_unknown")
            environment = summary["environment"]
            _require(all(environment.get(key) for key in ENVIRONMENT_KEYS) and environment.get("sources"), "actual_environment_incomplete")
            _require(not environment.get("incompatibilities"), "known_environment_incompatibility")
            listing = data["listing"]
            _require(listing["status"] == "listed" and listing.get("source") == USER_LISTING_SOURCE and listing.get("feedback") and listing.get("feedback_at"), "human_listing_feedback_missing")
            _require(data.get("final_image_id") and listing.get("image_id") == data["final_image_id"], "listing_final_image_ambiguous")
            _require(not data["gaps"], "critical_material_gaps")
            for area, value in data["coverage"].items():
                _require(value["state"] != "missing", f"coverage_missing:{area}")
                _require(value["state"] != "recorded" or value["evidence"], f"coverage_evidence_missing:{area}")
            for required in ("installation", "verification", "packaging"):
                _require(data["coverage"][required]["state"] == "recorded", f"required_coverage_missing:{required}")
            for item in data["materials"]:
                _require(item["state"] != "missing" and (item["state"] != "recorded" or item["evidence"]), "necessary_material_missing")
            _require(data["effective_steps"] and all(item["evidence"] for item in data["effective_steps"]), "final_effective_steps_missing")
            revisions = data["stage_revisions"]
            _require(all(revisions.get(stage) for stage in STAGES), "stage_provenance_missing")
            linked = {stage: self.revision(production["production_id"], revisions[stage]) for stage in STAGES}
            _require(all(item["stage"] == stage and item["data"]["status"] == "passed" for stage, item in linked.items()), "final_stage_not_passed")
            guide_id = revisions["guide"]
            _require(all(item["guide_revision"] == guide_id for stage, item in linked.items() if stage != "guide"), "final_guide_revision_mismatch")
            _require(linked["verify"]["inputs"].get("install_revision") == revisions["install"], "verification_install_mismatch")
            _require(linked["archive"]["inputs"].get("verify_revision") == revisions["verify"] and linked["archive"]["inputs"].get("install_revision") == revisions["install"], "image_verification_mismatch")
            _require(all(linked["pipeline"]["inputs"].get(stage + "_revision") == revisions[stage] for stage in ("install", "verify", "archive")), "pipeline_revision_mismatch")
            _require(linked["verify"]["data"].get("actual_version") == version["actual"], "verified_version_mismatch")
            _require(linked["install"]["data"].get("commands"), "installation_execution_evidence_missing")
            _require(_environment(linked["install"]["data"].get("environment")) == environment, "final_environment_evidence_mismatch")
            _require(any(item["image_id"] == data["final_image_id"] for item in linked["archive"]["data"].get("images", [])), "final_image_not_produced")
        except (RecordError, KeyError, TypeError, AttributeError) as exc:
            reasons.append(str(exc))
        return {"eligible": not reasons, "reasons": reasons}

    @staticmethod
    def _environment_match(target, candidate):
        if not all(target.get(key) for key in ENVIRONMENT_KEYS) or not target.get("sources"):
            return False, "target_environment_unknown", []
        if target.get("incompatibilities") or candidate.get("incompatibilities"):
            return False, "known_environment_incompatibility", []
        for key in ENVIRONMENT_KEYS:
            if target[key] != candidate.get(key):
                return False, f"environment_mismatch:{key}", []
        adaptations = [key for key in ("base_image", "dependencies") if target.get(key) != candidate.get(key)]
        return True, "environment_matches", adaptations

    def check_environment(self, production_id, environment):
        production = self.get(production_id)
        selected = production["baseline"].get("selected")
        if not selected:
            return {"compatible": True, "reason": "official_documentation", "adaptations": []}
        summary = self.revision(selected["production_id"], selected["summary_revision"], summary=True)
        matches, reason, adaptations = self._environment_match(_environment(environment), summary["environment"])
        return {"compatible": matches, "reason": reason, "adaptations": adaptations}

    def select_baseline(self, production_id):
        with self._locked():
            production = self.get(production_id)
            target_version = production["version"]
            excluded, usable = [], []
            for summary in self.summaries(software=production["software"]["id"], validate=False):
                report = self.candidate_report(summary)
                reason = report["reasons"][0] if not report["eligible"] else None
                identity = summary.get("identity", {})
                version = identity.get("version", {})
                adaptations = []
                exact = False
                if not reason:
                    if not target_version["actual"] or not target_version["major_source"] or target_version["major"] is None:
                        reason = "target_version_or_major_unknown"
                    else:
                        comparison = compare_versions(version.get("actual"), target_version["actual"],
                            left_tag_confirmed=version.get("tag_prefix_confirmed", False), right_tag_confirmed=target_version["tag_prefix_confirmed"])
                        exact = version.get("actual") == target_version["actual"] or comparison["comparison"] == 0
                        if not exact:
                            if comparison["comparison"] is None:
                                reason = comparison["reason"]
                            elif version.get("major") != target_version["major"]:
                                reason = "major_version_differs"
                            elif comparison["comparison"] > 0:
                                reason = "candidate_newer_than_target"
                if not reason:
                    matches, env_reason, adaptations = self._environment_match(production["target_environment"], summary.get("environment", {}))
                    if not matches:
                        reason = env_reason
                if reason:
                    excluded.append({"summary_revision": summary["revision_id"], "reason": reason})
                else:
                    usable.append((exact, version, summary, adaptations))
            selection = None
            if usable:
                # Equal-version revisions share their version directory and its commit sequence.
                exacts = [item for item in usable if item[0]]
                if exacts:
                    chosen = max(exacts, key=lambda item: item[2]["commit_sequence"])
                else:
                    chosen = max(usable, key=lambda item: (numeric_version(item[1]["actual"], tag_prefix_confirmed=item[1]["tag_prefix_confirmed"]), item[2]["commit_sequence"]))
                summary = chosen[2]
                selection = {"production_id": summary["production_id"], "summary_revision": summary["revision_id"],
                             "actual_version": chosen[1]["actual"], "summary_file": summary["paths"]["summary_file"],
                             "adaptations": chosen[3]}
                reason = "same_version_latest_valid" if chosen[0] else "highest_lower_same_major"
            else:
                reason = "official_documentation_required"
            baseline = {"selected": selection, "reason": reason, "excluded": excluded, "selected_at": _now()}
            production["baseline"] = baseline
            _atomic_json(self._path(production["paths"]["production_file"]), production)
            return baseline
