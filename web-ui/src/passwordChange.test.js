import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const response = (data, status = 200) => ({ ok: status < 400, status, json: async () => data })
const pending = { username: 'alice', must_change_password: true, user_version: 'alice-v1' }
let store
let sources

beforeEach(async () => {
  vi.resetModules()
  vi.useFakeTimers()
  sources = []
  vi.stubGlobal('EventSource', class {
    constructor() { this.close = vi.fn(); sources.push(this) }
    addEventListener() {}
  })
  vi.stubGlobal('fetch', vi.fn(async () => response({}, 401)))
  store = await import('./store.js')
  await store.initAuth()
  fetch.mockClear()
})

afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals() })

describe('待改密身份', () => {
  it('登录和刷新只进入改密流程，不建流或轮询业务', async () => {
    fetch.mockImplementation(async () => response(pending))
    await store.login('alice', 'initial')
    expect(store.getState().auth).toBe('password-change')
    expect(store.getState().userVersion).toBe('alice-v1')
    await store.initAuth()
    await vi.advanceTimersByTimeAsync(15000)
    expect(sources).toHaveLength(0)
    expect(fetch.mock.calls.map(([url]) => url)).toEqual(['/api/auth/login', '/api/auth/me'])
  })

  it('身份转为待改密后关闭已有流，迟到的会话清单不能启动快照加载', async () => {
    let finishRuns
    fetch.mockImplementation(async (url) => {
      if (url === '/api/runs') return new Promise((done) => { finishRuns = done })
      return response({ username: 'alice' })
    })
    await store.login('alice', 'initial')
    fetch.mockResolvedValue(response(pending))
    await store.initAuth()
    fetch.mockClear()
    finishRuns(response({ runs: [{ run_id: 'old-run', status: 'READY' }] }))
    await vi.advanceTimersByTimeAsync(15000)
    expect(sources[0].close).toHaveBeenCalled()
    expect(store.getState().auth).toBe('password-change')
    expect(fetch).not.toHaveBeenCalled()
    expect(store.getState().order).toEqual([])
  })

  it('转为待改密时，在途摘要轮询的后续任务刷新也停止', async () => {
    fetch.mockResolvedValue(response({ username: 'alice', runs: [] }))
    await store.login('alice', 'initial')
    let finishPoll
    fetch.mockImplementation(async (url) => {
      if (url === '/api/runs') return new Promise((done) => { finishPoll = done })
      return response(pending)
    })
    await vi.advanceTimersByTimeAsync(5000)
    await store.initAuth()
    fetch.mockClear()
    finishPoll(response({ runs: [] }))
    await vi.advanceTimersByTimeAsync(15000)
    expect(store.getState().auth).toBe('password-change')
    expect(fetch).not.toHaveBeenCalled()
  })

  it.each(['recorded', 'failed'])('改密已提交（审计 %s）回登录并保留明确提示', async (auditStatus) => {
    fetch.mockResolvedValue(response(pending))
    await store.login('alice', 'initial')
    fetch.mockClear()
    fetch.mockResolvedValue(response({ outcome: 'committed', audit_status: auditStatus }))
    await store.changePassword('initial', 'new-password', 'new-password')
    expect(fetch).toHaveBeenCalledTimes(1)
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({
      current_password: 'initial', new_password: 'new-password',
      confirm_password: 'new-password', expected_version: 'alice-v1',
    })
    expect(store.getState().auth).toBe('anonymous')
    expect(store.getState().authNotice.text).toContain('新密码重新登录')
    if (auditStatus === 'failed') expect(store.getState().authNotice.text).toContain('审计记录异常')
    expect(sources).toHaveLength(0)
  })

  it.each(['network', 'invalid-response'])('结果未知（%s）提示密码恢复顺序，不自动重试', async (failure) => {
    fetch.mockResolvedValue(response(pending))
    await store.login('alice', 'initial')
    fetch.mockClear()
    if (failure === 'network') fetch.mockRejectedValue(new TypeError('offline'))
    else fetch.mockResolvedValue(response({}, 502))
    await store.changePassword('initial', 'new-password', 'new-password')
    await vi.advanceTimersByTimeAsync(30000)
    expect(fetch).toHaveBeenCalledTimes(1)
    expect(store.getState().auth).toBe('anonymous')
    expect(store.getState().authNotice.text).toContain('先用新密码登录')
    expect(store.getState().authNotice.text).toContain('原密码')
    expect(store.getState().authNotice.text).toContain('两者均失败请联系管理员')
    expect(sources).toHaveLength(0)
  })

  it.each([[422, 'current_password_incorrect', '当前密码不正确'],
    [422, 'invalid_new_password', '8–128'], [503, 'users_unavailable', '尚未修改'],
    [503, 'audit_unavailable', '尚未修改']])('明确未提交 %s/%s 保留表单供修正', async (status, detail, hint) => {
    fetch.mockResolvedValue(response(pending))
    await store.login('alice', 'initial')
    fetch.mockResolvedValue(response({ outcome: 'not_committed', detail }, status))
    await store.changePassword('initial', 'new-password', 'new-password')
    expect(store.getState().auth).toBe('password-change')
    expect(store.getState().passwordChange.busy).toBe(false)
    expect(store.getState().passwordChange.error).toContain(hint)
  })

  it.each([401, 403, 409])('过期或冲突 %s 返回登录以重新确认身份', async (status) => {
    fetch.mockResolvedValue(response(pending))
    await store.login('alice', 'initial')
    fetch.mockResolvedValue(response({ outcome: 'not_committed', detail: 'user_version_conflict' }, status))
    await store.changePassword('initial', 'new-password', 'new-password')
    expect(store.getState().auth).toBe('anonymous')
    expect(store.getState().authNotice.text).toContain('重新登录')
    expect(store.getState().authNotice.text).toContain('尚未修改')
  })

  it('提交在途禁用重复提交，迟到的响应不覆盖新登录身份', async () => {
    fetch.mockResolvedValue(response(pending))
    await store.login('alice', 'initial')
    let resolve
    fetch.mockClear()
    fetch.mockImplementation(() => new Promise((done) => { resolve = done }))
    const saving = store.changePassword('initial', 'new-password', 'new-password')
    await store.changePassword('initial', 'new-password', 'new-password')
    expect(fetch).toHaveBeenCalledTimes(1)
    expect(store.getState().passwordChange.busy).toBe(true)
    fetch.mockResolvedValue(response({ ...pending, username: 'bob' }))
    await store.login('bob', 'initial')
    resolve(response({ outcome: 'committed', audit_status: 'recorded' }))
    await saving
    expect(store.getState().auth).toBe('password-change')
    expect(store.getState().user).toBe('bob')
  })
})
