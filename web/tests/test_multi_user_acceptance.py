#!/usr/bin/env python3
"""多用户集成验收 —— 多认证客户端一次走完全部隔离承诺。

缝：FastAPI ASGI 测试客户端 ×3（alice / bob / admin 打同一 app）。主线
验收：会话控制面隔离（各自创建运行、列表互不可见、单 run 统一 404）、
全局流逐帧 owner 过滤与匿名容量、产物与 OBS 共享读取（OBS 配置仅部署
管理员可写）、停止/Fork/结束与越权拒绝无跨 owner 影响、审计完整控制
链路（时序递增、拒绝带原因、敏感值零泄露）；另测重启后 owner/run_id/
墓碑/Fork 来源保留与禁用-恢复的登录撤销语义。纯 assert，无 pytest。

运行：python web/tests/test_multi_user_acceptance.py
"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from web.app import REASON_NOT_ADMIN, REASON_NOT_OWNER  # noqa: E402
from web.auth import COOKIE_NAME  # noqa: E402
from web.fake import DEFAULT_SCRIPT, FakeSessionFactory  # noqa: E402
from web.tests.support import (  # noqa: E402
    TEST_PASSWORD, audit_lines, async_client, make_test_app, write_test_users,
)
from web.tests.test_stream_isolation import (  # noqa: E402
    collect_frames, open_global_stream, wait_status,
)

REL = "deploy/nginx/1.25/result.md"
ARTIFACT_MARK = "ACCEPTANCE-ARTIFACT"
OBS_MARK = "ACCEPTANCE-OBS-BODY"
SCOPE_AK = "ACCEPTAKEXAMPLE0000000000"
SCOPE_SK = "ACCEPTSKEXAMPLE000000000000000000"


def acceptance_app(tmp):
    """三用户验收应用：产物根与 OBS 全走本地假实现（不触云），审计与簿记
    落临时目录，部署管理员为 admin（与生产 WEB_DEFAULT_OWNER 同源）。"""
    root = Path(tmp)
    out = root / "art" / "deploy" / "nginx" / "1.25"
    out.mkdir(parents=True, exist_ok=True)
    (out / "result.md").write_text(f"# 验收产物\n{ARTIFACT_MARK}\n", encoding="utf-8")
    return make_test_app(
        session_factory=FakeSessionFactory(script=DEFAULT_SCRIPT, delay=0.05),
        artifact_roots={"deploy": root / "art" / "deploy"},
        obs_list_fn=lambda limit=1000: {
            "bucket": "test-image-gen", "region": "ap-southeast-1",
            "domain": "https://test-image-gen.obs.ap-southeast-1.myhuaweicloud.com",
            "count": 1, "truncated": False,
            "objects": [
                {"key": "deploy/nginx/result.md", "size": 5, "etag": '"abc"',
                 "last_modified": "2026/09/22 10:00:00", "storage_class": None},
            ],
        },
        obs_url_fn=lambda key="", expires=3600: {
            "key": key, "signed_url": f"https://s/{key}", "expires_in": expires},
        obs_read_fn=lambda key="": {
            "dir": "deploy", "name": "result.md", "size": len(OBS_MARK),
            "content": OBS_MARK},
        obs_archive_fn=lambda items: {
            "archived": [{"key": k, "size": 1, "url": f"https://d/{k}"} for k, _p in items],
            "failed": [], "ok": True, "count": len(items)},
        obs_health_fn=lambda: {"ok": True, "bucket": "test-image-gen",
                               "region": "ap-southeast-1", "latency_ms": 3},
        audit_dir=root / "audit",
        default_owner="admin",
    )


def write_scope(app):
    """测试 scope：顶层凭据（值为审计脱敏断言素材）+ obs 段（配置写入目标）。"""
    data = {"ak": SCOPE_AK, "sk": SCOPE_SK, "region": "ap-southeast-1",
            "obs": {"bucket": "old-bucket"}}
    (app.state.test_root / "scope.yaml").write_text(
        yaml.safe_dump(data), encoding="utf-8")


def chain_positions(entries, steps):
    """控制链路时序断言：每步（actor, action, run_id）都有成功审计，且整链
    位置严格递增——审计能按发生顺序重建控制链。"""
    positions = []
    for actor, action, run_id in steps:
        hits = [i for i, e in enumerate(entries)
                if e["actor"] == actor and e["action"] == action
                and e["result"] == "success" and e.get("run_id") == run_id]
        assert hits, (actor, action, run_id)
        positions.append(hits)
    for earlier, later in zip(positions, positions[1:]):
        assert min(later) > max(earlier), steps
    return positions


async def wait_ready(client, run_id, timeout_s=8.0):
    return await wait_status(client, run_id, "READY", timeout_s=timeout_s)


async def test_multi_user_acceptance_end_to_end():
    """验收主线：隔离、流过滤与容量、共享产物/OBS、控制无跨 owner 影响、
    审计链路与脱敏，一次完整走通。"""
    with tempfile.TemporaryDirectory() as tmp:
        app = acceptance_app(tmp)
        write_scope(app)
        async with async_client(app, username="alice") as alice, \
                async_client(app, username="bob") as bob, \
                async_client(app, username="admin") as admin:
            tokens = [c.cookies.get(COOKIE_NAME) for c in (alice, bob, admin)]
            assert all(tokens), tokens

            # -- 会话控制面隔离：各自创建并运行，列表互不可见 --
            a_run = (await alice.post("/api/runs", json={})).json()["run_id"]
            b_run = (await bob.post("/api/runs", json={})).json()["run_id"]
            a_prompt = "alice 部署 nginx 1.25"
            b_prompt = "bob 部署 redis 7"
            resp_a = await open_global_stream(alice)
            resp_b = await open_global_stream(bob)
            await bob.post(f"/api/runs/{b_run}/messages", json={"text": b_prompt})
            await alice.post(f"/api/runs/{a_run}/messages", json={"text": a_prompt})

            # 容量匿名且全局：双方回合并行时 running_count=2（跨用户合计），
            # bob 的响应不带 alice 的 run id、指令或用户名
            body = (await bob.get("/api/runs")).json()
            raw = json.dumps(body, ensure_ascii=False)
            assert body["running_count"] == 2, body
            assert isinstance(body["max_parallel"], int) and body["max_parallel"] >= 2
            assert {r["run_id"] for r in body["runs"]} == {b_run}, body
            assert a_run not in raw and a_prompt not in raw and "alice" not in raw, raw

            # -- 全局流逐帧 owner 过滤：各收各的完整回合，他人事件一帧不进流 --
            frames_a, _ = await collect_frames(
                resp_a, stop=lambda e: e["data"]["type"] == "turn.completed")
            frames_b, _ = await collect_frames(
                resp_b, stop=lambda e: e["data"]["type"] == "turn.completed")
            await resp_a.aclose()
            await resp_b.aclose()
            assert {f["data"]["run_id"] for f in frames_a} == {a_run}
            assert {f["data"]["run_id"] for f in frames_b} == {b_run}
            texts_a = [f["data"]["payload"].get("text") for f in frames_a
                       if f["data"]["type"] == "user.message"]
            assert texts_a == [a_prompt], texts_a
            await wait_status(alice, a_run, "READY", timeout_s=8.0)
            await wait_status(bob, b_run, "READY", timeout_s=8.0)

            # 他人 run 与未知 run 同一 404；admin 也不是会话越权角色
            r = await alice.get(f"/api/runs/{b_run}")
            assert r.status_code == (await alice.get("/api/runs/run_999")).status_code == 404
            assert (await admin.get("/api/runs")).json()["runs"] == []

            # -- 共享产物与 OBS：双用户同样可读，配置仅部署管理员可写 --
            for client in (alice, bob):
                r = await client.get("/api/artifacts")
                assert r.status_code == 200, r.text
                names = [f["name"] for g in r.json()["groups"] for f in g["files"]]
                assert "result.md" in names, names
                r = await client.get(f"/api/artifacts/file/{REL}")
                assert r.status_code == 200 and ARTIFACT_MARK in r.json()["content"]
                r = await client.get("/api/obs/objects")
                assert r.status_code == 200 and r.json()["count"] == 1, r.text
                r = await client.get("/api/obs/content?key=deploy/nginx/result.md")
                assert r.status_code == 200 and r.json()["content"] == OBS_MARK, r.text
                r = await client.get("/api/obs/config")
                assert r.status_code == 200 and r.json()["can_write"] is False, r.text
            r = await bob.post("/api/obs/archive", json={"paths": [REL]})
            assert r.status_code == 200 and r.json()["ok"] is True, r.text
            r = await bob.post("/api/obs/config", json={"bucket": "hacked"})
            assert r.status_code == 403, r.text
            r = await admin.post("/api/obs/config", json={"bucket": "accept-bucket"})
            assert r.status_code == 200 and r.json()["saved"] is True, r.text
            scope = yaml.safe_load(
                (app.state.test_root / "scope.yaml").read_text(encoding="utf-8"))
            assert scope["obs"]["bucket"] == "accept-bucket", scope

            # -- 停止/Fork/结束与拒绝：多用户下无跨 owner 影响 --
            await alice.post(f"/api/runs/{a_run}/messages", json={"text": "alice 第二回合"})
            await wait_status(alice, a_run, "RUNNING")
            for path, payload in (
                (f"/api/runs/{a_run}", None),
                (f"/api/runs/{a_run}/events", None),
                (f"/api/runs/{a_run}/messages", {"text": "越权发送"}),
                (f"/api/runs/{a_run}/stop", {}),
                (f"/api/runs/{a_run}/clone", {}),
                (f"/api/runs/{a_run}/end", {}),
            ):
                if payload is None:
                    r = await bob.get(path)
                else:
                    r = await bob.post(path, json=payload)
                assert r.status_code == 404, (path, r.status_code)
            assert (await alice.get(f"/api/runs/{a_run}")).json()["status"] == "RUNNING"

            r = await alice.post(f"/api/runs/{a_run}/stop")
            assert r.status_code == 200, r.text
            await wait_ready(alice, a_run)
            ev = await alice.get(f"/api/runs/{a_run}/events")
            assert ev.status_code == 200 and "turn.stopped" in ev.text, ev.text

            fork = (await alice.post(f"/api/runs/{a_run}/clone", json={})).json()["run_id"]
            assert fork in {x["run_id"] for x in (await alice.get("/api/runs")).json()["runs"]}
            assert fork not in {x["run_id"] for x in (await bob.get("/api/runs")).json()["runs"]}
            r = await bob.post(f"/api/runs/{fork}/end")
            assert r.status_code == 404, r.text
            await alice.post(f"/api/runs/{fork}/messages", json={"text": "分支续聊"})
            await wait_ready(alice, fork)
            r = await alice.post(f"/api/runs/{fork}/end")
            assert r.status_code == 200, r.text
            assert (await alice.get(f"/api/runs/{fork}")).json()["status"] == "ENDED"

            # bob 的会话全程只受 bob 影响：READY、指令只有自己的
            b_sum = (await bob.get(f"/api/runs/{b_run}")).json()
            assert b_sum["status"] == "READY" and b_sum["first_prompt"] == b_prompt, b_sum

        # -- 审计：完整控制链路 + 拒绝入册 + 敏感值零泄露 --
        entries = audit_lines(Path(tmp) / "audit")
        chain = chain_positions(entries, [
            ("alice", "login", None),
            ("alice", "create_run", a_run),
            ("alice", "send", a_run),
            ("alice", "stop", a_run),
            ("alice", "clone", a_run),
            ("alice", "end", fork),
        ])
        for hits in chain[1:]:
            assert all(entries[i]["run_owner"] == "alice" and entries[i]["request_id"]
                       for i in hits), entries
        # 共享资源动作与 admin 链路同样在册
        assert any(e["actor"] == "bob" and e["action"] == "obs_archive"
                   and e["result"] == "success" for e in entries), entries
        assert any(e["actor"] == "admin" and e["action"] == "obs_config"
                   and e["result"] == "success" for e in entries), entries
        # 越权拒绝全部入册：带 actor/run owner 与拒绝原因；配置写拒绝同册
        denied = [e for e in entries if e["result"] == "denied"]
        cross = [e for e in denied if e.get("run_owner") == "alice"]
        assert {e["action"] for e in cross} >= {"send", "stop", "clone", "end"}, denied
        assert all(e["actor"] == "bob" and e["reason"] == REASON_NOT_OWNER
                   for e in cross), cross
        assert any(e["actor"] == "bob" and e["action"] == "obs_config"
                   and e["reason"] == REASON_NOT_ADMIN for e in denied), denied
        # 发送只记长度与摘要，不落指令原文
        sends = [e for e in entries if e["action"] == "send" and e["result"] == "success"]
        assert sends and all(e["meta"].startswith("len:") for e in sends), sends
        # 敏感值不入审计：口令、Cookie、AK/SK、指令原文、产物与 OBS 内容
        audit_raw = "".join(f.read_text(encoding="utf-8")
                            for f in (Path(tmp) / "audit").glob("audit-*.jsonl"))
        for secret in (TEST_PASSWORD, *tokens, SCOPE_AK, SCOPE_SK,
                       a_prompt, b_prompt, ARTIFACT_MARK, OBS_MARK):
            assert secret not in audit_raw, secret


async def test_restart_keeps_owner_tombstone_fork_and_revocation():
    """重启验收：owner/run_id/墓碑/Fork 来源随簿记恢复，禁用即时撤销登录、
    重新启用恢复可见。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        state_path = root / "state.json"
        users_path = write_test_users(root / "users.yaml")
        factory = FakeSessionFactory(script=DEFAULT_SCRIPT, delay=0.02)
        app_a = make_test_app(session_factory=factory, state_path=state_path,
                              users_path=users_path, audit_dir=root / "audit",
                              default_owner="admin")
        async with async_client(app_a, username="alice") as alice:
            a_run = (await alice.post("/api/runs", json={})).json()["run_id"]
            await alice.post(f"/api/runs/{a_run}/messages", json={"text": "alice 的部署"})
            await wait_ready(alice, a_run)
            fork = (await alice.post(f"/api/runs/{a_run}/clone", json={})).json()["run_id"]
            await alice.post(f"/api/runs/{fork}/messages", json={"text": "分支续聊"})
            await wait_ready(alice, fork)
            assert (await alice.post(f"/api/runs/{fork}/end")).status_code == 200
        async with async_client(app_a, username="bob") as bob:
            b_run = (await bob.post("/api/runs", json={})).json()["run_id"]
            await bob.post(f"/api/runs/{b_run}/messages", json={"text": "bob 的部署"})
            await wait_ready(bob, b_run)

        # 重启：同一 transcript 与簿记（同一工厂跨实例保留 transcript）
        app_b = make_test_app(session_factory=factory, state_path=state_path,
                              users_path=users_path, audit_dir=root / "audit2",
                              list_sessions_fn=factory.list_sessions,
                              get_session_messages_fn=factory.get_session_messages,
                              default_owner="admin")
        async with async_client(app_b, username="alice") as alice, \
                async_client(app_b, username="bob") as bob:
            a_list = {r["run_id"]: r for r in (await alice.get("/api/runs")).json()["runs"]}
            assert set(a_list) == {a_run, fork}, a_list
            assert a_list[a_run]["status"] == "READY"
            assert a_list[fork]["status"] == "ENDED"        # 墓碑不复活
            assert a_list[fork]["resumed_from"] == a_run    # Fork 来源保留
            assert {r["run_id"] for r in (await bob.get("/api/runs")).json()["runs"]} == {b_run}
            assert (await bob.get(f"/api/runs/{a_run}")).status_code == 404
            # 恢复后仍按 owner 授权：owner 可续聊，终态不可发
            r = await alice.post(f"/api/runs/{a_run}/messages", json={"text": "重启后续写"})
            assert r.status_code == 200, r.text
            r = await alice.post(f"/api/runs/{fork}/messages", json={"text": "不该成功"})
            assert r.status_code == 409 and r.json()["detail"] == "session_not_active"
            await wait_ready(alice, a_run)

            # 管理启停永久撤销旧登录，重新登录仍可见原会话。
            async with async_client(app_b, username="admin") as admin:
                for action in ('disable', 'enable'):
                    rows = (await admin.get('/api/admin/users')).json()['users']
                    target = next(row for row in rows if row['username'] == 'alice')
                    response = await admin.post(f'/api/admin/users/{action}', json={
                        'username': 'alice', 'expected_version': target['user_version']})
                    assert response.status_code == 200
                    assert (await alice.get('/api/runs')).status_code == 401
            assert (await alice.get("/api/runs")).status_code == 401
            alice.cookies.clear()
            assert (await alice.post('/api/auth/login', json={'username': 'alice', 'password': TEST_PASSWORD})).status_code == 200
            assert {r['run_id'] for r in (await alice.get('/api/runs')).json()['runs']} == {a_run, fork}


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
