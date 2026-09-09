#!/usr/bin/env python
"""华为云 OBS skill 入口 —— 上传(upload) + 列举(list) + 属性(head) + 删除(delete) + 链接(url)。

官方 esdk-obs-python SDK（from obs import ObsClient）。纯 JSON 输出（stdout 只出 JSON，
进度/告警走 stderr），upload/delete 带 --dry-run 确认杠杆，logs/ 归档，输出不含任何凭证。

注意：本文件刻意不叫 obs.py——SDK 包名就是 `obs`，同名会自我遮蔽。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from obs_client import (
    DEFAULT_SCOPE_PATH,
    Credentials,
    ObsTarget,
    build_client,
    load_scope_config,
    resolve_credentials,
    resolve_obs_target,
)
from obs_ops import (
    ACL_PUBLIC_READ,
    MAX_PUTFILE_BYTES,
    STORAGE_CLASSES,
    build_upload_plan,
    err_from_resp,
    normalize_key,
    public_url,
)

LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"
DEFAULT_CONCURRENCY = 8   # 目录并发上传线程数（官方建议逐文件并发，putFile 自身无并发）
LIST_PAGE_SIZE = 1000     # listObjects 单页上限


# ----------------------------------------------------------------------------
# 小工具
# ----------------------------------------------------------------------------
def pick(obj: Any, *names: str, default: Any = None) -> Any:
    """按多个候选属性名取值（SDK 模型 camelCase/snake_case 兜底）。"""
    for n in names:
        v = getattr(obj, n, None)
        if v is not None:
            return v
    return default


def emit(obj: dict[str, Any]) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def note(msg: str) -> None:
    """进度/告警只走 stderr——stdout 保持纯 JSON。"""
    print(f"[obs] {msg}", file=sys.stderr, flush=True)


def new_log_path(name: str) -> Path:
    return LOGS_DIR / f"{name}-{time.strftime('%Y%m%d-%H%M%S')}.json"


def write_log(path: Path, payload: dict[str, Any]) -> str:
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        return str(path)
    except OSError:
        return ""


def resolve_target(args: argparse.Namespace) -> tuple[dict[str, Any], Credentials, ObsTarget]:
    scope = load_scope_config(Path(args.scope))
    creds = resolve_credentials(scope)
    target = resolve_obs_target(scope, creds, getattr(args, "bucket", None))
    return scope, creds, target


# ----------------------------------------------------------------------------
# upload：文件/目录 → putFile（≤5GB）并发上传
# ----------------------------------------------------------------------------
def cmd_upload(args: argparse.Namespace) -> int:
    _, creds, target = resolve_target(args)
    try:
        plan = build_upload_plan(Path(args.path), key=args.key, prefix=args.prefix)
    except (FileNotFoundError, ValueError) as e:
        emit({"ok": False, "action": "upload", "error": str(e)})
        return 1

    oversize = [it for it in plan if it["size"] > MAX_PUTFILE_BYTES]
    if oversize:
        emit({
            "ok": False, "action": "upload",
            "error": "超过 putFile 单文件 5GB 上限（官方约束，更大文件需多段上传，本 skill 不支持）",
            "oversize": oversize,
        })
        return 1

    base: dict[str, Any] = {
        "action": "upload",
        "bucket": target.bucket,
        "endpoint": target.endpoint,
        "domain": target.domain,
        "region": creds.region,
        "object_acl": args.acl,
        "storage_class": args.storage_class or "STANDARD",
        "content_type": args.content_type or "(按扩展名自动)",
        "total_count": len(plan),
        "total_size": sum(it["size"] for it in plan),
    }

    if args.dry_run:
        payload = {
            **base,
            "ok": True,
            "dry_run": True,
            "objects": [{"key": it["key"], "local": it["local"], "size": it["size"]} for it in plan],
            "hint": "确认无误后去掉 --dry-run 执行真实上传。",
        }
        payload["log"] = write_log(new_log_path("upload-dryrun"), payload)
        emit(payload)
        return 0

    from obs import PutObjectHeader  # 真实运行才需要 SDK

    def make_headers():
        h = PutObjectHeader()
        if args.acl != "private":
            h.acl = args.acl  # 如 public-read（HeadPermission 原始值）
        if args.content_type:
            h.contentType = args.content_type
        if args.storage_class:
            h.storageClass = args.storage_class
        return h

    client = build_client(creds, target)

    def upload_one(item: dict[str, Any]):
        resp = client.putFile(
            bucketName=target.bucket,
            objectKey=item["key"],
            file_path=item["local"],
            headers=make_headers(),
        )
        return item, resp

    uploaded: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    note(f"上传 {len(plan)} 个对象到 {target.bucket}（并发 {args.concurrency}）")
    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        futures = [pool.submit(upload_one, it) for it in plan]
        for fut in as_completed(futures):
            item, resp = fut.result()
            if resp.status < 300:
                uploaded.append({
                    "key": item["key"],
                    "local": item["local"],
                    "size": item["size"],
                    "etag": pick(resp.body, "etag") if resp.body is not None else None,
                    "object_url": pick(resp.body, "objectUrl") if resp.body is not None else None,
                    "public_url": public_url(target.domain, item["key"]),
                    "request_id": resp.requestId,
                })
                note(f"ok {item['key']} ({item['size']}B)")
            else:
                failed.append({"key": item["key"], "local": item["local"],
                               "error": err_from_resp(resp)})
                note(f"FAIL {item['key']} status={resp.status} {resp.errorCode}")
    uploaded.sort(key=lambda r: r["key"])
    failed.sort(key=lambda r: r["key"])

    payload = {**base, "ok": not failed, "uploaded": uploaded, "failed": failed}
    if failed:
        payload["error"] = f"{len(failed)}/{len(plan)} 个对象上传失败"
        payload["hint"] = "看 failed[].error（error_code/request_id 可提工单）；重跑同 key 覆盖即可。"
    payload["log"] = write_log(new_log_path("upload"), payload)
    emit(payload)
    return 0 if not failed else 1


# ----------------------------------------------------------------------------
# list：列举对象（分页取到 --max）
# ----------------------------------------------------------------------------
def _list_prefix(client, target: ObsTarget, prefix: str, limit: int) -> tuple[list[dict], dict | None]:
    objects: list[dict] = []
    marker: str | None = None
    err: dict | None = None
    while len(objects) < limit:
        resp = client.listObjects(
            bucketName=target.bucket,
            prefix=prefix or None,
            max_keys=min(limit - len(objects), LIST_PAGE_SIZE),
            marker=marker,
        )
        if resp.status >= 300:
            err = err_from_resp(resp)
            break
        body = resp.body
        for c in (pick(body, "contents", default=[]) or []):
            objects.append({
                "key": pick(c, "key"),
                "size": pick(c, "size"),
                "etag": pick(c, "etag"),
                "last_modified": str(pick(c, "lastModified", "last_modified", default="") or ""),
                "storage_class": pick(c, "storageClass", "storage_class"),
            })
        truncated = pick(body, "is_truncated", "isTruncated", default=False)
        marker = pick(body, "next_marker", "nextMarker")
        if not truncated or not marker:
            break
    return objects, err


def cmd_list(args: argparse.Namespace) -> int:
    _, creds, target = resolve_target(args)
    client = build_client(creds, target)
    objects, err = _list_prefix(client, target, args.prefix or "", args.max)
    if err:
        emit({"ok": False, "action": "list", "bucket": target.bucket,
              "prefix": args.prefix or "", "error": err})
        return 1
    emit({
        "ok": True, "action": "list", "bucket": target.bucket,
        "prefix": args.prefix or "", "count": len(objects), "objects": objects,
    })
    return 0


# ----------------------------------------------------------------------------
# head：查对象属性（404 = 不存在，不算失败）
# ----------------------------------------------------------------------------
def cmd_head(args: argparse.Namespace) -> int:
    _, creds, target = resolve_target(args)
    key = normalize_key(args.key)
    client = build_client(creds, target)
    resp = client.headObject(bucketName=target.bucket, objectKey=key)
    if resp.status < 300:
        body = resp.body
        # HEAD 请求无响应体：元数据在响应头（list of (name, value)）；body 属性兜底。
        hdrs = {str(k).lower(): v for k, v in (resp.header or [])}
        size = hdrs.get("content-length") or pick(body, "contentLength")
        emit({
            "ok": True, "action": "head", "bucket": target.bucket, "key": key, "exists": True,
            "size": int(size) if size is not None else None,
            "content_type": hdrs.get("content-type") or pick(body, "contentType"),
            "etag": hdrs.get("etag") or pick(body, "etag"),
            "last_modified": hdrs.get("last-modified")
                             or str(pick(body, "lastModified", default="") or ""),
            "storage_class": hdrs.get("x-obs-storage-class") or pick(body, "storageClass"),
        })
        return 0
    if resp.status == 404:
        emit({"ok": True, "action": "head", "bucket": target.bucket, "key": key, "exists": False})
        return 0
    emit({"ok": False, "action": "head", "bucket": target.bucket, "key": key,
          "error": err_from_resp(resp)})
    return 1


# ----------------------------------------------------------------------------
# delete：删除对象（精确 key 可重复 / --prefix 批量）
# ----------------------------------------------------------------------------
def cmd_delete(args: argparse.Namespace) -> int:
    _, creds, target = resolve_target(args)
    keys: list[str] = [normalize_key(k) for k in (args.key or [])]
    listed_err: dict | None = None
    if args.prefix:
        client = build_client(creds, target)
        objs, listed_err = _list_prefix(client, target, args.prefix, args.max)
        keys.extend(o["key"] for o in objs)
    keys = sorted(set(keys))
    if not keys:
        emit({"ok": False, "action": "delete", "bucket": target.bucket,
              "error": "未指定对象：需要 --key（可重复）或 --prefix"})
        return 1

    base = {"action": "delete", "bucket": target.bucket, "count": len(keys)}
    if args.dry_run:
        payload = {**base, "ok": True, "dry_run": True, "objects": keys,
                   "hint": "确认无误后去掉 --dry-run 执行真实删除。"}
        payload["log"] = write_log(new_log_path("delete-dryrun"), payload)
        emit(payload)
        return 0

    client = build_client(creds, target)
    deleted: list[str] = []
    failed: list[dict] = []

    def delete_one(k: str):
        return k, client.deleteObject(bucketName=target.bucket, objectKey=k)

    with ThreadPoolExecutor(max_workers=min(16, max(1, len(keys)))) as pool:
        for fut in as_completed([pool.submit(delete_one, k) for k in keys]):
            k, resp = fut.result()
            if resp.status < 300:
                deleted.append(k)
                note(f"deleted {k}")
            else:
                failed.append({"key": k, "error": err_from_resp(resp)})
                note(f"FAIL delete {k} status={resp.status}")
    deleted.sort()
    failed.sort(key=lambda f: f["key"])

    payload = {**base, "ok": not failed and not listed_err, "deleted": deleted, "failed": failed}
    if listed_err:
        payload["list_error"] = listed_err
    if failed:
        payload["error"] = f"{len(failed)} 个对象删除失败"
    payload["log"] = write_log(new_log_path("delete"), payload)
    emit(payload)
    return 0 if not failed and not listed_err else 1


# ----------------------------------------------------------------------------
# url：对象公开 URL + 带签名 URL（本地签名，不触网）
# ----------------------------------------------------------------------------
def cmd_url(args: argparse.Namespace) -> int:
    _, creds, target = resolve_target(args)
    key = normalize_key(args.key)
    client = build_client(creds, target)
    resp = client.createSignedUrl("GET", bucketName=target.bucket, objectKey=key,
                                  expires=args.expires)
    signed = pick(resp, "signedUrl")
    if not signed:
        emit({"ok": False, "action": "url", "bucket": target.bucket, "key": key,
              "error": err_from_resp(resp)})
        return 1
    emit({
        "ok": True, "action": "url", "bucket": target.bucket, "key": key,
        "public_url": public_url(target.domain, key),
        "signed_url": signed,
        "expires_in": args.expires,
        "note": "public_url 仅在桶/对象 ACL 为公共读时可匿名访问；私有对象用 signed_url。",
    })
    return 0


# ----------------------------------------------------------------------------
# health：健康检查（headBucket 单请求，覆盖配置解密/网络/凭据/桶存在）
# ----------------------------------------------------------------------------
def cmd_health(args: argparse.Namespace) -> int:
    _, creds, target = resolve_target(args)
    client = build_client(creds, target)
    started = time.monotonic()
    resp = client.headBucket(bucketName=target.bucket)
    latency = round((time.monotonic() - started) * 1000)
    if resp.status < 300:
        emit({
            "ok": True, "action": "health", "bucket": target.bucket,
            "region": creds.region, "endpoint": target.endpoint,
            "domain": target.domain, "latency_ms": latency,
        })
        return 0
    err = err_from_resp(resp)
    known = {"NoSuchBucket": "桶不存在（检查 scope obs.bucket）",
             "AccessDenied": "凭据无权限（AK/SK 错误或无该桶权限）"}.get(err.get("error_code"))
    emit({
        "ok": False, "action": "health", "bucket": target.bucket,
        "region": creds.region, "latency_ms": latency, "error": err,
        "hint": known or "检查凭据/网络（error_code/request_id 可提工单）",
    })
    return 1


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="obscli.py",
        description="华为云 OBS 上传/管理（esdk-obs-python；纯 JSON 输出）",
    )
    p.add_argument("--scope", default=str(DEFAULT_SCOPE_PATH), help="scope.yaml 路径（默认仓库根）")
    sub = p.add_subparsers(dest="command", required=True)

    up = sub.add_parser("upload", help="上传文件/目录到桶（putFile ≤5GB）")
    up.add_argument("path", help="本地文件或目录")
    up.add_argument("--key", help="对象名（仅文件）；不给则 <prefix>/<basename>")
    up.add_argument("--prefix", help="目标前缀；目录默认 <目录名>/，--prefix \"\" 落桶根")
    up.add_argument("--bucket", help="覆盖 scope.obs.bucket")
    up.add_argument("--acl", choices=["private", ACL_PUBLIC_READ], default="private",
                    help="对象 ACL（默认 private）")
    up.add_argument("--content-type", help="MIME；不给则 SDK 按扩展名自动填充")
    up.add_argument("--storage-class", choices=list(STORAGE_CLASSES),
                    help="存储类别（默认 STANDARD）")
    up.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY,
                    help=f"目录并发上传线程数（默认 {DEFAULT_CONCURRENCY}）")
    up.add_argument("--dry-run", action="store_true", help="只打印上传计划，不触网")
    up.set_defaults(func=cmd_upload)

    ls = sub.add_parser("list", help="列举对象")
    ls.add_argument("--prefix", default="", help="对象名前缀过滤")
    ls.add_argument("--max", type=int, default=100, help="最多返回条数（默认 100）")
    ls.add_argument("--bucket", help="覆盖 scope.obs.bucket")
    ls.set_defaults(func=cmd_list)

    hd = sub.add_parser("head", help="查对象属性（404 = 不存在）")
    hd.add_argument("--key", required=True, help="对象名")
    hd.add_argument("--bucket", help="覆盖 scope.obs.bucket")
    hd.set_defaults(func=cmd_head)

    de = sub.add_parser("delete", help="删除对象（--key 可重复；--prefix 批量，先列举）")
    de.add_argument("--key", action="append", help="精确对象名（可重复多次）")
    de.add_argument("--prefix", help="按前缀批量删除（先列举再逐个删）")
    de.add_argument("--max", type=int, default=1000, help="--prefix 模式最多删除条数")
    de.add_argument("--bucket", help="覆盖 scope.obs.bucket")
    de.add_argument("--dry-run", action="store_true", help="只列出将删除的对象，不删除")
    de.set_defaults(func=cmd_delete)

    hh = sub.add_parser("health", help="健康检查（headBucket：配置解密/网络/凭据/桶存在）")
    hh.add_argument("--bucket", help="覆盖 scope.obs.bucket")
    hh.set_defaults(func=cmd_health)

    ul = sub.add_parser("url", help="取对象公开 URL + 带签名 URL")
    ul.add_argument("--key", required=True, help="对象名")
    ul.add_argument("--expires", type=int, default=3600, help="签名有效期秒数（默认 3600）")
    ul.add_argument("--bucket", help="覆盖 scope.obs.bucket")
    ul.set_defaults(func=cmd_url)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ValueError as e:
        emit({"ok": False, "command": args.command, "error": str(e)})
        return 1
    except FileNotFoundError as e:
        emit({"ok": False, "command": args.command, "error": f"文件不存在：{e}"})
        return 1
    except Exception as e:  # noqa: BLE001 —— SDK/网络异常兜底为 JSON 错误面
        emit({"ok": False, "command": args.command, "error": f"{type(e).__name__}: {e}"})
        return 1


if __name__ == "__main__":
    sys.exit(main())
