// derive 纯模块规则钉子：累计执行时长的派生口径冻结在这里——各回合求和、
// 扣等待输入空档、终态与挂起同口径。UI 的「总计时间」押在这块地基上。
// 只断言外部可见行为（秒数、展示串），不断言内部实现。
import { describe, expect, it } from 'vitest'
import { activeSeconds, fmtActive, obsGroups, resumeMark, OBS_ROOT_DIR } from './derive'

// 事件构造：ts 并入 payload（快照与全局流两路落地后的统一形状）
const ev = (type, ts) => ({ seq: ts, type, payload: { ts } })
const run = (events, { status = 'READY', startedAt = 60_000, endedAt = null } = {}) => ({
  status, events, startedAt, endedAt,
})

describe('activeSeconds', () => {
  it('多回合求和、扣除等待输入空档', () => {
    // 回合一 30 秒，等待 200 秒，回合二 40 秒——总计 70 而非跨回合 wall-clock
    const r = run([
      ev('user.message', 100),
      ev('turn.completed', 130),
      ev('user.message', 330),
      ev('turn.completed', 370),
    ])
    expect(activeSeconds(r, 999_000)).toBe(70)
  })

  it('turn.stopped / turn.failed 同为回合收尾', () => {
    const stopped = run([ev('user.message', 100), ev('turn.stopped', 115)])
    const failed = run([ev('user.message', 100), ev('turn.failed', 128)])
    expect(activeSeconds(stopped, 999_000)).toBe(15)
    expect(activeSeconds(failed, 999_000)).toBe(28)
  })

  it('ENDED 与非终态同口径：无 wall-clock 兜底，空档同样扣除', () => {
    // 事件求和 30 秒；endedAt - startedAt 的 wall-clock 是 4700 秒——若兜底
    // 分支仍在，等待输入的整段空档会被计入
    const events = [ev('user.message', 100), ev('turn.completed', 130)]
    const ended = run(events, { status: 'ENDED', startedAt: 50_000, endedAt: 4_750_000 })
    const ready = run(events, { status: 'READY', startedAt: 50_000 })
    expect(activeSeconds(ended, 999_000)).toBe(30)
    expect(activeSeconds(ready, 999_000)).toBe(30)
  })

  it('interrupted 收口段如实计入（重启截断的回合定格，不随 now 增长）', () => {
    const r = run([
      ev('user.message', 100),
      ev('agent.message', 120),
      ev('turn.interrupted', 125),
    ])
    expect(activeSeconds(r, 999_000)).toBe(25)
    expect(activeSeconds(r, 999_900_000)).toBe(25)
  })

  it('执行中的回合以 now 收口', () => {
    const r = run([ev('user.message', 100), ev('agent.message', 120)], { status: 'RUNNING' })
    expect(activeSeconds(r, 150_000)).toBe(50)
  })

  it('终态未闭合的回合不计（异常边界：ENDED 不以 now 增长）', () => {
    const r = run([ev('user.message', 100)], { status: 'ENDED', startedAt: 100_000, endedAt: 400_000 })
    expect(activeSeconds(r, 999_000)).toBe(0)
  })
})

describe('fmtActive', () => {
  it('无 startedAt 显示占位 --:--', () => {
    expect(fmtActive(run([], { startedAt: null }), 999_000)).toBe('--:--')
  })
})

describe('resumeMark', () => {
  it('Fork 来源标记使用统一术语并说明源会话', () => {
    const source = { firstPrompt: '部署 nginx', events: [] }
    const fork = { resumedFrom: 'run_source' }

    expect(resumeMark(fork, { run_source: source })).toBe(
      " ⑂ Fork 自『部署 nginx』",
    )
  })
})

describe('obsGroups', () => {
  const obj = (key, size = 1, last_modified = '') => ({ key, size, last_modified })

  it('按 key 目录段分组，文件条目保留 key 全名（根级对象不能由组前缀拼回）', () => {
    const groups = obsGroups([
      obj('deploy/lobechat/1.143.3/install-result.md', 5659, '2026/09/09 15:35:07'),
      obj('readme.md', 12, '2026/09/09 10:00:00'),
    ])
    const byDir = Object.fromEntries(groups.map((g) => [g.dir, g]))
    expect(byDir['deploy/lobechat/1.143.3'].files[0].key).toBe('deploy/lobechat/1.143.3/install-result.md')
    expect(byDir[OBS_ROOT_DIR].files[0].key).toBe('readme.md')
    expect(byDir[OBS_ROOT_DIR].files[0].name).toBe('readme.md')
  })

  it('组间按组内最新 last_modified 降序，组内按对象名升序', () => {
    const groups = obsGroups([
      obj('old/x.md', 1, '2026/09/01 00:00:00'),
      obj('new/b.md', 1, '2026/09/09 00:00:00'),
      obj('new/a.md', 2, '2026/09/05 00:00:00'),
    ])
    expect(groups.map((g) => g.dir)).toEqual(['new', 'old'])
    expect(groups[0].files.map((f) => f.name)).toEqual(['a.md', 'b.md'])
  })

  it('无 key 的坏条目跳过，空清单/空入参返回空组', () => {
    expect(obsGroups([])).toEqual([])
    expect(obsGroups(null)).toEqual([])
    expect(obsGroups([{ size: 3 }, { key: '' }])).toEqual([])
  })
})

// 任务面板派生：token 格式与阶段顺序（阶段流转图的地基）
describe('任务派生', () => {
  it('fmtTokens：null 为 —，万级缩写，小数值原样', async () => {
    const { fmtTokens } = await import('./derive')
    expect(fmtTokens(null)).toBe('—')
    expect(fmtTokens(undefined)).toBe('—')
    expect(fmtTokens(0)).toBe('0')
    expect(fmtTokens(999)).toBe('999')
    expect(fmtTokens(12_345)).toBe('1.2万')
    expect(fmtTokens(234_500_000)).toBe('2.3亿')
  })

  it('stageOrder：镜像走 INSTALL，RPM 走 BUILD', async () => {
    const { stageOrder } = await import('./derive')
    expect(stageOrder('image')).toEqual(['GUIDE', 'INSTALL', 'VERIFY', 'ARCHIVE'])
    expect(stageOrder('rpm')).toEqual(['GUIDE', 'BUILD', 'VERIFY', 'ARCHIVE'])
  })
})
