#!/usr/bin/env python3
"""安全恢复与历史 owner 迁移 —— 状态版本化恢复的主缝测试。

缝：FastAPI ASGI 测试客户端（多用户打同一 app），覆盖 legacy 首启 admin
归属迁移、现代 owner 保留、未知/禁用 owner 隐藏、初始化标记后的 state
缺失受限恢复、state 损坏受限恢复、owner 映射落盘失败 503、重启保
run_id/墓碑/Fork 来源，以及停服人工 owner 转移（tools 脚本）的审计与
Web 不可达性。纯 assert，无 pytest。

运行：python web/tests/test_recovery.py
"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from web.fake import DEFAULT_SCRIPT, FakeSessionFactory  # noqa: E402
from web.tests.support import (  # noqa: E402
    TEST_PASSWORD, audit_lines, async_client, make_test_app, write_test_users,
)
from web.tests.test_history import session_info  # noqa: E402

PASSWORD = TEST_PASSWORD


def transcript():
    """一段最简可重放 transcript（tmsg 从 test_state 复用——桩只有一份）。"""
    from web.tests.test_state import tmsg
    return [
        tmsg("user", "部署 nginx"),
        tmsg("assistant", [{"type": "text", "text": "完成。"}]),
    ]


def recovery_app(state_path, infos, transcripts, factory=None, default_owner="admin",
                 **overrides):
    """簿记 + 假 transcript 装配的重启后应用（restore_app 同款，默认
    owner 与 alice/bob 区分开，其余 overrides 透传 make_test_app）。"""
    def get_messages(sid):
        if sid not in transcripts:
            raise FileNotFoundError(sid)
        return transcripts[sid]

    return make_test_app(
        session_factory=factory or FakeSessionFactory(script=DEFAULT_SCRIPT),
        list_sessions_fn=lambda: list(infos),
        get_session_messages_fn=get_messages,
        state_path=state_path,
        default_owner=default_owner,
        **overrides,
    )


def write_legacy_state(path, sessions=None, ended=(), owners=None):
    """旧字符串映射簿记（无 version）——write_state 是簿记桩的单一权威。"""
    from web.tests.test_state import write_state
    write_state(path, ended_sessions=ended, sessions=sessions,
                clone_sources={}, owners=owners, legacy=True)


def write_modern_state(path, sessions=None, ended=(), owners=None, clone_sources=None):
    """现代 v2 簿记（write_state 缺省形态）。"""
    from web.tests.test_state import write_state
    write_state(path, ended_sessions=ended, sessions=sessions,
                clone_sources=clone_sources, owners=owners)


async def wait_ready(client, run_id, timeout_s=5.0):
    deadline = asyncio.get_event_loop().time() + timeout_s
    last = None
    while asyncio.get_event_loop().time() < deadline:
        last = (await client.get(f"/api/runs/{run_id}")).json()
        if last["status"] == "READY":
            return last
        await asyncio.sleep(0.01)
    raise AssertionError(f"run {run_id} 未回 READY，最后状态 {last}")


async def test_legacy_first_boot_migrates_unowned_to_default():
    """首次启动（legacy 簿记 + 无初始化标记）：无映射历史与簿记无 owner
    记录的会话统一归默认 owner（admin）；直跑 CLI 的无映射 transcript
    同样归 admin。"""
    with tempfile.TemporaryDirectory() as tmp:
        state_path = Path(tmp) / "state.json"
        # 簿记有映射但无 owner（旧版服务部署过）；另有直跑 CLI 的无映射
        # transcript（sess_cli，不进簿记）
        write_legacy_state(state_path, sessions={"run_3": "sess_mapped"})
        infos = [session_info("sess_mapped", "旧簿记会话", 900),
                 session_info("sess_cli", "直跑 CLI 会话", 500)]
        app = recovery_app(state_path, infos,
                           {"sess_mapped": transcript(), "sess_cli": transcript()})
        async with async_client(app, username="admin") as admin, \
                async_client(app, username="alice") as alice:
            admin_runs = {r["run_id"] for r in (await admin.get("/api/runs")).json()["runs"]}
            # 映射命中沿用 run_3；无映射派生 run_hist_sess_cli——都归 admin
            assert admin_runs == {"run_3", "run_hist_sess_cli"}, admin_runs
            alice_runs = {r["run_id"] for r in (await alice.get("/api/runs")).json()["runs"]}
            assert alice_runs == set(), alice_runs
            # 迁移结果已物化为 v2（含 owners），初始化标记已写
            from web.state import load_state
            state = load_state(state_path)
            assert state["status"] == "modern", state
            assert state["owners"] == {"run_3": "admin", "run_hist_sess_cli": "admin"}, state
            assert (Path(tmp) / "state.json.initialized").exists()


async def test_modern_owner_preserved_and_unknown_owner_hidden():
    """现代簿记重启：owner 保留、run_id/墓碑/Fork 来源稳定；未知 owner
    （用户清单没有的用户名）字符串保留但对一切用户隐藏，重新启用同名
    用户后恢复可见。"""
    with tempfile.TemporaryDirectory() as tmp:
        users_path = write_test_users(Path(tmp) / "users.yaml")
        state_path = Path(tmp) / "state.json"
        # alice 的活会话、alice 已 ENDED 的会话、Fork 自源的会话、未知
        # owner（leftuser 已从清单删除）的会话
        write_modern_state(
            state_path,
            sessions={"run_1": "sess_live", "run_2": "sess_dead",
                      "run_3": "sess_fork", "run_4": "sess_orphan"},
            ended=["sess_dead"],
            owners={"run_1": "alice", "run_2": "alice",
                    "run_3": "alice", "run_4": "leftuser"},
            clone_sources={"sess_fork": "run_1"},
        )
        infos = [session_info("sess_live", "活会话", 900),
                 session_info("sess_dead", "已结束", 800),
                 session_info("sess_fork", "Fork", 700),
                 session_info("sess_orphan", "孤儿", 600)]
        transcripts = {sid: transcript() for sid in
                       ("sess_live", "sess_dead", "sess_fork", "sess_orphan")}
        app = recovery_app(state_path, infos, transcripts, users_path=users_path)
        async with async_client(app, username="alice") as alice, \
                async_client(app, username="bob") as bob:
            a_list = {r["run_id"]: r for r in (await alice.get("/api/runs")).json()["runs"]}
            assert set(a_list) == {"run_1", "run_2", "run_3"}, a_list
            assert a_list["run_2"]["status"] == "ENDED"       # 墓碑不复活
            assert a_list["run_3"]["resumed_from"] == "run_1"  # Fork 来源保留
            # 未知 owner：对 bob、对 admin、对 alice 都不可见（列表与单 run）
            for client in (bob,):
                b_list = {r["run_id"] for r in (await client.get("/api/runs")).json()["runs"]}
                assert "run_4" not in b_list, b_list
                r = await client.get("/api/runs/run_4")
                assert r.status_code == 404, r.text
            r = await alice.get("/api/runs/run_4")
            assert r.status_code == 404, r.text
            # owner 字符串保留在簿记（不因隐藏而抹掉）
            from web.state import load_state
            assert load_state(state_path)["owners"]["run_4"] == "leftuser"

            # 重新启用同名用户（写回清单）：原会话恢复可见、可续聊
            data = json.loads(users_path.read_text(encoding="utf-8"))
            from web.auth import hash_password
            data["users"]["leftuser"] = {
                "password_hash": hash_password(PASSWORD, 1000), "enabled": True}
            future = time.time() + 2
            users_path.write_text(json.dumps(data), encoding="utf-8")
            os.utime(users_path, (future, future))
            async with async_client(app, username="leftuser") as left:
                runs = {r["run_id"] for r in (await left.get("/api/runs")).json()["runs"]}
                assert runs == {"run_4"}, runs
                r = await left.post("/api/runs/run_4/messages", json={"text": "回归续聊"})
                assert r.status_code == 200, r.text


async def test_state_missing_after_init_enters_restricted_recovery():
    """初始化完成后 state 缺失：受限恢复——会话隐藏（含默认 owner）、
    控制动作 503、读取继续；不是自动归 admin。"""
    with tempfile.TemporaryDirectory() as tmp:
        state_path = Path(tmp) / "state.json"
        # 第一个进程正常起一次（写初始化标记）
        write_modern_state(state_path, sessions={"run_1": "sess_live"},
                           owners={"run_1": "alice"})
        infos = [session_info("sess_live", "活会话", 900)]
        app = recovery_app(state_path, infos, {"sess_live": transcript()})
        async with async_client(app, username="alice") as alice:
            assert (await alice.get("/api/runs")).json()["runs"]  # 正常恢复

        # 删 state（保留标记）：重启进受限恢复
        state_path.unlink()
        app2 = recovery_app(state_path, infos, {"sess_live": transcript()})
        async with async_client(app2, username="admin") as admin, \
                async_client(app2, username="alice") as alice:
            admin_runs = (await admin.get("/api/runs")).json()["runs"]
            assert admin_runs == [], admin_runs  # 不能自动归 admin
            alice_runs = (await alice.get("/api/runs")).json()["runs"]
            assert alice_runs == [], alice_runs  # 重放会话 owner 未知，隐藏
            # 控制动作全部 503 且不执行；读取不受影响
            r = await admin.post("/api/runs", json={})
            assert r.status_code == 503, r.text
            r = await alice.post("/api/runs/run_hist_sess_live/messages", json={"text": "x"})
            assert r.status_code == 503, r.text
            r = await alice.post("/api/runs/run_hist_sess_live/end")
            assert r.status_code == 503, r.text
            assert (await alice.get("/api/runs")).status_code == 200


async def test_corrupt_state_restricted_not_silent_replay():
    """state 损坏：受限恢复（不沿用「损坏只告警按空簿记启动」），未知
    归属会话不暴露、控制动作阻断；清理坏文件后重启回到正常。"""
    with tempfile.TemporaryDirectory() as tmp:
        state_path = Path(tmp) / "state.json"
        state_path.write_text("garbage{", encoding="utf-8")
        infos = [session_info("sess_a", "会话 A", 900)]
        app = recovery_app(state_path, infos, {"sess_a": transcript()})
        async with async_client(app, username="admin") as admin:
            assert (await admin.get("/api/runs")).json()["runs"] == []
            r = await admin.post("/api/runs", json={})
            assert r.status_code == 503, r.text
        # 人工恢复路径：停服、放回有效簿记（模拟从备份恢复）、重启
        write_modern_state(state_path, sessions={"run_1": "sess_a"},
                           owners={"run_1": "admin"})
        app2 = recovery_app(state_path, infos, {"sess_a": transcript()})
        async with async_client(app2, username="admin") as admin:
            runs = {r["run_id"] for r in (await admin.get("/api/runs")).json()["runs"]}
            assert runs == {"run_1"}, runs


async def test_legacy_state_after_init_restricted_not_remigrated():
    """初始化完成后 state 回退旧格式（备份覆盖、误还原）：受限恢复而不是
    再走一次 legacy 迁移——初始化后的用户会话不能按旧格式归默认 owner。"""
    with tempfile.TemporaryDirectory() as tmp:
        state_path = Path(tmp) / "state.json"
        # 第一个进程正常初始化（写标记）
        write_modern_state(state_path, sessions={"run_1": "sess_live"},
                           owners={"run_1": "alice"})
        infos = [session_info("sess_live", "活会话", 900)]
        app = recovery_app(state_path, infos, {"sess_live": transcript()})
        async with async_client(app, username="alice") as alice:
            assert (await alice.get("/api/runs")).json()["runs"]

        # state 被旧格式文件覆盖（保留初始化标记）：受限恢复
        write_legacy_state(state_path, sessions={"run_1": "sess_live"})
        app2 = recovery_app(state_path, infos, {"sess_live": transcript()})
        async with async_client(app2, username="admin") as admin:
            admin_runs = (await admin.get("/api/runs")).json()["runs"]
            assert admin_runs == [], admin_runs  # 不按 legacy 再迁给 admin
            r = await admin.post("/api/runs", json={})
            assert r.status_code == 503, r.text


async def test_persist_failure_blocks_control_actions():
    """owner 映射无法安全落盘：发送、停止、Fork、结束与 OBS 配置修改
    503 且不执行；读取不受影响。"""
    with tempfile.TemporaryDirectory() as tmp:
        state_path = Path(tmp) / "state.json"
        write_modern_state(state_path, sessions={"run_1": "sess_live"},
                           owners={"run_1": "alice"})
        infos = [session_info("sess_live", "活会话", 900)]
        app = recovery_app(state_path, infos, {"sess_live": transcript()})
        async with async_client(app, username="alice") as alice:
            # 正常路径可用（先验证归属已恢复）
            summary = (await alice.get("/api/runs/run_1")).json()
            assert summary["status"] == "READY", summary
            # 簿记写路径封堵：state 文件换成同名目录（mkstemp 进不去、
            # os.replace 目录对文件必败——审计的目录换文件手法等价）
            state_path.unlink()
            state_path.mkdir()

            r = await alice.post("/api/runs", json={})
            assert r.status_code == 503, r.text
            r = await alice.post("/api/runs/run_1/messages", json={"text": "继续"})
            assert r.status_code == 503, r.text
            r = await alice.post("/api/runs/run_1/stop")
            assert r.status_code == 503, r.text
            r = await alice.post("/api/runs/run_1/clone")
            assert r.status_code == 503, r.text
            r = await alice.post("/api/runs/run_1/end")
            assert r.status_code == 503, r.text
            # OBS 配置修改（admin）同样被落盘门拒绝
            async with async_client(app, username="admin") as admin:
                r = await admin.post("/api/obs/config",
                                     json={"bucket": "shared-bucket"})
                assert r.status_code == 503, r.text
            # 读取不受阻断；被 503 的动作确实没执行（无新会话、无新指令）
            summary = (await alice.get("/api/runs/run_1")).json()
            assert summary["status"] == "READY", summary
            assert summary["first_prompt"] == "活会话", summary  # 重放值未被覆盖
            new_ids = {x["run_id"] for x in (await alice.get("/api/runs")).json()["runs"]}
            assert new_ids == {"run_1"}, new_ids


async def test_manual_owner_transfer_flow_audited():
    """人工 owner 转移：只能停服用 tools 脚本完成（备份 + 转移 + 审计），
    Web API 不提供转移入口；转移后新 owner 可见可续聊、原 owner 404。"""
    with tempfile.TemporaryDirectory() as tmp:
        audit_dir = Path(tmp) / "audit"
        state_path = Path(tmp) / "state.json"
        write_modern_state(state_path, sessions={"run_1": "sess_live"},
                           owners={"run_1": "alice"})
        infos = [session_info("sess_live", "活会话", 900)]
        app = recovery_app(state_path, infos, {"sess_live": transcript()},
                           audit_dir=audit_dir)
        async with async_client(app, username="alice") as alice:
            assert "run_1" in {r["run_id"] for r in (await alice.get("/api/runs")).json()["runs"]}

        # 停服后人工转移：脚本改簿记 owner 并写审计
        env = dict(os.environ,
                   AUTO_IMAGE_STATE=str(state_path),
                   AUTO_IMAGE_AUDIT_DIR=str(audit_dir))
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve().parent.parent.parent
                                 / "tools" / "transfer_ownership.py"),
             "--from", "alice", "--to", "bob", "--reason", "人员调整"],
            capture_output=True, text=True, env=env, timeout=60)
        assert result.returncode == 0, result.stderr
        backup = Path(str(state_path) + ".bak-transfer")
        assert backup.exists()  # 转移前自动备份

        # 重启：bob 得到会话（可续聊），alice 失去（404）
        app2 = recovery_app(state_path, infos, {"sess_live": transcript()},
                            audit_dir=audit_dir)
        async with async_client(app2, username="bob") as bob, \
                async_client(app2, username="alice") as alice:
            b_list = {r["run_id"] for r in (await bob.get("/api/runs")).json()["runs"]}
            assert b_list == {"run_1"}, b_list
            r = await bob.post("/api/runs/run_1/messages", json={"text": "转移后续聊"})
            assert r.status_code == 200, r.text
            r = await alice.get("/api/runs/run_1")
            assert r.status_code == 404, r.text

        # 转移入审计：actor=operator、带 from/to/reason，可追溯
        entries = audit_lines(audit_dir)
        transfers = [e for e in entries if e["action"] == "owner_transfer"]
        assert len(transfers) == 1, entries
        e = transfers[0]
        assert e["meta"]["from"] == "alice" and e["meta"]["to"] == "bob", e
        assert e["meta"]["reason"] == "人员调整", e
        assert e["meta"]["runs"] == ["run_1"], e

        # Web API 无转移入口：不存在的路由 404 / 已有路由的 owner 字段忽略
        async with async_client(app2, username="bob") as bob:
            r = await bob.post("/api/runs/run_1/transfer", json={"owner": "bob"})
            assert r.status_code in (404, 405), r.text  # 无此路由
            r = await bob.post("/api/runs/run_1/messages",
                               json={"text": "x", "owner": "alice"})
            assert r.status_code == 200, r.text  # owner 字段被忽略，bob 照常发送


async def main():
    tests = [fn for name, fn in sorted(globals().items()) if name.startswith("test_")]
    for fn in tests:
        await fn()
        print(f"ok {fn.__name__}")
    print(f"{len(tests)} passed")


if __name__ == "__main__":
    asyncio.run(main())
