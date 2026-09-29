import { useEffect, useState } from 'react'
import * as store from '../store.js'

function CreatedAt({ value }) {
  const date = typeof value === 'string' && /(?:Z|[+-]\d{2}:\d{2})$/.test(value) ? new Date(value) : null
  if (!date || Number.isNaN(date.getTime())) return <>未知</>
  return <time dateTime={value}>{new Intl.DateTimeFormat('zh-CN', {
    dateStyle: 'medium', timeStyle: 'short',
  }).format(date)}</time>
}

export default function UsersPanel() {
  const { users, userCreate } = store.useRunState()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const busy = userCreate.busy || submitting
  const blocked = busy || Boolean(userCreate.verifyUsername)
  useEffect(() => { store.refreshUsers() }, [])
  const onSubmit = async (event) => {
    event.preventDefault()
    if (blocked) return
    setSubmitting(true)
    try {
      const outcome = await store.createUser(username, password)
      if (outcome === 'committed' || outcome === 'unknown') {
        setPassword('')
        setUsername('')
      }
    } finally {
      setSubmitting(false)
    }
  }
  return (
    <section className="va-side-panel" aria-label="用户清单" aria-busy={users.loading}>
      <div className="va-art-panel-head">用户管理</div>
      <div className="va-art-tools">
        <button onClick={() => store.refreshUsers()} disabled={users.loading || busy}>
          {userCreate.verifyUsername ? '刷新清单核实' : '刷新'}
        </button>
      </div>
      {userCreate.notice && <p className={`va-users-zone va-auth-notice ${userCreate.notice.tone}`} role="status">
        {userCreate.notice.text}
      </p>}
      <details className="va-user-create">
        <summary>新增用户</summary>
        <form onSubmit={onSubmit} aria-label="新增普通用户" aria-busy={busy}>
          <p className="va-auth-help">新用户默认启用，首次登录须改密。初始密码请自行交付，提交后无法回看。新人请使用独立用户名，不要转交他人旧账号。</p>
          <label className="va-password-field">
            用户名
            <input className="va-login-input" name="username" autoComplete="off" autoCapitalize="none" spellCheck={false}
              value={username} onChange={(e) => setUsername(e.target.value)} disabled={blocked}
              required aria-describedby="create-username-rules" />
          </label>
          <p className="va-auth-help" id="create-username-rules">1–64 位英文字母、数字、下划线、短横线或点，区分大小写，不可含空格。</p>
          <label className="va-password-field">
            初始密码
            <input className="va-login-input" name="password" type="password" autoComplete="new-password"
              value={password} onChange={(e) => setPassword(e.target.value)} disabled={blocked}
              required aria-describedby="create-password-rules" />
          </label>
          <p className="va-auth-help" id="create-password-rules">8–128 位英文字母、数字或半角符号，不含空格、其他空白或中文；不要求组合。</p>
          {userCreate.error && <div className="va-login-error" role="alert">{userCreate.error}</div>}
          <button className="va-login-submit" type="submit" disabled={blocked}>{busy ? '提交中…' : '创建普通用户'}</button>
        </form>
      </details>
      <p className="va-users-zone">创建时间时区：{Intl.DateTimeFormat().resolvedOptions().timeZone}</p>
      {users.loading && <div className="va-side-empty" role="status">正在加载用户清单…</div>}
      {users.error && <div className="va-side-empty" role="alert">{users.error}</div>}
      {!users.loading && !users.error && users.items.length === 0 && <div className="va-side-empty">暂无用户</div>}
      <ul className="va-users-list">
        {users.items.map((user) => (
          <li key={user.username} className="va-users-row">
            <div className="va-users-name">{user.username}</div>
            <div>{user.enabled ? '已启用' : '已禁用'} · {user.role === 'admin' ? '管理员 · 只读' : '普通用户'}</div>
            <div>创建时间：<CreatedAt value={user.created_at} /></div>
          </li>
        ))}
      </ul>
    </section>
  )
}
