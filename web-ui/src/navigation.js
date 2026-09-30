import { useSyncExternalStore } from 'react'

// hash 地址由静态首页承载，刷新与收藏不依赖服务端路由回退。
export const WORKBENCH_HREF = '#/'
export const USERS_HREF = '#/users'
const currentPage = () => window.location.hash === USERS_HREF ? 'users' : 'workbench'
const subscribe = (notify) => {
  window.addEventListener('hashchange', notify)
  return () => window.removeEventListener('hashchange', notify)
}
export const usePage = () => useSyncExternalStore(subscribe, currentPage, () => 'workbench')
