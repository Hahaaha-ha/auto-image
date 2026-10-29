import { useEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import * as store from '../store.js'
import { STAGE_LABEL, TASK_OUTCOME_LABEL, TASK_TYPE_LABEL } from '../derive.js'
import { taskFieldError, taskFormFromSpec, taskSpecFromForm } from '../taskForm.js'
import { useModal } from './modal.js'
import { useDialogLeaveProtection } from './useDialogLeaveProtection.js'
import './TaskDialog.css'

const instanceKey = (instance) => instance?.id || (instance ? `${instance.name}@${instance.ip}` : '')
const statusText = (task) => task.status === 'INIT' ? '待运行' : task.status === 'RUNNING' ? '进行中' : TASK_OUTCOME_LABEL[task.outcome] ?? '已完成'

function TaskParameters({ spec }) {
  const form = taskFormFromSpec(spec)
  const entries = [
    ['类型', TASK_TYPE_LABEL[form.taskType] ?? form.taskType], ['软件名称', form.software],
    ['请求版本', form.version || '最新稳定版'], ['安装文档链接', form.installDoc || '由 Agent 检索官方文档'],
    ['部署目标', form.ecsMode === 'existing' ? '已有 ECS' : '按需创建 ECS'],
    ...(form.ecsMode === 'existing'
      ? [['实例名称', form.ecsInstance?.name || '—'], ['实例 ID', form.ecsInstance?.id || '—'], ['IP', form.ecsInstance?.ip || '—']]
      : [['镜像 ID', form.image || '默认配置'], ['规格', form.flavor || '默认配置'],
        ['ECS 名称', form.name || '自动命名'], ['系统盘 GB', form.diskSize || '默认配置'], ['带宽 Mbit/s', form.bandwidth || '默认配置']]),
  ]
  return <>
    <dl className="va-task-parameters">{entries.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl>
    <h3>补充要求</h3>
    <p className="va-task-extra-value">{form.extraInstructions || '未填写'}</p>
  </>
}

export default function TaskDialog({ task = null, onClose, onCreated, returnFocusRef }) {
  const state = store.useRunState()
  const [mode, setMode] = useState(task ? 'view' : 'new')
  const [draft, setDraft] = useState(() => taskFormFromSpec(task?.spec))
  const [baseline, setBaseline] = useState(draft)
  const [editUpdatedAt, setEditUpdatedAt] = useState(task?.updatedAt)
  const [instances, setInstances] = useState(null)
  const [instanceError, setInstanceError] = useState(null)
  const [defaultError, setDefaultError] = useState(false)
  const [saving, setSaving] = useState(false)
  const [refreshing, setRefreshing] = useState(false)
  const [problem, setProblem] = useState(null)
  const [preview, setPreview] = useState(null)
  const [previewAttempt, setPreviewAttempt] = useState(0)
  const touched = useRef(new Set())
  const savingRef = useRef(false)
  const mounted = useRef(true)
  const fields = useRef({})
  const errorRef = useRef(null)
  const leaveTarget = useRef('close')
  const editor = mode === 'new' || mode === 'edit'
  const hasDraft = editor || mode === 'latest'
  const dirty = hasDraft && JSON.stringify(draft) !== JSON.stringify(baseline)
  const action = state.taskActions?.[task?.taskId]
  const busy = saving || Boolean(action?.busy)
  const isBusy = () => savingRef.current || Boolean(store.getState().taskActions?.[task?.taskId]?.busy)
  const discard = () => {
    if (leaveTarget.current === 'view') { setMode('view'); setProblem(null) }
    else onClose()
  }
  const leave = useDialogLeaveProtection({ isBusy, dirty, onDiscard: discard })
  const requestClose = () => { leaveTarget.current = 'close'; return leave() }
  const panelRef = useModal(() => !isBusy(), requestClose, undefined, undefined, returnFocusRef)

  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])
  useEffect(() => {
    if (editor) fields.current.software?.focus()
    else panelRef.current?.focus()
  }, [mode])
  useEffect(() => {
    if (!saving && problem) (fields.current[problem.field] || errorRef.current)?.focus()
  }, [saving, problem])

  // 默认值只影响新建的未编辑字段；编辑始终以已保存参数为准。
  useEffect(() => {
    if (task) return
    let active = true
    fetch('/api/ecs/defaults').then(async (response) => {
      if (!response.ok) throw new Error('defaults unavailable')
      const data = await response.json()
      if (!active) return
      const defaults = { image: data.image ?? '', flavor: data.flavor ?? '', diskSize: String(data.disk_size ?? ''), bandwidth: String(data.bandwidth ?? '') }
      const fill = Object.fromEntries(Object.entries(defaults).filter(([key]) => !touched.current.has(key)))
      setDraft(form => ({ ...form, ...fill }))
      setBaseline(form => ({ ...form, ...fill }))
    }).catch(() => { if (active) setDefaultError(true) })
    return () => { active = false }
  }, [])

  useEffect(() => {
    if (!editor || draft.ecsMode !== 'existing' || instances !== null) return
    let active = true
    setInstanceError(null)
    fetch('/api/ecs/instances?limit=1000').then(async (response) => {
      const data = await response.json()
      if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `HTTP ${response.status}`)
      if (active) setInstances(data.instances ?? [])
    }).catch(error => { if (active) setInstanceError(error.message) })
    return () => { active = false }
  }, [editor, draft.ecsMode, instances])

  const source = editor ? draft : task?.spec ? taskFormFromSpec(task.spec) : null
  const request = useMemo(() => {
    if (!source || (!editor && task?.status !== 'INIT')) return null
    try { return { spec: taskSpecFromForm(source) } }
    catch (error) { return { problem: taskFieldError(error) } }
  }, [JSON.stringify(source), editor, task?.status])
  // 输入与任务快照都参与预览身份，旧响应不能成为新内容的执行依据。
  const previewKey = request ? JSON.stringify([mode, request, task?.updatedAt, editUpdatedAt]) : null
  useEffect(() => {
    if (!request?.spec) return
    let active = true
    const timer = setTimeout(() => {
      store.previewTask(request.spec).then(prompt => {
        if (active && prompt !== undefined) setPreview({ key: previewKey, prompt })
      }).catch(error => {
        if (active) setPreview({ key: previewKey, problem: taskFieldError(error), error: store.taskErrorMessage(error) })
      })
    }, editor ? 250 : 0)
    return () => { active = false; clearTimeout(timer) }
  }, [previewKey, previewAttempt])
  const currentPreview = preview?.key === previewKey ? preview : null
  const previewProblem = request?.problem || currentPreview?.problem
  const fieldProblem = problem?.field ? problem : previewProblem && touched.current.has(previewProblem.field) ? previewProblem : null
  const canStart = task?.status === 'INIT' && typeof currentPreview?.prompt === 'string' && !request?.problem && !editor && mode !== 'latest'
  const prompt = task?.status !== 'INIT' && !editor ? task?.turnText : currentPreview?.prompt

  const change = (key, value) => {
    touched.current.add(key)
    setDraft(form => ({ ...form, [key]: value }))
    setProblem(null)
  }
  const beginEditing = () => {
    const form = taskFormFromSpec(task.spec)
    setDraft(form); setBaseline(form); setEditUpdatedAt(task.updatedAt)
    touched.current = new Set()
    setProblem(null); store.clearTaskError(task.taskId); setMode('edit')
  }
  const cancelEditing = () => {
    leaveTarget.current = 'view'
    leave()
    leaveTarget.current = 'close'
  }
  const submit = async (event) => {
    event.preventDefault()
    if (isBusy()) return
    let spec
    try { spec = taskSpecFromForm(draft) }
    catch (error) { setProblem(taskFieldError(error)); return }
    savingRef.current = true; setSaving(true); setProblem(null)
    try {
      const saved = task
        ? await store.updateTask(task.taskId, spec, editUpdatedAt)
        : await store.createTask(spec)
      if (!saved || !mounted.current) return
      if (task) { setBaseline(draft); setMode('view') }
      else onCreated(saved)
    } catch (error) {
      if (mounted.current) setProblem({ ...(taskFieldError(error) || { text: store.taskErrorMessage(error) }), conflict: error.status === 409 })
    } finally {
      savingRef.current = false
      if (mounted.current) setSaving(false)
    }
  }
  const showLatest = async () => {
    if (refreshing) return
    setRefreshing(true)
    const tasks = await store.refreshTasks()
    if (!mounted.current) return
    setRefreshing(false)
    if (tasks) setMode('latest')
    else setProblem({ text: '任务刷新失败，请稍后重试', conflict: true })
  }
  const start = async () => {
    if (isBusy() || !canStart) return
    if (await store.runTask(task.taskId, task.updatedAt)) onClose()
  }
  const field = (key, label, placeholder = '') => <label className="va-task-field">
    <span>{label}{key === 'software' && <span aria-hidden="true"> *</span>}</span>
    <input ref={node => { fields.current[key] = node }} name={key} className="va-cfg-input" type="text"
      value={draft[key]} onChange={event => change(key, event.target.value)} disabled={busy} autoComplete="off"
      required={key === 'software'} aria-label={label} aria-invalid={fieldProblem?.field === key}
      aria-describedby={fieldProblem?.field === key ? `task-${key}-error` : undefined} placeholder={placeholder} />
    {fieldProblem?.field === key && <span id={`task-${key}-error`} className="va-task-field-error" role="alert">{fieldProblem.text}</span>}
  </label>
  const radios = (key, title, options) => <fieldset className="va-task-radio-group" disabled={busy}>
    <legend>{title}</legend>
    {options.map(([value, label]) => <label key={value}><input type="radio" name={key} checked={draft[key] === value} onChange={() => change(key, value)} />{label}</label>)}
  </fieldset>
  const selected = draft.ecsInstance
  const choices = selected ? [selected, ...(instances ?? []).filter(item => instanceKey(item) !== instanceKey(selected))] : instances ?? []
  const title = mode === 'new' ? '新建任务' : mode === 'edit' ? '编辑任务' : mode === 'latest' ? '最新任务详情' : '任务详情'
  const pending = task?.status === 'INIT'
  const canOpenRun = Boolean(task?.runId && state.runs[task.runId])
  const actionError = !editor && pending && action?.error

  return createPortal(<div className="va-modal-overlay va-task-overlay" onClick={requestClose}>
    <section className="va-modal va-task-dialog" ref={panelRef} tabIndex={-1} role="dialog" aria-modal="true" aria-labelledby="task-dialog-title"
      onClick={event => event.stopPropagation()}>
      <header className="va-task-dialog-heading">
        <div><h2 id="task-dialog-title">{title}</h2><p>{task ? task.name : '填写部署参数，可在创建后继续核对和修改。'}</p></div>
        {task && <span className="va-task-dialog-status">{statusText(task)}</span>}
        <button type="button" className="va-modal-close" onClick={requestClose} disabled={busy} aria-label="关闭任务详情">✕</button>
      </header>
      <form className="va-task-dialog-form" onSubmit={submit} noValidate aria-label={editor ? title : '任务保存内容'} aria-busy={busy}>
        <div className="va-task-dialog-body">
          <section className="va-task-dialog-fields" aria-label={editor ? '任务参数' : '已保存参数'}>
            <h3>{editor ? '任务参数' : task?.spec ? '已保存参数' : '任务信息'}</h3>
            {editor ? <>
              {radios('taskType', '类型', [['image', '镜像（部署并制镜像）'], ['rpm', 'RPM（制作 RPM 包）']])}
              {field('software', '软件名称', '如 nginx')}
              {field('version', '版本', '留空使用最新稳定版')}
              {field('installDoc', '安装文档链接', '留空由 Agent 检索官方文档')}
              {radios('ecsMode', '部署目标', [['create', '按需创建 ECS'], ['existing', '已有 ECS']])}
              {draft.ecsMode === 'create' ? <>
                {field('image', '镜像 ID', '留空使用默认配置')}
                {field('flavor', '规格', '如 c6.xlarge.2')}
                {field('name', 'ECS 名称', '留空自动命名')}
                <div className="va-task-number-fields">{field('diskSize', '系统盘 GB', '默认配置')}{field('bandwidth', '带宽 Mbit/s', '默认配置')}</div>
                {defaultError && <p className="va-task-help">默认配置未加载，可手动填写或留空使用默认值。</p>}
              </> : <label className="va-task-field"><span>选择实例</span>
                <select ref={node => { fields.current.ecsInstance = node }} value={instanceKey(selected)} disabled={busy}
                  onChange={event => change('ecsInstance', choices.find(item => instanceKey(item) === event.target.value) || null)}
                  aria-invalid={fieldProblem?.field === 'ecsInstance'} aria-describedby={fieldProblem?.field === 'ecsInstance' ? 'task-instance-error' : undefined}>
                  <option value="">请选择已有 ECS 实例</option>
                  {choices.map(item => <option key={instanceKey(item)} value={instanceKey(item)}>{item.name}（{item.ip || '无 IP'}）</option>)}
                </select>
                {selected && <span className="va-task-help">目标 IP：{selected.ip || '未知'}{selected.id ? ` · ${selected.id}` : ''}</span>}
                {instances === null && !instanceError && <span className="va-task-help" role="status">正在加载实例清单…</span>}
                {instanceError && <span className="va-task-help">实例清单加载失败：{instanceError}{selected ? '；当前已保存的实例仍可保留。' : ''}</span>}
                {instances?.length === 0 && <span className="va-task-help">当前没有可选实例。</span>}
                {fieldProblem?.field === 'ecsInstance' && <span id="task-instance-error" className="va-task-field-error" role="alert">{fieldProblem.text}</span>}
              </label>}
              <label className="va-task-field va-task-extra"><span>补充要求</span>
                <textarea ref={node => { fields.current.extraInstructions = node }} value={draft.extraInstructions} disabled={busy} rows={4}
                  onChange={event => change('extraInstructions', event.target.value)} placeholder="例如：启用 TLS，并额外验证 /health 接口。"
                  aria-invalid={fieldProblem?.field === 'extraInstructions'} />
                {fieldProblem?.field === 'extraInstructions' && <span className="va-task-field-error" role="alert">{fieldProblem.text}</span>}
              </label>
            </> : task?.spec ? <TaskParameters spec={task.spec} /> : <p className="va-task-help">此任务由会话中的流水线执行产生，没有单独填写的任务表单。</p>}
            {!editor && task && <dl className="va-task-execution-info">
              <div><dt>任务状态</dt><dd>{statusText(task)}</dd></div>
              {task.currentStage && <div><dt>当前阶段</dt><dd>{STAGE_LABEL[task.currentStage] ?? task.currentStage}</dd></div>}
              {task.runId && <div><dt>关联会话</dt><dd>{task.runId}{!canOpenRun && <span className="va-task-help">当前账号无法访问此会话</span>}</dd></div>}
            </dl>}
          </section>
          <section className="va-task-prompt" role="region" aria-label={pending || editor ? '执行指令预览' : '实际执行指令'}>
            <h3>{pending || editor ? '执行指令预览' : '实际执行指令'}</h3>
            <p className="va-task-help">{pending || editor ? '启动时发送给 Agent。创建和保存不会执行部署。' : '本任务启动时实际发送的内容，后续配置修改不会改变这份记录。'}</p>
            {mode === 'latest' && <p className="va-task-help va-task-draft-note">这里显示最新的已保存内容；你的未保存输入仍保留，可返回编辑后复制。</p>}
            {typeof prompt === 'string' ? <pre className="va-task-prompt-text">{prompt || '没有记录到执行指令。'}</pre>
              : request?.problem ? <p className="va-task-preview-placeholder">{request.problem.text}。完善参数后会自动生成预览。</p>
                : currentPreview?.error ? <div className="va-task-preview-error"><p role="alert">{currentPreview.problem?.text || currentPreview.error}</p><button type="button" onClick={() => { setPreview(null); setPreviewAttempt(value => value + 1) }}>重新生成预览</button></div>
                  : request ? <p className="va-task-preview-placeholder" role="status">正在生成预览…</p> : <p className="va-task-preview-placeholder">没有记录到执行指令。</p>}
          </section>
        </div>
        <footer className="va-task-dialog-footer">
          {(problem || actionError) && <div className="va-task-dialog-error" ref={errorRef} role="alert" tabIndex={-1}>
            <span>{problem?.text || actionError}</span>
            {problem?.conflict && <button type="button" onClick={showLatest} disabled={refreshing || busy}>{refreshing ? '刷新中…' : '查看最新任务'}</button>}
          </div>}
          <div className="va-task-dialog-actions" key={mode}>
            {editor ? <>
              <button type="button" onClick={mode === 'new' ? requestClose : cancelEditing} disabled={busy}>{mode === 'new' ? '取消' : '取消编辑'}</button>
              <button type="submit" className="va-cfg-save" disabled={busy}>{saving ? '保存中…' : mode === 'new' ? '创建任务' : '保存修改'}</button>
            </> : mode === 'latest' ? <>
              <button type="button" onClick={requestClose}>关闭</button>
              <button type="button" onClick={() => setMode('edit')}>返回未保存编辑</button>
            </> : <>
              <button type="button" onClick={requestClose} disabled={busy}>关闭</button>
              {pending && <button type="button" onClick={beginEditing} disabled={busy}>编辑</button>}
              {pending && <button type="button" className="va-cfg-save" onClick={start} disabled={busy || !canStart}>{action?.busy ? '启动中…' : '启动执行'}</button>}
              {canOpenRun && <button type="button" onClick={() => { store.selectRun(task.runId); onClose() }}>打开会话</button>}
            </>}
          </div>
        </footer>
      </form>
    </section>
  </div>, document.body)
}
