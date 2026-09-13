// ECS 实例面板：全量清单（/api/ecs/instances，状态点着色）+ 一键存活
// 检查（POST /api/ecs/check：ACTIVE 且 22 端口可达 = 存活，✓/✗ 行内
// 显示）+ 新建对话框（GET /api/ecs/defaults 预填 → POST /api/ecs/create
// 同步等到就绪，分钟级长请求，完成后展示 IP 与登录密码——密码仅此一次
// 返回，密钥对登录则提示 scope key_name）。清单不随流水线事件联动——
// 云侧变化不经本服务事件面，刷新钮手动重拉 + 检查/建机完成后自动刷新。
import { useEffect, useState } from 'react'
import * as store from '../store.js'
import { fmtAgo, STAGE_LABEL } from '../derive.js'

// 状态点配色：ACTIVE 绿 / BUILD·REBOOT 蓝（呼吸）/ SHUTOFF·DELETED 灰 /
// ERROR 红 / 其余空心（未知）
const STATUS_CLASS = {
  ACTIVE: 's-active',
  SHUTOFF: 's-shutoff',
  DELETED: 's-deleted',
  ERROR: 's-error',
  FAILED: 's-error',
}
function statusClass(status) {
  if (STATUS_CLASS[status]) return STATUS_CLASS[status]
  if (status && (status.startsWith('BUILD') || status === 'REBOOT' || status === 'HARD_REBOOT')) {
    return 's-build'
  }
  return 's-other'
}

function StatusDot({ status }) {
  return <span className={`va-ecs-status ${statusClass(status)}`} title={status} />
}

function EcsRow({ inst }) {
  const createdMs = inst.created ? Date.parse(inst.created) : null
  // 运行任务中 vs 空闲中：与进行中任务按 instance_id 精确 join（已有别名
  // 安装路径按 ecs-<别名> 名字兜底），任务清单随摘要周期刷新保活
  const task = store.runningTaskByInstance(inst)
  return (
    <div className="va-ecs-row" title={inst.id}>
      <StatusDot status={inst.status} />
      <div className="va-ecs-main">
        <div className="va-ecs-name">
          <span className="va-ecs-name-text">{inst.name || inst.id}</span>
          <span className="va-ecs-status-text">{inst.status}</span>
          {inst.checked_at != null && (
            inst.alive ? (
              <span className="va-ecs-alive ok" title="存活：ACTIVE 且 22 端口可达">✓</span>
            ) : (
              <span className="va-ecs-alive bad" title="不存活（非 ACTIVE 或 22 不可达；私网 IP 从外部探测不通也算不存活）">✗</span>
            )
          )}
        </div>
        <div className="va-ecs-sub">
          <span className="va-ecs-flavor">{inst.flavor || '—'}</span>
          {inst.ip ? (
            <span className="va-ecs-ip" title={inst.ip_type === 'floating' ? '公网浮动 IP' : '私网固定 IP'}>
              {inst.ip}{inst.ip_type === 'private' ? '（私网）' : ''}
            </span>
          ) : (
            <span className="va-ecs-ip none">无 IP</span>
          )}
          {createdMs && <span className="va-ecs-ago">{fmtAgo(createdMs)}</span>}
        </div>
        {task ? (
          <button
            className="va-ecs-task"
            onClick={() => store.openTask(task.taskId)}
            title={`${task.name} · ${task.taskId}（点击跳到任务面板）`}
          >
            运行任务中：{task.software ?? '未知'}{task.version ? ` ${task.version}` : ''}
            {' '}· {STAGE_LABEL[task.currentStage] ?? task.currentStage ?? '—'}
          </button>
        ) : (
          <span className="va-ecs-idle">空闲中</span>
        )}
      </div>
    </div>
  )
}

// 密码行：默认遮蔽（type=password），👁 切换显示，⧉ 复制（成功 ✓ 1.5 秒）
function PasswordRow({ value }) {
  const [show, setShow] = useState(false)
  const [copied, setCopied] = useState(false)
  const copy = async () => {
    if (await store.copyText(value)) {
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    }
  }
  return (
    <div className="va-cfg-row">
      <span className="va-cfg-label">登录密码</span>
      <span className="va-cfg-value">
        <input className="va-ecs-pwd" type={show ? 'text' : 'password'} readOnly value={value} />
        <button className="va-ecs-pwd-btn" onClick={() => setShow(!show)} title={show ? '遮蔽' : '显示'}>
          {show ? '🙈' : '👁'}
        </button>
        <button className="va-ecs-pwd-btn" onClick={copy} title="复制密码">{copied ? '✓' : '⧉'}</button>
      </span>
    </div>
  )
}

// 新建对话框：表单仅名称/规格/镜像/系统盘/带宽五项（其余网络与登录配置
// 全由 scope.yaml ecs_create.server 默认）；提交后同步等就绪（服务端轮询
// ACTIVE + IP + 22 通，通常 1–10 分钟），期间保持对话框打开。结果两态：
// ok=true 展示 IP 与登录凭据；ok=false 红框展示 error/hint + id（机器可能
// 已建出，可刷新列表或 ecs-skill show --id 复查）。
function EcsCreateDialog({ onClose }) {
  const s = store.useRunState()
  const [defaults, setDefaults] = useState(null)
  const [loadErr, setLoadErr] = useState(null)
  const [form, setForm] = useState(null)
  const [result, setResult] = useState(null)
  const [msg, setMsg] = useState(null) // {kind:'err', text} HTTP 层失败

  useEffect(() => {
    fetch('/api/ecs/defaults')
      .then(async (r) => {
        const data = await r.json().catch(() => ({}))
        if (!r.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `HTTP ${r.status}`)
        setDefaults(data)
        setForm({
          name: '',
          flavor: data.flavor ?? '',
          image: data.image ?? '',
          diskSize: String(data.disk_size ?? ''),
          bandwidth: String(data.bandwidth ?? ''),
        })
      })
      .catch((e) => setLoadErr(e.message))
  }, [])

  const set = (k) => (e) => setForm({ ...form, [k]: e.target.value })

  const submit = async () => {
    if (!form || s.ecsCreating) return
    const body = {}
    if (form.name.trim()) body.name = form.name.trim()
    if (form.flavor.trim()) body.flavor = form.flavor.trim()
    if (form.image.trim()) body.image = form.image.trim()
    if (form.diskSize.trim()) body.disk_size = Number(form.diskSize)
    if (form.bandwidth.trim()) body.bandwidth = Number(form.bandwidth)
    setMsg(null)
    try {
      setResult(await store.createEcs(body))
    } catch (e) {
      setMsg({ kind: 'err', text: `创建失败：${e.message}` })
    }
  }

  const field = (k, label, placeholder = '') => (
    <label className="va-cfg-field">
      <span>{label}</span>
      <input
        className="va-cfg-input"
        type="text"
        autoComplete="off"
        value={form[k]}
        onChange={set(k)}
        disabled={s.ecsCreating || !!result}
        placeholder={placeholder}
      />
    </label>
  )

  return (
    <div className="va-modal-overlay" onClick={onClose}>
      <div className="va-modal" onClick={(e) => e.stopPropagation()} role="dialog" aria-label="新建 ECS">
        <div className="va-modal-title">
          <span>新建 ECS 实例</span>
          {defaults && (
            <span className={`va-cfg-badge${defaults.configured ? ' on' : ''}`}>
              {defaults.configured ? '已配置' : '未配置'}
            </span>
          )}
          <button className="va-modal-close" onClick={onClose} title="关闭">✕</button>
        </div>
        {loadErr && <div className="va-cfg-msg err">默认值读取失败：{loadErr}</div>}
        {defaults && !defaults.configured && defaults.reason && (
          <div className="va-cfg-msg err">{defaults.reason}</div>
        )}
        {s.ecsCreating && (
          <div className="va-ecs-create-hint">
            创建中，通常 1–10 分钟（轮询到 ACTIVE + 公网 IP + 22 端口通）。
            请保持对话框打开，完成后返回 IP 与登录密码；已提交的创建不可撤销。
          </div>
        )}
        {result ? (
          <>
            <div className="va-cfg-sec">
              {result.ok ? '创建成功（已就绪）' : '未就绪（机器可能已创建，已开始计费）'}
            </div>
            <div className="va-cfg-row">
              <span className="va-cfg-label">名称</span>
              <span className="va-cfg-value"><code>{result.name}</code></span>
            </div>
            <div className="va-cfg-row">
              <span className="va-cfg-label">实例 ID</span>
              <span className="va-cfg-value"><code>{result.id}</code></span>
            </div>
            {result.ip && (
              <div className="va-cfg-row">
                <span className="va-cfg-label">IP</span>
                <span className="va-cfg-value">
                  <code>{result.ip}</code>
                  <span className="va-cfg-sub">{result.ip_type === 'floating' ? '公网浮动' : '私网'}</span>
                </span>
              </div>
            )}
            <div className="va-cfg-row">
              <span className="va-cfg-label">状态</span>
              <span className="va-cfg-value">
                <code>{result.status}</code>
                <span className="va-cfg-sub">22 端口{result.ssh_port_open ? '可达' : '未通'}</span>
              </span>
            </div>
            {result.ok && result.admin_pass && <PasswordRow value={result.admin_pass} />}
            {result.ok && result.auth_method === 'key_pair' && (
              <div className="va-cfg-row">
                <span className="va-cfg-label">登录方式</span>
                <span className="va-cfg-value">密钥对（scope.ecs_create.server.key_name）</span>
              </div>
            )}
            {!result.ok && (
              <div className="va-cfg-msg err">
                {result.error}
                {result.hint ? ` ${result.hint}` : ''}
                {result.id ? '（可用列表刷新或 ecs-skill show --id 复查）' : ''}
              </div>
            )}
          </>
        ) : (
          defaults && (
            <>
              <div className="va-cfg-sec">
                参数（留空沿用 scope.yaml 默认；网络/安全组/可用区不在此改）
              </div>
              <div className="va-cfg-fields">
                {field('name', '名称', `留空自动（默认 ${defaults.name ?? 'ecs-<随机>'}）`)}
                {field('flavor', '规格', '如 kc1.xlarge.2')}
                {field('image', '镜像 ID')}
                {field('diskSize', '系统盘 GB', `默认 ${defaults.disk_size}`)}
                {field('bandwidth', '带宽 Mbit/s', `默认 ${defaults.bandwidth}`)}
              </div>
              <div className="va-cfg-row">
                <span className="va-cfg-label">region</span>
                <span className="va-cfg-value">
                  <code>{defaults.region ?? '—'}</code>
                  {defaults.has_eip && <span className="va-cfg-sub">带公网 EIP</span>}
                </span>
              </div>
            </>
          )
        )}
        {msg && <div className={`va-cfg-msg ${msg.kind}`}>{msg.text}</div>}
        <div className="va-modal-actions">
          <button className="va-cfg-cancel" onClick={onClose} disabled={s.ecsCreating}>
            {result ? '关闭' : '取消'}
          </button>
          {!result && (
            <button
              className="va-cfg-save"
              onClick={submit}
              disabled={s.ecsCreating || !form || !defaults?.configured}
              title="提交后同步等到就绪（ACTIVE + IP + 22 通），通常 1–10 分钟"
            >
              {s.ecsCreating ? '创建中…' : '开始创建'}
            </button>
          )}
        </div>
      </div>
    </div>
  )
}

export default function EcsPanel() {
  const s = store.useRunState()
  const [createOpen, setCreateOpen] = useState(false)
  return (
    <div className="va-side-panel">
      <div className="va-art-panel-head va-obs-head">
        <span>ECS 实例 · {s.ecs.instances.length}</span>
        {s.ecs.region && (
          <span className="va-obs-bucket" title="华为云 region">{s.ecs.region}</span>
        )}
        <button
          className="va-obs-refresh"
          onClick={() => setCreateOpen(true)}
          disabled={s.ecsCreating}
          title="新建一台 ECS（同步等到就绪，通常 1–10 分钟，完成后给 IP 与密码）"
        >
          + 新建
        </button>
        <button
          className="va-obs-refresh"
          onClick={() => store.checkEcs()}
          disabled={s.ecs.checking || !s.ecs.instances.length}
          title="检查全部实例：ACTIVE 且 22 端口可达 = 存活"
        >
          {s.ecs.checking ? '检查中…' : '✓ 存活检查'}
        </button>
        <button
          className="va-obs-refresh"
          onClick={() => store.refreshEcs()}
          disabled={s.ecs.loading}
          title="重拉实例清单"
        >
          {s.ecs.loading ? '加载中…' : '⟳ 刷新'}
        </button>
      </div>
      {s.ecs.error && (
        <div className="va-obs-error" title={typeof s.ecs.error === 'string' ? s.ecs.error : ''}>
          ECS 不可用：{s.ecs.error}
        </div>
      )}
      {s.ecs.instances.length === 0 && !s.ecs.error && (
        <div className="va-side-empty">{s.ecs.loading ? '清单加载中…' : '暂无 ECS 实例'}</div>
      )}
      {s.ecs.instances.map((i) => (
        <EcsRow key={i.id} inst={i} />
      ))}
      {createOpen && <EcsCreateDialog onClose={() => setCreateOpen(false)} />}
    </div>
  )
}
