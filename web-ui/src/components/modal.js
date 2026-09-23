// 模态对话框共享行为：Esc 关闭（请求在飞时拦下）+ 打开时聚焦面板 +
// 关闭后焦点归还触发钮。三个对话框（任务/OBS 配置/ECS 新建）同用。
import { useEffect, useRef } from 'react'

export function useModal(closable, onClose) {
  const panelRef = useRef(null)
  const restoreRef = useRef(null)
  useEffect(() => {
    restoreRef.current = document.activeElement
    panelRef.current?.focus()
    const onKey = (e) => {
      if (e.key === 'Escape' && closable()) onClose()
    }
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('keydown', onKey)
      restoreRef.current?.focus?.()
    }
    // closable/onClose 都是每次渲染的新闭包，语义由调用方保证稳定；
    // 挂卸只随对话框开关发生一次
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])
  return panelRef
}
