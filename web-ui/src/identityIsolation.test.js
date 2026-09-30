import { afterEach, beforeEach, expect, it, vi } from 'vitest'

const response = (data) => ({ ok: true, status: 200, json: async () => data })
let store, sources
beforeEach(async () => {
  vi.resetModules()
  vi.useFakeTimers()
  vi.stubGlobal('localStorage', { getItem: key => key === 'va-side-panel' ? 'users' : null, setItem: vi.fn() })
  sources = []
  vi.stubGlobal('EventSource', class {
    static CLOSED = 2
    constructor() { sources.push(this) }
    addEventListener() {}
    close() { this.closed = true }
  })
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 401 })))
  store = await import('./store.js')
  await store.initAuth()
  fetch.mockResolvedValue(response({ username: 'operator', can_manage_users: true, runs: [], tasks: [], groups: [] }))
  await store.login('operator', 'password')
})
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals() })

it('旧用户侧栏选择回落到产物，管理员也不能再选择已移除的面板', () => {
  expect(store.getState().sidePanel).toBe('artifacts')
  store.setSidePanel('users')
  expect(store.getState().sidePanel).toBe('artifacts')
})

it('换身份清除控制面、草稿和文件缓存，迟到的文件读取不能打开旧标签', async () => {
  await store.openArtifact('deploy/old.rpm', { name: 'old.rpm', binary: true })
  store.setDraft('old-run', 'private draft')
  const finish = []
  fetch.mockImplementation(() => new Promise(resolve => finish.push(resolve)))
  const pending = [store.openArtifact('deploy/late.md'), store.openObsObject('late.md')]
  fetch.mockResolvedValue(response({ username: 'second', runs: [], tasks: [], groups: [] }))
  await store.login('second', 'password')
  for (const resolve of finish) resolve(response({ name: 'late.md', content: 'private content' }))
  await Promise.all(pending)
  expect(store.getState().tabs).toEqual([])
  expect(store.getState().artifactCache).toEqual({})
  expect(store.getState().obsCache).toEqual({})
  expect(store.draftOf('old-run')).toBe('')
})

it('登出后重新登录同名身份，旧清单响应也不能恢复管理反馈', async () => {
  let finish
  fetch.mockImplementation(() => new Promise(resolve => { finish = resolve }))
  const pending = store.refreshUsers()
  fetch.mockResolvedValue(response({ username: 'operator', can_manage_users: true, runs: [], tasks: [], groups: [] }))
  await store.logout()
  await store.login('operator', 'password')
  finish(response({ users: [{ username: 'old-private-user' }] }))
  await pending
  expect(store.getState().users.items).toEqual([])
})

it('切换身份后在途后台清单不能覆盖新身份的资源与任务', async () => {
  const finish = []
  fetch.mockImplementation(() => new Promise(resolve => finish.push(resolve)))
  const pending = [store.refreshTasks(), store.refreshArtifacts(), store.refreshObs(), store.refreshEcs()]
  fetch.mockResolvedValue(response({ username: 'second', runs: [], tasks: [], groups: [] }))
  await store.login('second', 'password')
  for (const resolve of finish) resolve(response({ tasks: [{ task_id: 'old-task' }], groups: [],
    objects: [{ key: 'old-object' }], instances: [{ id: 'old-instance' }] }))
  await Promise.all(pending)
  expect(store.getState().tasks).toEqual([])
  expect(store.getState().obs.objects).toEqual([])
  expect(store.getState().ecs.instances).toEqual([])
})

it('登出后的迟到身份检查不能重新登录旧身份', async () => {
  let finish
  fetch.mockImplementation(() => new Promise(resolve => { finish = resolve }))
  const checking = store.initAuth()
  fetch.mockResolvedValue(response({}))
  await store.logout()
  finish(response({ username: 'operator', can_manage_users: true }))
  await checking
  expect(store.getState().auth).toBe('anonymous')
  expect(store.getState().user).toBeNull()
})

it('SSE 复核换身份后重新同步本人会话，只有一条活动流和一组轮询', async () => {
  const previous = sources.at(-1)
  previous.readyState = EventSource.CLOSED
  fetch.mockResolvedValue(response({ username: 'second', runs: [], tasks: [], groups: [] }))
  await previous.onerror()
  expect(store.getState().user).toBe('second')
  expect(sources.filter(source => !source.closed)).toHaveLength(1)
  expect(vi.getTimerCount()).toBe(2)
})

it.each(['createRun', 'cloneRun', 'runTask'])('换身份后 %s 的迟到响应不能打开旧会话', async (action) => {
  fetch.mockResolvedValue(response({ run_id: 'base-run', status: 'READY' }))
  await store.createRun()
  let finish
  fetch.mockImplementation(() => new Promise(resolve => { finish = resolve }))
  const pending = store[action]('old-task')
  fetch.mockResolvedValue(response({ username: 'second', runs: [], tasks: [], groups: [] }))
  await store.login('second', 'password')
  finish(response({ run_id: 'old-run', status: 'READY' }))
  await pending
  expect(store.getState().tabs).toEqual([])
  expect(store.getState().runs).toEqual({})
  expect(store.getState().notice).toBeNull()
})
