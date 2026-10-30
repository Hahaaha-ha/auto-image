// 表单只保存用户请求；不使用任务后来识别出的软件、版本或机器回填。
export function taskFormFromSpec(spec) {
  const params = spec?.ecsParams ?? {}
  return {
    software: spec?.software ?? '', version: spec?.version ?? '',
    taskType: spec?.taskType ?? 'image', installDoc: spec?.installDoc ?? '',
    extraInstructions: spec?.extraInstructions ?? '', ecsMode: spec?.ecsMode ?? 'create',
    ecsInstance: spec?.ecsInstance ?? null,
    image: params.image ?? '', flavor: params.flavor ?? '', name: params.name ?? '',
    diskSize: String(params.disk_size ?? ''), bandwidth: String(params.bandwidth ?? ''),
  }
}

const invalid = (field, text) => Object.assign(new Error(text), { field })

export function taskSpecFromForm(form) {
  if (!form.software.trim()) throw invalid('software', '请填写软件名称')
  const spec = {
    software: form.software.trim(), version: form.version.trim(), task_type: form.taskType,
    install_doc: form.installDoc.trim(), extra_instructions: form.extraInstructions.trim(),
    ecs_mode: form.ecsMode,
  }
  if (form.ecsMode === 'existing') {
    if (!form.ecsInstance) throw invalid('ecsInstance', '请选择一个已有 ECS 实例')
    spec.ecs_instance = { ...form.ecsInstance }
  } else {
    const params = {}
    for (const key of ['image', 'flavor', 'name']) {
      if (form[key].trim()) params[key] = form[key].trim()
    }
    for (const [field, key, label] of [['diskSize', 'disk_size', '系统盘大小'], ['bandwidth', 'bandwidth', '带宽']]) {
      if (!form[field].trim()) continue
      const value = Number(form[field])
      if (!Number.isFinite(value)) throw invalid(field, `${label}请填写数字，不要带单位`)
      params[key] = value
    }
    spec.ecs_params = params
  }
  return spec
}

export function taskFieldError(error) {
  if (error.field) return { field: error.field, text: error.message }
  const detail = typeof error.detail === 'string' ? error.detail : error.message
  const fields = [
    ['software required', 'software', '软件名称须为 1–64 个字符'],
    ['invalid version', 'version', '版本长度不能超过 512 个字符'],
    ['invalid install_doc', 'installDoc', '安装文档链接长度不能超过 512 个字符'],
    ['invalid extra_instructions', 'extraInstructions', '补充要求格式不正确'],
    ['ecs_instance ', 'ecsInstance', '请选择具有名称和可用 IP 的 ECS 实例'],
    ['invalid ecs_params.image', 'image', '镜像 ID 须为 1–128 个字符'],
    ['invalid ecs_params.flavor', 'flavor', '规格须为 1–128 个字符'],
    ['invalid ecs_params.name', 'name', 'ECS 名称须为 1–128 个字符'],
    ['invalid ecs_params.disk_size', 'diskSize', '系统盘须为 10–1024 的整数（GB）'],
    ['invalid ecs_params.bandwidth', 'bandwidth', '带宽须为 1–2000 的整数（Mbit/s）'],
  ]
  const match = fields.find(([prefix]) => detail?.startsWith(prefix))
  return match ? { field: match[1], text: match[2] } : null
}
