import { useLayoutEffect, useRef } from 'react'

// 共享焦点范围、背景隔离与焦点归还；关闭规则由具体操作决定。
export function useModal(closable, onClose, initialFocusRef, fallbackFocusRef, returnFocusRef) {
  const panelRef = useRef(null)
  const liveRef = useRef(null)
  liveRef.current = { closable, onClose }
  useLayoutEffect(() => {
    const panel = panelRef.current
    const restore = returnFocusRef?.current || document.activeElement
    const background = []
    for (let node = panel; node?.parentElement; node = node.parentElement) {
      for (const sibling of node.parentElement.children) {
        if (sibling !== node && !sibling.contains(panel)) {
          background.push([sibling, sibling.inert])
          sibling.inert = true
        }
      }
      if (node.parentElement === document.body) break
    }
    const focusPanel = () => (initialFocusRef?.current || panel)?.focus()
    focusPanel()
    const onFocus = (event) => { if (!panel.contains(event.target)) panel.focus() }
    const onKey = (event) => {
      if (event.key === 'Escape') {
        event.preventDefault()
        event.stopPropagation()
        if (liveRef.current.closable()) liveRef.current.onClose()
      }
      if (event.key === 'Tab') {
        const items = Array.from(panel.querySelectorAll('a[href], button, input, select, textarea, [tabindex]'))
          .filter(el => !el.disabled && el.tabIndex >= 0 && el.getClientRects().length && !el.closest('[inert]'))
        const index = items.indexOf(document.activeElement)
        if (!items.length || index === -1 || (event.shiftKey ? index === 0 : index === items.length - 1)) {
          event.preventDefault()
          ;(event.shiftKey ? items.at(-1) : items[0])?.focus()
          if (!items.length) panel.focus()
        }
      }
    }
    document.addEventListener('keydown', onKey, true)
    document.addEventListener('focusin', onFocus)
    return () => {
      document.removeEventListener('keydown', onKey, true)
      document.removeEventListener('focusin', onFocus)
      for (const [node, inert] of background) node.inert = inert
      // 关闭提交完成后再判断触发钮，避免读取到同次渲染尚未解除的 disabled。
      queueMicrotask(() => {
        if (document.querySelector('[aria-modal="true"]')) return
        const available = el => el?.isConnected && !el.disabled && !el.closest('[inert]') && el.getClientRects().length
        if (available(restore)) restore.focus()
        else if (available(fallbackFocusRef?.current)) fallbackFocusRef.current.focus()
      })
    }
  }, [])
  return panelRef
}
