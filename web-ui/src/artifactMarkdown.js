import { parse } from 'yaml'

// 文件头只在产物预览中拆分，正文仍交给通用 Markdown 渲染器。
export function parseArtifactMarkdown(text) {
  const original = { metadata: null, body: text }
  const header = /^\uFEFF?---\r?\n([\s\S]*?)\r?\n---(?:\r?\n|$)/.exec(text)
  if (!header) return original
  try {
    const metadata = parse(header[1])
    if (!metadata || typeof metadata !== 'object' || Array.isArray(metadata)) return original
    return { metadata, body: text.slice(header[0].length) }
  } catch {
    return original
  }
}
