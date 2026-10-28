---
name: deploy
version: 3.0.0
description: "端到端 Ubuntu 软件部署编排：依次调用 deploy-guide、deploy-install、deploy-verify、deploy-archive，完成指南、安装、验证和打包交付。用户要求一键部署、完整部署、部署并验证、部署并打包或继续部署排障时使用。需要软件名与安装资料，目标版本和机器别名可选；无关联机器时 install 创建 ECS。"
allowed-tools: Agent, Read, Write, Bash, Glob
---

# Deploy 部署流水线

完整部署依次派发 guide → install → verify → archive；软件名称与完整产物路径按 [部署总结参考](../../deploy-summary.md) 确认，同版本覆盖对应产物。guide 默认查找已有总结作为调查参考；总结由用户独立手动调用 deploy-summary，缺少总结不影响部署。

## 编排职责与门禁

编排只定位当前软件/版本、传递参数、派发子 agent、校验本次结果和写 pipeline-result。四阶段必须分别通过 Agent 工具调用独立子 agent，依次阻塞完成，不合并、并行或内联替做。文档探索、远程安装/修改、只读验证、清理与云操作由各阶段执行；编排不抓文档，不运行远程命令或解析执行安装、验证、清理代码块。

| 阶段 | 当前产物 | 进入下步条件 |
|------|------|------|
| guide | install_file、verify_file | 本次指南生成完成；verify 头部有精确的 `> 验证契约: exit-code-v1`，否则停止 |
| install | install_result_file、install_meta_file | 安装失败仍派发 verify 采证，机器缺口如实透传 |
| verify | verify_result_file | 仅本次实际通过且对应当前安装、目标机器和版本时进入 archive；其他结论跳过 |
| archive | archive_result_file、deploy_list_file | 记录本次真实结果与镜像；未执行或失败不视为完成 |

每步完成后用 Read 校验对应产物已落盘，核对软件、版本、机器及本次调用结果；不能只检查旧同名文件存在。guide 产物缺失或失败停止后续；install 结果缺失记失败，仍执行 verify 采证；verify 报告缺失或未通过则跳过 archive；archive 报告或交付清单缺失记失败。archive 跳过原因引用 verify 的具体结论，保留软件失败、基础设施异常、检查项无法判定和指南问题的区别，不能都改写成软件未装好。

## 输入与续作

用户提供软件与安装文档/链接；版本、分支/tag、Ubuntu 版本、机器 alias 与 ECS 规格可选。文档可为 URL、GitHub 仓库、本地文件或粘贴内容。本次明确关联的 alias、instance_id、认证来源可用时沿用；同目录旧 meta 不自动把上次机器变成本次新部署目标。目标系统、架构、基础镜像、安装方式来自用户要求、解析后的 ECS 规格或明确目标机探测；未知如实说明，不能代入 agent 宿主环境。

用户还可指定参考版本或总结路径。按 [部署总结参考](../../deploy-summary.md) 的“目标版本与指定参考”区分安装目标和参考要求；把用户的参考版本、路径及原话单独传给 guide，不用参考版本填充本次目标版本或产物目录。

续作先从会话、安装 meta 和报告核对软件、版本与目标机器，按用户问题及实际进展继续相应阶段，不强制重跑完整流水线。文档调查、安装修改、只读验证和云操作仍交给各自 agent；修改后用本次验证结果决定是否归档。机器失效或关联不清时先核对，不能因连接失败自动新建机器。仅旧 state 中存在的资源身份可按需读取核对后带入现有 meta/报告，旧文件保留，新部署不依赖它。现有 run_id/task_id 可写作来源。

编排不收集和透传密码；archive 由 ecs.py 的 scope 配置获取。已有 alias 分支不能把已知 instance_id 和认证方式写空；确实缺少 instance_id 时说明 archive 跳过原因。

## 执行

### 0. 收集本次定位信息

读取共享参考，保留用户原始软件名、目标版本、输入资料、明确指定的输出路径和本次已确认的现有文件；同时收集已知目标机器及来源。新部署把这些信息交给 guide 确认官方项目与路径，此时不按用户别名或参考总结目录预设输出目录。续作已持有完整路径结果时直接沿用，缺少文件路径时按共享参考补齐。

### 1. 生成或校正指南

通过 Agent（subagent_type: deploy-guide）传入原始软件名、目标版本（若有）、文档/链接、分支/tag（若有）、目标环境与来源，以及第 0 步收集的明确路径；同时要求返回本次确认的完整路径结果。单独传入用户的参考要求（若有），已有用户反馈和当前失败现象一并提供。

guide 按共享参考负责官方项目确认、路径定位、自动查找与阅读及本次指南生成；解析警告或辅助脚本异常按其中的“警告与路径纠正”处理。结束后捕获其确认的 `software`、`project_url`、`canonical_name`（若有）、`version`、`output_dir` 和 `files`，连同必要的定位依据与调整传给后续阶段。读取 `files.install_file` 和 `files.verify_file`，并确认验证指南在首个二级标题前包含精确的 `> 验证契约: exit-code-v1`；指南缺失或标记错误即停止后续，不由编排执行验证代码块。

### 2. 执行安装

通过 Agent（subagent_type: deploy-install）传入当前软件/版本、指南路径、已知 alias/instance_id/认证方式和 ECS 创建规格，已有定时删除记录及来源一并传入。已有 alias 使用指定机器；无 alias 且没有关联实例时，由 install 读取 `deploy.config.yaml` 的 `ecs_terminate_hours`，显式传 `--terminate-hours` 创建并注册。已有机器和续作沿用原安排，配置变更不重置已创建实例的时间。

结束后读取 `install_result_file` 与 `install_meta_file`，缓存 meta 中的 `server_alias`、`instance_id`、`path`、`auth_method`、`auto_terminate_time` 及报告中的时间来源。后续 alias 以 install 实际使用并保存的值为准，尤其创建路径不可猜测。meta 缺失或某字段未知时，沿用本次已核对的调用方/会话机器信息，不把已知值写空；仍无 instance_id 则注明 archive 将跳过。安装失败仍派发 verify 只读采证。

### 3. 只读验证

通过 Agent（subagent_type: deploy-verify）传入当前软件/版本、验证指南、当前安装结果和明确目标机器。每次实际执行检查，旧通过文件不能替代本次结果。

读取本次 `verify_result_file`；只有本次实际通过、机器与安装内容对应时才进入 archive，目标版本已确定时还须与实测版本一致。无法判定、失败或报告缺失则跳过，写明报告中的具体原因或材料缺口。

### 4. 打包归档

本次 verify 通过且 instance_id 明确时，通过 Agent（subagent_type: deploy-archive）传入本次指南/安装/验证路径、目标机器、认证方式、已有定时删除记录及来源与已有 image_id/job_id/动作结果；否则跳过并说明原因。创建路径 instance_id 由 install meta 自动提供，已有机器则沿用已核对信息，编排不猜测云资源身份。

子 agent 遵循清理 → 制镜像 → 切 OS 顺序并输出交付清单；续作先核对已有云动作，进行中或已完成操作不重复提交。只剩云操作时不要求重新 SSH 清理。结束后读取 `archive_result_file`、`deploy_list_file` 与实际镜像身份。上传、审核和上架仍由用户完成。

### 5. 覆盖当前流水线结果

用 Write 写本次确认的 `files.pipeline_result_file`，记录本次四阶段结论、产物路径和未执行原因。中途失败仍写汇总，不让旧成功报告代替本次结果；缺少路径时按共享参考定位与纠正，保存位置仍有实质歧义才在回复中说明缺口。

回复软件/实际版本、各阶段结论、当前镜像及汇总路径。pipeline-result 记录执行结果；用户可手动调用 deploy-summary 整理已有经验和人工反馈，部署流程不自动运行或更新总结。

## 流水线汇总模板

```markdown
# <软件名> 部署流水线结果

> 软件：<本次确认 software> | 官方项目：<project_url 或未知> | 请求/实际版本：<实际值或未知> | 日期：<日期>
> 来源：<已有 run_id/task_id；无 Web 上下文则注明> | 目录：<本次确认 output_dir>

## 本次输入
- 目标机器与来源：<alias、已知 instance_id、认证方式；秘密值省略>
- 定时删除：<本次建机保留小时数及配置来源、记录的时刻及其时区和实际来源；未知如实记录，创建请求回显不写成云端确认>
- 计划/实际环境：<系统、架构、基础镜像、安装方式及来源、差异和未知>
- 参考要求：<用户指定版本/路径及约束；未指定写自动查找，用户排除历史则注明>
- 参考总结：<guide 实际采用的版本、文件与调整；未采用或指定材料不可用时说明原因>
- 定位与调整：<本次名称/路径的确认依据与必要调整；无则省略>
- 本次操作：<实际执行与未执行步骤>

## 四阶段结论
| 阶段 | 本次真实结论 | 当前产物 / 未执行原因 |
|------|------|------|
| guide | <本次结果> | <install_file / verify_file> |
| install | <本次结果> | <install_result_file / install_meta_file> |
| verify | <本次结论和具体原因> | <verify_result_file> |
| archive | <本次结果或跳过> | <archive_result_file / deploy_list_file / image_id；或原因> |

## 有效修改与镜像
- 实际执行：<最终有效命令、参数、配置/脚本路径及结果>
- 未执行建议：<用户建议及未执行原因，不能写成已完成>
- 镜像与云操作：<当前 image_id、job_id 与真实状态；未生成则注明>
- 人工上架：<已有明确反馈或尚待用户操作，执行成功不推断上架>

## 当前缺口
<未执行步骤、无法判定原因、缺失材料和下一步；无则写无。>
```
