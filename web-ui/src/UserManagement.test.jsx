import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'

const { state } = vi.hoisted(() => ({ state: {
  sidePanel: 'users', tabs: [], canManageUsers: true,
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
  it('重置仅面向普通用户，确认目标、撤销与交付说明，禁用状态保留', () => {
    let html = renderToStaticMarkup(<UsersPanel />)
    expect(html).toContain('重置密码 legacy')
    expect(html).not.toContain('重置密码 operator')
    expect(html).not.toContain('重置密码 second')
    state.userReset.target = { username: 'legacy', enabled: false }
    html = renderToStaticMarkup(<UsersPanel />)
    expect(html).toContain('确认重置「legacy」的密码')
    expect(html).toContain('全部既有登录')
    expect(html).toContain('自行交付')
    expect(html).toContain('仍保持禁用')
    expect(html).toContain('无法回看')
    expect(html).toContain('name="reset_password"')
    expect(html).toContain('type="password"')
    state.userReset = { target: null, busy: false, unknown: true,
      notice: { tone: 'warning', text: '清单无法验证密码，重置结果仍未知。' } }
    html = renderToStaticMarkup(<UsersPanel />)
    expect(html).toContain('重置结果仍未知')
    expect(html).toContain('发起新的重置 legacy')
    state.userReset = { target: null, busy: false, error: null, notice: null, verifyUsername: null, unknown: false }
  })
  it('只有普通用户有启停按钮；确认清楚展示目标和影响', () => {
    let html = renderToStaticMarkup(<UsersPanel />)
    expect(html).toContain('启用用户 legacy')
    expect(html).not.toContain('禁用用户 operator')
    expect(html).not.toContain('禁用用户 second')
    state.userAccess.target = { username: 'legacy', enabled: true }
    html = renderToStaticMarkup(<UsersPanel />)
    expect(html).toContain('确认禁用「legacy」')
    expect(html).toContain('撤销既有登录')
    expect(html).toContain('不停止回合')
    expect(html).toContain('不撤销已提交的云操作')
    expect(html).toContain('取消')
    state.userAccess.busy = true
    html = renderToStaticMarkup(<UsersPanel />)
    expect(html).toContain('提交中…')
    state.userAccess = { target: null, busy: false, error: '请刷新清单重新确认', verifyUsername: 'legacy' }
    html = renderToStaticMarkup(<UsersPanel />)
    expect(html).toContain('role="alert"')
    expect(html).toContain('刷新清单核实')
    state.userAccess = { target: { username: 'legacy', enabled: false }, busy: false }
    html = renderToStaticMarkup(<UsersPanel />)
    expect(html).toContain('原使用者')
    expect(html).toContain('重新登录')
    state.userAccess = { target: null, busy: false, error: null, notice: null, verifyUsername: null }
  })
  it('管理员可见只读用户清单、本地时区创建时间及未知时间', () => {
    const html = renderToStaticMarkup(<UsersPanel />)
    expect(html).toContain('用户管理')
    expect(html).toContain('operator')
    expect(html.match(/管理员 · 只读/g)).toHaveLength(2)
    expect(html).toContain('已禁用')
    expect(html).toContain('未知')
    expect(html).toContain(Intl.DateTimeFormat().resolvedOptions().timeZone)
    expect(html).toContain(new Intl.DateTimeFormat('zh-CN', {
      dateStyle: 'medium', timeStyle: 'short',
    }).format(new Date('2026-09-01T02:00:00Z')))
    for (const operation of ['删除', '改名']) expect(html).not.toContain(operation)
    expect(html).toContain('新增用户')
    expect(html).toContain('初始密码')
    expect(html).toContain('type="password"')
    expect(html).toContain('1–64')
    expect(html).toContain('8–128')
    expect(html).toContain('自行交付')
    expect(html).toContain('首次登录')
    expect(html).not.toContain('name="role"')
  })
  it('提交状态、校验错误、审计异常和未知结果展示于表单旁', () => {
    state.userCreate = { busy: true }
    let html = renderToStaticMarkup(<UsersPanel />)
    expect(html).toContain('提交中…')
    expect(html.match(/disabled=""/g).length).toBeGreaterThanOrEqual(3)
    state.userCreate = { busy: false, error: '用户名已存在，未覆盖原用户。' }
    html = renderToStaticMarkup(<UsersPanel />)
    expect(html).toContain('role="alert"')
    expect(html).toContain('用户名已存在')
    for (const text of ['变更已生效，审计记录异常', '无法确认新增结果，请先核实']) {
      state.userCreate = { busy: false, notice: { tone: 'warning', text }, verifyUsername: 'new-user' }
      html = renderToStaticMarkup(<UsersPanel />)
      expect(html).toContain('role="status"')
      expect(html).toContain(text)
      expect(html).toContain('刷新清单核实')
      expect(html).toContain('disabled=""')
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
