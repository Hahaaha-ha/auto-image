import { describe, expect, it } from 'vitest'
import { taskFormFromSpec, taskSpecFromForm } from './taskForm.js'

describe('任务表单请求', () => {
  it('无法解析的数值不能经 JSON 变成缺省参数', () => {
    for (const [field, value] of [['diskSize', '80GB'], ['bandwidth', 'abc'], ['bandwidth', 'Infinity']]) {
      const form = { ...taskFormFromSpec(), software: 'nginx', [field]: value }
      expect(() => JSON.stringify(taskSpecFromForm(form))).toThrow('请填写数字')
    }
    const form = { ...taskFormFromSpec(), software: 'nginx', diskSize: '80', bandwidth: '10' }
    expect(JSON.parse(JSON.stringify(taskSpecFromForm(form))).ecs_params).toEqual({ disk_size: 80, bandwidth: 10 })
  })

  it('已保存请求可完整编辑往返，保留补充要求换行及目标身份快照', () => {
    const saved = {
      software: 'Node.js', version: '20', taskType: 'image', installDoc: 'https://example.test/install',
      extraInstructions: '启用 TLS\n验证 /health', ecsMode: 'existing',
      ecsInstance: { id: 'original-id', name: 'original-name', ip: '192.0.2.20' },
    }
    expect(taskSpecFromForm(taskFormFromSpec(saved))).toEqual({
      software: 'Node.js', version: '20', task_type: 'image', install_doc: 'https://example.test/install',
      extra_instructions: '启用 TLS\n验证 /health', ecs_mode: 'existing', ecs_instance: saved.ecsInstance,
    })
  })

  it('切换到新建机器时不再提交旧目标', () => {
    const form = taskFormFromSpec({ software: 'nginx', ecsMode: 'existing',
      ecsInstance: { id: 'old-id', name: 'old', ip: '192.0.2.20' } })
    form.ecsMode = 'create'
    form.flavor = 'c6.xlarge.2'
    const spec = taskSpecFromForm(form)
    expect(spec).not.toHaveProperty('ecs_instance')
    expect(spec.ecs_params).toEqual({ flavor: 'c6.xlarge.2' })
  })
})
