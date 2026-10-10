import { useEffect, useState } from 'react'
import './App.css'
import * as store from './store.js'
import Workbench from './Workbench.jsx'
import PasswordChange from './components/PasswordChange.jsx'
import ManagementPage from './components/ManagementPage.jsx'
import { usePage } from './navigation.js'

// 登录壳：未认证时的唯一界面（数据面不启动——不建流、不拉清单）
function LoginShell() {
  const { authNotice } = store.useRunState()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)

  const onSubmit = async (e) => {
    e.preventDefault()
    if (busy) return
    setBusy(true)
    setError(null)
    try {
      await store.login(username, password)
    } catch (err) {
      setError(err.status === 401 ? '用户名或密码错误，或账号已禁用' : `登录失败：${err.message}`)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="va-login">
      <form className="va-login-card" onSubmit={onSubmit}>
        <div className="va-login-title">auto-image 部署会话</div>
        {authNotice && <div className={`va-auth-notice ${authNotice.tone}`} role="status">{authNotice.text}</div>}
        <input
          className="va-login-input"
          placeholder="用户名"
          aria-label="用户名"
          value={username}
          autoComplete="username"
          onChange={(e) => setUsername(e.target.value)}
          autoFocus
        />
        <input
          className="va-login-input"
          type="password"
          placeholder="密码"
          aria-label="密码"
          value={password}
          autoComplete="current-password"
          onChange={(e) => setPassword(e.target.value)}
        />
        <button className="va-login-submit" type="submit" disabled={busy || !username}>
          {busy ? '登录中…' : '登录'}
        </button>
        {error && <div className="va-login-error" role="alert">{error}</div>}
      </form>
    </div>
  )
}

function AuthenticatedApp() {
  const page = usePage()
  const [managementVisited, setManagementVisited] = useState(page === 'users')
  useEffect(() => { if (page === 'users') setManagementVisited(true) }, [page])
  return <div className="va-root">
    <Workbench visible={page === 'workbench'} />
    {(managementVisited || page === 'users') && <ManagementPage visible={page === 'users'} />}
  </div>
}

export default function App() {
  const s = store.useRunState()
  if (s.auth === 'user') return <AuthenticatedApp key={s.user} />
  return (
    <div className="va-root">
      {s.auth === 'anonymous' ? <LoginShell /> : s.auth === 'password-change' ? <PasswordChange /> : (
        <div className="va-login">
          <div className="va-login-card va-login-waiting">正在确认登录状态…</div>
        </div>
      )}
    </div>
  )
}
