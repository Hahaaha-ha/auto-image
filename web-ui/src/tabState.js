// 混合标签栏的开-关-激活-控制面决策规则：会话标签页与产物文件、OBS 对象
// 标签页同栏混排（复合 key 寻址 session:<runId> / file:<relPath> / obs:<key>），
// 全部判定为纯函数——状态容器仍归 store，这里不渲染、不碰网络、不碰 localStorage。
// 语义冻结自布局冻结案与原型验证过的内核；本文件被 vitest 单测
// 逐条钉住，改动先过测试。
//
// 标签页对象形状：
//   { kind: 'session', runId }
//   { kind: 'file', relPath, name }
//   { kind: 'obs', key, name }

// 标签页 → 复合 key（寻址与判重）
export const tabKey = (t) =>
  t.kind === 'session'
    ? `session:${t.runId}`
    : t.kind === 'obs'
      ? `obs:${t.key}`
      : `file:${t.relPath}`

// 开会话标签页：已有只激活不重插；新则尾插（会话是持久对象，贴旧手感）
// 并激活
export function openSession(tabs, activeKey, runId) {
  const exists = tabs.some((t) => t.kind === 'session' && t.runId === runId)
  if (exists) return { tabs, activeKey: `session:${runId}` }
  return { tabs: [...tabs, { kind: 'session', runId }], activeKey: `session:${runId}` }
}

// 开文件标签页：已有只激活不重插；新则插当前激活标签页右侧（VSCode
// 次序，相关文件聚在一起）并激活。无激活标签页（tabs 非空但 activeKey
// 未命中）时尾插。
export function openFile(tabs, activeKey, relPath, name) {
  const exists = tabs.some((t) => t.kind === 'file' && t.relPath === relPath)
  if (exists) return { tabs, activeKey: `file:${relPath}` }
  const tab = { kind: 'file', relPath, name: name ?? relPath.slice(relPath.lastIndexOf('/') + 1) }
  const idx = tabs.findIndex((t) => tabKey(t) === activeKey)
  const next = [...tabs]
  next.splice(idx === -1 ? next.length : idx + 1, 0, tab)
  return { tabs: next, activeKey: `file:${relPath}` }
}

// 开 OBS 对象标签页：语义同 openFile（已有只激活；新则插当前激活标签页
// 右侧并激活）。key 即桶内对象全名（含目录段）。
export function openObs(tabs, activeKey, key, name) {
  const exists = tabs.some((t) => t.kind === 'obs' && t.key === key)
  if (exists) return { tabs, activeKey: `obs:${key}` }
  const tab = { kind: 'obs', key, name: name ?? key.slice(key.lastIndexOf('/') + 1) }
  const idx = tabs.findIndex((t) => tabKey(t) === activeKey)
  const next = [...tabs]
  next.splice(idx === -1 ? next.length : idx + 1, 0, tab)
  return { tabs: next, activeKey: `obs:${key}` }
}

// 关标签页：最后一枚会话标签页拦截（原状态返回）——控制面永远有对象。
// 关激活的非会话标签页（文件 / OBS 对象）→ 激活左邻；关激活的会话标签页 → 激活右邻否则
// 左邻；兜底激活首枚会话标签页（文件全关空后仍落回常驻会话）。
// 关非激活标签页时激活态不动。
export function closeTab(tabs, activeKey, key) {
  const idx = tabs.findIndex((t) => tabKey(t) === key)
  if (idx === -1) return { tabs, activeKey }
  const closing = tabs[idx]
  const sessionTabs = tabs.filter((t) => t.kind === 'session')
  if (closing.kind === 'session' && sessionTabs.length === 1) return { tabs, activeKey }
  const next = tabs.filter((t) => tabKey(t) !== key)
  if (activeKey !== key) return { tabs: next, activeKey }
  // 关闭后激活左邻（非会话）或右邻否则左邻（session），越界向内收；两邻
  // 皆无（关空，仅无会话标签页的病态输入可达）归 null
  const neighbor = closing.kind !== 'session' ? next[idx - 1] : next[idx]
  const fallback = next[idx - 1] ?? next[idx]
  const target = neighbor ?? fallback ?? next[0] ?? null
  return { tabs: next, activeKey: target ? tabKey(target) : null }
}

// 控制面（header/输入条）绑定的会话：激活的是会话标签页 → 它；是文件
// 或空 → 最后激活的会话标签页（记住的那枚，不是序列末位——末位只是
// 「最后被打开」的，开着 A 打字再去看文件时控制面不能静默换绑 B）。
// 无会话标签页（服务端彻底无会话）返回 null。
export function controlRunId(tabs, activeKey, lastSessionKey) {
  const active = tabs.find((t) => tabKey(t) === activeKey)
  if (active?.kind === 'session') return active.runId
  const last = tabs.find((t) => tabKey(t) === lastSessionKey)
  return last?.kind === 'session' ? last.runId : null
}

// 落盘形状：只序列化会话标签页与它的激活态（文件标签页不持久化，刷新
// 后消失）。激活的是文件标签页时 viewRun 落到记住的最后激活会话标签页；
// 记忆失效（防御）回落数组首枚。
export function persistableTabs(tabs, activeKey, lastSessionKey) {
  const openTabs = tabs.filter((t) => t.kind === 'session').map((t) => t.runId)
  const viewRunId = controlRunId(tabs, activeKey, lastSessionKey)
  return { openTabs, viewRunId: openTabs.includes(viewRunId) ? viewRunId : openTabs[0] ?? null }
}
