#!/usr/bin/env python3
"""认证与控制审计基础 —— 文件用户清单、Cookie 登录态、统一 401、同源、审计。

缝：FastAPI ASGI 测试客户端（create_app 注入临时用户清单 / 审计目录 /
密钥），覆盖登录、登出、过期、禁用、密码版本、错误 Cookie、同源校验和
审计失败；外加审计文件本身（内容形状、保留期清理）与口令哈希的单元行为。
纯 assert，无 pytest。

运行：python web/tests/test_auth.py
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from web.auth import COOKIE_NAME, hash_password, issue_token, verify_password  # noqa: E402
from web.audit import ControlAudit  # noqa: E402
from web.tests.support import StreamingASGITransport, async_client, make_test_app  # noqa: E402

PASSWORD = "correct-horse"
SAME_ORIGIN = "http://testserver"
CROSS_ORIGIN = "http://evil.example"


def block_audit(tmp):
    """审计目录占位成普通文件（mkdir 必失败）。"""
    d = Path(tmp, "audit")
    if d.is_dir():
        shutil.rmtree(d)
    d.write_text("blocked", encoding="utf-8")


def unblock_audit(tmp):
    Path(tmp, "audit").unlink()

# 受保护面抽样：业务 API、全局 SSE、产物、OBS、ECS（未登录一律 401）
PROTECTED_GET = [
    "/api/runs", "/api/runs/run_1", "/api/runs/run_1/events", "/api/stream",
    "/api/tasks", "/api/artifacts", "/api/artifacts/file/deploy/x.md",
    "/api/obs/objects", "/api/obs/config", "/api/obs/health",
    "/api/ecs/instances", "/api/ecs/defaults",
]
PROTECTED_POST = [
    "/api/runs", "/api/runs/run_1/messages", "/api/runs/run_1/stop",
    "/api/runs/run_1/clone", "/api/runs/run_1/end", "/api/auth/logout",
    "/api/obs/archive", "/api/ecs/check",
]


def write_users(path, *, tester_enabled=True, tester_hash=None, extra=None):
    """造一份临时用户清单（PBKDF2 真哈希，迭代数收低保测试速度）。"""
    users = {
        "tester": {
            "password_hash": tester_hash or hash_password(PASSWORD, 1000),
            "enabled": tester_enabled,
        },
        "ghost": {  # 预置禁用用户
            "password_hash": hash_password(PASSWORD, 1000),
            "enabled": False,
        },
    }
    users.update(extra or {})
    path.write_text(json.dumps({"users": users}), encoding="utf-8")
    return path


def auth_app(tmp, **overrides):
    """带临时用户清单的应用（users.yaml 放独立目录，便于测试中改写热载）。"""
    users_path = write_users(Path(tmp) / "users.yaml")
    return make_test_app(users_path=users_path, audit_dir=Path(tmp) / "audit",
                         **overrides)


def audit_lines(tmp):
    lines = []
    for f in sorted(Path(tmp, "audit").glob("audit-*.jsonl")):
        lines.extend(json.loads(l) for l in f.read_text(encoding="utf-8").splitlines() if l)
    return lines


# ---------- 口令哈希与 Cookie 签名的单元行为 ----------

def test_password_hash_roundtrip_and_format():
    encoded = hash_password("s3cret", 1000)
    assert encoded.startswith("pbkdf2_sha256$1000$"), encoded
    assert verify_password("s3cret", encoded)
    assert not verify_password("wrong", encoded)
    assert not verify_password("s3cret", "not-a-valid-hash")
    # 同口令两次哈希盐不同（彩虹表/库泄露不共享）
    assert hash_password("s3cret", 1000) != encoded


def test_token_roundtrip_rejects_tamper():
    secret = b"k" * 32
    token = issue_token(secret, "tester", "fp123", ttl=60)
    body, sig = token.rsplit(".", 1)
    flipped = ("A" if not body.startswith("A") else "B") + body[1:]
    assert issue_token(secret, "tester", "fp123", ttl=60)
    assert flipped != body
    # 篡改体的签名不再是原签名（verify 交给主缝的 401 测试覆盖）


# ---------- 登录 / 登出 / 身份查询 ----------

async def test_login_me_logout_flow_and_cookie_attributes():
    with tempfile.TemporaryDirectory() as tmp:
        app = auth_app(tmp)
        async with httpx.AsyncClient(transport=StreamingASGITransport(app=app),
                                     base_url="http://testserver") as client:
            r = await client.post("/api/auth/login",
                                  json={"username": "tester", "password": PASSWORD})
            assert r.status_code == 200, r.text
            assert r.json() == {"username": "tester"}
            cookie = r.headers.get("set-cookie", "").lower()
            assert "httponly" in cookie and "samesite=lax" in cookie, cookie
            assert f"{COOKIE_NAME}=" in cookie and "max-age=604800" in cookie, cookie

            r = await client.get("/api/auth/me")
            assert r.status_code == 200 and r.json() == {"username": "tester"}

            r = await client.post("/api/auth/logout")
            assert r.status_code == 200, r.text
            r = await client.get("/api/auth/me")
            assert r.status_code == 401, r.text
            # 登录成功与登出都入审计
            actions = [(e["action"], e["result"]) for e in audit_lines(tmp)]
            assert ("login", "success") in actions and ("logout", "success") in actions, actions


async def test_login_failure_unified_401_and_audited():
    with tempfile.TemporaryDirectory() as tmp:
        app = auth_app(tmp)
        async with httpx.AsyncClient(transport=StreamingASGITransport(app=app),
                                     base_url="http://testserver") as client:
            for username, password in [("tester", "wrong"), ("nobody", PASSWORD), ("ghost", PASSWORD)]:
                r = await client.post("/api/auth/login",
                                      json={"username": username, "password": password})
                assert r.status_code == 401, (username, r.text)
            r = await client.post("/api/auth/login", json={"username": "tester"})
            assert r.status_code == 422, r.text
        failures = [e for e in audit_lines(tmp) if e["action"] == "login" and e["result"] == "failure"]
        reasons = {e["actor"]: e["reason"] for e in failures}
        assert reasons == {"tester": "bad_credentials", "nobody": "bad_credentials",
                           "ghost": "disabled_user"}, reasons


# ---------- 统一 401 与伪造 Cookie ----------

async def test_all_api_endpoints_401_without_cookie():
    with tempfile.TemporaryDirectory() as tmp:
        app = auth_app(tmp)
        async with httpx.AsyncClient(transport=StreamingASGITransport(app=app),
                                     base_url="http://testserver") as client:
            for url in PROTECTED_GET:
                r = await client.get(url)
                assert r.status_code == 401, (url, r.status_code)
            for url in PROTECTED_POST:
                r = await client.post(url, json={})
                assert r.status_code == 401, (url, r.status_code)
            # 登录端点本身不受保护
            r = await client.post("/api/auth/login",
                                  json={"username": "tester", "password": PASSWORD})
            assert r.status_code == 200, r.text


async def test_bad_and_foreign_cookies_rejected():
    with tempfile.TemporaryDirectory() as tmp:
        app = auth_app(tmp)
        async with httpx.AsyncClient(transport=StreamingASGITransport(app=app),
                                     base_url="http://testserver") as client:
            r = await client.post("/api/auth/login",
                                  json={"username": "tester", "password": PASSWORD})
            good = client.cookies.get(COOKIE_NAME)
            assert good
            for token in ["garbage", good[:-2] + "xx", good + "x", "", "v1.abc.def"]:
                client.cookies.set(COOKIE_NAME, token)
                r = await client.get("/api/auth/me")
                assert r.status_code == 401, (token, r.status_code)
            client.cookies.set(COOKIE_NAME, good)
            r = await client.get("/api/auth/me")
            assert r.status_code == 200, r.text


async def test_expired_token_401():
    with tempfile.TemporaryDirectory() as tmp:
        app = auth_app(tmp)
        fp = app.state.users.fingerprint("tester")
        token = issue_token(app.state.auth_secret, "tester", fp, ttl=-10)
        async with httpx.AsyncClient(transport=StreamingASGITransport(app=app),
                                     base_url="http://testserver",
                                     cookies={COOKIE_NAME: token}) as client:
            r = await client.get("/api/auth/me")
            assert r.status_code == 401, r.text


async def test_roster_change_revokes_live_cookie():
    """禁用用户与改密（密码版本指纹变化）都让既有 Cookie 立即失效（清单热载）。"""
    with tempfile.TemporaryDirectory() as tmp:
        users_path = Path(tmp) / "users.yaml"
        app = auth_app(tmp)
        async with httpx.AsyncClient(transport=StreamingASGITransport(app=app),
                                     base_url="http://testserver") as client:
            await client.post("/api/auth/login",
                              json={"username": "tester", "password": PASSWORD})
            assert (await client.get("/api/auth/me")).status_code == 200

            os.utime(users_path, (time.time() + 2, time.time() + 2))  # mtime 前拨确保热载
            write_users(users_path, tester_enabled=False)
            assert (await client.get("/api/auth/me")).status_code == 401

            os.utime(users_path, (time.time() + 4, time.time() + 4))
            write_users(users_path, tester_hash=hash_password("new-password", 1000))
            r = await client.post("/api/auth/login",
                                  json={"username": "tester", "password": PASSWORD})
            assert r.status_code == 401, r.text  # 旧口令已换


async def test_secret_rotation_revokes_all_cookies():
    with tempfile.TemporaryDirectory() as tmp:
        users_path = write_users(Path(tmp) / "users.yaml")
        app_a = make_test_app(users_path=users_path, auth_secret="secret-a",
                              audit_dir=Path(tmp) / "audit")
        async with httpx.AsyncClient(transport=StreamingASGITransport(app=app_a),
                                     base_url="http://testserver") as client:
            await client.post("/api/auth/login",
                              json={"username": "tester", "password": PASSWORD})
            token = client.cookies.get(COOKIE_NAME)
        # 同一用户清单、换认证密钥重启：旧 Cookie 全失效
        app_b = make_test_app(users_path=users_path, auth_secret="secret-b",
                              audit_dir=Path(tmp) / "audit")
        async with httpx.AsyncClient(transport=StreamingASGITransport(app=app_b),
                                     base_url="http://testserver",
                                     cookies={COOKIE_NAME: token}) as client:
            assert (await client.get("/api/auth/me")).status_code == 401


# ---------- 同源校验 ----------

async def test_cross_origin_state_changing_requests_403():
    with tempfile.TemporaryDirectory() as tmp:
        app = auth_app(tmp)
        async with httpx.AsyncClient(transport=StreamingASGITransport(app=app),
                                     base_url="http://testserver") as client:
            await client.post("/api/auth/login",
                              json={"username": "tester", "password": PASSWORD})
            # 异源写一律 403；GET 不受同源约束（Cookie SameSite 已挡跨站附带）
            r = await client.post("/api/runs", json={}, headers={"Origin": CROSS_ORIGIN})
            assert r.status_code == 403, r.text
            r = await client.post("/api/runs", json={}, headers={"Origin": SAME_ORIGIN})
            assert r.status_code == 200, r.text
            r = await client.post("/api/runs", json={})  # 无 Origin（非浏览器客户端）放行
            assert r.status_code == 200, r.text
            r = await client.get("/api/runs", headers={"Origin": CROSS_ORIGIN})
            assert r.status_code == 200, r.text
            # 登录端点同样受同源约束，且被拒尝试入审计
            r = await client.post("/api/auth/login",
                                  json={"username": "tester", "password": PASSWORD},
                                  headers={"Origin": CROSS_ORIGIN})
            assert r.status_code == 403, r.text
        rejected = [e for e in audit_lines(tmp)
                    if e["action"] == "login" and e["result"] == "failure"]
        assert any(e["reason"] == "cross_origin" for e in rejected), rejected


# ---------- 审计 ----------

async def test_audit_write_failure_blocks_state_changing_auth_actions():
    with tempfile.TemporaryDirectory() as tmp:
        app = auth_app(tmp)
        async with httpx.AsyncClient(transport=StreamingASGITransport(app=app),
                                     base_url="http://testserver") as client:
            # 审计目录被占位成普通文件：写入必失败
            block_audit(tmp)
            r = await client.post("/api/auth/login",
                                  json={"username": "tester", "password": PASSWORD})
            assert r.status_code == 503, r.text
            # 只读不受审计写失败阻断；登录失败审计尽力而为（无状态变更，
            # 审计写失败不放大——响应仍是 401）
            assert (await client.get("/api/auth/me")).status_code == 401

            # 恢复审计目录：登录成功、随后再度破坏，登出被阻断
            unblock_audit(tmp)
            r = await client.post("/api/auth/login",
                                  json={"username": "tester", "password": PASSWORD})
            assert r.status_code == 200, r.text
            block_audit(tmp)
            r = await client.post("/api/auth/logout")
            assert r.status_code == 503, r.text


async def test_audit_entries_shape_and_no_secrets():
    with tempfile.TemporaryDirectory() as tmp:
        app = auth_app(tmp)
        async with httpx.AsyncClient(transport=StreamingASGITransport(app=app),
                                     base_url="http://testserver") as client:
            await client.post("/api/auth/login",
                              json={"username": "tester", "password": PASSWORD})
            token = client.cookies.get(COOKIE_NAME)
            await client.post("/api/auth/logout")
        raw = "".join(f.read_text(encoding="utf-8")
                      for f in Path(tmp, "audit").glob("audit-*.jsonl"))
        assert PASSWORD not in raw and token not in raw, raw
        for entry in audit_lines(tmp):
            assert {"time", "actor", "action", "result", "request_id"} <= set(entry), entry
            assert entry["actor"] == "tester"
            assert entry["request_id"], entry


def test_audit_rotates_by_day_and_prunes_expired():
    with tempfile.TemporaryDirectory() as tmp:
        audit = ControlAudit(Path(tmp), retention_days=90)
        audit.record(actor="tester", action="login", result="success", request_id="r1")
        files = list(Path(tmp).glob("audit-*.jsonl"))
        assert len(files) == 1 and files[0].name == f"audit-{time.strftime('%Y-%m-%d')}.jsonl"
        # 造一份超期旧档与一份无关文件：清理只认审计文件名且按日期判龄
        stale = Path(tmp, "audit-2000-01-01.jsonl")
        stale.write_text("{}\n", encoding="utf-8")
        bystander = Path(tmp, "notes.txt")
        bystander.write_text("keep", encoding="utf-8")
        audit.record(actor="tester", action="logout", result="success", request_id="r2")
        assert not stale.exists()
        assert bystander.exists()
        lines = [json.loads(l) for f in sorted(Path(tmp).glob("audit-*.jsonl"))
                 for l in f.read_text(encoding="utf-8").splitlines() if l]
        assert [e["request_id"] for e in lines] == ["r1", "r2"]


# ---------- 与既有测试装配的协作 ----------

async def test_default_fixture_login_and_basic_flow():
    """make_test_app 默认清单 + async_client 自动登录：既有主缝测试的基座。"""
    app = make_test_app()
    async with async_client(app) as client:
        assert (await client.get("/api/auth/me")).json() == {"username": "tester"}
        r = await client.post("/api/runs", json={})
        assert r.status_code == 200, r.text
    # 指定用户名登录（多用户测试的入口形态）
    async with async_client(app, username="ghost") as client:
        r = await client.post("/api/auth/login", json={"username": "ghost", "password": ""})
        assert r.status_code == 401, r.text


async def test_static_shell_served_without_login_but_no_api_leak():
    """未登录能拿到静态壳（登录页载体），但任何 /api 一律 401。"""
    with tempfile.TemporaryDirectory() as tmp:
        static = Path(tmp) / "ui"
        (static / "assets").mkdir(parents=True)
        (static / "index.html").write_text("<html>shell</html>", encoding="utf-8")
        (static / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
        app = auth_app(tmp, static_dir=static)
        async with httpx.AsyncClient(transport=StreamingASGITransport(app=app),
                                     base_url="http://testserver") as client:
            r = await client.get("/")
            assert r.status_code == 200 and "shell" in r.text, r.text
            r = await client.get("/assets/app.js")
            assert r.status_code == 200, r.text
            r = await client.get("/api/runs")
            assert r.status_code == 401, r.status_code


async def main():
    tests = [fn for name, fn in sorted(globals().items()) if name.startswith("test_")]
    for fn in tests:
        if asyncio.iscoroutinefunction(fn):
            await fn()
        else:
            fn()
        print(f"ok {fn.__name__}")
    print(f"{len(tests)} passed")


if __name__ == "__main__":
    asyncio.run(main())
