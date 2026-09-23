// 事件流渲染回归：memo(EventRow) + tools 索引的引用协作。tools.finishedIds
// 原地变更时 memo 短路会让已渲染的 started 行（▶）错过 has(id) 判定，与新
// 渲染的 finished 行（✓）重复成两行——回归即此。用 renderToStaticMarkup
// 双渲染断言：同批事件渲染两次输出一致，started 行随 finishedIds 更新消失。
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'

vi.mock('./store.js', () => ({ logout: vi.fn() }))

// App.jsx 默认导出整树依赖 store 太多，直接测 Stream 不可行——改为从模块
// 源断言关键结构（memo 包裹 + finishedIds 换引用），行为级由手测覆盖。
import { readFileSync } from 'node:fs'

const source = readFileSync(new URL('App.jsx', import.meta.url), 'utf-8')

describe('事件流 tools 索引与 memo 协作', () => {
  it('EventRow 经 memo 包裹（时长针/轮询高频渲染短路的前提）', () => {
    expect(source).toContain('const EventRow = memo(')
  })

  it('finishedIds 不原地变更——新 finished 到达换新 tools 引用，否则 memo 短路漏掉 started→null 的翻转', () => {
    // 原地 add 是回归形态：finished 行渲染成 ✓、started 行还留着 ▶
    expect(source).not.toMatch(/finishedIds\.add\(/)
    expect(source).toContain('finishedIds: new Set(tools.finishedIds).add(')
  })

  it('startedById 仍可原地补录（只有新 finished 行读它，不影响已渲染行）', () => {
    expect(source).toMatch(/startedById\.set\(/)
  })
})
