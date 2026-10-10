"""华为云 ECS 实例管理（Web 后端）。

复用仓库根 scope.yaml 的顶层 ak/sk/region/project_id 与 ecs_create.server
段（与 ecs-skill 同一配置源，实现独立——服务不跨包 import skill 脚本）。
huaweicloudsdkecs 延迟导入：SDK 未装或凭据缺失时抛 EcsNotConfigured
（端点 503），不阻断 Web 启动与其余功能。scope 支持 {default: {...}}
包裹（与 ecs-skill load_scope_config 语义一致；obs.py 有意不解包）。

面板三件事：实例清单（list_instances）、一键存活检查（check_all：
status==ACTIVE 且 TCP 22 可达，与 ecs-skill 就绪判定同语义）、同步建机
（create_instance：轮询到 ACTIVE + 可达 IP + 22 通才返回，契约对齐
ecs-skill create 的输出）。SDK 调用是阻塞 IO——app.py 侧端点一律同步
def（FastAPI 自动派线程池，不占事件循环）。客户端按 scope 路径进程内
缓存。admin_pass 只在 create 成功响应里出现一次（用户需要它登录），
并登记进脱敏已知清单防止经 SSE 事件面泄漏；绝不写日志。
"""
from __future__ import annotations

import os
import secrets
import socket
import string
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

DEFAULT_SCOPE_PATH = Path(__file__).resolve().parent.parent / "scope.yaml"

# SDK 原生环境变量名（env 优先，与 ecs-skill 的 ecs_client 对齐）
ENV_AK = "HUAWEICLOUD_SDK_AK"
ENV_SK = "HUAWEICLOUD_SDK_SK"
ENV_REGION = "HUAWEICLOUD_SDK_REGION"
ENV_PROJECT_ID = "HUAWEICLOUD_SDK_PROJECT_ID"
PASSWORD_ENV_VAR = "ECS_ADMIN_PASSWORD"

# 轮询/探测常量（与 ecs-skill 的 ecs.py 同值）
POLL_TIMEOUT = 600
POLL_INTERVAL = 10
PROBE_TIMEOUT = 5
PORT_GRACE = 60
# 清单分页：单页与总量上限（API limit 上限 1000）、页数护栏
LIST_PAGE_LIMIT = 1000
LIST_MAX_LIMIT = 1000
LIST_MAX_PAGES = 10
# 存活检查：并发探测 worker 上限与单探测超时（全量检查在数秒内收口）
CHECK_MAX_WORKERS = 8
CHECK_PROBE_TIMEOUT = 4

# root_volume / EIP 默认（scope 完全没给时注入；与 ecs_ops 同值）
DEFAULT_ROOT_VOLUME_TYPE = "SSD"
DEFAULT_ROOT_VOLUME_SIZE = 40
DEFAULT_BANDWIDTH_SIZE = 5
DEFAULT_EIP_IPTYPE = "5_bgp"
DEFAULT_EIP_SHARETYPE = "PER"
DEFAULT_EIP_CHARGEMODE = "traffic"

# 密码复杂度（华为 admin_pass 字段定义，与 ecs_ops 同规则）
PASSWORD_MIN_LEN = 8
PASSWORD_MAX_LEN = 26
PASSWORD_GENERATED_LEN = 16
PASSWORD_SPECIAL_CHARS = set("!@$%^-_=+[{}]:,./?")
PASSWORD_FORBIDDEN_SUBSTRINGS = ("root", "toor")


class EcsNotConfigured(RuntimeError):
    """SDK 缺失、凭据缺失或 ecs_create.server 必填段不全（端点 503）。"""


class EcsApiError(RuntimeError):
    """ECS 服务端/网络失败（端点 502）；error 属性为可 JSON 错误面。"""

    def __init__(self, exc):
        self.error = {
            "status_code": getattr(exc, "status_code", None),
            "error_code": getattr(exc, "error_code", None),
            "error_msg": getattr(exc, "error_msg", None) or str(exc),
            "request_id": getattr(exc, "request_id", None),
        }
        super().__init__(str(self.error))


def _api(call):
    """SDK 调用统一包装：云侧/网络失败收口为 EcsApiError（端点 502）。"""
    try:
        return call()
    except (EcsNotConfigured, EcsApiError):
        raise
    except Exception as exc:  # noqa: BLE001 —— SDK 异常形状不固定，统一面
        raise EcsApiError(exc) from exc


def _read_scope(scope_path):
    """scope.yaml → dict（缺失/坏 YAML/非 dict 一律 {}，不抛）。

    兼容 {default: {...}} 包裹（ecs-skill 语义）。
    """
    try:
        data = yaml.safe_load(Path(scope_path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    if not isinstance(data, dict):
        return {}
    if isinstance(data.get("default"), dict):
        data = data["default"]
    return data


def _resolve_conf(scope_path):
    """scope.yaml + env → (ak, sk, region, project_id)。env 优先。

    缺 ak/sk/region → EcsNotConfigured（503）；project_id 可空（SDK 按
    region 自动推导）。
    """
    data = _read_scope(scope_path)
    ak = (os.getenv(ENV_AK) or str(data.get("ak", ""))).strip()
    sk = (os.getenv(ENV_SK) or str(data.get("sk", ""))).strip()
    region = (os.getenv(ENV_REGION) or str(data.get("region", ""))).strip()
    project_id = (os.getenv(ENV_PROJECT_ID) or str(data.get("project_id", ""))).strip()
    missing = [k for k, v in (("ak", ak), ("sk", sk), ("region", region)) if not v]
    if missing:
        raise EcsNotConfigured(
            "ECS not configured: scope.yaml needs " + "/".join(missing)
            + "（或环境变量 HUAWEICLOUD_SDK_AK/_SK/_REGION，env 优先）"
        )
    return ak, sk, region, project_id


_clients: dict[str, object] = {}


def _get_client(scope_path):
    """按 scope 路径缓存的 EcsClient（凭据随缓存固化；改 scope 需重启）。"""
    cache_key = str(Path(scope_path).resolve())
    if cache_key not in _clients:
        ak, sk, region, project_id = _resolve_conf(scope_path)
        try:
            from huaweicloudsdkcore.auth.credentials import BasicCredentials
            from huaweicloudsdkcore.client import ClientBuilder
            from huaweicloudsdkcore.region.region import Region
            from huaweicloudsdkecs.v2 import EcsClient
        except ImportError as exc:
            raise EcsNotConfigured(f"huaweicloudsdkecs not installed: {exc}") from exc
        credentials = (BasicCredentials(ak, sk, project_id) if project_id
                       else BasicCredentials(ak, sk))
        _clients[cache_key] = (
            ClientBuilder(EcsClient)
            .with_credentials(credentials)
            .with_region(Region(id=region, endpoint=f"https://ecs.{region}.myhuaweicloud.com"))
            .build()
        )
    return _clients[cache_key]


# ---------------------------------------------------------------------------
# 纯助手（语义照抄 ecs-skill 的 ecs_ops/ecs.py，勿重推导）
# ---------------------------------------------------------------------------

def _obj_id(obj):
    """取对象/字典形态的 id 字段（flavor/image 两种形态都可能出现）。"""
    if isinstance(obj, dict):
        return str(obj.get("id", "") or "")
    return str(getattr(obj, "id", "") or "")


def _addr_entries(server):
    """从 server.addresses 取所有地址条目（跨网络的扁平列表）。"""
    addrs = getattr(server, "addresses", None)
    if not isinstance(addrs, dict):
        return []
    out = []
    for items in addrs.values():
        if isinstance(items, list):
            out.extend(items)
    return out


def _addr_type(it):
    """取一条地址的 OS-EXT-IPS:type（SDK 属性名 os_ext_ip_stype），小写。"""
    return str(getattr(it, "os_ext_ip_stype", "") or "").lower()


def _ip_by_type(server, kind):
    for it in _addr_entries(server):
        if _addr_type(it) == kind:
            a = str(getattr(it, "addr", "")).strip()
            if a:
                return a
    return None


def _first_addr(server, *, exclude_floating=False):
    for it in _addr_entries(server):
        if exclude_floating and _addr_type(it) == "floating":
            continue
        a = str(getattr(it, "addr", "")).strip()
        if a:
            return a
    return None


def floating_ip(server):
    """浮动 EIP（公网）。"""
    return _ip_by_type(server, "floating")


def fixed_ip(server):
    """固定私网 IP（兜底取第一个非浮动 addr）。"""
    return _ip_by_type(server, "fixed") or _first_addr(server, exclude_floating=True)


def decide_ready_ip(server, has_eip):
    """EIP 模式决定就绪可达 IP：带 EIP 只认浮动 IP（不回退私网）。

    返回 (ip, ip_type)，ip_type ∈ {"floating", "private"}。
    """
    if server is None:
        return None, "floating" if has_eip else "private"
    if has_eip:
        return floating_ip(server), "floating"
    return fixed_ip(server), "private"


def validate_password(password):
    """密码复杂度本地预校验，不合规抛 ValueError 指明规则。

    长度 8–26、四类字符至少三类、特殊字符限于允许集、不含 root/toor。
    """
    if not isinstance(password, str) or len(password) < PASSWORD_MIN_LEN:
        raise ValueError(
            f"密码长度不合规：需 {PASSWORD_MIN_LEN}–{PASSWORD_MAX_LEN} 位，实得 "
            f"{len(password) if password else 0} 位。"
        )
    if len(password) > PASSWORD_MAX_LEN:
        raise ValueError(
            f"密码长度不合规：需 {PASSWORD_MIN_LEN}–{PASSWORD_MAX_LEN} 位，实得 {len(password)} 位。"
        )
    has_upper = any(c.isupper() for c in password)
    has_lower = any(c.islower() for c in password)
    has_digit = any(c.isdigit() for c in password)
    specials = {c for c in password if not c.isalnum()}
    classes = sum([has_upper, has_lower, has_digit, bool(specials)])
    disallowed = specials - PASSWORD_SPECIAL_CHARS
    if disallowed:
        raise ValueError(
            f"密码含不允许的特殊字符 {''.join(sorted(disallowed))!r}；"
            f"允许集为：{''.join(sorted(PASSWORD_SPECIAL_CHARS))}"
        )
    if classes < 3:
        raise ValueError(
            "密码字符类不足：需至少满足三类（大写字母、小写字母、数字、特殊字符），"
            f"当前仅 {classes} 类。"
        )
    lower_pwd = password.lower()
    for sub in PASSWORD_FORBIDDEN_SUBSTRINGS:
        if sub in lower_pwd:
            raise ValueError(f"密码不得包含用户名或其逆序（禁止子串：{sub}）。")


def generate_password():
    """生成必定合规的强密码（16 位，特殊字符只从允许集选取）。"""
    upper, lower, digits = string.ascii_uppercase, string.ascii_lowercase, string.digits
    special_pool = "".join(sorted(PASSWORD_SPECIAL_CHARS))
    pwd = [secrets.choice(upper), secrets.choice(lower),
           secrets.choice(digits), secrets.choice(special_pool)]
    pool = upper + lower + digits + special_pool
    pwd += [secrets.choice(pool) for _ in range(PASSWORD_GENERATED_LEN - 4)]
    secrets.SystemRandom().shuffle(pwd)
    result = "".join(pwd)
    validate_password(result)  # 安全网（理论上是保证的）
    return result


def tcp_probe(host, port, timeout):
    """TCP 端口可达性探测（socket 单连接尝试）。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _server_spec(scope):
    """scope.ecs_create.server 段；缺失/畸形 → EcsNotConfigured（503 语义）。"""
    ecs_create = scope.get("ecs_create") or {}
    if not isinstance(ecs_create, dict):
        raise EcsNotConfigured("scope 的 ecs_create 必须是对象。")
    server = ecs_create.get("server") or {}
    if not isinstance(server, dict):
        raise EcsNotConfigured("scope 的 ecs_create.server 必须是对象。")
    return server


def _spec_subnet(spec):
    """从 scope 的 nics[0].subnet_id 取子网标识；无则空串。"""
    nics = spec.get("nics")
    if isinstance(nics, list) and nics and isinstance(nics[0], dict):
        return str(nics[0].get("subnet_id") or "")
    return ""


def _spec_sg(spec):
    """从 scope 的 security_groups 取全部安全组标识；无则空列表。"""
    sgs = spec.get("security_groups")
    if not isinstance(sgs, list):
        return []
    return [str(sg.get("id")) for sg in sgs if isinstance(sg, dict) and sg.get("id")]


def _summarize(server):
    """SDK Server → 面板清单条目（纯投影，不触网）。"""
    ip = floating_ip(server) or fixed_ip(server)
    return {
        "id": str(getattr(server, "id", "") or ""),
        "name": str(getattr(server, "name", "") or ""),
        "status": str(getattr(server, "status", "") or "").upper() or "UNKNOWN",
        "ip": ip,
        "ip_type": ("floating" if ip == floating_ip(server) else "private") if ip else None,
        "flavor": _obj_id(getattr(server, "flavor", None)),
        "image": _obj_id(getattr(server, "image", None)),
        "availability_zone": str(getattr(server, "os_ext_a_zavailability_zone", "") or ""),
        "created": str(getattr(server, "created", "") or ""),
        "auto_terminate_time": getattr(server, "auto_terminate_time", None),
        "key_name": getattr(server, "key_name", None),
    }


def _show_server(client, server_id):
    """按 id 查单台（复用清单 API 的 server_id 过滤）；不存在返回 None。"""
    from huaweicloudsdkecs.v2 import ListServersDetailsRequest

    resp = _api(lambda: client.list_servers_details(
        ListServersDetailsRequest(server_id=server_id, limit=10)
    ))
    for s in (resp.servers or []):
        if (str(getattr(s, "id", "")) == str(server_id)
                and str(getattr(s, "status", "") or "").strip()):
            return s
    return None


# ---------------------------------------------------------------------------
# 公开函数（scope_path 首参，返回纯 dict）
# ---------------------------------------------------------------------------

def list_instances(scope_path, limit=500):
    """全量实例清单（offset 分页，页数护栏）→ 面板列表。"""
    _ak, _sk, region, _pid = _resolve_conf(scope_path)
    client = _get_client(scope_path)
    from huaweicloudsdkecs.v2 import ListServersDetailsRequest

    cap = max(1, min(int(limit), LIST_MAX_LIMIT))
    servers, offset, pages = [], 0, 0
    while len(servers) < cap and pages < LIST_MAX_PAGES:
        req = ListServersDetailsRequest(limit=min(LIST_PAGE_LIMIT, cap - len(servers)), offset=offset)
        resp = _api(lambda: client.list_servers_details(req))
        batch = list(resp.servers or [])
        if not batch:
            break
        servers.extend(batch)
        offset += len(batch)
        pages += 1
        total = int(getattr(resp, "count", 0) or 0)
        if total and len(servers) >= total:
            break
    return {"region": region, "count": len(servers), "instances": [_summarize(s) for s in servers[:cap]]}


def check_all(scope_path):
    """一键存活检查：全量清单 + 并发 TCP 22 探测。

    alive = status==ACTIVE 且 22 可达（与 ecs-skill 就绪判定同语义）。
    只配了私网 IP 的实例从服务所在网络探测不通是如实结果——alive=false，
    不静默跳过。无 IP 实例（BUILD 中等）ssh_port_open=false。
    """
    listing = list_instances(scope_path, limit=LIST_MAX_LIMIT)
    instances = listing["instances"]
    probe_items = [(i["id"], i["ip"]) for i in instances if i["ip"]]
    probes: dict[str, bool] = {}
    if probe_items:
        with ThreadPoolExecutor(max_workers=min(CHECK_MAX_WORKERS, len(probe_items))) as pool:
            results = list(pool.map(lambda it: tcp_probe(it[1], 22, CHECK_PROBE_TIMEOUT), probe_items))
        probes = {(iid, ip): open_ for (iid, ip), open_ in zip(probe_items, results)}
    out = []
    for i in instances:
        ssh_open = probes.get((i["id"], i["ip"]), False)
        entry = {**i, "ssh_port_open": ssh_open, "alive": i["status"] == "ACTIVE" and ssh_open}
        out.append(entry)
    return {
        "region": listing["region"],
        "checked_at": time.time(),
        "count": len(out),
        "alive_count": sum(1 for e in out if e["alive"]),
        "instances": out,
    }


def resolve_defaults(scope_path):
    """scope.yaml + env → 新建表单默认值视图（纯读取，永不抛、无密钥值）。

    configured = 凭据可解析 且 ecs_create.server 必填四项（imageRef/
    flavorRef/vpcid/nics[0].subnet_id）齐备；未就绪时带 reason 供面板提示。
    """
    scope = _read_scope(scope_path)
    try:
        _resolve_conf(scope_path)
        creds_ok, reason = True, None
    except EcsNotConfigured as exc:
        creds_ok, reason = False, str(exc)
    region = (os.getenv(ENV_REGION) or str(scope.get("region", ""))).strip() or None
    spec = {}
    if isinstance(scope.get("ecs_create"), dict) and isinstance(scope["ecs_create"].get("server"), dict):
        spec = scope["ecs_create"]["server"]
    rv = spec.get("root_volume") if isinstance(spec.get("root_volume"), dict) else {}
    eip = {}
    pub = spec.get("publicip") if isinstance(spec.get("publicip"), dict) else {}
    if isinstance(pub.get("eip"), dict) and isinstance(pub["eip"].get("bandwidth"), dict):
        eip = pub["eip"]["bandwidth"]
    missing = [f for f in ("imageRef", "flavorRef", "vpcid") if not str(spec.get(f) or "").strip()]
    if not _spec_subnet(spec):
        missing.append("nics[0].subnet_id")
    env_pwd = bool(os.getenv(PASSWORD_ENV_VAR))
    key_name = str(spec.get("key_name") or "").strip()
    scope_pwd = str(spec.get("password") or "").strip()
    if not env_pwd and key_name:
        auth_method, auto_password = "key_pair", False
    else:
        auth_method, auto_password = "password", not (env_pwd or scope_pwd)
    return {
        "scope_path": str(scope_path),
        "configured": creds_ok and not missing,
        "reason": reason or (f"scope.ecs_create.server 缺少必填字段：{', '.join(missing)}" if missing else None),
        "region": region,
        "region_from_env": bool(os.getenv(ENV_REGION)),
        "name": str(spec.get("name") or "") or None,
        "flavor": str(spec.get("flavorRef") or "") or None,
        "image": str(spec.get("imageRef") or "") or None,
        "disk_type": str(rv.get("volumetype") or "") or DEFAULT_ROOT_VOLUME_TYPE,
        "disk_size": rv.get("size") if isinstance(rv.get("size"), int) else DEFAULT_ROOT_VOLUME_SIZE,
        "bandwidth": eip.get("size") if isinstance(eip.get("size"), int) else DEFAULT_BANDWIDTH_SIZE,
        "availability_zone": str(spec.get("availability_zone") or "") or None,
        "has_eip": True,  # 表单不暴露 no-eip：与 ecs-skill 默认一致恒带 EIP
        "auth_method": auth_method,
        "auto_password": auto_password,
    }


def resolve_create_spec(scope, body):
    """scope.ecs_create.server 默认 + 表单覆盖 → 建机参数（纯函数）。

    表单可覆盖 name/flavor/image/disk_size/bandwidth；网络层（vpcid/
    subnet/安全组/AZ）与镜像默认全由 scope 提供，不经本 API 覆盖。
    鉴权四层（表单无密码字段）：env ECS_ADMIN_PASSWORD > scope key_name
    > scope password > 自动生成。必填缺失 → EcsNotConfigured；scope/env
    密码不合规 → ValueError（端点 422）。
    """
    body = body or {}
    spec = _server_spec(scope)
    missing = [f for f in ("imageRef", "flavorRef", "vpcid") if not str(spec.get(f) or "").strip()]
    if not _spec_subnet(spec):
        missing.append("nics[0].subnet_id")
    if missing:
        raise EcsNotConfigured(
            "scope.ecs_create.server 缺少必填字段：" + ", ".join(missing) + "（请在 scope.yaml 提供）。"
        )

    name = (str(body.get("name") or "").strip()
            or str(spec.get("name") or "").strip()
            or ("ecs-" + uuid.uuid4().hex[:8]))
    flavor_ref = str(body.get("flavor") or "").strip() or str(spec.get("flavorRef") or "").strip()
    image_ref = str(body.get("image") or "").strip() or str(spec.get("imageRef") or "").strip()

    rv = spec.get("root_volume") if isinstance(spec.get("root_volume"), dict) else {}
    volumetype = str(rv.get("volumetype") or "") or DEFAULT_ROOT_VOLUME_TYPE
    size = rv.get("size") if isinstance(rv.get("size"), int) else DEFAULT_ROOT_VOLUME_SIZE
    if isinstance(body.get("disk_size"), int) and not isinstance(body.get("disk_size"), bool):
        size = body["disk_size"]

    # EIP：scope publicip 模板 > 默认模板；表单 bandwidth 覆盖带宽
    pub = spec.get("publicip") if isinstance(spec.get("publicip"), dict) else {}
    eip_spec = pub.get("eip") if isinstance(pub.get("eip"), dict) else {}
    bw = eip_spec.get("bandwidth") if isinstance(eip_spec.get("bandwidth"), dict) else {}
    bandwidth = bw.get("size") if isinstance(bw.get("size"), int) else DEFAULT_BANDWIDTH_SIZE
    if isinstance(body.get("bandwidth"), int) and not isinstance(body.get("bandwidth"), bool):
        bandwidth = body["bandwidth"]
    eip = {
        "iptype": str(eip_spec.get("iptype") or "") or DEFAULT_EIP_IPTYPE,
        "sharetype": str(bw.get("sharetype") or "") or DEFAULT_EIP_SHARETYPE,
        "chargemode": str(bw.get("chargemode") or "") or DEFAULT_EIP_CHARGEMODE,
        "size": bandwidth,
    }

    # 鉴权四层，命中即停（密钥对与密码互斥）
    env_pwd = os.getenv(PASSWORD_ENV_VAR)
    scope_key = str(spec.get("key_name") or "").strip() or None
    scope_pwd = str(spec.get("password") or "").strip() or None
    if env_pwd:
        validate_password(env_pwd)
        key_name, admin_pass, auth_method = None, env_pwd, "password"
    elif scope_key:
        key_name, admin_pass, auth_method = scope_key, None, "key_pair"
    elif scope_pwd:
        validate_password(scope_pwd)
        key_name, admin_pass, auth_method = None, scope_pwd, "password"
    else:
        key_name, admin_pass, auth_method = None, generate_password(), "password"

    return {
        "name": name,
        "image_ref": image_ref,
        "flavor_ref": flavor_ref,
        "vpcid": str(spec.get("vpcid") or ""),
        "subnet_id": _spec_subnet(spec),
        "sg_ids": _spec_sg(spec),
        "availability_zone": str(spec.get("availability_zone") or "") or None,
        "volumetype": volumetype,
        "size": size,
        "eip": eip,
        "key_name": key_name,
        "admin_pass": admin_pass,
        "auth_method": auth_method,
    }


def _wait_for_port(host, grace, interval, probe_timeout):
    """grace 秒内反复探 22 直到通或超时（ACTIVE 后等 sshd 首启）。"""
    if not host:
        return False
    deadline = time.monotonic() + grace
    while True:
        if tcp_probe(host, 22, probe_timeout):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)


def _poll_until_ready(client, server_id, has_eip, timeout, interval):
    """轮询直至 ACTIVE + 可达 IP 出现（或超时/错误）。返回 (server, status, ip)。

    带 EIP 时浮动 IP 迟迟不来继续轮询直至超时，绝不回退私网。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        srv = _show_server(client, server_id)
        status = str(getattr(srv, "status", "UNKNOWN") or "UNKNOWN").upper() if srv else "PENDING"
        ip, _t = decide_ready_ip(srv, has_eip)
        if status == "ACTIVE" and ip:
            return srv, "ACTIVE", ip
        if status in ("ERROR", "FAILED"):
            return srv, status, None
        time.sleep(interval)
    return None, "TIMEOUT", None


def create_instance(scope_path, body=None, timeout=POLL_TIMEOUT, poll_interval=POLL_INTERVAL,
                    port_grace=PORT_GRACE):
    """同步建机到就绪（ACTIVE + 可达 IP + 22 通），返回 ecs-skill create 契约。

    未就绪（超时/ERROR/22 不通）返回 ok=False + error/hint——机器可能已
    创建（已开始计费），id 必随响应带回供 ecs-skill show --id 复查；
    自动生成的密码在未就绪路径不回传（与 skill 语义一致，密码丢失由
    change-os/重置路径兜底）。就绪时 admin_pass 登记进脱敏已知清单
    （HTTP 响应照常携带——这正是用户要的登录凭据；登记只堵 SSE 泄漏）。
    """
    _ak, _sk, region, _pid = _resolve_conf(scope_path)
    scope = _read_scope(scope_path)
    spec = resolve_create_spec(scope, body)
    client = _get_client(scope_path)
    from huaweicloudsdkecs.v2 import (
        CreateServersRequest,
        CreateServersRequestBody,
        PrePaidServer,
        PrePaidServerEip,
        PrePaidServerEipBandwidth,
        PrePaidServerNic,
        PrePaidServerPublicip,
        PrePaidServerRootVolume,
        PrePaidServerSecurityGroup,
    )

    eip = spec["eip"]
    publicip = None
    if eip:
        publicip = PrePaidServerPublicip(eip=PrePaidServerEip(
            iptype=eip["iptype"],
            bandwidth=PrePaidServerEipBandwidth(
                size=eip["size"], sharetype=eip["sharetype"], chargemode=eip["chargemode"],
            ),
        ))
    server = PrePaidServer(
        name=spec["name"],
        image_ref=spec["image_ref"],
        flavor_ref=spec["flavor_ref"],
        vpcid=spec["vpcid"],
        key_name=spec["key_name"],
        admin_pass=spec["admin_pass"],
        availability_zone=spec["availability_zone"],
        nics=[PrePaidServerNic(subnet_id=spec["subnet_id"])],
        root_volume=PrePaidServerRootVolume(volumetype=spec["volumetype"], size=spec["size"]),
        security_groups=[PrePaidServerSecurityGroup(id=sid) for sid in spec["sg_ids"]],
        publicip=publicip,
    )
    req = CreateServersRequest(body=CreateServersRequestBody(server=server))
    has_eip = publicip is not None

    resp = _api(lambda: client.create_servers(req))
    server_ids = list(getattr(resp, "server_ids", None) or [])
    job_id = str(getattr(resp, "job_id", "") or "")
    base = {
        "action": "create", "name": spec["name"], "region": region,
        "flavor": spec["flavor_ref"], "image": spec["image_ref"], "job_id": job_id,
    }
    if not server_ids:
        return {**base, "ok": False, "id": "", "ip": None, "ip_type": None,
                "status": "UNKNOWN", "ssh_port_open": False,
                "error": "CreateServers 未返回 serverIds"}

    server_id = str(server_ids[0])
    srv, status, ip = _poll_until_ready(client, server_id, has_eip, timeout, poll_interval)
    if status == "ACTIVE" and ip:
        port_open = _wait_for_port(ip, grace=port_grace, interval=min(poll_interval, 10),
                                   probe_timeout=PROBE_TIMEOUT)
    else:
        port_open = False
    ready = status == "ACTIVE" and bool(ip) and port_open

    result = {
        **base, "ok": ready, "id": server_id, "ip": ip,
        "ip_type": "floating" if has_eip else "private",
        "status": status, "ssh_port_open": port_open,
    }
    if ready:
        result["auth_method"] = spec["auth_method"]
        if spec["admin_pass"]:
            result["admin_pass"] = spec["admin_pass"]
            from .redact import register_secrets  # 惰性导入（redact 无反向依赖）
            register_secrets([spec["admin_pass"]])
    else:
        server_status = str(getattr(srv, "status", "") or "").upper() if srv else ""
        if status == "TIMEOUT":
            if server_status == "ACTIVE" and has_eip:
                result["error"] = f"轮询超时（{timeout}s）：已 ACTIVE 但公网浮动 IP 始终未出现"
                result["hint"] = "请检查 EIP 配额/权限（如 eip:publicIps:create）；未自动销毁。"
            else:
                result["error"] = f"轮询超时（{timeout}s）仍未 ACTIVE"
                result["hint"] = "机器可能仍在创建，稍后用列表刷新或 ecs-skill show --id 复查；未自动销毁。"
        elif status in ("ERROR", "FAILED"):
            result["error"] = f"ECS 进入 {status} 状态"
            result["hint"] = "未自动销毁，可在华为云控制台或经 ecs-skill 检查原因。"
        elif not port_open:
            result["error"] = "已 ACTIVE 但 22 端口不通"
            if has_eip:
                result["hint"] = "请确认安全组对公网放行 22；未自动销毁。"
            else:
                result["hint"] = "请确认该 VPC 安全组放行 22、且访问方与新机同子网；未自动销毁。"
    return result
