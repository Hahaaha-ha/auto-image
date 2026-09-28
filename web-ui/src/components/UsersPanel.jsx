import { useEffect } from 'react'
import * as store from '../store.js'

function CreatedAt({ value }) {
  const date = typeof value === 'string' && /(?:Z|[+-]\d{2}:\d{2})$/.test(value) ? new Date(value) : null
  if (!date || Number.isNaN(date.getTime())) return <>未知</>
  return <time dateTime={value}>{new Intl.DateTimeFormat('zh-CN', {
    dateStyle: 'medium', timeStyle: 'short',
  }).format(date)}</time>
}

export default function UsersPanel() {
  const { users } = store.useRunState()
  useEffect(() => { store.refreshUsers() }, [])
  return (
    <section className="va-side-panel" aria-label="用户清单" aria-busy={users.loading}>
      <div className="va-art-panel-head">用户管理 · 只读清单</div>
      <div className="va-art-tools">
        <button onClick={() => store.refreshUsers()} disabled={users.loading}>刷新</button>
      </div>
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
