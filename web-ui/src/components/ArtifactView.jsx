import { Fragment } from 'react'
import { fmtSize } from '../derive.js'
import { mdToHtml } from '../markdown.js'
import { parseArtifactMarkdown } from '../artifactMarkdown.js'
import StageBadge from './StageBadge.jsx'

const METADATA_FIELDS = [
  ['software', '软件'],
  ['version', '版本'],
  ['project_url', '项目地址'],
  ['os', '操作系统'],
  ['os_version', '系统版本'],
  ['architecture', '架构'],
  ['installation_method', '安装方式'],
  ['updated_at', '更新时间'],
]
const hasText = value => typeof value === 'string' && value.trim() !== ''

function MetadataValue({ field, value }) {
  if (field === 'project_url' && /^https?:\/\//i.test(value)) {
    return <a href={value} target="_blank" rel="noopener noreferrer">{value}</a>
  }
  if (field === 'updated_at') {
    const date = new Date(value)
    if (!Number.isNaN(date.getTime())) {
      return <time dateTime={value}>{new Intl.DateTimeFormat('zh-CN', {
        dateStyle: 'medium', timeStyle: 'long',
      }).format(date)}</time>
    }
  }
  return value
}

function ArtifactMetadata({ metadata }) {
  const fields = METADATA_FIELDS.filter(([field]) => hasText(metadata[field]))
  if (!fields.length) return null
  const environment = [
    [metadata.os, metadata.os_version].filter(hasText).join(' '),
    metadata.architecture,
  ].filter(hasText).join(' · ')

  return (
    <details className="va-artifact-info">
      <summary>文档信息{environment && <span className="va-artifact-environment"> · {environment}</span>}</summary>
      <dl>
        {fields.map(([field, label]) => (
          <Fragment key={field}>
            <dt>{label}</dt>
            <dd><MetadataValue field={field} value={metadata[field]} /></dd>
          </Fragment>
        ))}
      </dl>
    </details>
  )
}

// downloadLabel/binaryLabel：头部与二进制占位视图的动作按钮文案（本地产物
// 「⤓ 下载」，OBS 对象「⧉ 复制链接」——动作语义由 onDownload 决定）
function ArtifactShell({ artifact, relPath, onDownload, downloadLabel, children }) {
  return (
    <div className="va-artifact">
      <div className="va-artifact-head">
        {artifact.stage && <StageBadge stage={artifact.stage} />}
        <span className="va-artifact-name" title={artifact.name}>{artifact.name}</span>
        <span className="va-artifact-dir" title={artifact.dir}>{artifact.dir}</span>
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

  if (artifact.missing) {
    return <div className="artifact-empty" role="status">当前文件已移除或尚未重新生成。</div>
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

  const { metadata, body } = artifact.name.endsWith('.md')
    ? parseArtifactMarkdown(artifact.content)
    : { metadata: null, body: artifact.content }

  return (
    <ArtifactShell artifact={artifact} relPath={relPath} onDownload={onDownload} downloadLabel={downloadLabel}>
      <div className="va-artifact-md">
        {metadata && <ArtifactMetadata metadata={metadata} />}
        <div className="va-md" dangerouslySetInnerHTML={{ __html: mdToHtml(body) }} />
      </div>
    </ArtifactShell>
  )
}
