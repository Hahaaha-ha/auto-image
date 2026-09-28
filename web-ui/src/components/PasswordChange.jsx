import { useState } from 'react'
import * as store from '../store.js'

export default function PasswordChange() {
  const { user, passwordChange: { busy, error } } = store.useRunState()
  const [current, setCurrent] = useState('')
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')

  const onSubmit = (event) => {
    event.preventDefault()
    store.changePassword(current, password, confirm)
  }

  return (
    <div className="va-login">
      <form className="va-login-card va-password-card" onSubmit={onSubmit} aria-busy={busy}>
        <h1 className="va-login-title">设置新密码</h1>
        <p className="va-auth-help">当前用户：<strong>{user}</strong>。进入部署会话前，请先替换初始或重置密码。改密成功后需重新登录。</p>
        <label className="va-password-field">
          当前密码
          <input className="va-login-input" name="current_password" type="password" autoComplete="current-password"
            value={current} onChange={(e) => setCurrent(e.target.value)} disabled={busy} autoFocus />
        </label>
        <label className="va-password-field">
          新密码
          <input className="va-login-input" name="new_password" type="password" autoComplete="new-password"
            value={password} onChange={(e) => setPassword(e.target.value)} disabled={busy}
            required minLength={8} aria-describedby="password-rules" />
        </label>
        <p className="va-auth-help" id="password-rules">8–128 位可见 ASCII 字符（英文字母、数字或半角符号），不可含空格、其他空白或中文；不要求组合，须与当前密码不同。</p>
        <label className="va-password-field">
          确认新密码
          <input className="va-login-input" name="confirm_password" type="password" autoComplete="new-password"
            value={confirm} onChange={(e) => setConfirm(e.target.value)} disabled={busy} required />
        </label>
        {error && <div className="va-login-error" role="alert">{error}</div>}
        <button className="va-login-submit" type="submit" disabled={busy}>
          {busy ? '提交中…' : '更新密码并重新登录'}
        </button>
        <button type="button" disabled={busy} onClick={() => store.logout()}>登出</button>
      </form>
    </div>
  )
}
