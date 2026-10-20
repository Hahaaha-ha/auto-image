# 制作记录交接约定

所有 Ubuntu deploy 阶段、编排和手动总结先读取本文。固定工作目录为项目根；用 `python scripts/deploy_record.py --help` 查看入口，先运行 `python scripts/deploy_record.py schema` 读取当前契约。**正式字段、枚举、默认路径和校验只维护于 `deploy_records/contract.py` 与统一记录模块**；下文示例中的变量取工具返回值，不另造状态、原因码或目录规则。

## 制作身份

- 新制作使用 `create --software <输入名> --version <原始版本串> --data <JSON文件>`。同软件同版本也新建；`create` 返回随机 `production_id` 及记录路径。版本参数只是请求值，不能当作实测身份。
- 续作明确 `production_id`，用 `show --production-id <id>` 读取。需要定位时用 `list` 的软件、版本或 `--call-id` 条件列出；多条结果由用户指定，不能按 mtime、最新目录或事件序号猜选。install、verify、archive 从明确制作或指南头读取身份，不创建制作。
- 用 `update --production-id <id> --data <JSON文件>` 保存已核实的实际版本、大版本依据、标签前缀确认、目标环境或新增来源；键名及结构取 schema。实际版本变化或目标机器失效后需要新建机器时，新建制作并用来源关联旧制作，不能把旧制作改成另一身份。
- Web 执行上下文中已有的 run_id、call_id、task_id 按 schema 的 source 原样保存；它们与 production_id 分别维护。续作追加当前来源，保持制作身份。没有 Web 上下文时留空。调用标识由 Web 提供，不从会话事件序号拼接，也不复用上一回合标识。
- 软件别名只使用 deploy.config.yaml 中已确认的统一映射，原始输入与来源保留。实际版本及环境字段未知时如实记录。目标环境来自用户要求、解析后的 ECS 规格/基础镜像或明确目标机的只读探测，不来自 agent 宿主机。

## 阶段修订及交接

编排先调用：

```bash
python scripts/deploy_record.py begin-stage --production-id <id> --stage <本阶段> --guide-revision <已提交指南修订> --mode <本轮模式> --data <本轮关联JSON文件>
```

guide 首轮无需 guide-revision；其补救修订可把前轮关联放入 data。pipeline 可以明确关联本轮失败 guide，只作执行收尾；它不能绕过后续阶段的指南通过门禁。data 的 inputs 及其他允许字段按 `--help` 和 schema 填写。编排把返回的 **production_id、revision_id、mode、guide_revision、paths、inputs** 原样传给阶段 agent；已经分配的修订不再 begin。直接调用阶段时由该 agent 分配本轮修订，guide 可先初始化新制作。

- `paths` 是本轮输出；`inputs` 是明确已提交的上游指南及相关安装、验证。使用返回路径原文，不按软件/版本自行拼路径，不从输出目录搜同名旧文件。编排可明确传 inputs 的安装与验证修订；archive 必须绑定当前通过的验证及它描述的安装。
- guide 为每轮指南写两份独立文件；下游读取本轮已提交 guide 的 install_file / verify_file。报告、meta、配置和脚本写本阶段独立修订，已提交修订保持不变。
- 补救 guide 的 execution_scope 结构取 schema，内容须包括可核对前置状态、实际命令、保留/跳过项、复验要求和系统更新开关。install 核对原机身份与前置状态，只执行该范围；前置状态不成立则停止反馈。原机补救没有默认系统更新、重建 ECS 或重复云操作。
- `set-machine --production-id <id> --data <JSON文件>` 保存机器。已知 alias、instance_id、认证方式及来源持续保留；本轮未取得的字段不写空覆盖。取得 ECS id 后立即落盘，失败仍保留；密码、私钥及 token 不进入 JSON 或正文。

各阶段开始前读输入，成功或失败收尾都写必要输出和证据，再提交：

```bash
python scripts/deploy_record.py commit-stage --production-id <id> --revision-id <本轮修订> --data <实际结果JSON文件>
```

JSON 使用 schema.stage 样例的正式字段和允许取值。实际执行命令写入 commands（实际 command、exit_code、脱敏输出），同时保存 evidence；补救 install 的 commands 只承载 execution_scope.commands 中实际执行的命令，前置只读探测另存 evidence。实际环境由 install 记录，实测版本由 verify 记录，镜像与云操作由 archive 记录。不能把目标版本抄成实测版本或把旧轮结果写成本轮执行。

成功提交需具备本阶段全部必需产物；失败无法生成正常产物时，写返回的 `paths.stage_result_file` 作为本轮失败报告，保留原始现象与证据，未执行步骤在正文和 gaps 明确说明。`commit-stage` 拒绝时，修正本轮材料再提交；未提交草稿不作为下游通过证据。编排检查提交状态、当前 revision_id 及 required 输出路径，不能仅检查旧同名文件存在。

## 稳定证据

本轮 `evidence_dir` 保存实际命令及结果、有效非敏感参数、必要配置、安装/启动脚本和云返回。共享 alias 日志只作补充；远程路径、临时文件或会滚动的日志不能作为唯一证据。清理前先保存会被删除的材料。每取得云镜像或任务 id 立即写本轮操作证据。中断恢复先用 `stages --production-id <id> --include-drafts` 发现同制作的草稿；archive 的 inputs.previous_archives 也包含这些修订标识。读取对应固定 paths 下的本轮操作证据，查询云状态，明确区分已执行、仍在进行和未完成步骤，再接续尚未完成部分。草稿仅作状态发现与恢复来源，不作已提交或通过证据。

已有固定材料可引用其已提交修订路径；需要复制时使用入口 `snapshot`（先查看其 `--help`）或将脱敏内容写到当前 evidence_dir。evidence 传相对项目根的真实文件路径，保留来源、配置原路径与采集时间；提交校验引用并固定内容。秘密值只记参数名和获取方式，保留其余实际有效内容。用户建议与 agent 实际执行分开记录，未知原因、未执行验证、材料缺口明确说明。

每阶段报告头写制作、当前指南及本轮修订标识；正文列执行/验证范围、固定证据和缺口。阶段回复返回标识、路径、真实结论及提交状态，便于下一阶段准确交接。

## 基准与总结

guide 在核实身份与目标环境后调用 `select-baseline --production-id <id>`，读取返回的选择及排除原因。版本比较、环境匹配、提交完整性与候选资格由记录模块统一校验；agent 不另做字符串/mtime 排序。读取所选已提交总结和证据，再适配本次目标版本与环境；无候选、关键环境未知或版本不可可靠比较时查目标版本官方资料。

手动总结使用独立 deploy-summary skill。流水线仍只有 guide → install → verify → archive；pipeline-result 只记本轮执行及索引，上架由用户反馈，成功打包不能替代人工上架。总结仅整理本地持久证据和文档，不调用远程或云操作，不自动触发阶段或创建部署任务。
