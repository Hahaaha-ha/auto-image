import { useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import * as store from '../store.js'
import { useModal } from './modal.js'
import { useDialogLeaveProtection } from './useDialogLeaveProtection.js'
import { useSubmissionErrorFocus } from './useSubmissionErrorFocus.js'
import PasswordInput from './PasswordInput.jsx'

export default function ResetPasswordDialog({ reset, fallbackFocusRef, returnFocusRef }) {
  const [password, setPassword] = useState('')
  const passwordRef = useRef(null)
  const errorRef = useRef(null)
  const passwordError = reset.errorField === 'password' ? reset.error : null
  const isBusy = () => store.getState().userReset.busy
  const discard = () => {
    setPassword('')
    store.cancelUserReset()
  }
  const requestClose = useDialogLeaveProtection({ isBusy, dirty: Boolean(password), onDiscard: discard })
  const panelRef = useModal(() => !isBusy(), requestClose, passwordRef, fallbackFocusRef, returnFocusRef)
  useSubmissionErrorFocus({ busy: reset.busy, error: reset.error,
    targetRef: passwordError ? passwordRef : errorRef })
  return createPortal(<div className="va-modal-overlay">
    <section ref={panelRef} className="va-modal va-reset-dialog" role="dialog" aria-modal="true"
      aria-labelledby="reset-password-title" aria-describedby="reset-password-identity reset-password-impact" tabIndex={-1}>
      <div className="va-modal-title">
        <h2 className="va-users-name" id="reset-password-title">重置密码</h2>
        <button className="va-modal-close" type="button" aria-label="关闭密码重置" disabled={reset.busy} onClick={requestClose}>✕</button>
      </div>
      <form aria-label="重置普通用户密码" aria-busy={reset.busy}
        onSubmit={(event) => { event.preventDefault(); store.resetUserPassword(password) }}>
        <p id="reset-password-identity" className="va-user-dialog-identity"><span>用户名</span><strong>{reset.target.username}</strong></p>
        <p id="reset-password-impact">将撤销全部既有登录，下次登录须再次改密。不停止执行中的回合，不改变会话归属。</p>
        {!reset.target.enabled && <p>该用户仍保持禁用，不能登录。</p>}
        <PasswordInput label="新密码" inputRef={passwordRef} id="reset-password" name="reset_password" autoComplete="new-password"
          value={password} onChange={(event) => { setPassword(event.target.value); store.clearUserResetError('password') }} disabled={reset.busy}
          required aria-invalid={Boolean(passwordError)}
          aria-describedby={`reset-password-rules reset-password-delivery${passwordError ? ' reset-password-error' : ''}`} />
        {passwordError && <p className="va-field-error va-login-error" id="reset-password-error" role="alert">{passwordError}</p>}
        <p className="va-auth-help" id="reset-password-rules">8–128 位英文字母、数字或半角符号，不含空格、其他空白或中文；不要求组合。</p>
        <p id="reset-password-delivery">请自行交付新密码，提交后无法回看。</p>
        {reset.error && !reset.errorField && <p ref={errorRef} className="va-login-error" role="alert" tabIndex={-1}>{reset.error}</p>}
        {reset.busy && <p className="va-auth-help" role="status">正在提交，请等待结果。刷新或关闭浏览器不会撤销已提交的操作。</p>}
        <div className="va-modal-actions">
          <button type="button" disabled={reset.busy} onClick={requestClose}>取消</button>
          <button className="va-cfg-save" type="submit" disabled={reset.busy}>{reset.busy ? '提交中…' : '确认重置密码'}</button>
        </div>
      </form>
    </section>
  </div>, document.body)
}
