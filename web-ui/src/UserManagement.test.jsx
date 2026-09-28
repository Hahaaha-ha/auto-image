import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'

const { state } = vi.hoisted(() => ({ state: {
  sidePanel: 'users', tabs: [], canManageUsers: true,
  users: { items: [
    { username: 'operator', role: 'admin', enabled: true, created_at: '2026-09-01T02:00:00Z' },
    { username: 'second', role: 'admin', enabled: true, created_at: null },
    { username: 'legacy', role: 'user', enabled: false, created_at: null },
  ], loading: false, error: null },
  artifacts: { groups: [] }, artifactSel: {},
} }))
vi.mock('./store.js', () => ({ useRunState: () => state, refreshUsers: vi.fn() }))
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
    for (const operation of ['重置密码', '新增用户', '删除', '改名']) expect(html).not.toContain(operation)
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
