#!/usr/bin/env python3
"""Owner 过滤的全局事件流与容量 —— 多用户流隔离的主缝测试。

缝：FastAPI ASGI 测试客户端 ×2（alice / bob 打同一 app），覆盖全局流逐帧
owner 过滤（多客户端混流互不可见）、心跳周期身份撤销关流（禁用/改密）、
匿名容量字段（running_count / max_parallel 不带他人会话细节）与跨用户
全局并发边界。纯 assert，无 pytest。

运行：python web/tests/test_stream_isolation.py
"""
import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from web.auth import hash_password  # noqa: E402
from web.fake import DEFAULT_SCRIPT, FakeSessionFactory  # noqa: E402
from web.tests.support import TEST_PASSWORD, async_client, make_test_app  # noqa: E402

PASSWORD = TEST_PASSWORD


def stream_app(tmp, factory=None, **overrides):
    """alice/bob 双用户应用（用户清单可热改：路径在 app.state.test_root）。"""
    return make_test_app(
        session_factory=factory or FakeSessionFactory(script=DEFAULT_SCRIPT, delay=0.02),
        **overrides,
    )


def rewrite_users(users_path, mutate):
    """改写用户清单并前拨 mtime 确保热载（同款手法见 test_auth）。"""
    data = json.loads(users_path.read_text(encoding="utf-8"))
    mutate(data["users"])
    users_path.write_text(json.dumps(data), encoding="utf-8")
    future = time.time() + 2
    os.utime(users_path, (future, future))


async def open_global_stream(client):
    return await client.send(
        client.build_request("GET", "/api/stream"),
        stream=True,
    )


async def collect_frames(resp, stop=None, deadline_s=5.0, max_pings=None):
    """聚合全局流为 (帧列表, 心跳数)；stop(frame) 为 True 停止，max_pings
    收满即停（常驻流不会自己结束）。"""
    frames = []
    pings = 0
    block = []
    deadline = time.monotonic() + deadline_s
    async for line in resp.aiter_lines():
        if time.monotonic() > deadline:
            break
        if line == "":
            if block:
                ev = {}
                for l in block:
                    key, _, value = l.partition(":")
                    if key == "data":
                        ev["data"] = json.loads(value)
                    elif key in ("id", "event"):
                        ev[key] = value.strip()
                frames.append(ev)
                block = []
                if stop and stop(ev):
                    break
            continue
        if line.startswith(":"):
            pings += 1
            if max_pings is not None and pings >= max_pings:
                break
            continue
        block.append(line)
    return frames, pings


async def drain_until_closed(resp):
    """读到流自然结束（服务端关流）；传输异常同样视为连接终止。"""
    try:
        async for _ in resp.aiter_lines():
            pass
    except Exception:  # noqa: BLE001 —— 客户端侧读到任何收尾都算关流
        pass


async def wait_status(client, run_id, want, timeout_s=5.0):
    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        last = (await client.get(f"/api/runs/{run_id}")).json()
        if last["status"] == want:
            return last
        await asyncio.sleep(0.01)
    raise AssertionError(f"run {run_id} 未进入 {want}，最后状态 {last}")


async def test_global_stream_filters_frames_by_owner():
    """两个用户同时开流：各自只收自己会话的帧，他人事件一帧不进流。"""
    with tempfile.TemporaryDirectory() as tmp:
        app = stream_app(tmp)
        async with async_client(app, username="alice") as alice, \
                async_client(app, username="bob") as bob:
            a_run = (await alice.post("/api/runs", json={})).json()["run_id"]
            b_run = (await bob.post("/api/runs", json={})).json()["run_id"]
            resp_a = await open_global_stream(alice)
            resp_b = await open_global_stream(bob)
            # bob 先发起跑（他的事件窗口覆盖 alice 的收流全程），alice 后发
            await bob.post(f"/api/runs/{b_run}/messages", json={"text": "bob 的 redis"})
            await wait_status(bob, b_run, "RUNNING")
            await alice.post(f"/api/runs/{a_run}/messages", json={"text": "alice 的 nginx"})

            frames_a, _ = await collect_frames(
                resp_a, stop=lambda e: e["data"]["type"] == "turn.completed")
            frames_b, _ = await collect_frames(
                resp_b, stop=lambda e: e["data"]["type"] == "turn.completed")
            await resp_a.aclose()
            await resp_b.aclose()
            await wait_status(alice, a_run, "READY", timeout_s=8.0)
            await wait_status(bob, b_run, "READY", timeout_s=8.0)

            # 逐帧 owner 过滤：流上只有自己的 run（bob 的窗口内事件全被滤掉）
            assert {f["data"]["run_id"] for f in frames_a} == {a_run}
            assert {f["data"]["run_id"] for f in frames_b} == {b_run}
            # 自己会话的回合完整到达（含收尾）
            types_a = [f["data"]["type"] for f in frames_a]
            assert "turn.completed" in types_a, types_a
            texts_a = [f["data"]["payload"].get("text") for f in frames_a
                       if f["data"]["type"] == "user.message"]
            assert texts_a == ["alice 的 nginx"], texts_a


async def test_stream_closed_within_heartbeat_after_user_disabled():
    """连接存续期间禁用用户：心跳周期内关流（流忙——事件持续到达——重验
    也不跳过）；他人连接不受牵连，心跳照常。"""
    with tempfile.TemporaryDirectory() as tmp:
        app = stream_app(tmp, factory=FakeSessionFactory(script=DEFAULT_SCRIPT, delay=0.2))
        users_path = app.state.test_root / "users.yaml"
        async with async_client(app, username="alice") as alice, \
                async_client(app, username="bob") as bob:
            a_run = (await alice.post("/api/runs", json={})).json()["run_id"]
            resp_a = await open_global_stream(alice)
            resp_b = await open_global_stream(bob)
            await alice.post(f"/api/runs/{a_run}/messages", json={"text": "部署 nginx"})
            await wait_status(alice, a_run, "RUNNING")

            rewrite_users(users_path, lambda users: users["alice"].update(enabled=False))
            # 心跳周期（测试装配 0.05s）内被服务端关闭
            await asyncio.wait_for(drain_until_closed(resp_a), timeout=3.0)

            # bob 的连接照常：心跳还在发
            _, pings_b = await collect_frames(resp_b, max_pings=2, deadline_s=2.0)
            await resp_b.aclose()
            assert pings_b >= 2, pings_b


async def test_busy_stream_still_rechecks_identity():
    """流忙不豁免重验：事件持续到达（每步间隔小于心跳间隔，flag 不断被
    唤醒、等不来超时分支）时，禁用用户仍在心跳周期内被关流。"""
    with tempfile.TemporaryDirectory() as tmp:
        # 剧本拉长 + delay 0.02s（心跳 0.05s）：事件唤醒不断、窗口常忙
        busy_script = []
        for step in DEFAULT_SCRIPT:
            if step.get("type") == "assistant":
                content = step["message"]["content"]
                busy_script.extend(
                    {"type": "assistant", "message": {"content": [dict(item)]}}
                    for item in content
                )
            else:
                busy_script.append(step)
        app = stream_app(tmp, factory=FakeSessionFactory(script=busy_script, delay=0.02))
        users_path = app.state.test_root / "users.yaml"
        async with async_client(app, username="alice") as alice:
            a_run = (await alice.post("/api/runs", json={})).json()["run_id"]
            resp_a = await open_global_stream(alice)
            await alice.post(f"/api/runs/{a_run}/messages", json={"text": "部署 nginx"})
            await wait_status(alice, a_run, "RUNNING")

            rewrite_users(users_path, lambda users: users["alice"].update(enabled=False))
            # 事件仍在持续产生（回合未收尾）：关流必须来自重验，不是流静默
            await asyncio.wait_for(drain_until_closed(resp_a), timeout=3.0)
            status = (await alice.get(f"/api/runs/{a_run}")).status_code
            assert status == 401, status  # 连接身份已失效（一并验证撤销生效）


async def test_stream_closed_after_password_change():
    """改密（密码版本指纹漂移）同样在心跳周期内撤销长连接。"""
    with tempfile.TemporaryDirectory() as tmp:
        app = stream_app(tmp, factory=FakeSessionFactory(script=DEFAULT_SCRIPT, delay=0.2))
        users_path = app.state.test_root / "users.yaml"
        async with async_client(app, username="alice") as alice:
            a_run = (await alice.post("/api/runs", json={})).json()["run_id"]
            resp_a = await open_global_stream(alice)
            await alice.post(f"/api/runs/{a_run}/messages", json={"text": "部署 nginx"})
            await wait_status(alice, a_run, "RUNNING")

            rewrite_users(users_path, lambda users: users["alice"].update(
                password_hash=hash_password("rotated-password", 1000)))
            await asyncio.wait_for(drain_until_closed(resp_a), timeout=3.0)


async def test_capacity_fields_anonymous_and_global():
    """/api/runs 附带匿名容量：全局 running_count/max_parallel（跨用户合计），
    响应不带他人 run id、标题、prompt、owner 或目标机器。"""
    with tempfile.TemporaryDirectory() as tmp:
        app = stream_app(tmp, factory=FakeSessionFactory(script=DEFAULT_SCRIPT, delay=0.2))
        async with async_client(app, username="alice") as alice, \
                async_client(app, username="bob") as bob:
            a_run = (await alice.post("/api/runs", json={})).json()["run_id"]
            prompt = "alice 的机密部署指令"
            await alice.post(f"/api/runs/{a_run}/messages", json={"text": prompt})
            await wait_status(alice, a_run, "RUNNING")

            r = await bob.get("/api/runs")
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["runs"] == [], body  # bob 自己没有会话
            assert body["running_count"] == 1, body  # 全局口径：alice 的回合计入
            assert isinstance(body["max_parallel"], int) and body["max_parallel"] >= 1
            raw = json.dumps(body, ensure_ascii=False)
            assert a_run not in raw and prompt not in raw and "alice" not in raw, raw


async def test_capacity_counts_running_turns_only_and_cross_user_limit():
    """并发只数 RUNNING 回合（空会话与 Fork 不占名额）；上限跨用户生效，
    名额释放后恢复。"""
    with tempfile.TemporaryDirectory() as tmp:
        app = stream_app(tmp, factory=FakeSessionFactory(script=DEFAULT_SCRIPT, delay=0.2),
                         max_parallel_runs=1)
        async with async_client(app, username="alice") as alice, \
                async_client(app, username="bob") as bob:
            a_run = (await alice.post("/api/runs", json={})).json()["run_id"]
            # 空会话与 Fork 不占名额：三个会话在场，running_count 仍 0
            await alice.post("/api/runs", json={})
            await alice.post(f"/api/runs/{a_run}/clone")
            assert (await alice.get("/api/runs")).json()["running_count"] == 0

            await alice.post(f"/api/runs/{a_run}/messages", json={"text": "部署 nginx"})
            await wait_status(alice, a_run, "RUNNING")
            # 名额已被 alice 占满：bob 的发送 409（全局边界不分用户）
            b_run = (await bob.post("/api/runs", json={})).json()["run_id"]
            r = await bob.post(f"/api/runs/{b_run}/messages", json={"text": "bob 的部署"})
            assert r.status_code == 409, r.text
            assert r.json()["detail"] == "parallel_limit_reached"
            assert (await bob.get("/api/runs")).json()["running_count"] == 1

            # 名额释放（alice 回合完成）后 bob 可发
            await wait_status(alice, a_run, "READY", timeout_s=8.0)
            r = await bob.post(f"/api/runs/{b_run}/messages", json={"text": "bob 的部署"})
            assert r.status_code == 200, r.text


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
