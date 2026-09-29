import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'

const { state } = vi.hoisted(() => ({ state: {
  sidePanel: 'users', tabs: [], canManageUsers: true,
  users: { items: [
    { username: 'operator', role: 'admin', enabled: true, created_at: '2026-09-01T02:00:00Z' },
    { username: 'second', role: 'admin', enabled: true, created_at: null },
    { username: 'legacy', role: 'user', enabled: false, created_at: null },
  ], loading: false, error: null },
  userCreate: { busy: false, error: null, notice: null, verifyUsername: null },
  artifacts: { groups: [] }, artifactSel: {},
} }))
vi.mock('./store.js', () => ({ useRunState: () => state, refreshUsers: vi.fn(), createUser: vi.fn() }))
import SidePanel from './components/SidePanel.jsx'

describe('用户管理侧栏', () => {
  it('管理员可见只读用户清单、本地时区创建时间及未知时间', () => {
    const html = renderToStaticMarkup(<SidePanel />)
    expect(html).toContain('用户管理')
    expect(html).toContain('operator')
    expect(html.match(/管理员 · 只读/g)).toHaveLength(2)
    expect(html).toContain('已禁用')
    expect(html).toContain('未知')
    expect(html).toContain(Intl.DateTimeFormat().resolvedOptions().timeZone)
    expect(html).toContain(new Intl.DateTimeFormat('zh-CN', {
      dateStyle: 'medium', timeStyle: 'short',
    }).format(new Date('2026-09-01T02:00:00Z')))
    for (const operation of ['重置密码', '删除', '改名']) expect(html).not.toContain(operation)
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
    let html = renderToStaticMarkup(<SidePanel />)
    expect(html).toContain('提交中…')
    expect(html.match(/disabled=""/g).length).toBeGreaterThanOrEqual(3)
    state.userCreate = { busy: false, error: '用户名已存在，未覆盖原用户。' }
    html = renderToStaticMarkup(<SidePanel />)
    expect(html).toContain('role="alert"')
    expect(html).toContain('用户名已存在')
    for (const text of ['变更已生效，审计记录异常', '无法确认新增结果，请先核实']) {
      state.userCreate = { busy: false, notice: { tone: 'warning', text }, verifyUsername: 'new-user' }
      html = renderToStaticMarkup(<SidePanel />)
      expect(html).toContain('role="status"')
      expect(html).toContain(text)
      expect(html).toContain('刷新清单核实')
      expect(html).toContain('disabled=""')
    }
    state.userCreate = { busy: false, error: null, notice: null, verifyUsername: null }
  })
  it('能力缺失、非布尔或普通用户即使残留用户面板选择也看不到管理清单', () => {
    for (const value of [false, undefined, 'true', 1]) {
      state.canManageUsers = value
      const html = renderToStaticMarkup(<SidePanel />)
      expect(html).not.toContain('用户管理')
      expect(html).not.toContain('operator')
    }
    state.canManageUsers = true
  })
})
