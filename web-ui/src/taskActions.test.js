import { afterEach, beforeEach, expect, it, vi } from 'vitest'

let store
const response = (data, status = 200) => ({ ok: status < 400, status, json: async () => data, text: async () => '' })
const deferred = () => {
  let resolve
  const promise = new Promise(done => { resolve = done })
  return { promise, resolve }
}
const spec = { software: 'Node.js', version: '20', task_type: 'image', ecs_mode: 'create',
  ecs_params: { disk_size: 80 }, install_doc: 'https://example.test/install', extra_instructions: '启用 TLS\n验证 /health' }
const task = (overrides = {}) => ({ task_id: 't1', type: 'image', status: 'INIT',
  software: 'nodejs', version: '20.18.1', created_at: 1, updated_at: 2, spec, ...overrides })

beforeEach(async () => {
  vi.resetModules()
  vi.useFakeTimers()
  vi.stubGlobal('localStorage', { getItem: () => null, setItem: vi.fn() })
  vi.stubGlobal('fetch', vi.fn(async () => response({}, 401)))
  store = await import('./store.js')
  await store.initAuth()
  fetch.mockReset().mockImplementation(async () => response({ tasks: [] }))
})
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals() })

it('详情保留原始表单与补充要求，不能以执行中确认值回填', async () => {
  fetch.mockResolvedValue(response({ tasks: [task()] }))
  const [saved] = await store.refreshTasks()
  expect(saved.updatedAt).toBe(2)
  expect(saved.software).toBe('nodejs')
  expect(saved.spec).toEqual({ software: 'Node.js', version: '20', taskType: 'image',
    ecsMode: 'create', ecsInstance: undefined, ecsParams: { disk_size: 80 },
    installDoc: spec.install_doc, extraInstructions: spec.extra_instructions })
})

it('预览仅调用生成接口，不改变任务或创建会话', async () => {
  fetch.mockResolvedValue(response({ prompt: '执行指令\n\n补充要求：\n启用 TLS\n验证 /health' }))
  expect(await store.previewTask(spec)).toContain(spec.extra_instructions)
  expect(fetch).toHaveBeenCalledTimes(1)
  expect(fetch.mock.calls[0][0]).toBe('/api/tasks/preview')
  expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual(spec)
  expect(store.getState().tasks).toEqual([])
  expect(store.getState().tabs).toEqual([])
})

it('创建成功立即回填详情，较早的清单响应不能让新任务消失', async () => {
  const oldList = deferred()
  fetch.mockReturnValueOnce(oldList.promise)
  const pending = store.refreshTasks()
  fetch.mockResolvedValueOnce(response(task()))
  const saved = await store.createTask(spec)
  expect(saved.taskId).toBe('t1')
  oldList.resolve(response({ tasks: [] }))
  await pending
  expect(store.getState().tasks).toEqual([saved])
})

it('完整表单和查看时的更新时间一同保存，采用服务端规范化返回', async () => {
  fetch.mockResolvedValue(response(task({ updated_at: 3 })))
  const saved = await store.updateTask('t1', spec, 2)
  expect(fetch.mock.calls[0][0]).toBe('/api/tasks/t1')
  expect(fetch.mock.calls[0][1].method).toBe('PUT')
  expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ spec, expected_updated_at: 2 })
  expect(saved.updatedAt).toBe(3)
  expect(store.getState().tasks).toEqual([saved])
})

it('编辑冲突保留错误给表单，并刷新其他用户已经保存的任务', async () => {
  fetch.mockResolvedValueOnce(response({ detail: 'task_changed' }, 409))
    .mockResolvedValueOnce(response({ tasks: [task({ updated_at: 4 })] }))
  await expect(store.updateTask('t1', spec, 2)).rejects.toMatchObject({ detail: 'task_changed' })
  expect(store.getState().tasks[0].updatedAt).toBe(4)
  expect(fetch).toHaveBeenCalledTimes(2)
})

it('启动满额时返回可显示的任务错误，不产生未处理拒绝或成功提示', async () => {
  fetch.mockResolvedValueOnce(response({ detail: 'parallel_limit_reached' }, 409))
    .mockResolvedValueOnce(response({ tasks: [task()] }))
  await expect(store.runTask('t1', 2)).resolves.toBeNull()
  expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ expected_updated_at: 2 })
  expect(store.getState().taskActions.t1).toEqual({ busy: false, error: expect.stringContaining('并发上限') })
  expect(store.getState().notice).toBeNull()
  expect(store.getState().tabs).toEqual([])
})

it('列表和详情重复启动共用进行中状态，只发一次请求并打开一次会话', async () => {
  const started = deferred()
  fetch.mockReturnValueOnce(started.promise)
  const first = store.runTask('t1', 2)
  expect(store.getState().taskActions.t1.busy).toBe(true)
  expect(await store.runTask('t1', 2)).toBeNull()
  started.resolve(response({ task_id: 't1', run_id: 'r1', status: 'RUNNING' }))
  await first
  expect(fetch.mock.calls.filter(([url]) => url === '/api/tasks/t1/run')).toHaveLength(1)
  expect(store.getState().tabs).toEqual([{ kind: 'session', runId: 'r1' }])
  expect(store.getState().taskActions.t1).toEqual({ busy: false, error: null })
})

it('前次失败后的清单刷新不能清掉下一次启动的忙碌状态', async () => {
  const oldRefresh = deferred()
  const retryResponse = deferred()
  fetch.mockResolvedValueOnce(response({ detail: 'task_changed' }, 409))
    .mockReturnValueOnce(oldRefresh.promise)
    .mockReturnValueOnce(retryResponse.promise)
  const first = store.runTask('t1', 2)
  await vi.waitFor(() => expect(fetch).toHaveBeenCalledTimes(2))
  const retry = store.runTask('t1', 3)
  expect(store.getState().taskActions.t1.busy).toBe(true)
  oldRefresh.resolve(response({ tasks: [task({ updated_at: 3 })] }))
  await first
  expect(store.getState().taskActions.t1.busy).toBe(true)
  expect(await store.runTask('t1', 3)).toBeNull()
  retryResponse.resolve(response({ task_id: 't1', run_id: 'r1', status: 'RUNNING' }))
  await retry
  expect(fetch.mock.calls.filter(([url]) => url === '/api/tasks/t1/run')).toHaveLength(2)
  expect(store.getState().taskActions.t1.busy).toBe(false)
})
