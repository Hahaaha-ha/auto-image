// 任务面板：流水线任务清单（rpm-*/deploy-* 子 agent 派发即建，服务端
// task/ 目录持久化）。每行：状态点 + 任务名（类型+软件+版本+时间戳）+
// 阶段流转图（done ✓ / current 呼吸 / upcoming 灰）+ token（缓存/输入/
// 输出；进行中为 —，回合结束才采到总量）+ 机器 + 会话 chip（点击跳会话
// 标签页）。行可展开看各阶段起止时刻。「会话标签上的任务 pill」与
// ECS 运行态都跳/联到这里（openTask 高亮）。
import { useEffect, useState } from 'react'
import * as store from '../store.js'
import { fmtAgo, fmtTokens, STAGE_LABEL, TASK_OUTCOME_LABEL, TASK_TYPE_LABEL, stageOrder } from '../derive.js'

// 新建任务对话框：软件名/版本/类型（镜像|RPM）+ 可选安装文档链接 +
// 部署目标 ECS（按需创建：参数预填 scope 默认、只提交覆盖项；已有 ECS：
// 下拉选实例）。提交 → INIT 待运行态；「▶ 运行」才起会话。
function TaskCreateDialog({ onClose }) {
  const [instances, setInstances] = useState(null) // null=加载中 [] =空
  const [instErr, setInstErr] = useState(null)
  const [defaults, setDefaults] = useState(null)
  const [form, setForm] = useState({
    taskType: 'image', software: '', version: '', installDoc: '',
    ecsMode: 'create', ecsInstance: '',
    image: '', flavor: '', name: '', diskSize: '', bandwidth: '',
  })
  const [msg, setMsg] = useState(null)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    // 已有 ECS 下拉数据与创建参数默认值并行拉取（尽力而为，失败不阻断）
    fetch('/api/ecs/instances?limit=1000')
      .then(async (r) => {
        const data = await r.json().catch(() => ({}))
        if (!r.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `HTTP ${r.status}`)
        setInstances(data.instances ?? [])
      })
      .catch((e) => setInstErr(e.message))
    fetch('/api/ecs/defaults')
      .then(async (r) => {
        const data = await r.json().catch(() => ({}))
        if (r.ok) {
          setDefaults(data)
          setForm((f) => ({
            ...f,
            flavor: data.flavor ?? '',
            image: data.image ?? '',
            diskSize: String(data.disk_size ?? ''),
            bandwidth: String(data.bandwidth ?? ''),
          }))
        }
      })
      .catch(() => { /* 默认值失败只影响预填，可手填 */ })
  }, [])

  const set = (k) => (e) => setForm({ ...form, [k]: e.target.value })

  const submit = async () => {
    if (saving) return
    if (!form.software.trim()) {
      setMsg({ kind: 'err', text: '软件名称必填' })
      return
    }
    if (form.ecsMode === 'existing' && !form.ecsInstance) {
      setMsg({ kind: 'err', text: '请选择一个已有 ECS 实例' })
      return
    }
    const spec = {
      software: form.software.trim(),
      version: form.version.trim(),
      install_doc: form.installDoc.trim(),
      task_type: form.taskType,
      ecs_mode: form.ecsMode,
    }
    if (form.ecsMode === 'existing') {
      const inst = (instances ?? []).find((i) => i.id === form.ecsInstance)
      if (inst) spec.ecs_instance = { id: inst.id, name: inst.name, ip: inst.ip }
    } else {
      const params = {}
      if (form.image.trim()) params.image = form.image.trim()
      if (form.flavor.trim()) params.flavor = form.flavor.trim()
      if (form.name.trim()) params.name = form.name.trim()
      if (form.diskSize.trim()) params.disk_size = Number(form.diskSize)
      if (form.bandwidth.trim()) params.bandwidth = Number(form.bandwidth)
      spec.ecs_params = params
    }
    setSaving(true)
    setMsg(null)
    try {
      await store.createTask(spec)
      onClose()
    } catch (e) {
      const detail = typeof e.detail === 'object' && e.detail ? JSON.stringify(e.detail) : (e.detail || e.message)
      setMsg({ kind: 'err', text: `创建失败：${detail}` })
    } finally {
      setSaving(false)
    }
  }

  const radio = (group, value, label) => (
    <label className="va-task-radio">
      <input
        type="radio"
        name={group}
        checked={form[group] === value}
        onChange={() => setForm({ ...form, [group]: value })}
      />
      <span>{label}</span>
    </label>
  )

  const field = (k, label, placeholder = '') => (
    <label className="va-cfg-field">
      <span>{label}</span>
      <input
        className="va-cfg-input"
        type="text"
        autoComplete="off"
        value={form[k]}
        onChange={set(k)}
        placeholder={placeholder}
      />
    </label>
  )

  return (
    <div className="va-modal-overlay" onClick={onClose}>
      <div className="va-modal" onClick={(e) => e.stopPropagation()} role="dialog" aria-label="新建任务">
        <div className="va-modal-title">
          <span>新建任务</span>
          <span className="va-cfg-sub">提交后为「待运行」，点任务的 ▶ 运行才创建会话执行</span>
          <button className="va-modal-close" onClick={onClose} title="关闭">✕</button>
        </div>
        <div className="va-cfg-fields">
          <div className="va-cfg-field">
            <span>类型</span>
            <span className="va-task-radios">
              {radio('taskType', 'image', '镜像（部署并制镜像）')}
              {radio('taskType', 'rpm', 'RPM（制作 RPM 包）')}
            </span>
          </div>
          {field('software', '软件名称 *', '如 nginx')}
          {field('version', '版本', '留空部署最新稳定版')}
          {field('installDoc', '安装文档链接', '留空由 agent 检索官方文档')}
          <div className="va-cfg-field">
            <span>部署目标</span>
            <span className="va-task-radios">
              {radio('ecsMode', 'create', '按需创建 ECS')}
              {radio('ecsMode', 'existing', '已有 ECS')}
            </span>
          </div>
          {form.ecsMode === 'create' ? (
            <>
              {field('image', '镜像 ID', defaults?.image ? `默认 ${defaults.image}` : 'scope 默认')}
              {field('flavor', '规格', defaults?.flavor ? `默认 ${defaults.flavor}` : '如 c6.xlarge.2')}
              {field('name', 'ECS 名称', '留空自动命名')}
              {field('diskSize', '系统盘 GB', defaults ? `默认 ${defaults.disk_size}` : '默认 40')}
              {field('bandwidth', '带宽 Mbit/s', defaults ? `默认 ${defaults.bandwidth}` : '默认 5')}
            </>
          ) : (
            <label className="va-cfg-field">
              <span>选择实例</span>
              {instErr ? (
                <span className="va-cfg-msg err">ECS 清单拉取失败：{instErr}</span>
              ) : instances == null ? (
                <span className="va-cfg-sub">实例清单加载中…</span>
              ) : instances.length === 0 ? (
                <span className="va-cfg-sub">当前 region 无 ECS 实例</span>
              ) : (
                <select className="va-ecs-select" value={form.ecsInstance} onChange={set('ecsInstance')}>
                  <option value="">— 请选择 —</option>
                  {instances.map((i) => (
                    <option key={i.id} value={i.id}>
                      {i.name}（{i.ip ?? i.id}，{i.status}）
                    </option>
                  ))}
                </select>
              )}
            </label>
          )}
        </div>
        {msg && <div className={`va-cfg-msg ${msg.kind}`}>{msg.text}</div>}
        <div className="va-modal-actions">
          <button className="va-cfg-cancel" onClick={onClose} disabled={saving}>取消</button>
          <button className="va-cfg-save" onClick={submit} disabled={saving}>创建任务</button>
        </div>
      </div>
    </div>
  )
}

// 阶段流转图：按类型排 pill（镜像 GUIDE→INSTALL→VERIFY→ARCHIVE；RPM
// GUIDE→BUILD→VERIFY→ARCHIVE），done=绿✓、current=蓝呼吸、upcoming=灰。
// 复用产物徽标的阶段配色（va-art-badge s-*）+ 流转态类。
function StageFlow({ task }) {
  const order = stageOrder(task.type)
  const entries = task.stages ?? []
  return (
    <div className="va-flow" aria-label="阶段流转">
      {order.map((stage, i) => {
        const starts = entries.filter((s) => s.stage === stage)
        const last = starts[starts.length - 1]
        // 已开始过且（已结束 或 后面阶段已经开过）= done；正在跑且没有
        // 更晚的阶段条目 = current；从未开始 = upcoming
        const laterStarted = order.slice(i + 1).some((st) => entries.some((s) => s.stage === st))
        const isCurrent = task.status === 'RUNNING' && stage === task.currentStage
        const phase = starts.length === 0 ? 'upcoming' : (isCurrent && !laterStarted ? 'current' : 'done')
        return (
          <span key={stage} className="va-flow-seg">
            {i > 0 && <span className="va-flow-arrow">→</span>}
            <span
              className={`va-flow-pill s-${stage.toLowerCase()} ${phase}`}
              title={last ? `${STAGE_LABEL[stage] ?? stage}：${fmtClock(last.startedAt)}${last.endedAt ? ` → ${fmtClock(last.endedAt)}` : ' 进行中'}` : STAGE_LABEL[stage] ?? stage}
            >
              {phase === 'done' ? '✓ ' : ''}{STAGE_LABEL[stage] ?? stage}
            </span>
          </span>
        )
      })}
    </div>
  )
}

const fmtClock = (ts) => (ts ? new Date(ts * 1000).toTimeString().slice(0, 8) : '—')

function TaskRow({ task, active, expanded, onToggle }) {
  const cache = task.usage ? (task.usage.cacheReadTokens ?? 0) + (task.usage.cacheCreationTokens ?? 0) : null
  const isInit = task.status === 'INIT'
  return (
    <div className={`va-task-row${active ? ' on' : ''}`}>
      <div className="va-task-main" role="button" tabIndex={0} onClick={onToggle}
           onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onToggle() } }}
           title={task.turnText || task.name}>
        <div className="va-task-name">
          {isInit ? (
            <span className="va-ecs-status s-other" title="待运行" />
          ) : task.status === 'RUNNING' ? (
            <span className="va-ecs-status s-build" title="进行中" />
          ) : (
            <span className={`va-ecs-status ${task.outcome === 'success' ? 's-active' : task.outcome === 'failed' ? 's-error' : 's-shutoff'}`}
                  title={TASK_OUTCOME_LABEL[task.outcome] ?? task.outcome} />
          )}
          <span className="va-task-name-text">{task.name}</span>
          <span className={`va-task-outcome o-${isInit ? 'pending' : task.outcome ?? 'none'}`}>
            {isInit ? '待运行' : task.status === 'RUNNING' ? '进行中' : (TASK_OUTCOME_LABEL[task.outcome] ?? '已完成')}
          </span>
        </div>
        <StageFlow task={task} />
        <div className="va-task-sub">
          <span className="va-task-chip">{TASK_TYPE_LABEL[task.type] ?? task.type}</span>
          {isInit && task.spec && (
            <span className="va-task-chip">
              {task.spec.ecsMode === 'existing' ? `已有 ${task.spec.ecsInstance?.name ?? ''}` : '按需创建'}
            </span>
          )}
          <span title="缓存 token = 读 + 写">缓存 {fmtTokens(cache)}</span>
          <span title="输入 / 输出 token">入 {fmtTokens(task.usage?.inputTokens)} / 出 {fmtTokens(task.usage?.outputTokens)}</span>
          <span className="va-task-ago">{fmtAgo((task.createdAt ?? 0) * 1000)}</span>
        </div>
      </div>
      <div className="va-task-actions">
        {isInit ? (
          <button
            className="va-task-run"
            onClick={() => store.runTask(task.taskId)}
            title="创建会话执行本任务（自动跳到该会话）"
          >
            ▶ 运行
          </button>
        ) : (
          <button
            className="va-task-jump"
            onClick={() => store.selectRun(task.runId)}
            title={`跳到关联会话 ${task.runId}（打开/激活其标签页）`}
          >
            ⑂ 会话
          </button>
        )}
      </div>
      {expanded && (
        <div className="va-task-detail">
          <div className="va-task-line"><span>任务 ID</span><code>{task.taskId}</code></div>
          {task.runId && <div className="va-task-line"><span>会话</span><code>{task.runId}</code></div>}
          {task.spec && (
            <>
              <div className="va-task-line">
                <span>ECS 目标</span>
                <code>
                  {task.spec.ecsMode === 'existing'
                    ? `已有：${task.spec.ecsInstance?.name ?? '—'}（${task.spec.ecsInstance?.ip ?? ''}）`
                    : '按需创建'}
                </code>
              </div>
              {task.spec.ecsMode === 'create' && task.spec.ecsParams && Object.keys(task.spec.ecsParams).length > 0 && (
                <div className="va-task-line">
                  <span>ECS 参数</span>
                  <code>{Object.entries(task.spec.ecsParams).map(([k, v]) => `${k}=${v}`).join('，')}</code>
                </div>
              )}
              {task.spec.installDoc && (
                <div className="va-task-line"><span>文档</span><code>{task.spec.installDoc}</code></div>
              )}
            </>
          )}
          <div className="va-task-line">
            <span>机器</span>
            <code>{task.instanceId ?? task.serverAlias ?? '—'}</code>
            {task.serverAlias && task.instanceId && <span className="va-task-note">别名 {task.serverAlias}</span>}
          </div>
          {(task.stages ?? []).map((s, i) => (
            <div className="va-task-line" key={i}>
              <span>{STAGE_LABEL[s.stage] ?? s.stage}</span>
              <code>{fmtClock(s.startedAt)} → {s.endedAt ? fmtClock(s.endedAt) : '进行中'}</code>
            </div>
          ))}
          {task.usage && (
            <div className="va-task-line">
              <span>token</span>
              <code>
                缓存读 {fmtTokens(task.usage.cacheReadTokens)} · 缓存写 {fmtTokens(task.usage.cacheCreationTokens)}
                {' '}· 入 {fmtTokens(task.usage.inputTokens)} · 出 {fmtTokens(task.usage.outputTokens)}
              </code>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

export default function TasksPanel() {
  const s = store.useRunState()
  const [expanded, setExpanded] = useState({})
  const [createOpen, setCreateOpen] = useState(false)
  const running = s.tasks.filter((t) => t.status === 'RUNNING').length
  return (
    <div className="va-side-panel">
      <div className="va-art-panel-head va-obs-head">
        <span>任务 · {s.tasks.length}</span>
        {running > 0 && (
          <span className="va-obs-bucket" title="进行中的流水线任务">{running} 进行中</span>
        )}
        <button
          className="va-obs-refresh"
          onClick={() => setCreateOpen(true)}
          title="手动创建任务（提交后为待运行，点 ▶ 运行才创建会话执行）"
        >
          + 新建任务
        </button>
        <button
          className="va-obs-refresh"
          onClick={() => store.refreshTasks()}
          title="重拉任务清单"
        >
          ⟳ 刷新
        </button>
      </div>
      {s.tasks.length === 0 && (
        <div className="va-side-empty">暂无任务（流水线派发即建，或点「+ 新建任务」手动创建）</div>
      )}
      {s.tasks.map((t) => (
        <TaskRow
          key={t.taskId}
          task={t}
          active={s.activeTaskId === t.taskId}
          expanded={!!expanded[t.taskId]}
          onToggle={() => setExpanded({ ...expanded, [t.taskId]: !expanded[t.taskId] })}
        />
      ))}
      {createOpen && <TaskCreateDialog onClose={() => setCreateOpen(false)} />}
    </div>
  )
}
