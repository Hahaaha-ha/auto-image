#!/usr/bin/env python
"""华为云 ECS skill 入口 —— 拉起(create) + 查询(show) + 切换 OS(change-os)。

官方 huaweicloudsdkecs.v2 SDK（取代原 CustomClient 手搓 HTTP）。
纯 JSON 输出（stdout 只出 JSON），logs/ 归档，--dry-run 杠杆。
"""
from __future__ import annotations

import argparse
import json
import socket
import sys
import time
from pathlib import Path
from typing import Any

from huaweicloudsdkecs.v2 import CreateServersRequest, ListServersDetailsRequest

from ecs_client import DEFAULT_SCOPE_PATH, build_client, load_scope_config, resolve_credentials
from ecs_ops import (
    DEFAULT_TERMINATE_HOURS,
    MAX_TERMINATE_HOURS,
    MIN_TERMINATE_HOURS,
    build_change_os_request,
    build_create_request,
    build_delete_request,
    decide_ready_ip,
    fixed_ip,
    floating_ip,
    mask_password,
)

LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"
DEFAULT_POLL_TIMEOUT = 600
DEFAULT_POLL_INTERVAL = 10
DEFAULT_PROBE_TIMEOUT = 5
DEFAULT_PORT_GRACE = 60
DEFAULT_LIST_LIMIT = 10


# ----------------------------------------------------------------------------
# 模型序列化（--dry-run 用：递归把 typed model 转成可 JSON 的 dict）
# ----------------------------------------------------------------------------
def _jsonable(o: Any) -> Any:
    if hasattr(o, "to_dict"):
        return _jsonable(o.to_dict())
    if isinstance(o, dict):
        return {k: _jsonable(v) for k, v in o.items() if v is not None}
    if isinstance(o, list):
        return [_jsonable(x) for x in o]
    return o


# ----------------------------------------------------------------------------
# 就绪探测
# ----------------------------------------------------------------------------
# IP 提取与就绪决策（floating_ip / fixed_ip / decide_ready_ip）从 ecs_ops 导入。

def tcp_probe(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def wait_for_port(host, port, *, grace, interval, probe_timeout, trace) -> bool:
    """grace 秒内反复探端口直到通或超时（ACTIVE 后等 sshd 首启）。"""
    if not host:
        return False
    deadline = time.monotonic() + grace
    while True:
        if tcp_probe(host, port, probe_timeout):
            return True
        trace.append({"t": time.strftime("%H:%M:%S"), "port22": "closed"})
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)


# ----------------------------------------------------------------------------
# ECS 操作（传输层）
# ----------------------------------------------------------------------------
def show_server(client, *, server_id: str) -> Any | None:
    """按 id 查单台；不存在返回 None。"""
    resp = client.list_servers_details(
        ListServersDetailsRequest(server_id=server_id, limit=DEFAULT_LIST_LIMIT)
    )
    for s in (resp.servers or []):
        if str(getattr(s, "id", "")) == str(server_id) and str(getattr(s, "status", "") or "").strip():
            return s
    return None


def find_server_by_name(client, *, name: str) -> Any | None:
    resp = client.list_servers_details(
        ListServersDetailsRequest(name=name, limit=DEFAULT_LIST_LIMIT)
    )
    for s in (resp.servers or []):
        if str(getattr(s, "name", "")) == name and str(getattr(s, "status", "") or "").strip():
            return s
    return None


def _obj_id(obj: Any) -> str:
    """取对象/字典形态的 id 字段（API 返回结构两种形态都可能出现）。"""
    if isinstance(obj, dict):
        return str(obj.get("id", "") or "")
    return str(getattr(obj, "id", "") or "")


def poll_until_ready(client, *, server_id, timeout, interval, trace, has_eip,
                     expect_image_id: str | None = None) -> tuple[Any | None, str, str | None]:
    """轮询直至 ACTIVE + 可达 IP 出现（或超时/错误）。

    has_eip=True 时，ACTIVE 后仍需公网浮动 IP 出现才算地址就绪——
    浮动 IP 迟迟不来时继续轮询直至超时，**绝不回退私网**。
    expect_image_id 非空时，还要求镜像元数据已变为该镜像才算就绪——
    change-os 提交后旧系统仍 ACTIVE + 22 通，仅凭状态/端口会在换盘
    完成前误判；镜像 ID 已切换是换盘发生的正向证据。
    返回 (server, status, ip)。
    """
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        srv = show_server(client, server_id=server_id)
        status = str(getattr(srv, "status", "UNKNOWN") or "UNKNOWN").upper() if srv else "PENDING"
        last = srv
        ip, _ = decide_ready_ip(srv, has_eip)
        image_now = _obj_id(getattr(srv, "image", None)) if srv else ""
        trace.append({"t": time.strftime("%H:%M:%S"), "status": status,
                      "ip": ip or "", "image": image_now})
        if status == "ACTIVE" and ip and (
                expect_image_id is None or image_now == expect_image_id):
            return srv, "ACTIVE", ip
        if status in ("ERROR", "FAILED"):
            return srv, status, None
        time.sleep(interval)
    return last, "TIMEOUT", None


def summarize_server(server: Any) -> dict[str, Any]:
    ip = floating_ip(server) or fixed_ip(server)
    return {
        "id": getattr(server, "id", ""),
        "name": getattr(server, "name", ""),
        "status": str(getattr(server, "status", "UNKNOWN") or "UNKNOWN").upper(),
        "ip": ip,
        "ip_type": "floating" if (ip and ip == floating_ip(server)) else "private",
        "flavor": _obj_id(getattr(server, "flavor", None)),
        "image": _obj_id(getattr(server, "image", None)),
    }


# ----------------------------------------------------------------------------
# 输出 / 日志
# ----------------------------------------------------------------------------
def emit(obj: dict[str, Any]) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def new_log_path(name: str) -> Path:
    """本次运行的日志路径：一次运行一个文件，创建后先落盘、结束时覆盖写。"""
    return LOGS_DIR / f"{name}-{time.strftime('%Y%m%d-%H%M%S')}.json"


def write_log(path: Path, payload: dict[str, Any]) -> str:
    """归档日志到文件。密码在归档边界恒为掩码——无论创建/失败/结束。"""
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(mask_password(_jsonable(payload)), fh, ensure_ascii=False, indent=2)
        return str(path)
    except OSError:
        return ""


# ----------------------------------------------------------------------------
# 命令
# ----------------------------------------------------------------------------
def cmd_create(args: argparse.Namespace) -> int:
    scope = load_scope_config(Path(args.scope))
    creds = resolve_credentials(scope)
    req = build_create_request(scope, args)
    server_name = req.body.server.name
    has_eip = req.body.server.publicip is not None

    # --dry-run：仅打印解析后的请求，不调 API（确认杠杆）
    # 密码在输出边界脱敏——此路径不创建机器，密码无实际意义
    if args.dry_run:
        emit(mask_password({
            "ok": True, "action": "create", "dry_run": True, "region": creds.region,
            "project_id": creds.project_id, "request": _jsonable(req),
            "note": ("带公网 EIP（按流量计费）" if has_eip else "不带 EIP（同 VPC 私网可达）") + "；未调用 API。",
        }))
        return 0

    client = build_client(creds)

    # --validate：服务端预检（dry_run=true），不真创建
    if args.validate:
        req.body.dry_run = True
        try:
            resp = client.create_servers(req)
            emit({"ok": True, "action": "create", "validate": True, "region": creds.region,
                  "name": server_name, "response": _jsonable(resp)})
            return 0
        except Exception as exc:
            emit({"ok": False, "action": "create", "validate": True, "name": server_name,
                  "error": str(exc)})
            return 1

    # 真创建（非交互）
    log_file = new_log_path(server_name)
    try:
        resp = client.create_servers(req)
    except Exception as exc:
        result = {"ok": False, "action": "create", "name": server_name,
                  "error": f"CreateServers 调用失败：{exc}"}
        result["log"] = write_log(log_file, {"action": "create", "name": server_name,
                                             "region": creds.region, "project_id": creds.project_id,
                                             "request": _jsonable(req), "error": str(exc)})
        emit(result)
        return 1

    server_ids = getattr(resp, "server_ids", []) or []
    server_id = server_ids[0] if server_ids else ""
    trace: list[dict[str, Any]] = []
    log_payload: dict[str, Any] = {
        "action": "create", "name": server_name, "region": creds.region, "project_id": creds.project_id,
        "request": _jsonable(req), "create_response": _jsonable(resp), "poll_trace": trace,
    }

    if not server_id:
        log_path = write_log(log_file, {**log_payload, "final": {"ok": False, "error": "未返回 serverIds"}})
        emit({"ok": False, "action": "create", "name": server_name, "job_id": getattr(resp, "job_id", ""),
              "error": "CreateServers 未返回 serverIds", "log": log_path})
        return 1

    # 机器已建出（开始计费）。先落一次盘并向 stderr 报 id：此后若轮询期抛异常、
    # 或进程被 Ctrl-C / 上层工具超时杀掉，server_id 都不会丢，可 `show --id` 复查。
    # stdout 仍保持纯 JSON —— 提示只走 stderr。
    write_log(log_file, {**log_payload, "final": {
        "ok": False, "id": server_id, "status": "CREATED",
        "note": "已创建但轮询未完成；若进程在此中断，用 `show --id` 复查（未立即销毁）。",
    }})
    print(f"[ecs] created id={server_id} name={server_name}（轮询中；中断请用 show --id 复查）",
          file=sys.stderr, flush=True)

    # 轮询 ACTIVE + 可达 IP → 探 22
    server, status, ip = poll_until_ready(
        client, server_id=server_id, timeout=args.timeout,
        interval=args.poll_interval, trace=trace, has_eip=has_eip,
    )
    if status == "ACTIVE" and ip:
        port_open = wait_for_port(ip, 22, grace=args.port_grace,
                                  interval=min(args.poll_interval, 10),
                                  probe_timeout=DEFAULT_PROBE_TIMEOUT, trace=trace)
    else:
        port_open = False
    ready = status == "ACTIVE" and bool(ip) and port_open

    ip_type = "floating" if has_eip else "private"
    result = {
        "ok": ready, "action": "create", "name": server_name, "id": server_id,
        "ip": ip, "ip_type": ip_type,
        "status": status, "ssh_port_open": port_open,
        "region": creds.region, "flavor": req.body.server.flavor_ref,
        "image": req.body.server.image_ref, "job_id": getattr(resp, "job_id", ""),
        "auto_terminate_time": req.body.server.auto_terminate_time,
    }
    # 登录鉴权方式回显：成功时附上 auth_method + 密码供下游取用
    if ready:
        result["auth_method"] = "key_pair" if req.body.server.key_name else "password"
        if req.body.server.admin_pass:
            result["admin_pass"] = req.body.server.admin_pass
    if not ready:
        # 机器已排定定时删除：排障有时限，到点华为侧自动删（含系统盘+EIP）
        if req.body.server.auto_terminate_time:
            result["auto_terminate_time"] = req.body.server.auto_terminate_time
        server_status = str(getattr(server, "status", "") or "").upper() if server else ""
        if status == "TIMEOUT":
            if server_status == "ACTIVE" and has_eip:
                result["error"] = f"轮询超时（{args.timeout}s）：已 ACTIVE 但公网浮动 IP 始终未出现"
                result["hint"] = "请检查 EIP 配额/权限（如 eip:publicIps:create）；未立即销毁。",
            else:
                result["error"] = f"轮询超时（{args.timeout}s）仍未 ACTIVE"
                result["hint"] = "机器可能仍在创建，稍后用 `show --id` 复查；未立即销毁。",
        elif status in ("ERROR", "FAILED"):
            result["error"] = f"ECS 进入 {status} 状态"
        elif not port_open:
            result["error"] = "已 ACTIVE 但 22 端口不通"
            if has_eip:
                result["hint"] = "请确认安全组对公网放行 22；未立即销毁。",
            else:
                result["hint"] = "请确认该 VPC 安全组放行 22、且本机与新机同子 subnet；未立即销毁。",

    log_payload["final"] = result
    result["log"] = write_log(log_file, log_payload)
    emit(result)
    return 0 if ready else 1


def cmd_show(args: argparse.Namespace) -> int:
    scope = load_scope_config(Path(args.scope))
    creds = resolve_credentials(scope)
    client = build_client(creds)

    if args.id:
        server = show_server(client, server_id=args.id)
    elif args.name:
        server = find_server_by_name(client, name=args.name)
    else:
        emit({"ok": False, "action": "show", "error": "需要 --id 或 --name 之一"})
        return 1

    if not server:
        emit({"ok": False, "action": "show", "id": args.id or "", "name": args.name or "",
              "error": "未找到该 ECS（可能尚未建出或已删除）"})
        return 1

    summary = summarize_server(server)
    summary["ssh_port_open"] = tcp_probe(summary["ip"], 22, DEFAULT_PROBE_TIMEOUT) if summary["ip"] else False
    emit({"ok": True, "action": "show", "server": summary})
    return 0


def cmd_change_os(args: argparse.Namespace) -> int:
    scope = load_scope_config(Path(args.scope))
    creds = resolve_credentials(scope)
    req = build_change_os_request(scope, args)
    instance_id = req.server_id
    image_id = req.body.os_change.imageid

    # --dry-run：仅打印解析后的请求，不调 API（确认杠杆）
    # 密码在输出边界脱敏——此路径不切换 OS，密码无实际意义
    if args.dry_run:
        emit(mask_password({
            "ok": True, "action": "change-os", "dry_run": True, "region": creds.region,
            "project_id": creds.project_id, "request": _jsonable(req),
        }))
        return 0

    client = build_client(creds)
    log_file = new_log_path(f"ecs-change-os-{instance_id[:8] or 'unknown'}")

    # 预探测 EIP 模式：change-os 不改变 EIP 绑定，提交前查一次决定就绪 IP 路径。
    # 带 EIP → 轮询浮动 IP；不带 → 取私网固定 IP（同 poll_until_ready 的 has_eip 语义）。
    pre = show_server(client, server_id=instance_id)
    if pre is None:
        result = {"ok": False, "action": "change-os", "instance_id": instance_id,
                  "image_id": image_id, "status": "NOT_FOUND",
                  "ip": None, "ip_type": None, "ssh_port_open": False,
                  "region": creds.region,
                  "error": "目标 ECS 不存在（可能已删除或 id 错误）。",
                  "hint": "请确认 --instance-id 正确；未自动回滚。"}
        result["log"] = write_log(log_file, {
            "action": "change-os", "instance_id": instance_id, "image_id": image_id,
            "region": creds.region, "request": _jsonable(req), "final": result})
        emit(result)
        return 1
    has_eip = floating_ip(pre) is not None

    trace: list[dict[str, Any]] = []
    log_payload: dict[str, Any] = {
        "action": "change-os", "instance_id": instance_id, "image_id": image_id,
        "region": creds.region, "project_id": creds.project_id,
        "has_eip": has_eip, "request": _jsonable(req), "poll_trace": trace,
    }

    try:
        client.change_server_os_with_cloud_init(req)
    except Exception as exc:
        result = {"ok": False, "action": "change-os", "instance_id": instance_id,
                  "image_id": image_id, "error": f"ChangeServerOsWithCloudInit 调用失败：{exc}"}
        result["log"] = write_log(log_file, {**log_payload, "final": result, "error_detail": str(exc)})
        emit(result)
        return 1

    # 切换已提交（华为自动关机→替换系统盘→重装→重启）。先落盘：中断后可用 show --id 复查。
    write_log(log_file, {**log_payload, "final": {
        "ok": False, "instance_id": instance_id, "image_id": image_id, "status": "SUBMITTED",
        "note": "已提交切换 OS；若进程在此中断，用 `show --id` 复查（未自动回滚）。",
    }})
    print(f"[ecs] change-os submitted id={instance_id} image={image_id}"
          f"（轮询中；中断请用 show --id 复查）",
          file=sys.stderr, flush=True)

    # 轮询 ACTIVE + 探 22（复用 create 的 poll_until_ready / wait_for_port）。
    # 就绪判定要求镜像元数据已变为目标镜像：change-os 提交后旧系统仍
    # ACTIVE + 22 通，仅凭状态/端口会误判；镜像 ID 已切换 = 换盘正向证据。
    # 限制：用同一镜像重装时旧新 ID 相同，该证据失效，判定退回状态+端口。
    server, status, ip = poll_until_ready(
        client, server_id=instance_id, timeout=args.timeout,
        interval=args.poll_interval, trace=trace, has_eip=has_eip,
        expect_image_id=image_id,
    )
    if status == "ACTIVE" and ip:
        port_open = wait_for_port(ip, 22, grace=args.port_grace,
                                  interval=min(args.poll_interval, 10),
                                  probe_timeout=DEFAULT_PROBE_TIMEOUT, trace=trace)
    else:
        port_open = False
    ready = status == "ACTIVE" and bool(ip) and port_open

    ip_type = "floating" if has_eip else "private"
    result: dict[str, Any] = {
        "ok": ready, "action": "change-os", "instance_id": instance_id, "image_id": image_id,
        "ip": ip, "ip_type": ip_type, "status": status, "ssh_port_open": port_open,
        "region": creds.region,
    }
    if not ready:
        if status == "TIMEOUT":
            result["error"] = f"轮询超时（{args.timeout}s）仍未 ACTIVE"
            result["hint"] = "切换可能仍在进行，稍后用 `show --id` 复查；未自动回滚。"
        elif status in ("ERROR", "FAILED"):
            result["error"] = f"ECS 进入 {status} 状态"
            result["hint"] = "切换 OS 失败；未自动回滚，用 `show --id` 复查。"
        elif not port_open:
            result["error"] = "已 ACTIVE 但 22 端口不通"
            result["hint"] = "请确认安全组放行 22；未自动回滚。"

    log_payload["final"] = result
    result["log"] = write_log(log_file, log_payload)
    emit(result)
    return 0 if ready else 1


# ----------------------------------------------------------------------------
# 删除
# ----------------------------------------------------------------------------
def poll_until_deleted(client, *, server_id, timeout, interval, trace) -> str:
    """轮询直至实例已删除（或超时）。返回 status：DELETED 或 TIMEOUT。

    华为云删除后实例不会立即从 API 消失，而是以 DELETED 状态保留一段时间，
    因此 DELETED 状态即视为删除成功。实例彻底从列表消失（None）同样判定成功。

    ERROR 态不提前退出——删除过程中实例可能短暂进入 ERROR，
    继续轮询直至 DELETED / 消失 / 超时。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        srv = show_server(client, server_id=server_id)
        status = str(getattr(srv, "status", "") or "").upper() if srv else "GONE"
        trace.append({"t": time.strftime("%H:%M:%S"), "status": status})
        if srv is None or status == "DELETED":
            return "DELETED"
        time.sleep(interval)
    return "TIMEOUT"


# ssh 别名清理提醒——delete 成功和幂等 NOT_FOUND 都附带。
_SSH_ALIAS_HINT = "如该机器注册过 ssh 别名，请另行用 ssh-skill 清理。"


def cmd_delete(args: argparse.Namespace) -> int:
    scope = load_scope_config(Path(args.scope))
    creds = resolve_credentials(scope)

    # --dry-run：不触网，仅预览将删什么
    if args.dry_run:
        if args.id:
            req = build_delete_request(args.id)
            emit({
                "ok": True, "action": "delete", "dry_run": True, "region": creds.region,
                "id": args.id, "request": _jsonable(req),
                "note": "未调用 API；实际执行将删除 ECS + 系统盘 + EIP + 数据盘。",
            })
        else:
            # --name 的 dry-run 无法解析 id（不触网），输出 cascade 预览。
            emit({
                "ok": True, "action": "delete", "dry_run": True, "region": creds.region,
                "name": args.name,
                "cascade": {"delete_publicip": True, "delete_volume": True},
                "note": "未调用 API；实际执行将先查询 name→id 再删除 ECS + 系统盘 + EIP + 数据盘。",
            })
        return 0

    client = build_client(creds)

    # 解析目标并确认存在：--id 直接查，--name 先按名找。
    # 两条路径最终都拿到 server 对象或判定不存在（幂等成功）。
    if args.id:
        pre = show_server(client, server_id=args.id)
        if pre is None:
            emit({"ok": True, "action": "delete", "id": args.id,
                  "status": "NOT_FOUND", "note": "目标 ECS 不存在，视为已删除。",
                  "hint": _SSH_ALIAS_HINT})
            return 0
    else:
        pre = find_server_by_name(client, name=args.name)
        if pre is None:
            emit({"ok": True, "action": "delete", "name": args.name,
                  "status": "NOT_FOUND", "note": "目标 ECS 不存在，视为已删除。",
                  "hint": _SSH_ALIAS_HINT})
            return 0

    instance_id = str(getattr(pre, "id", "") or "")
    server_name = str(getattr(pre, "name", "") or "")
    server_ip = floating_ip(pre) or fixed_ip(pre) or ""

    req = build_delete_request(instance_id)
    log_file = new_log_path(f"ecs-delete-{instance_id[:8] or 'unknown'}")
    trace: list[dict[str, Any]] = []
    log_payload: dict[str, Any] = {
        "action": "delete", "id": instance_id, "name": server_name,
        "ip": server_ip, "region": creds.region,
        "request": _jsonable(req), "poll_trace": trace,
    }

    # 提交删除（华为异步：返回 job_id，后台删除实例+盘+EIP）
    try:
        resp = client.delete_servers(req)
    except Exception as exc:
        result = {"ok": False, "action": "delete", "id": instance_id, "name": server_name,
                  "error": f"DeleteServers 调用失败：{exc}"}
        result["log"] = write_log(log_file, {**log_payload, "final": result, "error_detail": str(exc)})
        emit(result)
        return 1

    job_id = getattr(resp, "job_id", "") or ""
    print(f"[ecs] delete submitted id={instance_id} name={server_name}"
          f"（轮询中；中断可用 show --id 复查）",
          file=sys.stderr, flush=True)

    # 轮询直至实例消失
    status = poll_until_deleted(
        client, server_id=instance_id, timeout=args.timeout,
        interval=args.poll_interval, trace=trace,
    )

    deleted = status == "DELETED"
    result: dict[str, Any] = {
        "ok": deleted, "action": "delete", "id": instance_id, "name": server_name,
        "ip": server_ip, "status": status, "job_id": job_id,
        "region": creds.region,
    }
    if deleted:
        result["hint"] = _SSH_ALIAS_HINT
    else:
        result["error"] = f"轮询超时（{args.timeout}s）仍未删除"
        result["hint"] = "删除可能仍在进行，稍后用 `show --id` 复查。"

    log_payload["final"] = result
    result["log"] = write_log(log_file, log_payload)
    emit(result)
    return 0 if deleted else 1


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="华为云 ECS skill：拉起(create) + 查询(show) + 切换 OS(change-os)。纯 JSON 输出。默认带公网 EIP。",
    )
    parser.add_argument("--scope", default=str(DEFAULT_SCOPE_PATH), help="scope.yaml 路径")
    sub = parser.add_subparsers(dest="command", required=True)

    p_create = sub.add_parser("create", help="拉起一台 ECS 到就绪（ACTIVE + 可达 IP + 22 通）")
    p_create.add_argument("--name", help="机器名；不给则自动 ecs-<rand>")
    p_create.add_argument("--flavor", help="规格型 flavorRef（覆盖 scope）")
    p_create.add_argument("--image", help="镜像 imageRef（覆盖 scope）")
    p_create.add_argument("--key", help="密钥对 key_name（覆盖 scope）")
    p_create.add_argument("--password", help="登录密码（覆盖 scope；裁决优先级见 SKILL.md）")
    p_create.add_argument("--vpc", help="VPC ID（覆盖 scope）")
    p_create.add_argument("--subnet", help="子网 ID（覆盖 scope nics[0].subnet_id）")
    p_create.add_argument("--sg", help="安全组 ID（覆盖 scope security_groups[0].id）")
    p_create.add_argument("--az", help="可用区（覆盖 scope；不给则由华为自选有货的 AZ）")
    p_create.add_argument("--no-eip", dest="no_eip", action="store_true",
                          help="不带公网 EIP（退回同 VPC 私网路径；默认带 EIP）")
    p_create.add_argument("--disk-type", dest="disk_type", help="系统盘类型（覆盖 root_volume.volumetype）")
    p_create.add_argument("--disk-size", dest="disk_size", type=int, help="系统盘大小 GB（覆盖 root_volume.size）")
    p_create.add_argument("--bandwidth", type=int, help="EIP 带宽 Mbit/s（默认 5）")
    # 互斥：编排层透传两 flag 时静默忽略一个会造成持续计费的误解——直接报错
    term = p_create.add_mutually_exclusive_group()
    term.add_argument("--terminate-hours", dest="terminate_hours", type=float,
                     help=f"定时删除时长（小时；默认 {DEFAULT_TERMINATE_HOURS}，范围 {MIN_TERMINATE_HOURS}–{MAX_TERMINATE_HOURS}）")
    term.add_argument("--no-auto-terminate", dest="no_auto_terminate", action="store_true",
                     help="不设定时删除（长期机）；默认创建后 24h 自动删除（联删系统盘+EIP）")
    p_create.add_argument("--dry-run", action="store_true", help="仅打印解析后的请求，不调 API")
    p_create.add_argument("--validate", action="store_true", help="服务端预检(dry_run=true)，不真创建")
    p_create.add_argument("--timeout", type=int, default=DEFAULT_POLL_TIMEOUT, help=f"轮询 ACTIVE 超时秒（默认 {DEFAULT_POLL_TIMEOUT}）")
    p_create.add_argument("--poll-interval", dest="poll_interval", type=int, default=DEFAULT_POLL_INTERVAL, help=f"轮询间隔秒（默认 {DEFAULT_POLL_INTERVAL}）")
    p_create.add_argument("--port-grace", dest="port_grace", type=int, default=DEFAULT_PORT_GRACE, help=f"ACTIVE 后探 22 宽限秒（默认 {DEFAULT_PORT_GRACE}）")
    p_create.set_defaults(func=cmd_create)

    p_show = sub.add_parser("show", help="查询单台 ECS 状态/IP/22")
    target = p_show.add_mutually_exclusive_group(required=True)
    target.add_argument("--id", help="ECS server id")
    target.add_argument("--name", help="ECS 名称（精确匹配）")
    p_show.set_defaults(func=cmd_show)

    p_changos = sub.add_parser("change-os", help="切换一台 ECS 的操作系统（系统盘镜像替换→轮询 ACTIVE→探 22）")
    p_changos.add_argument("--instance-id", dest="instance_id", required=True, help="目标 ECS server id")
    p_changos.add_argument("--image-id", dest="image_id", required=True, help="新系统盘镜像 ID")
    p_changos.add_argument("--key", help="密钥对 key_name（与 --password 互斥；同时给时密钥对优先）")
    p_changos.add_argument("--password", help="登录密码（与 --key 互斥；不给时从 scope ecs_create.server.password 兜底）")
    p_changos.add_argument("--dry-run", action="store_true", help="仅打印请求体，不调 API")
    p_changos.add_argument("--timeout", type=int, default=DEFAULT_POLL_TIMEOUT, help=f"轮询 ACTIVE 超时秒（默认 {DEFAULT_POLL_TIMEOUT}）")
    p_changos.add_argument("--poll-interval", dest="poll_interval", type=int, default=DEFAULT_POLL_INTERVAL, help=f"轮询间隔秒（默认 {DEFAULT_POLL_INTERVAL}）")
    p_changos.add_argument("--port-grace", dest="port_grace", type=int, default=DEFAULT_PORT_GRACE, help=f"ACTIVE 后探 22 宽限秒（默认 {DEFAULT_PORT_GRACE}）")
    p_changos.set_defaults(func=cmd_change_os)

    p_delete = sub.add_parser("delete", help="删除一台 ECS（实例 + 系统盘 + EIP + 数据盘级联清理）")
    target_del = p_delete.add_mutually_exclusive_group(required=True)
    target_del.add_argument("--id", help="ECS server id")
    target_del.add_argument("--name", help="ECS 名称（精确匹配）")
    p_delete.add_argument("--dry-run", action="store_true", help="仅打印将删除什么，不调 API")
    p_delete.add_argument("--timeout", type=int, default=DEFAULT_POLL_TIMEOUT, help=f"轮询删除完成超时秒（默认 {DEFAULT_POLL_TIMEOUT}）")
    p_delete.add_argument("--poll-interval", dest="poll_interval", type=int, default=DEFAULT_POLL_INTERVAL, help=f"轮询间隔秒（默认 {DEFAULT_POLL_INTERVAL}）")
    p_delete.set_defaults(func=cmd_delete)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print(json.dumps({"ok": False, "error": "cancelled"}, ensure_ascii=False), file=sys.stderr)
        return 130
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
