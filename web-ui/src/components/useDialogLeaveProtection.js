import { useEffect, useRef } from 'react'
import { setNavigationGuard } from '../navigation.js'

export function useDialogLeaveProtection({ isBusy, dirty, onDiscard }) {
  const live = useRef(null)
  live.current = { isBusy, dirty, onDiscard }
  const requestClose = () => {
    const current = live.current
    if (current.isBusy()) return false
    if (current.dirty && !window.confirm('放弃尚未提交的输入？已填写的内容将被清空。')) return false
    current.onDiscard()
    return true
  }
  useEffect(() => {
    const removeGuard = setNavigationGuard(requestClose)
    const onUnload = (event) => {
      if (live.current.isBusy() || live.current.dirty) {
        event.preventDefault()
        event.returnValue = ''
      }
    }
    window.addEventListener('beforeunload', onUnload)
    return () => {
      removeGuard()
      window.removeEventListener('beforeunload', onUnload)
    }
  }, [])
  return requestClose
}
