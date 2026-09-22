// OBS 面板共享性测试：面板对普通用户保留共享资源浏览与配置入口；
// 配置对话框的写面（改表单/保存钮）按 can_write 门控，普通用户只读提示。
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

describe('OBS 面板（共享资源）', () => {
  it('普通用户面板保留共享清单浏览与配置入口（不按 owner 隐藏）', () => {
    const html = renderToStaticMarkup(<ObsPanel openKeys={new Set()} activeKey={null} />)

    expect(html).toContain('OBS 产物 · 2')
    expect(html).toContain('test-image-gen')
    expect(html).toContain('⚙ 配置')
    expect(html).toContain('result.md')
  })
})

describe('OBS 配置写权限门控', () => {
  const source = readFileSync(new URL('components/ObsPanel.jsx', import.meta.url), 'utf-8')

  it('改表单与保存钮都在 canWrite 门内（非管理员无写入口）', () => {
    expect(source).toContain('canWrite && (')
    // 门控覆盖字段区与保存钮两处
    const gated = source.match(/\{canWrite && \(/g) ?? []
    expect(gated.length).toBe(2)
  })

  it('can_write 来自服务端视图，普通用户有只读说明文案', () => {
    expect(source).toContain("cfg?.can_write === true")
    expect(source).toContain('仅限管理员')
  })
})
