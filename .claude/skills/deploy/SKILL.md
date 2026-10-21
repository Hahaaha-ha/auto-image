---
name: deploy
version: 2.1.0
description: "端到端 Ubuntu 软件部署编排：依次调用 deploy-guide、deploy-install、deploy-verify、deploy-archive；同软件同版本覆盖当前产物，也用于用户反馈后的原机补救。用户要求一键部署、完整部署、部署并验证、部署并打包或继续整改时使用。软件名与目标版本/官方项目资料必需；已有机器别名可选，无别名时仅首次 install 创建 ECS。版本文档总结由用户手动调用 deploy-summary。"
allowed-tools: Agent, Read, Write, Bash, Glob
---

# Deploy 部署流水线

先读取 [软件版本文件交接约定](../../deploy-records.md)，运行记录入口 schema。完整制作依次派发 guide → install → verify → archive；复用发生于 guide 输入选择，不省略阶段。四阶段文件和总结都放在固定 `deploy/<software>/<version>/`，同版本覆盖当前文件。总结由用户独立手动触发。

## 编排职责与门禁

编排只定位当前软件/版本、传递参数、派发子 agent、校验本次结果和写 pipeline-result。文档探索、远程安装/修改、只读验证、清理与云操作分别由四个独立 Agent 执行，顺序阻塞完成，不内联替做。

| 阶段 | 当前产物 | 进入下步条件 |
|------|------|------|
| guide | install_file、verify_file | 本次指南生成完成；verify 头部有精确的 `> 验证契约: exit-code-v1`，否则停止 |
| install | install_result_file、install_meta_file | 安装失败仍派发 verify 采证，机器缺口如实透传 |
| verify | verify_result_file | 仅本次实际通过且对应当前安装、目标机器和版本时进入 archive；其他结论跳过 |
| archive | archive_result_file、deploy_list_file | 记录本次真实结果与镜像；未执行或失败不视为完成 |

每步完成后 Read 当前报告和状态，核对软件、版本、机器及本次调用结果；不能只检查旧同名文件存在。guide 失败停止后续；install 失败仍执行 verify；verify 未通过时按报告具体结论原因写 archive 跳过原因，保留软件失败、基础设施异常、检查项无法判定和指南问题的区别。

## 输入与续作

用户提供软件、目标版本/官方资料、部署要求及目标环境。已有 alias、instance_id、认证来源可用时沿用。目标系统、架构、基础镜像、安装方式来自用户要求、解析后的 ECS 规格或明确目标机探测；关键字段未知由 guide 按官方资料生成，不能代入 agent 宿主环境。

新制作或续作都明确软件、版本及目标机器。同软件同版本重跑覆盖当前文件，不生成额外制作或修订身份。现有 run_id/task_id 可作为来源，现有云 id 保持资源语义。用户反馈后沿用原机，先交 guide 根据当前状态明确补救范围，再派发 install；机器失效或关联不清时停止反馈，需要另建机器时先明确目标与当前状态，避免误用旧机器。

编排不收集和透传密码；archive 由 ecs.py 的 scope 配置获取。已有 alias 分支不能把已知 instance_id 和认证方式写空；确实缺少 instance_id 时说明 archive 跳过原因。

## 执行

### 0. 定位当前版本文件

读取 deploy.config.yaml 和 schema，通过 paths/show 定位固定版本目录、当前状态及已知机器。软件/版本及可用来源显式传给阶段 agent；版本未核实可使用配置占位，guide 核实后更新实际版本。guide 先按明确查询数据 select-baseline 并读取所选总结与必要材料，之后才 begin-stage；不能在读取同版本基准前重置当前文件。

### 1. 生成或校正指南

通过 Agent（subagent_type: deploy-guide）传入软件、版本、paths、目标环境及来源、官方资料，以及用户反馈和当前失败现象。首次完整制作明确 guide 模式 initial，清旧机器/cloud/版本身份后重写本次明确输入；原机补救明确 guide 模式 remediation，保留机器/cloud 并传入前置状态及要修正内容。

guide 负责用已核实版本/大版本依据/目标环境查询 select-baseline、读完基准及材料后 begin-stage，再做官方调查与适配并覆盖指南。结束后读取本次两份指南，检查实际目标版本、目标环境和验证契约。补救范围缺少前置条件、命令或复验要求时返回 guide，不能把重试派发成完整安装。

### 2. 执行安装或原机补救

通过 Agent（subagent_type: deploy-install）传入当前软件/版本、指南路径、模式、已知 alias/instance_id/认证方式、补救范围和首次创建规格。install 在 begin-stage 开始时清当前安装和下游旧结果、旧 summary 及其上架资格，保留机器与云操作身份；子 agent 探测实际环境，关键筛选条件不再满足或已知不兼容时停止套用并反馈 guide。

结束后读取 install_result_file、install_meta_file 和当前状态。保留已知机器身份，安装失败仍派发 verify。失败只在当前报告说明，不要求额外失败归档；纠错后当前文件整理实际有效步骤、参数及所需配置/脚本。

### 3. 只读验证

通过 Agent（subagent_type: deploy-verify）传入当前软件/版本、验证指南、当前安装结果和明确目标机器。每次实际执行检查，旧通过文件不能替代本次结果。

读取本次 verify_result_file 与当前状态；只有本次实际通过、实测版本与目标一致、机器和安装内容一致时才进入 archive。无法判定或失败则跳过，写报告中的具体原因。

### 4. 打包归档

本次 verify 通过且 instance_id 明确时，通过 Agent（subagent_type: deploy-archive）传入当前指南/安装/验证路径、目标机器及 state.cloud 当前 image_id/job_id/操作状态；否则跳过并说明原因。

子 agent 清理前保存当前必要配置/脚本，先读取 state.cloud 并查询当前云动作，再遵循清理 → 制镜像 → 切 OS 顺序。进行中或已完成操作不重复提交；整改改变镜像内容时更新当前镜像身份和报告。结束后读取 archive_result_file、deploy_list_file 与实际镜像身份。上传、审核和上架仍由用户完成。

### 5. 覆盖当前流水线结果

写 paths.pipeline_result_file，覆盖当前四阶段结论、有效产物路径和未执行原因，并通过 commit-stage 更新当前 pipeline 状态。中途失败仍说明本次结论和后续缺口，不另建失败档案。

pipeline-result 只记录执行结果，人工上架反馈由手动总结补充。回复软件/实际版本、各阶段结论、当前镜像及汇总路径；本次成功制作收尾由用户手动调用 deploy-summary，系统不自动运行总结。

## 流水线汇总模板

```markdown
# <软件名> 部署流水线结果

> 软件：<输入名 / 稳定软件标识> | 请求/实际版本：<实际值或未知> | 日期：<日期> | 模式：<首次安装或原机补救>
> 来源：<已有 run_id/task_id；无 Web 上下文则注明> | 目录：<output_dir>

## 本次输入
- 目标机器与来源：<alias、已知 instance_id、认证方式；秘密值省略>
- 计划/实际环境：<系统、架构、基础镜像、安装方式及来源、差异和未知>
- 复用基准：<软件版本、summary 路径、选用原因；无则说明>
- 执行范围：<完整安装或明确补救命令、保留/跳过步骤>

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
<未执行步骤、无法判定原因、缺失材料和下一步；无则写无。不额外归档失败过程。>
```
