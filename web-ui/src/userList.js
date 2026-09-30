export const USER_PAGE_SIZES = [10, 20, 50, 100]
export const emptyUsersQuery = () => ({ keyword: '', page: 1, pageSize: 20 })

export function filterAndSortUsers(items, keyword) {
  // 搜索忽略大小写，但不规范化账户原文；排序固定按字符串序，不受浏览器语言影响。
  const needle = keyword.toLowerCase()
  return items.filter(user => user.username.toLowerCase().includes(needle))
    .sort((a, b) => a.username < b.username ? -1 : a.username > b.username ? 1 : 0)
}

export function getUserListView(items, query) {
  const matching = filterAndSortUsers(items, query.keyword)
  const total = matching.length
  const pageCount = Math.max(1, Math.ceil(total / query.pageSize))
  const page = Math.min(Math.max(1, query.page), pageCount)
  const start = (page - 1) * query.pageSize
  return { items: matching.slice(start, start + query.pageSize), total, page, pageCount, start }
}
