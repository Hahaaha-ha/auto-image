import { USER_PAGE_SIZES } from '../userList.js'

export default function UsersPagination({ page, pageCount, pageSize, onPageChange, onPageSizeChange, loading }) {
  return <div className="va-users-pagination" aria-busy={loading}>
    <nav aria-label="用户清单分页">
      <label className="va-page-size">
        <span className="va-users-sr-only">每页条数</span>
        <select aria-label="每页条数" title="每页条数" value={pageSize} onChange={event => onPageSizeChange(Number(event.target.value))} disabled={loading}>
          {USER_PAGE_SIZES.map(size => <option key={size} value={size}>{size}</option>)}
        </select>
      </label>
      <div className="va-page-buttons va-page-previous">
        <button type="button" aria-label="上一页" title="上一页" disabled={loading || page === 1} onClick={() => onPageChange(page - 1)}>
          <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false"><path d="m9 3-4 4 4 4" /></svg>
        </button>
      </div>
      <p className="va-page-position" role="status" aria-label="当前页码" title={`第 ${page} 页，共 ${pageCount} 页`}>
        <span className="va-users-sr-only">第 </span><strong>{page}</strong>
        <span className="va-users-sr-only"> 页，共 {pageCount} 页</span>
      </p>
      <div className="va-page-buttons va-page-next">
        <button type="button" aria-label="下一页" title="下一页" disabled={loading || page === pageCount} onClick={() => onPageChange(page + 1)}>
          <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false"><path d="m5 3 4 4-4 4" /></svg>
        </button>
      </div>
    </nav>
  </div>
}
