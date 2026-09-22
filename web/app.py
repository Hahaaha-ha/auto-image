"""FastAPI 应用：创建会话 / 发送·停止·克隆·结束 / SSE 事件流。

错误统一走 HTTPException 默认响应体。事件通道两条：per-run 端点是纯
快照——SSE 的 id 即内部事件 seq，按 Last-Event-ID 重放历史、重放完毕正常
结束响应；全局流常驻广播全部会话的实时事件，空闲按 heartbeat_interval
发 `: ping` 注释行保活。

停止的执行动作（session.interrupt）在 stop_requested 置位之后由 HTTP 层
调用；连接仍在建立时只保留停止意图，run_turn 会在 query 前消费。标记与
回合收尾在单线程事件循环上互斥，interrupt 晚于回合结束时停止目标已达成，
无需把失败放大成错误。

end 的收尾序列（RUNNING 中）：end 校验 → 取消在飞回合任务（回合不补
收尾事件）→ session.ended 作为流的最后一条事件 → 墓碑入册。快照端点
不替前端判终态：session.ended 本身在历史里，重放完毕自然断开。
"""
import asyncio
import hashlib
import json
import logging
import os
import secrets
import subprocess
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import artifacts as artifacts_mod
from . import audit as audit_mod
from . import auth as auth_mod
from . import ecs as ecs_mod
from . import obs as obs_mod
from . import rebuild as rebuild_mod
from . import redact as redact_mod
from . import runs as runs_mod
from . import sdk as sdk_mod
from . import state as state_mod
from . import tasks as tasks_mod
from . import title as title_mod
from .events import EventStore
from .runs import ENDED, RunManager
from .sdk import SDKSessionFactory
from .session import run_turn

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
# 任务跟踪存储（每任务一个 JSON，重启恢复；运行数据不入库）
DEFAULT_TASK_DIR = Path(__file__).resolve().parent.parent / "task"
# 恢复簿记（墓碑 + 身份映射 + 克隆链镜像；同一 HOME 下多实例共用一份）
DEFAULT_STATE_PATH = Path.home() / ".auto-image-web" / "state.json"
# 并发上限（数执行中回合；新建、克隆、标题生成不占名额）
DEFAULT_MAX_PARALLEL_RUNS = 10
# 用户清单与控制审计（多用户隔离基础；审计按天轮转默认留 90 天）
DEFAULT_USERS_PATH = auth_mod.DEFAULT_USERS_PATH
DEFAULT_AUDIT_DIR = Path.home() / ".auto-image-web" / "audit"
# legacy 无 owner 会话的迁移归属（部署配置显式指定；不从用户清单推断）
DEFAULT_OWNER = os.environ.get("WEB_DEFAULT_OWNER", "admin")

# 认证旁路：登录端点与静态壳（index.html/JS/CSS——不含业务数据，前端壳
# 加载后自己查 /api/auth/me 决定登录壳还是数据面）；其余一切 API、全局
# SSE、产物、OBS、ECS 端点默认 401
PUBLIC_PATH_PREFIXES = ("/api/auth/login",)

# 状态修改请求的同源判定：Host 头与 Origin 不同源即 403（Cookie SameSite=Lax
# 之外的服务端防线）。非浏览器客户端不带 Origin，不受约束。
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
# 拒绝原因判定值（审计 reason 与测试断言同源引用）；actor 未知时的占位
REASON_CROSS_ORIGIN = "cross_origin"
REASON_NOT_OWNER = "not_owner"
REASON_NOT_ADMIN = "not_admin"
UNKNOWN_ACTOR = "-"


def create_app(session_factory=None, heartbeat_interval=15.0, static_dir=None,
               artifact_roots=None, deploy_config=None, scope_config=None,
               list_sessions_fn=None, get_session_messages_fn=None, transcript_times_fn=None,
               residual_cli_scan=None,
               obs_list_fn=None, obs_url_fn=None, obs_read_fn=None, obs_archive_fn=None,
               obs_upload_zip_fn=None, obs_health_fn=None,
               ecs_list_fn=None, ecs_check_fn=None, ecs_create_fn=None, ecs_defaults_fn=None,
    state_path=None, title_factory=None, max_parallel_runs=None, task_dir=None,
    users_path=None, audit_dir=None, auth_secret=None, default_owner=None):
    """session_factory 可注入：生产为 ClaudeSDKClient 真实现（默认），
    测试注入按剧本推消息的假实现——注入边界即唯一测试缝。artifact_roots
    （根名 → 目录映射）与 deploy_config 同理注入（产物目录与文件名约定
    造桩用），默认项目根下。
    scope_config 为脱敏已知值清单的凭据源（测试传造桩，不载真实凭据）。
    list_sessions_fn / get_session_messages_fn 注入假历史（重启重建测试缝），
    transcript_times_fn 注入假时刻表（重放事件时刻透传的测试缝，形状
    session_id → {uuid: epoch 秒}），residual_cli_scan 注入残留 CLI 检测
    （pgrep 告警测试缝），默认生产实现。
    state_path 为簿记落盘路径（恢复测试缝），默认 HOME 下固定位置。
    title_factory 为标题生成会话工厂（测试缝；生产为独立 cwd 的隔离配置，
    transcript 不落项目根、不进重启恢复的发现层）。
    max_parallel_runs 为并发上限（默认 WEB_MAX_PARALLEL_RUNS 环境变量，
    缺省 10；测试注入收紧）。
    users_path 为用户清单（默认项目根 users.yaml；mtime 热载），audit_dir
    为控制审计目录（默认 HOME 下固定位置），auth_secret 为认证签名密钥
    （默认 WEB_AUTH_SECRET，再缺省机器派生——三者皆为测试缝）。default_owner
    为 legacy 无 owner 会话的迁移归属（默认 WEB_DEFAULT_OWNER，再缺省
    admin；测试缝）。"""
    # 已知凭据值入脱敏清单（幂等；scope 缺失时只剩形状正则防线）
    redact_mod.load_scope_secrets(scope_config or DEFAULT_SCOPE_CONFIG)
    app = FastAPI(title="auto-image deploy web")
    # 认证与控制审计基础：文件用户清单（热载）+ HMAC Cookie + 追加式审计
    roster = auth_mod.UserRoster(users_path or DEFAULT_USERS_PATH)
    secret = auth_mod.resolve_secret(auth_secret)
    audit = audit_mod.ControlAudit(audit_dir or DEFAULT_AUDIT_DIR)
    app.state.users = roster
    app.state.auth_secret = secret
    app.state.audit = audit

    @app.middleware("http")
    async def auth_gate(request: Request, call_next):
        """统一认证入口：除登录端点与静态壳外一切请求都要求有效登录
        Cookie——未认证客户端读不到任何业务数据（壳本身不含数据）。

        状态修改请求（非安全方法）另做同源 Origin 校验。/api/auth/me 不在
        旁路名单——它就是「当前身份」探针，未登录 401 即答案本身。
        """
        path = request.url.path
        if path.startswith(PUBLIC_PATH_PREFIXES):
            return await _login_guard(request, call_next)
        if not path.startswith("/api/"):
            # 静态资源（登录壳的载体）放行；业务面全在 /api/ 下
            return await call_next(request)
        cookie = request.cookies.get(auth_mod.COOKIE_NAME)
        username, reason = (None, auth_mod.REASON_BAD_COOKIE)
        if cookie:
            username, reason = auth_mod.verify_token(secret, cookie, roster)
        if username is None:
            return _unauthorized(reason)
        request.state.username = username
        if request.method not in SAFE_METHODS and not _same_origin(request):
            return JSONResponse(status_code=403, content={"detail": "cross-origin rejected"})
        return await call_next(request)

    def _unauthorized(reason):
        # 401 响应带可识别原因（前端据此回登录页，不无限重连 SSE）
        return JSONResponse(status_code=401, content={"detail": reason})

    def _same_origin(request: Request) -> bool:
        origin = request.headers.get("origin")
        if not origin:
            return True  # 非浏览器客户端（curl / SDK）无 Origin，放行
        host = request.headers.get("host", "")
        try:
            parsed = urlparse(origin)
        except ValueError:
            return False
        return parsed.netloc == host and origin == f"{parsed.scheme}://{parsed.netloc}"

    def audit_best_effort(**kwargs):
        """尽力而为审计（登录失败/被拒类无状态变更动作）：写失败只告警，
        拒绝响应照常返回——审计不可用不能把拒绝变成 500。"""
        try:
            audit.record(**kwargs)
        except audit_mod.AuditWriteError:
            logging.getLogger("web").warning("审计写入失败（尽力而为路径）", exc_info=True)

    async def _login_guard(request: Request, call_next):
        """登录端点：不要求 Cookie，但要求同源（被拒尝试入审计）。"""
        if request.method == "POST" and not _same_origin(request):
            audit_best_effort(actor=UNKNOWN_ACTOR, action="login", result="failure",
                              request_id=request_id(request), reason=REASON_CROSS_ORIGIN)
            return JSONResponse(status_code=403, content={"detail": "cross-origin rejected"})
        return await call_next(request)

    def request_id(request: Request) -> str:
        return request.headers.get("x-request-id") or secrets.token_hex(8)

    limit = max_parallel_runs if max_parallel_runs is not None else int(
        os.environ.get("WEB_MAX_PARALLEL_RUNS", DEFAULT_MAX_PARALLEL_RUNS))
    manager = RunManager(max_parallel=limit)
    store = EventStore()
    store.bind_runs(manager.runs)
    factory = session_factory or SDKSessionFactory()
    titles = title_factory or sdk_mod.TitleSessionFactory()
    artifact_roots = {name: Path(p) for name, p in (artifact_roots or DEFAULT_ARTIFACT_ROOTS).items()}
    file_stages = artifacts_mod.load_file_stages(deploy_config or DEFAULT_DEPLOY_CONFIG)
    # OBS 对象浏览：默认实现绑定真实 scope（与脱敏同源），测试注入假函数
    # （不触网）。SDK 缺失或未配置在请求时报 503，不影响启动与其它功能。
    obs_scope = Path(scope_config or DEFAULT_SCOPE_CONFIG)
    obs_list = obs_list_fn or (lambda limit=1000: obs_mod.list_objects(obs_scope, limit))
    obs_url = obs_url_fn or (lambda key="", expires=3600: obs_mod.signed_url(obs_scope, key, expires))
    obs_read = obs_read_fn or (lambda key="": obs_mod.read_text(obs_scope, key))
    obs_archive = obs_archive_fn or (lambda items: obs_mod.upload_paths(obs_scope, items))
    obs_upload_zip = obs_upload_zip_fn or (lambda key, data: obs_mod.upload_bytes(obs_scope, key, data))
    obs_health = obs_health_fn or (lambda: obs_mod.health_check(obs_scope))
    # ECS 实例面板：默认实现绑定同一 scope（顶层 ak/sk/region + ecs_create
    # 段），测试注入假函数（不触云）。SDK 缺失或未配置在请求时报 503，
    # 不影响启动与其它功能。
    ecs_scope = obs_scope
    ecs_list_impl = ecs_list_fn or (lambda limit=500: ecs_mod.list_instances(ecs_scope, limit))
    ecs_check_impl = ecs_check_fn or (lambda: ecs_mod.check_all(ecs_scope))
    ecs_create_impl = ecs_create_fn or (lambda body=None: ecs_mod.create_instance(ecs_scope, body))
    ecs_defaults_impl = ecs_defaults_fn or (lambda: ecs_mod.resolve_defaults(ecs_scope))
    app.state.run_manager = manager
    app.state.event_store = store
    app.state.session_factory = factory
    app.state.heartbeat_interval = heartbeat_interval
    state_file = Path(state_path) if state_path is not None else DEFAULT_STATE_PATH

    # 克隆链镜像（session_id → 来源 run_id）：transcript 里没有克隆血缘，
    # 簿记撤销后克隆链父指针无从找回——克隆挂 run.clone_source，persist 时
    # （首回合接受并预分配 session_id 后）随身份映射一并入册；启动时播种
    clone_sources = {}

    def persist():
        """状态变更点统一落盘（墓碑 + 身份映射 + 克隆链镜像，全量原子替换，
        见 state.save_state）。"""
        for r in manager.runs.values():
            if r.clone_source and r.session_id:
                clone_sources[r.session_id] = r.clone_source
        state_mod.save_state(manager.runs.values(), state_file, clone_sources)

    def maybe_assign_title(run, text, is_first):
        """新对话的首条指令到达即起标题生成（Codex 同构：不等回合完成）。
        is_first 由调用方在回合开卷前快照（commit_turn 首条指令写
        first_prompt，事后无法判定）——续聊/克隆/重启恢复的老会话一律不再
        生成（否则续聊指令被总结成「继续执行任务」类标题）。"""
        if is_first:
            return asyncio.create_task(
                title_mod.assign_title(run, text, titles, store, on_change=persist)
            )
        return None

    def start_turn(run, text):
        """起回合任务（回合校验通过并置位后调用）：按回合开合连接，
        收尾即散；任务引用挂 run 供 stop / end 定向。"""
        run.turn_task = asyncio.create_task(run_turn(run, text, factory, store, on_change=persist))

    # 服务重启语义：全量 transcript 重放恢复所有会话（可续聊），state 簿记
    # （墓碑 + 身份映射 + 克隆链镜像）叠加；未收尾回合补 turn.interrupted
    # 提示，不自动重试；残留 CLI 子进程只告警不杀（可能处于云操作中间态）
    bookkeeping = state_mod.load_state(state_file)
    clone_sources.update(bookkeeping["clone_sources"])
    restored = rebuild_mod.recover_sessions(
        manager, store,
        list_sessions_fn or sdk_mod.list_project_sessions,
        get_session_messages_fn or sdk_mod.project_session_messages,
        bookkeeping,
        transcript_times=transcript_times_fn or sdk_mod.transcript_times,
    )
    if restored:
        logging.getLogger("web").info("服务重启后重放恢复 %d 条会话（可续聊）", len(restored))
    # legacy 迁移：簿记无 owner 记录的重放会话归默认 owner（历史数据有确定
    # 归属，部署配置显式指定、不从用户清单推断）；现代记录缺 owner 的严格化
    # 处理归后续恢复切片
    legacy_owner = default_owner or DEFAULT_OWNER
    # OBS 全局配置的唯一写权限人（部署管理员）：与 legacy 迁移归属同一
    # 显式配置源（WEB_DEFAULT_OWNER，缺省 admin），语义独立——普通用户
    # 读共享、写 403
    obs_admin = default_owner or DEFAULT_OWNER
    for r in restored:
        if r.owner is None:
            r.owner = legacy_owner
    residual_pids = (residual_cli_scan or residual_cli_processes)()
    if residual_pids:
        logging.getLogger("web").warning(
            "检测到残留 CLI 子进程（不自动处理，可能处于云操作中间态，请人工处置）：%s",
            residual_pids,
        )

    # 任务跟踪（rpm-*/deploy-* 流水线）：恢复重放完成后注册观察者——重放
    # 事件不进任务面（历史不是新事实）；重启前未收尾的任务在 recover 里按
    # interrupted 定格。观察者异常被 EventStore 吞掉，绝不影响回合执行。
    task_store = tasks_mod.TaskStore(
        Path(task_dir) if task_dir is not None else DEFAULT_TASK_DIR, artifact_roots)
    task_store.recover()
    store.add_observer(task_store.handle_event)
    app.state.task_store = task_store

    # ---------- 认证端点 ----------

    # 登录：口令校验通过先落审计（审计写失败 503、不发 Cookie），再签发
    # Cookie。失败统一 401（不区分用户不存在/口令错/禁用，防探测；原因
    # 只入审计）。
    @app.post("/api/auth/login")
    async def login(request: Request):
        body = await _json_body(request)
        username = body.get("username")
        password = body.get("password")
        if not isinstance(username, str) or not isinstance(password, str) or not username:
            raise HTTPException(status_code=422, detail="username and password required")
        ok, reason = roster.check_password(username, password)
        if not ok:
            audit_best_effort(actor=username, action="login", result="failure",
                              request_id=request_id(request), reason=reason)
            raise HTTPException(status_code=401, detail="invalid credentials")
        _audit_or_503(actor=username, action="login", result="success",
                      request_id=request_id(request))
        token = auth_mod.issue_token(secret, username, roster.fingerprint(username),
                                     auth_mod.COOKIE_TTL_SECONDS)
        response = JSONResponse({"username": username})
        response.set_cookie(
            auth_mod.COOKIE_NAME, token,
            max_age=auth_mod.COOKIE_TTL_SECONDS, httponly=True, samesite="lax",
        )
        return response

    # 登出：清 Cookie（过期即弃）并审计；审计写失败同样 503（状态变更类）
    @app.post("/api/auth/logout")
    async def logout(request: Request):
        _audit_or_503(actor=request.state.username, action="logout", result="success",
                      request_id=request_id(request))
        response = JSONResponse({"ok": True})
        response.delete_cookie(auth_mod.COOKIE_NAME)
        return response

    # 当前身份探针：前端启动先问它，200 才建全局流；未登录 401（middleware
    # 已拦，这里只会是已登录分支）
    @app.get("/api/auth/me")
    async def whoami(request: Request):
        return {"username": request.state.username}

    def _audit_or_503(**kwargs):
        """状态变更类动作的审计前置：写失败抛 503（HTTPException 由调用方
        向上冒泡），动作不执行。"""
        try:
            audit.record(**kwargs)
        except audit_mod.AuditWriteError:
            raise HTTPException(status_code=503, detail="audit unavailable")

    def _audit_denied(request: Request, action: str, run=None, reason: str = REASON_NOT_OWNER):
        """被拒绝的控制动作入审计（尽力而为）：写失败只告警，拒绝响应照常
        返回——拒绝不改变状态，审计不可用不能把拒绝放大成 500。默认场景是
        owner 校验拒绝（404），状态机拒绝（409）由调用方带 reason。"""
        audit_best_effort(actor=request.state.username, action=action,
                          result="denied", request_id=request_id(request),
                          run_id=run.run_id if run is not None else None,
                          run_owner=run.owner if run is not None else None,
                          reason=reason)

    def _owned_run_or_404(request: Request, run_id: str, action: str | None = None):
        """owner 校验统一入口：未知 run 与非 owner run 同一 404（不泄露会话
        存在性）。action 给定时是控制入口（发送/停止/Fork/结束）——拒绝先入
        审计（尽力而为）再 404；只读入口（摘要/快照）不产生拒绝审计。
        审计里区分未知 run 与他人 run 没有意义（对外同为 404），统一
        not_owner。"""
        run = manager.get(run_id)
        if run is None or run.owner != request.state.username:
            if action is not None:
                _audit_denied(request, action, run=run)
            raise HTTPException(status_code=404, detail="run not found")
        return run

    async def _json_body(request: Request):
        try:
            body = await request.json()
        except ValueError:
            return {}
        return body if isinstance(body, dict) else {}

    @app.get("/api/runs")
    async def list_runs(request: Request):
        return {"runs": manager.summaries(owner=request.state.username)}

    @app.post("/api/runs")
    async def create_run(request: Request, body: dict | None = None):
        # owner 由服务端从登录身份注入（客户端请求体任何字段都不构成归属
        # 声明）；draft 不注册，审计成功才入册——审计失败不留下半创建会话
        run = manager.draft(owner=request.state.username)
        _audit_or_503(actor=request.state.username, action="create_run",
                      result="success", request_id=request_id(request),
                      run_id=run.run_id, run_owner=run.owner)
        manager.register(run)
        store.create(run.run_id)
        # 会话流同步开卷：session.started 先行（无历史转录——续接语义已由
        # clone 承担，新建即全新会话）
        store.append(run.run_id, "session.started", {})
        persist()
        return {"run_id": run.run_id, "status": run.status, "resumed_from": run.resumed_from}

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: str, request: Request):
        run = _owned_run_or_404(request, run_id)
        return run.summary()

    # 任务清单（无会话依赖，同产物清单）：rpm-*/deploy-* 流水线任务的
    # 注册表视图，事件观察者实时维护
    @app.get("/api/tasks")
    async def list_tasks():
        return {"tasks": task_store.list()}

    # 手动建任务（面板「+ 新建任务」）：表单 spec → INIT 待运行态落盘；
    # 「▶ 运行」才建会话发指令。输入非法 422。
    @app.post("/api/tasks")
    async def create_task(body: dict | None = None):
        b = body or {}
        spec = {}
        software = b.get("software")
        if not isinstance(software, str) or not software.strip() or len(software.strip()) > 64:
            raise HTTPException(status_code=422, detail="software required (1-64 chars)")
        spec["software"] = software.strip()
        for field in ("version", "install_doc"):
            val = b.get(field)
            if val is not None:
                if not isinstance(val, str) or len(val) > 512:
                    raise HTTPException(status_code=422, detail=f"invalid {field}")
                spec[field] = val.strip()
        task_type = b.get("task_type", "image")
        if task_type not in ("image", "rpm"):
            raise HTTPException(status_code=422, detail="task_type must be image|rpm")
        spec["task_type"] = task_type
        ecs_mode = b.get("ecs_mode", "create")
        if ecs_mode not in ("existing", "create"):
            raise HTTPException(status_code=422, detail="ecs_mode must be existing|create")
        spec["ecs_mode"] = ecs_mode
        if ecs_mode == "existing":
            inst = b.get("ecs_instance")
            if (not isinstance(inst, dict)
                    or not str(inst.get("name") or "").strip()
                    or not str(inst.get("ip") or "").strip()):
                raise HTTPException(
                    status_code=422, detail="ecs_instance {name, ip} required for existing mode")
            spec["ecs_instance"] = {
                "id": str(inst.get("id") or "").strip() or None,
                "name": str(inst["name"]).strip(),
                "ip": str(inst["ip"]).strip(),
            }
        else:
            params = b.get("ecs_params")
            if params is None:
                params = {}
            if not isinstance(params, dict):
                raise HTTPException(status_code=422, detail="ecs_params must be an object")
            clean_params = {}
            for field in ("image", "flavor", "name"):
                val = params.get(field)
                if val is not None:
                    if not isinstance(val, str) or not val.strip() or len(val.strip()) > 128:
                        raise HTTPException(status_code=422, detail=f"invalid ecs_params.{field}")
                    clean_params[field] = val.strip()
            for field, lo, hi in (("disk_size", 10, 1024), ("bandwidth", 1, 2000)):
                val = params.get(field)
                if val is not None:
                    if isinstance(val, bool) or not isinstance(val, int) or not lo <= val <= hi:
                        raise HTTPException(
                            status_code=422, detail=f"invalid ecs_params.{field} (int {lo}-{hi})")
                    clean_params[field] = val
            spec["ecs_params"] = clean_params
        return task_store.create_manual(spec)

    # 运行 INIT 任务：新建会话 → 按表单 spec 构造部署指令发出 → 任务绑定
    # 该会话转 RUNNING（mark_running 先于 start_turn，首个 stage.changed
    # 即认领本任务）。非 INIT（已运行/已收尾）409；未知 404。
    @app.post("/api/tasks/{task_id}/run")
    async def run_task(task_id: str, request: Request):
        task = task_store.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        if task.get("status") != "INIT":
            raise HTTPException(
                status_code=409, detail=f"task not in INIT (current: {task.get('status')})")
        prompt = tasks_mod.build_task_prompt(task.get("spec") or {})
        # 会话创建与首回合：owner 注入 + 审计前置（与 create_run / send 同构）
        run = manager.draft(owner=request.state.username)
        try:
            manager.check_turn(run)
        except runs_mod.Conflict as exc:
            _audit_denied(request, "create_run", run, reason=exc.detail)
            raise HTTPException(status_code=409, detail=exc.detail) from exc
        _audit_or_503(actor=request.state.username, action="create_run",
                      result="success", request_id=request_id(request),
                      run_id=run.run_id, run_owner=run.owner)
        manager.register(run)
        store.create(run.run_id)
        store.append(run.run_id, "session.started", {})
        manager.commit_turn(run, prompt)
        # 首回合发送与 send 同构：send 审计前置（此时回合已必然可开——
        # 新建会话刚创建，check_turn 已过）
        _audit_or_503(actor=request.state.username, action="send", result="success",
                      request_id=request_id(request), run_id=run.run_id,
                      run_owner=run.owner, meta=_prompt_meta(prompt))
        store.append(run.run_id, "turn.started", {})
        store.append(run.run_id, "user.message", {"text": prompt})
        maybe_assign_title(run, prompt, True)
        task_store.mark_running(task_id, run.run_id, prompt)
        persist()
        start_turn(run, prompt)
        return {"task_id": task_id, "run_id": run.run_id, "status": "RUNNING"}

    @app.post("/api/runs/{run_id}/messages")
    async def send_message(run_id: str, request: Request, body: dict):
        actor = request.state.username
        run = _owned_run_or_404(request, run_id, action="send")
        text = (body or {}).get("text")
        if not isinstance(text, str) or not text.strip():
            raise HTTPException(status_code=422, detail="text required")
        is_first = run.first_prompt is None  # 快照先于 commit_turn（它写 first_prompt）
        try:
            manager.check_turn(run)
        except runs_mod.Conflict as exc:
            _audit_denied(request, "send", run, reason=exc.detail)
            raise HTTPException(status_code=409, detail=exc.detail) from exc
        # 发送审计：指令内容不入审计，只记长度与摘要 hash（meta）
        _audit_or_503(actor=actor, action="send", result="success",
                      request_id=request_id(request), run_id=run.run_id,
                      run_owner=run.owner, meta=_prompt_meta(text))
        manager.commit_turn(run, text)
        # 接受指令与开卷事件是同一个同步段：成功响应一旦返回，随后到达的
        # stop 必然排在这两条事实之后，不依赖异步回合任务是否已获调度。
        store.append(run.run_id, "turn.started", {})
        store.append(run.run_id, "user.message", {"text": text})
        persist()
        maybe_assign_title(run, text, is_first)
        start_turn(run, text)
        return {"run_id": run.run_id, "status": run.status}

    @app.post("/api/runs/{run_id}/stop")
    async def stop_run(run_id: str, request: Request, body: dict | None = None):
        actor = request.state.username
        run = _owned_run_or_404(request, run_id, action="stop")
        try:
            manager.check_stop(run)
        except runs_mod.Conflict as exc:
            _audit_denied(request, "stop", run, reason=exc.detail)
            raise HTTPException(status_code=409, detail=exc.detail) from exc
        _audit_or_503(actor=actor, action="stop", result="success",
                      request_id=request_id(request), run_id=run.run_id,
                      run_owner=run.owner)
        manager.commit_stop(run)
        persist()
        await _interrupt_if_requested(run)
        return {"run_id": run.run_id, "status": run.status}

    @app.post("/api/runs/{run_id}/clone")
    async def clone_run(run_id: str, request: Request):
        actor = request.state.username
        run = _owned_run_or_404(request, run_id, action="clone")
        try:
            new = manager.clone_draft(run)  # owner 继承源：归属不因 Fork 跨越
        except runs_mod.Conflict as exc:
            _audit_denied(request, "clone", run, reason=exc.detail)
            raise HTTPException(status_code=409, detail=exc.detail) from exc
        _audit_or_503(actor=actor, action="clone", result="success",
                      request_id=request_id(request), run_id=run.run_id,
                      run_owner=run.owner, meta=f"to:{new.run_id}")
        manager.register(new)
        store.create(new.run_id)
        store.append(new.run_id, "session.started", {})
        # 源流转录进新会话（seq 重新编号、ts 原样透传——实时事件的 ts 本就是
        # 真实时刻）：生命周期事件不是新会话的状态——源的 session.ended 会被
        # 前端当成本流终态关流判死，克隆后无法续聊
        store.adopt_history(
            new.run_id, run.run_id,
            skip_types={"session.started", "session.ended"},
        )
        # 转录不是真实活动（与 rebuild「重放事件不是真实活动」同款手法）：
        # 末活动时刻覆写为克隆操作时刻——刚点的克隆在列表排最前，归档历史
        # 的「N 分钟前」归源会话自己显示
        new.last_event_at = time.time()
        persist()
        return {"run_id": new.run_id, "status": new.status, "resumed_from": run.run_id}

    @app.post("/api/runs/{run_id}/end")
    async def end_run(run_id: str, request: Request):
        actor = request.state.username
        run = _owned_run_or_404(request, run_id, action="end")
        try:
            manager.check_end(run)
        except runs_mod.Conflict as exc:
            _audit_denied(request, "end", run, reason=exc.detail)
            raise HTTPException(status_code=409, detail=exc.detail) from exc
        _audit_or_503(actor=actor, action="end", result="success",
                      request_id=request_id(request), run_id=run.run_id,
                      run_owner=run.owner)
        # 在飞回合的取消与收尾（manager.end 校验已在上面完成）
        if run.turn_task is not None:
            run.turn_task.cancel()
            try:
                await run.turn_task  # 等取消收尾（回合不补事件）再写终态
            except asyncio.CancelledError:
                pass
            run.turn_task = None
        run.status = ENDED
        run.ended_at = time.time()
        store.append(run.run_id, "session.ended", {})
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

    # 批量打包下载：POST {"paths": [根前缀相对路径…]} → 一个 zip（包内按
    # 文件名平铺、不带目录树；越界/缺失项如实跳过，一个都收不到 404）
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

    # OBS 对象浏览（scope obs 段 + esdk-obs-python）：清单 / 签名下载链接 /
    # 文本预览。未配置（SDK 缺失或 scope 无 obs 段）503、云侧失败 502、
    # 对象级 404/422。SDK 是阻塞 IO——同步 def 由 FastAPI 派线程池执行，
    # 不占事件循环。
    @app.get("/api/obs/objects")
    def obs_objects(limit: int = 1000):
        try:
            return obs_list(limit=max(1, min(limit, 5000)))
        except obs_mod.ObsNotConfigured as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except obs_mod.ObsApiError as exc:
            raise HTTPException(status_code=502, detail=exc.error) from exc

    @app.get("/api/obs/url")
    def obs_signed_url(key: str = "", expires: int = 3600):
        try:
            return obs_url(key=key, expires=max(60, min(expires, 7 * 24 * 3600)))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except obs_mod.ObsNotConfigured as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except obs_mod.ObsApiError as exc:
            raise HTTPException(status_code=502, detail=exc.error) from exc

    @app.get("/api/obs/content")
    def obs_content(key: str = ""):
        try:
            found = obs_read(key=key)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except obs_mod.ObsNotConfigured as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except obs_mod.ObsApiError as exc:
            raise HTTPException(status_code=502, detail=exc.error) from exc
        if found is None:
            raise HTTPException(status_code=404, detail="object not found")
        if found.get("binary"):
            raise HTTPException(
                status_code=422,
                detail="binary or oversized object; use /api/obs/url for a signed download link",
            )
        return found

    # 本地产物批量归档到 OBS：paths（根前缀相对路径，与 zip 端点同形状）→
    # 服务端 resolve（路径约束复用——服务不是任意文件上传器）后逐个 putFile，
    # 对象名 = 产物相对路径（目录结构原样保留，与桶内既有 key 同构）。
    # 共享资源写：不按会话 owner 过滤（任何登录用户可归档），先审计后执行
    # （审计失败 503 不上传）。缺失/越界项如实跳过并计数，一个都收不到
    # 404；未配置 503、云失败 502。
    @app.post("/api/obs/archive")
    def archive_to_obs(request: Request, body: dict | None = None):  # 函数名不叫 obs_archive——避免遮蔽同名闭包（默认实现 lambda）自递归
        paths = (body or {}).get("paths")
        if not isinstance(paths, list) or not paths or not all(
            isinstance(p, str) and p for p in paths
        ):
            raise HTTPException(status_code=422, detail="paths required")
        items = []
        for rel in paths:
            found = artifacts_mod.resolve(artifact_roots, file_stages, rel)
            if found is not None:
                items.append((rel, found[1]))
        if not items:
            raise HTTPException(status_code=404, detail="no artifacts to archive")
        _audit_or_503(actor=request.state.username, action="obs_archive",
                      result="success", request_id=request_id(request),
                      meta=f"items:{len(items)},skipped:{len(paths) - len(items)}")
        try:
            result = obs_archive(items)
        except obs_mod.ObsNotConfigured as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except obs_mod.ObsApiError as exc:
            raise HTTPException(status_code=502, detail=exc.error) from exc
        return {**result, "requested": len(paths), "skipped": len(paths) - len(items)}

    # 多选产物打包 zip 归档到 OBS：paths（与 zip 下载端点同形状）在服务端
    # 内存打包（artifacts.zip_files 复用，不落盘）后 putContent 直传，对象名
    # 固定 zip/ 前缀 + 自定义包名（normalize_zip_name 清洗）。共享资源写：
    # 任何登录用户可用，先审计后执行（审计失败 503 不上传）。无有效产物
    # 404、包名/paths 非法 422、未配置 503、云失败 502。
    @app.post("/api/obs/archive-zip")
    def archive_zip_to_obs(request: Request, body: dict | None = None):
        paths = (body or {}).get("paths")
        if not isinstance(paths, list) or not paths or not all(
            isinstance(p, str) and p for p in paths
        ):
            raise HTTPException(status_code=422, detail="paths required")
        try:
            name = obs_mod.normalize_zip_name((body or {}).get("name"))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        stream, count = artifacts_mod.zip_files(artifact_roots, file_stages, paths)
        if stream is None:
            raise HTTPException(status_code=404, detail="no artifacts to zip")
        _audit_or_503(actor=request.state.username, action="obs_archive_zip",
                      result="success", request_id=request_id(request),
                      meta=f"key:zip/{name},items:{count}")
        try:
            result = obs_upload_zip(f"zip/{name}", stream.getvalue())
        except obs_mod.ObsNotConfigured as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except obs_mod.ObsApiError as exc:
            raise HTTPException(status_code=502, detail=exc.error) from exc
        return {**result, "zipped": count, "sources": paths}

    # OBS 配置查看（GET，脱敏视图：明文 ak/sk 永不出服务，只回脱敏形 +
    # 来源 + endpoint/bucket/region；can_write 标当前用户有无修改权——前端
    # 据此展示只读态）与保存（POST，仅部署管理员）：ak/sk 服务端加密
    # （enc:v1）落 scope.yaml 的 obs 段、明文键自动删除，其余内容逐行
    # 保留；保存后尽力健康检查（失败不回滚，结果如实带回由用户决断）。
    # 输入非法 422、非管理员 403、加密依赖缺失 503；写动作先审计后执行
    # （审计失败 503 不落盘）。
    @app.get("/api/obs/config")
    def get_obs_config(request: Request):
        return {**obs_mod.get_config(obs_scope),
                "can_write": request.state.username == obs_admin}

    @app.post("/api/obs/config")
    def save_obs_config(request: Request, body: dict | None = None):
        b = body or {}
        if request.state.username != obs_admin:
            _audit_denied(request, "obs_config", reason=REASON_NOT_ADMIN)
            raise HTTPException(
                status_code=403, detail=f"obs config is admin-only ({obs_admin})")
        try:  # 入口预检与 save_config 同一份清洗（非法输入 422 先于审计）
            clean = obs_mod.clean_config_input(
                ak=b.get("ak"), sk=b.get("sk"),
                region=b.get("region"), bucket=b.get("bucket"),
                endpoint=b.get("endpoint"),
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        _audit_or_503(actor=request.state.username, action="obs_config",
                      result="success", request_id=request_id(request),
                      meta="fields:" + ",".join(sorted(clean)))
        try:
            view = obs_mod.save_config(obs_scope, **clean)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except obs_mod.ObsNotConfigured as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        check = {"ok": False, "error": None}
        try:
            check = obs_health()
        except obs_mod.ObsNotConfigured as exc:
            check = {"ok": False, "error": str(exc)}
        except obs_mod.ObsApiError as exc:
            check = {"ok": False, "error": exc.error}
        return {**view, "saved": True, "check": check}

    # OBS 健康检查：headBucket 单请求全链路（配置解析 → 凭据解密 → SDK →
    # 网络 → 凭据有效性 → 桶存在）。200 = 健康；503 = 未配置/密钥问题；
    # 502 = 云侧失败（凭据错误/网络不通/桶不存在）——状态码即监控判定。
    @app.get("/api/obs/health")
    def check_obs_health():
        try:
            return obs_health()
        except obs_mod.ObsNotConfigured as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except obs_mod.ObsApiError as exc:
            raise HTTPException(status_code=502, detail=exc.error) from exc

    # ECS 实例面板（scope 顶层凭据 + huaweicloudsdkecs）：清单 / 一键存活
    # 检查（status==ACTIVE 且 TCP 22 可达，并发探测）/ 同步建机到就绪
    # （长持请求，分钟级；ok:false 仍 200——机器可能已建出，id 不丢）/
    # 新建表单默认值。未配置（SDK 缺失或凭据/必填段缺失）503、云侧失败
    # 502、表单非法 422。SDK 是阻塞 IO——同步 def 由 FastAPI 派线程池
    # 执行，不占事件循环。
    @app.get("/api/ecs/instances")
    def list_ecs_instances(limit: int = 500):
        try:
            return ecs_list_impl(limit=max(1, min(limit, 1000)))
        except ecs_mod.EcsNotConfigured as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except ecs_mod.EcsApiError as exc:
            raise HTTPException(status_code=502, detail=exc.error) from exc

    @app.post("/api/ecs/check")
    def check_ecs_alive():
        try:
            return ecs_check_impl()
        except ecs_mod.EcsNotConfigured as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except ecs_mod.EcsApiError as exc:
            raise HTTPException(status_code=502, detail=exc.error) from exc

    @app.post("/api/ecs/create")
    def create_ecs_instance(body: dict | None = None):
        b = body or {}
        clean = {}
        name = b.get("name")
        if name is not None:
            if not isinstance(name, str):
                raise HTTPException(status_code=422, detail="name must be a string")
            name = name.strip()
            if len(name) > 64 or "/" in name:
                raise HTTPException(status_code=422, detail="invalid name (≤64 chars, no '/')")
        clean["name"] = name or None
        for field in ("flavor", "image"):
            val = b.get(field)
            if val is not None:
                if not isinstance(val, str) or not val.strip():
                    raise HTTPException(status_code=422, detail=f"invalid {field}")
                clean[field] = val.strip()
        for field, lo, hi in (("disk_size", 10, 1024), ("bandwidth", 1, 2000)):
            val = b.get(field)
            if val is not None:
                if isinstance(val, bool) or not isinstance(val, int) or not lo <= val <= hi:
                    raise HTTPException(
                        status_code=422, detail=f"invalid {field} (int {lo}-{hi})")
                clean[field] = val
        try:
            return ecs_create_impl(clean)
        except ValueError as exc:  # scope/env 密码不合规等
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except ecs_mod.EcsNotConfigured as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except ecs_mod.EcsApiError as exc:
            raise HTTPException(status_code=502, detail=exc.error) from exc

    @app.get("/api/ecs/defaults")
    def get_ecs_defaults():
        return ecs_defaults_impl()

    # per-run 事件端点收窄为纯快照：按 Last-Event-ID 重放历史（断点续传），
    # 重放完毕正常结束响应——不常驻、无心跳、无 ENDED 关流判定（终态事件
    # session.ended 本身在历史里，快照一次给完；实时事件由全局流续接）
    @app.get("/api/runs/{run_id}/events")
    async def event_stream(run_id: str, request: Request):
        _owned_run_or_404(request, run_id)  # 未知/非 owner 404
        seen = _parse_last_event_id(request.headers.get("Last-Event-ID"))

        async def generate():
            for event in store.replay_from(run_id, seen):
                yield _sse_chunk(event)

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # 全局事件流：一条连接广播全部会话的实时事件（帧带 run_id），零连接
    # 状态——无 Last-Event-ID 断点、无连接簿记、无 TTL，增量游标是流自身
    # 的局部变量；连接前的事件不重放（历史由快照补），断线重连靠快照重拉
    # + per-run seq 去重吸收。永不因会话终态主动关闭：终态后不再产事件，
    # 天然静默，空闲按 heartbeat_interval 心跳保活
    @app.get("/api/stream")
    async def global_stream():
        async def generate():
            with store.subscribe_global() as flag:
                cursor = store.broadcast_len()
                while True:
                    flag.clear()
                    for event in store.broadcast_from(cursor):
                        cursor += 1
                        yield _broadcast_chunk(event)
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
    """执行 request_stop 排队的打断。回合可能刚好已自然结束（停止目标视为
    达成）、会话可能尚未建立（此时由回合在 query 前消费），两种情形均
    无需报错；打断失败不改变服务端权威状态（回合如何收尾以事件流为准）。"""
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


def _broadcast_chunk(event):
    # 全局帧：run_id/seq/ts/type/payload 全量下发（seq 仍是 per-run seq，
    # 客户端去重锚点）；无 id 行——断点语义不存在，重连靠快照重拉
    data = json.dumps({
        "run_id": event["run_id"],
        "seq": event["seq"],
        "ts": event["ts"],
        "type": event["type"],
        "payload": event["payload"],
    }, ensure_ascii=False)
    return f"event: {event['type']}\ndata: {data}\n\n"


def _parse_last_event_id(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0




def _prompt_meta(text):
    """发送指令的审计元数据：长度 + 摘要 hash——指令内容不进审计。"""
    return f"len:{len(text)},sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]}"


def residual_cli_processes():
    """pgrep -f claude 检测残留 CLI 子进程，返回 pid 列表；无匹配或 pgrep 缺失为空。

    只发现不处置：残留进程可能正处于云操作中间态，杀不杀由人工判断。
    """
    try:
        proc = subprocess.run(["pgrep", "-f", "claude"], capture_output=True, text=True)
    except OSError:
        return []
    return proc.stdout.split() if proc.returncode == 0 else []
