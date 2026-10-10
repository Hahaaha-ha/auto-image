import DOMPurify from 'dompurify'
import { marked } from 'marked'

// markdown → 消毒后 HTML 的单点：产物与消息流共用（内容都系 agent 转述
// 外部文档/工具输出，同威胁模型，HTML 一律消毒再进 DOM）。
// 结果按原文缓存：事件对象跨渲染引用稳定，但流上任意渲染（1s 时长针 /
// 5s 摘要轮询 / SSE 追加）都会重跑全部行，重复解析同一文本纯浪费
const htmlCache = new Map()
const CACHE_LIMIT = 500

export function mdToHtml(text) {
  const cached = htmlCache.get(text)
  if (cached !== undefined) return cached
  const html = DOMPurify.sanitize(marked.parse(text, { async: false }))
  if (htmlCache.size >= CACHE_LIMIT) htmlCache.delete(htmlCache.keys().next().value)
  htmlCache.set(text, html)
  return html
}
