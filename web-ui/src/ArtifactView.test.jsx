import { renderToStaticMarkup } from 'react-dom/server'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const { sanitize } = vi.hoisted(() => ({
  sanitize: vi.fn((html) => html.replace(/ onerror="[^"]*"/g, '')),
}))
vi.mock('dompurify', () => ({ default: { sanitize } }))

import ArtifactView from './components/ArtifactView.jsx'

const rpm = {
  binary: true,
  dir: 'rpm/nginx/1.25.3/rpms/binary',
  name: 'nginx-1.25.3-1.aarch64.rpm',
  size: 12 * 1024 * 1024,
  stage: 'BUILD',
}
const onDownload = () => {}
const summaryBody = '# anything-llm 1.12.0 部署总结\n\n## 对象与来源\n\n正文内容。'
const summaryContent = `---
software: anything-llm
version: 1.12.0
project_url: https://github.com/Mintplex-Labs/anything-llm
os: ubuntu
os_version: 24.04.4 LTS (noble)
architecture: amd64
installation_method: official docker image
  (mintplexlabs/anythingllm:1.12.0), single-container docker run
updated_at: '2026-10-27T15:48:02.028391+00:00'
---

${summaryBody}`
const summaryArtifact = {
  dir: 'deploy/anything-llm/1.12.0',
  name: 'anything-llm-summary.md',
  content: summaryContent,
}

beforeEach(() => {
  sanitize.mockClear()
})

describe('产物视图', () => {
  it('未加载时只显示加载占位', () => {
    const html = renderToStaticMarkup(
      <ArtifactView artifact={null} onDownload={onDownload} />,
    )

    expect(html).toContain('加载中…')
    expect(html).not.toContain('<button')
    expect(sanitize).not.toHaveBeenCalled()
  })

  it('无 content 的 RPM 显示不可预览占位和下载动作', () => {
    const render = () => renderToStaticMarkup(
      <ArtifactView artifact={rpm} onDownload={onDownload} />,
    )

    expect(render).not.toThrow()
    const html = render()
    expect(html).toContain(rpm.name)
    expect(html).toContain('12.0 MB')
    expect(html).toContain('不支持在线预览')
    expect(html).toContain('下载此文件')
    expect(html.match(/<button/g)).toHaveLength(2)
    expect(sanitize).not.toHaveBeenCalled()
  })

  it('二进制标记优先于 JSON 文件名', () => {
    const html = renderToStaticMarkup(
      <ArtifactView
        artifact={{ ...rpm, name: 'package.json' }}
        onDownload={onDownload}
      />,
    )

    expect(html).toContain('二进制产物')
    expect(html).not.toContain('va-artifact-raw')
    expect(sanitize).not.toHaveBeenCalled()
  })

  it('Markdown 经消毒后渲染并保留下载动作', () => {
    const html = renderToStaticMarkup(
      <ArtifactView
        artifact={{
          dir: 'deploy/nginx/1.25',
          name: 'nginx-install.md',
          stage: 'GUIDE',
          content: '**安全内容**\n\n<img src="x" onerror="alert(1)">',
        }}
        onDownload={onDownload}
      />,
    )

    expect(sanitize).toHaveBeenCalledOnce()
    expect(sanitize.mock.calls[0][0]).toContain('onerror=')
    expect(html).toContain('<strong>安全内容</strong>')
    expect(html).not.toContain('onerror=')
    expect(html).toContain('title="下载此文件"')
    expect(html).not.toContain('<details')
  })

  it('总结文件头显示为默认收起的文档信息，正文标题不混入元数据', () => {
    const html = renderToStaticMarkup(<ArtifactView artifact={summaryArtifact} onDownload={onDownload} />)

    expect(html).toContain('<details')
    expect(html).not.toMatch(/<details[^>]*\bopen(?:=|\s|>)/)
    expect(html).toContain('文档信息')
    expect(html).toContain('ubuntu 24.04.4 LTS (noble) · amd64')
    expect(html.match(/<dt>/g)).toHaveLength(8)
    expect(html).toContain('<dt>版本</dt><dd>1.12.0</dd>')
    expect(html).toContain('<dt>项目地址</dt><dd><a href="https://github.com/Mintplex-Labs/anything-llm"')
    expect(html).toContain('official docker image (mintplexlabs/anythingllm:1.12.0), single-container docker run')
    expect(html).toContain('<time dateTime="2026-10-27T15:48:02.028391+00:00">')
    expect(html).not.toContain('<h2>software:')
    expect(html).not.toContain('<hr')
    expect(sanitize).toHaveBeenCalledOnce()
    expect(sanitize.mock.calls[0][0]).toMatch(/^<h1>anything-llm 1.12.0 部署总结<\/h1>/)
    expect(summaryArtifact.content).toBe(summaryContent)
  })

  it('省略没有记录的字段，OBS 使用同样的文档信息与原有链接动作', () => {
    const html = renderToStaticMarkup(<ArtifactView
      artifact={{ ...summaryArtifact, content: '---\nsoftware: anything-llm\nversion: 1.12.0\nos: null\narchitecture: ""\n---\n\n' + summaryBody }}
      onDownload={onDownload}
      downloadLabel="⧉ 复制链接"
    />)

    expect(html.match(/<dt>/g)).toHaveLength(2)
    expect(html).not.toContain('<dt>操作系统</dt>')
    expect(html).not.toContain('<dt>架构</dt>')
    expect(html).toContain('<summary>文档信息</summary>')
    expect(html).toContain('⧉ 复制链接')
    expect(html).toContain('<h1>anything-llm 1.12.0 部署总结</h1>')
  })

  it.each([
    '---\nsoftware: [unclosed\n---\n\n' + summaryBody,
    '---\nsoftware: anything-llm\n\n' + summaryBody,
    '---\n普通说明\n---\n\n' + summaryBody,
  ])('不能识别文件头时保留原有 Markdown 内容：%s', content => {
    const html = renderToStaticMarkup(<ArtifactView artifact={{ ...summaryArtifact, content }} onDownload={onDownload} />)

    expect(html).not.toContain('<details')
    expect(html).toContain('<hr')
    expect(html).toContain('<h1>anything-llm 1.12.0 部署总结</h1>')
  })

  it('文档信息按文本显示外部内容，项目链接沿用网页地址', () => {
    const html = renderToStaticMarkup(<ArtifactView
      artifact={{ ...summaryArtifact, content: '---\nsoftware: "<img src=x onerror=alert(1)>"\nproject_url: "javascript:alert(1)"\n---\n\n' + summaryBody }}
      onDownload={onDownload}
    />)

    expect(html).toContain('&lt;img src=x onerror=alert(1)&gt;')
    expect(html).not.toContain('<img')
    expect(html).not.toContain('href="javascript:')
  })

  it('JSON 保留原文且不进入 Markdown 消毒器', () => {
    const content = '{"z": 1,\n  "a": "<原文>"}'
    const html = renderToStaticMarkup(
      <ArtifactView
        artifact={{
          dir: 'deploy/nginx/1.25',
          name: 'nginx-install-meta.json',
          stage: 'INSTALL',
          content,
        }}
        onDownload={onDownload}
      />,
    )

    expect(html).toContain('{&quot;z&quot;: 1,\n  &quot;a&quot;: &quot;&lt;原文&gt;&quot;}')
    expect(html).toContain('title="下载此文件"')
    expect(sanitize).not.toHaveBeenCalled()
  })
})
