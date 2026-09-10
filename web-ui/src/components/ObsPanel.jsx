// OBS 产物面板：桶内对象全量清单（/api/obs/objects），树形与本地产物面板
// 同构（obsGroups 派生分组 → artifactTree 复用），行点击开 OBS 对象标签页
// （文本预览 / 二进制占位）。下载不直下：⧉ 复制签名下载链接（7 天有效，
// 剪贴板带 execCommand 兜底），成功后行内 ✓ 反馈。无勾选与 zip——OBS 侧
// 只有在线列举与单对象访问；清单不随流水线事件联动（上传不经过本服务
// 事件面），刷新钮手动重拉 + 归档动作完成后自动刷新。头部 ⚙ 配置钮开
// 配置对话框（当前配置脱敏视图 + 在线改配：ak/sk 服务端密文落盘）。
import { useEffect, useState } from 'react'
import * as store from '../store.js'
import { fmtSize, obsGroups, artifactTree, defaultOpenPaths } from '../derive.js'

// 行级复制链接按钮：复制成功后 ✓ 反馈 1.5 秒（本地态，不打断浏览）
function CopyLink({ objKey }) {
  const [copied, setCopied] = useState(false)
  const onClick = async (e) => {
    e.stopPropagation()
    if (await store.copyObsLink(objKey)) {
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    }
  }
  return (
    <button
      className="va-art-dl"
      title="复制下载链接（签名，7 天有效；桶私有，匿名不可访问）"
      onClick={onClick}
    >
      {copied ? '✓' : '⧉'}
    </button>
  )
}

// 目录树节点：与 ArtDir 同构，去勾选（无 zip）；文件条目带 key 全名
// （根级对象的 key 不能由「（桶根）」前缀拼回），title 附最后修改时间。
function ObsDir({ node, toggles, setToggles, defaultOpen, openKeys, activeKey }) {
  const open = toggles[node.path] ?? defaultOpen.has(node.path)
  const toggleOpen = () => setToggles({ ...toggles, [node.path]: !open })
  return (
    <div className="va-art-group">
      <div
        className="va-art-dir-head"
        role="button"
        tabIndex={0}
        onClick={toggleOpen}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault()
            toggleOpen()
          }
        }}
        title={node.path}
      >
        <span className="va-art-dir-arrow">{open ? '▾' : '▸'}</span>
        <span className="va-art-name">{node.name}</span>
        <span className="va-art-count" title="本目录（含子目录）对象数">{node.count}</span>
      </div>
      {open && (
        <div className="va-art-children">
          {node.files.map((f) => {
            const key = f.key ?? `${node.path}/${f.name}`
            return (
              <div
                key={key}
                role="button"
                tabIndex={0}
                className={`va-art-item${activeKey === key ? ' on' : ''}`}
                onClick={() => store.openObsObject(key, f)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault()
                    store.openObsObject(key, f)
                  }
                }}
                title={`${key}${f.last_modified ? ` · ${f.last_modified}` : ''}`}
              >
                <span className="va-art-name">{f.name}</span>
                {openKeys.has(key) && <span className="va-art-opened" title="已打开为标签页" />}
                <span className="va-art-size">{fmtSize(f.size)}</span>
                <CopyLink objKey={key} />
              </div>
            )
          })}
          {node.dirs.map((d) => (
            <ObsDir
              key={d.path}
              node={d}
              toggles={toggles}
              setToggles={setToggles}
              defaultOpen={defaultOpen}
              openKeys={openKeys}
              activeKey={activeKey}
            />
          ))}
        </div>
      )}
    </div>
  )
}

// 配置对话框：打开即拉当前配置（GET /api/obs/config 脱敏视图——ak/sk
// 只显头尾几位 + 来源与长度，明文永不出服务）；提交走 POST /api/obs/
// config：ak/sk 服务端加密（enc:v1）落 scope.yaml 的 obs 段、明文键自动
// 删除，改 region 时 endpoint/domain 服务端重推；保存后尽力健康检查，
// 结果行内反馈（失败不回滚，由使用者决断），成功即刷新对象清单。
function ObsConfigDialog({ onClose }) {
  const [cfg, setCfg] = useState(null)
  const [loadErr, setLoadErr] = useState(null)
  const [form, setForm] = useState({ ak: '', sk: '', bucket: '', region: '', endpoint: '' })
  const [saving, setSaving] = useState(false)
  const [msg, setMsg] = useState(null) // {kind: 'ok'|'warn'|'err', text}

  useEffect(() => {
    fetch('/api/obs/config')
      .then(async (r) => {
        const data = await r.json().catch(() => ({}))
        if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`)
        setCfg(data)
      })
      .catch((e) => setLoadErr(e.message))
  }, [])

  const set = (k) => (e) => setForm({ ...form, [k]: e.target.value })

  const save = async () => {
    const body = Object.fromEntries(
      Object.entries(form).filter(([, v]) => v.trim()),
    )
    if (!Object.keys(body).length) {
      setMsg({ kind: 'err', text: '没有要保存的字段（至少填一项）' })
      return
    }
    setSaving(true)
    setMsg(null)
    try {
      const resp = await fetch('/api/obs/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      const data = await resp.json().catch(() => ({}))
      if (!resp.ok) {
        setMsg({ kind: 'err', text: `保存失败：${data.detail || `HTTP ${resp.status}`}` })
        return
      }
      setCfg(data)
      setForm({ ak: '', sk: '', bucket: '', region: '', endpoint: '' })
      const ck = data.check?.ok
        ? `健康检查通过（${data.check.latency_ms ?? '?'}ms）`
        : `健康检查未通过：${
            typeof data.check?.error === 'string'
              ? data.check.error
              : JSON.stringify(data.check?.error ?? data.check)
          }`
      setMsg({ kind: data.check?.ok ? 'ok' : 'warn', text: `已保存（ak/sk 密文落盘、明文已删）。${ck}` })
      store.refreshObs()
    } catch (e) {
      setMsg({ kind: 'err', text: `保存失败：${e.message}` })
    } finally {
      setSaving(false)
    }
  }

  const cred = (label, c) => (
    <div className="va-cfg-row">
      <span className="va-cfg-label">{label}</span>
      <span className="va-cfg-value">
        <code>{c?.masked ?? '—'}</code>
        {c?.length != null && <span className="va-cfg-sub">{c.length} 位</span>}
        {c?.source && <span className="va-cfg-src">{c.source}</span>}
      </span>
    </div>
  )

  const field = (k, label, type = 'text') => (
    <label className="va-cfg-field">
      <span>{label}</span>
      <input
        className="va-cfg-input"
        type={type}
        autoComplete="off"
        value={form[k]}
        onChange={set(k)}
        placeholder={k === 'ak' || k === 'sk' ? '留空则不修改' : ''}
      />
    </label>
  )

  return (
    <div className="va-modal-overlay" onClick={onClose}>
      <div className="va-modal" onClick={(e) => e.stopPropagation()} role="dialog" aria-label="OBS 配置">
        <div className="va-modal-title">
          <span>OBS 配置</span>
          {cfg && (
            <span className={`va-cfg-badge${cfg.configured ? ' on' : ''}`}>
              {cfg.configured ? '已配置' : '未配置'}
            </span>
          )}
          <button className="va-modal-close" onClick={onClose} title="关闭">✕</button>
        </div>
        {loadErr && <div className="va-cfg-msg err">配置读取失败：{loadErr}</div>}
        {cfg && (
          <>
            <div className="va-cfg-sec">当前配置（ak/sk 脱敏显示，明文不出服务）</div>
            <div className="va-cfg-row">
              <span className="va-cfg-label">bucket</span>
              <span className="va-cfg-value"><code>{cfg.bucket ?? '—'}</code></span>
            </div>
            <div className="va-cfg-row">
              <span className="va-cfg-label">region</span>
              <span className="va-cfg-value"><code>{cfg.region ?? '—'}</code></span>
            </div>
            <div className="va-cfg-row">
              <span className="va-cfg-label">endpoint</span>
              <span className="va-cfg-value"><code>{cfg.endpoint ?? '—'}</code></span>
            </div>
            <div className="va-cfg-row">
              <span className="va-cfg-label">domain</span>
              <span className="va-cfg-value"><code>{cfg.domain ?? '—'}</code></span>
            </div>
            {cred('AK', cfg.ak)}
            {cred('SK', cfg.sk)}
            <div className="va-cfg-row">
              <span className="va-cfg-label">加密密钥</span>
              <span className="va-cfg-value">
                <span className="va-cfg-src">{cfg.enc_key?.source}</span>
                <span className="va-cfg-sub">{cfg.enc_key?.file}</span>
              </span>
            </div>
            <div className="va-cfg-sec">修改（ak/sk 提交后加密落盘，明文自动删除；留空不改）</div>
            <div className="va-cfg-fields">
              {field('ak', 'AK', 'password')}
              {field('sk', 'SK', 'password')}
              {field('bucket', 'bucket')}
              {field('region', 'region')}
              {field('endpoint', 'endpoint')}
            </div>
          </>
        )}
        {msg && <div className={`va-cfg-msg ${msg.kind}`}>{msg.text}</div>}
        <div className="va-modal-actions">
          <button className="va-cfg-cancel" onClick={onClose} disabled={saving}>关闭</button>
          <button
            className="va-cfg-save"
            onClick={save}
            disabled={saving || !cfg}
            title="保存（ak/sk 密文落盘）后自动做一次健康检查"
          >
            {saving ? '保存中…' : '保存'}
          </button>
        </div>
      </div>
    </div>
  )
}


export default function ObsPanel({ openKeys, activeKey }) {
  const s = store.useRunState()
  const groups = obsGroups(s.obs.objects)
  const roots = artifactTree(groups)
  const [toggles, setToggles] = useState({})
  const [cfgOpen, setCfgOpen] = useState(false)
  return (
    <div className="va-side-panel">
      <div className="va-art-panel-head va-obs-head">
        <span>OBS 产物 · {s.obs.objects.length}</span>
        {s.obs.bucket && (
          <span className="va-obs-bucket" title={s.obs.domain ?? s.obs.bucket}>
            ☁ {s.obs.bucket}
          </span>
        )}
        <button
          className="va-obs-refresh"
          onClick={() => setCfgOpen(true)}
          title="查看/配置 OBS 凭据与桶（ak/sk 加密落盘，明文自动删除）"
        >
          ⚙ 配置
        </button>
        <button
          className="va-obs-refresh"
          onClick={() => store.refreshObs()}
          disabled={s.obs.loading}
          title="重拉桶内对象清单"
        >
          {s.obs.loading ? '加载中…' : '⟳ 刷新'}
        </button>
      </div>
      {s.obs.error && (
        <div className="va-obs-error" title={typeof s.obs.error === 'string' ? s.obs.error : ''}>
          OBS 不可用：{s.obs.error}
        </div>
      )}
      {roots.length === 0 && !s.obs.error && (
        <div className="va-side-empty">{s.obs.loading ? '清单加载中…' : '桶内暂无对象'}</div>
      )}
      {roots.map((r) => (
        <ObsDir
          key={r.path}
          node={r}
          toggles={toggles}
          setToggles={setToggles}
          defaultOpen={defaultOpenPaths(groups)}
          openKeys={openKeys}
          activeKey={activeKey}
        />
      ))}
      {cfgOpen && <ObsConfigDialog onClose={() => setCfgOpen(false)} />}
    </div>
  )
}
