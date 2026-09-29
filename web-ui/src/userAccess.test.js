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

describe('用户访问资格', () => {
  it('提交前在途的清单不能核实之后才出现的未知结果', async () => {
    store.beginUserAccess(alice.username)
    let finish
    fetch.mockImplementation(() => new Promise(done => { finish = done }))
    const loading = store.refreshUsers()
    fetch.mockRejectedValue(new TypeError('response lost'))
    await store.submitUserAccess()
    finish(response({ users: [alice] }))
    await loading
    expect(store.getState().userAccess.verifyUsername).toBe(alice.username)
    expect(store.getState().userAccess.notice.text).toContain('无法确认')
  })
  it.each([[409, 'user_version_conflict', '刷新'], [503, 'users_unavailable', '用户文件'],
    [503, 'audit_unavailable', '审计'], [503, 'state_unavailable', '受限恢复'],
    [403, 'admin_read_only', '管理员'], [404, 'no_such_user', '不存在']])(
    '未提交 %s/%s 显示原因，冲突必须刷新重确认', async (status, detail, hint) => {
      store.beginUserAccess(alice.username)
      fetch.mockResolvedValue(response({ outcome: 'not_committed', detail }, status))
      expect(await store.submitUserAccess()).toBe('not_committed')
      expect(store.getState().userAccess.error).toContain(hint)
      expect(fetch).toHaveBeenCalledTimes(1)
      if (status === 409) {
        expect(store.getState().userAccess.target).toBeNull()
        store.beginUserAccess(alice.username)
        await store.submitUserAccess()
        expect(fetch).toHaveBeenCalledTimes(1)
        fetch.mockResolvedValue(response({ users: [{ ...alice, user_version: 'fresh' }] }))
        await store.refreshUsers()
        expect(store.getState().userAccess.target).toBeNull()
        store.beginUserAccess(alice.username)
        expect(store.getState().userAccess.target.user_version).toBe('fresh')
      }
    })

  it.each(['offline', 'bad-json', 'unexpected-body'])('结果未知 %s 不自动重提，刷新失败不能解锁', async failure => {
    store.beginUserAccess(alice.username)
    fetch.mockImplementation(async () => {
      if (failure === 'offline') throw new TypeError('offline')
      if (failure === 'bad-json') return { status: 502, json: async () => { throw new SyntaxError('invalid') } }
      return response({}, 502)
    })
    expect(await store.submitUserAccess()).toBe('unknown')
    expect(store.getState().userAccess.notice.text).toContain('无法确认')
    fetch.mockRejectedValue(new TypeError('offline'))
    await store.refreshUsers()
    expect(store.getState().userAccess.verifyUsername).toBe(alice.username)
    store.beginUserAccess(alice.username)
    await store.submitUserAccess()
    expect(fetch.mock.calls.filter(([, options]) => options?.method === 'POST')).toHaveLength(1)
    fetch.mockResolvedValue(response({ users: [{ ...alice, enabled: false, user_version: 'fresh' }] }))
    await store.refreshUsers()
    expect(store.getState().userAccess.verifyUsername).toBeNull()
    expect(store.getState().userAccess.notice.text).toContain('当前已禁用')
    expect(store.getState().userAccess.target).toBeNull()
    await vi.advanceTimersByTimeAsync(15000)
    expect(fetch.mock.calls.filter(([, options]) => options?.method === 'POST')).toHaveLength(1)
    store.beginUserAccess(alice.username)
    fetch.mockResolvedValue(response({ outcome: 'committed', audit_status: 'recorded' }))
    expect(await store.submitUserAccess()).toBe('committed')
    expect(fetch.mock.calls.filter(([, options]) => options?.method === 'POST').at(-1)[0]).toBe('/api/admin/users/enable')
  })

  it.each([401, 403])('权限失效 %s 清除确认与用户管理', async status => {
    store.beginUserAccess(alice.username)
    fetch.mockResolvedValue(response({ detail: status === 401 ? 'bad_cookie' : 'not_admin' }, status))
    await store.submitUserAccess()
    expect(store.getState().canManageUsers).toBe(false)
    expect(store.getState().userAccess.target).toBeNull()
    if (status === 401) expect(store.getState().auth).toBe('anonymous')
  })

  it('提交中锁定操作，身份切换后忽略迟到响应', async () => {
    store.beginUserAccess(alice.username)
    let finish
    fetch.mockImplementation(() => new Promise(done => { finish = done }))
    const saving = store.submitUserAccess()
    store.cancelUserAccess()
    store.beginUserAccess(alice.username)
    await store.submitUserAccess()
    expect(fetch).toHaveBeenCalledTimes(1)
    expect(store.getState().userAccess.busy).toBe(true)
    fetch.mockResolvedValue(response({ username: 'second', can_manage_users: true, runs: [], tasks: [] }))
    await store.login('second', 'password')
    finish(response({ outcome: 'committed', audit_status: 'recorded' }))
    await saving
    expect(store.getState().userAccess.notice).toBeNull()
    expect(store.getState().userAccess.target).toBeNull()
  })
  it.each(['recorded', 'failed'])('确认提交保留精确用户名和原版本，结果审计 %s 后刷新清单', async (auditStatus) => {
    store.beginUserAccess(alice.username)
    fetch.mockResolvedValue(response({ users: [{ ...alice, user_version: 'changed' }] }))
    await store.refreshUsers()
    fetch.mockClear()
    fetch.mockImplementation(async (url, options) => options?.method === 'POST'
      ? response({ outcome: 'committed', audit_status: auditStatus })
      : response({ users: [{ ...alice, enabled: false, user_version: 'v2' }] }))
    expect(await store.submitUserAccess()).toBe('committed')
    expect(fetch.mock.calls[0][0]).toBe('/api/admin/users/disable')
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ username: alice.username, expected_version: 'alice-v1' })
    expect(store.getState().userAccess.target).toBeNull()
    expect(store.getState().userAccess.notice.text).toContain('已禁用')
    if (auditStatus === 'failed') expect(store.getState().userAccess.notice.text).toContain('审计记录异常')
  })
  it('选择只打开确认，取消不写入；管理员不能选中', async () => {
    store.beginUserAccess(operator.username)
    expect(store.getState().userAccess.target).toBeNull()
    store.beginUserAccess(alice.username)
    expect(store.getState().userAccess.target).toEqual(alice)
    store.cancelUserAccess()
    await store.submitUserAccess()
    expect(fetch).not.toHaveBeenCalled()
  })
})
