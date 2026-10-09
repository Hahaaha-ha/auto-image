import { useEffect, useMemo, useRef, useState } from 'react'
import * as store from '../store.js'
import CreateUserDialog from './CreateUserDialog.jsx'
import UserAccessDialog from './UserAccessDialog.jsx'
import ResetPasswordDialog from './ResetPasswordDialog.jsx'
import UsersPagination from './UsersPagination.jsx'
import { getUserListView } from '../userList.js'

function CreatedAt({ value }) {
  const date = typeof value === 'string' && /(?:Z|[+-]\d{2}:\d{2})$/.test(value) ? new Date(value) : null
  if (!date || Number.isNaN(date.getTime())) return <>未知</>
  return <time dateTime={value}>{new Intl.DateTimeFormat('zh-CN', {
    dateStyle: 'medium', timeStyle: 'short',
  }).format(date)}</time>
}

export default function UsersPanel() {
  const { users, usersQuery, userCreate, userAccess, userReset } = store.useRunState()
  const view = useMemo(() => getUserListView(users.items, usersQuery), [users.items, usersQuery])
  const createdRowRef = useRef(null)
  const listRef = useRef(null)
  const headingRef = useRef(null)
  const dismissNotice = key => {
    store.dismissUserNotice(key)
    headingRef.current?.focus({ preventScroll: true })
  }
  const [creating, setCreating] = useState(false)
  const createFeedbackRef = useRef(null)
  const feedbackRef = useRef(null)
  const accessTriggerRef = useRef(null)
  const resetFeedbackRef = useRef(null)
  const resetTriggerRef = useRef(null)
  const focusFirstRow = () => requestAnimationFrame(() => {
    const row = listRef.current?.querySelector('tbody tr')
    row?.focus({ preventScroll: true })
    row?.scrollIntoView({ block: 'start' })
  })
  const changePage = page => {
    store.setUsersPage(page)
    focusFirstRow()
  }
  const busy = userCreate.busy
  const needsVerification = Boolean(userCreate.verifyUsername || userAccess.verifyUsername || userReset.verifyUsername)
  const refreshLabel = users.loading ? '刷新中…' : needsVerification ? '刷新清单核实' : '刷新'
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
    <section className="va-users-panel" aria-label="用户清单" aria-busy={users.loading}>
      <div className="va-users-heading">
        <h1 ref={headingRef} tabIndex={-1}>用户管理</h1>
        <button className="va-users-create" disabled={users.loading || busy || Boolean(userCreate.verifyUsername || userAccess.target || userReset.target)}
          onClick={() => { store.clearUserCreateError(); setCreating(true) }}>创建用户</button>
      </div>
      <div className="va-users-workarea">
        <div className="va-users-toolbar">
          <div className="va-users-search-actions">
            <label className="va-users-search">
              <span className="va-users-sr-only">搜索</span>
              <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.25" strokeLinecap="round" aria-hidden="true" focusable="false"><circle cx="7" cy="7" r="4.75" /><path d="m10.5 10.5 3.25 3.25" /></svg>
              <input type="search" value={usersQuery.keyword} placeholder="输入用户名"
                onChange={event => store.setUsersKeyword(event.target.value)} />
            </label>
            <button className="va-users-refresh" aria-label={refreshLabel} title={refreshLabel}
              onClick={() => store.refreshUsers()} disabled={users.loading || busy || userAccess.busy || userReset.busy}>
              <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.25" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false"><path d="M13.25 6A5.5 5.5 0 1 0 13 11M13.25 2.75V6H10" /></svg>
              <span className={needsVerification ? undefined : 'va-users-sr-only'}>{refreshLabel}</span>
            </button>
          </div>
        </div>
        <div className="va-users-feedback">
          {userCreate.notice && <p ref={createFeedbackRef} tabIndex={-1} className={`va-users-zone va-auth-notice ${userCreate.notice.tone}`} role="status">
            {userCreate.notice.text}
            {userCreate.notice.tone === 'success' && <button className="va-notice-dismiss" aria-label="关闭创建成功提示" onClick={() => dismissNotice('userCreate')}>✕</button>}
            {userCreate.createdUsername && <button className="va-users-locate"
              disabled={users.loading || Boolean(users.error) || !users.items.some(user => user.username === userCreate.createdUsername)}
              onClick={() => {
                if (store.showCreatedUser()) requestAnimationFrame(() => {
                  createdRowRef.current?.focus()
                  createdRowRef.current?.scrollIntoView({ block: 'nearest' })
                })
              }}>查看该用户</button>}
          </p>}
          {creating && <CreateUserDialog fallbackFocusRef={createFeedbackRef} onClose={() => setCreating(false)} />}
          <div ref={feedbackRef} tabIndex={-1}>
            {userAccess.notice && <p className={`va-users-zone va-auth-notice ${userAccess.notice.tone}`} role="status">
              {userAccess.notice.text}
              {userAccess.notice.tone === 'success' && <button className="va-notice-dismiss" aria-label="关闭状态变更成功提示" onClick={() => dismissNotice('userAccess')}>✕</button>}
            </p>}
            {userAccess.error && !userAccess.target && <p className="va-users-zone va-login-error" role="alert">{userAccess.error}</p>}
          </div>
          {userAccess.target && <UserAccessDialog access={userAccess} fallbackFocusRef={feedbackRef} returnFocusRef={accessTriggerRef} />}
          <div ref={resetFeedbackRef} tabIndex={-1}>
            {userReset.notice && <p className={`va-users-zone va-auth-notice ${userReset.notice.tone}`} role="status">
              {userReset.notice.text}
              {userReset.notice.tone === 'success' && <button className="va-notice-dismiss" aria-label="关闭密码重置成功提示" onClick={() => dismissNotice('userReset')}>✕</button>}
            </p>}
            {userReset.error && !userReset.target && <p className="va-users-zone va-login-error" role="alert">{userReset.error}</p>}
          </div>
          {userReset.target && <ResetPasswordDialog reset={userReset} fallbackFocusRef={resetFeedbackRef} returnFocusRef={resetTriggerRef} />}
        </div>
        <div className="va-users-loading" role="status">
          {users.loading && (users.items.length ? '正在更新用户清单…' : '正在加载用户清单…')}
        </div>
        {users.error && <div className="va-users-empty" role="alert">
          <p>{users.error}</p>
          <button onClick={() => store.refreshUsers()} disabled={users.loading}>重试加载</button>
        </div>}
        {!users.loading && !users.error && view.total === 0 && <div className="va-users-empty">
          <p>{users.items.length === 0 ? '暂无用户' : '没有匹配的用户，请修改搜索关键词'}</p>
          {usersQuery.keyword && <button onClick={() => store.setUsersKeyword('')}>清空搜索</button>}
        </div>}
        {!users.error && view.items.length > 0 && <>
          <p className="va-users-table-hint">左右滚动查看完整表格</p>
          <div className="va-users-table-scroll" role="region" aria-label="用户信息表格" tabIndex={0}>
            <table ref={listRef} className="va-users-list" role="table" aria-label="用户信息">
              <thead><tr role="row"><th scope="col">用户名</th><th scope="col">角色</th><th scope="col">状态</th><th scope="col">创建时间</th><th scope="col">操作</th></tr></thead>
              <tbody>
                {view.items.map((user) => (
                  <tr key={user.username} className="va-users-row" role="row" tabIndex={-1}
                    ref={user.username === userCreate.createdUsername ? createdRowRef : null}>
                    <th scope="row" role="rowheader" className="va-users-name"><span className="va-users-username" title={user.username}>{user.username}</span></th>
                    <td role="cell" className="va-users-role">{user.role === 'admin' ? '管理员' : '普通用户'}</td>
                    <td role="cell" className="va-users-status-cell"><span className={`va-users-status ${user.enabled ? 'enabled' : 'disabled'}`}>{user.enabled ? '已启用' : '已禁用'}</span></td>
                    <td role="cell" className="va-users-created"><CreatedAt value={user.created_at} /></td>
                    <td role="cell">
                      {user.role === 'user' ?
                        <div className="va-user-access-actions va-user-access-button">
                          <button aria-label={`${userReset.unknown ? '发起新的重置' : '重置密码'} ${user.username}`}
                            disabled={Boolean(userAccess.target || userReset.target || userReset.verifyUsername) || users.loading || Boolean(users.error) || !user.user_version}
                            onClick={(event) => { resetTriggerRef.current = event.currentTarget; store.beginUserReset(user.username) }}>{userReset.unknown ? '发起新的重置' : '重置密码'}</button>
                          <button aria-label={`${user.enabled ? '禁用' : '启用'}用户 ${user.username}`}
                            disabled={Boolean(userAccess.target || userReset.target || userAccess.verifyUsername) || users.loading || Boolean(users.error) || !user.user_version}
                            onClick={(event) => { accessTriggerRef.current = event.currentTarget; store.beginUserAccess(user.username) }}>{user.enabled ? '禁用' : '启用'}</button>
                        </div>
                        : <span className="va-users-no-actions" aria-label="无可用操作">—</span>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>}
        {!users.error && view.total > 0 && <div className="va-users-footer" aria-busy={users.loading}>
          <div className="va-users-results">
            <p role="status">共 {view.total} 名用户<span className="va-users-range">显示 {view.start + 1}–{view.start + view.items.length} 名</span></p>
          </div>
          <UsersPagination page={view.page} pageCount={view.pageCount}
            pageSize={usersQuery.pageSize} loading={users.loading}
            onPageChange={changePage} onPageSizeChange={store.setUsersPageSize} />
        </div>}
      </div>
    </section>
  )
}
