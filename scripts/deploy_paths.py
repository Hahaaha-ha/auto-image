#!/usr/bin/env python3
"""Resolve deployment names and paths without writing files or using the network.

Usage: python scripts/deploy_paths.py resolve --data input.json [--root .]
Use --data - for stdin. Input: {software, version?, project_url?, canonical_name?,
output_dir?, files?}. project_url identifies the confirmed official product or
component. A GitHub repository root supplies its lowercase repository name;
canonical_name explicitly selects a confirmed product/component name instead.
Other sources use canonical_name or software, preserving existing punctuation.
files maps configured *_file keys to known paths within one version directory.
Output: {software, project_url, version, output_dir, files, warnings, canonical_name?}.
Warnings contain {path, code, message}; historical metadata is advisory and may
need investigation by the caller. Path safety and actual file collisions fail.
Paths are project-relative; explicit paths may be absolute within the artifact
root. Unknown version stays null; its directory uses configured unknown_version.
Errors are JSON on stderr with exit status 1. Requires PyYAML.
"""

import argparse
from itertools import islice
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit, urlunsplit

import yaml


ENVIRONMENT_FIELDS = ("os", "os_version", "architecture", "installation_method")
NAME_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._+-]*"


def software_identifier(value):
    if not isinstance(value, str) or not re.fullmatch(NAME_PATTERN, value):
        raise ValueError("software must be a nonempty identifier without spaces or path separators")
    return value.lower()


def project_source(value):
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError("project_url must be text or null")
    url = urlsplit(value.strip())
    if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password:
        raise ValueError("project_url must be an HTTP(S) official project URL without credentials")
    path = url.path.rstrip("/")
    host = url.netloc.lower()
    if host == "github.com" and len(path.strip("/").split("/")) == 2:
        path = path.removesuffix(".git").lower()
    # Paths, queries and fragments may distinguish products within one repository.
    return urlunsplit((url.scheme.lower(), host, path, url.query, url.fragment))


def canonical_software(data):
    value = data.get("software")
    if not isinstance(value, str) or not value.strip() or any(c in value for c in ("/", "\\", "\x00")):
        raise ValueError("software must be a nonempty name without path separators")
    if data.get("canonical_name") is not None:
        return software_identifier(data["canonical_name"])
    source = project_source(data.get("project_url"))
    if source:
        url = urlsplit(source)
        parts = url.path.strip("/").split("/")
        if url.netloc == "github.com" and len(parts) == 2 and not url.query and not url.fragment:
            return software_identifier(parts[1])
    return software_identifier(value)


def same_software(record, query):
    left, right = project_source(record.get("project_url")), project_source(query.get("project_url"))
    if left and right:
        if left != right:
            return False
        if record.get("canonical_name") or query.get("canonical_name"):
            return canonical_software(record) == canonical_software(query)
        return True
    return (canonical_software(record) == canonical_software(query)
            or record["software"].lower() == query["software"].lower())


class HeaderLoader(yaml.SafeLoader):
    """Keep recorded timestamps and reject ambiguous or non-string keys."""

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


def validate_record(record, *, require_version=False):
    if not isinstance(record, dict) or not all(isinstance(key, str) for key in record):
        raise ValueError("summary header must be a mapping with string keys")
    canonical_software(record)
    project_source(record.get("project_url"))
    if require_version and "version" not in record:
        raise ValueError("version is required; use null when unconfirmed")
    for key in ("version", "project_url", "updated_at", *ENVIRONMENT_FIELDS):
        if record.get(key) is not None and not isinstance(record[key], str):
            raise ValueError(f"{key} must be text or null")
    json.dumps(record, allow_nan=False)


def read_record(path):
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    if not lines or lines[0] != "---":
        raise ValueError("missing YAML front matter; software/version are unconfirmed")
    try:
        end = lines.index("---", 1)
    except ValueError as exc:
        raise ValueError("unterminated YAML front matter; software/version are unconfirmed") from exc
    record = yaml.load("\n".join(lines[1:end]), Loader=HeaderLoader)
    validate_record(record)
    return record


class DeploymentPaths:
    """One configured layout for resolution, summary scanning and safe writes."""

    def __init__(self, root):
        self.root = Path(root).resolve()
        config = yaml.safe_load((self.root / "deploy.config.yaml").read_text(encoding="utf-8"))
        if not isinstance(config, dict):
            raise ValueError("deploy.config.yaml must contain a mapping")
        self.output = config.get("output_dir")
        if not isinstance(self.output, str) or not all(t in self.output for t in ("{{software}}", "{{version}}")):
            raise ValueError("output_dir must contain {{software}} and {{version}}")
        if Path(self.output).is_absolute() or any(p in ("", ".", "..") for p in self.output.split("/")):
            raise ValueError("configured paths must stay within a project artifact directory")
        self.filenames = {key: value for key, value in config.items()
                          if isinstance(key, str) and key.endswith("_file") and isinstance(value, str)}
        self.filename = self.filenames.get("summary_file")
        if not self.filename or not self.filename.endswith(".md"):
            raise ValueError("summary_file must be a Markdown filename")
        for filename in self.filenames.values():
            if Path(filename).name != filename or filename in (".", ".."):
                raise ValueError("configured artifact filenames must be basenames")
            self._template_pattern(filename)
        if len(set(self.filenames.values())) != len(self.filenames):
            raise ValueError("summary_file and stage filenames must differ")
        prefix = self.output.split("{{", 1)[0].rsplit("/", 1)[0]
        if not prefix:
            raise ValueError("output_dir needs a fixed artifact directory before its placeholders")
        self.base = self.root / prefix
        if not self.base.resolve().is_relative_to(self.root):
            raise ValueError("artifact directory resolves outside the project")
        self.tokens = re.findall(r"{{(software|version)}}", self.output)
        self.directory_pattern = self._template_pattern(self.output)
        self.pattern = self._template_pattern(f"{self.output}/{self.filename}")
        self.unknown_version = config.get("unknown_version", "latest")
        self._version_segment(self.unknown_version)

    @staticmethod
    def _template_pattern(template):
        parts = re.split(r"{{(?:software|version)}}", template)
        if any("{{" in part or "}}" in part for part in parts):
            raise ValueError("unsupported placeholder in path configuration")
        return re.compile("([^/]+)".join(re.escape(part) for part in parts))

    @staticmethod
    def _version_segment(value):
        if not isinstance(value, str) or not value or value in (".", "..") or any(c in value for c in ("/", "\\", "\x00")):
            raise ValueError("directory version must be a nonempty path segment")
        return value

    def relative(self, path):
        return path.relative_to(self.root).as_posix()

    def safe_path(self, value):
        if not isinstance(value, (str, Path)) or not str(value):
            raise ValueError("path must be a nonempty artifact path")
        candidate = Path(value)
        path = (candidate if candidate.is_absolute() else self.root / candidate).resolve()
        if not path.is_relative_to(self.root) or not path.is_relative_to(self.base.resolve()):
            raise ValueError("path resolves outside the configured artifact directory")
        return path

    def directory_values(self, directory):
        match = self.directory_pattern.fullmatch(self.relative(directory))
        if not match:
            raise ValueError("path does not match output_dir/summary_file")
        values = {"software": [], "version": []}
        for token, value in zip(self.tokens, match.groups()):
            values[token].append(software_identifier(value) if token == "software" else value)
        if any(len(set(items)) != 1 for items in values.values()):
            raise ValueError("path placeholders must identify the same software and directory version")
        return values["software"][0], values["version"][0]

    def filename_software(self, key, filename, version):
        template = self.filenames[key].replace("{{version}}", version)
        parts = template.split("{{software}}")
        pattern = re.escape(parts[0])
        for index, part in enumerate(parts[1:]):
            pattern += (f"(?P<software>{NAME_PATTERN})" if index == 0 else "(?P=software)") + re.escape(part)
        match = re.fullmatch(pattern, filename, flags=re.IGNORECASE)
        if not match:
            return None
        return (match.groupdict().get("software") or "").lower()

    def path(self, value, record=None, *, existing=False):
        path = self.safe_path(value)
        directory_name, version = self.directory_values(path.parent)
        filename_name = self.filename_software("summary_file", path.name, version)
        if filename_name is None:
            raise ValueError("path does not match output_dir/summary_file; stage reports, meta and attachments are not summaries")
        if any(self.filename_software(key, path.name, version) is not None
               for key in self.filenames if key != "summary_file"):
            raise ValueError("summary path collides with a configured stage or attachment file")
        if record is not None and not existing:
            canonical = canonical_software(record)
            if directory_name != canonical or filename_name not in ("", canonical):
                raise ValueError("new summaries must use the resolved canonical software name")
        return path

    def summaries(self):
        records, warnings, seen = [], [], set()

        def warn(path, code, message):
            warnings.append({"path": self.relative(Path(path)), "code": code, "message": str(message)})

        if not self.base.exists():
            return records, warnings
        for directory, _, filenames in os.walk(self.base, onerror=lambda exc: warn(exc.filename, "unreadable_directory", exc)):
            for filename in sorted(filenames):
                candidate = Path(directory) / filename
                if not self.pattern.fullmatch(self.relative(candidate)):
                    continue
                try:
                    path = self.path(candidate)
                except (OSError, ValueError, RuntimeError) as exc:
                    warn(candidate, "unsafe_summary_path", exc)
                    continue
                if path in seen:
                    continue
                seen.add(path)
                try:
                    record = read_record(path)
                except (OSError, UnicodeError) as exc:
                    warn(path, "unreadable_summary", exc)
                    continue
                except (ValueError, TypeError, RecursionError, yaml.YAMLError) as exc:
                    warn(path, "invalid_summary", exc)
                    continue
                records.append((path, record))
        return records, sorted(warnings, key=lambda item: item["path"])

    def stage_sources(self, software_directory):
        """Read explicit official-project headers, including runs without a summary."""
        sources = []
        for directory, _, filenames in os.walk(self.base):
            parent = Path(directory)
            try:
                name, version = self.directory_values(parent)
            except ValueError:
                continue
            if name != software_directory:
                continue
            for filename in filenames:
                if not filename.endswith(".md") or not any(
                    self.filename_software(key, filename, version) is not None
                    for key in self.filenames if key != "summary_file"
                ):
                    continue
                try:
                    path = self.safe_path(parent / filename)
                    self.directory_values(path.parent)
                    with path.open(encoding="utf-8") as stream:
                        for line in islice(stream, 40):
                            if not line.startswith(">"):
                                continue
                            match = re.search(r"(?:^>\s*|\|\s*)(?:官方项目|project_url)\s*[:：]\s*[`<]?(https?://[^\s|`>]+)", line)
                            if match:
                                sources.append((path, project_source(match[1])))
                                break
                except (OSError, UnicodeError, ValueError, RuntimeError):
                    # The caller separately validates selected paths; unrelated
                    # unreadable historical files do not block a new deployment.
                    continue
        return sources

    def check_directory_identity(self, directory, data, records=None, stage_sources=None, *, writing=False):
        """Report identity differences; reject write conflicts in the target directory."""
        directory_name, _ = self.directory_values(directory)
        source = project_source(data.get("project_url"))
        warnings = []
        if records is None:
            records = self.summaries()[0]
        if stage_sources is None:
            stage_sources = self.stage_sources(directory_name)

        def report(path, recorded_source, detail):
            message = (f"{detail}; recorded project_url={recorded_source or 'unknown'}, "
                       f"requested project_url={source or 'unknown'}; "
                       "read the existing material and use the current task context to confirm or correct the paths")
            if writing and path.parent == directory and source and recorded_source:
                raise ValueError(message)
            warnings.append({"path": self.relative(path), "code": "identity_unconfirmed", "message": message})

        for path, record in records:
            other_name, _ = self.directory_values(path.parent)
            other_source = project_source(record.get("project_url"))
            if ((other_name == directory_name and source and other_source)
                    or path.parent == directory) and not same_software(record, data):
                report(path, other_source,
                       f"Software identity differs: recorded={record['software']}, requested={data['software']}")
        for path, other_source in stage_sources:
            other_name, _ = self.directory_values(path.parent)
            if other_name == directory_name and source and other_source != source:
                report(path, other_source, "An existing stage records a different project URL")
        return warnings

    def resolve(self, data):
        validate_record(data)
        software = canonical_software(data)
        source = project_source(data.get("project_url"))
        version = data.get("version")
        directory_version = self._version_segment(version if version is not None else self.unknown_version)
        overrides = data.get("files", {})
        if not isinstance(overrides, dict) or any(key not in self.filenames for key in overrides):
            raise ValueError("files must map configured *_file keys to known paths")
        files = {key: self.safe_path(value) for key, value in overrides.items()}
        if any(path.exists() and not path.is_file() for path in files.values()):
            raise ValueError("explicit artifact files must be regular files")
        parents = {path.parent for path in files.values()}
        if data.get("output_dir") is not None:
            parents.add(self.safe_path(data["output_dir"]))
        if len(parents) > 1:
            raise ValueError("explicit files and output_dir must identify one version directory")
        records, summary_warnings = self.summaries()
        matches = [(path, record) for path, record in records if same_software(record, data)]
        candidates = {path.parent for path, record in matches
                      if (version is not None and record.get("version") == version)
                      or (record.get("version") is None
                          and self.directory_values(path.parent)[1] == directory_version)}
        if not parents and len(candidates) > 1:
            raise ValueError("ambiguous existing version directories; provide output_dir or known files")
        directory = next(iter(parents or candidates), None)
        if directory is None:
            relative = self.output.replace("{{software}}", software).replace("{{version}}", directory_version)
            directory = self.safe_path(relative)
        directory_name, actual_directory_version = self.directory_values(directory)
        if directory.exists() and not directory.is_dir():
            raise ValueError("output_dir must be a directory")
        directory_version = actual_directory_version
        stage_sources = self.stage_sources(directory_name)
        warnings = self.check_directory_identity(directory, data, records, stage_sources)
        warnings.extend(item for item in summary_warnings if (self.root / item["path"]).parent == directory)
        local_records = [(path, record) for path, record in matches if path.parent == directory]
        known_names = {software, directory_name}
        for _, record in local_records:
            known_names.add(record["software"].lower())
        prefix = software
        for key, path in files.items():
            name = self.filename_software(key, path.name, directory_version)
            if name is None:
                raise ValueError("explicit file does not match its configured filename")
            if name:
                prefix = name
                known_names.add(name)
        if not files and len(local_records) == 1:
            prefix = self.filename_software("summary_file", local_records[0][0].name, directory_version) or software
        existing_stages = []
        for key, template in self.filenames.items():
            if key in files:
                if key != "summary_file" and files[key].is_file():
                    existing_stages.append(files[key])
                continue
            existing = []
            if directory.is_dir():
                for path in directory.iterdir():
                    name = self.filename_software(key, path.name, directory_version)
                    if name is not None and (not name or name in known_names):
                        if not path.is_file():
                            raise ValueError(f"existing {key} must be a regular file")
                        existing.append(self.safe_path(path))
            if len(existing) > 1:
                raise ValueError(f"ambiguous existing {key}; provide its exact path in files")
            filename = template.replace("{{software}}", prefix).replace("{{version}}", directory_version)
            files[key] = existing[0] if existing else self.safe_path(directory / filename)
            if existing and key != "summary_file":
                existing_stages.append(existing[0])
        if len(set(files.values())) != len(files):
            raise ValueError("resolved artifact paths collide")
        if any(path.parent != directory for path in files.values()):
            raise ValueError("resolved artifact files must remain in the selected version directory")
        if existing_stages and not local_records and not any(path.parent == directory for path, _ in stage_sources):
            warnings.append({"path": self.relative(sorted(existing_stages)[0]), "code": "identity_unconfirmed",
                             "message": "Existing stage files have no confirmed project metadata; use the current task context and read them before continuing"})
        result = {"software": software, "project_url": source, "version": version,
                  "output_dir": self.relative(directory),
                  "files": {key: self.relative(path) for key, path in files.items()},
                  "warnings": sorted(warnings, key=lambda item: (item["path"], item["code"]))}
        if data.get("canonical_name") is not None:
            result["canonical_name"] = software_identifier(data["canonical_name"])
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("resolve",))
    parser.add_argument("--data", required=True)
    parser.add_argument("--root", default=".")
    args = parser.parse_args(argv)
    try:
        data = json.load(sys.stdin) if args.data == "-" else json.loads(Path(args.data).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("--data must contain a JSON object")
        print(json.dumps(DeploymentPaths(args.root).resolve(data), ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except (OSError, ValueError, TypeError, RuntimeError, yaml.YAMLError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
