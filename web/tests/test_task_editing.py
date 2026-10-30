#!/usr/bin/env python
"""共享任务编辑与指令预览：通过 HTTP、持久文件与假 SDK 验证行为。"""
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from web.fake import FakeSessionFactory  # noqa: E402
from web.tests.support import async_client, make_test_app  # noqa: E402


BASE_SPEC = {
    "software": "nginx", "version": "1.25.3", "task_type": "image",
    "ecs_mode": "create", "ecs_params": {"disk_size": 80, "bandwidth": 5},
    "install_doc": "https://nginx.org/en/docs.html",
}


async def create_task(client, spec=None):
    response = await client.post("/api/tasks", json=spec or BASE_SPEC)
    assert response.status_code == 200, response.text
    return response.json()


async def listed(client, task_id):
    tasks = (await client.get("/api/tasks")).json()["tasks"]
    return next(task for task in tasks if task["task_id"] == task_id)


async def wait_ready(client, run_id):
    for _ in range(100):
        response = await client.get(f"/api/runs/{run_id}")
        if response.json()["status"] == "READY":
            return
        await asyncio.sleep(0.01)
    raise AssertionError("fake deployment did not finish")


async def test_preview_and_edit_only_save_configuration():
    factory = FakeSessionFactory(script=[])
    app = make_test_app(session_factory=factory)
    spec = {**BASE_SPEC, "extra_instructions": "  启用 TLS\n保留安装日志  "}
    async with async_client(app) as client:
        response = await client.post("/api/tasks/preview", json=spec)
        assert response.status_code == 200, response.text
        assert response.json()["prompt"].endswith("补充要求：\n启用 TLS\n保留安装日志")
        assert (await client.get("/api/tasks")).json()["tasks"] == []
        assert not (app.state.test_root / "task").exists()
        task = await create_task(client, spec)
        updated_spec = {**spec, "version": "1.26.0", "extra_instructions": "验证 /health\n保留配置"}
        response = await client.put(f"/api/tasks/{task['task_id']}", json={
            "spec": updated_spec, "expected_updated_at": task["updated_at"],
        })
        assert response.status_code == 200, response.text
        saved = response.json()
        assert saved["task_id"] == task["task_id"]
        assert saved["created_at"] == task["created_at"]
        assert saved["updated_at"] != task["updated_at"]
        assert saved["status"] == "INIT" and saved["run_id"] is None
        assert saved["turn_text"] == ""
        assert saved["spec"]["extra_instructions"] == "验证 /health\n保留配置"
        assert saved["name"].startswith("镜像 nginx 1.26.0 (")
        assert await listed(client, task["task_id"]) == saved
        disk = app.state.test_root / "task" / f"{task['task_id']}.json"
        assert json.loads(disk.read_text()) == saved
        assert (await client.get("/api/runs")).json()["runs"] == []
        assert factory.starts == [] and factory.queries == []
        assert app.state.title_factory.starts == []
    restarted = make_test_app(task_dir=app.state.test_root / "task")
    async with async_client(restarted) as client:
        assert await listed(client, task["task_id"]) == saved


async def test_another_user_can_edit_and_start_with_identical_preview():
    factory = FakeSessionFactory(script=[{"type": "result", "subtype": "success", "result": "完成"}])
    app = make_test_app(session_factory=factory)
    async with async_client(app, "alice") as alice, async_client(app, "bob") as bob:
        task = await create_task(alice)
        spec = {**BASE_SPEC, "extra_instructions": "启用 TLS\n检查 /health"}
        response = await bob.put(f"/api/tasks/{task['task_id']}", json={
            "spec": spec, "expected_updated_at": task["updated_at"],
        })
        assert response.status_code == 200, response.text
        saved = response.json()
        prompt = (await bob.post("/api/tasks/preview", json=saved["spec"])).json()["prompt"]
        response = await bob.post(f"/api/tasks/{task['task_id']}/run", json={
            "expected_updated_at": saved["updated_at"],
        })
        assert response.status_code == 200, response.text
        run_id = response.json()["run_id"]
        await wait_ready(bob, run_id)
        assert factory.queries == [prompt]
        started = await listed(bob, task["task_id"])
        assert started["turn_text"] == prompt
        assert started["spec"] == saved["spec"]
        assert await listed(alice, task["task_id"]) == started
        assert (await alice.get(f"/api/runs/{run_id}")).status_code == 404
        assert (await alice.get("/api/runs")).json()["runs"] == []
        assert (await bob.get(f"/api/runs/{run_id}")).status_code == 200
        disk = app.state.test_root / "task" / f"{task['task_id']}.json"
        assert json.loads(disk.read_text())["turn_text"] == prompt
        changed = await bob.put(f"/api/tasks/{task['task_id']}", json={
            "spec": BASE_SPEC, "expected_updated_at": started["updated_at"],
        })
        assert changed.status_code == 409 and changed.json()["detail"] == "task_not_pending"
        assert (await listed(bob, task["task_id"]))["turn_text"] == prompt


async def test_stale_changes_and_start_do_not_overwrite_or_execute():
    factory = FakeSessionFactory(script=[])
    app = make_test_app(session_factory=factory)
    async with async_client(app, "alice") as alice, async_client(app, "bob") as bob:
        task = await create_task(alice)
        response = await bob.put(f"/api/tasks/{task['task_id']}", json={
            "spec": {**BASE_SPEC, "version": "1.26.0"}, "expected_updated_at": task["updated_at"],
        })
        assert response.status_code == 200, response.text
        saved = response.json()
        stale = await alice.put(f"/api/tasks/{task['task_id']}", json={
            "spec": {**BASE_SPEC, "version": "1.27.0"}, "expected_updated_at": task["updated_at"],
        })
        assert stale.status_code == 409 and stale.json()["detail"] == "task_changed"
        stale = await alice.post(f"/api/tasks/{task['task_id']}/run", json={
            "expected_updated_at": task["updated_at"],
        })
        assert stale.status_code == 409 and stale.json()["detail"] == "task_changed"
        assert await listed(alice, task["task_id"]) == saved
        assert factory.starts == []
        assert (await alice.get("/api/runs")).json()["runs"] == []
        assert (await bob.get("/api/runs")).json()["runs"] == []


async def test_two_starts_create_only_one_session_and_started_edit_is_rejected():
    factory = FakeSessionFactory(script=[{"type": "result", "subtype": "success"}], delay=0.03)
    app = make_test_app(session_factory=factory)
    async with async_client(app, "alice") as alice, async_client(app, "bob") as bob:
        task = await create_task(alice)
        responses = await asyncio.gather(*[
            client.post(f"/api/tasks/{task['task_id']}/run", json={"expected_updated_at": task["updated_at"]})
            for client in (alice, bob)
        ])
        assert sorted(response.status_code for response in responses) == [200, 409]
        assert next(response for response in responses if response.status_code == 409).json()["detail"] == "task_not_pending"
        winner = alice if responses[0].status_code == 200 else bob
        run_id = next(response for response in responses if response.status_code == 200).json()["run_id"]
        response = await bob.put(f"/api/tasks/{task['task_id']}", json={
            "spec": {**BASE_SPEC, "software": "redis"}, "expected_updated_at": task["updated_at"],
        })
        assert response.status_code == 409 and response.json()["detail"] == "task_not_pending"
        await wait_ready(winner, run_id)
        assert len(factory.queries) == 1
        assert len((await alice.get("/api/runs")).json()["runs"]) + len((await bob.get("/api/runs")).json()["runs"]) == 1


async def test_edit_replaces_target_fields_instead_of_retaining_old_instance():
    app = make_test_app()
    async with async_client(app) as client:
        spec = {**BASE_SPEC, "ecs_mode": "existing", "ecs_instance": {
            "id": "old-instance", "name": "ecs-old", "ip": "192.0.2.1",
        }}
        task = await create_task(client, spec)
        assert task["instance_id"] == "old-instance" and task["server_alias"] == "old"
        response = await client.put(f"/api/tasks/{task['task_id']}", json={
            "spec": {**BASE_SPEC, "software": "redis", "task_type": "rpm"},
            "expected_updated_at": task["updated_at"],
        })
        assert response.status_code == 200, response.text
        task = response.json()
        assert task["type"] == "rpm" and task["software"] == "redis"
        assert task["name"].startswith("RPM redis 1.25.3 (")
        assert task["instance_id"] is None and task["server_alias"] is None
        assert task["spec"]["ecs_instance"] is None
        assert task["spec"]["ecs_params"] == BASE_SPEC["ecs_params"]
        response = await client.put(f"/api/tasks/{task['task_id']}", json={
            "spec": {**spec, "ecs_instance": {"id": "new-instance", "name": "ecs-new", "ip": "192.0.2.2"}},
            "expected_updated_at": task["updated_at"],
        })
        assert response.status_code == 200, response.text
        task = response.json()
        assert task["instance_id"] == "new-instance" and task["server_alias"] == "new"
        assert task["spec"]["ecs_params"] is None


async def test_create_edit_and_preview_share_validation():
    app = make_test_app()
    async with async_client(app) as client:
        task = await create_task(client)
        invalid_specs = [
            {**BASE_SPEC, "software": ""},
            {**BASE_SPEC, "ecs_params": {"disk_size": "80GB"}},
            {**BASE_SPEC, "ecs_params": {"bandwidth": 2001}},
            {**BASE_SPEC, "ecs_params": {"disk_size": True}},
            {**BASE_SPEC, "version": "v" * 513},
            {**BASE_SPEC, "extra_instructions": ["invalid"]},
        ]
        for spec in invalid_specs:
            created = await client.post("/api/tasks", json=spec)
            preview = await client.post("/api/tasks/preview", json=spec)
            edited = await client.put(f"/api/tasks/{task['task_id']}", json={
                "spec": spec, "expected_updated_at": task["updated_at"],
            })
            assert created.status_code == preview.status_code == edited.status_code == 422
            assert created.json()["detail"] == preview.json()["detail"] == edited.json()["detail"]
        assert await listed(client, task["task_id"]) == task


async def test_expected_timestamp_requires_number_and_legacy_start_still_works():
    factory = FakeSessionFactory(script=[{"type": "result", "subtype": "success"}])
    app = make_test_app(session_factory=factory)
    async with async_client(app) as client:
        task = await create_task(client)
        for expected in (None, True, "123", float("nan"), float("inf")):
            for path, payload, method in (
                (f"/api/tasks/{task['task_id']}", {"spec": BASE_SPEC, "expected_updated_at": expected}, "PUT"),
                (f"/api/tasks/{task['task_id']}/run", {"expected_updated_at": expected}, "POST"),
            ):
                response = await client.request(method, path, content=json.dumps(payload), headers={"Content-Type": "application/json"})
                assert response.status_code == 422, response.text
                assert response.json()["detail"] == "invalid expected_updated_at"
        missing = await client.put(f"/api/tasks/{task['task_id']}", json={"spec": BASE_SPEC})
        assert missing.status_code == 422
        assert factory.starts == []
        started = await client.post(f"/api/tasks/{task['task_id']}/run")
        assert started.status_code == 200, started.text
        await wait_ready(client, started.json()["run_id"])
        assert len(factory.queries) == 1


async def test_unknown_task_and_unauthenticated_actions():
    app = make_test_app()
    async with async_client(app) as client:
        response = await client.put("/api/tasks/task-missing", json={"spec": BASE_SPEC, "expected_updated_at": 1})
        assert response.status_code == 404
    async with async_client(app, username=None) as client:
        assert (await client.post("/api/tasks/preview", json=BASE_SPEC)).status_code == 401
        assert (await client.put("/api/tasks/task-missing", json={"spec": BASE_SPEC, "expected_updated_at": 1})).status_code == 401


async def test_failed_manual_save_preserves_previous_task_and_does_not_create_phantom():
    app = make_test_app()
    async with async_client(app) as client:
        task = await create_task(client)
        directory = app.state.test_root / "task"
        backup = app.state.test_root / "saved-tasks"
        directory.rename(backup)
        directory.write_text("block task directory")
        try:
            response = await client.put(f"/api/tasks/{task['task_id']}", json={
                "spec": {**BASE_SPEC, "software": "redis"}, "expected_updated_at": task["updated_at"],
            })
            assert response.status_code == 503 and response.json()["detail"] == "task_save_failed"
            assert await listed(client, task["task_id"]) == task
            created = await client.post("/api/tasks", json={**BASE_SPEC, "software": "mysql"})
            assert created.status_code == 503 and created.json()["detail"] == "task_save_failed"
            assert (await client.get("/api/tasks")).json()["tasks"] == [task]
            assert (await client.post("/api/tasks/preview", json=BASE_SPEC)).status_code == 200
        finally:
            directory.unlink()
            backup.rename(directory)
        assert json.loads((directory / f"{task['task_id']}.json").read_text()) == task


async def test_legacy_task_without_extra_instructions_remains_editable():
    app = make_test_app()
    async with async_client(app) as client:
        task = await create_task(client)
    directory = app.state.test_root / "task"
    path = directory / f"{task['task_id']}.json"
    task["spec"].pop("extra_instructions")
    path.write_text(json.dumps(task))
    restarted = make_test_app(task_dir=directory)
    async with async_client(restarted) as client:
        saved = await listed(client, task["task_id"])
        response = await client.post("/api/tasks/preview", json=saved["spec"])
        assert response.status_code == 200 and "补充要求" not in response.json()["prompt"]
        response = await client.put(f"/api/tasks/{task['task_id']}", json={
            "spec": {**saved["spec"], "extra_instructions": "保留日志"},
            "expected_updated_at": saved["updated_at"],
        })
        assert response.status_code == 200, response.text
        assert response.json()["spec"]["extra_instructions"] == "保留日志"


async def test_observer_save_failure_still_does_not_interrupt_execution():
    factory = FakeSessionFactory()
    app = make_test_app(session_factory=factory)
    async with async_client(app) as client:
        task = await create_task(client)
        directory = app.state.test_root / "task"
        backup = app.state.test_root / "saved-tasks"
        directory.rename(backup)
        directory.write_text("block observer persistence")
        try:
            response = await client.post(f"/api/tasks/{task['task_id']}/run")
            assert response.status_code == 200, response.text
            await wait_ready(client, response.json()["run_id"])
            assert (await listed(client, task["task_id"]))["status"] == "DONE"
            assert len(factory.queries) == 1
        finally:
            directory.unlink()
            backup.rename(directory)


async def main():
    tests = [fn for name, fn in sorted(globals().items()) if name.startswith("test_")]
    for fn in tests:
        await fn()
        print(f"ok {fn.__name__}")
    print(f"{len(tests)} passed")


if __name__ == "__main__":
    asyncio.run(main())
