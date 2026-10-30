// 全局共享任务清单；查看只打开详情，启动才创建会话执行。
import { useRef, useState } from 'react'
import * as store from '../store.js'
import { fmtAgo, fmtTokens, STAGE_LABEL, TASK_OUTCOME_LABEL, TASK_TYPE_LABEL, stageOrder } from '../derive.js'
import TaskDialog from './TaskDialog.jsx'

const fmtClock = (ts) => (ts ? new Date(ts * 1000).toTimeString().slice(0, 8) : '—')

function StageFlow({ task }) {
  const entries = task.stages ?? []
  return (
    <div className="va-flow" aria-label="阶段流转">
      {stageOrder(task.type).map((stage, i) => {
        const last = entries.filter(entry => entry.stage === stage).at(-1)
        const current = task.status === 'RUNNING' && stage === task.currentStage
        // 回炉优先认当前阶段；异常收尾只能证明最后阶段被打断，不能记为完成。
        const halted = task.status === 'DONE' && task.outcome !== 'success' && last === entries.at(-1)
        const phase = !last ? 'upcoming' : current ? 'current' : halted ? 'halted' : 'done'
        const label = STAGE_LABEL[stage] ?? stage
        const stateLabel = phase === 'current' ? '进行中' : phase === 'halted'
          ? TASK_OUTCOME_LABEL[task.outcome] ?? '已结束' : phase === 'done' ? '已结束' : '未开始'
        return (
          <span key={stage} className="va-flow-seg">
            {i > 0 && <span className="va-flow-arrow" aria-hidden="true">→</span>}
            <span className={`va-flow-pill s-${stage.toLowerCase()} ${phase}`}
              aria-label={`${label}：${stateLabel}`}
              title={last ? `${label}：${fmtClock(last.startedAt)}${last.endedAt ? ` → ${fmtClock(last.endedAt)}` : ''} ${stateLabel}` : label}>
              {phase === 'done' ? '✓ ' : ''}{label}{phase === 'halted' ? ` · ${stateLabel}` : ''}
            </span>
          </span>
        )
      })}
    </div>
  )
}

function TaskRow({ task, active, action, canOpenRun, onView }) {
  const cache = task.usage ? (task.usage.cacheReadTokens ?? 0) + (task.usage.cacheCreationTokens ?? 0) : null
  const isInit = task.status === 'INIT'
  const statusLabel = isInit ? '待运行' : task.status === 'RUNNING' ? '进行中' : TASK_OUTCOME_LABEL[task.outcome] ?? '已完成'
  return (
    <div className={`va-task-row${active ? ' on' : ''}`}>
      <div className="va-task-main" role="button" tabIndex={0} aria-label={`查看任务 ${task.name}`}
        title={`查看任务 ${task.name}`} onClick={onView}
        onKeyDown={event => {
          if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); onView(event) }
        }}>
        <div className="va-task-name">
          <span className={`va-ecs-status ${isInit ? 's-other' : task.status === 'RUNNING' ? 's-build' : task.outcome === 'failed' ? 's-error' : 's-shutoff'}`}
            aria-hidden="true" />
          <span className="va-task-name-text">{task.name}</span>
          <span className={`va-task-outcome o-${isInit ? 'pending' : task.outcome ?? 'none'}`}>{statusLabel}</span>
        </div>
        <StageFlow task={task} />
        <div className="va-task-sub">
          <span className="va-task-chip">{TASK_TYPE_LABEL[task.type] ?? task.type}</span>
          {isInit && task.spec && <span className="va-task-chip">
            {task.spec.ecsMode === 'existing' ? `已有 ${task.spec.ecsInstance?.name ?? ''}` : '按需创建'}
          </span>}
          <span title="缓存 token = 读 + 写">缓存 {fmtTokens(cache)}</span>
          <span title="输入 / 输出 token">入 {fmtTokens(task.usage?.inputTokens)} / 出 {fmtTokens(task.usage?.outputTokens)}</span>
          <span className="va-task-ago">{fmtAgo((task.createdAt ?? 0) * 1000)}</span>
        </div>
      </div>
      <div className="va-task-actions">
        {isInit ? <button className="va-task-run" disabled={action?.busy}
          onClick={() => store.runTask(task.taskId, task.updatedAt)}
          title="创建会话并立即发送已保存的执行指令">
          {action?.busy ? '启动中…' : '启动执行'}
        </button> : canOpenRun && <button className="va-task-jump"
          onClick={() => store.selectRun(task.runId)} title={`打开关联会话 ${task.runId}`}>
          会话
        </button>}
      </div>
      {isInit && action?.error && <div className="va-cfg-msg err va-task-row-error" role="alert">{action.error}</div>}
    </div>
  )
}

export default function TasksPanel() {
  const s = store.useRunState()
  // undefined=关闭，null=新建，其余为查看中的 task_id。
  const [dialogTaskId, setDialogTaskId] = useState(undefined)
  const returnFocusRef = useRef(null)
  const openDialog = (taskId, trigger) => {
    returnFocusRef.current = trigger
    setDialogTaskId(taskId)
  }
  const running = s.tasks.filter(task => task.status === 'RUNNING').length
  const selected = s.tasks.find(task => task.taskId === dialogTaskId)
  return (
    <div className="va-side-panel">
      <div className="va-art-panel-head va-obs-head">
        <span>任务 · {s.tasks.length}</span>
        {running > 0 && <span className="va-obs-bucket" title="进行中的流水线任务">{running} 进行中</span>}
        <button className="va-obs-refresh" onClick={event => openDialog(null, event.currentTarget)} title="填写并保存待运行任务">+ 新建任务</button>
        <button className="va-obs-refresh" onClick={() => store.refreshTasks()} title="重拉任务清单">⟳ 刷新</button>
      </div>
      {s.tasks.length === 0 && <div className="va-side-empty">暂无任务，点「+ 新建任务」填写参数，或在会话中发起部署。</div>}
      {s.tasks.map(task => <TaskRow key={task.taskId} task={task}
        active={s.activeTaskId === task.taskId || dialogTaskId === task.taskId}
        action={s.taskActions[task.taskId]} canOpenRun={Boolean(s.runs[task.runId])}
        onView={event => openDialog(task.taskId, event.currentTarget)} />)}
      {(dialogTaskId === null || selected) && <TaskDialog key={dialogTaskId ?? 'new'}
        task={selected ?? null} returnFocusRef={returnFocusRef} onClose={() => setDialogTaskId(undefined)}
        onCreated={task => setDialogTaskId(task.taskId)} />}
    </div>
  )
}
