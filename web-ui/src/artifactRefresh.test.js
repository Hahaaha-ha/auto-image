import { afterEach, beforeEach, expect, it, vi } from 'vitest'

const rel = 'deploy/nginx/1.3.0/nginx-summary.md'
const response = data => ({ ok: true, status: 200, json: async () => data })
let store, content, fileReads, present
const listing = { groups: [{ dir: 'deploy/nginx/1.3.0', files: [{ name: 'nginx-summary.md', size: 4, stage: null }] }] }
const body = () => ({ dir: 'deploy/nginx/1.3.0', name: 'nginx-summary.md', stage: null, content })

beforeEach(async () => {
  vi.resetModules()
  vi.useFakeTimers()
  vi.stubGlobal('localStorage', { getItem: () => null, setItem: vi.fn() })
  vi.stubGlobal('EventSource', class { addEventListener() {} close() {} })
  content = '旧的总结'
  present = true
  fileReads = 0
  vi.stubGlobal('fetch', vi.fn(async url => {
    if (url === '/api/artifacts') return response(present ? listing : { groups: [] })
    if (url === `/api/artifacts/file/${rel}`) { fileReads++; return response(body()) }
    return response({ username: 'tester', runs: [], tasks: [] })
  }))
  store = await import('./store.js')
  await store.initAuth()
  await store.openArtifact(rel)
})
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals() })

it('同路径同字节长度覆盖后，定时刷新更新已打开正文且不出现加载空档', async () => {
  const observed = []
  const unsubscribe = store.subscribe(() => observed.push(store.getState().artifactCache[rel]?.content))
  content = '新的总结'
  await vi.advanceTimersByTimeAsync(5000)
  unsubscribe()
  expect(store.getState().artifactCache[rel].content).toBe('新的总结')
  expect(observed).not.toContain(undefined)
  expect(fileReads).toBeGreaterThan(1)
})

it('没有变化的正文在刷新后保持缓存引用，关闭后重开仍读取最新内容', async () => {
  const previous = store.getState().artifactCache[rel]
  await store.refreshArtifacts()
  expect(store.getState().artifactCache[rel]).toBe(previous)
  store.closeTab(`file:${rel}`)
  content = '关闭后覆盖的新总结'
  await store.openArtifact(rel)
  expect(store.getState().artifactCache[rel].content).toBe(content)
})

it('较早的正文读取晚返回时不会覆盖较新内容', async () => {
  let finishOld
  fetch.mockImplementationOnce(async () => response(listing))
    .mockImplementationOnce(() => new Promise(resolve => { finishOld = resolve }))
  const previousRefresh = store.refreshArtifacts()
  await vi.advanceTimersByTimeAsync(0)
  content = '最新总结'
  await store.refreshArtifacts()
  finishOld(response({ ...body(), content: '过期总结' }))
  await previousRefresh
  expect(store.getState().artifactCache[rel].content).toBe('最新总结')
})

it('阶段清理移除文件后不再显示旧成功正文，同路径重建后恢复最新预览', async () => {
  present = false
  await store.refreshArtifacts()
  expect(store.getState().artifactCache[rel]).toEqual({ missing: true })
  present = true
  content = '重新生成的当前结果'
  await store.refreshArtifacts()
  expect(store.getState().artifactCache[rel].content).toBe(content)
  expect(store.getState().artifactCache[rel].missing).toBeUndefined()
})

it('后台读取失败保留原正文，换账号后迟到的读取不能恢复旧缓存', async () => {
  fetch.mockImplementationOnce(async () => response(listing))
    .mockImplementationOnce(async () => ({ ok: false, status: 503, json: async () => ({ detail: 'temporarily unavailable' }) }))
  await store.refreshArtifacts()
  expect(store.getState().artifactCache[rel].content).toBe('旧的总结')
  let finishOld
  fetch.mockImplementationOnce(async () => response(listing))
    .mockImplementationOnce(() => new Promise(resolve => { finishOld = resolve }))
  const pending = store.refreshArtifacts()
  await vi.advanceTimersByTimeAsync(0)
  fetch.mockResolvedValue(response({ username: 'second', runs: [], tasks: [], groups: [] }))
  await store.login('second', 'password')
  finishOld(response({ ...body(), content: '旧身份内容' }))
  await pending
  expect(store.getState().artifactCache).toEqual({})
})
