---
name: deploy-summary
description: "手动整理某次软件版本制作的最终有效步骤、参数、差异、修正经验和人工上架反馈，追加独立版本总结修订。"
disable-model-invocation: true
allowed-tools: Read, Write, Bash, Glob
---

# 软件版本制作总结

仅在用户手动调用本 skill 时运行。先读取 [制作记录交接约定](../../deploy-records.md)，运行 `python scripts/deploy_record.py schema` 获取正式字段、枚举和摘要结构。总结仅读取持久制作记录与本地材料并写文档；不派发 deploy 阶段，不运行 SSH、安装、验证、清理、制镜像、切 OS、上传或上架操作，也不因此创建部署任务或阶段事件。

## 1. 确定制作及人工反馈

从当前会话可准确取得的 production_id、软件、实际版本和镜像关联自动读取；跨会话使用明确 production_id。需要查找时通过记录入口 list 筛选软件、实际版本或来源，再 show 读取；多条结果必须由用户指定，不按 mtime 或最近目录猜选。已有资料不能确定的实际版本、环境和来源保持未知。

读取 `stages --production-id <id>`、`summaries --production-id <id>` 及其已提交正文与固定证据，确定本次采用的指南、安装、验证、归档及流水线修订。用户尚未生成镜像时如实记录未生成；制作未完成、失败或尚未上架都可以总结，不要求补不存在的镜像。

若用户明确反馈已上架，核对反馈指向最终哪次 image_id；多个镜像或制作间存在歧义时先请求明确关联，不猜。人工反馈时间、原意和来源写入 listing，取值按 schema；缺少上架反馈则如实记录当前状态，打包成功、上传或提交审核均不能替代上架。此处只需要用户补齐关键事实，不增加文档审批步骤。

本次手动调用的 Web 来源可通过 update 追加 source；保持原制作身份。只做总结时不 begin-stage。

## 2. 核对证据与最终有效状态

逐项核对 schema.summary_sections：软件实际版本和实际环境及来源；制作/会话/最终镜像和人工结果；所用基准；实际有效的准备、依赖、安装、配置、启动、验证及打包；具体参数和脚本；相对基准差异及依据；失败与修正；验证范围和缺口；本次与此前总结的变化。

- 最终有效步骤依据实际执行和当前最终状态整理，引用对应修订的命令、配置和脚本。保留失败前已成功步骤、补装依赖和后续配置修改的来源；把用户建议与 agent 已执行修改分开，未执行建议不写成成功步骤。
- 命令和非敏感参数、环境变量、目录、服务、端口、必要配置内容和脚本保持具体可查；秘密值只记参数名及获取方式。被引用的材料必须在已提交修订内固定，或复制脱敏快照到本次 evidence_dir；共享 alias 日志、远程路径和长对话记忆不能作唯一证据。
- 以本次实际使用的基准记录版本、依赖、参数、配置与步骤变化；无基准仍写本版本完整记录。另选历史比较对象时明确其身份。官网功能变化只有已有官方依据才写，跨大版本差异不意味着可以复用旧步骤。
- 实际验证须对应最终安装与镜像；旧结果不能代替本轮实测。可选诊断未执行单列，不混成关键缺口。仅在确实从基础环境重新执行过时记录相应复现结论，本 skill 不创建机器补验证。
- 依据证据填写 schema.summary 的 coverage、materials、effective_steps、stage_revisions 和 gaps。适用区域记录实际证据，不适用项给具体理由，缺失项保留缺口；不要用一个“完整”布尔值替代检查。review.sections_checked 使用契约要求的全部章节，review.notes 说明正文与执行证据核对结果。

## 3. 生成独立总结修订

```bash
python scripts/deploy_record.py begin-summary --production-id <id>
```

使用返回的 revision_id、paths.summary_file 和 evidence_dir，按 [总结模板](references/template.md) 写正文。模板中的阶段、环境、镜像和证据全部替换为本制作真实信息；未知、未执行或不适用写清原因，不能保留示例值。正式章节以 schema.summary_sections 为准。

把每处正文结论与固定来源对齐，填写 schema.summary 对应 JSON。需要固定已有材料时使用记录入口 snapshot（查看 `snapshot --help`，总结修订带 `--summary`）；文档详细内容留在 Markdown，JSON 保存关联与校验信息。用户上架反馈与 final_image_id 必须一致，未经明确反馈不填成功结果。

## 4. 校验并提交

```bash
python scripts/deploy_record.py commit-summary --production-id <id> --revision-id <本轮总结修订> --data <本轮JSON文件>
python scripts/deploy_record.py candidate --production-id <id> --revision-id <本轮总结修订>
```

先写正文及全部附件，再提交；中断草稿不作为已提交候选。校验失败时只修本次草稿并重新提交，不覆盖旧正文或其他修订。材料有缺口的留痕总结也可完整提交，是否可供后续复用另以 candidate 输出为准；上架状态不能替代步骤与证据检查。candidate 结果在回复中报告；不要回写已提交正文，以免破坏固定引用。后续补齐材料或上架后再手动调用，创建新修订。

回复 production_id、总结修订、正文路径、提交结果、复用资格及其原因、关键缺口。总结完成不触发任何部署或云操作。
