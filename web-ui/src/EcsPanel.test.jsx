import { renderToStaticMarkup } from 'react-dom/server'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const fixture = vi.hoisted(() => ({ instances: [], task: null }))

vi.mock('./store.js', () => ({
  useRunState: () => ({
    ecs: { instances: fixture.instances, region: 'cn-north-4', error: null, loading: false },
    ecsCreating: false,
  }),
  runningTaskByInstance: () => fixture.task,
}))

import EcsPanel from './components/EcsPanel.jsx'

beforeEach(() => {
  fixture.instances = [{
    id: 'server-1', name: '临时机', status: 'ACTIVE', flavor: 'c7.large.2',
    ip: '203.0.113.10', ip_type: 'floating',
    created: '2026-10-01T02:03:04Z',
    auto_terminate_time: '2026-10-09T08:00:00Z',
  }]
  fixture.task = null
})

describe('ECS 计划删除时间', () => {
  it('在规格/IP和空闲信息之间显示北京时间，保留原始时间供机器读取', () => {
    const html = renderToStaticMarkup(<EcsPanel />)

    expect(html).toContain('计划删除：')
    expect(html).toMatch(/<time\b[^>]*dateTime="2026-10-09T08:00:00Z"[^>]*>2026-10-09 16:00<\/time>/)
    expect(html.indexOf('203.0.113.10')).toBeLessThan(html.indexOf('计划删除：'))
    expect(html.indexOf('计划删除：')).toBeLessThan(html.indexOf('空闲中'))
    expect(html).not.toContain('北京时间')
  })

  it.each([
    ['2026-10-09T15:59:59Z', '2026-10-09 23:59'],
    ['2026-12-31T16:00:00Z', '2027-01-01 00:00'],
    ['2028-02-29T23:45:59Z', '2028-03-01 07:45'],
  ])('按北京时间处理日期边界并显示到分钟（%s）', (value, expected) => {
    fixture.instances[0].auto_terminate_time = value
    const html = renderToStaticMarkup(<EcsPanel />)

    expect(html).toContain(`>${expected}</time>`)
  })

  it.each([undefined, null, ''])('未设置时隐藏整行（%j）', value => {
    fixture.instances[0].auto_terminate_time = value
    const html = renderToStaticMarkup(<EcsPanel />)

    expect(html).not.toContain('计划删除：')
    expect(html).not.toContain('<time')
    expect(html).toContain('空闲中')
  })

  it.each([
    '不是日期', '   ', '2026-10-09T08:00:00',
    '2026-02-30T08:00:00Z', '2026-02-29T08:00:00Z',
    '2026-13-01T08:00:00Z', '2026-10-09T24:00:00Z',
    0, false, {}, ['2026-10-09T08:00:00Z'],
  ].map(value => [value]))('非空但无法确定的删除时间显示未知（%j）', value => {
    fixture.instances[0].auto_terminate_time = value
    const html = renderToStaticMarkup(<EcsPanel />)

    expect(html).toContain('计划删除：时间未知')
    expect(html).not.toContain('<time')
    expect(html).toContain('临时机')
  })

  it('已过计划时刻仍在清单中时保留原时间，运行任务显示在其下方', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-10-10T00:00:00Z'))
    fixture.task = { taskId: 'task-1', name: '部署 nginx', software: 'nginx', currentStage: 'INSTALL' }
    try {
      const html = renderToStaticMarkup(<EcsPanel />)

      expect(html).toContain('计划删除：')
      expect(html).toContain('>2026-10-09 16:00</time>')
      expect(html).toContain('运行任务中：nginx')
      expect(html.indexOf('计划删除：')).toBeLessThan(html.indexOf('运行任务中：nginx'))
      expect(html).not.toContain('已删除')
      expect(html).not.toContain('倒计时')
    } finally {
      vi.useRealTimers()
    }
  })
})
