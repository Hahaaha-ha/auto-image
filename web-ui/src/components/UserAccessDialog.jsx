import { useRef } from 'react'
import { createPortal } from 'react-dom'
import * as store from '../store.js'
import { useModal } from './modal.js'
import { useDialogLeaveProtection } from './useDialogLeaveProtection.js'

export default function UserAccessDialog({ access, fallbackFocusRef, returnFocusRef }) {
  const cancelRef = useRef(null)
  const isBusy = () => store.getState().userAccess.busy
  const requestClose = useDialogLeaveProtection({ isBusy, dirty: false, onDiscard: store.cancelUserAccess })
  const panelRef = useModal(() => !isBusy(), requestClose, cancelRef, fallbackFocusRef, returnFocusRef)
  const action = access.target.enabled ? '禁用' : '启用'
  return createPortal(<div className="va-modal-overlay">
    <section ref={panelRef} className="va-modal va-access-dialog" role="dialog" aria-modal="true"
      aria-labelledby="user-access-title" aria-describedby="user-access-identity user-access-impact" tabIndex={-1}>
      <div className="va-modal-title">
        <h2 className="va-users-name" id="user-access-title">{action}用户</h2>
        <button className="va-modal-close" type="button" aria-label={`关闭${action}确认`} disabled={access.busy} onClick={requestClose}>✕</button>
      </div>
      <form aria-label={`确认${action}用户`} aria-busy={access.busy}
        onSubmit={(event) => { event.preventDefault(); store.submitUserAccess() }}>
        <p id="user-access-identity" className="va-user-dialog-identity"><span>用户名</span><strong>{access.target.username}</strong></p>
        <p id="user-access-impact">{access.target.enabled
          ? '将撤销既有登录并停止后续访问。不停止回合、不结束会话、不撤销已提交的云操作。'
          : '仅恢复原使用者的访问资格，请勿转交新人。原会话归属保留，须重新登录；旧登录仍无效，待改密要求保留。'}</p>
        {access.error && <p className="va-login-error" role="alert">{access.error}</p>}
        {access.busy && <p className="va-auth-help" role="status">正在提交，请等待结果。刷新或关闭浏览器不会撤销已提交的操作。</p>}
        <div className="va-modal-actions">
          <button ref={cancelRef} type="button" disabled={access.busy} onClick={requestClose}>取消</button>
          <button className={access.target.enabled ? 'va-user-disable' : 'va-cfg-save'} type="submit" disabled={access.busy}>
            {access.busy ? '提交中…' : `确认${action}`}
          </button>
        </div>
      </form>
    </section>
  </div>, document.body)
}
