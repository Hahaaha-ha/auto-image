// store 草稿规则钉子：输入框是受控输入（value=draftOf(runId)），草稿必须
// 驱动重渲染——否则任何外来渲染（摘要轮询、SSE 事件、时长针）都会用旧
// value 把 DOM 里的字冲掉，表现为「输入延迟/丢字、backspace 光标跳尾」。
// 断言订阅者视角的外部可见行为：setDraft 后通知到达、快照含新值、外来
// set 不冲掉草稿。
import { afterEach, describe, expect, it, vi } from 'vitest'

// store 模块级副作用重：SSE EventSource、轮询 setInterval、loadRuns——
// 全部 stub 掉，模块隔离成纯状态容器
vi.mock('./store.js', async () => {
  const actual = await vi.importActual('./store.js')
  return actual
})

const sseListeners = {}
let globalSource = null
global.EventSource = class {
  constructor() {
    this.readyState = 0
    globalSource = this
  }
  addEventListener(type, fn) { (sseListeners[type] ??= []).push(fn) }
  onopen() {}
  onerror() {}
}
vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false })))
const timers = []
vi.stubGlobal('setInterval', (fn, ms) => { timers.push([fn, ms]); return timers.length })

const store = await import('./store.js')

afterEach(() => {
  store.clearDraft('r1')
})

describe('输入草稿', () => {
  it('setDraft 通知订阅者且快照可见（受控输入的 value 源）', () => {
    const seen = []
    const unsub = store.subscribe(() => seen.push(store.getState().drafts.r1))
    store.setDraft('r1', 'nginx 1.25')
    unsub()
    expect(seen).toContain('nginx 1.25')
    expect(store.getState().drafts.r1).toBe('nginx 1.25')
    expect(store.draftOf('r1')).toBe('nginx 1.25')
  })

  it('外来渲染（轮询/SSE 推进）不冲掉草稿——每次 set 后 draftOf 仍是最新值', () => {
    // 模拟外来事件流：外来 set 与用户输入交错
    store.setDraft('r1', 'a')
    //外来渲染等价物：任何不碰 drafts 的 set()
    store.getState() // 触发一次读
    store.setDraft('r1', 'ab')
    expect(store.draftOf('r1')).toBe('ab')
    store.clearDraft('r1')
    expect(store.draftOf('r1')).toBe('')
  })
})

describe('快照与全局流归并', () => {
  it('tail 先到仍收敛到有序事实，断线补齐游标取最大 seq', async () => {
    const sse = (...frames) => frames.map(([seq, type, payload]) => [
      `id: ${seq}`,
      `event: ${type}`,
      `data: ${JSON.stringify(payload)}`,
    ].join('\n')).join('\n\n')
    const response = (body) => ({ ok: true, text: async () => body })
    const deferredResponse = () => {
      let resolve
      const promise = new Promise((done) => { resolve = (body) => done(response(body)) })
      return { promise, resolve }
    }
    const broadcast = (seq, type, payload = {}) => {
      sseListeners[type][0]({
        data: JSON.stringify({ run_id: 'event-run', seq, ts: seq * 10, type, payload }),
      })
    }
    const initialReplay = deferredResponse()
    const queuedReplays = [initialReplay.promise]
    const snapshotRequests = []
    fetch.mockImplementation(async (url, options = {}) => {
      if (url === '/api/runs' && options.method === 'POST') {
        return {
          ok: true,
          json: async () => ({ run_id: 'event-run', status: 'READY', resumed_from: null }),
        }
      }
      if (url === '/api/runs/event-run/events') {
        snapshotRequests.push(options)
        return queuedReplays.shift() ?? response('')
      }
      return { ok: false }
    })

    await store.createRun()
    expect(snapshotRequests.at(-1).headers['Last-Event-ID']).toBe('0')
    broadcast(5, 'turn.started')
    initialReplay.resolve(sse(
      [1, 'session.started', { ts: 10 }],
      [2, 'stage.changed', { ts: 20, stage: 'VERIFY' }],
      [3, 'session.title_changed', { ts: 30, title: '验证 nginx' }],
      [4, 'turn.completed', { ts: 40, result: '上一回合完成' }],
      [5, 'turn.started', { ts: 50 }],
    ))

    await vi.waitFor(() => expect(store.getState().runs['event-run'].events).toHaveLength(5))
    const session = store.getState().runs['event-run']
    expect({
      seqs: session.events.map((item) => item.seq),
      status: session.status,
      stage: session.stage,
      title: session.title,
      result: session.result,
      lastEventAt: session.lastEventAt,
    }).toEqual({
      seqs: [1, 2, 3, 4, 5],
      status: 'RUNNING',
      stage: 'VERIFY',
      title: '验证 nginx',
      result: '上一回合完成',
      lastEventAt: 50_000,
    })

    const gapReplay = deferredResponse()
    queuedReplays.push(gapReplay.promise)
    globalSource.onopen()
    expect(snapshotRequests.at(-1).headers['Last-Event-ID']).toBe('5')
    broadcast(10, 'turn.started')
    broadcast(8, 'agent.message', { text: '实时先到' })
    gapReplay.resolve(sse(
      [6, 'user.message', { ts: 60, text: '继续' }],
      [7, 'stage.changed', { ts: 70, stage: 'ARCHIVE' }],
      [8, 'agent.message', { ts: 80, text: '实时先到' }],
      [9, 'turn.completed', { ts: 90, result: '补齐完成' }],
      [10, 'turn.started', { ts: 100 }],
    ))

    await vi.waitFor(() => expect(store.getState().runs['event-run'].events).toHaveLength(10))
    expect(store.getState().runs['event-run'].events.map((item) => item.seq)).toEqual([1, 2, 3, 4, 5, 6, 7, 8, 9, 10])

    queuedReplays.push(Promise.resolve(response('')))
    globalSource.onopen()
    expect(snapshotRequests.at(-1).headers['Last-Event-ID']).toBe('10')
  })
})

describe('任务面板联动', () => {
  const mockTasks = (tasks) => {
    fetch.mockImplementation(async (url) => {
      if (url === '/api/tasks') {
        return { ok: true, json: async () => ({ tasks }) }
      }
      return { ok: false }
    })
  }

  it('refreshTasks 映射服务端任务（snake→camel + usage 四键）', async () => {
    mockTasks([{
      task_id: 'task-20260914073000-ab12', run_id: 'r1', type: 'rpm',
      status: 'DONE', outcome: 'success', name: 'RPM redis 7.2 (202609140730)',
      software: 'redis', version: '7.2', confirmed: true,
      stages: [{ stage: 'GUIDE', started_at: 1, ended_at: 2 }, { stage: 'BUILD', started_at: 2, ended_at: 3 }],
      current_stage: null,
      usage: { input_tokens: 10, output_tokens: 5, cache_read_tokens: 100, cache_creation_input_tokens_unused: 0 },
      server_alias: 'redis-2026091407', instance_id: 'i-1',
      created_at: 1, ended_at: 3, turn_text: '制作 redis 7.2',
    }])
    await store.refreshTasks()
    const t = store.getState().tasks[0]
    expect(t.taskId).toBe('task-20260914073000-ab12')
    expect(t.runId).toBe('r1')
    expect(t.currentStage).toBeNull()
    expect(t.stages[1]).toEqual({ stage: 'BUILD', startedAt: 2, endedAt: 3 })
    expect(t.usage.inputTokens).toBe(10)
    expect(t.usage.outputTokens).toBe(5)
    expect(t.usage.cacheReadTokens).toBe(100)
  })

  it('openTask 切任务面板并高亮；选择器按 run/instance 联查（instance_id 精确 + ecs-别名兜底）', async () => {
    mockTasks([
      { task_id: 'task-run', run_id: 'r1', type: 'image', status: 'RUNNING', outcome: null,
        name: '镜像 nginx (…)', software: 'nginx', version: null, confirmed: false,
        stages: [], current_stage: 'INSTALL', usage: null,
        server_alias: 'nginx-2026091407', instance_id: 'i-1',
        created_at: 1, ended_at: null, turn_text: '' },
      { task_id: 'task-done', run_id: 'r2', type: 'rpm', status: 'DONE', outcome: 'success',
        name: 'RPM redis (…)', software: 'redis', version: '7.2', confirmed: true,
        stages: [], current_stage: null, usage: null,
        server_alias: 'redis-x', instance_id: 'i-2',
        created_at: 2, ended_at: 3, turn_text: '' },
    ])
    await store.refreshTasks()
    store.openTask('task-run')
    expect(store.getState().sidePanel).toBe('tasks')
    expect(store.getState().activeTaskId).toBe('task-run')
    expect(store.runningTaskByRun('r1')?.taskId).toBe('task-run')
    expect(store.runningTaskByRun('r2')).toBeNull() // 已完成不占运行态
    expect(store.runningTaskByInstance({ id: 'i-1', name: '随便' })?.taskId).toBe('task-run')
    expect(store.runningTaskByInstance({ id: 'zzz', name: 'ecs-nginx-2026091407' })?.taskId).toBe('task-run')
    expect(store.runningTaskByInstance({ id: 'zzz', name: '别的机器' })).toBeNull()
    store.setSidePanel('sessions')
    expect(store.getState().sidePanel).toBe('sessions')
  })
})
