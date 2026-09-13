// 任务面板：流水线任务清单（rpm-*/deploy-* 子 agent 派发即建，服务端
// task/ 目录持久化）。每行：状态点 + 任务名（类型+软件+版本+时间戳）+
// 阶段流转图（done ✓ / current 呼吸 / upcoming 灰）+ token（缓存/输入/
// 输出；进行中为 —，回合结束才采到总量）+ 机器 + 会话 chip（点击跳会话
// 标签页）。行可展开看各阶段起止时刻。「会话标签上的任务 pill」与
// ECS 运行态都跳/联到这里（openTask 高亮）。
import { useState } from 'react'
import * as store from '../store.js'
import { fmtAgo, fmtTokens, STAGE_LABEL, TASK_OUTCOME_LABEL, TASK_TYPE_LABEL, stageOrder } from '../derive.js'

// 阶段流转图：按类型排 pill（镜像 GUIDE→INSTALL→VERIFY→ARCHIVE；RPM
// GUIDE→BUILD→VERIFY→ARCHIVE），done=绿✓、current=蓝呼吸、upcoming=灰。
// 复用产物徽标的阶段配色（va-art-badge s-*）+ 流转态类。
function StageFlow({ task }) {
  const order = stageOrder(task.type)
  const entries = task.stages ?? []
  return (
    <div className="va-flow" aria-label="阶段流转">
      {order.map((stage, i) => {
        const starts = entries.filter((s) => s.stage === stage)
        const last = starts[starts.length - 1]
        // 已开始过且（已结束 或 后面阶段已经开过）= done；正在跑且没有
        // 更晚的阶段条目 = current；从未开始 = upcoming
        const laterStarted = order.slice(i + 1).some((st) => entries.some((s) => s.stage === st))
        const isCurrent = task.status === 'RUNNING' && stage === task.currentStage
        const phase = starts.length === 0 ? 'upcoming' : (isCurrent && !laterStarted ? 'current' : 'done')
        return (
          <span key={stage} className="va-flow-seg">
            {i > 0 && <span className="va-flow-arrow">→</span>}
            <span
              className={`va-flow-pill s-${stage.toLowerCase()} ${phase}`}
              title={last ? `${STAGE_LABEL[stage] ?? stage}：${fmtClock(last.startedAt)}${last.endedAt ? ` → ${fmtClock(last.endedAt)}` : ' 进行中'}` : STAGE_LABEL[stage] ?? stage}
            >
              {phase === 'done' ? '✓ ' : ''}{STAGE_LABEL[stage] ?? stage}
            </span>
          </span>
        )
      })}
    </div>
  )
}

const fmtClock = (ts) => (ts ? new Date(ts * 1000).toTimeString().slice(0, 8) : '—')

function TaskRow({ task, active, expanded, onToggle }) {
  const cache = task.usage ? (task.usage.cacheReadTokens ?? 0) + (task.usage.cacheCreationTokens ?? 0) : null
  return (
    <div className={`va-task-row${active ? ' on' : ''}`}>
      <div className="va-task-main" role="button" tabIndex={0} onClick={onToggle}
           onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onToggle() } }}
           title={task.turnText || task.name}>
        <div className="va-task-name">
          {task.status === 'RUNNING'
            ? <span className="va-ecs-status s-build" title="进行中" />
            : <span className={`va-ecs-status ${task.outcome === 'success' ? 's-active' : task.outcome === 'failed' ? 's-error' : 's-shutoff'}`}
                     title={TASK_OUTCOME_LABEL[task.outcome] ?? task.outcome} />}
          <span className="va-task-name-text">{task.name}</span>
          <span className={`va-task-outcome o-${task.outcome ?? 'none'}`}>
            {task.status === 'RUNNING' ? '进行中' : (TASK_OUTCOME_LABEL[task.outcome] ?? '已完成')}
          </span>
        </div>
        <StageFlow task={task} />
        <div className="va-task-sub">
          <span className="va-task-chip">{TASK_TYPE_LABEL[task.type] ?? task.type}</span>
          <span title="缓存 token = 读 + 写">缓存 {fmtTokens(cache)}</span>
          <span title="输入 / 输出 token">入 {fmtTokens(task.usage?.inputTokens)} / 出 {fmtTokens(task.usage?.outputTokens)}</span>
          <span className="va-task-ago">{fmtAgo((task.createdAt ?? 0) * 1000)}</span>
        </div>
      </div>
      <div className="va-task-actions">
        <button
          className="va-task-jump"
          onClick={() => store.selectRun(task.runId)}
          title={`跳到关联会话 ${task.runId}（打开/激活其标签页）`}
        >
          ⑂ 会话
        </button>
      </div>
      {expanded && (
        <div className="va-task-detail">
          <div className="va-task-line"><span>任务 ID</span><code>{task.taskId}</code></div>
          <div className="va-task-line"><span>会话</span><code>{task.runId}</code></div>
          <div className="va-task-line">
            <span>机器</span>
            <code>{task.instanceId ?? task.serverAlias ?? '—'}</code>
            {task.serverAlias && task.instanceId && <span className="va-task-note">别名 {task.serverAlias}</span>}
          </div>
          {(task.stages ?? []).map((s, i) => (
            <div className="va-task-line" key={i}>
              <span>{STAGE_LABEL[s.stage] ?? s.stage}</span>
              <code>{fmtClock(s.startedAt)} → {s.endedAt ? fmtClock(s.endedAt) : '进行中'}</code>
            </div>
          ))}
          {task.usage && (
            <div className="va-task-line">
              <span>token</span>
              <code>
                缓存读 {fmtTokens(task.usage.cacheReadTokens)} · 缓存写 {fmtTokens(task.usage.cacheCreationTokens)}
                {' '}· 入 {fmtTokens(task.usage.inputTokens)} · 出 {fmtTokens(task.usage.outputTokens)}
              </code>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

export default function TasksPanel() {
  const s = store.useRunState()
  const [expanded, setExpanded] = useState({})
  const running = s.tasks.filter((t) => t.status === 'RUNNING').length
  return (
    <div className="va-side-panel">
      <div className="va-art-panel-head va-obs-head">
        <span>任务 · {s.tasks.length}</span>
        {running > 0 && (
          <span className="va-obs-bucket" title="进行中的流水线任务">{running} 进行中</span>
        )}
        <button
          className="va-obs-refresh"
          onClick={() => store.refreshTasks()}
          title="重拉任务清单"
        >
          ⟳ 刷新
        </button>
      </div>
      {s.tasks.length === 0 && (
        <div className="va-side-empty">暂无任务（rpm-*/deploy-* 流水线派发即建）</div>
      )}
      {s.tasks.map((t) => (
        <TaskRow
          key={t.taskId}
          task={t}
          active={s.activeTaskId === t.taskId}
          expanded={!!expanded[t.taskId]}
          onToggle={() => setExpanded({ ...expanded, [t.taskId]: !expanded[t.taskId] })}
        />
      ))}
    </div>
  )
}
