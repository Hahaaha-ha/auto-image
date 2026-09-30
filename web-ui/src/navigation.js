import { useSyncExternalStore } from 'react'

// hash 地址由静态首页承载，刷新与收藏不依赖服务端路由回退。
export const WORKBENCH_HREF = '#/'
export const USERS_HREF = '#/users'
const pageAt = (hash) => hash === USERS_HREF ? 'users' : 'workbench'
const listeners = new Set()
let accepted, restoring = false, guard = null
const historyKey = 'autoImageNavigation'
const entryPosition = () => window.navigation?.currentEntry?.index
const newChain = () => ({ chain: `${Date.now()}-${Math.random()}`, index: 0 })
const sameChain = (a, b) => a && b && a.chain === b.chain
const readEntry = () => {
  const entry = window.history.state?.[historyKey]
  return typeof entry?.chain === 'string' && Number.isInteger(entry.index) ? entry : null
}
const publish = () => { for (const notify of listeners) notify() }

export const mayLeavePage = () => !guard || guard()
export function setNavigationGuard(callback) {
  guard = callback
  return () => { if (guard === callback) guard = null }
}

function startNavigation() {
  if (accepted) return
  accepted = { hash: window.location.hash, entry: readEntry() || newChain(), position: entryPosition() }
  window.history.replaceState({ ...window.history.state, [historyKey]: accepted.entry }, '')
  document.addEventListener('click', (event) => {
    const anchor = event.target.closest?.('a[href]')
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey
      || !anchor || anchor.target || anchor.hasAttribute('download')) return
    const url = new URL(anchor.href)
    if (url.origin !== window.location.origin || url.pathname !== window.location.pathname || url.search !== window.location.search) return
    if (![WORKBENCH_HREF, USERS_HREF].includes(url.hash)) return
    event.preventDefault()
    if (restoring || pageAt(url.hash) === pageAt(accepted.hash)) return
    if (!mayLeavePage()) { event.stopImmediatePropagation(); return }
    const entry = accepted.entry ? { ...accepted.entry, index: accepted.entry.index + 1 } : newChain()
    window.history.pushState({ [historyKey]: entry }, '', url)
    accepted = { hash: url.hash, entry, position: entryPosition() }
    publish()
  }, true)
  const onHistory = () => {
    const hash = window.location.hash, entry = readEntry(), position = entryPosition()
    const sameEntry = sameChain(entry, accepted.entry) && entry.index === accepted.entry.index
    if (restoring) {
      if (hash === accepted.hash && (sameEntry || position === accepted.position)) restoring = false
      return
    }
    if (hash === accepted.hash && sameEntry) return
    const hasPosition = Number.isInteger(position) && Number.isInteger(accepted.position)
    const delta = hasPosition ? accepted.position - position
      : sameChain(entry, accepted.entry) ? accepted.entry.index - entry.index : null
    if (pageAt(hash) !== pageAt(accepted.hash) && !mayLeavePage()) {
      if (delta) {
        restoring = true
        window.history.go(delta)
      } else {
        // 无法确定旧历史方向时保留当前页，并开始新序列，避免伪造遍历距离。
        accepted = { ...accepted, entry: newChain(), position }
        window.history.replaceState({ ...window.history.state, [historyKey]: accepted.entry }, '', accepted.hash || WORKBENCH_HREF)
      }
      return
    }
    // 未标记的旧条目保持未知；只有应用自己的连续 push 才能推算距离。
    accepted = { hash, entry, position }
    publish()
  }
  window.addEventListener('popstate', onHistory)
  window.addEventListener('hashchange', onHistory)
}
const subscribe = (notify) => {
  startNavigation()
  listeners.add(notify)
  return () => listeners.delete(notify)
}
export const usePage = () => useSyncExternalStore(subscribe,
  () => pageAt(accepted?.hash ?? window.location.hash), () => 'workbench')
