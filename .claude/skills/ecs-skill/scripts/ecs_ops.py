#!/usr/bin/env python
"""ecs_ops —— 华为云 ECS 操作与请求构造（官方 huaweicloudsdkecs.v2 SDK）。

只含纯逻辑：把 scope dict + CLI 覆盖映射成 typed CreateServersRequest，
以及就绪 IP 决策（从编排层收进来的纯函数）。
传输层（EcsClient 调用、轮询、探活）在 ecs.py 编排。
"""
from __future__ import annotations

import copy
import os
import secrets
import string
import uuid
from datetime import datetime, timedelta, timezone
from argparse import Namespace
from typing import Any

from huaweicloudsdkecs.v2 import (
    ChangeServerOsWithCloudInitOption,
    ChangeServerOsWithCloudInitRequestBody,
    ChangeServerOsWithCloudInitRequest,
    CreateServersRequest,
    CreateServersRequestBody,
    DeleteServersRequest,
    DeleteServersRequestBody,
    PrePaidServer,
    PrePaidServerEip,
    PrePaidServerEipBandwidth,
    PrePaidServerNic,
    PrePaidServerPublicip,
    PrePaidServerRootVolume,
    PrePaidServerSecurityGroup,
    ServerId,
)

# root_volume 默认：仅当 scope 完全没给时注入（只对低风险字段配硬默认）
DEFAULT_ROOT_VOLUME_TYPE = "SSD"
DEFAULT_ROOT_VOLUME_SIZE = 40

# 定时删除默认：新建按需机一律 24h 后由华为侧自动删除（联删系统盘 + EIP）
DEFAULT_TERMINATE_HOURS = 24
# API 约束：最短当前时间半小时后，最长不超过当前时间三年（3 年按 3*365 天近似）
MIN_TERMINATE_HOURS = 0.5
MAX_TERMINATE_HOURS = 24 * 365 * 3

# EIP 默认使用（scope 无 publicip 模板时的兜底；按流量计费）
DEFAULT_BANDWIDTH_SIZE = 5
DEFAULT_EIP_IPTYPE = "5_bgp"
DEFAULT_EIP_SHARETYPE = "PER"
DEFAULT_EIP_CHARGEMODE = "traffic"
DEFAULT_PUBLICIP = {
    "eip": {
        "iptype": DEFAULT_EIP_IPTYPE,
        "bandwidth": {"sharetype": DEFAULT_EIP_SHARETYPE, "size": DEFAULT_BANDWIDTH_SIZE, "chargemode": DEFAULT_EIP_CHARGEMODE},
    }
}

# ----------------------------------------------------------------------------
# 密码复杂度校验与生成（规则取自华为对 admin_pass 字段的定义）
# ----------------------------------------------------------------------------
PASSWORD_MIN_LEN = 8
PASSWORD_MAX_LEN = 26
PASSWORD_GENERATED_LEN = 16
PASSWORD_SPECIAL_CHARS = set("!@$%^-_=+[{}]:,./?")
# Linux 管理员账户为 root，故排除 root 及其逆序 toor（大小写不敏感）
PASSWORD_FORBIDDEN_SUBSTRINGS = ("root", "toor")
PASSWORD_ENV_VAR = "ECS_ADMIN_PASSWORD"


def validate_password(password: str) -> None:
    """本地预校验密码复杂度，不合规直接抛 ValueError 并指明违反的规则。

    规则：
    - 长度 8–26
    - 大写字母、小写字母、数字、特殊字符四类中至少满足三类
    - 特殊字符限于 PASSWORD_SPECIAL_CHARS
    - 不含用户名或其逆序（root / toor）
    """
    if not isinstance(password, str) or len(password) < PASSWORD_MIN_LEN:
        raise ValueError(
            f"密码长度不合规：需 {PASSWORD_MIN_LEN}–{PASSWORD_MAX_LEN} 位，实得 {len(password) if password else 0} 位。"
        )
    if len(password) > PASSWORD_MAX_LEN:
        raise ValueError(
            f"密码长度不合规：需 {PASSWORD_MIN_LEN}–{PASSWORD_MAX_LEN} 位，实得 {len(password)} 位。"
        )

    has_upper = any(c.isupper() for c in password)
    has_lower = any(c.islower() for c in password)
    has_digit = any(c.isdigit() for c in password)
    specials = {c for c in password if not c.isalnum()}
    has_special = bool(specials)
    classes = sum([has_upper, has_lower, has_digit, has_special])

    # 先检查是否有不允许的特殊字符
    disallowed = specials - PASSWORD_SPECIAL_CHARS
    if disallowed:
        raise ValueError(
            f"密码含不允许的特殊字符 {''.join(sorted(disallowed))!r}；"
            f"允许集为：{''.join(sorted(PASSWORD_SPECIAL_CHARS))}"
        )

    if classes < 3:
        missing = []
        if not has_upper:
            missing.append("大写字母")
        if not has_lower:
            missing.append("小写字母")
        if not has_digit:
            missing.append("数字")
        if not has_special:
            missing.append("特殊字符")
        raise ValueError(
            f"密码字符类不足：需至少满足三类（大写字母、小写字母、数字、特殊字符），"
            f"当前仅 {classes} 类；缺少：{', '.join(missing)}。"
        )

    lower_pwd = password.lower()
    for sub in PASSWORD_FORBIDDEN_SUBSTRINGS:
        if sub in lower_pwd:
            raise ValueError(
                f"密码不得包含用户名或其逆序（禁止子串：{sub}）。"
            )


def generate_password() -> str:
    """生成一个必定合规的强密码（16 位，特殊字符只从允许集选取）。"""
    upper = string.ascii_uppercase
    lower = string.ascii_lowercase
    digits = string.digits
    specials = "".join(sorted(PASSWORD_SPECIAL_CHARS))

    # 保证至少每类一个（四类全覆盖 → 必定 ≥ 三类）
    pwd = [
        secrets.choice(upper),
        secrets.choice(lower),
        secrets.choice(digits),
        secrets.choice(specials),
    ]
    pool = upper + lower + digits + specials
    pwd += [secrets.choice(pool) for _ in range(PASSWORD_GENERATED_LEN - 4)]
    # secrets 无 shuffle —— 用 SystemRandom 的 shuffle（CSPRNG 洗牌）
    secrets.SystemRandom().shuffle(pwd)
    result = "".join(pwd)

    # 生成的密码必须通过同一套校验（理论上是保证的，此处是安全网）
    validate_password(result)
    return result


# ----------------------------------------------------------------------------
# 登录鉴权方式裁决（六顺位，自上而下命中即停）
# ----------------------------------------------------------------------------
#   1 CLI --key         → 密钥对
#   2 CLI --password     → 密码
#   3 环境变量 ECS_ADMIN_PASSWORD → 密码
#   4 scope key_name     → 密钥对
#   5 scope password     → 密码
#   6 以上皆无           → 自动生成密码
def resolve_auth(scope: dict[str, Any], args: Namespace) -> tuple[str, str | None, str | None]:
    """裁决登录鉴权方式。

    返回 ``(auth_method, key_name, admin_pass)`` ——
    auth_method ∈ {"key_pair", "password"}。
    key_name 与 admin_pass 恒互斥：其中一个有值，另一个为 None。
    """
    cli_key = getattr(args, "key", None)
    cli_pwd = getattr(args, "password", None)
    env_pwd = os.getenv(PASSWORD_ENV_VAR)
    scope_key = _server_spec(scope).get("key_name") or None
    scope_pwd = _server_spec(scope).get("password") or None

    # 顺位 1：CLI 密钥对
    if cli_key:
        return "key_pair", cli_key, None

    # 顺位 2：CLI 密码
    if cli_pwd:
        validate_password(cli_pwd)
        return "password", None, cli_pwd

    # 顺位 3：环境变量密码
    if env_pwd:
        validate_password(env_pwd)
        return "password", None, env_pwd

    # 顺位 4：scope 密钥对（同层级内密钥对优先于密码）
    if scope_key:
        return "key_pair", scope_key, None

    # 顺位 5：scope 密码
    if scope_pwd:
        validate_password(scope_pwd)
        return "password", None, scope_pwd

    # 顺位 6：自动生成
    return "password", None, generate_password()


def mask_password(obj: Any) -> Any:
    """递归把 dict 中所有密码字段值替换为掩码 ******。

    覆盖三种键名：``admin_pass``（create 的 PrePaidServer）、
    ``adminpass``（change-os 的 ChangeServerOsWithCloudInitOption）、
    ``password``（scope 兜底字段）。
    在输出与归档的边界调用——不在结果对象构造时替换，
    因为 create 成功的 stdout 需要回显真实密码。
    ``and v`` 留过 None 值（裁决为密钥对时密码为 None，不掩码）。
    """
    if isinstance(obj, dict):
        return {
            k: ("******" if k in ("admin_pass", "adminpass", "password") and v else
                mask_password(v) if isinstance(v, (dict, list)) else v)
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [mask_password(x) for x in obj]
    return obj


def _server_spec(scope: dict[str, Any]) -> dict[str, Any]:
    ecs_create = scope.get("ecs_create") or {}
    if not isinstance(ecs_create, dict):
        raise ValueError("scope 的 ecs_create 必须是对象。")
    server = ecs_create.get("server") or {}
    if not isinstance(server, dict):
        raise ValueError("scope 的 ecs_create.server 必须是对象。")
    return server


def _spec_subnet(spec: dict[str, Any]) -> str:
    """从 scope 的 nics[0].subnet_id 取子网标识；无则空串。"""
    nics = spec.get("nics")
    if isinstance(nics, list) and nics and isinstance(nics[0], dict):
        return str(nics[0].get("subnet_id") or "")
    return ""


def _spec_sg(spec: dict[str, Any]) -> list[str]:
    """从 scope 的 security_groups 取全部安全组标识；无则空列表。"""
    sgs = spec.get("security_groups")
    if not isinstance(sgs, list):
        return []
    return [str(sg.get("id") or "") for sg in sgs if isinstance(sg, dict) and sg.get("id")]


def _require_fields(spec: dict[str, Any], args: Namespace) -> None:
    missing = []
    if not (args.image or spec.get("imageRef")):
        missing.append("imageRef")
    if not (args.flavor or spec.get("flavorRef")):
        missing.append("flavorRef")
    if not (getattr(args, "vpc", None) or spec.get("vpcid")):
        missing.append("vpcid")
    if not (getattr(args, "subnet", None) or _spec_subnet(spec)):
        missing.append("nics[0].subnet_id")
    if missing:
        raise ValueError(
            "server 缺少必填字段：" + ", ".join(missing)
            + "（请在 scope.ecs_create.server 或 CLI 提供）。"
        )


def resolve_auto_terminate_time(args: Namespace) -> str | None:
    """定时删除时刻：UTC ISO8601（yyyy-MM-ddTHH:mm:ssZ），秒归零。

    --no-auto-terminate 关闭；--terminate-hours 覆盖时长（默认 24h）。
    边界对齐 API：最短半小时后、最长三年；不合规早炸不发 API。
    ``args._now`` 为可注入时钟（测试缝），缺省取真实当前时刻。
    """
    if getattr(args, "no_auto_terminate", False):
        return None
    hours = getattr(args, "terminate_hours", None)
    if hours is None:
        hours = DEFAULT_TERMINATE_HOURS
    # math.isnan 在 both-comparisons-false 时漏网，显式拦下
    if hours != hours or hours < MIN_TERMINATE_HOURS or hours > MAX_TERMINATE_HOURS:
        raise ValueError(
            f"定时删除时长 {hours}h 越界：须在 {MIN_TERMINATE_HOURS}h 与 {MAX_TERMINATE_HOURS}h 之间"
            "（API 约束：最短半小时后、最长三年）。"
        )
    now = getattr(args, "_now", None) or datetime.now(timezone.utc)
    # 秒非 00 时华为侧自动取整到分钟开始；本地取整须向后（ceil），否则截断会把
    # 0.5h 下界实际提前到不足半小时，被 API 拒绝
    at = now + timedelta(hours=hours)
    if at.second or at.microsecond:
        at = at.replace(second=0, microsecond=0) + timedelta(minutes=1)
    else:
        at = at.replace(second=0, microsecond=0)
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


def build_create_request(scope: dict[str, Any], args: Namespace) -> CreateServersRequest:
    """合并 scope.ecs_create.server 默认 + CLI 覆盖，返回 typed CreateServersRequest。"""
    spec = _server_spec(scope)

    # 必填校验：宁可早炸也不把 None 静默发给 API
    _require_fields(spec, args)

    # root_volume：scope 完全没给 → 注入默认 {SSD, 40}；给了则原样尊重（不补默认 size）。
    # --disk-type / --disk-size 再覆盖盘类型/大小。
    rv_spec = spec.get("root_volume") if isinstance(spec.get("root_volume"), dict) else {}
    if rv_spec:
        vol_type = rv_spec.get("volumetype")
        size = rv_spec.get("size")
    else:
        vol_type = DEFAULT_ROOT_VOLUME_TYPE
        size = DEFAULT_ROOT_VOLUME_SIZE
    if getattr(args, "disk_type", None):
        vol_type = args.disk_type
    if getattr(args, "disk_size", None):
        size = args.disk_size
    root_volume = PrePaidServerRootVolume(volumetype=vol_type, size=size)

    # CLI 覆盖 scope（规格层 + 网络层：CLI > scope）。name 缺省 → 自动 ecs-<8hex>。
    # 只交付一台：不透传 count，由华为按缺省值建 1 台。
    # 可用区空串视为不给——不向服务端下发空值，由华为自选有货的 AZ。
    vpcid = getattr(args, "vpc", None) or spec.get("vpcid")
    subnet_id = getattr(args, "subnet", None) or _spec_subnet(spec)
    sg_ids = [getattr(args, "sg")] if getattr(args, "sg", None) else _spec_sg(spec)
    az = getattr(args, "az", None) or spec.get("availability_zone") or None

    name = args.name or spec.get("name") or ("ecs-" + uuid.uuid4().hex[:8])

    # 登录鉴权方式裁决（六顺位，命中即停）——密钥对与密码互斥
    auth_method, key_name, admin_pass = resolve_auth(scope, args)

    server = PrePaidServer(
        name=name,
        auto_terminate_time=resolve_auto_terminate_time(args),
        image_ref=args.image or spec.get("imageRef"),
        flavor_ref=args.flavor or spec.get("flavorRef"),
        vpcid=vpcid,
        key_name=key_name,
        admin_pass=admin_pass,
        availability_zone=az,
        nics=[PrePaidServerNic(subnet_id=subnet_id)],
        root_volume=root_volume,
        security_groups=[PrePaidServerSecurityGroup(id=sid) for sid in sg_ids],
        publicip=_build_publicip(spec, args),
    )

    body = CreateServersRequestBody(server=server)
    return CreateServersRequest(body=body)


def _build_publicip(spec: dict[str, Any], args: Namespace) -> PrePaidServerPublicip | None:
    """默认带 EIP（公网可达）；--no-eip 时不注入。带宽默认 5，--bandwidth 覆盖。"""
    if getattr(args, "no_eip", False):
        return None
    pub_spec = spec.get("publicip") if isinstance(spec.get("publicip"), dict) else copy.deepcopy(DEFAULT_PUBLICIP)
    eip_spec = pub_spec.get("eip") if isinstance(pub_spec.get("eip"), dict) else {}
    bw_spec = eip_spec.get("bandwidth") if isinstance(eip_spec.get("bandwidth"), dict) else {}
    size = getattr(args, "bandwidth", None) or bw_spec.get("size") or DEFAULT_BANDWIDTH_SIZE
    # delete_on_termination=True：定时删除路径只认创建时的该字段（事后不可补救），
    # 不设则到期只删机器、EIP 悬挂继续计费。手动 delete 走 delete_publicip=True 强删，与此正交。
    return PrePaidServerPublicip(
        delete_on_termination=True,
        eip=PrePaidServerEip(
            iptype=eip_spec.get("iptype", DEFAULT_EIP_IPTYPE),
            bandwidth=PrePaidServerEipBandwidth(
                size=size,
                sharetype=bw_spec.get("sharetype", DEFAULT_EIP_SHARETYPE),
                chargemode=bw_spec.get("chargemode", DEFAULT_EIP_CHARGEMODE),
            ),
        )
    )


# ----------------------------------------------------------------------------
# 就绪 IP 决策（纯函数，从编排层收进来——无需网络即可测试）
# ----------------------------------------------------------------------------
def _addr_entries(server: Any) -> list[Any]:
    """从 server.addresses 取所有地址条目（跨网络的扁平列表）。"""
    addrs = getattr(server, "addresses", None)
    if not isinstance(addrs, dict):
        return []
    out: list[Any] = []
    for items in addrs.values():
        if isinstance(items, list):
            out.extend(items)
    return out


def _addr_type(it: Any) -> str:
    """取一条地址的 OS-EXT-IPS:type（SDK 属性名 os_ext_ip_stype），小写。"""
    return str(getattr(it, "os_ext_ip_stype", "") or "").lower()


def _ip_by_type(server: Any, kind: str) -> str | None:
    """取指定 OS-EXT-IPS:type 的第一个 addr。"""
    for it in _addr_entries(server):
        if _addr_type(it) == kind:
            a = str(getattr(it, "addr", "")).strip()
            if a:
                return a
    return None


def _first_addr(server: Any, *, exclude_floating: bool = False) -> str | None:
    """兜底：取第一个非空 addr。exclude_floating 时跳过浮动 IP。"""
    for it in _addr_entries(server):
        if exclude_floating and _addr_type(it) == "floating":
            continue
        a = str(getattr(it, "addr", "")).strip()
        if a:
            return a
    return None


def floating_ip(server: Any) -> str | None:
    """浮动 EIP（公网）。"""
    return _ip_by_type(server, "floating")


def fixed_ip(server: Any) -> str | None:
    """固定私网 IP（兜底取第一个非浮动 addr——避免误取公网浮动 IP）。"""
    return _ip_by_type(server, "fixed") or _first_addr(server, exclude_floating=True)


def decide_ready_ip(server: Any | None, has_eip: bool) -> tuple[str | None, str]:
    """纯函数：根据 EIP 模式决定就绪可达 IP。

    has_eip=True：只接受公网浮动 IP；浮动 IP 尚未出现时返回 ``(None, "floating")``
        —— 调用方据此继续轮询，**不得回退私网**。
    has_eip=False（``--no-eip``）：取同 VPC 私网固定 IP。

    返回 ``(ip, ip_type)``，ip_type ∈ {"floating", "private"}。
    """
    if server is None:
        return None, "floating" if has_eip else "private"
    if has_eip:
        return floating_ip(server), "floating"
    return fixed_ip(server), "private"


# ----------------------------------------------------------------------------
# 切换操作系统（change-os）请求构造
# ----------------------------------------------------------------------------
# 华为云 ChangeServerOsWithCloudInit——带 cloud-init 的切换操作系统。
# Ubuntu 镜像默认装了 cloud-init，切换后需 cloud-init 注入密码/密钥。
CHANGE_OS_MODE = "withStopServer"


def build_change_os_request(scope: dict[str, Any], args: Namespace) -> ChangeServerOsWithCloudInitRequest:
    """构造 ``ChangeServerOsWithCloudInitRequest``（把已有 ECS 的系统盘镜像替换为指定镜像）。

    纯逻辑：把 scope + CLI 映射成 typed request，不触网。

    - ``--instance-id`` / ``--image-id`` 必填，缺失时抛 ValueError 并点名。
    - ``--password`` 与 ``--key`` 互斥：同时给时密钥对优先（与 create 的裁决一致）。
    - ``mode`` 恒为 ``withStopServer``——开机状态下自动关机再切换，不暴露 CLI。
    - ``--password`` 不给且无 ``--key`` 时，从 scope ``ecs_create.server.password`` 兜底。
    - change-os **不自动生成密码**——无凭证时直接报错（切换后必须能登录）。
    """
    instance_id = (getattr(args, "instance_id", None) or "").strip()
    image_id = (getattr(args, "image_id", None) or "").strip()
    if not instance_id:
        raise ValueError("change-os 需要 --instance-id（目标 ECS server id）。")
    if not image_id:
        raise ValueError("change-os 需要 --image-id（新系统盘镜像 ID）。")

    cli_key = getattr(args, "key", None) or None
    cli_pwd = getattr(args, "password", None) or None

    if cli_key:
        keyname, adminpass = cli_key, None
    elif cli_pwd:
        validate_password(cli_pwd)
        keyname, adminpass = None, cli_pwd
    else:
        scope_pwd = _server_spec(scope).get("password") or None
        if scope_pwd:
            validate_password(scope_pwd)
            keyname, adminpass = None, scope_pwd
        else:
            raise ValueError(
                "change-os 需要登录凭证：请用 --key（密钥对）或 --password（密码），"
                "或在 scope.ecs_create.server.password 配置密码（change-os 不自动生成）。"
            )

    os_change = ChangeServerOsWithCloudInitOption(
        imageid=image_id,
        adminpass=adminpass,
        keyname=keyname,
        mode=CHANGE_OS_MODE,
    )
    body = ChangeServerOsWithCloudInitRequestBody(os_change=os_change)
    return ChangeServerOsWithCloudInitRequest(server_id=instance_id, body=body)


# ----------------------------------------------------------------------------
# 删除（delete）请求构造
# ----------------------------------------------------------------------------
def build_delete_request(instance_id: str) -> DeleteServersRequest:
    """构造 ``DeleteServersRequest``（删除 ECS + 系统盘 + EIP + 数据盘）。

    纯逻辑：把 resolved instance_id 映射成 typed request，不触网。

    - instance_id 必填（由 cmd_delete 在调用前从 --id 或 --name 解析）。
    - ``delete_publicip`` 恒为 True——释放绑定的 EIP，不解绑悬挂。
    - ``delete_volume`` 恒为 True——删除数据盘；系统盘随实例默认删除。
    - 一次删一台（servers 列表只含一条）。
    """
    sid = (instance_id or "").strip()
    if not sid:
        raise ValueError("delete 需要 --id 或 --name（目标 ECS 标识）。")
    body = DeleteServersRequestBody(
        delete_publicip=True,
        delete_volume=True,
        servers=[ServerId(id=sid)],
    )
    return DeleteServersRequest(body=body)
