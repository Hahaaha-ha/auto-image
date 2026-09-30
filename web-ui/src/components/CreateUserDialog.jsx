import { useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import * as store from '../store.js'
import { useModal } from './modal.js'
import { useDialogLeaveProtection } from './useDialogLeaveProtection.js'

export default function CreateUserDialog({ onClose, fallbackFocusRef }) {
  const { userCreate } = store.useRunState()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const submitting = useRef(false)
  const usernameRef = useRef(null)
  const busy = userCreate.busy
  const isBusy = () => submitting.current || store.getState().userCreate.busy
  const discard = () => {
    setUsername('')
    setPassword('')
    store.clearUserCreateError()
    onClose()
  }
  const requestClose = useDialogLeaveProtection({ isBusy, dirty: Boolean(username || password), onDiscard: discard })
  const panelRef = useModal(() => !isBusy(), requestClose, usernameRef, fallbackFocusRef)
  const onSubmit = async (event) => {
    event.preventDefault()
    if (isBusy()) return
    submitting.current = true
    try {
      const outcome = await store.createUser(username, password)
      if (outcome === 'committed' || outcome === 'unknown') discard()
    } finally {
      submitting.current = false
    }
  }
  return createPortal(<div className="va-modal-overlay">
    <section ref={panelRef} className="va-modal va-create-dialog" role="dialog" aria-modal="true"
      aria-labelledby="create-user-title" tabIndex={-1}>
      <div className="va-modal-title">
        <h2 id="create-user-title">创建用户</h2>
        <button className="va-modal-close" type="button" aria-label="关闭创建用户" disabled={busy} onClick={requestClose}>✕</button>
      </div>
      <form onSubmit={onSubmit} aria-label="新增普通用户" aria-busy={busy}>
        <p className="va-auth-help">新用户默认启用，首次登录须改密。初始密码请自行交付，提交后无法回看。新人请使用独立用户名，不要转交他人旧账号。</p>
        <label className="va-password-field">用户名
          <input ref={usernameRef} className="va-login-input" name="username" autoComplete="off" autoCapitalize="none" spellCheck={false}
            value={username} onChange={(event) => setUsername(event.target.value)} disabled={busy}
            required aria-describedby="create-username-rules" />
        </label>
        <p className="va-auth-help" id="create-username-rules">1–64 位英文字母、数字、下划线、短横线或点，区分大小写，不可含空格。</p>
        <label className="va-password-field">初始密码
          <input className="va-login-input" name="password" type="password" autoComplete="new-password"
            value={password} onChange={(event) => setPassword(event.target.value)} disabled={busy}
            required aria-describedby="create-password-rules" />
        </label>
        <p className="va-auth-help" id="create-password-rules">8–128 位英文字母、数字或半角符号，不含空格、其他空白或中文；不要求组合。</p>
        {userCreate.error && <div className="va-login-error" role="alert">{userCreate.error}</div>}
        {busy && <p className="va-auth-help" role="status">正在提交，请等待结果。刷新或关闭浏览器不会撤销已提交的操作。</p>}
        <div className="va-modal-actions">
          <button type="button" disabled={busy} onClick={requestClose}>取消</button>
          <button className="va-cfg-save" type="submit" disabled={busy}>{busy ? '提交中…' : '创建普通用户'}</button>
        </div>
      </form>
    </section>
  </div>, document.body)
}
