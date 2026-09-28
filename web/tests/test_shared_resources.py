#!/usr/bin/env python3
"""共享产物与 OBS 权限 —— 多用户共享读取、归档共享、OBS 配置仅管理员可写。

缝：FastAPI ASGI 测试客户端 ×2（alice/bob 普通用户）+ admin 客户端，产物
根与 OBS 函数注入假实现（不触云）；admin 通过测试清单的显式 role: admin 获得管理权限。覆盖：双用户产物四端点与 OBS 三端点共享
读取、脱敏配置与健康状态共享、共享归档（不按 owner 过滤）、普通用户配置
写入 403（请求体伪造无效）、admin 配置写入、归档与配置修改审计前置（写
失败 503 不执行共享资源写）。纯 assert，无 pytest。

运行：python web/tests/test_shared_resources.py
"""
import asyncio
import io
import os
import sys
import tempfile
import zipfile
from pathlib import Path

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from web.app import REASON_NOT_ADMIN  # noqa: E402
from web.tests.support import (  # noqa: E402
    audit_lines, async_client, block_audit, make_test_app, unblock_audit,
)

FIXTURE_LIST = {
    "bucket": "test-image-gen", "region": "ap-southeast-1",
    "domain": "https://test-image-gen.obs.ap-southeast-1.myhuaweicloud.com",
    "count": 1, "truncated": False,
    "objects": [
        {"key": "deploy/nginx/result.md", "size": 5, "etag": '"abc"',
         "last_modified": "2026/09/09 15:35:07", "storage_class": None},
    ],
}
FIXTURE_HEALTHY = {
    "ok": True, "bucket": "test-image-gen", "region": "ap-southeast-1",
    "latency_ms": 7,
}
REL = "deploy/nginx/1.25/result.md"


def shared_app(tmp, **overrides):
    """admin 为部署管理员的三用户共享资源应用（产物根指向临时目录）。"""
    root = Path(tmp)
    out = root / "art" / "deploy" / "nginx" / "1.25"
    out.mkdir(parents=True, exist_ok=True)
    (out / "result.md").write_text("# 结果\n", encoding="utf-8")
    (out / "pkg.rpm").write_bytes(b"PKFAKE" * 8)
    options = {
        "artifact_roots": {"deploy": root / "art" / "deploy"},
        "audit_dir": Path(tmp) / "audit",
        "default_owner": "admin",  # 仅配置历史会话迁移归属
    }
    options.update(overrides)
    return make_test_app(**options)


def write_scope(app, obs=None):
    """测试 scope：顶层 ECS 凭据 + 可选 obs 段（配置写入测试的落盘目标）。"""
    data = {"ak": "TOPLEVELAKXXXXX", "sk": "TOPLEVELSKXXXXXXXXXXXXXXXXXXXX",
            "region": "ap-southeast-1"}
    if obs is not None:
        data["obs"] = obs
    (app.state.test_root / "scope.yaml").write_text(
        yaml.safe_dump(data), encoding="utf-8")


def audit_dir_of(tmp):
    return Path(tmp) / "audit"


async def test_artifact_reads_shared_by_two_users():
    """产物清单、文本内容、二进制下载与批量 zip 对两个登录用户同样可用。"""
    with tempfile.TemporaryDirectory() as tmp:
        app = shared_app(tmp)
        async with async_client(app, username="alice") as alice, \
                async_client(app, username="bob") as bob:
            for client in (alice, bob):
                r = await client.get("/api/artifacts")
                assert r.status_code == 200, r.text
                files = [f["name"] for g in r.json()["groups"] for f in g["files"]]
                assert "result.md" in files and "pkg.rpm" in files, files
                r = await client.get(f"/api/artifacts/file/{REL}")
                assert r.status_code == 200 and r.json()["content"] == "# 结果\n", r.text
                r = await client.get("/api/artifacts/download/deploy/nginx/1.25/pkg.rpm")
                assert r.status_code == 200 and r.content[:2] == b"PK", r.text
                r = await client.post("/api/artifacts/zip", json={"paths": [REL]})
                assert r.status_code == 200, r.text
                assert "result.md" in zipfile.ZipFile(io.BytesIO(r.content)).namelist()


async def test_obs_reads_and_config_view_shared_by_two_users():
    """OBS 清单、签名链接、文本预览、脱敏配置与健康状态对所有登录用户共享。"""
    with tempfile.TemporaryDirectory() as tmp:
        app = shared_app(
            tmp,
            obs_list_fn=lambda limit=1000: FIXTURE_LIST,
            obs_url_fn=lambda key="", expires=3600: {
                "key": key, "signed_url": f"https://s/{key}", "expires_in": expires},
            obs_read_fn=lambda key="": {
                "dir": "deploy", "name": "result.md", "size": 3, "content": "abc"},
            obs_health_fn=lambda: FIXTURE_HEALTHY,
        )
        async with async_client(app, username="alice") as alice, \
                async_client(app, username="bob") as bob:
            for client in (alice, bob):
                r = await client.get("/api/obs/objects")
                assert r.status_code == 200 and r.json()["count"] == 1, r.text
                r = await client.get("/api/obs/url?key=deploy/nginx/result.md")
                assert r.status_code == 200 and r.json()["signed_url"], r.text
                r = await client.get("/api/obs/content?key=deploy/nginx/result.md")
                assert r.status_code == 200 and r.json()["content"] == "abc", r.text
                r = await client.get("/api/obs/config")
                assert r.status_code == 200, r.text
                assert r.json()["can_write"] is False, r.json()  # 普通用户只读
                r = await client.get("/api/obs/health")
                assert r.status_code == 200 and r.json()["ok"] is True, r.text


async def test_archive_shared_by_any_user_and_audited():
    """本地产物归档与 zip 归档对所有登录用户开放（不按 owner 过滤），动作入审计。"""
    with tempfile.TemporaryDirectory() as tmp:
        calls = []

        def fake_archive(items):
            calls.append(("files", tuple(k for k, _p in items)))
            return {"archived": [{"key": k, "size": 1, "url": f"https://d/{k}"} for k, _p in items],
                    "failed": [], "ok": True, "count": len(items)}

        def fake_zip(key, data):
            calls.append(("zip", key))
            return {"key": key, "size": len(data), "url": f"https://d/{key}"}

        app = shared_app(tmp, obs_archive_fn=fake_archive, obs_upload_zip_fn=fake_zip)
        async with async_client(app, username="alice") as alice, \
                async_client(app, username="bob") as bob:
            r = await alice.post("/api/obs/archive", json={"paths": [REL]})
            assert r.status_code == 200 and r.json()["ok"] is True, r.text
            r = await bob.post("/api/obs/archive-zip", json={"paths": [REL], "name": "bob-bundle"})
            assert r.status_code == 200 and r.json()["key"] == "zip/bob-bundle.zip", r.text
        assert ("files", (REL,)) in calls and ("zip", "zip/bob-bundle.zip") in calls, calls
        entries = audit_lines(audit_dir_of(tmp))
        assert any(e["actor"] == "alice" and e["action"] == "obs_archive"
                   and e["result"] == "success" for e in entries), entries
        assert any(e["actor"] == "bob" and e["action"] == "obs_archive_zip"
                   and e["result"] == "success" for e in entries), entries


async def test_non_admin_config_write_denied_403():
    """普通用户改 OBS 配置：明确 403，请求体伪造管理身份无效，落盘不动、审计拒绝。"""
    with tempfile.TemporaryDirectory() as tmp:
        app = shared_app(tmp)
        write_scope(app, obs={"bucket": "old-bucket"})
        async with async_client(app, username="alice") as alice:
            r = await alice.post("/api/obs/config", json={
                "bucket": "hacked", "admin": True, "actor": "admin", "owner": "admin",
            })
            assert r.status_code == 403, r.text
            assert "admin" in r.json()["detail"], r.text
            # 落盘不动：桶名未被改写
            scope = yaml.safe_load((app.state.test_root / "scope.yaml").read_text(encoding="utf-8"))
            assert scope["obs"]["bucket"] == "old-bucket", scope
        entries = [e for e in audit_lines(audit_dir_of(tmp))
                   if e["action"] == "obs_config" and e["actor"] == "alice"]
        assert entries and entries[0]["result"] == "denied", entries
        assert entries[0]["reason"] == REASON_NOT_ADMIN, entries[0]


async def test_admin_config_write_succeeds_and_audited():
    """部署管理员改 OBS 配置：200 落盘、审计成功，视图标 can_write。"""
    with tempfile.TemporaryDirectory() as tmp:
        app = shared_app(tmp)
        write_scope(app, obs={"bucket": "old-bucket"})
        async with async_client(app, username="admin") as admin:
            r = await admin.get("/api/obs/config")
            assert r.json()["can_write"] is True, r.text
            r = await admin.post("/api/obs/config", json={"bucket": "new-bucket"})
            assert r.status_code == 200 and r.json()["saved"] is True, r.text
        scope = yaml.safe_load((app.state.test_root / "scope.yaml").read_text(encoding="utf-8"))
        assert scope["obs"]["bucket"] == "new-bucket", scope
        assert any(e["actor"] == "admin" and e["action"] == "obs_config"
                   and e["result"] == "success" for e in audit_lines(audit_dir_of(tmp)))


async def test_audit_failure_blocks_shared_writes():
    """审计写失败：归档、zip 归档与配置修改全部 503 且不执行；恢复后可用。"""
    with tempfile.TemporaryDirectory() as tmp:
        calls = []

        def fake_archive(items):
            calls.append("archive")
            return {"archived": [], "failed": [], "ok": True, "count": 0}

        def fake_zip(key, data):
            calls.append("zip")
            return {"key": key, "size": 0}

        app = shared_app(tmp, obs_archive_fn=fake_archive, obs_upload_zip_fn=fake_zip)
        write_scope(app, obs={"bucket": "old-bucket"})
        async with async_client(app, username="alice") as alice, \
                async_client(app, username="admin") as admin:
            block_audit(audit_dir_of(tmp))
            r = await alice.post("/api/obs/archive", json={"paths": [REL]})
            assert r.status_code == 503, r.text
            r = await alice.post("/api/obs/archive-zip", json={"paths": [REL], "name": "x"})
            assert r.status_code == 503, r.text
            r = await admin.post("/api/obs/config", json={"bucket": "blocked-bucket"})
            assert r.status_code == 503, r.text
            assert calls == [], calls  # 一个共享资源写都没执行
            scope = yaml.safe_load((app.state.test_root / "scope.yaml").read_text(encoding="utf-8"))
            assert scope["obs"]["bucket"] == "old-bucket", scope
            # 读取不受审计阻断
            assert (await alice.get("/api/obs/config")).status_code == 200

            unblock_audit(audit_dir_of(tmp))
            r = await alice.post("/api/obs/archive", json={"paths": [REL]})
            assert r.status_code == 200, r.text
            assert calls == ["archive"], calls


async def main():
    tests = [fn for name, fn in sorted(globals().items()) if name.startswith("test_")]
    for fn in tests:
        await fn()
        print(f"ok {fn.__name__}")
    print(f"{len(tests)} passed")


if __name__ == "__main__":
    asyncio.run(main())
