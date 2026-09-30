import { useEffect, useRef, useState } from 'react'
import * as store from '../store.js'
import CreateUserDialog from './CreateUserDialog.jsx'

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
  const resetFeedbackRef = useRef(null)
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
      {userAccess.notice && <p ref={feedbackRef} className={`va-users-zone va-auth-notice ${userAccess.notice.tone}`} role="status">
        {userAccess.notice.text}
      </p>}
      {userAccess.error && !userAccess.target && <p ref={feedbackRef} className="va-users-zone va-login-error" role="alert">{userAccess.error}</p>}
      {userReset.notice && <p ref={resetFeedbackRef} className={`va-users-zone va-auth-notice ${userReset.notice.tone}`} role="status">
        {userReset.notice.text}
      </p>}
      {userReset.error && !userReset.target && <p ref={resetFeedbackRef} className="va-users-zone va-login-error" role="alert">{userReset.error}</p>}
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
                  onClick={() => store.beginUserAccess(user.username)}>{user.enabled ? '禁用' : '启用'}</button>
                <button aria-label={`${userReset.unknown ? '发起新的重置' : '重置密码'} ${user.username}`}
                  disabled={Boolean(userAccess.target || userReset.target || userReset.verifyUsername) || users.loading || Boolean(users.error) || !user.user_version}
                  onClick={() => store.beginUserReset(user.username)}>{userReset.unknown ? '发起新的重置' : '重置密码'}</button>
              </div>
              {userAccess.target?.username === user.username && <AccessConfirmation access={userAccess} />}
              {userReset.target?.username === user.username && <ResetConfirmation reset={userReset} />}
            </>}
          </li>
        ))}
      </ul>
    </section>
  )
}

function ResetConfirmation({ reset }) {
  const [password, setPassword] = useState('')
  const cancelRef = useRef(null)
  useEffect(() => { cancelRef.current?.focus() }, [])
  return <form className="va-user-access-confirm va-user-reset" aria-label={`确认重置「${reset.target.username}」的密码`}
    aria-busy={reset.busy} onSubmit={(event) => { event.preventDefault(); store.resetUserPassword(password) }}>
    <p className="va-users-name">确认重置「{reset.target.username}」的密码</p>
    <p>将撤销全部既有登录，下次登录须再次改密。不停止执行中的回合，不改变会话归属。</p>
    {!reset.target.enabled && <p>该用户仍保持禁用，不能登录。</p>}
    <label className="va-password-field">
      新密码
      <input className="va-login-input" name="reset_password" type="password" autoComplete="new-password"
        value={password} onChange={(event) => setPassword(event.target.value)} disabled={reset.busy}
        required aria-describedby="reset-password-rules reset-password-delivery" />
    </label>
    <p className="va-auth-help" id="reset-password-rules">8–128 位英文字母、数字或半角符号，不含空格、其他空白或中文；不要求组合。</p>
    <p id="reset-password-delivery">请自行交付新密码，提交后无法回看。</p>
    {reset.error && <p className="va-login-error" role="alert">{reset.error}</p>}
    <div className="va-user-access-actions">
      <button ref={cancelRef} type="button" disabled={reset.busy} onClick={() => store.cancelUserReset()}>取消</button>
      <button type="submit" disabled={reset.busy}>{reset.busy ? '提交中…' : '确认重置密码'}</button>
    </div>
  </form>
}

function AccessConfirmation({ access }) {
  const cancelRef = useRef(null)
  useEffect(() => { cancelRef.current?.focus() }, [])
  const action = access.target.enabled ? '禁用' : '启用'
  return <form className="va-user-access-confirm" aria-label={`确认${action}「${access.target.username}」`}
    aria-busy={access.busy} onSubmit={(event) => { event.preventDefault(); store.submitUserAccess() }}>
    <p className="va-users-name">确认{action}「{access.target.username}」</p>
    <p>{access.target.enabled
      ? '将撤销既有登录并停止后续访问。不停止回合、不结束会话、不撤销已提交的云操作。'
      : '仅恢复原使用者的访问资格，请勿转交新人。原会话归属保留，须重新登录；旧登录仍无效，待改密要求保留。'}</p>
    {access.error && <p className="va-login-error" role="alert">{access.error}</p>}
    <div className="va-user-access-actions">
      <button ref={cancelRef} type="button" disabled={access.busy} onClick={() => store.cancelUserAccess()}>取消</button>
      <button className={access.target.enabled ? 'va-user-disable' : ''} type="submit" disabled={access.busy}>
        {access.busy ? '提交中…' : `确认${action}`}
      </button>
    </div>
  </form>
}
