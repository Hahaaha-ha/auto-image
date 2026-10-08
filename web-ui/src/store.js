// 共享会话状态：多会话并行（并发上限内的执行中回合可多个），动作经
// HTTP/SSE 与服务端交互。事件通道是一条全局 SSE：启动即连 /api/stream、
// 永不主动关闭，广播帧按 runId 分发给打开的标签页（未打开的丢弃）；
// 打开会话标签页 = 拉一次快照补历史（按 Last-Event-ID 重放，与流重叠的
// 事件由 per-run seq 去重吸收）；断线由浏览器自动重连，恢复后对打开的
// 标签页逐个重拉快照追平。
//
// 标签页是纯客户端视图（会话与产物文件同栏混排，开-关-激活-控制面决策
// 全走 tabState 纯模块）：closeTab 只关视图，会话仍在列表里，重新打开
// （selectRun）重拉快照恢复历史；「结束会话」才是服务端动作。
// 非查看中的标签页状态点由 GET /api/runs 摘要轮询驱动。header 与输入条
// 构成控制面，绑定 controlRunId 解析出的会话——激活文件标签页不换对象。
import { useSyncExternalStore } from 'react'
import { fmtSize } from './derive.js'
import { mergeSessionEvents, mergeSessionSummary, SESSION_STATUS } from './eventMerge.js'
import * as tabState from './tabState.js'
import { emptyUsersQuery, USER_PAGE_SIZES, getUserListView, filterAndSortUsers } from './userList.js'

// 与服务端内部事件协议一致的事件类型全集（四族：session.* / turn.* /
// user.message / agent.* / stage.*）
export const EVENT_TYPES = [
  'session.started',
  'session.title_changed',
  'session.ended',
  'user.message',
  'agent.thinking',
  'agent.message',
  'agent.tool_started',
  'agent.tool_finished',
  'stage.changed',
  'turn.started',
  'turn.stopped',
  'turn.completed',
  'turn.failed',
  'turn.interrupted',
]

// 会触发产物清单刷新的事件：阶段推进（新产物落盘）与回合/会话收尾。
// 清单是 deploy/ + rpm/ 全量镜像（与查看中的会话无关），任一 run 触发都全局刷新
const REFRESH_EVENT_TYPES = ['stage.changed', 'turn.completed', 'turn.stopped', 'turn.failed', 'session.ended']

const { RUNNING, READY, ENDED } = SESSION_STATUS
// 可继续操作的会话状态集合：判定值与服务端状态机一致，单处维护
const OPERABLE = [RUNNING, READY]
export const isOperable = (status) => OPERABLE.includes(status)

// 409 detail 判定值 → 人话提示（判定值与服务端 runs.Conflict.detail 一致，单处维护）
const CONFLICT_HINT = {
  turn_in_progress: '本会话回合执行中，想改方向先点「停止」',
  session_running: '源会话正在执行，回合结束后才能 Fork',
  parallel_limit_reached: '执行中回合已达并发上限，稍后再发',
  session_not_active: '会话已结束，不可再操作（可 Fork 后继续）',
}

const listeners = new Set()
// 标签页视图随浏览器刷新与重开存活（刷新/误关/关窗重开后标签页与会话
// 一一对应还在），服务端已不存在的会话（重启丢了空会话等）在 loadRuns
// 合并列表时自然剪掉。localStorage（跨窗口、跨浏览器会话）而非
// sessionStorage：用户故事要求「关闭浏览器后重新打开」标签也还在；共享
// 服务时各客户端各存各的（存储按本机源隔离），互不沾染。只存会话标签页
// （含次序与激活态），文件标签页刷新后消失、内容缓存随之丢弃。
const TABS_KEY = 'va-open-tabs'
function restoreTabs() {
  try {
    const saved = JSON.parse(localStorage.getItem(TABS_KEY) || 'null')
    if (saved && Array.isArray(saved.openTabs)) {
      const openTabs = saved.openTabs.filter((id) => typeof id === 'string')
      const viewRunId = openTabs.includes(saved.viewRunId) ? saved.viewRunId : openTabs[0] ?? null
      return { openTabs, viewRunId }
    }
  } catch {
    // 存储不可用/损坏：从空标签集开始，功能照常
  }
  return { openTabs: [], viewRunId: null }
}

// 落盘走 tabState.persistableTabs（文件标签页滤掉、viewRun 落到记住的
// 最后激活会话标签页）；形状与旧版本兼容（openTabs 纯 runId 数组 + viewRunId）
function persistTabs() {
  try {
    const { openTabs, viewRunId } = tabState.persistableTabs(state.tabs, state.activeKey, state.lastSessionKey)
    localStorage.setItem(TABS_KEY, JSON.stringify({ openTabs, viewRunId }))
  } catch {
    // 存储不可用（隐私模式等）：只丢刷新存活，不影响使用
  }
}

// 侧栏面板选择持久化（从 SidePanel 上提；跨面板跳转需要 store 持有状态）
const SIDE_PANEL_KEY = 'va-side-panel'
const SIDE_PANELS = ['sessions', 'tasks', 'artifacts', 'obs', 'ecs']
function readSidePanel() {
  try {
    const v = localStorage.getItem(SIDE_PANEL_KEY)
    if (SIDE_PANELS.includes(v)) return v
  } catch {
    // 存储不可用（隐私模式等）：回落默认面板
  }
  return 'artifacts'
}

// order：全部会话的列表序（含未打开的，服务端列表同源）；tabs：混合标签
// 栏的标签页数组（{kind:'session',runId} | {kind:'file',relPath,name}），
// activeKey 复合 key 寻址（session:<runId> / file:<relPath>），决策全走
// tabState 纯模块，这里只当状态容器。lastSessionKey 记住最后激活的会话
// 标签页——激活文件标签页时控制面（header/输入条）仍绑定它。
const restored = restoreTabs()
const emptyUserReset = () => ({ target: null, busy: false, error: null, errorField: null, notice: null, verifyUsername: null, unknown: false })
const emptyUserAccess = () => ({ target: null, busy: false, error: null, notice: null, verifyUsername: null })
let state = {
  auth: 'checking',             // checking → anonymous | password-change | user
  userVersion: null,
  passwordChange: { busy: false, error: null },
  authNotice: null,
  canManageUsers: false,
  users: { items: [], loading: false, error: null },
  usersQuery: emptyUsersQuery(),
  userCreate: { busy: false, error: null, notice: null, verifyUsername: null },
  userAccess: emptyUserAccess(), userReset: emptyUserReset(),
  user: null,                   // 当前用户名（auth === 'user' 时非空）
  runs: {},
  order: [],
  tabs: restored.openTabs.map((runId) => ({ kind: 'session', runId })),
  activeKey: restored.viewRunId ? `session:${restored.viewRunId}` : null,
  lastSessionKey: restored.viewRunId ? `session:${restored.viewRunId}` : null,
  connection: 'connecting', // 全局事件流连接态：connecting → live / reconnecting
  capacity: null,           // 匿名全局容量（服务端随 /api/runs 下发：runningCount/maxParallel，不含他人会话细节）
  submitError: null,
  notice: null,               // 成功提示条（与 submitError 对称，绿色短暂展示）
  now: Date.now(),
  drafts: {},                // 会话草稿镜像（runId → 文本；真身在 setDraft 侧的 map）
  artifacts: { groups: [] }, // deploy/ + rpm/ 全量产物（目录分组，全局不属于任何 run）
  artifactCache: {},         // 产物内容多槽缓存（relPath → 条目+content），关标签页不清
  artifactSel: {},           // 批量下载勾选集（relPath → true，随清单刷新剪枝）
  artifactZipping: false,    // zip 打包请求进行中（按钮防重复触发）
  obs: { objects: [], bucket: null, region: null, domain: null, error: null, loading: false }, // OBS 桶内对象清单（scope obs 段绑定，尽力而为）
  obsCache: {},              // OBS 对象内容多槽缓存（对象 key → 条目+content），关标签页不清
  obsArchiving: false,       // 批量归档请求进行中（按钮防重复触发）
  obsZipArchiving: false,    // 打包 zip 归档请求进行中（按钮防重复触发）
  ecs: { instances: [], region: null, error: null, loading: false, checking: false }, // ECS 实例清单（scope 顶层凭据绑定，尽力而为）
  ecsCreating: false,        // 建机请求进行中（分钟级长请求，对话框防重复提交）
  tasks: [],                 // 流水线任务（rpm-*/deploy-* 派发即建；服务端 task/ 目录持久化）
  sidePanel: readSidePanel(), // 侧栏面板选择（上提到 store：会话标签的任务 pill 要跨面板跳转）
  activeTaskId: null,        // 任务面板高亮行（openTask 跳转锚点）
}

export function setSidePanel(panel) {
  if (!SIDE_PANELS.includes(panel)) return
  set({ sidePanel: panel })
  try {
    localStorage.setItem(SIDE_PANEL_KEY, panel)
  } catch {
    // 存储不可用：只丢面板选择存活，不影响使用
  }
}

// 服务端任务 → 前端形状（snake→camel；usage 四键原样数值或 null）
function makeTask(t) {
  return {
    taskId: t.task_id,
    runId: t.run_id,
    type: t.type,
    status: t.status,
    outcome: t.outcome,
    name: t.name,
    software: t.software,
    version: t.version,
    confirmed: t.confirmed,
    stages: (t.stages ?? []).map((s) => ({ stage: s.stage, startedAt: s.started_at, endedAt: s.ended_at })),
    currentStage: t.current_stage,
    usage: t.usage
      ? {
          inputTokens: t.usage.input_tokens,
          outputTokens: t.usage.output_tokens,
          cacheReadTokens: t.usage.cache_read_tokens,
          cacheCreationTokens: t.usage.cache_creation_tokens,
        }
      : null,
    serverAlias: t.server_alias,
    instanceId: t.instance_id,
    createdAt: t.created_at,
    endedAt: t.ended_at,
    turnText: t.turn_text,
    origin: t.origin,
    spec: t.spec
      ? {
          ecsMode: t.spec.ecs_mode,
          ecsInstance: t.spec.ecs_instance,
          ecsParams: t.spec.ecs_params,
          installDoc: t.spec.install_doc,
        }
      : null,
  }
}

// 任务清单刷新（阶段事件/摘要周期/启动驱动；尽力而为，失败静默——
// 面板下一周期自愈）
export async function refreshTasks() {
  const epoch = identityEpoch
  try {
    const resp = await fetch('/api/tasks')
    const data = await resp.json().catch(() => ({}))
    if (epoch !== identityEpoch) return
    if (!resp.ok) return
    set({ tasks: (data.tasks ?? []).map(makeTask) })
  } catch {
    // 网络断等：静默
  }
}

// 跳转到任务：切任务面板 + 高亮行（会话标签 pill / 任务互跳的入口）
export function openTask(taskId) {
  set({ sidePanel: 'tasks', activeTaskId: taskId })
  if (!state.tasks.length) refreshTasks()
}

// 手动建任务（「+ 新建任务」表单提交）：INIT 待运行态落服务端；返回任务
// 供对话框收尾（失败抛错由对话框行内展示）
export async function createTask(spec) {
  const epoch = identityEpoch
  const data = await postJson('/api/tasks', spec)
  if (epoch !== identityEpoch) return
  refreshTasks()
  return data
}

// 运行 INIT 任务：服务端新建会话并发出按表单构造的部署指令——新会话
// 本地落位（adoptNewRun，同新建会话路径）即自动跳到该会话标签页看
// agent 执行（用户已确认此交互）
export async function runTask(taskId) {
  const epoch = identityEpoch
  const data = await postJson(`/api/tasks/${encodeURIComponent(taskId)}/run`, {})
  if (epoch !== identityEpoch) return
  adoptNewRun(makeRun({
    runId: data.run_id,
    status: data.status,
    startedAt: Date.now(),
  }))
  refreshTasks()
  ok(`任务已启动，已打开会话 ${data.run_id}`)
  return data
}

// runId → 进行中任务（会话标签 pill 用；一个 run 同时至多一个活动任务）
export function runningTaskByRun(runId) {
  return state.tasks.find((t) => t.status === 'RUNNING' && t.runId === runId) ?? null
}

// ECS 实例 → 运行中任务（instance_id 精确 join；已有别名安装路径的 meta
// 无 instance_id，按创建命名规约 ecs-<server_alias> 名字兜底）
export function runningTaskByInstance(inst) {
  return state.tasks.find((t) => t.status === 'RUNNING' && (
    (t.instanceId && t.instanceId === inst.id)
    || (t.serverAlias && inst.name === 'ecs-' + t.serverAlias)
  )) ?? null
}

// 时长走针仅在控制面会话执行期间（挂起与终态冻结，终态由事件求和定格）；
// 轮询计时器统一由 startTimers 登记（deauthed 清停、再登录重建——同页
// 登出→登录后轮询不丢）
const timersRef = new Set()
function startTimers() {
  if (timersRef.size) return
  timersRef.add(setInterval(() => {
    if (state.runs[controlRunId()]?.status === RUNNING) set({ now: Date.now() })
  }, 1000))
  timersRef.add(setInterval(() => pollSummaries(), 5000))
}

function set(patch) {
  state = { ...state, ...patch }
  if ('tabs' in patch || 'activeKey' in patch || 'lastSessionKey' in patch) persistTabs()
  listeners.forEach((l) => l())
}

function setRun(runId, patch) {
  const run = state.runs[runId]
  if (!run) return
  state = { ...state, runs: { ...state.runs, [runId]: { ...run, ...patch } } }
  listeners.forEach((l) => l())
}

export function subscribe(l) {
  listeners.add(l)
  return () => listeners.delete(l)
}
export const getState = () => state
export function useRunState() {
  return useSyncExternalStore(subscribe, getState)
}

// 控制面（header/输入条）绑定的会话：激活的是会话标签页 → 它；是文件
// 或空 → 记住的最后激活会话标签页。无会话标签页（服务端彻底无会话）为 null。
export function controlRunId() {
  return tabState.controlRunId(state.tabs, state.activeKey, state.lastSessionKey)
}

// 控制面会话（useControlRun 的数据源钩子；消息流视图在 App 按 tabs 派生）
export function useControlRun() {
  const s = useRunState()
  return s.runs[controlRunId()] ?? null
}

async function postJson(url, body) {
  const resp = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body ?? {}),
  })
  const data = await resp.json().catch(() => ({}))
  if (!resp.ok) {
    const err = new Error(data.detail || `HTTP ${resp.status}`)
    err.status = resp.status
    err.detail = data.detail
    throw err
  }
  return data
}

let errorTimer = null
function fail(message) {
  clearTimeout(errorTimer)
  set({ submitError: message })
  errorTimer = setTimeout(() => state.submitError && set({ submitError: null }), 4000)
}

// 成功提示条（与 fail 对称：绿色短暂展示）
let noticeTimer = null
function ok(message) {
  clearTimeout(noticeTimer)
  set({ notice: message })
  noticeTimer = setTimeout(() => state.notice && set({ notice: null }), 4000)
}

// 409 家族提示条文案：命中判定值给人话提示，其余如实透传服务端 detail
const conflictText = (detail) => `409 — ${detail}：${CONFLICT_HINT[detail]}`
function conflictMessage(err) {
  return err.status === 409 && CONFLICT_HINT[err.detail]
    ? conflictText(err.detail)
    : err.detail || err.message
}

// ---------- 事件通道：全局流 + 快照 ----------

// 事件落地（广播帧与快照重放同一归途）：整批交给纯归并入口按 seq
// 寻址、去重、排序并重算派生状态。阶段推进与收尾类事件顺手触发产物
// 清单刷新（幂等无害）。
function ingestEvents(runId, events) {
  const session = state.runs[runId]
  if (!session || !events.length) return
  setRun(runId, mergeSessionEvents(session, events))
  if (events.some((event) => REFRESH_EVENT_TYPES.includes(event.type))) {
    refreshArtifacts()
    refreshTasks() // 任务面同源驱动：阶段推进/回合收尾即任务状态变化
  }
}

// 广播帧 → 事件落地：帧带 run_id/seq/ts，先过「该 run 打开着标签页」守卫
// （未打开的丢弃——侧栏态势由摘要轮询驱动）。ts 在帧顶层（快照路径则是
// data 里已并入），统一并进 payload——归并入口读 payload.ts
function onBroadcastFrame(e) {
  let frame
  try {
    frame = JSON.parse(e.data)
  } catch {
    return
  }
  if (!openRunIds().has(frame.run_id)) return
  ingestEvents(frame.run_id, [{
    seq: frame.seq,
    type: frame.type,
    payload: { ...frame.payload, ts: frame.ts },
  }])
}

// 打开着的会话标签页的 runId 集（分发守卫；loadRuns 剪枝前对恢复标签页
// 宽进——不存在的 run 的帧会被 ingestEvents 的存在性守卫拦下）
function openRunIds() {
  const ids = new Set()
  for (const t of state.tabs) if (t.kind === 'session') ids.add(t.runId)
  return ids
}

// SSE 文本 → 事件数组：id/event/data 三行成帧，`:` 开头注释行（心跳）跳过。
// 单帧 data 非法 JSON 只丢那一帧（外部输出不保证，坏一帧不放大成整批丢失）
function parseSseEvents(text) {
  const events = []
  for (const block of text.split('\n\n')) {
    if (!block || block.startsWith(':')) continue
    let seq = null
    let type = null
    let data = null
    for (const line of block.split('\n')) {
      if (line.startsWith('id:')) seq = Number(line.slice(3).trim())
      else if (line.startsWith('event:')) type = line.slice(6).trim()
      else if (line.startsWith('data:')) data = line.slice(5).trim()
    }
    if (seq == null || !type || data == null) continue
    try {
      events.push({ seq, type, payload: JSON.parse(data) })
    } catch {
      // 坏帧丢弃：下一帧继续
    }
  }
  return events
}

// 拉一次快照补历史：per-run 端点按 Last-Event-ID 重放、重放完即断，重叠
// 事件由纯归并入口的 seq 去重吸收。run 已不在（服务端重启丢了空会话等）
// 静默作罢——摘要轮询会把它从列表剪掉。
async function loadSnapshot(runId) {
  const epoch = identityEpoch
  const run = state.runs[runId]
  if (!run) return
  const lastSeq = run.maxSeq ?? 0
  try {
    const resp = await fetch(`/api/runs/${runId}/events`, { headers: { 'Last-Event-ID': String(lastSeq) } })
    if (!resp.ok) return
    const events = parseSseEvents(await resp.text())
    if (epoch !== identityEpoch || !events.length) return
    ingestEvents(runId, events)
  } catch {
    // 快照失败不打断使用：全局流仍在，断线恢复或下次打开再补
  }
}

// 对打开的会话标签页逐个重拉快照（onopen 恢复与 loadRuns 首屏恢复共用）
function refreshOpenSnapshots() {
  for (const runId of openRunIds()) loadSnapshot(runId)
}

// ---------- 认证 ----------

// auth：'checking'（启动确认中）→ 'anonymous'（未登录，登录壳）/'user'
// （已登录，username 就位）。未登录期间一切数据面（EventSource、轮询、
// 初始拉取）不启动——未认证客户端读不到任何业务数据。
let globalStream = null
let identityEpoch = 0

// 认证失效统一出口：回登录壳并关停数据面。SSE onerror / 401 响应都会
// 走这里；EventSource 关闭后浏览器不再自动重连（不无限重连的权威手段）。
// 会话视图一并清空——同一浏览器随后换账号登录时，上一个用户的会话
// 列表/标签页/草稿不残留（服务端列表本就按 owner 过滤，这里是客户端
// 不暂存他人数据的收尾）
function deauthed(reasonText) {
  identityEpoch += 1
  if (globalStream) {
    const stream = globalStream
    globalStream = null
    stream.close()
  }
  clearTimeout(errorTimer)
  clearTimeout(noticeTimer)
  for (const id of timersRef) clearInterval(id)
  timersRef.clear()
  for (const key of Object.keys(drafts)) delete drafts[key]
  set({
    auth: 'anonymous', user: null, canManageUsers: false, userVersion: null,
    passwordChange: { busy: false, error: null },
    users: { items: [], loading: false, error: null }, connection: 'connecting',
    usersQuery: emptyUsersQuery(),
    userCreate: { busy: false, error: null, notice: null, verifyUsername: null },
    userAccess: emptyUserAccess(), userReset: emptyUserReset(),
    runs: {}, order: [], capacity: null,
    tabs: [], activeKey: null, lastSessionKey: null,
    tasks: [], activeTaskId: null,
    drafts: {},
    artifacts: { groups: [] }, artifactCache: {}, artifactSel: {}, artifactZipping: false,
    obs: { objects: [], bucket: null, region: null, domain: null, error: null, loading: false },
    obsCache: {}, obsArchiving: false, obsZipArchiving: false,
    ecs: { instances: [], region: null, error: null, loading: false, checking: false }, ecsCreating: false,
    submitError: null, notice: null,
  })
  persistTabs()
  if (reasonText) fail(reasonText)
}

// 登录成功后的数据面启动：建全局流（一次）+ 各初始拉取（幂等——已登录
// 状态下的重复调用不重复建流）
function startDataPlane() {
  if (state.auth !== 'user' || globalStream) return
  startTimers()
  globalStream = new EventSource('/api/stream')
  for (const type of EVENT_TYPES) globalStream.addEventListener(type, onBroadcastFrame)
  globalStream.onopen = () => {
    set({ connection: 'live' })
    refreshOpenSnapshots()
  }
  globalStream.onerror = async () => {
    if (globalStream?.readyState === EventSource.CLOSED) {
      // 服务端主动关流：401（登录过期/被禁用）还是网络断，先重新确认身份
      // 再决定——认证失效回登录壳，网络断走重连态。关流顺序：先 close
      // （防浏览器自动重连）再查身份，查完才清理全局引用。
      const stream = globalStream
      stream.close()
      globalStream = null
      const confirmed = await confirmIdentity()
      if (confirmed === null) return
      if (!confirmed) {
        deauthed('登录已失效，请重新登录')
        return
      }
      startDataPlane()
      return
    }
    set({ connection: 'reconnecting' })
  }
  loadRuns()
  refreshArtifacts()
  refreshObs()
  refreshEcs()
  refreshTasks()
}

// 当前身份确认：有效身份 true，失败 false，旧登录的迟到响应 null。
async function confirmIdentity() {
  const epoch = identityEpoch
  try {
    const resp = await fetch('/api/auth/me')
    if (epoch !== identityEpoch) return null
    if (!resp.ok) return false
    const data = await resp.json().catch(() => null)
    if (epoch !== identityEpoch) return null
    if (!data?.username) return false
    acceptIdentity(data)
    return true
  } catch {
    return epoch === identityEpoch ? false : null
  }
}

function acceptIdentity(data) {
  if (state.user && state.user !== data.username) deauthed(null)
  if (data.must_change_password === true) {
    deauthed(null)
    set({ auth: 'password-change', user: data.username, userVersion: data.user_version, authNotice: null })
    return
  }
  const canManageUsers = data.can_manage_users === true
  const users = state.user === data.username && canManageUsers
    ? state.users : { items: [], loading: false, error: null }
  const usersQuery = state.user === data.username && canManageUsers ? state.usersQuery : emptyUsersQuery()
  const userCreate = state.user === data.username && canManageUsers
    ? state.userCreate : { busy: false, error: null, notice: null, verifyUsername: null }
  const userAccess = state.user === data.username && canManageUsers ? state.userAccess : emptyUserAccess()
  const userReset = state.user === data.username && canManageUsers ? state.userReset : emptyUserReset()
  set({ auth: 'user', user: data.username, canManageUsers, users, usersQuery, userCreate, userAccess, userReset, userVersion: null, authNotice: null })
}

export function setUsersKeyword(keyword) {
  if (state.canManageUsers) set({ usersQuery: { ...state.usersQuery, keyword, page: 1 } })
}

export function setUsersPageSize(pageSize) {
  if (state.canManageUsers && USER_PAGE_SIZES.includes(pageSize)) {
    set({ usersQuery: { ...state.usersQuery, pageSize, page: 1 } })
  }
}

export function setUsersPage(page) {
  if (!state.canManageUsers || !Number.isInteger(page)) return
  const query = { ...state.usersQuery, page }
  set({ usersQuery: { ...query, page: getUserListView(state.users.items, query).page } })
}

export async function refreshUsers(force = false) {
  if (!state.canManageUsers || (state.users.loading && !force)) return
  const identity = state.users
  const accessToVerify = state.userAccess
  const resetToVerify = state.userReset
  const loading = { ...identity, loading: true, error: null }
  set({ users: loading })
  try {
    const resp = await fetch('/api/admin/users')
    if (state.users !== loading) return
    if (resp.status === 401) { deauthed('登录已失效，请重新登录'); return }
    if (resp.status === 403) {
      const data = await resp.json().catch(() => ({}))
      if (state.users !== loading) return
      if (userManagementAccessLost(resp.status, data.detail)) return
    }
    if (!resp.ok) throw new Error('用户清单加载失败，请点击刷新重试')
    const data = await resp.json()
    if (!Array.isArray(data.users)) throw new Error('invalid users response')
    if (state.users === loading) {
      set({ users: { items: data.users, loading: false, error: null },
        usersQuery: { ...state.usersQuery, page: getUserListView(data.users, state.usersQuery).page } })
      const verifyAccess = state.userAccess === accessToVerify && accessToVerify.verifyUsername
      if (verifyAccess) {
        const user = data.users.find(item => item.username === verifyAccess)
        set({ userAccess: { ...emptyUserAccess(), notice: { tone: 'warning',
          text: user
            ? `清单中「${verifyAccess}」当前${user.enabled ? '已启用' : '已禁用'}。如仍需变更，请重新选择并确认；本次刷新仅核实当前状态。`
            : `清单中未找到「${verifyAccess}」，请联系管理员核实身份后再操作。`,
        } } })
      }
      if (state.userReset === resetToVerify && resetToVerify.verifyUsername) {
        const username = resetToVerify.verifyUsername
        set({ userReset: { ...resetToVerify, verifyUsername: null, error: null, notice: { tone: 'warning',
          text: resetToVerify.unknown
            ? `已刷新「${username}」的用户信息，但清单无法验证密码，重置结果仍未知。请通过外部渠道核实；仍无法核实且需要继续时，请明确发起新的重置。`
            : `已刷新用户信息。如仍需重置「${username}」的密码，请重新选择目标并确认。`,
        } } })
      }
      const username = state.userCreate.verifyUsername
      if (username) {
        const exists = data.users.some((user) => user.username === username)
        set({ userCreate: { ...state.userCreate, verifyUsername: null, notice: { tone: 'warning',
          text: exists
            ? `清单中已有同名用户「${username}」。无法仅凭清单确认是否由本次创建或密码是否匹配，请先核实，不要重复新增。`
            : `刷新后清单中未找到「${username}」。如仍需新增，请重新填写并明确提交。`,
        } } })
      }
    }
  } catch {
    if (state.users === loading) set({ users: { items: [], loading: false, error: '用户清单加载失败，请点击刷新重试' } })
  }
}

function revokeUserManagement() {
  set({ canManageUsers: false, sidePanel: 'artifacts', users: { items: [], loading: false, error: null },
    usersQuery: emptyUsersQuery(),
    userAccess: emptyUserAccess(), userReset: emptyUserReset(),
    userCreate: { busy: false, error: null, notice: null, verifyUsername: null } })
}

function userManagementAccessLost(status, detail) {
  if (status === 401) {
    deauthed('登录已失效，请重新登录')
    return true
  }
  if (status === 403 && (detail === 'not_admin' || detail === 'password_change_required')) {
    revokeUserManagement()
    return true
  }
  return false
}

const USER_NOTICE_KEYS = ['userCreate', 'userAccess', 'userReset']

export function dismissUserNotice(key) {
  if (!USER_NOTICE_KEYS.includes(key) || state[key].notice?.tone !== 'success') return
  set({ [key]: { ...state[key], notice: null } })
}

function clearPreviousUserSuccess() {
  const updates = {}
  for (const key of USER_NOTICE_KEYS) {
    if (state[key].notice?.tone === 'success') updates[key] = { ...state[key], notice: null }
  }
  return updates
}

const USER_RECONFIRM_ERRORS = ['user_version_conflict', 'admin_read_only', 'no_such_user']

export function beginUserAccess(username) {
  if (!state.canManageUsers || state.users.loading || state.users.error || state.userAccess.busy || state.userAccess.verifyUsername || state.userReset.busy || state.userReset.target) return
  const user = state.users.items.find(item => item.username === username)
  if (user?.role !== 'user' || !user.user_version) return
  set({ userAccess: { ...emptyUserAccess(), target: { ...user } } })
}

export function cancelUserAccess() {
  if (!state.userAccess.busy) set({ userAccess: { ...state.userAccess, target: null, error: null } })
}

const USER_ACCESS_HINT = {
  user_version_conflict: '目标用户已发生变化，本次未提交。请刷新清单，重新选择并确认。',
  users_unavailable: '状态尚未修改：用户文件不可用，请联系管理员修复后再试。',
  audit_unavailable: '状态尚未修改：审计不可用，请联系管理员修复后再试。',
  state_unavailable: '状态尚未修改：服务处于受限恢复状态，请联系管理员。',
  admin_read_only: '管理员只读，不能启用或禁用。请刷新清单。',
  no_such_user: '目标用户不存在，请刷新清单核实。',
}

export async function submitUserAccess() {
  // 仅确认时保存的版本可提交，清单刷新不能偷偷替换旧表单的版本。
  const { target, busy, verifyUsername } = state.userAccess
  if (!state.canManageUsers || !target || busy || verifyUsername) return
  const attempt = { ...emptyUserAccess(), target, busy: true }
  const action = target.enabled ? '禁用' : '启用'
  set({ userAccess: attempt })
  try {
    const resp = await fetch(`/api/admin/users/${target.enabled ? 'disable' : 'enable'}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: target.username, expected_version: target.user_version }),
    })
    if (state.userAccess !== attempt) return
    if (userManagementAccessLost(resp.status)) return 'not_committed'
    const data = await resp.json()
    if (state.userAccess !== attempt) return
    if (data.outcome === 'committed') {
      const auditFailed = data.audit_status !== 'recorded'
      set({ ...clearPreviousUserSuccess(), userAccess: { ...emptyUserAccess(), notice: {
        tone: auditFailed ? 'warning' : 'success',
        text: `用户「${target.username}」已${action}。${target.enabled ? '既有登录已撤销；执行中的回合继续。' : '请原使用者重新登录，旧登录仍无效。'}${auditFailed ? '变更已生效，审计记录异常；请联系管理员检查审计，无需重复提交。' : ''}`,
      } } })
      refreshUsers(true)
      return 'committed'
    }
    if (userManagementAccessLost(resp.status, data.detail)) return 'not_committed'
    if (data.outcome === 'not_committed' || resp.status === 403) {
      const recheck = USER_RECONFIRM_ERRORS.includes(data.detail)
      set({ userAccess: { ...attempt, busy: false, target: recheck ? null : target,
        verifyUsername: recheck ? target.username : null,
        error: USER_ACCESS_HINT[data.detail] || '状态尚未修改，请刷新页面后重新确认操作。',
      } })
      return 'not_committed'
    }
    throw new Error('unknown access outcome')
  } catch {
    if (state.userAccess !== attempt) return
    set({ userAccess: { ...emptyUserAccess(), verifyUsername: target.username, notice: {
      tone: 'warning', text: `无法确认「${target.username}」的${action}结果。请先刷新清单核实当前状态，再决定是否重新操作；系统不会自动重提。`,
    } } })
    return 'unknown'
  }
}

export function beginUserReset(username) {
  if (!state.canManageUsers || state.users.loading || state.users.error || state.userReset.busy
      || state.userReset.verifyUsername || state.userAccess.busy || state.userAccess.target) return
  const user = state.users.items.find(item => item.username === username)
  if (user?.role !== 'user' || !user.user_version) return
  set({ userReset: { ...emptyUserReset(), target: { ...user } } })
}

export function cancelUserReset() {
  if (!state.userReset.busy) set({ userReset: { ...state.userReset, target: null, error: null, errorField: null } })
}

export function clearUserResetError(field) {
  if (!state.userReset.busy && state.userReset.error && state.userReset.errorField === field) {
    set({ userReset: { ...state.userReset, error: null, errorField: null } })
  }
}

const RESET_PASSWORD_HINT = {
  user_version_conflict: '目标用户已发生变化，本次未提交。请刷新清单，重新选择并确认。',
  invalid_new_password: '新密码须为 8–128 位英文字母、数字或半角符号，不含空格、其他空白或中文；不要求组合。',
  users_unavailable: '密码尚未修改：用户文件不可用，请联系管理员修复后再试。',
  audit_unavailable: '密码尚未修改：审计不可用，请联系管理员修复后再试。',
  state_unavailable: '密码尚未修改：服务处于受限恢复状态，请联系管理员。',
  admin_read_only: '管理员只读，不能重置密码。请刷新清单。',
  no_such_user: '目标用户不存在，请刷新清单核实。',
}

export async function resetUserPassword(password) {
  const { target, busy, verifyUsername } = state.userReset
  if (!state.canManageUsers || !target || busy || verifyUsername) return
  const attempt = { ...emptyUserReset(), target, busy: true }
  set({ userReset: attempt })
  try {
    const resp = await fetch('/api/admin/users/reset-password', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: target.username, expected_version: target.user_version, password }),
    })
    if (state.userReset !== attempt) return
    if (userManagementAccessLost(resp.status)) return 'not_committed'
    const data = await resp.json()
    if (state.userReset !== attempt) return
    if (data.outcome === 'committed') {
      const auditFailed = data.audit_status !== 'recorded'
      set({ ...clearPreviousUserSuccess(), userReset: { ...emptyUserReset(), notice: {
        tone: auditFailed ? 'warning' : 'success',
        text: `用户「${target.username}」的密码已重置，既有登录已撤销。请自行交付新密码，下次登录须再次改密。${target.enabled ? '' : '该用户仍已禁用，不能登录。'}${auditFailed ? '重置已生效，审计记录异常；请联系管理员检查审计，无需重复提交。' : ''}`,
      } } })
      refreshUsers(true)
      return 'committed'
    }
    if (userManagementAccessLost(resp.status, data.detail)) return 'not_committed'
    if (data.outcome === 'not_committed' || resp.status === 403) {
      const recheck = USER_RECONFIRM_ERRORS.includes(data.detail)
      set({ userReset: { ...attempt, busy: false, target: recheck ? null : target,
        verifyUsername: recheck ? target.username : null,
        error: RESET_PASSWORD_HINT[data.detail] || '密码尚未修改，请刷新页面后重新确认操作。',
        errorField: data.detail === 'invalid_new_password' ? 'password' : null,
      } })
      return 'not_committed'
    }
    throw new Error('unknown reset outcome')
  } catch {
    if (state.userReset !== attempt) return
    set({ userReset: { ...emptyUserReset(), verifyUsername: target.username, unknown: true, notice: {
      tone: 'warning', text: `无法确认「${target.username}」的重置结果，系统不会自动重提。请先刷新清单并通过外部渠道核实；清单无法验证密码，仍无法核实时须明确发起新的重置。`,
    } } })
    return 'unknown'
  }
}

const CREATE_USER_HINT = {
  invalid_username: '用户名须为 1–64 位英文字母、数字、下划线、短横线或点；区分大小写，不可含空格。',
  invalid_new_password: '初始密码须为 8–128 位可见 ASCII 字符（英文字母、数字或半角符号），不可含空格、其他空白或中文。',
  username_exists: '用户名已存在，未覆盖原用户。请为新使用者选择独立用户名。',
  users_unavailable: '用户尚未创建：用户文件不可用，请联系管理员修复后再试。',
  audit_unavailable: '用户尚未创建：审计不可用，请联系管理员修复后再试。',
  state_unavailable: '用户尚未创建：服务处于受限恢复状态，请联系管理员。',
}

export function clearUserCreateError(field) {
  if (!state.userCreate.busy && state.userCreate.error && (!field || state.userCreate.errorField === field)) {
    set({ userCreate: { ...state.userCreate, error: null, errorField: null } })
  }
}

export function showCreatedUser() {
  const username = state.userCreate.createdUsername
  if (!state.canManageUsers || !username || state.users.loading || state.users.error) return false
  const matching = filterAndSortUsers(state.users.items, username)
  const index = matching.findIndex(user => user.username === username)
  if (index < 0) return false
  set({ usersQuery: { ...state.usersQuery, keyword: username,
    page: Math.floor(index / state.usersQuery.pageSize) + 1 } })
  return true
}

export async function createUser(username, password) {
  if (!state.canManageUsers || state.userCreate.busy || state.userCreate.verifyUsername) return
  const attempt = { busy: true, error: null, errorField: null, notice: null, verifyUsername: null }
  set({ userCreate: attempt })
  try {
    const resp = await fetch('/api/admin/users', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password }),
    })
    const data = await resp.json()
    if (state.userCreate !== attempt) return
    if (data.outcome === 'committed') {
      const auditFailed = data.audit_status !== 'recorded'
      set({ ...clearPreviousUserSuccess(), userCreate: { ...attempt, busy: false, createdUsername: username, notice: {
        tone: auditFailed ? 'warning' : 'success',
        text: `用户「${username}」已创建。${auditFailed ? '变更已生效，审计记录异常；无需重复新增，请联系管理员检查审计。' : '请自行交付初始密码；用户首次登录须改密。'}`,
      } } })
      refreshUsers(true)
      return 'committed'
    }
    if (resp.status === 401) { deauthed('登录已失效，请重新登录'); return 'not_committed' }
    if (resp.status === 403) {
      if (data.detail === 'not_admin' || data.detail === 'password_change_required') revokeUserManagement()
      else set({ userCreate: { ...attempt, busy: false, error: '新增请求被拒绝，请刷新页面并确认管理权限后再试。' } })
      return 'not_committed'
    }
    if (data.outcome === 'not_committed') {
      set({ userCreate: { ...attempt, busy: false, error: CREATE_USER_HINT[data.detail]
        || '用户尚未创建，请检查输入；仍失败请联系管理员。',
        errorField: ['invalid_username', 'username_exists'].includes(data.detail) ? 'username'
          : data.detail === 'invalid_new_password' ? 'password' : null } })
      return 'not_committed'
    }
    throw new Error('unknown create outcome')
  } catch {
    if (state.userCreate !== attempt) return
    set({ userCreate: { ...attempt, busy: false, verifyUsername: username, notice: {
      tone: 'warning', text: `无法确认「${username}」的新增结果。请先刷新清单核实；核实前不能再次新增，系统不会自动重提。`,
    } } })
    refreshUsers(true)
    return 'unknown'
  }
}

// 启动身份检查：通过即带用户名进入数据面；否则登录壳（不建 EventSource）
export async function initAuth() {
  set({ auth: 'checking' })
  const confirmed = await confirmIdentity()
  if (confirmed === null) return
  if (confirmed) startDataPlane()
  else deauthed(null)
}

// 登录：成功后带用户名进入数据面（登录壳表单提交入口）
export async function login(username, password) {
  const epoch = identityEpoch
  const resp = await fetch('/api/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password }),
  })
  const data = await resp.json().catch(() => ({}))
  if (epoch !== identityEpoch) return
  if (!resp.ok) throw Object.assign(new Error(data.detail || `HTTP ${resp.status}`), {
    status: resp.status, detail: data.detail,
  })
  acceptIdentity(data)
  startDataPlane()
  return data
}

const PASSWORD_CHANGE_HINT = {
  current_password_incorrect: '当前密码不正确，请重新输入。',
  invalid_new_password: '新密码须为 8–128 位可见 ASCII 字符，不含空格、空白、控制字符或中文。',
  password_confirmation_mismatch: '两次新密码不一致，请检查后提交。',
  password_unchanged: '新密码必须与当前密码不同。',
  users_unavailable: '密码尚未修改：用户文件不可用，请联系管理员修复后再试。',
  audit_unavailable: '密码尚未修改：审计不可用，请联系管理员修复后再试。',
  state_unavailable: '密码尚未修改：服务处于受限恢复状态，请联系管理员。',
}

export async function changePassword(currentPassword, newPassword, confirmPassword) {
  if (state.auth !== 'password-change' || state.passwordChange.busy) return
  const attempt = { busy: true, error: null }
  set({ passwordChange: attempt })
  const backToLogin = (text, tone = 'warning') => {
    deauthed(null)
    set({ authNotice: { tone, text } })
  }
  try {
    const resp = await fetch('/api/auth/change-password', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ current_password: currentPassword, new_password: newPassword,
        confirm_password: confirmPassword, expected_version: state.userVersion }),
    })
    const data = await resp.json()
    if (state.passwordChange !== attempt) return
    if (data.outcome === 'committed') {
      const auditFailed = data.audit_status !== 'recorded'
      backToLogin(auditFailed
        ? '密码已生效，审计记录异常。请用新密码重新登录，无需重复改密，并联系管理员检查审计。'
        : '密码已更新，请用新密码重新登录。', auditFailed ? 'warning' : 'success')
    } else if ([401, 403, 409].includes(resp.status)) {
      backToLogin('本次密码尚未修改：登录已失效或用户信息已变化，请重新登录确认最新状态。')
    } else if (data.outcome === 'not_committed') {
      set({ passwordChange: { busy: false, error: PASSWORD_CHANGE_HINT[data.detail]
        || '密码尚未修改，请检查输入；仍失败请联系管理员。' } })
    } else {
      throw new Error('unknown change outcome')
    }
  } catch {
    if (state.passwordChange !== attempt) return
    backToLogin('无法确认改密结果。请先用新密码登录；失败可尝试原密码，两者均失败请联系管理员。请勿重复提交原改密请求。')
  }
}

// 登出：清服务端 Cookie 后回登录壳（数据面关停由 deauthed 完成）
export async function logout() {
  try {
    await fetch('/api/auth/logout', { method: 'POST' })
  } catch {
    // 网络失败也照样回登录壳（本地态为准）
  }
  deauthed(null)
}

// ---------- HTTP ----------

// run 对象的唯一构造点：服务端摘要（loadRuns/轮询）与新建/Fork 响应共用
// 同一形状，字段差异由 overrides 给出
function makeRun(overrides) {
  return {
    runId: null,
    status: null,
    stage: null,
    firstPrompt: null,
    title: null,
    resumedFrom: null,
    events: [],
    result: null,
    startedAt: null,
    endedAt: null,
    lastEventAt: null,
    maxSeq: 0,
    ...overrides,
  }
}

// 摘要列表拉取与合并（loadRuns 首屏与轮询共用）：新会话补进 runs，order
// 以服务端为源覆盖；容量字段（匿名全局 running count / max parallel）随
// 摘要周期一并刷新——跨用户负载可见，他人会话细节不可见。返回列表 order
// （失败返回 null，调用方各自善后）
async function fetchSummaries() {
  const epoch = identityEpoch
  const resp = await fetch('/api/runs')
  if (!resp.ok) return null
  const { runs, running_count: runningCount, max_parallel: maxParallel } = await resp.json()
  if (epoch !== identityEpoch) return null
  const map = {}
  const order = []
  for (const s of runs ?? []) {
    map[s.run_id] = mergeSummary(state.runs[s.run_id] ?? makeRun({ runId: s.run_id }), s)
    order.push(s.run_id)
  }
  const capacity = runningCount != null && maxParallel != null
    ? { runningCount, maxParallel }
    : null
  set({ runs: { ...state.runs, ...map }, order, capacity })
  return order
}

// 启动加载：拉全量 run 摘要恢复会话列表（服务重启后经 transcript 重放，
// 全部可续聊、ENDED 只读回看）；刷新恢复的标签集里不在列表的会话剪掉，
// 无存续标签（或全被剪空）时首屏打开最新一条。尽力而为，
// 失败从空开始。存续标签页的历史由快照补齐（实时事件走全局流）。
export async function loadRuns() {
  try {
    const order = await fetchSummaries()
    if (!order?.length) return
    // 存续会话标签页里在列表的保留（服务端重启丢了空会话等则剪掉；文件
    // 标签页防御性保留——启动恢复时本就没有）；全剪空（列表换代等）退回
    // 首屏开最新一条
    const liveTabs = state.tabs.filter((t) => t.kind !== 'session' || !!state.runs[t.runId])
    if (liveTabs.some((t) => t.kind === 'session')) {
      // 剪枝后记忆失效（记住的会话被剪掉）时换记末位会话标签页
      const remembered = liveTabs.some((t) => tabState.tabKey(t) === state.lastSessionKey)
      const fallbackKey = tabState.tabKey(liveTabs[liveTabs.length - 1])
      const patch = { tabs: liveTabs }
      if (!remembered) patch.lastSessionKey = fallbackKey
      if (!liveTabs.some((t) => tabState.tabKey(t) === state.activeKey)) patch.activeKey = fallbackKey
      set(patch)
      refreshOpenSnapshots()
    } else {
      // 无存续会话标签页或全被剪空：回到首屏开最新一条
      set({ tabs: [], activeKey: null, lastSessionKey: null })
      selectRun(order[0])
    }
  } catch {
    // 历史加载失败不打断使用：界面从空会话开始
  }
}

// 摘要 → run 的合并（loadRuns 与轮询共用同一形状）
function mergeSummary(run, s) {
  const session = {
    ...run,
    firstPrompt: s.first_prompt,
    resumedFrom: s.resumed_from,
    startedAt: s.started_at * 1000,
  }
  return mergeSessionSummary(session, {
    status: s.status,
    stage: s.stage,
    title: s.title ?? null,
    endedAt: s.ended_at ? s.ended_at * 1000 : null,
    lastEventAt: s.last_event_at ? s.last_event_at * 1000 : null,
  })
}

// 摘要轮询：驱动非查看中标签页的状态点与排序（全局流只覆盖打开的标签
// 页，他人会话或重启新会话只有列表最知道）。轻字段覆盖，不动 events。
// （注册在 startTimers——随登录态开合）
async function pollSummaries() {
  const epoch = identityEpoch
  try {
    await fetchSummaries()
  } catch {
    // 轮询失败静默：SSE 在的标签页不受影响，下个周期再试
  }
  // 任务清单随摘要周期刷新：未开标签页的 run 广播帧被丢弃，任务面板/
  // ECS 运行态/会话标签 pill 都靠这里保活（服务端是内存读，开销可忽略）
  if (epoch === identityEpoch) refreshTasks()
}

// 新会话落位（新建/Fork 共用）：run 注册、标签页尾插并切为查看中，再拉一次
// 快照补齐开卷事件与转录历史（广播帧可能先于 run 落位到达被守卫丢弃，
// 快照才是历史的确定入口；Last-Event-ID= 已有最大 seq，去重吸收重叠）
function adoptNewRun(run) {
  set({ runs: { ...state.runs, [run.runId]: run }, order: [run.runId, ...state.order], submitError: null })
  applyTabState(tabState.openSession(state.tabs, state.activeKey, run.runId))
  loadSnapshot(run.runId)
}

// 新建 = 一步创建空会话（READY），无中间表单；新建不受其他会话执行影响
export async function createRun() {
  const epoch = identityEpoch
  try {
    const data = await postJson('/api/runs', {})
    if (epoch !== identityEpoch) return
    adoptNewRun(
      makeRun({
        runId: data.run_id,
        status: data.status,
        resumedFrom: data.resumed_from ?? null,
        startedAt: Date.now(),
      })
    )
  } catch (err) {
    if (epoch !== identityEpoch) return
    fail(`新建会话失败：${conflictMessage(err)}`)
  }
}

// Fork = 从控制面会话（READY/ENDED）分叉新会话：事件流转录、标题继承
// （转录历史经 adoptNewRun 的快照补齐——转录不带 session.started）
export async function cloneRun() {
  const epoch = identityEpoch
  const src = state.runs[controlRunId()]
  if (!src) return
  try {
    const data = await postJson(`/api/runs/${src.runId}/clone`, {})
    if (epoch !== identityEpoch) return
    adoptNewRun(
      makeRun({
        runId: data.run_id,
        status: data.status,
        resumedFrom: data.resumed_from ?? null,
        startedAt: Date.now(),
      })
    )
  } catch (err) {
    if (epoch !== identityEpoch) return
    fail(`Fork 失败：${conflictMessage(err)}`)
  }
}

// 停止：打断控制面会话的当前回合（只作用它，不误停别人）
export async function stop() {
  const epoch = identityEpoch
  const run = state.runs[controlRunId()]
  if (!run || run.status !== RUNNING) return
  try {
    await postJson(`/api/runs/${run.runId}/stop`, {})
    if (epoch !== identityEpoch) return
  } catch (err) {
    if (epoch !== identityEpoch) return
    fail(`停止失败：${err.message}`)
  }
}

// 向控制面会话发指令：执行中发送由服务端 409（turn_in_progress）拒绝，
// 想改方向先显式停止。返回是否投递成功（失败时输入由调用方保留）。
export async function send(text) {
  const epoch = identityEpoch
  const trimmed = (text ?? '').trim()
  const run = state.runs[controlRunId()]
  if (!run || !trimmed) return false
  if (!isOperable(run.status)) {
    // 只读会话（已结束）不静默吞掉输入，给出出路提示
    fail('该会话只读（已结束）——「+ 新建」或 Fork 此会话后继续')
    return false
  }
  try {
    const data = await postJson(`/api/runs/${run.runId}/messages`, { text: trimmed })
    if (epoch !== identityEpoch) return
    setRun(run.runId, { status: data.status })
    return true
  } catch (err) {
    if (epoch !== identityEpoch) return
    fail(`发送失败：${conflictMessage(err)}`)
    return false
  }
}

// 结束控制面会话（显式、不可逆；执行中或挂起均可）
export async function endRun() {
  const epoch = identityEpoch
  const run = state.runs[controlRunId()]
  if (!run || !isOperable(run.status)) return
  try {
    await postJson(`/api/runs/${run.runId}/end`, {})
    if (epoch !== identityEpoch) return
  } catch (err) {
    if (epoch !== identityEpoch) return
    fail(`结束会话失败：${err.message}`)
  }
}

// ---------- 标签页动作（决策归 tabState 纯模块，store 只当状态容器） ----------

// tabState 结果并入状态；激活的是会话标签页时记住它（控制面记忆——
// 之后激活文件标签页不换对象）
function applyTabState({ tabs, activeKey }) {
  const active = tabs.find((t) => tabState.tabKey(t) === activeKey)
  const patch = { tabs, activeKey }
  if (active?.kind === 'session') patch.lastSessionKey = activeKey
  set(patch)
}

// 激活标签页（点击标签 / 列表行），不产生服务端动作
export function activateTab(key) {
  const t = state.tabs.find((x) => tabState.tabKey(x) === key)
  if (!t) return
  const patch = { activeKey: key }
  if (t.kind === 'session') patch.lastSessionKey = key
  set(patch)
}

// 打开（或激活既有）会话标签页并拉快照补历史：不创建会话、不产生服务端
// 动作（loadRuns 首屏恢复与列表/标签点击共用）；已开着的标签页不重拉
export function selectRun(runId) {
  const run = state.runs[runId]
  if (!run) return
  const existed = openRunIds().has(runId)
  applyTabState(tabState.openSession(state.tabs, state.activeKey, runId))
  if (!existed) loadSnapshot(runId)
}

// 关闭标签页 = 只关视图：会话标签页的历史留在内存（会话仍在列表可重开，
// 重开时快照按 Last-Event-ID 只补缺口），文件标签页内容缓存保留
export function closeTab(key) {
  const prevTabs = state.tabs
  applyTabState(tabState.closeTab(state.tabs, state.activeKey, key))
  if (state.tabs === prevTabs) return // 拦截：没有标签被关
  // 关掉的是记住的会话标签页且回退目标不是会话（记忆悬空）→ 换记末位
  if (state.lastSessionKey === key && !state.tabs.some((t) => tabState.tabKey(t) === state.lastSessionKey)) {
    const last = state.tabs.filter((t) => t.kind === 'session').at(-1)
    if (last) set({ lastSessionKey: tabState.tabKey(last) })
  }
}

// ---------- 产物 ----------

// 清单组 → 组内全部文件的根前缀相对路径（勾选/下载的寻址形态）
export function groupRelPaths(group) {
  return group.files.map((f) => (group.dir ? `${group.dir}/${f.name}` : f.name))
}

// 勾选集剪枝：清单刷新后消失的文件移出勾选（否则 zip 请求会带上已
// 不存在的路径——服务端会跳过，但计数与按钮文案先骗了人）
function pruneSelection(groups) {
  const listed = new Set()
  for (const g of groups) for (const p of groupRelPaths(g)) listed.add(p)
  const next = {}
  for (const p of Object.keys(state.artifactSel)) if (listed.has(p)) next[p] = true
  return next
}

// 清单刷新：阶段推进/终态事件触发（无 run 参数，全局镜像）
export async function refreshArtifacts() {
  const epoch = identityEpoch
  try {
    const resp = await fetch('/api/artifacts')
    if (!resp.ok) return
    const data = await resp.json()
    if (epoch !== identityEpoch) return
    set({ artifacts: data, artifactSel: pruneSelection(data.groups) })
  } catch {
    // 清单刷新是尽力而为：失败不打断会话观察，下次阶段事件再试
  }
}

// 勾选单个产物（行内复选框）
export function toggleArtifactSel(relPath) {
  const next = { ...state.artifactSel }
  if (next[relPath]) delete next[relPath]
  else next[relPath] = true
  set({ artifactSel: next })
}

// 批量勾选/取消一组路径（组头全选、卡头全选用）
export function setArtifactSel(relPaths, on) {
  const next = { ...state.artifactSel }
  for (const p of relPaths) {
    if (on) next[p] = true
    else delete next[p]
  }
  set({ artifactSel: next })
}

export function clearArtifactSel() {
  set({ artifactSel: {} })
}

// ---------- 输入草稿 ----------

// 每枚会话标签页独立草稿（runId → 文本），切标签页不丢输入中的字；发送
// 成功后由调用方清空。入 state 容器：受控输入的字必须驱动重渲染，否则
// 下一次外来渲染（轮询/SSE/时长针）会用旧 value 把 DOM 里的字冲掉
const drafts = {}

export function draftOf(runId) {
  return drafts[runId] ?? ''
}

export function setDraft(runId, text) {
  drafts[runId] = text
  set({ drafts: { ...drafts } })
}

export function clearDraft(runId) {
  delete drafts[runId]
  set({ drafts: { ...drafts } })
}

// ---------- 产物文件标签页 ----------

// 打开产物文件标签页：已有则只激活；新则插当前激活标签页右侧并按需拉取
// 内容进多槽缓存（relPath → 条目+content）。二进制产物（清单带 binary
// 标记，如 rpms/ 下的 .rpm 包）不拉内容，占位视图元信息来自清单条目。
// relPath 形如 "rpm/nginx/1.25.3/nginx-rpm-result.md"；逐段编码（整段
// encode 会把 / 也编码）。内容缓存与标签页独立——关标签页不清缓存，
// 重开瞬开。
export async function openArtifact(relPath, entry) {
  const epoch = identityEpoch
  if (entry?.binary) {
    const cut = relPath.lastIndexOf('/')
    set({
      artifactCache: {
        ...state.artifactCache,
        [relPath]: {
          dir: cut > 0 ? relPath.slice(0, cut) : '',
          name: entry.name,
          stage: entry.stage ?? null,
          size: entry.size ?? null,
          binary: true,
        },
      },
    })
  } else if (!state.artifactCache[relPath]) {
    try {
      const resp = await fetch(`/api/artifacts/file/${relPath.split('/').map(encodeURIComponent).join('/')}`)
      const data = await resp.json().catch(() => ({}))
      if (epoch !== identityEpoch) return
      if (!resp.ok) {
        fail(`打开产物失败：${data.detail || `HTTP ${resp.status}`}`)
        return
      }
      set({ artifactCache: { ...state.artifactCache, [relPath]: data } })
    } catch (err) {
      if (epoch !== identityEpoch) return
      fail(`打开产物失败：${err.message}`)
      return
    }
  }
  applyTabState(tabState.openFile(state.tabs, state.activeKey, relPath, entry?.name))
}

// 单文件下载：服务端带附件头，临时 <a> 触发浏览器下载（不离开当前页）
export function downloadArtifact(relPath) {
  const a = document.createElement('a')
  a.href = `/api/artifacts/download/${relPath.split('/').map(encodeURIComponent).join('/')}`
  document.body.appendChild(a)
  a.click()
  a.remove()
}

// 批量下载：勾选集 POST 到 zip 端点，blob 经 objectURL 触发下载；文件名
// 取服务端 Content-Disposition（auto-image-artifacts-<n>-<时间戳>.zip）
export async function downloadArtifactZip() {
  const epoch = identityEpoch
  const paths = Object.keys(state.artifactSel)
  if (!paths.length || state.artifactZipping) return
  set({ artifactZipping: true })
  try {
    const resp = await fetch('/api/artifacts/zip', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ paths }),
    })
    if (!resp.ok) {
      const data = await resp.json().catch(() => ({}))
      if (epoch !== identityEpoch) return
      fail(`打包下载失败：${data.detail || `HTTP ${resp.status}`}`)
      return
    }
    const disposition = resp.headers.get('Content-Disposition') || ''
    const match = disposition.match(/filename="?([^";]+)"?/)
    const blob = await resp.blob()
    if (epoch !== identityEpoch) return
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = match ? match[1] : 'auto-image-artifacts.zip'
    document.body.appendChild(a)
    a.click()
    a.remove()
    URL.revokeObjectURL(url)
  } catch (err) {
    if (epoch !== identityEpoch) return
    fail(`打包下载失败：${err.message}`)
  } finally {
    if (epoch === identityEpoch) set({ artifactZipping: false })
  }
}

// ---------- OBS 产物 ----------

// 桶内清单刷新（启动即拉一次 + 面板刷新钮；尽力而为，失败落面板错误行
// 不打断使用。流水线事件不联动——OBS 上传不经过本服务的已知事件面）
export async function refreshObs() {
  const epoch = identityEpoch
  if (state.obs.loading) return
  set({ obs: { ...state.obs, loading: true } })
  try {
    const resp = await fetch('/api/obs/objects?limit=1000')
    const data = await resp.json().catch(() => ({}))
    if (epoch !== identityEpoch) return
    if (!resp.ok) {
      set({ obs: { ...state.obs, loading: false, error: data.detail || `HTTP ${resp.status}` } })
      return
    }
    set({
      obs: {
        objects: data.objects ?? [],
        bucket: data.bucket ?? null,
        region: data.region ?? null,
        domain: data.domain ?? null,
        error: null,
        loading: false,
      },
    })
  } catch (err) {
    if (epoch !== identityEpoch) return
    set({ obs: { ...state.obs, loading: false, error: err.message } })
  }
}

// 打开 OBS 对象标签页：已有则只激活；新则拉文本预览进多槽缓存（对象 key
// 寻址，fetch 前逐段编码）。二进制/超限对象（端点 422）不拉内容，占位
// 视图元信息取清单条目。
export async function openObsObject(key, entry) {
  const epoch = identityEpoch
  if (!state.obsCache[key]) {
    try {
      const resp = await fetch(`/api/obs/content?key=${encodeURIComponent(key)}`)
      const data = await resp.json().catch(() => ({}))
      if (epoch !== identityEpoch) return
      if (resp.ok) {
        set({ obsCache: { ...state.obsCache, [key]: data } })
      } else if (resp.status === 422) {
        // 二进制/超限对象不可预览：占位视图元信息来自清单条目
        const cut = key.lastIndexOf('/')
        set({
          obsCache: {
            ...state.obsCache,
            [key]: {
              dir: cut > 0 ? key.slice(0, cut) : '',
              name: entry?.name ?? key.slice(cut + 1),
              size: entry?.size ?? null,
              binary: true,
            },
          },
        })
      } else {
        fail(`打开 OBS 对象失败：${data.detail || `HTTP ${resp.status}`}`)
        return
      }
    } catch (err) {
      if (epoch !== identityEpoch) return
      fail(`打开 OBS 对象失败：${err.message}`)
      return
    }
  }
  applyTabState(tabState.openObs(state.tabs, state.activeKey, key, entry?.name))
}

// OBS 对象下载：先取签名链接（服务端本地签名），临时 <a> 新标签打开——
// 文本浏览器直接渲染，二进制按 OBS 响应下载
export async function downloadObsObject(key) {
  const epoch = identityEpoch
  try {
    const resp = await fetch(`/api/obs/url?key=${encodeURIComponent(key)}`)
    const data = await resp.json().catch(() => ({}))
    if (epoch !== identityEpoch) return
    if (!resp.ok) {
      fail(`获取 OBS 下载链接失败：${data.detail || `HTTP ${resp.status}`}`)
      return
    }
    const a = document.createElement('a')
    a.href = data.signed_url
    a.target = '_blank'
    a.rel = 'noopener'
    document.body.appendChild(a)
    a.click()
    a.remove()
  } catch (err) {
    if (epoch !== identityEpoch) return
    fail(`获取 OBS 下载链接失败：${err.message}`)
  }
}

// 复制文本到剪贴板：优先 navigator.clipboard（仅安全上下文 https/localhost
// 可用），不可用或被拒回退 execCommand——http://IP 访问形态必须兜底
export async function copyText(text) {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text)
      return true
    }
  } catch {
    // 权限拒绝等：落到兜底路径
  }
  try {
    const ta = document.createElement('textarea')
    ta.value = text
    ta.style.position = 'fixed'
    ta.style.opacity = '0'
    document.body.appendChild(ta)
    ta.focus()
    ta.select()
    const copied = document.execCommand('copy')
    ta.remove()
    return copied
  } catch {
    return false
  }
}

// 复制 OBS 对象签名下载链接（7 天有效）：返回是否复制成功（行级按钮据此
// 打 ✓ 反馈）。两条兜底出口：剪贴板全拒时链接打到控制台供手动复制
export async function copyObsLink(key) {
  const epoch = identityEpoch
  try {
    const resp = await fetch(`/api/obs/url?key=${encodeURIComponent(key)}&expires=604800`)
    const data = await resp.json().catch(() => ({}))
    if (epoch !== identityEpoch) return
    if (!resp.ok) {
      fail(`获取 OBS 下载链接失败：${data.detail || `HTTP ${resp.status}`}`)
      return false
    }
    const copied = await copyText(data.signed_url)
    if (epoch !== identityEpoch) return
    if (copied) {
      ok(`已复制下载链接（签名 7 天有效）：${key}`)
      return true
    }
    // eslint-disable-next-line no-console
    console.log('OBS 签名链接（剪贴板不可用，请手动复制）：', data.signed_url)
    fail('复制到剪贴板失败（浏览器限制）——链接已打印到控制台（F12），可手动复制')
    return false
  } catch (err) {
    if (epoch !== identityEpoch) return
    fail(`获取 OBS 下载链接失败：${err.message}`)
    return false
  }
}

// 批量归档本地产物到 OBS：勾选集 → POST /api/obs/archive（服务端经产物
// 路径约束解析后逐个 putFile，对象名 = 产物路径），完成后刷新 OBS 清单；
// 部分失败如实逐项点名
export async function archiveToObs() {
  const epoch = identityEpoch
  const paths = Object.keys(state.artifactSel)
  if (!paths.length || state.obsArchiving) return
  set({ obsArchiving: true })
  try {
    const resp = await fetch('/api/obs/archive', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ paths }),
    })
    const data = await resp.json().catch(() => ({}))
    if (epoch !== identityEpoch) return
    if (!resp.ok) {
      fail(`归档到 OBS 失败：${data.detail || `HTTP ${resp.status}`}`)
      return
    }
    refreshObs()
    if (data.failed?.length) {
      fail(`归档完成：${data.count} 成功、${data.failed.length} 失败（${data.failed.map((f) => f.key).join('、')}）`)
    } else {
      ok(`已归档 ${data.count} 个产物到 OBS（对象名 = 产物路径）`)
    }
  } catch (err) {
    if (epoch !== identityEpoch) return
    fail(`归档到 OBS 失败：${err.message}`)
  } finally {
    if (epoch === identityEpoch) set({ obsArchiving: false })
  }
}

// 打包归档：勾选集 → 自定义包名（prompt，取消即中止）→ 服务端内存打 zip
// 直传 OBS（对象名固定 zip/ 前缀，.zip 后缀服务端自动补，同名覆盖）
export async function archiveZipToObs() {
  const epoch = identityEpoch
  const paths = Object.keys(state.artifactSel)
  if (!paths.length || state.obsZipArchiving) return
  const d = new Date()
  const pad = (n) => String(n).padStart(2, '0')
  const def = `bundle-${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}-${pad(d.getHours())}${pad(d.getMinutes())}`
  const answer = window.prompt(`打包 ${paths.length} 个产物为 zip（上传到 OBS 的 zip/ 目录，自动补 .zip 后缀）\n包名：`, def)
  if (answer == null) return // 取消：不动
  const name = String(answer).trim()
  if (!name) {
    fail('zip 包名不能为空')
    return
  }
  set({ obsZipArchiving: true })
  try {
    const resp = await fetch('/api/obs/archive-zip', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ paths, name }),
    })
    const data = await resp.json().catch(() => ({}))
    if (epoch !== identityEpoch) return
    if (!resp.ok) {
      fail(`打包归档失败：${data.detail || `HTTP ${resp.status}`}`)
      return
    }
    refreshObs()
    ok(`已打包 ${data.zipped} 个产物 → ${data.key}（${fmtSize(data.size)}，同名覆盖）`)
  } catch (err) {
    if (epoch !== identityEpoch) return
    fail(`打包归档失败：${err.message}`)
  } finally {
    if (epoch === identityEpoch) set({ obsZipArchiving: false })
  }
}

// ---------- ECS 实例 ----------

// 502 家族 detail 是对象（服务端 SDK 错误面）：人话化成字符串供错误行/toast
const ecsErrText = (detail, fallback) =>
  typeof detail === 'object' && detail !== null
    ? JSON.stringify(detail)
    : detail || fallback

// 实例清单刷新（启动即拉一次 + 面板刷新钮；尽力而为，失败落面板错误行
// 不打断使用。云侧变化不经本服务事件面，无联动——刷新钮手动重拉。已有
// 存活检查结果按 id 保留，✓/✗ 不被刷新清掉）
export async function refreshEcs() {
  const epoch = identityEpoch
  if (state.ecs.loading) return
  set({ ecs: { ...state.ecs, loading: true } })
  try {
    const resp = await fetch('/api/ecs/instances?limit=1000')
    const data = await resp.json().catch(() => ({}))
    if (epoch !== identityEpoch) return
    if (!resp.ok) {
      set({ ecs: { ...state.ecs, loading: false, error: ecsErrText(data.detail, `HTTP ${resp.status}`) } })
      return
    }
    const prev = {}
    state.ecs.instances.forEach((i) => {
      if (i.checked_at != null) prev[i.id] = i
    })
    set({
      ecs: {
        instances: (data.instances ?? []).map((i) => (
          prev[i.id]
            ? { ...i, ssh_port_open: prev[i.id].ssh_port_open, alive: prev[i.id].alive, checked_at: prev[i.id].checked_at }
            : i
        )),
        region: data.region ?? null,
        error: null,
        loading: false,
      },
    })
  } catch (err) {
    if (epoch !== identityEpoch) return
    set({ ecs: { ...state.ecs, loading: false, error: err.message } })
  }
}

// 一键存活检查：POST /api/ecs/check（服务端并发探测 22 端口），结果按 id
// 合并进清单（行内 ✓/✗）；列表里已消失的实例如实清掉检查标记
export async function checkEcs() {
  const epoch = identityEpoch
  if (state.ecs.checking) return
  set({ ecs: { ...state.ecs, checking: true } })
  try {
    const data = await postJson('/api/ecs/check', {})
    if (epoch !== identityEpoch) return
    const byId = Object.fromEntries((data.instances ?? []).map((i) => [i.id, i]))
    set({
      ecs: {
        ...state.ecs,
        checking: false,
        region: data.region ?? state.ecs.region,
        instances: state.ecs.instances.map((i) => (
          byId[i.id]
            ? { ...i, ssh_port_open: byId[i.id].ssh_port_open, alive: byId[i.id].alive, checked_at: data.checked_at }
            : i
        )),
      },
    })
    ok(`存活检查：${data.alive_count ?? 0}/${data.count ?? 0} 存活`)
  } catch (err) {
    if (epoch !== identityEpoch) return
    set({ ecs: { ...state.ecs, checking: false } })
    fail(`存活检查失败：${ecsErrText(err.detail, err.message)}`)
  }
}

// 建机（分钟级长请求；fetch 无超时正是所需）。成功与未就绪（ok=false，
// 机器可能已建出）都返回服务端契约供对话框渲染，HTTP 层失败抛错由对话
// 框行内展示；两种收尾都刷新清单
export async function createEcs(body) {
  const epoch = identityEpoch
  if (state.ecsCreating) throw new Error('建机请求进行中')
  set({ ecsCreating: true })
  try {
    const data = await postJson('/api/ecs/create', body)
    if (epoch !== identityEpoch) return
    refreshEcs()
    return data
  } catch (err) {
    if (epoch !== identityEpoch) return
    fail(`创建 ECS 失败：${ecsErrText(err.detail, err.message)}`)
    throw err
  } finally {
    if (epoch === identityEpoch) set({ ecsCreating: false })
  }
}

// 启动即恢复任务列表（含服务重启后经 transcript 重建的历史）与产物清单
// （loadRuns 无历史时提前 return，产物首刷不能依赖它）；OBS 清单、ECS
// 实例与流水线任务清单同样尽力拉一次——全部挪进登录后的数据面启动
// （startDataPlane），未认证不拉任何业务数据
initAuth()
