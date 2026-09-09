// OBS 产物面板：桶内对象全量清单（/api/obs/objects），树形与本地产物面板
// 同构（obsGroups 派生分组 → artifactTree 复用），行点击开 OBS 对象标签页
// （文本预览 / 二进制占位）。下载不直下：⧉ 复制签名下载链接（7 天有效，
// 剪贴板带 execCommand 兜底），成功后行内 ✓ 反馈。无勾选与 zip——OBS 侧
// 只有在线列举与单对象访问；清单不随流水线事件联动（上传不经过本服务
// 事件面），刷新钮手动重拉 + 归档动作完成后自动刷新。
import { useState } from 'react'
import * as store from '../store.js'
import { fmtSize, obsGroups, artifactTree, defaultOpenPaths } from '../derive.js'

// 行级复制链接按钮：复制成功后 ✓ 反馈 1.5 秒（本地态，不打断浏览）
function CopyLink({ objKey }) {
  const [copied, setCopied] = useState(false)
  const onClick = async (e) => {
    e.stopPropagation()
    if (await store.copyObsLink(objKey)) {
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    }
  }
  return (
    <button
      className="va-art-dl"
      title="复制下载链接（签名，7 天有效；桶私有，匿名不可访问）"
      onClick={onClick}
    >
      {copied ? '✓' : '⧉'}
    </button>
  )
}

// 目录树节点：与 ArtDir 同构，去勾选（无 zip）；文件条目带 key 全名
// （根级对象的 key 不能由「（桶根）」前缀拼回），title 附最后修改时间。
function ObsDir({ node, toggles, setToggles, defaultOpen, openKeys, activeKey }) {
  const open = toggles[node.path] ?? defaultOpen.has(node.path)
  const toggleOpen = () => setToggles({ ...toggles, [node.path]: !open })
  return (
    <div className="va-art-group">
      <div
        className="va-art-dir-head"
        role="button"
        tabIndex={0}
        onClick={toggleOpen}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault()
            toggleOpen()
          }
        }}
        title={node.path}
      >
        <span className="va-art-dir-arrow">{open ? '▾' : '▸'}</span>
        <span className="va-art-name">{node.name}</span>
        <span className="va-art-count" title="本目录（含子目录）对象数">{node.count}</span>
      </div>
      {open && (
        <div className="va-art-children">
          {node.files.map((f) => {
            const key = f.key ?? `${node.path}/${f.name}`
            return (
              <div
                key={key}
                role="button"
                tabIndex={0}
                className={`va-art-item${activeKey === key ? ' on' : ''}`}
                onClick={() => store.openObsObject(key, f)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault()
                    store.openObsObject(key, f)
                  }
                }}
                title={`${key}${f.last_modified ? ` · ${f.last_modified}` : ''}`}
              >
                <span className="va-art-name">{f.name}</span>
                {openKeys.has(key) && <span className="va-art-opened" title="已打开为标签页" />}
                <span className="va-art-size">{fmtSize(f.size)}</span>
                <CopyLink objKey={key} />
              </div>
            )
          })}
          {node.dirs.map((d) => (
            <ObsDir
              key={d.path}
              node={d}
              toggles={toggles}
              setToggles={setToggles}
              defaultOpen={defaultOpen}
              openKeys={openKeys}
              activeKey={activeKey}
            />
          ))}
        </div>
      )}
    </div>
  )
}

export default function ObsPanel({ openKeys, activeKey }) {
  const s = store.useRunState()
  const groups = obsGroups(s.obs.objects)
  const roots = artifactTree(groups)
  const [toggles, setToggles] = useState({})
  return (
    <div className="va-side-panel">
      <div className="va-art-panel-head va-obs-head">
        <span>OBS 产物 · {s.obs.objects.length}</span>
        {s.obs.bucket && (
          <span className="va-obs-bucket" title={s.obs.domain ?? s.obs.bucket}>
            ☁ {s.obs.bucket}
          </span>
        )}
        <button
          className="va-obs-refresh"
          onClick={() => store.refreshObs()}
          disabled={s.obs.loading}
          title="重拉桶内对象清单"
        >
          {s.obs.loading ? '加载中…' : '⟳ 刷新'}
        </button>
      </div>
      {s.obs.error && (
        <div className="va-obs-error" title={typeof s.obs.error === 'string' ? s.obs.error : ''}>
          OBS 不可用：{s.obs.error}
        </div>
      )}
      {roots.length === 0 && !s.obs.error && (
        <div className="va-side-empty">{s.obs.loading ? '清单加载中…' : '桶内暂无对象'}</div>
      )}
      {roots.map((r) => (
        <ObsDir
          key={r.path}
          node={r}
          toggles={toggles}
          setToggles={setToggles}
          defaultOpen={defaultOpenPaths(groups)}
          openKeys={openKeys}
          activeKey={activeKey}
        />
      ))}
    </div>
  )
}
