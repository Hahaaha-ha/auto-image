#!/usr/bin/env python3
"""JSON-only local production-record CLI. It never performs remote or cloud operations."""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deploy_records import RecordError, RecordStore, compare_versions, describe_contract


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", default=".")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("schema")
    compare = subparsers.add_parser("compare")
    compare.add_argument("left")
    compare.add_argument("right")
    compare.add_argument("--left-tag-confirmed", action="store_true")
    compare.add_argument("--right-tag-confirmed", action="store_true")
    for command in ("create", "list", "show", "update", "set-machine", "begin-stage", "commit-stage",
                    "begin-summary", "commit-summary", "select-baseline", "check-environment", "candidate", "snapshot", "stages", "summaries"):
        child = subparsers.add_parser(command)
        if command in ("create", "list", "summaries"):
            child.add_argument("--software", required=command == "create")
        if command in ("create", "list"):
            child.add_argument("--version")
        if command == "list":
            child.add_argument("--call-id")
        if command not in ("create", "list"):
            child.add_argument("--production-id", required=command != "summaries")
        if command in ("commit-stage", "commit-summary", "candidate", "snapshot"):
            child.add_argument("--revision-id", required=True)
        if command in ("create", "update", "set-machine", "begin-stage", "commit-stage", "commit-summary", "check-environment"):
            child.add_argument("--data", required=command in ("update", "set-machine", "commit-stage", "commit-summary", "check-environment"), help="JSON input file; '-' reads stdin")
        if command == "begin-stage":
            child.add_argument("--stage", required=True)
            child.add_argument("--guide-revision")
            child.add_argument("--mode", default="initial")
        if command == "stages":
            child.add_argument("--include-drafts", action="store_true", help="include interrupted work for operation-state recovery, never as passed evidence")
        if command == "snapshot":
            child.add_argument("--source", required=True)
            child.add_argument("--name", required=True)
            child.add_argument("--summary", action="store_true")
    args = parser.parse_args(argv)
    try:
        data = {}
        if getattr(args, "data", None):
            if args.data == "-":
                data = json.load(sys.stdin)
            else:
                with Path(args.data).open(encoding="utf-8") as stream:
                    data = json.load(stream)
            if not isinstance(data, dict):
                raise RecordError("--data must contain a JSON object")
        if args.command == "schema":
            result = describe_contract()
        elif args.command == "compare":
            result = compare_versions(args.left, args.right, left_tag_confirmed=args.left_tag_confirmed, right_tag_confirmed=args.right_tag_confirmed)
        else:
            store = RecordStore(args.project_root)
            command = args.command
            if command == "create":
                result = store.create(args.software, args.version, **data)
            elif command == "list":
                result = store.list_productions(software=args.software, version=args.version, call_id=args.call_id)
            elif command == "show":
                result = store.get(args.production_id)
            elif command == "update":
                result = store.update(args.production_id, **data)
            elif command == "set-machine":
                result = store.set_machine(args.production_id, data)
            elif command == "begin-stage":
                result = store.begin_stage(args.production_id, args.stage, guide_revision=args.guide_revision, mode=args.mode, **data)
            elif command == "commit-stage":
                result = store.commit_stage(args.production_id, args.revision_id, data)
            elif command == "begin-summary":
                result = store.begin_summary(args.production_id)
            elif command == "commit-summary":
                result = store.commit_summary(args.production_id, args.revision_id, data)
            elif command == "select-baseline":
                result = store.select_baseline(args.production_id)
            elif command == "check-environment":
                result = store.check_environment(args.production_id, data)
            elif command == "candidate":
                result = store.candidate_report(store.revision(args.production_id, args.revision_id, summary=True))
            elif command == "stages":
                result = store.stages(args.production_id, include_drafts=args.include_drafts)
            elif command == "summaries":
                result = store.summaries(software=args.software, production_id=args.production_id)
            elif command == "snapshot":
                result = {"path": store.snapshot(args.production_id, args.revision_id, args.source, args.name, summary=args.summary)}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (RecordError, OSError, TypeError, KeyError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
