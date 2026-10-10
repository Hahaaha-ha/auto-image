import { renderToStaticMarkup } from 'react-dom/server'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const { state } = vi.hoisted(() => ({ state: {} }))
vi.mock('./store.js', () => ({
  useRunState: () => state, useControlRun: () => null,
  changePassword: vi.fn(), logout: vi.fn(),
}))
import App from './App.jsx'

beforeEach(() => Object.assign(state, {
  auth: 'password-change', user: 'alice', tabs: [], runs: {},
  passwordChange: { busy: false, error: null }, authNotice: null,
}))

describe('强制改密页面', () => {
  it('只显示身份和有标签的改密表单、规则及登出入口', () => {
    const html = renderToStaticMarkup(<App />)
    for (const text of ['设置新密码', 'alice', '当前密码', '新密码', '确认新密码', '8–128', 'ASCII', '登出']) {
      expect(html).toContain(text)
    }
    expect(html.match(/type="password"/g)).toHaveLength(3)
    expect(html).toContain('autoComplete="current-password"')
    expect(html).toContain('autoComplete="new-password"')
    for (const text of ['新建会话', '产物', 'OBS', 'ECS', '用户管理']) expect(html).not.toContain(text)
  })

  it('错误可朗读，提交中按钮禁用且显示进度', () => {
    state.passwordChange = { busy: true, error: '当前密码不正确，请重新输入。' }
    const html = renderToStaticMarkup(<App />)
    expect(html).toContain('role="alert"')
    expect(html).toContain('当前密码不正确')
    expect(html).toContain('提交中…')
    expect(html).toContain('disabled=""')
  })

  it.each(['密码已生效，审计记录异常。请用新密码重新登录。',
    '无法确认改密结果。请先用新密码登录；失败可尝试原密码，两者均失败请联系管理员。'])('登录页持续显示恢复提示：%s', (text) => {
    state.auth = 'anonymous'
    state.authNotice = { tone: 'warning', text }
    const html = renderToStaticMarkup(<App />)
    expect(html).toContain(text)
    expect(html).toContain('role="status"')
    expect(html).not.toContain('确认新密码')
  })
})
