import { useEffect, useRef } from 'react'
import * as store from '../store.js'
import { WORKBENCH_HREF } from '../navigation.js'
import AccountMenu from './AccountMenu.jsx'
import UsersPanel from './UsersPanel.jsx'

export default function ManagementPage({ visible }) {
  const { canManageUsers } = store.useRunState()
  const headingRef = useRef(null)
  useEffect(() => { if (visible) headingRef.current?.focus() }, [visible])
  return <section className="va-management" hidden={!visible} aria-label="用户管理页面">
    <header className="va-head va-management-head">
      <a className="va-back" href={WORKBENCH_HREF}>返回工作台</a>
      <h1 ref={headingRef} tabIndex={-1}>用户管理</h1>
      <span className="va-spacer" />
      <AccountMenu visible={visible} />
    </header>
    <main className="va-management-body">
      {canManageUsers === true ? <UsersPanel /> : <div className="va-access-denied" role="status">
        <h2>无管理权限</h2>
        <p>当前账号无法管理用户。请返回工作台继续处理自己的会话。</p>
        <a href={WORKBENCH_HREF}>返回工作台</a>
      </div>}
    </main>
  </section>
}
