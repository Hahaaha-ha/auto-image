// 模态对话框共享行为：Esc 关闭（请求在飞时拦下）+ 打开时聚焦面板 +
// 关闭后焦点归还触发钮。三个对话框（任务/OBS 配置/ECS 新建）同用。
import { useEffect, useRef } from 'react'

export function useModal(closable, onClose) {
  const panelRef = useRef(null)
  const restoreRef = useRef(null)
  // 挂卸只随对话框开关发生一次，closable/onClose 每渲染都是新闭包——
  // 存进 ref 让 keydown 总调最新版，否则挂载初版（在飞=false）永远生效，
  // 请求中按 Esc 会误关（ECS 密码正是一次性凭证）
  const liveRef = useRef(null)
  liveRef.current = { closable, onClose }
  useEffect(() => {
    restoreRef.current = document.activeElement
    panelRef.current?.focus()
    const onKey = (e) => {
      if (e.key === 'Escape' && liveRef.current.closable()) liveRef.current.onClose()
    }
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('keydown', onKey)
      restoreRef.current?.focus?.()
    }
  }, [])
  return panelRef
}
