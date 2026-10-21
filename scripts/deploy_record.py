#!/usr/bin/env python3
"""Read and overwrite current software/version files. No remote or cloud actions."""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from deploy_records import RecordError, VersionStore, compare_versions, describe_contract


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", default=".")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("schema")
    compare = sub.add_parser("compare")
    compare.add_argument("left")
    compare.add_argument("right")
    compare.add_argument("--left-tag-confirmed", action="store_true")
    compare.add_argument("--right-tag-confirmed", action="store_true")
    for command in ("paths", "show", "update", "begin-stage", "commit-stage", "save-summary", "select-baseline", "check-environment", "candidate"):
        child = sub.add_parser(command)
        child.add_argument("--software", required=True)
        child.add_argument("--version", required=True)
        if command in ("update", "commit-stage", "save-summary", "select-baseline", "check-environment"):
            child.add_argument("--data", required=True, help="JSON input file, or '-' for stdin")
        if command in ("begin-stage", "commit-stage"):
            child.add_argument("--stage", required=True)
        if command == "begin-stage":
            child.add_argument("--mode", default="initial")
    args = parser.parse_args(argv)
    try:
        data = {}
        if getattr(args, "data", None):
            data = json.load(sys.stdin) if args.data == "-" else json.loads(Path(args.data).read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise RecordError("--data must contain an object")
        if args.command == "schema":
            result = describe_contract()
        elif args.command == "compare":
            result = compare_versions(args.left, args.right, left_tag_confirmed=args.left_tag_confirmed, right_tag_confirmed=args.right_tag_confirmed)
        else:
            store = VersionStore(args.project_root)
            pair = (args.software, args.version)
            if args.command == "paths":
                result = store.paths(*pair)
            elif args.command == "show":
                result = store.load(*pair)
            elif args.command == "update":
                result = store.update(*pair, **data)
            elif args.command == "begin-stage":
                result = store.begin_stage(*pair, args.stage, mode=args.mode)
            elif args.command == "commit-stage":
                result = store.commit_stage(*pair, args.stage, data)
            elif args.command == "save-summary":
                result = store.save_summary(*pair, data)
            elif args.command == "select-baseline":
                result = store.select_baseline(*pair, **data)
            elif args.command == "check-environment":
                result = store.check_environment(*pair, data)
            else:
                result = store.candidate_report(store.load(*pair))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (RecordError, OSError, KeyError, TypeError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
