import { useEffect, useRef, useState } from 'react'
import * as store from '../store.js'
import CreateUserDialog from './CreateUserDialog.jsx'
import UserAccessDialog from './UserAccessDialog.jsx'
import ResetPasswordDialog from './ResetPasswordDialog.jsx'

function CreatedAt({ value }) {
  const date = typeof value === 'string' && /(?:Z|[+-]\d{2}:\d{2})$/.test(value) ? new Date(value) : null
  if (!date || Number.isNaN(date.getTime())) return <>未知</>
  return <time dateTime={value}>{new Intl.DateTimeFormat('zh-CN', {
    dateStyle: 'medium', timeStyle: 'short',
  }).format(date)}</time>
}

export default function UsersPanel() {
  const { users, userCreate, userAccess, userReset } = store.useRunState()
  const [creating, setCreating] = useState(false)
  const createFeedbackRef = useRef(null)
  const feedbackRef = useRef(null)
  const accessTriggerRef = useRef(null)
  const resetFeedbackRef = useRef(null)
  const resetTriggerRef = useRef(null)
  const busy = userCreate.busy
  useEffect(() => { store.refreshUsers() }, [])
  useEffect(() => {
    if (userAccess.notice || (userAccess.error && !userAccess.target)) {
      feedbackRef.current?.scrollIntoView({ block: 'nearest' })
    }
  }, [userAccess.notice, userAccess.error, userAccess.target])
  useEffect(() => {
    if (userReset.notice || (userReset.error && !userReset.target)) {
      resetFeedbackRef.current?.scrollIntoView({ block: 'nearest' })
    }
  }, [userReset.notice, userReset.error, userReset.target])
  return (
    <section className="va-side-panel" aria-label="用户清单" aria-busy={users.loading}>
      <div className="va-art-panel-head">用户管理</div>
      <div className="va-art-tools">
        <button disabled={busy || Boolean(userCreate.verifyUsername || userAccess.target || userReset.target)}
          onClick={() => { store.clearUserCreateError(); setCreating(true) }}>创建用户</button>
        <button onClick={() => store.refreshUsers()} disabled={users.loading || busy || userAccess.busy || userReset.busy}>
          {userCreate.verifyUsername || userAccess.verifyUsername || userReset.verifyUsername ? '刷新清单核实' : '刷新'}
        </button>
      </div>
      {userCreate.notice && <p ref={createFeedbackRef} tabIndex={-1} className={`va-users-zone va-auth-notice ${userCreate.notice.tone}`} role="status">
        {userCreate.notice.text}
      </p>}
      {creating && <CreateUserDialog fallbackFocusRef={createFeedbackRef} onClose={() => setCreating(false)} />}
      <div ref={feedbackRef} tabIndex={-1}>
        {userAccess.notice && <p className={`va-users-zone va-auth-notice ${userAccess.notice.tone}`} role="status">
          {userAccess.notice.text}
        </p>}
        {userAccess.error && !userAccess.target && <p className="va-users-zone va-login-error" role="alert">{userAccess.error}</p>}
      </div>
      {userAccess.target && <UserAccessDialog access={userAccess} fallbackFocusRef={feedbackRef} returnFocusRef={accessTriggerRef} />}
      <div ref={resetFeedbackRef} tabIndex={-1}>
        {userReset.notice && <p className={`va-users-zone va-auth-notice ${userReset.notice.tone}`} role="status">
          {userReset.notice.text}
        </p>}
        {userReset.error && !userReset.target && <p className="va-users-zone va-login-error" role="alert">{userReset.error}</p>}
      </div>
      {userReset.target && <ResetPasswordDialog reset={userReset} fallbackFocusRef={resetFeedbackRef} returnFocusRef={resetTriggerRef} />}
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
            {user.role === 'user' && <>
              <div className="va-user-access-actions va-user-access-button">
                <button aria-label={`${user.enabled ? '禁用' : '启用'}用户 ${user.username}`}
                  disabled={Boolean(userAccess.target || userReset.target || userAccess.verifyUsername) || users.loading || Boolean(users.error) || !user.user_version}
                  onClick={(event) => { accessTriggerRef.current = event.currentTarget; store.beginUserAccess(user.username) }}>{user.enabled ? '禁用' : '启用'}</button>
                <button aria-label={`${userReset.unknown ? '发起新的重置' : '重置密码'} ${user.username}`}
                  disabled={Boolean(userAccess.target || userReset.target || userReset.verifyUsername) || users.loading || Boolean(users.error) || !user.user_version}
                  onClick={(event) => { resetTriggerRef.current = event.currentTarget; store.beginUserReset(user.username) }}>{userReset.unknown ? '发起新的重置' : '重置密码'}</button>
              </div>
            </>}
          </li>
        ))}
      </ul>
    </section>
  )
}
