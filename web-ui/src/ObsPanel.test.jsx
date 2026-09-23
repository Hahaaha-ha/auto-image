// OBS 面板共享性与配置写权限测试：面板对普通用户保留共享资源浏览与
// 配置入口；写权限判定 canWriteOf fail-closed（缺键/非布尔一律只读），
// 普通用户文案与术语哨兵钉住。
import { readFileSync } from 'node:fs'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'

vi.mock('./store.js', () => ({
  useRunState: () => ({
    obs: {
      objects: [
        { key: 'deploy/nginx/result.md', size: 120, last_modified: '2026/09/09 10:00:00' },
        { key: 'readme.md', size: 12, last_modified: '' },
      ],
      bucket: 'test-image-gen',
      region: 'ap-southeast-1',
      domain: 'https://test-image-gen.obs.ap-southeast-1.myhuaweicloud.com',
      error: null,
      loading: false,
    },
  }),
  refreshObs: vi.fn(),
  openObsObject: vi.fn(),
  copyObsLink: vi.fn(),
}))

import ObsPanel from './components/ObsPanel.jsx'
import { canWriteOf } from './components/ObsPanel.jsx'

describe('OBS 面板（共享资源）', () => {
  it('普通用户面板保留共享清单浏览与配置入口（不按 owner 隐藏）', () => {
    const html = renderToStaticMarkup(<ObsPanel openKeys={new Set()} activeKey={null} />)

    expect(html).toContain('OBS 产物 · 2')
    expect(html).toContain('test-image-gen')
    expect(html).toContain('配置')
    expect(html).toContain('result.md')
  })
})

describe('OBS 配置写权限判定', () => {
  it('can_write 为 true 才可见写面；缺键/非布尔一律只读（fail closed）', () => {
    expect(canWriteOf({ can_write: true })).toBe(true)
    for (const cfg of [null, undefined, {}, { can_write: false },
      { can_write: 'true' }, { can_write: 1 }]) {
      expect(canWriteOf(cfg), `${JSON.stringify(cfg)} 应只读`).toBe(false)
    }
  })

  it('组件源码只经 canWriteOf 判定写权限（不旁路布尔转换）', () => {
    const source = readFileSync(new URL('components/ObsPanel.jsx', import.meta.url), 'utf-8')
    expect(source).toContain('const canWrite = canWriteOf(cfg)')
    expect(source).toContain('仅限部署管理员')  // 普通用户只读说明文案
  })
})
