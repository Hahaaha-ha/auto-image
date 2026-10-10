#!/usr/bin/env python3
"""TaskStore 状态机单测 —— 创建/类型分流/阶段推进/收尾/产物确认/恢复。

直接以完整事件 dict 驱动 handle_event（与 EventStore 观察者同形），
不触 HTTP、不触云。纯 assert，无 pytest。

运行：python web/tests/test_tasks.py
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from web.tasks import TaskStore, build_task_prompt, parse_software_version  # noqa: E402


def ev(run_id, etype, payload, ts):
    return {"seq": 1, "ts": ts, "run_id": run_id, "type": etype, "payload": payload}


def make_store(task_dir, roots=None):
    return TaskStore(Path(task_dir), roots or {})


def stage_event(run_id, stage, subagent, ts):
    return ev(run_id, "stage.changed", {"stage": stage, "status": "running", "subagent": subagent}, ts)


def test_create_image_task_from_deploy_guide():
    with tempfile.TemporaryDirectory() as td:
        store = make_store(td)
        t0, t1 = 1_700_000_000.0, 1_700_000_030.0
        store.handle_event(ev("r1", "user.message", {"text": "一键部署 nginx，版本 1.25.3"}, t0))
        store.handle_event(stage_event("r1", "GUIDE", "deploy-guide", t1))
        (task,) = store.list()
        assert task["type"] == "image"
        assert task["status"] == "RUNNING"
        assert task["software"] == "nginx" and task["version"] == "1.25.3", task
        assert task["name"].startswith("镜像 nginx 1.25.3 ("), task["name"]
        assert task["stages"] == [{"stage": "GUIDE", "started_at": t1, "ended_at": None}]
        assert task["current_stage"] == "GUIDE"
        # 落盘了（task/ 目录一个 JSON）
        files = list(Path(td).glob("task-*.json"))
        assert len(files) == 1 and json.loads(files[0].read_text())["task_id"] == task["task_id"]


def test_create_rpm_task_from_rpm_build():
    with tempfile.TemporaryDirectory() as td:
        store = make_store(td)
        store.handle_event(ev("r1", "user.message", {"text": "制作 redis 7.2 的 RPM 包"}, 1.0))
        store.handle_event(stage_event("r1", "BUILD", "rpm-build", 2.0))
        (task,) = store.list()
        assert task["type"] == "rpm"
        assert task["stages"][0]["stage"] == "BUILD"


def test_stage_progression_and_dedupe():
    with tempfile.TemporaryDirectory() as td:
        store = make_store(td)
        base = 1_700_000_000.0
        store.handle_event(stage_event("r1", "GUIDE", "deploy-guide", base))
        # 相邻同阶段重派（guide 重试）：不新增条目
        store.handle_event(stage_event("r1", "GUIDE", "deploy-guide", base + 10))
        task = store.list()[0]
        assert len(task["stages"]) == 1
        assert task["stages"][0]["ended_at"] is None
        # 阶段推进：关上 GUIDE，开 INSTALL
        store.handle_event(stage_event("r1", "INSTALL", "deploy-install", base + 20))
        task = store.list()[0]
        assert [s["stage"] for s in task["stages"]] == ["GUIDE", "INSTALL"]
        assert task["stages"][0]["ended_at"] == base + 20
        assert task["current_stage"] == "INSTALL"
        # 非相邻重入（INSTALL 后回炉 GUIDE）：合法追加第二条 GUIDE
        store.handle_event(stage_event("r1", "GUIDE", "deploy-guide", base + 30))
        task = store.list()[0]
        assert [s["stage"] for s in task["stages"]] == ["GUIDE", "INSTALL", "GUIDE"]


def test_turn_end_outcomes_and_usage():
    with tempfile.TemporaryDirectory() as td:
        store = make_store(td)
        base = 1_700_000_000.0
        usage = {"input_tokens": 100, "output_tokens": 50,
                 "cache_read_input_tokens": 9000, "cache_creation_input_tokens": 7}
        store.handle_event(stage_event("r1", "GUIDE", "deploy-guide", base))
        store.handle_event(ev("r1", "turn.completed", {"result": "ok", "usage": usage}, base + 60))
        task = store.list()[0]
        assert task["status"] == "DONE" and task["outcome"] == "success"
        assert task["current_stage"] is None and task["ended_at"] == base + 60
        assert task["stages"][-1]["ended_at"] == base + 60
        assert task["usage"] == {"input_tokens": 100, "output_tokens": 50,
                                 "cache_read_tokens": 9000, "cache_creation_tokens": 7}

    with tempfile.TemporaryDirectory() as td:
        store = make_store(td)
        base = 1_700_000_100.0
        store.handle_event(stage_event("r1", "GUIDE", "rpm-guide", base))
        store.handle_event(ev("r1", "turn.failed", {"message": "boom"}, base + 5))
        task = store.list()[0]
        assert task["outcome"] == "failed"

    with tempfile.TemporaryDirectory() as td:
        store = make_store(td)
        base = 1_700_000_200.0
        store.handle_event(stage_event("r1", "GUIDE", "deploy-guide", base))
        store.handle_event(ev("r1", "turn.stopped", {}, base + 5))
        assert store.list()[0]["outcome"] == "stopped"


def test_session_ended_mid_task_interrupts():
    """RUNNING 中结束会话：回合被取消且不补收尾事件，session.ended 收尾。"""
    with tempfile.TemporaryDirectory() as td:
        store = make_store(td)
        base = 1_700_000_000.0
        store.handle_event(stage_event("r1", "INSTALL", "deploy-install", base))
        store.handle_event(ev("r1", "session.ended", {}, base + 5))
        task = store.list()[0]
        assert task["status"] == "DONE" and task["outcome"] == "interrupted"
        # 收尾后同 run 再无活动任务：下一条流水线指令可开新任务
        store.handle_event(ev("r1", "user.message", {"text": "再来一次 postgres 16.1"}, base + 10))
        store.handle_event(stage_event("r1", "GUIDE", "deploy-guide", base + 20))
        assert len(store.list()) == 2


def test_plain_and_second_turn_tasks():
    with tempfile.TemporaryDirectory() as td:
        store = make_store(td)
        base = 1_700_000_000.0
        # 无 stage 事件的回合：不建任务
        store.handle_event(ev("r1", "user.message", {"text": "你好"}, base))
        store.handle_event(ev("r1", "turn.completed", {"result": "ok"}, base + 1))
        assert store.list() == []
        # 同会话第二回合开流水线：建任务
        store.handle_event(ev("r1", "user.message", {"text": "部署 nginx 1.25"}, base + 10))
        store.handle_event(stage_event("r1", "GUIDE", "deploy-guide", base + 11))
        store.handle_event(ev("r1", "turn.completed", {"result": "ok"}, base + 20))
        # 第三回合再开一条：第二个任务
        store.handle_event(ev("r1", "user.message", {"text": "部署 redis 7.2"}, base + 30))
        store.handle_event(stage_event("r1", "GUIDE", "deploy-guide", base + 31))
        store.handle_event(ev("r1", "turn.completed", {"result": "ok"}, base + 40))
        tasks = store.list()
        assert len(tasks) == 2
        assert tasks[0]["software"] == "redis" and tasks[1]["software"] == "nginx"  # 新→旧


def test_artifact_confirm_and_install_meta():
    with tempfile.TemporaryDirectory() as td:
        roots_dir = Path(td) / "roots"
        deploy = roots_dir / "deploy"
        sw_ver = deploy / "nginx" / "1.25.3"
        sw_ver.mkdir(parents=True)
        (sw_ver / "nginx-install.md").write_text("guide", encoding="utf-8")
        (sw_ver / "nginx-install-meta.json").write_text(json.dumps({
            "path": "create", "server_alias": "nginx-2026091414",
            "instance_id": "i-123", "auth_method": "password",
        }), encoding="utf-8")
        store = make_store(Path(td) / "task", {"deploy": deploy, "rpm": roots_dir / "rpm"})
        base = time.time() - 5  # 产物 mtime（当下）晚于任务创建 → 候选
        store.handle_event(ev("r1", "user.message", {"text": "部署 nginx"}, base))
        store.handle_event(stage_event("r1", "INSTALL", "deploy-install", base + 1))
        task = store.list()[0]
        assert task["confirmed"] is True
        assert task["software"] == "nginx" and task["version"] == "1.25.3"
        assert task["instance_id"] == "i-123" and task["server_alias"] == "nginx-2026091414"
        assert "nginx 1.25.3" in task["name"]


def test_recover_interrupts_running_and_no_dup():
    with tempfile.TemporaryDirectory() as td:
        task_dir = Path(td) / "task"
        store = make_store(task_dir)
        base = 1_700_000_000.0
        store.handle_event(stage_event("r1", "INSTALL", "deploy-install", base))
        store.handle_event(ev("r1", "turn.completed", {"result": "done"}, base + 60))
        finished_id = store.list()[0]["task_id"]  # 已收尾：success
        store.handle_event(stage_event("r1", "GUIDE", "deploy-guide", base + 100))
        running_id = store.list()[0]["task_id"]   # 进行中：重启后被 interrupted 定格
        assert running_id != finished_id

        # 坏文件：不阻断恢复
        (task_dir / "task-broken.json").write_text("{oops", encoding="utf-8")

        recovered = make_store(task_dir)
        recovered.recover()
        tasks = {t["task_id"]: t for t in recovered.list()}
        assert len(tasks) == 2  # 坏文件跳过、两条任务不重复
        assert tasks[running_id]["status"] == "DONE"
        assert tasks[running_id]["outcome"] == "interrupted"
        assert tasks[running_id]["stages"][-1]["ended_at"] is not None
        assert tasks[finished_id]["outcome"] == "success"


def test_parse_software_version_cases():
    assert parse_software_version("一键部署 nginx，版本 1.25.3 到机器") == ("nginx", "1.25.3")
    assert parse_software_version("redis 7.2 打包") == ("redis", "7.2")
    assert parse_software_version("制作 postgresql v16.1 的 RPM") == ("postgresql", "16.1")
    assert parse_software_version("openEuler 22.03 LTS 下验证") == ("openEuler", "22.03")
    assert parse_software_version("聊聊今天天气") == (None, None)
    assert parse_software_version("") == (None, None)


def test_create_manual_init_task():
    with tempfile.TemporaryDirectory() as td:
        store = make_store(td)
        task = store.create_manual({
            "software": "nginx", "version": "1.25.3", "task_type": "image",
            "ecs_mode": "existing",
            "ecs_instance": {"id": "i-1", "name": "ecs-nginx-20260914", "ip": "1.2.3.4"},
            "install_doc": "https://nginx.org/en/docs.html",
        })
        assert task["status"] == "INIT" and task["origin"] == "manual"
        assert task["type"] == "image"
        assert task["software"] == "nginx" and task["version"] == "1.25.3"
        assert task["confirmed"] is True  # 手动任务的软件/版本来自表单即权威
        assert task["name"].startswith("镜像 nginx 1.25.3 (")
        assert task["stages"] == [] and task["run_id"] is None
        assert len(list(Path(td).glob("task-*.json"))) == 1


def test_mark_running_claims_stage_events():
    """mark_running 先于回合执行：首个 stage.changed 认领手动任务而非新建
    auto 任务；回合收尾按既有状态机走。"""
    with tempfile.TemporaryDirectory() as td:
        store = make_store(td)
        task = store.create_manual({"software": "redis", "version": "7.2",
                                    "task_type": "rpm", "ecs_mode": "create"})
        prompt = "制作 redis 7.2 的 RPM 包…"
        base = 1_700_000_000.0
        store.handle_event(ev("run_9", "user.message", {"text": prompt}, base))
        assert store.mark_running(task["task_id"], "run_9", prompt) is not None
        store.handle_event(stage_event("run_9", "GUIDE", "rpm-guide", base + 1))
        store.handle_event(stage_event("run_9", "BUILD", "rpm-build", base + 2))
        store.handle_event(ev("run_9", "turn.completed", {"result": "ok"}, base + 60))
        tasks = store.list()
        assert len(tasks) == 1  # 不新建第二个
        t = tasks[0]
        assert t["status"] == "DONE" and t["outcome"] == "success"
        assert [s["stage"] for s in t["stages"]] == ["GUIDE", "BUILD"]


def test_mark_running_rejects_non_init():
    with tempfile.TemporaryDirectory() as td:
        store = make_store(td)
        task = store.create_manual({"software": "a"})
        assert store.mark_running(task["task_id"], "r1", "x") is not None
        assert store.mark_running(task["task_id"], "r2", "x") is None  # 非 INIT 拒绝
        assert store.mark_running("task-nope", "r1", "x") is None


def test_build_task_prompt_cases():
    # 镜像 + 已有 ECS（流水线命名规约：ecs-<别名> → 别名直给）
    p = build_task_prompt({"software": "nginx", "version": "1.25.3", "task_type": "image",
                           "ecs_mode": "existing",
                           "ecs_instance": {"name": "ecs-nginx-20260914", "ip": "1.2.3.4"},
                           "install_doc": "https://doc.example"})
    assert p.startswith("一键部署 nginx 1.25.3")
    assert "目标服务器别名：nginx-20260914" in p
    assert "安装文档：https://doc.example" in p
    # 外来机器（无 ecs- 前缀）：name+IP 让 agent 解析
    p = build_task_prompt({"software": "redis", "task_type": "image", "ecs_mode": "existing",
                           "ecs_instance": {"name": "my-server", "ip": "5.6.7.8"}})
    assert "已有 ECS「my-server」（IP 5.6.7.8）" in p and "ssh-skill" in p
    assert "redis 最新稳定版" in p  # 版本缺省
    assert "请检索官方最新稳定版文档" in p
    # 按需创建：只列填写项
    p = build_task_prompt({"software": "mysql", "version": "8.0", "task_type": "image",
                           "ecs_mode": "create",
                           "ecs_params": {"flavor": "c6.xlarge.2", "disk_size": 100}})
    assert "按需创建 ECS" in p and "规格=c6.xlarge.2" in p and "系统盘GB=100" in p
    assert "镜像=" not in p  # 未填写项不透传
    # RPM 类型
    p = build_task_prompt({"software": "postgresql", "version": "16.1", "task_type": "rpm",
                           "ecs_mode": "create", "ecs_params": {}})
    assert p.startswith("制作 postgresql 16.1 的 RPM 包") and "scope 默认" in p


def test_recover_keeps_init():
    with tempfile.TemporaryDirectory() as td:
        store = make_store(td)
        store.create_manual({"software": "nginx", "version": "1.25.3"})
        recovered = make_store(td)
        recovered.recover()
        (task,) = recovered.list()
        assert task["status"] == "INIT"  # 待运行任务不受重启影响，仍可运行


def main():
    tests = [fn for name, fn in sorted(globals().items()) if name.startswith("test_")]
    for fn in tests:
        fn()
        print(f"ok {fn.__name__}")
    print(f"{len(tests)} passed")


if __name__ == "__main__":
    main()
