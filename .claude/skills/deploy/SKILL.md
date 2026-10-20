---
name: deploy
version: 2.0.0
description: "端到端 Ubuntu 软件部署编排：依次调用 deploy-guide、deploy-install、deploy-verify、deploy-archive，保存独立制作与阶段修订；也用于用户反馈后的原机补救。用户要求一键部署、完整部署、部署并验证、部署并打包或继续整改时使用。软件名与目标版本/官方项目资料必需；已有机器别名可选，无别名时仅首次 install 创建 ECS。版本文档总结由用户手动调用 deploy-summary。"
allowed-tools: Agent, Read, Write, Bash, Glob
---

# Deploy 部署流水线

先读取 [制作记录交接约定](../../deploy-records.md) 并运行记录入口的 schema；所有身份、枚举、路径和提交结构以统一记录模块为准。完整制作始终依次派发 guide → install → verify → archive，复用发生于 guide 输入选择，不省略阶段。总结为独立手动文档操作。

## 编排职责与门禁

编排只管理本地制作/修订记录、派发子 agent、校验产物并写 pipeline-result。文档探索、目标机安装/修改、只读验证、清理和云操作分别由对应的四个独立 Agent 执行。四阶段阻塞完成后顺序调用，不内联替做。

| 阶段 | 本轮必须提交的产物 | 进入下步条件 |
|------|------|------|
| guide | paths.install_file、paths.verify_file | 已提交本轮指南；verify 头部有精确的 `> 验证契约: exit-code-v1`，否则停止 |
| install | paths.install_result_file、机器快照和实际证据 | 安装失败仍派发 verify 采证；机器身份缺口如实透传 |
| verify | paths.verify_result_file、本轮实际只读检查 | 仅本轮通过且关联当前指南/安装时才进入 archive；其他结论跳过 archive |
| archive | paths.archive_result_file、paths.deploy_list_file、镜像/操作证据 | 记录真实结果；缺失或失败不视为完成 |

每步结束用 Read 读取其提交记录、成功阶段的 required 输出（失败阶段读取 paths.stage_result_file 或本轮正常失败报告）和必要字段，确认 production_id、revision_id 与本轮分配一致，不能凭旧文件存在判通过。install 失败仍派发 verify，但 guide 失败后无有效输入则停止。verify 未通过时，按 verify 报告的具体结论原因写 archive 跳过原因；保留基础设施异常、检查项无法判定和指南问题的真实语义。

## 输入与续作

用户提供软件、目标版本/官方资料、部署要求、目标系统/架构、基础镜像或 ECS 规格；已有 alias、instance_id、认证来源可用时保留。目标环境关键字段未知交给 guide 如实记录并按官方资料生成，不能代入 agent 宿主环境。

先区分新制作和继续明确制作：

- 新制作通过统一入口 create，同软件同版本仍创建新 production_id。source 保存执行者已给出的当前 Web run_id/call_id/task_id，没有上下文则为空。
- 续作通过明确 production_id 读取记录，并 update 追加当前 source；软件/版本/来源可帮助 list 定位，有多条时要求指定，不以最近目录或 mtime 猜选。
- 用户纠错沿用制作与原机。把失败现象、已知原因、用户建议、当前指南、安装/验证/镜像证据及目标机器交给 guide 修订；补救模式显式传入本次制作、机器身份和执行范围。若机器失效或身份变更，停止反馈；需要新建机器则新建制作并关联原制作。

编排不收集和透传密码；archive 自行通过 ecs.py 的 scope 配置获取。已有 alias 分支沿用制作记录中已知 instance_id 与认证方式，不能写空；确实缺失 instance_id 时记录 archive 跳过原因。

## 执行

### 0. 准备记录

读取 deploy.config.yaml 和记录工具契约，按共享约定创建或明确续作制作。已有机器信息调用 set-machine 持久化。请求版本尚不明确可用配置占位，guide 核实实际版本及大版本依据后 update。记录工具返回的目录是唯一输出来源。

### 1. 生成或修订指南

为 guide 分配本轮 begin-stage；初次模式用于完整安装，原机纠错使用补救模式。通过 Agent（subagent_type: deploy-guide）传入制作、revision_id、paths、inputs、目标环境与来源、版本资料、失败证据和用户反馈。guide 负责 select-baseline、官方文档调查和明确执行范围。

结束后校验本轮已提交的两份指南、头部关联和验证契约。固定该 guide_revision，后续阶段均显式传入。补救时读取已提交 execution_scope；缺少前置条件、命令或复验范围则停下返回 guide，不把重试派发成完整安装。

### 2. 执行安装或原机补救

为 install begin-stage，显式指定 guide_revision、mode，并传本轮 inputs。通过 Agent（subagent_type: deploy-install）传制作、当前指南、修订及路径、原机 alias/instance_id/认证方式、限定范围与首次创建规格。子 agent 根据实际环境检查复用条件，必要时停止并反馈 guide 修订。

结束后读取提交记录、install_result_file、install_meta_file 及制作机器字段。机器信息以本次制作和明确快照为准，沿用已知身份，不猜别的目录补齐。失败保留原机和本轮证据；已有成功步骤、后续补装和配置变更均保留。继续派发 verify 采证；不自动无限重试或修改配置。

### 3. 只读复验

为 verify begin-stage，显式传 guide_revision 与本轮 install_revision。通过 Agent（subagent_type: deploy-verify）传制作、当前指南、安装修订、本轮路径及明确机器身份。每轮实际执行验证，旧通过结果仅作来源。

读取本轮提交与 verify_result_file，保留其整体结论和具体原因。只有真实通过、实测版本与目标一致且关联本轮安装时才进入 archive。无法判定或失败时跳过 archive，写明原报告原因和需要继续处理的缺口，不把它们一律称为软件未装好。

### 4. 打包归档

本轮 verify 通过且 instance_id 明确时，为 archive begin-stage，显式关联当前 guide_revision、install_revision 和 verify_revision。否则记录跳过原因，进入汇总。

通过 Agent（subagent_type: deploy-archive）传制作、当前指南/安装/验证、paths、inputs、机器身份及已有镜像/任务。子 agent 清理前保存稳定材料，查询已记录云操作后按清理 → 制镜像 → 切 OS 顺序执行或恢复未完成步骤；已完成操作不重复提交。整改改变镜像内容时生成并记录新镜像身份，保留前次镜像。

结束后校验本轮提交、archive_result_file、deploy_list_file 和实际镜像身份；打包成功仅表示执行完成，上传、审核和上架由用户完成。

### 5. 提交本轮流水线结果

通过 begin-stage 分配 pipeline 修订，显式传本轮 guide、install、verify 和 archive 关联，把各阶段结论及固定路径写入 paths.pipeline_result_file。按 schema 提交本轮状态、source、evidence 和 gaps。即使中途停止，也保存本轮已有结果与未执行原因；guide 失败时 pipeline 显式关联该失败修订，不能借旧 guide 冒充本轮。

pipeline-result 记录执行结论和产物索引，人工上架结果留给手动总结补充。完成后回复 production_id、各阶段结论、实际版本、镜像及汇总修订路径；提醒本次工作收尾由用户手动调用 deploy-summary，系统不自动生成总结。

## 流水线汇总模板

```markdown
# <软件名> 部署流水线结果

> 软件：<原始输入 / 稳定软件标识> | 请求/实际版本：<实际值或未知> | 日期：<日期>
> 制作：<production_id> | 指南修订：<guide_revision> | 流水线修订：<revision_id> | 模式：<mode>

## 本轮输入
- 目标机器与来源：<alias、已知 instance_id、认证方式与来源；秘密值省略>
- 计划/实际环境：<本轮明确环境及来源、差异和未知字段>
- 复用基准：<记录工具返回的版本、制作、总结修订和原因>
- 本轮执行范围：<完整安装或指南中的补救范围、保留/跳过项>

## 四阶段结论
| 阶段 | 本轮修订与真实结论 | 固定产物路径 / 未执行原因 |
|------|------|------|
| guide | <本轮提交与结论> | <本轮 install_file / verify_file> |
| install | <本轮提交与结论> | <本轮 result / meta / evidence> |
| verify | <本轮提交、结论及具体原因> | <本轮 result / evidence> |
| archive | <本轮提交与结论；或跳过> | <本轮 result / deploy_list / image；或具体原因> |

## 修改与镜像
- 用户建议：<反馈原意，注明是否已执行>
- 实际执行：<已执行变更及固定证据，保留前轮来源>
- 镜像与云操作：<实际 image_id、job_id、当前状态及前次镜像关系；未生成则注明>
- 人工上架：<尚待用户操作或已有明确反馈；执行成功不推断上架>

## 缺口与后续
<未执行步骤、无法判定原因、缺失材料与下一步；无则写无。共享 alias 日志仅作补充，列本轮稳定证据路径。>
```
