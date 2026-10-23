#!/usr/bin/env python3
"""Save and find deployment summaries; Markdown front matter is the only record.

Usage: python scripts/deploy_summary.py {save,find} --data input.json [--root .]
Use --data - for stdin. Paths are relative to --root, or absolute within it.
save input: {path, software, version (string or null), body, ...optional fields}.
find input: {software, version?, os?, os_version?, architecture?, installation_method?}.
Optional header fields include project_url and the environment fields above;
additional JSON fields are preserved. path/body are not copied into the header,
and updated_at is generated with a timezone. Unknown facts may be null.
save output: {path, record}; find output: {matches, warnings}. Each match contains
path, the original record, version_match {relation, reason}, and environment
{field: {recorded, target, relation}}. Version relations are same, older, newer,
unknown, incomparable; environment relations are same, different, unknown.
Warnings contain path, code, message; they never infer records from directories.
Returned paths are project-relative actual file locations. Errors are JSON on
stderr with exit status 1; an empty find result is successful. Requires PyYAML.
"""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import tempfile

import yaml


ENVIRONMENT_FIELDS = ("os", "os_version", "architecture", "installation_method")


class HeaderLoader(yaml.SafeLoader):
    """Keep unquoted timestamps as strings when returning recorded values."""

    def construct_mapping(self, node, deep=False):
        self.flatten_mapping(node)
        keys = [self.construct_object(key, deep=deep) for key, _ in node.value]
        if not all(isinstance(key, str) for key in keys):
            raise ValueError("summary header keys must be strings")
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate summary header keys are ambiguous")
        return super().construct_mapping(node, deep=deep)


HeaderLoader.yaml_implicit_resolvers = {
    key: [(tag, pattern) for tag, pattern in resolvers if tag != "tag:yaml.org,2002:timestamp"]
    for key, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def _name(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]*", value):
        raise ValueError("software must be a nonempty identifier without spaces or path separators")
    return value.lower()


class SummaryPaths:
    """The shared configured filename and alias rules for both commands."""

    def __init__(self, root):
        self.root = Path(root).resolve()
        config = yaml.safe_load((self.root / "deploy.config.yaml").read_text(encoding="utf-8"))
        if not isinstance(config, dict):
            raise ValueError("deploy.config.yaml must contain a mapping")
        output, filename = config.get("output_dir"), config.get("summary_file")
        if not isinstance(output, str) or not all(token in output for token in ("{{software}}", "{{version}}")):
            raise ValueError("output_dir must contain {{software}} and {{version}}")
        if not isinstance(filename, str) or Path(filename).name != filename or not filename.endswith(".md"):
            raise ValueError("summary_file must be a Markdown filename")
        self.other_files = [value for key, value in config.items()
                            if isinstance(key, str) and key.endswith("_file") and key != "summary_file" and isinstance(value, str)]
        if filename in self.other_files:
            raise ValueError("summary_file must differ from stage and attachment filenames")
        template = f"{output}/{filename}"
        if Path(template).is_absolute() or any(part in (".", "..") for part in template.split("/")):
            raise ValueError("configured summary paths must stay within a project artifact directory")
        prefix = []
        for part in output.split("/"):
            if "{{" in part:
                break
            prefix.append(part)
        if not prefix or not all(prefix):
            raise ValueError("output_dir needs a fixed artifact directory before its placeholders")
        self.base = self.root.joinpath(*prefix)
        if not self.base.resolve().is_relative_to(self.root):
            raise ValueError("artifact directory resolves outside the project")
        self.aliases = {}
        aliases = config.get("software_aliases", {})
        if not isinstance(aliases, dict):
            raise ValueError("software_aliases must be a mapping")
        for alias, item in aliases.items():
            if not isinstance(item, dict) or not isinstance(item.get("source"), str) or not item["source"].strip():
                raise ValueError(f"software alias {alias!r} needs an id and confirmed source")
            self.aliases[_name(alias)] = _name(item.get("id"))
        self.filename = filename
        self.tokens = re.findall(r"{{(software|version)}}", output)
        parts = re.split(r"{{(?:software|version)}}", template)
        if any("{{" in part or "}}" in part for part in parts):
            raise ValueError("unsupported placeholder in summary path configuration")
        self.pattern = re.compile("([^/]+)".join(re.escape(part) for part in parts))
        self.directory_pattern = re.compile("([^/]+)".join(re.escape(part) for part in re.split(r"{{(?:software|version)}}", output)))

    def identity(self, software):
        name = _name(software)
        return self.aliases.get(name, name)

    def path(self, value, software=None):
        if not isinstance(value, (str, Path)) or not str(value):
            raise ValueError("path must be a nonempty summary file path")
        candidate = Path(value)
        path = (candidate if candidate.is_absolute() else self.root / candidate).resolve()
        if not path.is_relative_to(self.root) or not path.is_relative_to(self.base):
            raise ValueError("summary path resolves outside the configured artifact directory")
        match = self.directory_pattern.fullmatch(path.parent.relative_to(self.root).as_posix())
        if not match:
            raise ValueError("path does not match output_dir/summary_file; stage reports, meta and attachments are not summaries")
        names, versions = [], []
        for token, value in zip(self.tokens, match.groups()):
            (names if token == "software" else versions).append(self.identity(value) if token == "software" else value)
        if len(set(names)) != 1 or len(set(versions)) != 1:
            raise ValueError("summary path placeholders must identify the same software and directory version")
        raw_names = {value for token, value in zip(self.tokens, match.groups()) if token == "software"}
        raw_names.update(name for name in (*self.aliases, names[0]) if self.identity(name) == names[0])
        filename_pattern = re.escape(self.filename).replace(re.escape("{{version}}"), re.escape(versions[0]))
        filename_pattern = filename_pattern.replace(re.escape("{{software}}"), "(?i:" + "|".join(re.escape(name) for name in raw_names) + ")")
        if not re.fullmatch(filename_pattern, path.name):
            raise ValueError("path does not match output_dir/summary_file; stage reports, meta and attachments are not summaries")
        if any(path.name == pattern.replace("{{software}}", name).replace("{{version}}", versions[0])
               for pattern in self.other_files for name in raw_names):
            raise ValueError("summary path collides with a configured stage or attachment file")
        if software is not None and names[0] != self.identity(software):
            raise ValueError("summary path identifies a different software product")
        return path

    def relative(self, path):
        return path.relative_to(self.root).as_posix()


def _validate_record(record, *, require_version=False):
    if not isinstance(record, dict) or not all(isinstance(key, str) for key in record):
        raise ValueError("summary header must be a mapping with string keys")
    _name(record.get("software"))
    if require_version and "version" not in record:
        raise ValueError("version is required; use null when unconfirmed")
    for key in ("version", "project_url", "updated_at", *ENVIRONMENT_FIELDS):
        if record.get(key) is not None and not isinstance(record[key], str):
            raise ValueError(f"{key} must be text or null")
    json.dumps(record, allow_nan=False)


def save(paths, data):
    _validate_record(data, require_version=True)
    if not isinstance(data.get("body"), str) or not data["body"].strip():
        raise ValueError("body must contain nonempty Markdown")
    path = paths.path(data.get("path"), data["software"])
    record = {key: value for key, value in data.items() if key not in ("path", "body", "updated_at")}
    record["updated_at"] = datetime.now(timezone.utc).isoformat()
    content = "---\n" + yaml.safe_dump(record, allow_unicode=True, sort_keys=False) + "---\n\n" + data["body"]
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix=".deploy-", suffix=".tmp", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return {"path": paths.relative(path), "record": record}


def _read_record(path):
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    if not lines or lines[0] != "---":
        raise ValueError("missing YAML front matter; software/version are unconfirmed")
    try:
        end = lines.index("---", 1)
    except ValueError as exc:
        raise ValueError("unterminated YAML front matter; software/version are unconfirmed") from exc
    record = yaml.load("\n".join(lines[1:end]), Loader=HeaderLoader)
    _validate_record(record)
    return record


def _numeric(version):
    if isinstance(version, str) and re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", version):
        try:
            return tuple(int(part) for part in version.split("."))
        except ValueError:
            return None
    return None


def _version_match(recorded, target):
    if not recorded or not target:
        return {"relation": "unknown", "reason": "version_not_recorded_or_requested"}
    if recorded == target:
        return {"relation": "same", "reason": "exact_string"}
    left, right = _numeric(recorded), _numeric(target)
    if left is None or right is None:
        return {"relation": "incomparable", "reason": "non_numeric_version"}
    if len(left) != len(right):
        return {"relation": "incomparable", "reason": "different_segment_counts"}
    return {"relation": "older" if left < right else "newer" if left > right else "same", "reason": "numeric_segments"}


def find(paths, data):
    _validate_record(data)
    identity = paths.identity(data["software"])
    matches, warnings, seen = [], [], set()
    if not paths.base.exists():
        return {"matches": matches, "warnings": warnings}

    def warn(path, code, message):
        warnings.append({"path": paths.relative(Path(path)), "code": code, "message": str(message)})

    for directory, _, filenames in os.walk(paths.base, onerror=lambda exc: warn(exc.filename, "unreadable_directory", exc)):
        for filename in sorted(filenames):
            candidate = Path(directory) / filename
            if not paths.pattern.fullmatch(paths.relative(candidate)):
                continue
            try:
                path = paths.path(candidate)
            except (OSError, ValueError, RuntimeError) as exc:
                warn(candidate, "unsafe_summary_path", exc)
                continue
            if path in seen:
                continue
            seen.add(path)
            try:
                record = _read_record(path)
            except (OSError, UnicodeError) as exc:
                warn(path, "unreadable_summary", exc)
                continue
            except (ValueError, TypeError, RecursionError, yaml.YAMLError) as exc:
                warn(path, "invalid_summary", exc)
                continue
            if paths.identity(record["software"]) != identity:
                continue
            environment = {}
            for key in ENVIRONMENT_FIELDS:
                recorded, target = record.get(key), data.get(key)
                relation = "unknown" if not recorded or not target else "same" if recorded == target else "different"
                environment[key] = {"recorded": recorded, "target": target, "relation": relation}
            matches.append({"path": paths.relative(path), "record": record,
                            "version_match": _version_match(record.get("version"), data.get("version")), "environment": environment})
    ranks = {"same": 0, "older": 1, "newer": 2, "incomparable": 3, "unknown": 4}

    def order(item):
        relation = item["version_match"]["relation"]
        descending = tuple(-part for part in _numeric(item["record"]["version"])) if relation == "older" else ()
        return ranks[relation], descending, item["path"]

    return {"matches": sorted(matches, key=order), "warnings": sorted(warnings, key=lambda item: item["path"])}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("save", "find"))
    parser.add_argument("--data", required=True, help="JSON object file, or - for stdin")
    parser.add_argument("--root", default=".", help="project directory containing deploy.config.yaml (default: current directory)")
    args = parser.parse_args(argv)
    try:
        data = json.load(sys.stdin) if args.data == "-" else json.loads(Path(args.data).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("--data must contain a JSON object")
        paths = SummaryPaths(args.root)
        result = save(paths, data) if args.command == "save" else find(paths, data)
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except (OSError, ValueError, TypeError, RuntimeError, yaml.YAMLError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
