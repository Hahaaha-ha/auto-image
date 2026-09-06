"""FastAPI 应用：创建会话 / 干预（停止·投递·关闭）/ SSE 事件流。

错误统一走 HTTPException 默认响应体；SSE 的 id 即内部事件 seq，
空闲时按 heartbeat_interval 发 `: ping` 注释行保活。

停止的执行动作（session.interrupt）在 intervene 置标记之后由 HTTP 层
调用——标记与 run_agent 的回合收尾在单线程事件循环上互斥，interrupt
晚于回合结束时停止目标已达成，无需把失败放大成错误。
"""
import asyncio
import json
import logging
import subprocess
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import artifacts as artifacts_mod
from . import rebuild as rebuild_mod
from . import redact as redact_mod
from . import runs as runs_mod
from . import sdk as sdk_mod
from . import state as state_mod
from . import title as title_mod
from .events import EventStore
from .runs import RunManager
from .sdk import SDKSessionFactory
from .session import run_agent

# 前端构建产物（vite build 输出），存在才挂载；开发时走 vite dev proxy
DEFAULT_STATIC_DIR = Path(__file__).resolve().parent.parent / "web-ui" / "dist"
# 流水线产物根：deploy（deploy.config.yaml 的 output_dir 固定前缀）+ rpm
# （rpm-build/verify/archive 落盘处）+ rpmcheck（rpm-check / hce-rpm-check
# 按目标镜像分目录的报告树，如 rpmcheck/hce_3p0/…、rpmcheck/openeuler_22p03/…）。
# hce 为旧版 hce-rpm-check 目录的兼容根（历史产物仍可见，缺失则如实缺席）。
# 键即清单组与 URL 里的根前缀段，Agent cwd 即项目根
DEFAULT_ARTIFACT_ROOTS = {
    "deploy": Path(__file__).resolve().parent.parent / "deploy",
    "rpm": Path(__file__).resolve().parent.parent / "rpm",
    "rpmcheck": Path(__file__).resolve().parent.parent / "rpmcheck",
    "hce": Path(__file__).resolve().parent.parent / "hce",
}
# 产物文件名约定的权威源（见 artifacts.load_file_stages）
DEFAULT_DEPLOY_CONFIG = Path(__file__).resolve().parent.parent / "deploy.config.yaml"
# 运行时真实凭据源（ak/sk/ECS 密码值进脱敏已知清单，见 redact.load_scope_secrets）
DEFAULT_SCOPE_CONFIG = Path(__file__).resolve().parent.parent / "scope.yaml"
# 挂起会话的落盘簿记（服务重启恢复可聊；同一 HOME 下多实例共用一份）
DEFAULT_STATE_PATH = Path.home() / ".auto-image-web" / "state.json"


def create_app(session_factory=None, heartbeat_interval=15.0, static_dir=None,
               artifact_roots=None, deploy_config=None, scope_config=None,
               list_sessions_fn=None, get_session_messages_fn=None, residual_cli_scan=None,
    state_path=None, title_factory=None):
    """session_factory 可注入：生产为 ClaudeSDKClient 真实现（默认），
    测试注入按剧本推消息的假实现——注入边界即唯一测试缝。artifact_roots
    （根名 → 目录映射）与 deploy_config 同理注入（产物目录与文件名约定
    造桩用），默认项目根下。
    scope_config 为脱敏已知值清单的凭据源（测试传造桩，不载真实凭据）。

    list_sessions_fn / get_session_messages_fn 注入假历史（重启重建测试缝），
    residual_cli_scan 注入残留 CLI 检测（pgrep 告警测试缝），默认生产实现。
    state_path 为簿记落盘路径（恢复测试缝），默认 HOME 下固定位置。
    title_factory 为标题生成会话工厂（测试缝；生产为独立 cwd 的隔离配置，
    transcript 不落项目根、不进重启重建的发现层）。"""
    # 已知凭据值入脱敏清单（幂等；scope 缺失时只剩形状正则防线）
    redact_mod.load_scope_secrets(scope_config or DEFAULT_SCOPE_CONFIG)
    app = FastAPI(title="auto-image deploy web")
    manager = RunManager()
    store = EventStore()
    store.bind_runs(manager.runs)
    factory = session_factory or SDKSessionFactory()
    titles = title_factory or sdk_mod.TitleSessionFactory()
    artifact_roots = {name: Path(p) for name, p in (artifact_roots or DEFAULT_ARTIFACT_ROOTS).items()}
    file_stages = artifacts_mod.load_file_stages(deploy_config or DEFAULT_DEPLOY_CONFIG)
    app.state.run_manager = manager
    app.state.event_store = store
    app.state.session_factory = factory
    app.state.heartbeat_interval = heartbeat_interval
    state_file = Path(state_path) if state_path is not None else DEFAULT_STATE_PATH

    def persist():
        """状态变更点统一落盘（全量原子替换，见 state.save_state）。"""
        state_mod.save_state(manager.runs.values(), state_file)

    def maybe_assign_title(run, text, is_first):
        """新对话的首条指令到达即起标题生成（Codex 同构：不等回合完成）。
        is_first 由调用方在 intervene 前快照（intervene 首条指令写
        first_prompt，事后无法判定）——续聊/接续/重启恢复的老会话一律不再
        生成（否则续聊指令被总结成「继续执行任务」类标题）。"""
        if is_first:
            return asyncio.create_task(
                title_mod.assign_title(run, text, titles, store, on_change=persist)
            )
        return None

    # 服务重启语义：簿记里的挂起会话先恢复（可聊、resume 重建连接），CLI 侧
    # transcript 再重建其余历史（只读回看 + 续接起点）；正在执行的任务不自动
    # 重试（RUNNING 降级挂起 + run.interrupted 提示）；残留 CLI 子进程只告警
    # 不杀（可能处于云操作中间态）
    restored = rebuild_mod.restore_active_runs(
        manager, store,
        state_mod.load_state(state_file),
        get_session_messages_fn or sdk_mod.project_session_messages,
    )
    if restored:
        logging.getLogger("web").info("服务重启后恢复 %d 条挂起会话（可继续对话）", len(restored))
        for run in restored:
            run.task = asyncio.create_task(run_agent(run, factory, store, on_change=persist))
    rebuilt = rebuild_mod.rebuild_history(
        manager, store,
        list_sessions_fn or sdk_mod.list_project_sessions,
        get_session_messages_fn or sdk_mod.project_session_messages,
        skip_sessions={r.session_id for r in restored},
    )
    if rebuilt:
        logging.getLogger("web").info("服务重启后找回 %d 条历史会话", len(rebuilt))
    residual_pids = (residual_cli_scan or residual_cli_processes)()
    if residual_pids:
        logging.getLogger("web").warning(
            "检测到残留 CLI 子进程（不自动处理，可能处于云操作中间态，请人工处置）：%s",
            residual_pids,
        )

    @app.get("/api/runs")
    async def list_runs():
        return {"runs": manager.summaries()}

    @app.post("/api/runs")
    async def create_run(body: dict | None = None):
        resume_from = (body or {}).get("resume_from")
        if resume_from is not None and manager.get(resume_from) is None:
            raise HTTPException(status_code=404, detail="run not found")
        try:
            run = manager.create(resume_from)
        except runs_mod.Conflict as exc:
            raise HTTPException(status_code=409, detail=exc.detail) from exc
        store.create(run.run_id)
        # 会话流同步开卷：run.started 先行；接续创建时带入源会话全部历史
        # （CLI resume 的浏览体验），seq 重新编号、断点续传语义不变
        store.append(run.run_id, "run.started", {})
        if run.resumed_from is not None:
            store.append(run.run_id, "resumed.history", {"resumed_from": run.resumed_from})
            # 源流的生命周期事件不转录：源的起点/接续标记/收尾都不是新会话的
            # 状态——终态收尾被前端当成本 run 的终态会关流判死，接续后无法续聊
            store.adopt_history(
                run.run_id, run.resumed_from,
                skip_types={"run.started", "run.canceled", "run.failed", "run.ended", "resumed.history"},
            )
        run.task = asyncio.create_task(run_agent(run, factory, store, on_change=persist))
        persist()
        return {"run_id": run.run_id, "status": run.status, "resumed_from": run.resumed_from}

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: str):
        return _get_run_or_404(manager, run_id).summary()

    @app.post("/api/runs/{run_id}/messages")
    async def send_message(run_id: str, body: dict):
        run = _get_run_or_404(manager, run_id)
        text = (body or {}).get("text")
        if not isinstance(text, str) or not text.strip():
            raise HTTPException(status_code=422, detail="text required")
        is_first = run.first_prompt is None  # 快照先于 intervene（它写 first_prompt）
        try:
            # 本会话执行中：intervene 先请求停止，本端点返回后执行打断
            manager.intervene(run, text)
        except runs_mod.Conflict as exc:
            raise HTTPException(status_code=409, detail=exc.detail) from exc
        persist()
        maybe_assign_title(run, text, is_first)
        await _interrupt_if_requested(run)
        return {"run_id": run.run_id, "status": run.status}

    @app.post("/api/runs/{run_id}/stop")
    async def stop_run(run_id: str, body: dict | None = None):
        run = _get_run_or_404(manager, run_id)
        text = (body or {}).get("text")
        if text is not None and (not isinstance(text, str) or not text.strip()):
            raise HTTPException(status_code=422, detail="text must be non-empty")
        is_first = run.first_prompt is None and text is not None
        try:
            manager.intervene(run, text)
        except runs_mod.Conflict as exc:
            raise HTTPException(status_code=409, detail=exc.detail) from exc
        persist()
        maybe_assign_title(run, text, is_first)
        await _interrupt_if_requested(run)
        return {"run_id": run.run_id, "status": run.status}

    @app.post("/api/runs/{run_id}/cancel")
    async def cancel_run(run_id: str):
        run = _get_run_or_404(manager, run_id)
        try:
            manager.cancel(run)
        except runs_mod.Conflict as exc:
            raise HTTPException(status_code=409, detail=exc.detail) from exc
        run.task.cancel()
        try:
            await run.task  # 等收尾（run.canceled 已入事件流）再返回
        except asyncio.CancelledError:
            pass
        persist()
        return {"run_id": run.run_id, "status": run.status}

    # 产物浏览不依赖会话存在（deploy/ + rpm/ 全量镜像，含历史轮次）
    @app.get("/api/artifacts")
    async def list_artifacts():
        return artifacts_mod.browse(artifact_roots, file_stages)

    # /file/ 前缀段：{rel_path:path} 可匹配空串，无前缀段会与清单端点路由歧义
    @app.get("/api/artifacts/file/{rel_path:path}")
    async def read_artifact(rel_path: str):
        found = artifacts_mod.read(artifact_roots, file_stages, rel_path)
        if found is None:
            raise HTTPException(status_code=404, detail="artifact not found")
        entry, target = found
        if entry.get("binary"):  # 二进制产物无文本内容，指引到下载端点
            raise HTTPException(
                status_code=422,
                detail=f"binary artifact; use /api/artifacts/download/{rel_path}",
            )
        try:
            content = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):  # 二进制产物按不可读处理，不 500
            raise HTTPException(status_code=404, detail="artifact not found") from None
        return {**entry, "content": content}

    # 单文件下载：原始字节走附件响应（浏览端点只读文本，.sh 等非文本与
    # 将来的二进制产物从这里拿全量原文件）
    @app.get("/api/artifacts/download/{rel_path:path}")
    async def download_artifact(rel_path: str):
        found = artifacts_mod.resolve(artifact_roots, file_stages, rel_path)
        if found is None:
            raise HTTPException(status_code=404, detail="artifact not found")
        _entry, target = found
        return FileResponse(target, filename=target.name)

    # 批量打包下载：POST {"paths": [根前缀相对路径…]} → 一个 zip（保留
    # deploy/…、rpm/… 目录树；越界/缺失项如实跳过，一个都收不到 404）
    @app.post("/api/artifacts/zip")
    async def zip_artifacts(body: dict | None = None):
        paths = (body or {}).get("paths")
        if not isinstance(paths, list) or not paths or not all(
            isinstance(p, str) and p for p in paths
        ):
            raise HTTPException(status_code=422, detail="paths required")
        stream, count = artifacts_mod.zip_files(artifact_roots, file_stages, paths)
        if stream is None:
            raise HTTPException(status_code=404, detail="no artifacts to zip")
        name = f"auto-image-artifacts-{count}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"
        return StreamingResponse(
            stream,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )

    @app.get("/api/runs/{run_id}/events")
    async def event_stream(run_id: str, request: Request):
        run = _get_run_or_404(manager, run_id)
        seen = _parse_last_event_id(request.headers.get("Last-Event-ID"))

        async def generate():
            nonlocal seen
            with store.subscribe(run_id) as flag:
                while True:
                    flag.clear()
                    for event in store.replay_from(run_id, seen):
                        seen = event["seq"]
                        yield _sse_chunk(event)
                    # 终态且历史重放完毕：正常结束流
                    if run.status in runs_mod.TERMINAL and store.is_complete(run_id, seen):
                        return
                    try:
                        await asyncio.wait_for(flag.wait(), timeout=heartbeat_interval)
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    directory = static_dir or DEFAULT_STATIC_DIR
    if Path(directory).is_dir():
        app.mount("/", StaticFiles(directory=str(directory), html=True), name="ui")

    return app


async def _interrupt_if_requested(run):
    """执行 intervene 排队的打断。回合可能刚好已自然结束（停止目标视为
    达成）、会话可能尚未建立（创建后立即干预的窗口），两种情形均跳过；
    打断本身失败不改变服务端权威状态（回合如何收尾以事件流为准）。"""
    if not run.stop_requested or run.session is None:
        return
    try:
        await run.session.interrupt()
    except Exception:  # noqa: BLE001 —— 外部打断失败不放大为 HTTP 错误
        pass


def _sse_chunk(event):
    # ts 随 payload 下发（前端时长的冻结点），seq 走 SSE id 维持断点续传
    data = json.dumps({**event["payload"], "ts": event["ts"]}, ensure_ascii=False)
    return f"id: {event['seq']}\nevent: {event['type']}\ndata: {data}\n\n"


def _parse_last_event_id(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _get_run_or_404(manager, run_id):
    run = manager.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return run


def residual_cli_processes():
    """pgrep -f claude 检测残留 CLI 子进程，返回 pid 列表；无匹配或 pgrep 缺失为空。

    只发现不处置：残留进程可能正处于云操作中间态，杀不杀由人工判断。
    """
    try:
        proc = subprocess.run(["pgrep", "-f", "claude"], capture_output=True, text=True)
    except OSError:
        return []
    return proc.stdout.split() if proc.returncode == 0 else []
