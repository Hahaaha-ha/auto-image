#!/usr/bin/env python3
"""ECS 端点主缝测试 —— 清单 / 存活检查 / 建机 / 表单默认值 + 错误面映射。

缝：同 test_obs_api 的 ASGI 测试客户端；ecs 函数注入假实现，清单投影
通过 SDK 请求模型与假云客户端验证（不触网）。默认路径绑定测试用空 scope → EcsNotConfigured →
503，不阻断其余端点。纯函数（_read_scope / resolve_create_spec /
generate_password）直测。纯 assert，无 pytest。

运行：python web/tests/test_ecs_api.py
"""
import asyncio
import os
import re
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from web.ecs import (  # noqa: E402
    EcsApiError,
    EcsNotConfigured,
    _read_scope,
    generate_password,
    resolve_create_spec,
    validate_password,
)
from web.tests.support import async_client, make_test_app  # noqa: E402

PLAIN_AK = "AKTESTTOPSECRET0123456789"
PLAIN_SK = "SKTESTTOPSECRET0123456789xyz"
FULL_SCOPE = {
    "ak": PLAIN_AK, "sk": PLAIN_SK, "region": "ap-southeast-1",
    "ecs_create": {
        "server": {
            "imageRef": "img-001", "flavorRef": "kc1.xlarge.2", "vpcid": "vpc-1",
            "nics": [{"subnet_id": "subnet-1"}],
            "security_groups": [{"id": "sg-1"}],
            "availability_zone": "ap-southeast-1a",
            "key_name": "kp-web",
            "root_volume": {"volumetype": "GPSSD", "size": 60},
            "publicip": {"eip": {"iptype": "5_bgp",
                                 "bandwidth": {"sharetype": "PER", "size": 10, "chargemode": "traffic"}}},
        }
    },
}

FIXTURE_LIST = {
    "region": "ap-southeast-1", "count": 2,
    "instances": [
        {"id": "srv-1", "name": "ecs-a1b2c3d4", "status": "ACTIVE", "ip": "1.2.3.4",
         "ip_type": "floating", "flavor": "kc1.xlarge.2", "image": "img-001",
         "availability_zone": "ap-southeast-1a", "created": "2026-09-14T01:02:03Z",
         "key_name": "kp-web"},
        {"id": "srv-2", "name": "ecs-shutoff", "status": "SHUTOFF", "ip": "5.6.7.8",
         "ip_type": "private", "flavor": "kc1.large.2", "image": "img-002",
         "availability_zone": None, "created": "", "key_name": None},
    ],
}

FIXTURE_CHECK = {
    "region": "ap-southeast-1", "checked_at": 1789000000.0, "count": 2, "alive_count": 1,
    "instances": [
        {**FIXTURE_LIST["instances"][0], "ssh_port_open": True, "alive": True},
        {**FIXTURE_LIST["instances"][1], "ssh_port_open": False, "alive": False},
    ],
}


class StubSdkExc:
    """EcsApiError 的错误面来源（SDK ServiceResponseException 最小桩）。"""

    status_code = 403
    error_code = "Ecs.0018"
    error_msg = "flavor sold out"
    request_id = "req-1"


class _Env:
    """华为云/ECS 凭据环境变量快照恢复（真实实现路径的隔离护栏）。"""

    KEYS = ("HUAWEICLOUD_SDK_AK", "HUAWEICLOUD_SDK_SK", "HUAWEICLOUD_SDK_REGION",
            "HUAWEICLOUD_SDK_PROJECT_ID", "ECS_ADMIN_PASSWORD")

    def __init__(self, **overrides):
        self.overrides = overrides

    def __enter__(self):
        self.saved = {k: os.environ.get(k) for k in self.KEYS}
        for k in self.KEYS:
            os.environ.pop(k, None)
        os.environ.update(self.overrides)
        return self

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return False


def _scope_file(scope):
    td = tempfile.TemporaryDirectory(prefix="auto-image-ecs-test-")
    path = Path(td.name) / "scope.yaml"
    path.write_text(yaml.safe_dump(scope), encoding="utf-8")
    return td, path


async def test_default_scope_reports_not_configured():
    """未注入假函数时默认绑真实实现：空 scope → 清单/检查/建机 503、
    defaults 正常 200 且 configured=False（面板据此显示可修复状态）。"""
    with _Env():
        async with async_client(make_test_app()) as client:
            r = await client.get("/api/ecs/instances")
            assert r.status_code == 503, r.text
            assert "not configured" in r.json()["detail"], r.text
            r = await client.post("/api/ecs/check")
            assert r.status_code == 503, r.text
            r = await client.post("/api/ecs/create", json={})
            assert r.status_code == 503, r.text
            r = await client.get("/api/ecs/defaults")
            assert r.status_code == 200, r.text
            assert r.json()["configured"] is False, r.text


async def test_list_shape_and_limit_clamp():
    seen = {}

    def fake_list(limit=500):
        seen["limit"] = limit
        return FIXTURE_LIST

    async with async_client(make_test_app(ecs_list_fn=fake_list)) as client:
        r = await client.get("/api/ecs/instances?limit=99999")
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["region"] == "ap-southeast-1"
        assert data["count"] == 2
        assert data["instances"][0]["id"] == "srv-1"
        assert data["instances"][1]["status"] == "SHUTOFF"
        assert seen["limit"] == 1000  # 上限钳制透传给实现


async def test_check_shape():
    def fake_check():
        return FIXTURE_CHECK

    async with async_client(make_test_app(ecs_check_fn=fake_check)) as client:
        r = await client.post("/api/ecs/check")
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["alive_count"] == 1
        assert data["instances"][0]["alive"] is True
        assert data["instances"][0]["ssh_port_open"] is True
        assert data["instances"][1]["alive"] is False


async def test_list_and_check_preserve_cloud_delete_times():
    """清单和存活检查透传云端计划时间，空值和缺失不根据创建时间补造。"""
    servers = [
        SimpleNamespace(id="scheduled", auto_terminate_time="2026-10-09T08:00:00Z"),
        SimpleNamespace(id="empty", auto_terminate_time=""),
        SimpleNamespace(id="unset", auto_terminate_time=None),
        SimpleNamespace(id="missing"),
    ]
    for server in servers:
        server.status = "ACTIVE"
        server.created = "2026-10-01T08:00:00Z"
    cloud = SimpleNamespace(list_servers_details=lambda req: SimpleNamespace(
        servers=servers, count=len(servers),
    ))
    with _Env():
        td, path = _scope_file(FULL_SCOPE)
        try:
            # 只替换 SDK 的云客户端；保留真实列表、投影、存活检查及 HTTP 响应。
            with patch("huaweicloudsdkcore.client.ClientBuilder.build", return_value=cloud):
                async with async_client(make_test_app(scope_config=path)) as client:
                    for method, endpoint in (("GET", "/api/ecs/instances"),
                                             ("POST", "/api/ecs/check")):
                        r = await client.request(method, endpoint)
                        assert r.status_code == 200, r.text
                        times = {i["id"]: i["auto_terminate_time"] for i in r.json()["instances"]}
                        assert times == {
                            "scheduled": "2026-10-09T08:00:00Z",
                            "empty": "", "unset": None, "missing": None,
                        }, r.text
        finally:
            td.cleanup()


async def test_check_error_maps_502():
    def fake_check():
        raise EcsApiError(StubSdkExc())

    async with async_client(make_test_app(ecs_check_fn=fake_check)) as client:
        r = await client.post("/api/ecs/check")
        assert r.status_code == 502, r.text
        detail = r.json()["detail"]
        assert detail["error_code"] == "Ecs.0018", r.text
        assert detail["status_code"] == 403
        assert detail["request_id"] == "req-1"


async def test_create_body_validation_422():
    calls = []

    def fake_create(body=None):
        calls.append(body)
        return {"ok": True}

    async with async_client(make_test_app(ecs_create_fn=fake_create)) as client:
        bad_bodies = [
            {"name": "a/b"},
            {"name": "x" * 65},
            {"name": 123},
            {"flavor": 42},
            {"image": "  "},
            {"disk_size": "x"},
            {"disk_size": 5},
            {"disk_size": True},
            {"bandwidth": 0},
            {"bandwidth": 99999},
        ]
        for b in bad_bodies:
            r = await client.post("/api/ecs/create", json=b)
            assert r.status_code == 422, (b, r.text)
        assert calls == []  # 非法输入在端点拦下，不触实现


async def test_create_happy_passthrough():
    seen = {}

    def fake_create(body=None):
        seen["body"] = body
        return {"ok": True, "action": "create", "name": "ecs-ff001122", "id": "srv-9",
                "ip": "9.9.9.9", "ip_type": "floating", "status": "ACTIVE",
                "ssh_port_open": True, "region": "ap-southeast-1",
                "flavor": "kc1.xlarge.2", "image": "img-001", "job_id": "job-1",
                "auth_method": "password", "admin_pass": "GenPwd!2026x"}

    async with async_client(make_test_app(ecs_create_fn=fake_create)) as client:
        r = await client.post("/api/ecs/create",
                              json={"name": "  my-ecs  ", "flavor": "kc1.xlarge.2",
                                    "disk_size": 100, "bandwidth": 8})
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["ok"] is True
        assert data["admin_pass"] == "GenPwd!2026x"  # 成功响应携带登录密码
        assert seen["body"] == {"name": "my-ecs", "flavor": "kc1.xlarge.2",
                                "disk_size": 100, "bandwidth": 8}  # 清洗后透传


async def test_create_not_ready_is_200():
    def fake_create(body=None):
        return {"ok": False, "action": "create", "id": "srv-1", "ip": None,
                "status": "TIMEOUT", "ssh_port_open": False,
                "error": "轮询超时（600s）仍未 ACTIVE", "hint": "稍后复查；未自动销毁。"}

    async with async_client(make_test_app(ecs_create_fn=fake_create)) as client:
        r = await client.post("/api/ecs/create", json={})
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["ok"] is False
        assert data["id"] == "srv-1"  # 未就绪也 200：机器可能已建出，id 不丢


async def test_defaults_from_scope_without_secrets():
    with _Env():
        td, path = _scope_file(FULL_SCOPE)
        try:
            async with async_client(make_test_app(scope_config=path)) as client:
                r = await client.get("/api/ecs/defaults")
                assert r.status_code == 200, r.text
                data = r.json()
                assert data["configured"] is True, r.text
                assert data["flavor"] == "kc1.xlarge.2"
                assert data["image"] == "img-001"
                assert data["disk_size"] == 60  # root_volume.size
                assert data["bandwidth"] == 10  # publicip.eip.bandwidth.size
                assert data["availability_zone"] == "ap-southeast-1a"
                assert data["has_eip"] is True
                assert data["auth_method"] == "key_pair"  # scope key_name 命中
                assert data["auto_password"] is False
                assert PLAIN_AK not in r.text and PLAIN_SK not in r.text  # 无密钥泄漏
        finally:
            td.cleanup()


async def test_defaults_missing_server_fields():
    with _Env():
        scope = {"ak": PLAIN_AK, "sk": PLAIN_SK, "region": "ap-southeast-1",
                 "ecs_create": {"server": {"imageRef": "img-001"}}}
        td, path = _scope_file(scope)
        try:
            async with async_client(make_test_app(scope_config=path)) as client:
                r = await client.get("/api/ecs/defaults")
                assert r.status_code == 200, r.text
                data = r.json()
                assert data["configured"] is False
                assert "flavorRef" in data["reason"], data["reason"]
        finally:
            td.cleanup()


async def test_read_scope_unwraps_default_block():
    td, path = _scope_file({"default": {"ak": "a", "region": "r"}})
    try:
        assert _read_scope(path) == {"ak": "a", "region": "r"}
        assert _read_scope(path.with_name("missing.yaml")) == {}
    finally:
        td.cleanup()


async def test_resolve_create_spec_merge_and_defaults():
    scope = {"ecs_create": {"server": {
        "imageRef": "img-001", "flavorRef": "kc1.xlarge.2", "vpcid": "vpc-1",
        "nics": [{"subnet_id": "subnet-1"}], "security_groups": [{"id": "sg-1"}],
    }}}
    with _Env():
        spec = resolve_create_spec(scope, {"flavor": "kc1.large.2", "disk_size": 100,
                                           "bandwidth": 8, "name": "web-1"})
        assert spec["flavor_ref"] == "kc1.large.2"  # 表单覆盖
        assert spec["image_ref"] == "img-001"  # 缺省沿用 scope
        assert spec["name"] == "web-1"
        assert spec["size"] == 100
        assert spec["eip"] == {"iptype": "5_bgp", "sharetype": "PER",
                               "chargemode": "traffic", "size": 8}  # EIP 模板兜底 + 带宽覆盖
        assert spec["auth_method"] == "password"  # 无 env/key/scope 密码 → 自动生成
        assert spec["key_name"] is None
        validate_password(spec["admin_pass"])

        auto = resolve_create_spec(scope, {})
        assert re.fullmatch(r"ecs-[0-9a-f]{8}", auto["name"])  # 自动名
        assert auto["size"] == 40 and auto["eip"]["size"] == 5  # root_volume/EIP 硬默认


async def test_resolve_create_spec_auth_tiers():
    base = {"ecs_create": {"server": {
        "imageRef": "img", "flavorRef": "f", "vpcid": "v",
        "nics": [{"subnet_id": "s"}], "key_name": "kp-web", "password": "ScopePwd!2026x",
    }}}
    with _Env():
        both = resolve_create_spec(base, {})
        assert both["key_name"] == "kp-web" and both["admin_pass"] is None  # scope 内密钥对优先
        with _Env(**{"ECS_ADMIN_PASSWORD": "EnvPwd!2026x"}):
            env_wins = resolve_create_spec(base, {})
            assert env_wins["admin_pass"] == "EnvPwd!2026x" and env_wins["key_name"] is None
        keyless = resolve_create_spec({"ecs_create": {"server": {
            "imageRef": "img", "flavorRef": "f", "vpcid": "v",
            "nics": [{"subnet_id": "s"}], "password": "ScopePwd!2026x"}}}, {})
        assert keyless["admin_pass"] == "ScopePwd!2026x"  # scope 密码兜底
    with _Env(**{"ECS_ADMIN_PASSWORD": "short"}):
        try:
            resolve_create_spec(base, {})
            raise AssertionError("env 密码不合规应 ValueError")
        except ValueError as exc:
            assert "密码" in str(exc)


async def test_resolve_create_spec_missing_required():
    with _Env():
        try:
            resolve_create_spec({"ecs_create": {"server": {"imageRef": "img"}}}, {})
            raise AssertionError("必填缺失应 EcsNotConfigured")
        except EcsNotConfigured as exc:
            assert "flavorRef" in str(exc) and "vpcid" in str(exc)


async def test_generated_password_compliant():
    for _ in range(20):
        pwd = generate_password()
        assert len(pwd) == 16
        validate_password(pwd)  # 四类覆盖 + 允许特殊字符集 + 无 root/toor


async def main():
    tests = [fn for name, fn in sorted(globals().items()) if name.startswith("test_")]
    for fn in tests:
        await fn()
        print(f"ok {fn.__name__}")
    print(f"{len(tests)} passed")


if __name__ == "__main__":
    asyncio.run(main())
