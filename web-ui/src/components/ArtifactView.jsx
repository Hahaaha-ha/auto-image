import { fmtSize } from '../derive.js'
import { mdToHtml } from '../markdown.js'
import StageBadge from './StageBadge.jsx'

// downloadLabel/binaryLabel：头部与二进制占位视图的动作按钮文案（本地产物
// 「⤓ 下载」，OBS 对象「⧉ 复制链接」——动作语义由 onDownload 决定）
function ArtifactShell({ artifact, relPath, onDownload, downloadLabel, children }) {
  return (
    <div className="va-artifact">
      <div className="va-artifact-head">
        {artifact.stage && <StageBadge stage={artifact.stage} />}
        <span className="va-artifact-name">{artifact.name}</span>
        <span className="va-artifact-dir">{artifact.dir}</span>
        <button
          className="va-artifact-dl"
          onClick={() => onDownload(relPath)}
          title={downloadLabel === undefined ? '下载此文件' : '复制签名下载链接（7 天有效）'}
        >
          {downloadLabel ?? '⤓ 下载'}
        </button>
      </div>
      {children}
    </div>
  )
}

// 产物文件标签页内容按未加载、二进制、JSON、Markdown 顺序分派。二进制
// 分支只消费清单元信息，必须在读取 content 或调用 Markdown 解析器前返回。
export default function ArtifactView({ artifact, onDownload, downloadLabel, binaryLabel }) {
  if (!artifact) {
    return <div className="artifact-empty">加载中…</div>
  }

  const relPath = artifact.dir ? `${artifact.dir}/${artifact.name}` : artifact.name

  if (artifact.binary) {
    return (
      <ArtifactShell artifact={artifact} relPath={relPath} onDownload={onDownload} downloadLabel={downloadLabel}>
        <div className="va-artifact-binary">
          <div className="va-artifact-binary-icon" aria-hidden="true">■</div>
          <div className="va-artifact-binary-name" title={artifact.name}>{artifact.name}</div>
          <div className="va-artifact-binary-size">
            二进制产物{artifact.size != null ? ` · ${fmtSize(artifact.size)}` : ''}，不支持在线预览
          </div>
          <button className="va-artifact-binary-dl" onClick={() => onDownload(relPath)}>
            {binaryLabel ?? '⤓ 下载此文件'}
          </button>
        </div>
      </ArtifactShell>
    )
  }

  if (artifact.name.endsWith('.json')) {
    return (
      <ArtifactShell artifact={artifact} relPath={relPath} onDownload={onDownload}>
        <pre className="va-artifact-raw">{artifact.content}</pre>
      </ArtifactShell>
    )
  }

  return (
    <ArtifactShell artifact={artifact} relPath={relPath} onDownload={onDownload} downloadLabel={downloadLabel}>
      <div
        className="va-artifact-md va-md"
        dangerouslySetInnerHTML={{ __html: mdToHtml(artifact.content) }}
      />
    </ArtifactShell>
  )
}
