#!/usr/bin/env python3
"""Owner 会话生命周期隔离 —— 多用户控制面 ACL 的主缝测试。

缝：FastAPI ASGI 测试客户端 ×2（alice / bob 两个认证客户端打同一个
app），覆盖列表过滤、单 run 404 不泄露、控制动作 owner 校验、Fork owner
继承、请求体 owner 忽略、审计（成功/拒绝/503 阻断）与重启后 owner 保留。
纯 assert，无 pytest。

运行：python web/tests/test_owner_acl.py
"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from web.fake import DEFAULT_SCRIPT, FakeSessionFactory  # noqa: E402
from web.tests.support import (  # noqa: E402
    StreamingASGITransport, TEST_PASSWORD, audit_lines, async_client,
    block_audit, make_test_app, unblock_audit, write_test_users,
)

PASSWORD = TEST_PASSWORD


def owner_app(tmp, factory=None):
    """alice/bob/admin 三用户应用（审计目录与用户清单可热改）。"""
    users_path = write_test_users(Path(tmp) / "users.yaml")
    return make_test_app(
        session_factory=factory or FakeSessionFactory(script=DEFAULT_SCRIPT, delay=0.02),
        users_path=users_path,
        audit_dir=Path(tmp) / "audit",
    )


def audit_dir_of(tmp):
    return Path(tmp) / "audit"


async def create_run_as(client):
    return (await client.post("/api/runs", json={"owner": "mallory"})).json()["run_id"]
    # 请求体 owner 字段被忽略：归属由服务端登录身份注入


async def wait_ready(client, run_id, timeout_s=5.0):
    deadline = asyncio.get_event_loop().time() + timeout_s
    last = None
    while asyncio.get_event_loop().time() < deadline:
        last = (await client.get(f"/api/runs/{run_id}")).json()
        if last["status"] == "READY":
            return last
        await asyncio.sleep(0.01)
    raise AssertionError(f"run {run_id} 未回 READY，最后状态 {last}")


async def test_two_clients_cannot_see_or_control_each_others_runs():
    """验收主线：用户 A 看不到也控制不了用户 B 的会话。"""
    with tempfile.TemporaryDirectory() as tmp:
        app = owner_app(tmp)
        async with async_client(app, username="alice") as alice, \
                async_client(app, username="bob") as bob:
            a_run = await create_run_as(alice)
            b_run = await create_run_as(bob)

            # 列表各自只见自己的
            a_list = {r["run_id"] for r in (await alice.get("/api/runs")).json()["runs"]}
            b_list = {r["run_id"] for r in (await bob.get("/api/runs")).json()["runs"]}
            assert a_list == {a_run}, a_list
            assert b_list == {b_run}, b_list

            # 单 run 摘要 / 事件快照：他人 run 与未知 run 同一 404（不泄露存在性）
            for path in ("", "/events"):
                r = await alice.get(f"/api/runs/{b_run}{path}")
                unknown = await alice.get(f"/api/runs/run_999{path}")
                assert r.status_code == unknown.status_code == 404, (path, r.status_code)

            # 控制动作全部 404：发送、停止、Fork、结束
            r = await alice.post(f"/api/runs/{b_run}/messages", json={"text": "越权发送"})
            assert r.status_code == 404, r.text
            r = await alice.post(f"/api/runs/{b_run}/stop")
            assert r.status_code == 404, r.text
            r = await alice.post(f"/api/runs/{b_run}/clone")
            assert r.status_code == 404, r.text
            r = await alice.post(f"/api/runs/{b_run}/end")
            assert r.status_code == 404, r.text

            # 越权动作没有生效：bob 的会话仍 READY、无指令
            summary = (await bob.get(f"/api/runs/{b_run}")).json()
            assert summary["status"] == "READY" and summary["first_prompt"] is None


async def test_owner_controls_own_lifecycle_and_fork_inherits_owner():
    """owner 匹配时既有交互全可用；Fork 继承源 owner。"""
    with tempfile.TemporaryDirectory() as tmp:
        factory = FakeSessionFactory(script=DEFAULT_SCRIPT, delay=0.02)
        app = owner_app(tmp, factory)
        async with async_client(app, username="alice") as alice, \
                async_client(app, username="bob") as bob:
            src = await create_run_as(alice)
            # 发送 → RUNNING → 停止 → READY（状态机语义不变）
            await alice.post(f"/api/runs/{src}/messages", json={"text": "部署 nginx 1.25"})
            running = (await alice.get(f"/api/runs/{src}")).json()
            assert running["status"] == "RUNNING", running
            r = await alice.post(f"/api/runs/{src}/stop")
            assert r.status_code == 200, r.text
            await wait_ready(alice, src)

            # Fork：新会话继承 alice 归属（请求体 owner 不起作用）
            r = await alice.post(f"/api/runs/{src}/clone", json={})
            assert r.status_code == 200, r.text
            fork = r.json()["run_id"]
            assert fork in {x["run_id"] for x in (await alice.get("/api/runs")).json()["runs"]}
            assert fork not in {x["run_id"] for x in (await bob.get("/api/runs")).json()["runs"]}

            # Fork 会话归 alice：bob 对它同样 404
            r = await bob.post(f"/api/runs/{fork}/messages", json={"text": "抢答"})
            assert r.status_code == 404, r.text

            # alice 续聊 Fork 后结束自己的会话
            await alice.post(f"/api/runs/{fork}/messages", json={"text": "继续部署"})
            await wait_ready(alice, fork)
            r = await alice.post(f"/api/runs/{fork}/end")
            assert r.status_code == 200, r.text
            assert (await alice.get(f"/api/runs/{fork}")).json()["status"] == "ENDED"


async def test_admin_is_not_a_session_superuser():
    """admin 不是会话越权角色：他人会话对它同样 404。"""
    with tempfile.TemporaryDirectory() as tmp:
        app = owner_app(tmp)
        async with async_client(app, username="bob") as bob, \
                async_client(app, username="admin") as admin:
            b_run = await create_run_as(bob)
            r = await admin.get(f"/api/runs/{b_run}")
            assert r.status_code == 404, r.text
            r = await admin.post(f"/api/runs/{b_run}/end")
            assert r.status_code == 404, r.text
            assert b_run not in {
                x["run_id"] for x in (await admin.get("/api/runs")).json()["runs"]}


async def test_control_actions_audited_with_owner_and_denials():
    """创建/发送/停止/Fork/结束与拒绝都入审计，带 actor 与 run owner。"""
    with tempfile.TemporaryDirectory() as tmp:
        factory = FakeSessionFactory(script=DEFAULT_SCRIPT, delay=0.02)
        app = owner_app(tmp, factory)
        async with async_client(app, username="alice") as alice, \
                async_client(app, username="bob") as bob:
            a_run = await create_run_as(alice)
            await alice.post(f"/api/runs/{a_run}/messages", json={"text": "部署 nginx"})
            await wait_ready(alice, a_run)
            fork = (await alice.post(f"/api/runs/{a_run}/clone")).json()["run_id"]
            await alice.post(f"/api/runs/{a_run}/stop")  # READY 幂等：成功但不置位
            await alice.post(f"/api/runs/{fork}/end")
            # 越权拒绝（404）与状态机拒绝（409）都入审计
            await bob.post(f"/api/runs/{a_run}/messages", json={"text": "越权"})
            await alice.post(f"/api/runs/{fork}/messages", json={"text": "已结束"})  # session_not_active

        entries = audit_lines(audit_dir_of(tmp))
        by_action = {}
        for e in entries:
            by_action.setdefault((e["action"], e["result"]), []).append(e)
        # 成功路径全带 run_id / run_owner
        for action in ("create_run", "send", "clone", "end"):
            assert by_action[(action, "success")], entries
            e = by_action[(action, "success")][0]
            assert e["actor"] == "alice" and e["run_owner"] == "alice", e
            assert e["run_id"], e
        # 拒绝路径：404 越权（bob 对 alice 的 run）与 409 状态机
        assert any(e["actor"] == "bob" and e["result"] == "denied"
                   and e["run_owner"] == "alice" for e in entries), entries
        assert any(e["result"] == "denied" and e["reason"] == "session_not_active"
                   for e in entries), entries
        # 发送审计不含指令原文，只带长度与摘要（meta）
        send = by_action[("send", "success")][0]
        assert "部署 nginx" not in json.dumps(send, ensure_ascii=False), send
        assert send["meta"].startswith("len:"), send


async def test_audit_failure_blocks_control_actions():
    """审计写失败：创建/发送/停止/Fork/结束全部 503 不执行，读取不受阻断。"""
    with tempfile.TemporaryDirectory() as tmp:
        app = owner_app(tmp)
        async with async_client(app, username="alice") as alice:
            first = await create_run_as(alice)  # 审计正常时建得起来
            block_audit(audit_dir_of(tmp))

            r = await alice.post("/api/runs", json={})
            assert r.status_code == 503, r.text
            r = await alice.post(f"/api/runs/{first}/messages", json={"text": "继续"})
            assert r.status_code == 503, r.text
            r = await alice.post(f"/api/runs/{first}/stop")
            assert r.status_code == 503, r.text
            r = await alice.post(f"/api/runs/{first}/clone")
            assert r.status_code == 503, r.text
            r = await alice.post(f"/api/runs/{first}/end")
            assert r.status_code == 503, r.text

            # 读取不受阻断；被 503 的动作确实没执行
            assert (await alice.get("/api/runs")).status_code == 200
            summary = (await alice.get(f"/api/runs/{first}")).json()
            assert summary["status"] == "READY" and summary["first_prompt"] is None
            new_ids = {x["run_id"] for x in (await alice.get("/api/runs")).json()["runs"]}
            assert new_ids == {first}, new_ids


async def test_owner_survives_restart_via_bookkeeping():
    """重启恢复：簿记 owners 映射带回原归属；legacy 无 owner 会话归默认。"""
    with tempfile.TemporaryDirectory() as tmp:
        state_path = str(Path(tmp) / "state.json")
        factory = FakeSessionFactory(script=DEFAULT_SCRIPT, delay=0.02)
        app_a = make_test_app(
            session_factory=factory, state_path=state_path,
            default_owner="admin",  # legacy 迁移归属与 alice 区分开
        )
        async with async_client(app_a, username="alice") as alice, \
                async_client(app_a, username="bob") as bob:
            a_run = (await alice.post("/api/runs", json={})).json()["run_id"]
            await alice.post(f"/api/runs/{a_run}/messages", json={"text": "alice 的部署"})
            deadline = asyncio.get_event_loop().time() + 5.0
            while asyncio.get_event_loop().time() < deadline:
                if (await alice.get(f"/api/runs/{a_run}")).json()["status"] == "READY":
                    break
                await asyncio.sleep(0.01)

        # 重启：同一 transcript 与簿记，alice 的会话仍归 alice
        app_b = make_test_app(
            session_factory=factory, state_path=state_path,
            list_sessions_fn=factory.list_sessions,
            get_session_messages_fn=factory.get_session_messages,
            default_owner="admin",
        )
        async with async_client(app_b, username="alice") as alice, \
                async_client(app_b, username="bob") as bob:
            a_list = {r["run_id"] for r in (await alice.get("/api/runs")).json()["runs"]}
            b_list = {r["run_id"] for r in (await bob.get("/api/runs")).json()["runs"]}
            assert a_run in a_list, a_list
            assert a_run not in b_list, b_list
            # 恢复的会话仍可被 owner 续聊
            r = await alice.post(f"/api/runs/{a_run}/messages", json={"text": "重启后续写"})
            assert r.status_code == 200, r.text

        # legacy 形态：簿记抹掉 owners 后重启，恢复会话归默认 owner（admin）
        state = json.loads(Path(state_path).read_text(encoding="utf-8"))
        state.pop("owners", None)
        Path(state_path).write_text(json.dumps(state), encoding="utf-8")
        app_c = make_test_app(
            session_factory=factory, state_path=state_path,
            list_sessions_fn=factory.list_sessions,
            get_session_messages_fn=factory.get_session_messages,
            default_owner="admin",
        )
        async with async_client(app_c, username="admin") as admin:
            admin_list = {r["run_id"] for r in (await admin.get("/api/runs")).json()["runs"]}
            assert a_run in admin_list, admin_list


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
