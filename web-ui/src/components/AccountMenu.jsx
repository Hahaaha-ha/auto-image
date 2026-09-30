import { useEffect, useId, useRef, useState } from 'react'
import * as store from '../store.js'
import { USERS_HREF } from '../navigation.js'

export default function AccountMenu({ visible }) {
  const { user, canManageUsers } = store.useRunState()
  const [open, setOpen] = useState(false)
  const rootRef = useRef(null)
  const buttonRef = useRef(null)
  const menuRef = useRef(null)
  const initialIndex = useRef(0)
  const id = useId()
  const close = () => {
    setOpen(false)
    buttonRef.current?.focus()
  }

  useEffect(() => { if (!visible) setOpen(false) }, [visible])
  useEffect(() => {
    if (!open) return
    const items = menuRef.current.querySelectorAll('[role="menuitem"]')
    items[initialIndex.current === -1 ? items.length - 1 : 0]?.focus()
    const onOutside = (event) => {
      if (!rootRef.current.contains(event.target)) close()
    }
    document.addEventListener('pointerdown', onOutside)
    return () => document.removeEventListener('pointerdown', onOutside)
  }, [open])

  return <div className="va-account" ref={rootRef} onBlur={(event) => {
    if (!event.currentTarget.contains(event.relatedTarget)) setOpen(false)
  }}>
    <button ref={buttonRef} className="va-account-button" aria-label={`账号菜单：${user}`}
      aria-haspopup="menu" aria-expanded={open} aria-controls={open ? id : undefined}
      onClick={() => { initialIndex.current = 0; setOpen(!open) }}
      onKeyDown={(event) => {
        if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
          event.preventDefault()
          initialIndex.current = event.key === 'ArrowUp' ? -1 : 0
          setOpen(true)
        }
      }}>
      <span>{user}</span>
      <svg width="14" height="14" viewBox="0 0 16 16" aria-hidden="true"><path d="m4 6 4 4 4-4" fill="none" stroke="currentColor" strokeWidth="1.5" /></svg>
    </button>
    {open && <div className="va-account-menu" id={id} ref={menuRef} role="menu" aria-label="账号操作"
      onKeyDown={(event) => {
        if (event.key === 'Escape') { event.preventDefault(); close(); return }
        const items = Array.from(menuRef.current.querySelectorAll('[role="menuitem"]'))
        const index = items.indexOf(document.activeElement)
        if (event.key === ' ' && index !== -1) { event.preventDefault(); items[index].click(); return }
        const next = { ArrowDown: (index + 1) % items.length, ArrowUp: (index - 1 + items.length) % items.length,
          Home: 0, End: items.length - 1 }[event.key]
        if (next !== undefined) { event.preventDefault(); items[next].focus() }
      }}>
      {canManageUsers === true && <a role="menuitem" tabIndex={-1} href={USERS_HREF} onClick={close}>用户管理</a>}
      <button role="menuitem" tabIndex={-1} onClick={() => { close(); store.logout() }}>登出</button>
    </div>}
  </div>
}
