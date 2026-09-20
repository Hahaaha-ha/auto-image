#!/usr/bin/env python
"""ecs-skill 测试 —— 两条缝。

- 缝 A：请求构造（scope dict + CLI → CreateServersRequest），纯逻辑。
- 缝 B：命令行入口的 dry-run 端到端（argv → stdout JSON），不触网。

纯 assert，无 pytest（契合 ecs-skill 最小依赖调性）。
运行：python .claude/skills/ecs-skill/tests/test_ecs_ops.py
"""
import contextlib
import io
from datetime import datetime, timedelta, timezone
import json
import os
import pathlib
import socket
import sys
import tempfile
from argparse import Namespace
from types import SimpleNamespace

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import ecs  # noqa: E402
from ecs_client import DEFAULT_SCOPE_PATH, resolve_credentials  # noqa: E402
from ecs_ops import (  # noqa: E402
    DEFAULT_BANDWIDTH_SIZE,
    DEFAULT_EIP_CHARGEMODE,
    DEFAULT_EIP_IPTYPE,
    DEFAULT_EIP_SHARETYPE,
    PASSWORD_SPECIAL_CHARS,
    build_change_os_request,
    build_create_request,
    build_delete_request,
    decide_ready_ip,
    fixed_ip,
    generate_password,
    mask_password,
    validate_password,
)


# ---- 固件 ----
def make_args(**kw):
    """create 子命令的 CLI namespace 固件，默认全空（覆盖 scope）。"""
    base = dict(
        name=None, flavor=None, image=None, key=None, password=None,
        vpc=None, subnet=None, sg=None, az=None,
        no_eip=False, validate=False, dry_run=False,
        disk_type=None, disk_size=None, bandwidth=None,
        terminate_hours=None, no_auto_terminate=False, _now=None,
    )
    base.update(kw)
    return Namespace(**base)


def make_scope(**server_overrides):
    """最小 scope：满足必填 imageRef/flavorRef/vpcid/nics.subnet_id。"""
    server = dict(
        imageRef="img-x", flavorRef="s6.xlarge.2", vpcid="vpc-1",
        nics=[{"subnet_id": "subnet-1"}],
    )
    server.update(server_overrides)
    return {"ecs_create": {"server": server}}


# ---- 缝 A：请求构造 ----
def test_root_volume_default_when_absent():
    """scope 完全没有 root_volume → 注入默认 {SSD, 40}。"""
    scope = make_scope()  # 无 root_volume
    req = build_create_request(scope, make_args())
    rv = req.body.server.root_volume
    assert rv is not None, "root_volume 应被默认注入"
    assert rv.volumetype == "SSD", f"默认盘类型应为 SSD，实得 {rv.volumetype!r}"
    assert rv.size == 40, f"默认盘大小应为 40，实得 {rv.size!r}"


def test_root_volume_respected_when_present():
    """scope 给了 root_volume（即使缺 size）→ 原样尊重，不注入默认 size。"""
    scope = make_scope(root_volume={"volumetype": "SAS"})  # 有但无 size
    req = build_create_request(scope, make_args())
    rv = req.body.server.root_volume
    assert rv.volumetype == "SAS", f"应尊重 scope 的 SAS，实得 {rv.volumetype!r}"
    assert rv.size is None, f"scope 给了 root_volume 就不注入默认 size，实得 {rv.size!r}"


def test_disk_flags_merge_into_root_volume():
    """--disk-type/--disk-size 合并进 root_volume，覆盖 scope 的盘类型/大小。"""
    scope = make_scope(root_volume={"volumetype": "SSD"})  # 真实 scope 的常见形态
    req = build_create_request(scope, make_args(disk_type="GPSSD", disk_size=100))
    rv = req.body.server.root_volume
    assert rv.volumetype == "GPSSD", f"--disk-type 应覆盖为 GPSSD，实得 {rv.volumetype!r}"
    assert rv.size == 100, f"--disk-size 应为 100，实得 {rv.size!r}"


def test_cli_overrides_scope():
    """--name/--flavor/--image/--key 覆盖 scope 对应值。"""
    scope = make_scope(key_name="scope-key")
    req = build_create_request(scope, make_args(
        name="web-01", flavor="c6.large.2", image="img-y", key="cli-key"))
    s = req.body.server
    assert s.name == "web-01", f"name 实得 {s.name!r}"
    assert s.flavor_ref == "c6.large.2", f"flavor 实得 {s.flavor_ref!r}"
    assert s.image_ref == "img-y", f"image 实得 {s.image_ref!r}"
    assert s.key_name == "cli-key", f"key 实得 {s.key_name!r}"


def test_network_flags_override_scope():
    """--vpc/--subnet/--sg/--az 各自覆盖 scope 对应值。"""
    scope = make_scope(
        vpcid="scope-vpc", nics=[{"subnet_id": "scope-subnet"}],
        security_groups=[{"id": "scope-sg"}], availability_zone="cn-north-4a",
    )
    req = build_create_request(scope, make_args(
        vpc="cli-vpc", subnet="cli-subnet", sg="cli-sg", az="cn-north-4b"))
    s = req.body.server
    assert s.vpcid == "cli-vpc", f"--vpc 实得 {s.vpcid!r}"
    assert [n.subnet_id for n in s.nics] == ["cli-subnet"], \
        f"--subnet 实得 {[n.subnet_id for n in s.nics]!r}"
    assert [g.id for g in s.security_groups] == ["cli-sg"], \
        f"--sg 实得 {[g.id for g in s.security_groups]!r}"
    assert s.availability_zone == "cn-north-4b", f"--az 实得 {s.availability_zone!r}"


def test_network_falls_back_to_scope_when_flags_absent():
    """四个网络参数都不给 → 沿用 scope 的值（行为与改动前一致）。"""
    scope = make_scope(
        vpcid="scope-vpc", nics=[{"subnet_id": "scope-subnet"}],
        security_groups=[{"id": "scope-sg"}], availability_zone="cn-north-4a",
    )
    s = build_create_request(scope, make_args()).body.server
    assert s.vpcid == "scope-vpc", f"vpcid 实得 {s.vpcid!r}"
    assert [n.subnet_id for n in s.nics] == ["scope-subnet"], \
        f"nics 实得 {[n.subnet_id for n in s.nics]!r}"
    assert [g.id for g in s.security_groups] == ["scope-sg"], \
        f"security_groups 实得 {[g.id for g in s.security_groups]!r}"
    assert s.availability_zone == "cn-north-4a", f"availability_zone 实得 {s.availability_zone!r}"


def test_az_absent_means_not_sent():
    """两处都不给可用区 → 不下发，由华为自选有货的 AZ（规格库存按 AZ 变化）。"""
    scope = make_scope()  # 无 availability_zone
    s = build_create_request(scope, make_args()).body.server
    assert s.availability_zone is None, f"不应下发可用区，实得 {s.availability_zone!r}"


def test_az_empty_in_scope_means_not_sent():
    """scope 的可用区留空（范本即为留空）→ 视同不给，不下发空串。"""
    scope = make_scope(availability_zone="")
    s = build_create_request(scope, make_args()).body.server
    assert s.availability_zone is None, f"空串不应下发，实得 {s.availability_zone!r}"


def test_cli_network_satisfies_required_fields():
    """必填校验认得 CLI 传入的 VPC/子网：scope 里没有也不该报缺字段。"""
    scope = {"ecs_create": {"server": {"imageRef": "i", "flavorRef": "f"}}}
    s = build_create_request(scope, make_args(vpc="cli-vpc", subnet="cli-subnet")).body.server
    assert s.vpcid == "cli-vpc", f"vpcid 实得 {s.vpcid!r}"
    assert [n.subnet_id for n in s.nics] == ["cli-subnet"], \
        f"nics 实得 {[n.subnet_id for n in s.nics]!r}"


def test_missing_network_names_which_one():
    """仅当 CLI 与 scope 两处都缺时才报缺字段，且点名缺的是哪一个。"""
    base = {"imageRef": "i", "flavorRef": "f"}

    def expect_missing(args, needle, absent):
        try:
            build_create_request({"ecs_create": {"server": dict(base)}}, args)
        except ValueError as e:
            msg = str(e).lower()
            assert needle in msg, f"报错应提及 {needle}，实得：{e}"
            assert absent not in msg, f"报错不应提及已给出的 {absent}，实得：{e}"
            return
        raise AssertionError(f"缺 {needle} 应抛 ValueError")

    expect_missing(make_args(subnet="cli-subnet"), "vpcid", "subnet_id")
    expect_missing(make_args(vpc="cli-vpc"), "subnet_id", "vpcid")


def test_multiple_security_groups_preserved_from_scope():
    """scope 有多个安全组且不给 --sg 时，全部保留（行为与改动前一致）。"""
    scope = make_scope(security_groups=[{"id": "sg-1"}, {"id": "sg-2"}])
    req = build_create_request(scope, make_args())
    ids = [g.id for g in req.body.server.security_groups]
    assert ids == ["sg-1", "sg-2"], f"应保留全部安全组，实得 {ids!r}"


def test_sg_flag_replaces_all_scope_groups():
    """--sg 给出时替换为单个安全组（不与 scope 的叠加）。"""
    scope = make_scope(security_groups=[{"id": "sg-1"}, {"id": "sg-2"}])
    req = build_create_request(scope, make_args(sg="cli-sg"))
    ids = [g.id for g in req.body.server.security_groups]
    assert ids == ["cli-sg"], f"--sg 应替换全部，实得 {ids!r}"


def test_count_never_sent():
    """只交付一台：scope 里写了 count 也不透传，请求里 count 恒为 None。"""
    scope = make_scope(count=5)
    req = build_create_request(scope, make_args())
    assert req.body.server.count is None, f"count 不应透传，实得 {req.body.server.count!r}"


def test_name_auto_generated_when_absent():
    """scope 与 CLI 都没给 name → 自动 ecs-<8hex>（12 字符）。"""
    scope = make_scope()  # 无 name
    req = build_create_request(scope, make_args())
    name = req.body.server.name
    assert name and name.startswith("ecs-"), f"应自动 ecs- 前缀，实得 {name!r}"
    assert len(name) == 12, f"应为 ecs-+8hex=12 字符，实得 {len(name)}"


_EIP_TEMPLATE = {
    "publicip": {
        "eip": {"iptype": DEFAULT_EIP_IPTYPE, "bandwidth": {"sharetype": DEFAULT_EIP_SHARETYPE, "size": DEFAULT_BANDWIDTH_SIZE, "chargemode": DEFAULT_EIP_CHARGEMODE}},
    }
}


def test_default_includes_eip():
    """默认带 EIP（公网可达）——不加任何参数时请求体带 publicip。"""
    scope = make_scope(**_EIP_TEMPLATE)
    req = build_create_request(scope, make_args())  # no_eip=False
    assert req.body.server.publicip is not None, "默认应带 publicip"


def test_no_eip_skips_publicip():
    """--no-eip 时不注入 publicip（退回同 VPC 私网路径）。"""
    scope = make_scope(**_EIP_TEMPLATE)
    req = build_create_request(scope, make_args(no_eip=True))
    assert req.body.server.publicip is None, "--no-eip 时不应有 publicip"


def test_eip_uses_scope_template_default_bandwidth():
    """默认（带 EIP）用 scope 的 publicip 模板，带宽取模板默认 5。"""
    scope = make_scope(**_EIP_TEMPLATE)
    req = build_create_request(scope, make_args())
    pub = req.body.server.publicip
    assert pub is not None, "默认应有 publicip"
    assert pub.eip.iptype == DEFAULT_EIP_IPTYPE, f"iptype 实得 {pub.eip.iptype!r}"
    assert pub.eip.bandwidth.size == DEFAULT_BANDWIDTH_SIZE, f"带宽默认 {DEFAULT_BANDWIDTH_SIZE}，实得 {pub.eip.bandwidth.size!r}"


def test_bandwidth_flag_overrides_eip_size():
    """--bandwidth 覆盖 EIP 带宽 size。"""
    scope = make_scope(**_EIP_TEMPLATE)
    req = build_create_request(scope, make_args(bandwidth=10))
    assert req.body.server.publicip.eip.bandwidth.size == 10


def test_eip_default_when_scope_has_no_template():
    """默认带 EIP 但 scope 无 publicip 模板 → 用内置默认（5_bgp / 5M）。"""
    scope = make_scope()  # 无 publicip
    req = build_create_request(scope, make_args())
    pub = req.body.server.publicip
    assert pub.eip.iptype == DEFAULT_EIP_IPTYPE
    assert pub.eip.bandwidth.size == DEFAULT_BANDWIDTH_SIZE


def test_missing_required_fields_raise():
    """缺必填 imageRef/flavorRef/vpcid/nics.subnet_id → ValueError（不静默发给 API）。"""
    full = {"imageRef": "i", "flavorRef": "f", "vpcid": "v", "nics": [{"subnet_id": "s"}]}

    def expect_missing(removed_server, needle):
        scope = {"ecs_create": {"server": removed_server}}
        try:
            build_create_request(scope, make_args())
        except ValueError as e:
            assert needle in str(e).lower(), f"报错应提及 {needle}，实得：{e}"
            return
        raise AssertionError(f"缺 {needle} 应抛 ValueError")

    s = dict(full); del s["imageRef"]; expect_missing(s, "imageref")
    s = dict(full); del s["flavorRef"]; expect_missing(s, "flavorref")
    s = dict(full); del s["vpcid"]; expect_missing(s, "vpcid")
    s = dict(full); s["nics"] = [{"subnet_id": ""}]; expect_missing(s, "subnet_id")
    s = dict(full); del s["nics"]; expect_missing(s, "subnet_id")


# ---- 缝 B 固件：dry-run 端到端（argv → stdout JSON，全程不触网）----
# 占位凭证：--dry-run 在构造客户端之前就返回，凭证只需通过必填校验，不会被使用。
PLACEHOLDER_CREDENTIALS = {
    "ak": "AK-PLACEHOLDER", "sk": "SK-PLACEHOLDER",
    "region": "cn-north-4", "project_id": "PROJECT-PLACEHOLDER",
}


def make_cli_scope():
    """缝 B 的 scope：缝 A 最小 scope + 占位凭证 + 完整网络字段。

    盘刻意取 SAS/60 —— 与 ecs_ops 的内置默认 SSD/40 不同，否则「scope 走到请求体」
    的断言在透传彻底断掉时也会被默认值蒙混过去。
    """
    scope = make_scope(
        availability_zone="cn-north-4a",
        security_groups=[{"id": "sg-1"}],
        root_volume={"volumetype": "SAS", "size": 60},
    )
    scope.update(PLACEHOLDER_CREDENTIALS)
    return scope


@contextlib.contextmanager
def _isolate_env(*prefixes):
    """临时屏蔽以 prefixes 开头的环境变量，结束后恢复。"""
    saved = {}
    for k in list(os.environ):
        for pref in prefixes:
            if k.startswith(pref):
                saved[k] = os.environ.pop(k)
                break
    try:
        yield
    finally:
        os.environ.update(saved)


@contextlib.contextmanager
def _no_network():
    """把建连出口换成抛错：dry-run 若哪天开始触网，测试立刻炸而不是静默发真请求。

    是警报而非沙箱：只挡 socket 层的两个出口，不保证拦下一切。
    """
    real_socket, real_connect = socket.socket, socket.create_connection

    def _forbidden(*_a, **_kw):
        raise AssertionError("dry-run 不应发起任何网络请求")

    socket.socket, socket.create_connection = _forbidden, _forbidden
    try:
        yield
    finally:
        socket.socket, socket.create_connection = real_socket, real_connect


def run_cli(scope, argv):
    """用临时 scope 文件驱动 ecs.main，捕获 stdout 解析为 JSON。返回 (退出码, JSON)。

    argparse 拒绝未知参数时以 SystemExit(2) 退出、stdout 为空 —— 此时返回 (code, None)。
    """
    real_logs_dir = ecs.LOGS_DIR
    with tempfile.TemporaryDirectory() as tmp:
        scope_path = os.path.join(tmp, "scope.yaml")
        with open(scope_path, "w", encoding="utf-8") as fh:
            yaml.safe_dump(scope, fh, allow_unicode=True)
        # dry-run 本不写日志；改道只为万一走到写盘分支时别污染 skill 的 logs/。
        ecs.LOGS_DIR = pathlib.Path(tmp) / "logs"
        out, err = io.StringIO(), io.StringIO()
        try:
            with _isolate_env("HUAWEICLOUD_SDK_"), _no_network(), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = ecs.main(["--scope", scope_path, *argv])
        except SystemExit as e:  # argparse 拒绝未知参数（--eip 等）
            code = e.code
        finally:
            ecs.LOGS_DIR = real_logs_dir
    raw = out.getvalue()
    if not raw.strip():
        return code, None
    try:
        return code, json.loads(raw)
    except json.JSONDecodeError as e:
        # main() 把异常吞成 stderr 上的 JSON —— 带上它，否则只看到一个空 stdout。
        raise AssertionError(
            f"stdout 应为可解析的纯 JSON（{e}），实得：{raw!r}；stderr：{err.getvalue()!r}"
        )


def dry_run_server(*flags):
    """跑一次 create --dry-run，返回请求体的 server 段。"""
    _, payload = run_cli(make_cli_scope(), ["create", "--dry-run", *flags])
    server = ((payload.get("request") or {}).get("body") or {}).get("server")
    assert isinstance(server, dict), f"输出应含 request.body.server，实得：{payload!r}"
    return server


# ---- CLI 互斥：--terminate-hours vs --no-auto-terminate ----
def test_dry_run_terminate_flags_mutually_exclusive():
    """两 flag 同时给 → argparse 报错（exit 2），不静默择一。"""
    code, payload = run_cli(make_cli_scope(), ["create", "--dry-run",
                                               "--terminate-hours", "4", "--no-auto-terminate"])
    assert code == 2, f"应 exit 2，实得 {code!r}"
    assert payload is None, f"stdout 应为空，实得 {payload!r}"


# ---- 缝 B：命令行入口的 dry-run 端到端 ----
def test_dry_run_contract():
    """create --dry-run：stdout 是纯 JSON、标记 dry_run、退出码 0。"""
    code, payload = run_cli(make_cli_scope(), ["create", "--dry-run"])
    assert code == 0, f"退出码应为 0，实得 {code!r}"
    assert payload.get("ok") is True, f"ok 应为 True，实得 {payload.get('ok')!r}"
    assert payload.get("action") == "create", f"action 实得 {payload.get('action')!r}"
    assert payload.get("dry_run") is True, f"dry_run 应为 True，实得 {payload.get('dry_run')!r}"
    assert payload.get("region") == "cn-north-4", f"region 实得 {payload.get('region')!r}"


def test_dry_run_request_carries_scope_spec():
    """规格（镜像/规格型/系统盘）从 scope 走到请求体。"""
    server = dry_run_server()
    assert server.get("image_ref") == "img-x", f"image_ref 实得 {server.get('image_ref')!r}"
    assert server.get("flavor_ref") == "s6.xlarge.2", f"flavor_ref 实得 {server.get('flavor_ref')!r}"
    assert server.get("root_volume") == {"volumetype": "SAS", "size": 60}, \
        f"root_volume 实得 {server.get('root_volume')!r}"


def test_dry_run_request_carries_scope_network():
    """网络参数（VPC/子网/安全组/可用区）从 scope 走到请求体。"""
    server = dry_run_server()
    assert server.get("vpcid") == "vpc-1", f"vpcid 实得 {server.get('vpcid')!r}"
    assert server.get("nics") == [{"subnet_id": "subnet-1"}], f"nics 实得 {server.get('nics')!r}"
    assert server.get("security_groups") == [{"id": "sg-1"}], \
        f"security_groups 实得 {server.get('security_groups')!r}"
    assert server.get("availability_zone") == "cn-north-4a", \
        f"availability_zone 实得 {server.get('availability_zone')!r}"


def test_dry_run_cli_flags_reach_request():
    """既有参数确实从命令行接到请求体上（防止只改构造函数、忘了声明参数）。"""
    server = dry_run_server("--name", "web-01", "--flavor", "c6.large.2", "--image", "img-y",
                            "--key", "cli-key", "--disk-type", "GPSSD", "--disk-size", "100")
    assert server.get("name") == "web-01", f"--name 实得 {server.get('name')!r}"
    assert server.get("flavor_ref") == "c6.large.2", f"--flavor 实得 {server.get('flavor_ref')!r}"
    assert server.get("image_ref") == "img-y", f"--image 实得 {server.get('image_ref')!r}"
    assert server.get("key_name") == "cli-key", f"--key 实得 {server.get('key_name')!r}"
    assert server.get("root_volume") == {"volumetype": "GPSSD", "size": 100}, \
        f"--disk-type/--disk-size 实得 {server.get('root_volume')!r}"


def test_dry_run_eip_flags_reach_request():
    """EIP 契约：默认带 publicip；--no-eip 不带；--bandwidth 覆盖带宽。"""
    # 默认带 EIP
    assert dry_run_server().get("publicip") is not None, \
        f"默认应带 EIP，实得 {dry_run_server().get('publicip')!r}"
    # --no-eip 不带
    assert dry_run_server("--no-eip").get("publicip") is None, \
        f"--no-eip 应不带 EIP，实得 {dry_run_server('--no-eip').get('publicip')!r}"
    # --bandwidth 覆盖带宽
    pub = dry_run_server("--bandwidth", "10").get("publicip") or {}
    assert (pub.get("eip") or {}).get("iptype") == DEFAULT_EIP_IPTYPE, f"publicip 实得 {pub!r}"
    assert ((pub.get("eip") or {}).get("bandwidth") or {}).get("size") == 10, \
        f"--bandwidth 实得 {pub!r}"


# ---- project_id 降为可选 ----
def test_credentials_without_project_id():
    """缺 project_id 不再报缺凭证（交给 SDK 自动推导）。"""
    with _isolate_env("HUAWEICLOUD_SDK_"):
        scope = {"ak": "a", "sk": "s", "region": "cn-north-4"}
        creds = resolve_credentials(scope)
        assert creds.ak == "a" and creds.sk == "s" and creds.region == "cn-north-4"
        assert not creds.project_id, f"project_id 应为空，实得 {creds.project_id!r}"


def test_credentials_with_project_id_unchanged():
    """显式提供 project_id 时行为不变。"""
    with _isolate_env("HUAWEICLOUD_SDK_"):
        scope = {"ak": "a", "sk": "s", "region": "cn-north-4", "project_id": "pid-1"}
        creds = resolve_credentials(scope)
        assert creds.project_id == "pid-1", f"project_id 实得 {creds.project_id!r}"


def test_credentials_env_overrides_scope_for_project_id():
    """env 的 project_id 优先于 scope（既有优先级不变）。"""
    os.environ["HUAWEICLOUD_SDK_PROJECT_ID"] = "env-pid"
    try:
        scope = {"ak": "a", "sk": "s", "region": "cn-north-4", "project_id": "scope-pid"}
        creds = resolve_credentials(scope)
        assert creds.project_id == "env-pid", f"env 应优先，实得 {creds.project_id!r}"
    finally:
        del os.environ["HUAWEICLOUD_SDK_PROJECT_ID"]


def test_credentials_still_require_ak_sk_region():
    """AK/SK/region 仍为必填，缺失时早炸并点名缺哪个。"""
    with _isolate_env("HUAWEICLOUD_SDK_"):
        try:
            resolve_credentials({"sk": "s", "region": "cn-north-4"})
            raise AssertionError("缺 ak 应抛 ValueError")
        except ValueError as e:
            assert "ak" in str(e).lower(), f"报错应提及 ak，实得：{e}"

        try:
            resolve_credentials({"ak": "a", "region": "cn-north-4"})
            raise AssertionError("缺 sk 应抛 ValueError")
        except ValueError as e:
            assert "sk" in str(e).lower(), f"报错应提及 sk，实得：{e}"

        try:
            resolve_credentials({"ak": "a", "sk": "s"})
            raise AssertionError("缺 region 应抛 ValueError")
        except ValueError as e:
            assert "region" in str(e).lower(), f"报错应提及 region，实得：{e}"


# ---- 缝 B 补充：网络参数从 CLI 落到请求体 ----
def test_dry_run_network_cli_flags_reach_request():
    """--vpc/--subnet/--sg/--az 从命令行接到请求体上。"""
    server = dry_run_server("--vpc", "cli-vpc", "--subnet", "cli-subnet",
                            "--sg", "cli-sg", "--az", "cn-north-4b")
    assert server.get("vpcid") == "cli-vpc", f"--vpc 实得 {server.get('vpcid')!r}"
    assert server.get("nics") == [{"subnet_id": "cli-subnet"}], \
        f"--subnet 实得 {server.get('nics')!r}"
    assert server.get("security_groups") == [{"id": "cli-sg"}], \
        f"--sg 实得 {server.get('security_groups')!r}"
    assert server.get("availability_zone") == "cn-north-4b", \
        f"--az 实得 {server.get('availability_zone')!r}"


# ---- 缝 B 补充：project_id 缺省时 dry-run 正常 ----
def test_dry_run_without_project_id():
    """scope 不含 project_id 时 dry-run 正常输出、退出码 0。"""
    scope = make_cli_scope()
    del scope["project_id"]
    code, payload = run_cli(scope, ["create", "--dry-run"])
    assert code == 0, f"退出码应为 0，实得 {code!r}"
    assert payload.get("ok") is True, f"ok 应为 True，实得 {payload.get('ok')!r}"


# ---- 就绪 IP 决策纯函数 ----
def _addr_entry(addr, kind):
    """构造一条地址条目（os_ext_ip_stype 是 SDK 对 OS-EXT-IPS:type 的重命名）。"""
    return SimpleNamespace(addr=addr, os_ext_ip_stype=kind)


def _fake_server(*entries):
    """用 SimpleNamespace 伪造 server，addresses 按 SDK 结构嵌套。"""
    return SimpleNamespace(addresses={"net": list(entries)})


def test_decide_ready_ip_eip_with_floating():
    """带 EIP 且浮动 IP 已出现 → 取浮动 IP，类型 floating。"""
    srv = _fake_server(_addr_entry("10.0.0.5", "fixed"), _addr_entry("94.74.107.97", "floating"))
    ip, kind = decide_ready_ip(srv, has_eip=True)
    assert ip == "94.74.107.97", f"应取浮动 IP，实得 {ip!r}"
    assert kind == "floating", f"类型应为 floating，实得 {kind!r}"


def test_decide_ready_ip_eip_only_private_not_ready():
    """带 EIP 但只有私网 IP → 不回退私网，返回 (None, floating) 表示尚未就绪。"""
    srv = _fake_server(_addr_entry("10.0.0.5", "fixed"))
    ip, kind = decide_ready_ip(srv, has_eip=True)
    assert ip is None, f"不应回退私网 IP，实得 {ip!r}"
    assert kind == "floating", f"类型应为 floating，实得 {kind!r}"


def test_decide_ready_ip_no_eip_takes_private():
    """--no-eip 时取同 VPC 私网固定 IP。"""
    srv = _fake_server(_addr_entry("10.0.0.5", "fixed"), _addr_entry("94.74.107.97", "floating"))
    ip, kind = decide_ready_ip(srv, has_eip=False)
    assert ip == "10.0.0.5", f"应取私网 IP，实得 {ip!r}"
    assert kind == "private", f"类型应为 private，实得 {kind!r}"


def test_decide_ready_ip_none_server():
    """server 为 None（尚未建出）→ ip 为 None。"""
    ip_floating, kind_floating = decide_ready_ip(None, has_eip=True)
    assert ip_floating is None and kind_floating == "floating"
    ip_private, kind_private = decide_ready_ip(None, has_eip=False)
    assert ip_private is None and kind_private == "private"


def test_fixed_ip_never_returns_floating():
    """fixed_ip 无 fixed 条目时不退回浮动 IP（spec：--no-eip 取私网固定 IP）。"""
    srv = _fake_server(_addr_entry("94.74.107.97", "floating"))
    assert fixed_ip(srv) is None, \
        f"无 fixed 条目时不应退回浮动 IP，实得 {fixed_ip(srv)!r}"


# ---- --eip 误用显式报错 ----
def test_eip_flag_rejected():
    """--eip 已移除：误用时 argparse 以未知参数报错（退出码 2）。"""
    code, _ = run_cli(make_cli_scope(), ["create", "--dry-run", "--eip"])
    assert code == 2, f"--eip 应被拒绝（退出码 2），实得 {code!r}"


# ---- 登录鉴权裁决表 ----
# 顺位（自上而下，命中即停）：
#   1 CLI --key     → 密钥对
#   2 CLI --password → 密码
#   3 环境变量      → 密码
#   4 scope key_name → 密钥对
#   5 scope password → 密码
#   6 以上皆无      → 自动生成密码
def test_auth_cli_key_wins():
    """顺位 1：CLI --key 压过 CLI --password 与 scope 的一切。"""
    scope = make_scope(key_name="scope-key", password="Pwd@@123456")
    req = build_create_request(scope, make_args(key="cli-key", password="Pwd@@123456"))
    s = req.body.server
    assert s.key_name == "cli-key", f"key_name 实得 {s.key_name!r}"
    assert s.admin_pass is None, f"裁决为密钥对时不应带 admin_pass，实得 {s.admin_pass!r}"


def test_auth_cli_password_beats_scope_key():
    """顺位 2 压 4：CLI --password 压过 scope 的 key_name（跨层级 CLI 优先）。"""
    scope = make_scope(key_name="scope-key")
    req = build_create_request(scope, make_args(password="Pwd@@123456"))
    s = req.body.server
    assert s.admin_pass == "Pwd@@123456", f"admin_pass 实得 {s.admin_pass!r}"
    assert s.key_name is None, f"裁决为密码时不应带 key_name，实得 {s.key_name!r}"


def test_auth_env_password_beats_scope_key():
    """顺位 3 压 4：环境变量 ECS_ADMIN_PASSWORD 压过 scope 的 key_name。"""
    os.environ["ECS_ADMIN_PASSWORD"] = "EnvPwd@@123456"
    try:
        scope = make_scope(key_name="scope-key")
        req = build_create_request(scope, make_args())
        s = req.body.server
        assert s.admin_pass == "EnvPwd@@123456", f"admin_pass 实得 {s.admin_pass!r}"
        assert s.key_name is None, f"裁决为密码时不应带 key_name，实得 {s.key_name!r}"
    finally:
        del os.environ["ECS_ADMIN_PASSWORD"]


def test_auth_cli_password_beats_env_password():
    """顺位 2 压 3：CLI --password 压过环境变量。"""
    os.environ["ECS_ADMIN_PASSWORD"] = "EnvPwd@@123456"
    try:
        req = build_create_request(make_scope(), make_args(password="CliPwd@@123456"))
        assert req.body.server.admin_pass == "CliPwd@@123456", \
            f"CLI 密码应压过环境变量，实得 {req.body.server.admin_pass!r}"
    finally:
        del os.environ["ECS_ADMIN_PASSWORD"]


def test_auth_scope_key_beats_scope_password():
    """顺位 4 压 5：同一层级内密钥对优先于密码。"""
    scope = make_scope(key_name="scope-key", password="ScopePwd@@123456")
    req = build_create_request(scope, make_args())
    s = req.body.server
    assert s.key_name == "scope-key", f"同层密钥对应优先，实得 key_name={s.key_name!r}"
    assert s.admin_pass is None, f"同层密钥对优先时不应带 admin_pass，实得 {s.admin_pass!r}"


def test_auth_scope_password_when_no_key():
    """顺位 5：scope 有 password 但无 key_name，且 CLI/env 都没给 → 用 scope 密码。"""
    scope = make_scope(password="ScopePwd@@123456")
    req = build_create_request(scope, make_args())
    s = req.body.server
    assert s.admin_pass == "ScopePwd@@123456", f"admin_pass 实得 {s.admin_pass!r}"
    assert s.key_name is None, f"裁决为密码时不应带 key_name，实得 {s.key_name!r}"


def test_auth_auto_generate_when_nothing_given():
    """顺位 6：三条通道皆无密码、且无密钥对 → 自动生成密码。"""
    scope = make_scope()  # 无 key_name、无 password
    req = build_create_request(scope, make_args())
    s = req.body.server
    assert s.key_name is None, f"无密钥对时 key_name 应为 None，实得 {s.key_name!r}"
    assert s.admin_pass is not None and len(s.admin_pass) == 16, \
        f"应自动生成 16 位密码，实得 {s.admin_pass!r}"


def test_auth_no_generate_when_key_exists():
    """有密钥对时不触发生成（即便其他通道也没给密码）。"""
    scope = make_scope(key_name="scope-key")
    req = build_create_request(scope, make_args())
    assert req.body.server.key_name == "scope-key"
    assert req.body.server.admin_pass is None


# ---- 密码复杂度本地校验 ----
def test_password_too_short_rejected():
    """长度 < 8 → 拒绝，报错指明长度规则。"""
    try:
        validate_password("Ab1@456")  # 7 chars
        raise AssertionError("过短密码应被拒")
    except ValueError as e:
        assert "长度" in str(e) or "len" in str(e).lower(), f"报错应指明长度规则，实得：{e}"


def test_password_too_long_rejected():
    """长度 > 26 → 拒绝。"""
    try:
        validate_password("Ab1@" + "x" * 23)  # 27 chars
        raise AssertionError("过长密码应被拒")
    except ValueError as e:
        assert "长度" in str(e) or "len" in str(e).lower(), f"报错应指明长度规则，实得：{e}"


def test_password_insufficient_char_classes_rejected():
    """字符类不足三类 → 拒绝，报错指明字符类规则。"""
    try:
        validate_password("abcdefg123")  # 只有小写+数字，两类
        raise AssertionError("字符类不足应被拒")
    except ValueError as e:
        assert "字符类" in str(e) or "class" in str(e).lower() or "种类" in str(e) or "类型" in str(e), \
            f"报错应指明字符类规则，实得：{e}"


def test_password_contains_root_rejected():
    """密码包含 'root' → 拒绝。"""
    try:
        validate_password("MyRootPwd@@123")  # 含 root（大小写不敏感）
        raise AssertionError("含 root 应被拒")
    except ValueError as e:
        msg = str(e)
        assert "root" in msg.lower() or "用户名" in msg, f"报错应提及 root/用户名，实得：{e}"


def test_password_contains_toor_rejected():
    """密码包含 'toor'（root 逆序）→ 拒绝。"""
    try:
        validate_password("MyToorPwd@@123")  # 含 toor（大小写不敏感）
        raise AssertionError("含 toor 应被拒")
    except ValueError as e:
        msg = str(e)
        assert "toor" in msg.lower() or "root" in msg.lower() or "用户名" in msg, \
            f"报错应提及 toor/root/用户名，实得：{e}"


def test_password_valid_passes():
    """合规密码通过校验（大写+小写+数字+特殊，不含 root/toor）。"""
    validate_password("Abcd1234@")  # 不抛即通过
    validate_password("Xy9!abcd5678efgh")  # 16 chars，三大类+特殊


def test_password_disallowed_special_char_rejected():
    """特殊字符在允许集外（如 #、&）→ 拒绝。"""
    try:
        validate_password("Abcd1234#")  # # 不在允许集
        raise AssertionError("不允许的特殊字符应被拒")
    except ValueError as e:
        assert "特殊字符" in str(e) or "special" in str(e).lower() or "字符" in str(e), \
            f"报错应指明特殊字符规则，实得：{e}"


# ---- 密码自动生成 ----
def test_generated_password_length():
    """生成的密码为 16 位。"""
    pwd = generate_password()
    assert len(pwd) == 16, f"生成密码应为 16 位，实得 {len(pwd)}"


def test_generated_password_passes_validation():
    """生成的密码通过同一套校验。"""
    pwd = generate_password()
    validate_password(pwd)  # 不抛即通过


def test_generated_password_only_allowed_specials():
    """生成的密码只使用允许集中的特殊字符。"""
    for _ in range(50):  # 多次生成提高覆盖
        pwd = generate_password()
        specials = {c for c in pwd if not c.isalnum()}
        assert specials <= PASSWORD_SPECIAL_CHARS, \
            f"生成密码含不允许的特殊字符 {specials - PASSWORD_SPECIAL_CHARS}，密码：{pwd!r}"


def test_generated_password_has_at_least_3_classes():
    """生成的密码至少满足三类（多次生成）。"""
    for _ in range(50):
        pwd = generate_password()
        has_upper = any(c.isupper() for c in pwd)
        has_lower = any(c.islower() for c in pwd)
        has_digit = any(c.isdigit() for c in pwd)
        has_special = any(not c.isalnum() for c in pwd)
        classes = sum([has_upper, has_lower, has_digit, has_special])
        assert classes >= 3, f"生成密码字符类不足 3 类（{classes}），密码：{pwd!r}"


# ---- 不合规密码在构造请求时被拦 ----
def test_invalid_password_rejected_at_request_build():
    """CLI 提供的密码不合规 → 构造请求时就抛 ValueError，不发往 API。"""
    scope = make_scope()
    try:
        build_create_request(scope, make_args(password="short1"))
        raise AssertionError("不合规密码应被拦")
    except ValueError as e:
        # 确认是复杂度报错而非其他
        assert "长度" in str(e) or "len" in str(e).lower() or "字符" in str(e), \
            f"应报复杂度错误，实得：{e}"


def test_scope_invalid_password_rejected_at_request_build():
    """scope 提供的密码不合规 → 同样在构造请求时被拦。"""
    scope = make_scope(password="short1")
    try:
        build_create_request(scope, make_args())
        raise AssertionError("不合规密码应被拦")
    except ValueError as e:
        assert "长度" in str(e) or "len" in str(e).lower() or "字符" in str(e), \
            f"应报复杂度错误，实得：{e}"


# ---- CLI 密码落到请求体 ----
def test_dry_run_cli_password_reaches_request():
    """--password 从命令行接到请求体的 admin_pass 上（dry-run 输出中掩码，但字段存在）。"""
    server = dry_run_server("--password", "ValidPwd@@1234")
    assert server.get("admin_pass") == "******", \
        f"--password 应在 admin_pass 上（掩码），实得 {server.get('admin_pass')!r}"
    assert server.get("key_name") is None, \
        f"裁决为密码时 key_name 应为 None，实得 {server.get('key_name')!r}"


def test_dry_run_key_and_password_mutually_exclusive():
    """CLI 同时给 --key 和 --password 时，密钥对优先（顺位 1），admin_pass 不下发。"""
    server = dry_run_server("--key", "cli-key", "--password", "ValidPwd@@1234")
    assert server.get("key_name") == "cli-key", f"key_name 实得 {server.get('key_name')!r}"
    assert server.get("admin_pass") is None, f"密钥对优先时不应带 admin_pass，实得 {server.get('admin_pass')!r}"


# ---- 密码脱敏 ----
def test_dry_run_password_is_masked():
    """--dry-run 的 stdout 中密码为掩码——明文不出现。"""
    _, payload = run_cli(make_cli_scope(), ["create", "--dry-run", "--password", "ValidPwd@@1234"])
    raw = json.dumps(payload, ensure_ascii=False)
    assert "ValidPwd@@1234" not in raw, \
        f"dry-run 输出不应含明文密码"
    server = ((payload.get("request") or {}).get("body") or {}).get("server") or {}
    assert server.get("admin_pass") == "******", \
        f"dry-run 中 admin_pass 应掩码为 ******，实得 {server.get('admin_pass')!r}"


def test_dry_run_auto_generated_password_is_masked():
    """自动生成的密码在 dry-run 输出中也应掩码。"""
    _, payload = run_cli(make_cli_scope(), ["create", "--dry-run"])
    raw = json.dumps(payload, ensure_ascii=False)
    # 掩码格式应为 ******
    server = ((payload.get("request") or {}).get("body") or {}).get("server") or {}
    assert server.get("admin_pass") == "******", \
        f"自动生成的密码在 dry-run 应掩码为 ******，实得 {server.get('admin_pass')!r}"


def test_mask_password_function():
    """mask_password 把 admin_pass 值替换为 ******，不动其他字段。"""
    data = {"server": {"name": "ecs-1", "admin_pass": "Secret123@abc", "key_name": None}}
    masked = mask_password(data)
    assert masked["server"]["admin_pass"] == "******", \
        f"admin_pass 应掩码，实得 {masked['server']['admin_pass']!r}"
    assert masked["server"]["name"] == "ecs-1", "其他字段不应被改"


def test_mask_password_nested_in_result():
    """mask_password 能处理嵌套的 request.body.server.admin_pass。"""
    data = {"request": {"body": {"server": {"admin_pass": "Secret@@123456"}}}, "final": {"admin_pass": "X"}}
    masked = mask_password(data)
    assert masked["request"]["body"]["server"]["admin_pass"] == "******"
    assert masked["final"]["admin_pass"] == "******"


def test_mask_password_no_admin_pass_unchanged():
    """没有 admin_pass 字段时不报错、不改。"""
    data = {"server": {"name": "ecs-1"}}
    masked = mask_password(data)
    assert masked == data


# ---- 共享 scope.yaml 迁移 ----
def test_default_scope_path_at_repo_root():
    """DEFAULT_SCOPE_PATH 指向仓库根 scope.yaml，而非 skill 目录下。"""
    resolved = DEFAULT_SCOPE_PATH.resolve()
    assert resolved.name == "scope.yaml", f"文件名应为 scope.yaml，实得 {resolved.name!r}"
    # 仓库根 = 包含 .claude/ 的目录；scope.yaml 不应在 skill 目录内
    skill_dir = pathlib.Path(__file__).resolve().parent.parent  # .claude/skills/ecs-skill/
    assert resolved.parent != skill_dir, \
        f"scope.yaml 不应在 skill 目录 ({skill_dir})，实得 {resolved}"
    assert (resolved.parent / ".claude").is_dir(), \
        f"scope.yaml 应在仓库根（含 .claude/ 的目录），实得父目录 {resolved.parent}"


# ---- change-os：请求构造（缝 A）----
def make_change_os_args(**kw):
    """change-os 子命令的 CLI namespace 固件，默认全空。"""
    base = dict(
        instance_id=None, image_id=None, key=None, password=None,
        dry_run=False, timeout=600, poll_interval=10, port_grace=60,
    )
    base.update(kw)
    return Namespace(**base)


def test_change_os_requires_instance_id():
    """--instance-id 缺失 → ValueError 点名缺失字段。"""
    try:
        build_change_os_request(make_scope(), make_change_os_args(
            image_id="img-1", password="ValidPwd@@1234"))
        raise AssertionError("缺 --instance-id 应抛 ValueError")
    except ValueError as e:
        assert "instance" in str(e).lower(), f"报错应提及 instance，实得：{e}"


def test_change_os_requires_image_id():
    """--image-id 缺失 → ValueError 点名缺失字段。"""
    try:
        build_change_os_request(make_scope(), make_change_os_args(
            instance_id="srv-1", password="ValidPwd@@1234"))
        raise AssertionError("缺 --image-id 应抛 ValueError")
    except ValueError as e:
        assert "image" in str(e).lower(), f"报错应提及 image，实得：{e}"


def test_change_os_password_reaches_request():
    """--password → os_change.adminpass，走 validate_password；keyname 不下发。"""
    req = build_change_os_request(make_scope(), make_change_os_args(
        instance_id="srv-1", image_id="img-1", password="ValidPwd@@1234"))
    oc = req.body.os_change
    assert oc.imageid == "img-1", f"imageid 实得 {oc.imageid!r}"
    assert oc.adminpass == "ValidPwd@@1234", f"adminpass 实得 {oc.adminpass!r}"
    assert oc.keyname is None, f"密码裁决时 keyname 应为 None，实得 {oc.keyname!r}"


def test_change_os_key_reaches_request():
    """--key → os_change.keyname；adminpass 不下发。"""
    req = build_change_os_request(make_scope(), make_change_os_args(
        instance_id="srv-1", image_id="img-1", key="kp-1"))
    oc = req.body.os_change
    assert oc.keyname == "kp-1", f"keyname 实得 {oc.keyname!r}"
    assert oc.adminpass is None, f"密钥对裁决时 adminpass 应为 None，实得 {oc.adminpass!r}"


def test_change_os_key_wins_over_password():
    """同时给 --key 和 --password → 密钥对优先（与 create 裁决一致）。"""
    req = build_change_os_request(make_scope(), make_change_os_args(
        instance_id="srv-1", image_id="img-1", key="kp-1", password="ValidPwd@@1234"))
    oc = req.body.os_change
    assert oc.keyname == "kp-1", f"密钥对应优先，实得 keyname={oc.keyname!r}"
    assert oc.adminpass is None, f"密钥对优先时不应下发 adminpass，实得 {oc.adminpass!r}"


def test_change_os_mode_fixed_with_stop_server():
    """mode 恒为 withStopServer（不暴露 CLI）。"""
    req = build_change_os_request(make_scope(), make_change_os_args(
        instance_id="srv-1", image_id="img-1", password="ValidPwd@@1234"))
    assert req.body.os_change.mode == "withStopServer", \
        f"mode 应恒为 withStopServer，实得 {req.body.os_change.mode!r}"


def test_change_os_scope_password_fallback():
    """--password 不给且无 --key → 从 scope ecs_create.server.password 兜底。"""
    scope = make_scope(password="ScopePwd@@1234")
    req = build_change_os_request(scope, make_change_os_args(
        instance_id="srv-1", image_id="img-1"))
    assert req.body.os_change.adminpass == "ScopePwd@@1234", \
        f"应从 scope 密码兜底，实得 {req.body.os_change.adminpass!r}"
    assert req.body.os_change.keyname is None, \
        f"密码裁决时 keyname 应为 None，实得 {req.body.os_change.keyname!r}"


def test_change_os_no_credentials_raises():
    """无 --key / --password / scope 密码 → 报错（change-os 不自动生成）。"""
    try:
        build_change_os_request(make_scope(), make_change_os_args(
            instance_id="srv-1", image_id="img-1"))
        raise AssertionError("无凭证应抛 ValueError")
    except ValueError as e:
        assert "凭证" in str(e) or "password" in str(e).lower() or "key" in str(e).lower(), \
            f"报错应提及凭证缺失，实得：{e}"


def test_change_os_invalid_password_rejected():
    """--password 不合规 → 构造请求时被拦（走 validate_password）。"""
    try:
        build_change_os_request(make_scope(), make_change_os_args(
            instance_id="srv-1", image_id="img-1", password="short1"))
        raise AssertionError("不合规密码应被拦")
    except ValueError as e:
        assert "长度" in str(e) or "len" in str(e).lower() or "字符" in str(e), \
            f"应报复杂度错误，实得：{e}"


def test_change_os_server_id_carried():
    """--instance-id → request.server_id。"""
    req = build_change_os_request(make_scope(), make_change_os_args(
        instance_id="srv-abc", image_id="img-1", password="ValidPwd@@1234"))
    assert req.server_id == "srv-abc", f"server_id 实得 {req.server_id!r}"


def test_change_os_scope_invalid_password_rejected():
    """scope 密码不合规 → 兜底路径同样在构造请求时被拦。"""
    scope = make_scope(password="short1")
    try:
        build_change_os_request(scope, make_change_os_args(
            instance_id="srv-1", image_id="img-1"))
        raise AssertionError("scope 不合规密码应被拦")
    except ValueError as e:
        assert "长度" in str(e) or "len" in str(e).lower() or "字符" in str(e), \
            f"应报复杂度错误，实得：{e}"


# ---- change-os --dry-run 端到端（缝 B）----
def _change_os_dry_run(*flags):
    """跑 change-os --dry-run，返回 (payload, request_dict)。"""
    _, payload = run_cli(make_cli_scope(), ["change-os", "--dry-run", *flags])
    assert payload is not None, "change-os --dry-run 应有 stdout JSON"
    request = payload.get("request")
    assert request is not None, f"输出应含 request，实得：{payload!r}"
    return payload, request


def test_change_os_dry_run_contract():
    """change-os --dry-run：stdout 纯 JSON、dry_run:true、action:change-os、退出码 0。"""
    code, payload = run_cli(make_cli_scope(), [
        "change-os", "--dry-run", "--instance-id", "srv-1", "--image-id", "img-1",
        "--password", "ValidPwd@@1234"])
    assert code == 0, f"退出码应为 0，实得 {code!r}"
    assert payload.get("ok") is True, f"ok 应为 True，实得 {payload.get('ok')!r}"
    assert payload.get("action") == "change-os", f"action 实得 {payload.get('action')!r}"
    assert payload.get("dry_run") is True, f"dry_run 应为 True，实得 {payload.get('dry_run')!r}"


def test_change_os_dry_run_request_fields():
    """请求体含 server_id / imageid / adminpass（掩码）/ mode。"""
    _, request = _change_os_dry_run("--instance-id", "srv-1", "--image-id", "img-1",
                                     "--password", "ValidPwd@@1234")
    assert request.get("server_id") == "srv-1", f"server_id 实得 {request.get('server_id')!r}"
    os_change = (request.get("body") or {}).get("os_change") or {}
    assert os_change.get("imageid") == "img-1", f"imageid 实得 {os_change.get('imageid')!r}"
    assert os_change.get("adminpass") == "******", f"adminpass 应掩码，实得 {os_change.get('adminpass')!r}"
    assert os_change.get("mode") == "withStopServer", f"mode 实得 {os_change.get('mode')!r}"


def test_change_os_dry_run_key_reaches_request():
    """--key 从命令行接到请求体；密钥对优先时不带 adminpass。"""
    _, request = _change_os_dry_run("--instance-id", "srv-1", "--image-id", "img-1",
                                     "--key", "cli-key")
    os_change = (request.get("body") or {}).get("os_change") or {}
    assert os_change.get("keyname") == "cli-key", f"keyname 实得 {os_change.get('keyname')!r}"
    assert "adminpass" not in os_change, \
        f"密钥对裁决时不应下发 adminpass，实得 {os_change.get('adminpass')!r}"


def test_change_os_dry_run_password_masked():
    """dry-run 输出中密码为掩码——明文不出现。"""
    _, payload = _change_os_dry_run("--instance-id", "srv-1", "--image-id", "img-1",
                                     "--password", "ValidPwd@@1234")
    raw = json.dumps(payload, ensure_ascii=False)
    assert "ValidPwd@@1234" not in raw, "dry-run 输出不应含明文密码"


def test_change_os_dry_run_no_network():
    """dry-run 全程不触网（socket 层设陷阱——run_cli 已内置 _no_network）。"""
    code, payload = run_cli(make_cli_scope(), [
        "change-os", "--dry-run", "--instance-id", "srv-1", "--image-id", "img-1",
        "--password", "ValidPwd@@1234"])
    assert code == 0 and payload is not None, "dry-run 不应触网且应正常输出"


def test_change_os_dry_run_scope_password_fallback():
    """change-os --dry-run 不给 --password 时从 scope 密码兜底，掩码输出。"""
    scope = make_cli_scope()
    scope.setdefault("ecs_create", {}).setdefault("server", {})["password"] = "ScopePwd@@1234"
    _, payload = run_cli(scope, [
        "change-os", "--dry-run", "--instance-id", "srv-1", "--image-id", "img-1"])
    # make_cli_scope 的 scope 已含 ecs_create.server；上面 setdefault 在已有结构上补 password
    os_change = ((payload.get("request") or {}).get("body") or {}).get("os_change") or {}
    assert os_change.get("adminpass") == "******", \
        f"scope 兜底密码应掩码，实得 {os_change.get('adminpass')!r}"


def test_change_os_requires_instance_id_at_cli():
    """argparse 把 --instance-id 标为 required：缺失时退出码 2。"""
    code, _ = run_cli(make_cli_scope(), [
        "change-os", "--dry-run", "--image-id", "img-1", "--password", "ValidPwd@@1234"])
    assert code == 2, f"缺 --instance-id 应被 argparse 拒绝（退出码 2），实得 {code!r}"


def test_change_os_requires_image_id_at_cli():
    """argparse 把 --image-id 标为 required：缺失时退出码 2。"""
    code, _ = run_cli(make_cli_scope(), [
        "change-os", "--dry-run", "--instance-id", "srv-1", "--password", "ValidPwd@@1234"])
    assert code == 2, f"缺 --image-id 应被 argparse 拒绝（退出码 2），实得 {code!r}"


def test_mask_password_adminpass_key():
    """mask_password 也识别 adminpass（无下划线）键——change-os 的字段名。"""
    data = {"os_change": {"adminpass": "Secret@@123456", "imageid": "img-1"}}
    masked = mask_password(data)
    assert masked["os_change"]["adminpass"] == "******", \
        f"adminpass 应掩码，实得 {masked['os_change']['adminpass']!r}"
    assert masked["os_change"]["imageid"] == "img-1", "其他字段不应被改"


# ---- delete：请求构造（缝 A）----
def test_delete_requires_instance_id():
    """空 instance_id → ValueError 点名缺失标识。"""
    try:
        build_delete_request("")
        raise AssertionError("空 instance_id 应抛 ValueError")
    except ValueError as e:
        assert "id" in str(e).lower() or "标识" in str(e), f"报错应提及 id/标识，实得：{e}"


def test_delete_request_carries_server_id():
    """instance_id 落到 servers[0].id。"""
    req = build_delete_request("srv-abc")
    servers = req.body.servers
    assert servers and servers[0].id == "srv-abc", \
        f"servers[0].id 应为 srv-abc，实得 {servers!r}"


def test_delete_cascade_publicip_true():
    """delete_publicip 恒为 True（释放绑定的 EIP）。"""
    req = build_delete_request("srv-1")
    assert req.body.delete_publicip is True, \
        f"delete_publicip 应恒为 True，实得 {req.body.delete_publicip!r}"


def test_delete_cascade_volume_true():
    """delete_volume 恒为 True（删除数据盘 + 系统盘随实例默认删除）。"""
    req = build_delete_request("srv-1")
    assert req.body.delete_volume is True, \
        f"delete_volume 应恒为 True，实得 {req.body.delete_volume!r}"


def test_delete_request_single_server():
    """servers 列表只有一条（一次删一台）。"""
    req = build_delete_request("srv-1")
    assert len(req.body.servers) == 1, \
        f"servers 应只有 1 条，实得 {len(req.body.servers)} 条"


# ---- delete --dry-run 端到端（缝 B）----
def test_delete_dry_run_by_id():
    """delete --dry-run --id：stdout 纯 JSON、dry_run:true、action:delete、退出码 0。"""
    code, payload = run_cli(make_cli_scope(), ["delete", "--dry-run", "--id", "srv-1"])
    assert code == 0, f"退出码应为 0，实得 {code!r}"
    assert payload.get("ok") is True, f"ok 应为 True，实得 {payload.get('ok')!r}"
    assert payload.get("action") == "delete", f"action 实得 {payload.get('action')!r}"
    assert payload.get("dry_run") is True, f"dry_run 应为 True，实得 {payload.get('dry_run')!r}"
    assert payload.get("region") == "cn-north-4", f"region 实得 {payload.get('region')!r}"


def test_delete_dry_run_id_reaches_request():
    """--id 落到 request.body.servers[0].id。"""
    _, payload = run_cli(make_cli_scope(), ["delete", "--dry-run", "--id", "srv-abc"])
    body = ((payload.get("request") or {}).get("body")) or {}
    servers = body.get("servers") or []
    assert servers and servers[0].get("id") == "srv-abc", \
        f"servers[0].id 应为 srv-abc，实得 {servers!r}"


def test_delete_dry_run_cascade_flags():
    """dry-run 输出中 delete_publicip=True、delete_volume=True。"""
    _, payload = run_cli(make_cli_scope(), ["delete", "--dry-run", "--id", "srv-1"])
    body = ((payload.get("request") or {}).get("body")) or {}
    assert body.get("delete_publicip") is True, \
        f"delete_publicip 应为 True，实得 {body.get('delete_publicip')!r}"
    assert body.get("delete_volume") is True, \
        f"delete_volume 应为 True，实得 {body.get('delete_volume')!r}"


def test_delete_dry_run_by_name():
    """delete --dry-run --name：dry-run 不触网，输出 name + cascade info。"""
    code, payload = run_cli(make_cli_scope(), ["delete", "--dry-run", "--name", "web-01"])
    assert code == 0, f"退出码应为 0，实得 {code!r}"
    assert payload.get("ok") is True
    assert payload.get("action") == "delete"
    assert payload.get("dry_run") is True
    assert payload.get("name") == "web-01", f"name 实得 {payload.get('name')!r}"


def test_delete_dry_run_name_cascade_flags():
    """--name 的 dry-run 也显示 cascade flags。"""
    _, payload = run_cli(make_cli_scope(), ["delete", "--dry-run", "--name", "web-01"])
    cascade = payload.get("cascade") or {}
    assert cascade.get("delete_publicip") is True, \
        f"cascade.delete_publicip 应为 True，实得 {cascade!r}"
    assert cascade.get("delete_volume") is True, \
        f"cascade.delete_volume 应为 True，实得 {cascade!r}"


def test_delete_requires_id_or_name():
    """delete 不给 --id 也不给 --name → argparse 报错（退出码 2）。"""
    code, _ = run_cli(make_cli_scope(), ["delete", "--dry-run"])
    assert code == 2, f"不给 --id/--name 应被 argparse 拒绝（退出码 2），实得 {code!r}"


def test_delete_id_and_name_mutually_exclusive():
    """同时给 --id 和 --name → argparse 报错（退出码 2）。"""
    code, _ = run_cli(make_cli_scope(), ["delete", "--dry-run", "--id", "srv-1", "--name", "web-01"])
    assert code == 2, f"同时给 --id/--name 应被拒绝（退出码 2），实得 {code!r}"


def test_delete_dry_run_no_network():
    """dry-run 全程不触网（socket 层设陷阱——run_cli 已内置 _no_network）。"""
    code, payload = run_cli(make_cli_scope(), ["delete", "--dry-run", "--id", "srv-1"])
    assert code == 0 and payload is not None, "dry-run 不应触网且应正常输出"


# ---- poll_until_deleted：删除轮询判定 ----
@contextlib.contextmanager
def _patch_show_server(*states):
    """临时把 ecs.show_server 替换为按序吐状态的替身，结束后还原。

    与仓库既有 _isolate_env / _no_network 同一 contextmanager 范式。
    元素形态：None → 该轮返回 None（实例从 API 消失）；
    SimpleNamespace → 完整 server 桩原样返回（含 image/addresses）；
    其余 → 包成 SimpleNamespace(status=...)。
    耗尽后重复最后一个状态（用于「始终不变」场景）。
    """
    mocked = [s if isinstance(s, SimpleNamespace) else
              (None if s is None else SimpleNamespace(status=s)) for s in states]
    counter = [0]
    orig = ecs.show_server

    def _mock(client, *, server_id):
        i = min(counter[0], len(mocked) - 1)
        counter[0] += 1
        return mocked[i]

    ecs.show_server = _mock
    try:
        yield
    finally:
        ecs.show_server = orig


def test_poll_until_deleted_returns_on_deleted_status():
    """华为云删除后实例以 DELETED 状态保留在 API → 检测到 DELETED 应立即判定成功。

    回归 bug：旧逻辑只认「实例从 API 查不到（None）」为成功，而 DELETED 状态会
    一直非 None，导致轮询空转至超时（实网删除 ~10s 完成，却空等 600s）。
    """
    with _patch_show_server("DELETED"):
        trace = []
        status = ecs.poll_until_deleted(
            client=None, server_id="srv-1", timeout=10, interval=0, trace=trace)
        assert status == "DELETED", f"DELETED 状态应判定为已删除，实得 {status!r}"
        assert len(trace) == 1, f"应只轮询一次即返回，实得 {len(trace)} 次"


def test_poll_until_deleted_transitions_active_to_deleted():
    """先 ACTIVE 再 DELETED → 在 DELETED 那一轮返回，不超时。"""
    with _patch_show_server("ACTIVE", "DELETED"):
        trace = []
        status = ecs.poll_until_deleted(
            client=None, server_id="srv-1", timeout=10, interval=0, trace=trace)
        assert status == "DELETED", f"应在 DELETED 时返回，实得 {status!r}"
        assert len(trace) == 2, f"应轮询两次（ACTIVE→DELETED），实得 {len(trace)} 次"


def test_poll_until_deleted_returns_when_gone():
    """show_server 返回 None（实例彻底从 API 消失）→ 仍判定 DELETED（兼容）。"""
    with _patch_show_server(None):
        trace = []
        status = ecs.poll_until_deleted(
            client=None, server_id="srv-1", timeout=10, interval=0, trace=trace)
        assert status == "DELETED", f"实例消失应判定为已删除，实得 {status!r}"


def test_poll_until_deleted_timeout_when_never_deleted():
    """始终 ACTIVE（删除请求未生效）→ 超时返回 TIMEOUT。"""
    with _patch_show_server("ACTIVE"):
        trace = []
        status = ecs.poll_until_deleted(
            client=None, server_id="srv-1", timeout=0.3, interval=0, trace=trace)
        assert status == "TIMEOUT", f"持续 ACTIVE 应超时，实得 {status!r}"


# ---- poll_until_ready：change-os 就绪判定（镜像翻转正向证据） ----
def _os_server(status, image_id, *entries):
    """伪造带状态、镜像元数据与地址的 server（poll_until_ready 的查询桩）。"""
    return SimpleNamespace(
        status=status, image=SimpleNamespace(id=image_id),
        addresses={"net": list(entries)},
    )


def test_poll_until_ready_image_flip_regression():
    """回归：change-os 提交后旧系统仍 ACTIVE + IP 可达，镜像未换不得判就绪。"""
    with _patch_show_server(
        _os_server("ACTIVE", "img-old", _addr_entry("10.0.0.5", "fixed")),
        _os_server("SHUTOFF", "img-old", _addr_entry("10.0.0.5", "fixed")),
        _os_server("ACTIVE", "img-new", _addr_entry("10.0.0.5", "fixed")),
    ):
        trace = []
        server, status, ip = ecs.poll_until_ready(
            client=None, server_id="srv-1", timeout=5, interval=0, trace=trace,
            has_eip=False, expect_image_id="img-new")
        assert status == "ACTIVE", f"应在镜像切换后就绪，实得 {status!r}"
        assert ip == "10.0.0.5", f"应取私网 IP，实得 {ip!r}"
        stale = [t for t in trace if t.get("status") == "ACTIVE" and t.get("image") == "img-old"]
        assert stale, "旧镜像 + ACTIVE 的轮询点不得判就绪（回归点）"


def test_poll_until_ready_image_never_flips_timeout():
    """镜像始终未切换（仍是旧镜像）→ 走到超时，不误判就绪。"""
    with _patch_show_server(
        _os_server("ACTIVE", "img-old", _addr_entry("10.0.0.5", "fixed")),
    ):
        status = ecs.poll_until_ready(
            client=None, server_id="srv-1", timeout=0.3, interval=0.05, trace=[],
            has_eip=False, expect_image_id="img-new")[1]
        assert status == "TIMEOUT", f"镜像未换应超时，实得 {status!r}"


def test_poll_until_ready_without_expect_keeps_create_semantics():
    """不传 expect_image_id（create 路径）→ ACTIVE + IP 即就绪，行为不变。"""
    with _patch_show_server(
        _os_server("ACTIVE", "img-old", _addr_entry("10.0.0.5", "fixed")),
    ):
        server, status, ip = ecs.poll_until_ready(
            client=None, server_id="srv-1", timeout=5, interval=0, trace=[],
            has_eip=False)
        assert status == "ACTIVE", f"首轮 ACTIVE 应即就绪，实得 {status!r}"
        assert ip == "10.0.0.5", f"应取私网 IP，实得 {ip!r}"


# ---- 定时删除（auto_terminate_time + EIP 级联释放） ----
def test_default_sets_auto_terminate_24h():
    """默认：now+24h 的 UTC ISO8601，秒归零（对齐华为「ss 非 00 取整分钟」）。"""
    req = build_create_request(make_scope(), make_args(_now=datetime(2026, 9, 20, 10, 30, 45)))
    assert req.body.server.auto_terminate_time == "2026-09-21T10:31:00Z", \
        f"实得 {req.body.server.auto_terminate_time!r}"
    # 输入秒归零时不进位（取整只处理非零秒）
    req2 = build_create_request(make_scope(), make_args(_now=datetime(2026, 9, 20, 10, 30, 0)))
    assert req2.body.server.auto_terminate_time == "2026-09-21T10:30:00Z", \
        f"整分输入实得 {req2.body.server.auto_terminate_time!r}"


def test_terminate_hours_flag_overrides():
    """--terminate-hours 4 → now+4h。"""
    req = build_create_request(
        make_scope(), make_args(terminate_hours=4, _now=datetime(2026, 9, 20, 23, 15, 0)))
    assert req.body.server.auto_terminate_time == "2026-09-21T03:15:00Z", \
        f"实得 {req.body.server.auto_terminate_time!r}"


def test_no_auto_terminate_disables():
    """--no-auto-terminate → 不下发字段（长期机不自动销毁）。"""
    req = build_create_request(make_scope(), make_args(no_auto_terminate=True))
    assert req.body.server.auto_terminate_time is None


def test_terminate_hours_bounds():
    """边界校验：最短半小时（API 约束）、最长三年；nan 拦下。"""
    build_create_request(make_scope(), make_args(terminate_hours=0.5))  # 合法下界
    build_create_request(make_scope(), make_args(terminate_hours=24 * 365 * 3))  # 合法上界
    for bad in (0.1, 24 * 365 * 3 + 1, float("nan"), float("inf")):
        try:
            build_create_request(make_scope(), make_args(terminate_hours=bad))
        except ValueError as e:
            assert "定时删除" in str(e), f"报错应说明定时删除，实得：{e}"
        else:
            raise AssertionError(f"terminate_hours={bad} 应抛 ValueError")


def test_min_half_hour_kept_after_ceil():
    """0.5h 下界 + 秒非零：取整向后进位，实际提前量不跌破半小时（API 硬约束）。"""
    for sec in (0, 1, 30, 59):
        req = build_create_request(
            make_scope(), make_args(terminate_hours=0.5, _now=datetime(2026, 9, 20, 10, 30, sec)))
        got = datetime.strptime(req.body.server.auto_terminate_time, "%Y-%m-%dT%H:%M:%SZ")
        base = datetime(2026, 9, 20, 10, 30, 0)
        assert (got - base) >= timedelta(minutes=30), \
            f"秒={sec} 时提前量不足半小时，实得 {req.body.server.auto_terminate_time!r}"


def test_publicip_delete_on_termination_true():
    """默认带 EIP 时，EIP 随实例释放（定时删除路径只认创建时的该字段）。"""
    scope = make_scope(**_EIP_TEMPLATE)
    req = build_create_request(scope, make_args())
    pub = req.body.server.publicip
    assert pub is not None and pub.delete_on_termination is True, f"实得 {pub!r}"


def test_no_eip_still_no_publicip():
    """--no-eip 时无 publicip，不涉及 EIP 级联。"""
    scope = make_scope(**_EIP_TEMPLATE)
    req = build_create_request(scope, make_args(no_eip=True))
    assert req.body.server.publicip is None


def test_dry_run_auto_terminate_reaches_request():
    """CLI 端到端：默认 24h 走到请求体；--terminate-hours/--no-auto-terminate 覆盖；EIP 级联。"""
    att = dry_run_server().get("auto_terminate_time")
    assert att is not None and att.endswith("Z") and len(att) == 20, f"实得 {att!r}"
    now = datetime.now(timezone.utc)
    got = datetime.strptime(att, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    assert abs((got - (now + timedelta(hours=24))).total_seconds()) < 120, \
        f"默认应约为 now+24h，实得 {att!r}"
    att4 = dry_run_server("--terminate-hours", "4").get("auto_terminate_time")
    got4 = datetime.strptime(att4, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    assert abs((got4 - (now + timedelta(hours=4))).total_seconds()) < 120, \
        f"--terminate-hours 4 应约为 now+4h，实得 {att4!r}"
    assert dry_run_server("--no-auto-terminate").get("auto_terminate_time") is None
    pub = dry_run_server().get("publicip") or {}
    assert pub.get("delete_on_termination") is True, f"实得 {pub!r}"



TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]

if __name__ == "__main__":
    failed = 0
    for t in TESTS:
        try:
            t()
            print(f"✓ {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"✗ {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"✗ {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(TESTS) - failed}/{len(TESTS)} passed")
    sys.exit(1 if failed else 0)

