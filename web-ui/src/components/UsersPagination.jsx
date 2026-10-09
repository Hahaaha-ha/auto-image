import { useEffect, useId, useState } from 'react'
import { USER_PAGE_SIZES } from '../userList.js'

export default function UsersPagination({ page, pageCount, pageSize, onPageChange, onPageSizeChange, loading }) {
  const [jumpDraft, setJumpDraft] = useState('')
  const jumpId = useId()
  useEffect(() => { setJumpDraft('') }, [page, pageCount, pageSize])
  const jumpToPage = event => {
    event.preventDefault()
    const target = Number(jumpDraft)
    if (loading || !event.currentTarget.checkValidity() || !Number.isSafeInteger(target)
        || target < 1 || target > pageCount) return
    onPageChange(target)
    setJumpDraft('')
  }
  return <div className="va-users-pagination" aria-busy={loading}>
    <nav aria-label="用户清单分页">
      <label className="va-page-size">
        <span className="va-users-sr-only">每页条数</span>
        <select aria-label="每页条数" title="每页条数" value={pageSize} onChange={event => onPageSizeChange(Number(event.target.value))} disabled={loading}>
          {USER_PAGE_SIZES.map(size => <option key={size} value={size}>{size}</option>)}
        </select>
      </label>
      <div className="va-page-buttons va-page-previous">
        <button type="button" aria-label="首页" title="首页" disabled={loading || page === 1} onClick={() => onPageChange(1)}>
          <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false"><path d="M3 3v8M10 3 6 7l4 4" /></svg>
        </button>
        <button type="button" aria-label="上一页" title="上一页" disabled={loading || page === 1} onClick={() => onPageChange(page - 1)}>
          <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false"><path d="m9 3-4 4 4 4" /></svg>
        </button>
      </div>
      <p className="va-page-position" role="status" aria-label="当前页码" title={`第 ${page} 页，共 ${pageCount} 页`}>
        <span className="va-users-sr-only">第 </span><strong>{page}</strong>
        <span className="va-users-sr-only"> 页，共 </span><span className="va-page-total"><span aria-hidden="true">/ </span>{pageCount}</span><span className="va-users-sr-only"> 页</span>
      </p>
      <div className="va-page-buttons va-page-next">
        <button type="button" aria-label="下一页" title="下一页" disabled={loading || page === pageCount} onClick={() => onPageChange(page + 1)}>
          <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false"><path d="m5 3 4 4-4 4" /></svg>
        </button>
        <button type="button" aria-label="尾页" title="尾页" disabled={loading || page === pageCount} onClick={() => onPageChange(pageCount)}>
          <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false"><path d="M11 3v8M4 3l4 4-4 4" /></svg>
        </button>
      </div>
      <form className="va-page-jump" onSubmit={jumpToPage}>
        <label htmlFor={jumpId}>跳转到</label>
        <input id={jumpId} type="number" min={1} max={pageCount} step={1} required
          placeholder="页码" aria-label="跳转页码" title="输入页码后按Enter跳转"
          value={jumpDraft} onChange={event => setJumpDraft(event.target.value)} disabled={loading} />
        <span>页</span>
      </form>
    </nav>
  </div>
}
