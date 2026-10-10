import { useLayoutEffect } from 'react'

export function useSubmissionErrorFocus({ busy, error, targetRef }) {
  useLayoutEffect(() => {
    // 提交会禁用控件并丢失焦点；等错误和可编辑控件一起渲染后再恢复。
    if (!busy && error) targetRef.current?.focus()
  }, [busy, error, targetRef])
}
