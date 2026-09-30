import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'

const { state } = vi.hoisted(() => ({ state: {
  sidePanel: 'users', tabs: [], canManageUsers: true,
  usersQuery: { keyword: '', page: 1, pageSize: 20 },
  users: { items: [
    { username: 'operator', role: 'admin', enabled: true, created_at: '2026-09-01T02:00:00Z' },
    { username: 'second', role: 'admin', enabled: true, created_at: null },
    { username: 'legacy', role: 'user', enabled: false, created_at: null, user_version: 'v1' },
  ], loading: false, error: null },
  userCreate: { busy: false, error: null, notice: null, verifyUsername: null },
  userReset: { target: null, busy: false, error: null, notice: null, verifyUsername: null, unknown: false },
  userAccess: { target: null, busy: false, error: null, notice: null, verifyUsername: null },
  artifacts: { groups: [] }, artifactSel: {},
} }))
vi.mock('./store.js', () => ({ useRunState: () => state, refreshUsers: vi.fn(), createUser: vi.fn() }))
import SidePanel from './components/SidePanel.jsx'
import UsersPanel from './components/UsersPanel.jsx'

describe('用户管理内容', () => {
  it('完整清单稳定排序后默认仅展示二十人，跨页搜索保留旧用户名原文', () => {
    const original = state.users
    state.users = { items: Array.from({ length: 105 }, (_, i) => ({
      username: `member-${String(104 - i).padStart(3, '0')}`, role: 'user', enabled: true,
    })), loading: false, error: null }
    try {
      let html = renderToStaticMarkup(<UsersPanel />)
      expect(html).toContain('共 105 名用户')
      expect(html).toContain('member-019')
      expect(html).not.toContain('member-020')
      expect(html.indexOf('member-000')).toBeLessThan(html.indexOf('member-019'))
      state.usersQuery = { keyword: 'member-104', page: 1, pageSize: 20 }
      html = renderToStaticMarkup(<UsersPanel />)
      expect(html).toContain('member-104')
      expect(html).not.toContain('member-000')
      state.users.items.push({ username: '旧 身份@EXAMPLE.test', role: 'user', enabled: false })
      state.usersQuery.keyword = '@example'
      html = renderToStaticMarkup(<UsersPanel />)
      expect(html).toContain('旧 身份@EXAMPLE.test')
    } finally {
      state.users = original
      state.usersQuery = { keyword: '', page: 1, pageSize: 20 }
    }
  })
  it('重置仅面向普通用户，未知结果持续呈现并要求主动发起新重置', () => {
    let html = renderToStaticMarkup(<UsersPanel />)
    expect(html).toContain('重置密码 legacy')
    expect(html).not.toContain('重置密码 operator')
    expect(html).not.toContain('重置密码 second')
    state.userReset = { target: null, busy: false, unknown: true,
      notice: { tone: 'warning', text: '清单无法验证密码，重置结果仍未知。' } }
    html = renderToStaticMarkup(<UsersPanel />)
    expect(html).toContain('重置结果仍未知')
    expect(html).toContain('发起新的重置 legacy')
    state.userReset = { target: null, busy: false, error: null, notice: null, verifyUsername: null, unknown: false }
  })
  it('只有普通用户有启停按钮；待核实错误在页内可见', () => {
    let html = renderToStaticMarkup(<UsersPanel />)
    expect(html).toContain('启用用户 legacy')
    expect(html).not.toContain('禁用用户 operator')
    expect(html).not.toContain('禁用用户 second')
    state.userAccess = { target: null, busy: false, error: '请刷新清单重新确认', verifyUsername: 'legacy' }
    html = renderToStaticMarkup(<UsersPanel />)
    expect(html).toContain('role="alert"')
    expect(html).toContain('刷新清单核实')
    state.userAccess = { target: null, busy: false, error: null, notice: null, verifyUsername: null }
  })
  it('管理员可见只读用户清单、本地时区创建时间及未知时间', () => {
    const html = renderToStaticMarkup(<UsersPanel />)
    expect(html).toContain('用户管理')
    expect(html).toContain('operator')
    expect(html.match(/管理员 · 只读/g)).toHaveLength(2)
    expect(html).toContain('已禁用')
    expect(html).toContain('未知')
    expect(html).not.toContain(Intl.DateTimeFormat().resolvedOptions().timeZone)
    expect(html).toContain(new Intl.DateTimeFormat('zh-CN', {
      dateStyle: 'medium', timeStyle: 'short',
    }).format(new Date('2026-09-01T02:00:00Z')))
    for (const operation of ['删除', '改名']) expect(html).not.toContain(operation)
    expect(html).toContain('创建用户')
    expect(html).not.toContain('<details')
    expect(html).not.toContain('name="password"')
  })
  it('审计异常和未知结果在页内持续呈现，核实前不能再次创建', () => {
    for (const text of ['变更已生效，审计记录异常', '无法确认新增结果，请先核实']) {
      state.userCreate = { busy: false, notice: { tone: 'warning', text }, verifyUsername: 'new-user' }
      const html = renderToStaticMarkup(<UsersPanel />)
      expect(html).toContain('role="status"')
      expect(html).toContain(text)
      expect(html).toContain('刷新清单核实')
      expect(html).toMatch(/<button[^>]*disabled=""[^>]*>创建用户<\/button>/)
    }
    state.userCreate = { busy: false, error: null, notice: null, verifyUsername: null }
  })
  it('侧栏对所有身份均移除管理入口，旧面板选择不再渲染管理内容', () => {
    for (const value of [true, false, undefined, 'true', 1]) {
      state.canManageUsers = value
      const html = renderToStaticMarkup(<SidePanel />)
      expect(html).not.toContain('用户管理')
      expect(html).not.toContain('operator')
    }
    state.canManageUsers = true
  })
})
