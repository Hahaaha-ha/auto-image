import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const response = (data, status = 200) => ({ ok: status < 400, status, json: async () => data })
const alice = { username: ' Legacy 用户 / ', role: 'user', enabled: true, user_version: 'alice-v1', created_at: null }
const operator = { username: 'operator', role: 'admin', enabled: true, user_version: 'admin-v1' }
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
  fetch.mockResolvedValue(response({ users: [operator, alice] }))
  await store.refreshUsers()
  fetch.mockClear()
})
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals() })

describe('管理员重置密码', () => {
  it('确认目标时冻结版本，提交精确密码并明确已提交审计异常', async () => {
    store.beginUserReset(operator.username)
    expect(store.getState().userReset.target).toBeNull()
    store.beginUserReset(alice.username)
    expect(fetch).not.toHaveBeenCalled()
    fetch.mockResolvedValue(response({ users: [{ ...alice, user_version: 'changed' }] }))
    await store.refreshUsers()
    fetch.mockClear()
    fetch.mockImplementation(async (url, options) => options?.method === 'POST'
      ? response({ outcome: 'committed', audit_status: 'failed' })
      : response({ users: [{ ...alice, user_version: 'new' }] }))
    expect(await store.resetUserPassword(' temporary-password ')).toBe('committed')
    expect(fetch.mock.calls[0][0]).toBe('/api/admin/users/reset-password')
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ username: alice.username,
      expected_version: 'alice-v1', password: ' temporary-password ' })
    expect(store.getState().userReset.target).toBeNull()
    expect(store.getState().userReset.notice.text).toContain('重置已生效，审计记录异常')
    expect(store.getState().userReset.notice.text).toContain('自行交付')
    expect(JSON.stringify(store.getState())).not.toContain('temporary-password')
  })
  it.each([[409, 'user_version_conflict', '刷新'], [422, 'invalid_new_password', '8–128'],
    [503, 'users_unavailable', '用户文件'], [503, 'audit_unavailable', '审计'],
    [503, 'state_unavailable', '受限恢复'], [403, 'admin_read_only', '管理员'],
    [404, 'no_such_user', '不存在']])('未提交 %s/%s 明确原因，冲突刷新后仍须重新确认', async (status, detail, hint) => {
    store.beginUserReset(alice.username)
    fetch.mockResolvedValue(response({ outcome: 'not_committed', detail }, status))
    expect(await store.resetUserPassword('temporary-password')).toBe('not_committed')
    expect(store.getState().userReset.error).toContain(hint)
    expect(fetch).toHaveBeenCalledTimes(1)
    if (status === 409) {
      expect(store.getState().userReset.target).toBeNull()
      store.beginUserReset(alice.username)
      await store.resetUserPassword('temporary-password')
      expect(fetch).toHaveBeenCalledTimes(1)
      fetch.mockResolvedValue(response({ users: [{ ...alice, user_version: 'fresh' }] }))
      await store.refreshUsers()
      expect(store.getState().userReset.target).toBeNull()
      store.beginUserReset(alice.username)
      expect(store.getState().userReset.target.user_version).toBe('fresh')
    }
  })

  it.each(['offline', 'bad-json', 'unexpected-body'])('结果未知 %s，刷新清单不能验证密码，新的重置须主动发起', async failure => {
    store.beginUserReset(alice.username)
    fetch.mockImplementation(async () => {
      if (failure === 'offline') throw new TypeError('offline')
      if (failure === 'bad-json') return { status: 502, json: async () => { throw new SyntaxError('invalid') } }
      return response({}, 502)
    })
    expect(await store.resetUserPassword('temporary-password')).toBe('unknown')
    expect(store.getState().userReset.notice.text).toContain('无法确认')
    expect(store.getState().userReset.target).toBeNull()
    fetch.mockRejectedValue(new TypeError('offline'))
    await store.refreshUsers()
    store.beginUserReset(alice.username)
    await store.resetUserPassword('temporary-password')
    expect(store.getState().userReset.verifyUsername).toBe(alice.username)
    expect(fetch.mock.calls.filter(([, options]) => options?.method === 'POST')).toHaveLength(1)
    fetch.mockResolvedValue(response({ users: [{ ...alice, user_version: 'fresh' }] }))
    await store.refreshUsers()
    expect(store.getState().userReset.unknown).toBe(true)
    expect(store.getState().userReset.notice.text).toContain('清单无法验证密码')
    expect(store.getState().userReset.notice.text).toContain('仍未知')
    expect(store.getState().userReset.target).toBeNull()
    await store.resetUserPassword('temporary-password')
    await vi.advanceTimersByTimeAsync(15000)
    expect(fetch.mock.calls.filter(([, options]) => options?.method === 'POST')).toHaveLength(1)
    store.beginUserReset(alice.username)
    fetch.mockResolvedValue(response({ outcome: 'committed', audit_status: 'recorded' }))
    expect(await store.resetUserPassword('next-password')).toBe('committed')
    expect(JSON.parse(fetch.mock.calls.filter(([, options]) => options?.method === 'POST').at(-1)[1].body))
      .toEqual({ username: alice.username, expected_version: 'fresh', password: 'next-password' })
  })

  it('提交前的在途清单不能解除后发生的未知结果限制', async () => {
    store.beginUserReset(alice.username)
    let finish
    fetch.mockImplementation(() => new Promise(done => { finish = done }))
    const loading = store.refreshUsers()
    fetch.mockRejectedValue(new TypeError('response lost'))
    await store.resetUserPassword('temporary-password')
    finish(response({ users: [alice] }))
    await loading
    expect(store.getState().userReset.verifyUsername).toBe(alice.username)
  })

  it.each([401, 403])('权限失效 %s 清除确认与用户管理', async status => {
    store.beginUserReset(alice.username)
    fetch.mockResolvedValue(response({ detail: status === 401 ? 'bad_cookie' : 'not_admin' }, status))
    await store.resetUserPassword('temporary-password')
    expect(store.getState().canManageUsers).toBe(false)
    expect(store.getState().userReset.target).toBeNull()
    if (status === 401) expect(store.getState().auth).toBe('anonymous')
  })

  it('提交期间不能取消或重复提交，切换身份后忽略迟到响应', async () => {
    store.beginUserReset(alice.username)
    let finish
    fetch.mockImplementation(() => new Promise(done => { finish = done }))
    const saving = store.resetUserPassword('temporary-password')
    store.cancelUserReset()
    store.beginUserReset(alice.username)
    await store.resetUserPassword('temporary-password')
    expect(fetch).toHaveBeenCalledTimes(1)
    expect(store.getState().userReset.busy).toBe(true)
    fetch.mockResolvedValue(response({ username: 'second', can_manage_users: true, runs: [], tasks: [] }))
    await store.login('second', 'password')
    finish(response({ outcome: 'committed', audit_status: 'recorded' }))
    await saving
    expect(store.getState().userReset.notice).toBeNull()
    expect(store.getState().userReset.target).toBeNull()
  })

  it('选择、取消不写入，启停与重置确认互斥，禁用用户仍可重置', async () => {
    store.beginUserReset(alice.username)
    store.beginUserAccess(alice.username)
    expect(store.getState().userAccess.target).toBeNull()
    store.cancelUserReset()
    await store.resetUserPassword('temporary-password')
    expect(fetch).not.toHaveBeenCalled()
    store.beginUserAccess(alice.username)
    store.beginUserReset(alice.username)
    expect(store.getState().userReset.target).toBeNull()
    store.cancelUserAccess()
    fetch.mockResolvedValue(response({ users: [{ ...alice, enabled: false }] }))
    await store.refreshUsers()
    store.beginUserReset(alice.username)
    fetch.mockResolvedValue(response({ outcome: 'committed', audit_status: 'recorded' }))
    await store.resetUserPassword('temporary-password')
    expect(store.getState().userReset.notice.text).toContain('仍已禁用')
  })
})
