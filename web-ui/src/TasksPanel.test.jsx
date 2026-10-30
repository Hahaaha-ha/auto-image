import { renderToStaticMarkup } from 'react-dom/server'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const { state } = vi.hoisted(() => ({ state: { tasks: [], runs: {}, taskActions: {} } }))
vi.mock('./store.js', () => ({ useRunState: () => state }))
import TasksPanel from './components/TasksPanel.jsx'

const task = (overrides = {}) => ({
  taskId: 'task-1', name: '镜像 nginx 1.2', type: 'image', status: 'RUNNING',
  runId: 'run-1', currentStage: 'GUIDE', createdAt: 100,
  stages: [
    { stage: 'GUIDE', startedAt: 100, endedAt: 101 },
    { stage: 'INSTALL', startedAt: 101, endedAt: 102 },
    { stage: 'GUIDE', startedAt: 102, endedAt: null },
  ], ...overrides,
})

beforeEach(() => { state.tasks = []; state.runs = {}; state.taskActions = {} })

describe('任务列表的执行事实', () => {
  it('回炉时当前指南仍显示执行中，不被历史安装覆盖', () => {
    state.tasks = [task()]
    const html = renderToStaticMarkup(<TasksPanel />)
    expect(html).toMatch(/class="va-flow-pill s-guide current"/)
    expect(html).not.toContain('✓ 生成指南')
  })

  it.each(['stopped', 'failed', 'interrupted'])('安装中%s不会把最后阶段勾成完成', (outcome) => {
    state.tasks = [task({ status: 'DONE', outcome, currentStage: null, stages: [
      { stage: 'GUIDE', startedAt: 100, endedAt: 101 },
      { stage: 'INSTALL', startedAt: 101, endedAt: 102 },
    ] })]
    const html = renderToStaticMarkup(<TasksPanel />)
    expect(html).not.toContain('✓ 远程安装')
    expect(html).toContain('✓ 生成指南')
  })

  it('正常收尾仅标已完成，不声称部署成功', () => {
    state.tasks = [task({ status: 'DONE', outcome: 'success', currentStage: null })]
    const html = renderToStaticMarkup(<TasksPanel />)
    expect(html).toContain('已完成')
    expect(html).not.toContain('成功')
  })

  it('全局任务可查看，只有可访问的关联会话才有跳转按钮', () => {
    state.tasks = [task()]
    let html = renderToStaticMarkup(<TasksPanel />)
    expect(html).toContain('查看任务 镜像 nginx 1.2')
    expect(html).not.toContain('打开关联会话')
    state.runs['run-1'] = { runId: 'run-1' }
    html = renderToStaticMarkup(<TasksPanel />)
    expect(html).toContain('打开关联会话')
  })

  it('启动失败显示在对应任务行', () => {
    state.tasks = [task({ status: 'INIT' })]
    state.taskActions['task-1'] = { busy: false, error: '执行中回合已达并发上限' }
    expect(renderToStaticMarkup(<TasksPanel />)).toContain('执行中回合已达并发上限')
  })

  it.each(['RUNNING', 'DONE'])('任务已%s时不再显示待运行期间的旧启动错误', (status) => {
    state.tasks = [task({ status })]
    state.taskActions['task-1'] = { busy: false, error: '任务已被其他用户修改' }
    expect(renderToStaticMarkup(<TasksPanel />)).not.toContain('任务已被其他用户修改')
  })
})
