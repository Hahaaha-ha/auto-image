"""Conservative, numeric release comparison. Raw identities are never discarded."""

import re


def numeric_version(raw, *, tag_prefix_confirmed=False):
    value = str(raw or "").strip()
    if tag_prefix_confirmed and value.startswith("v"):
        value = value[1:]
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)+", value):
        return None
    return tuple(int(part) for part in value.split("."))


def compare_versions(left, right, *, left_tag_confirmed=False, right_tag_confirmed=False):
    a = numeric_version(left, tag_prefix_confirmed=left_tag_confirmed)
    b = numeric_version(right, tag_prefix_confirmed=right_tag_confirmed)
    if a is None or b is None:
        return {"comparison": None, "reason": "version_not_reliably_comparable"}
    if len(a) != len(b):
        return {"comparison": None, "reason": "version_segment_count_differs"}
    return {"comparison": (a > b) - (a < b), "reason": "numeric_segments"}


def major_version(raw, *, rule, tag_prefix_confirmed=False):
    parts = numeric_version(raw, tag_prefix_confirmed=tag_prefix_confirmed)
    if parts is None or rule == "unknown":
        return None
    if rule == "postgresql" and parts[0] < 10:
        return f"{parts[0]}.{parts[1]}"
    if rule in ("numeric", "postgresql"):
        return parts[0]
    return None
