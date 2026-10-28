#!/usr/bin/env python3
"""Save and find deployment summaries; Markdown front matter is the only record.

Usage: python scripts/deploy_summary.py {save,find} --data input.json [--root .]
Use --data - for stdin. Paths are relative to --root, or absolute within it.
save input: {path, software, version (string or null), body, ...optional fields}.
find input: {software, project_url?, canonical_name?, version?, os?, os_version?, architecture?, installation_method?}.
Optional header fields include project_url, canonical_name and the environment fields above;
additional JSON fields are preserved. path/body are not copied into the header,
and updated_at is generated with a timezone. Unknown facts may be null.
save output: {path, record, warnings}; find output: {matches, warnings}. Each match contains
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


if __package__:
    from .deploy_paths import (DeploymentPaths as SummaryPaths, ENVIRONMENT_FIELDS,
                               canonical_software, project_source, read_record,
                               same_software, validate_record)
else:
    from deploy_paths import (DeploymentPaths as SummaryPaths, ENVIRONMENT_FIELDS,
                              canonical_software, project_source, read_record,
                              same_software, validate_record)


def save(paths, data):
    validate_record(data, require_version=True)
    if not isinstance(data.get("body"), str) or not data["body"].strip():
        raise ValueError("body must contain nonempty Markdown")
    path = paths.path(data.get("path"))
    warnings = []
    record = {key: value for key, value in data.items() if key not in ("path", "body", "updated_at")}
    if "project_url" in record:
        record["project_url"] = project_source(record["project_url"])
    if path.exists():
        try:
            previous = read_record(path)
        except (ValueError, UnicodeError, TypeError, RecursionError, yaml.YAMLError) as exc:
            paths.path(path, record)
            previous = {}
            warnings.append({"path": paths.relative(path), "code": "invalid_summary", "message": str(exc)})
        for key in ("project_url", "canonical_name"):
            if key not in data and previous.get(key) is not None:
                record[key] = previous[key]
    else:
        _, directory_version = paths.directory_values(path.parent)
        existing_stage = path.parent.is_dir() and any(
            file.is_file() and paths.filename_software(key, file.name, directory_version) is not None
            for file in path.parent.iterdir() for key in paths.filenames if key != "summary_file")
        paths.path(path, record, existing=bool(existing_stage and record.get("project_url")))
    warnings.extend(paths.check_directory_identity(path.parent, record, writing=True))
    record["software"] = canonical_software(record)
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
    return {"path": paths.relative(path), "record": record, "warnings": warnings}


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
    validate_record(data)
    records, warnings = paths.summaries()
    matches = []
    for path, record in records:
        if not same_software(record, data):
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
