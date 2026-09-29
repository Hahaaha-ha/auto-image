import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const response = (data, status = 200) => ({ ok: status < 400, status, json: async () => data })
let store
beforeEach(async () => {
  vi.resetModules()
  vi.useFakeTimers()
  vi.stubGlobal('EventSource', class { addEventListener() {} close() {} })
  vi.stubGlobal('fetch', vi.fn(async () => response({}, 401)))
  store = await import('./store.js')
  await store.initAuth()
  fetch.mockResolvedValue(response({ username: 'operator', can_manage_users: true, runs: [], tasks: [] }))
  await store.login('operator', 'password')
  fetch.mockClear()
})
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals() })

describe('新增用户', () => {
  it.each(['committed', 'unknown'])('清单读取挂起不阻止返回 %s，让表单及时清空密码', async (outcome) => {
    let finishRead, result
    fetch.mockImplementation(async (url, options) => {
      if (options?.method !== 'POST') return new Promise((done) => { finishRead = done })
      if (outcome === 'unknown') throw new TypeError('response lost')
      return response({ outcome: 'committed', audit_status: 'recorded' }, 201)
    })
    const saving = store.createUser('new-user', 'initial-password').then(value => { result = value })
    try {
      await vi.waitFor(() => expect(result).toBe(outcome), { timeout: 100 })
      expect(store.getState().userCreate.busy).toBe(false)
      expect(store.getState().users.loading).toBe(true)
    } finally {
      finishRead(response({ users: [] }))
      await saving
    }
  })

  it.each(['invalid-json', 'unexpected-body'])('异常响应 %s 同样先核实，不自动重提', async (failure) => {
    fetch.mockImplementation(async (url, options) => {
      if (options?.method !== 'POST') return response({ users: [] })
      return failure === 'invalid-json' ? { status: 502, json: async () => { throw new SyntaxError('bad json') } }
        : response({}, 502)
    })
    expect(await store.createUser('new-user', 'initial-password')).toBe('unknown')
    await vi.waitFor(() => expect(store.getState().userCreate.notice.text).toContain('未找到'))
    expect(fetch.mock.calls.map(([, options]) => options?.method || 'GET')).toEqual(['POST', 'GET'])
  })

  it.each([[422, 'invalid_username', '1–64'], [422, 'invalid_new_password', '8–128'],
    [409, 'username_exists', '已存在'], [503, 'users_unavailable', '用户文件'],
    [503, 'audit_unavailable', '审计'], [503, 'state_unavailable', '受限恢复']])(
    '明确未提交 %s/%s 显示可执行错误', async (status, detail, hint) => {
      fetch.mockResolvedValue(response({ outcome: 'not_committed', detail }, status))
      expect(await store.createUser(' new-user ', 'initial-password')).toBe('not_committed')
      expect(JSON.parse(fetch.mock.calls[0][1].body).username).toBe(' new-user ')
      expect(store.getState().userCreate.error).toContain(hint)
      expect(store.getState().userCreate.busy).toBe(false)
      expect(fetch).toHaveBeenCalledTimes(1)
    })

  it.each([401, 403])('创建请求 %s 清除权限与新增状态', async (status) => {
    fetch.mockResolvedValue(response({ detail: status === 403 ? 'not_admin' : 'bad_cookie' }, status))
    await store.createUser('new-user', 'initial-password')
    expect(store.getState().canManageUsers).toBe(false)
    expect(store.getState().userCreate.busy).toBe(false)
    if (status === 401) expect(store.getState().auth).toBe('anonymous')
    fetch.mockClear()
    await store.createUser('new-user', 'initial-password')
    expect(fetch).not.toHaveBeenCalled()
  })

  it('提交中只发一次，身份切换后迟到响应不污染新用户', async () => {
    let finish
    fetch.mockImplementation(() => new Promise((done) => { finish = done }))
    const saving = store.createUser('new-user', 'initial-password')
    await store.createUser('new-user', 'initial-password')
    expect(fetch).toHaveBeenCalledTimes(1)
    expect(store.getState().userCreate.busy).toBe(true)
    fetch.mockResolvedValue(response({ username: 'second', can_manage_users: true, runs: [], tasks: [] }))
    await store.login('second', 'password')
    finish(response({ outcome: 'committed', audit_status: 'recorded' }, 201))
    await saving
    expect(store.getState().userCreate.notice).toBeNull()
    expect(store.getState().userCreate.busy).toBe(false)
    expect(store.getState().users.items).toEqual([])
  })

  it('创建后刷新失败仍保留已生效提示，不误报为未知或重提', async () => {
    fetch.mockImplementation(async (url, options) => {
      if (options?.method === 'POST') return response({ outcome: 'committed', audit_status: 'recorded' }, 201)
      throw new TypeError('offline')
    })
    expect(await store.createUser('new-user', 'initial-password')).toBe('committed')
    expect(store.getState().userCreate.notice.text).toContain('已创建')
    expect(store.getState().userCreate.verifyUsername).toBeNull()
    expect(store.getState().users.error).toContain('加载失败')
  })

  it('创建后的刷新取代创建前在途清单，旧响应不能覆盖新用户', async () => {
    let finish
    fetch.mockImplementation(() => new Promise((done) => { finish = done }))
    const loading = store.refreshUsers()
    fetch.mockImplementation(async (url, options) => options?.method === 'POST'
      ? response({ outcome: 'committed', audit_status: 'recorded' }, 201)
      : response({ users: [{ username: 'new-user' }] }))
    await store.createUser('new-user', 'initial-password')
    finish(response({ users: [] }))
    await loading
    expect(store.getState().users.items).toEqual([{ username: 'new-user' }])
  })

  it.each([true, false])('结果未知先读清单核实（同名存在 %s），读失败期间阻止再次新增', async (exists) => {
    fetch.mockRejectedValue(new TypeError('offline'))
    expect(await store.createUser('new-user', 'initial-password')).toBe('unknown')
    expect(store.getState().userCreate.verifyUsername).toBe('new-user')
    expect(store.getState().userCreate.notice.text).toContain('无法确认')
    expect(store.getState().users.error).toContain('加载失败')
    await store.createUser('new-user', 'initial-password')
    expect(fetch.mock.calls.filter(([, options]) => options?.method === 'POST')).toHaveLength(1)
    fetch.mockImplementation(async (url) => url === '/api/admin/users'
      ? response({ users: exists ? [{ username: 'new-user' }] : [] }) : response({ runs: [], tasks: [] }))
    await store.refreshUsers()
    expect(store.getState().userCreate.verifyUsername).toBeNull()
    expect(store.getState().userCreate.notice.text).toContain(exists ? '已有同名用户' : '未找到')
    await vi.advanceTimersByTimeAsync(15000)
    expect(fetch.mock.calls.filter(([, options]) => options?.method === 'POST')).toHaveLength(1)
    expect(JSON.stringify(store.getState())).not.toContain('initial-password')
  })

  it.each(['recorded', 'failed'])('创建后刷新真实清单，审计 %s 仍明确已生效', async (auditStatus) => {
    const created = { username: 'New.user', role: 'user', enabled: true, created_at: '2026-09-29T00:00:00Z' }
    fetch.mockImplementation(async (url, options) => options?.method === 'POST'
      ? response({ outcome: 'committed', audit_status: auditStatus }, 201)
      : response({ users: [created] }))
    expect(await store.createUser('New.user', 'initial-password')).toBe('committed')
    expect(fetch.mock.calls.map(([url, options]) => [url, options?.method || 'GET'])).toEqual([
      ['/api/admin/users', 'POST'], ['/api/admin/users', 'GET'],
    ])
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ username: 'New.user', password: 'initial-password' })
    await vi.waitFor(() => expect(store.getState().users.items).toEqual([created]))
    expect(store.getState().userCreate.busy).toBe(false)
    expect(store.getState().userCreate.notice.text).toContain('已创建')
    if (auditStatus === 'failed') expect(store.getState().userCreate.notice.text).toContain('审计记录异常')
    expect(JSON.stringify(store.getState())).not.toContain('initial-password')
  })
})
