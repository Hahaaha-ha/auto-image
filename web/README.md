# web — 部署会话 Web 服务端

浏览器会话式入口：新建空会话 → 输入部署指令 → SSE 实时看 agent 事件流。
事件通道两条：全局流（`GET /api/stream`，一条连接广播当前用户自己会话的实时事件、按 owner 逐帧过滤、心跳周期重验身份、常驻心跳保活）+ per-run 快照（`GET /api/runs/{run_id}/events`，按
Last-Event-ID 重放历史、重放完即断）。
多会话并行（并发上限 `WEB_MAX_PARALLEL_RUNS`，默认 10，数执行中回合——
新建、Fork、标题生成不占名额）；SDK 连接按回合开合，挂起会话零 CLI 进程。
会话三态 READY / RUNNING / ENDED：回合完成、停止、失败都回 READY 可续聊；
ENDED 只来自用户显式结束（不可续聊只能 Fork，墓碑入册）。执行中发送 409
`turn_in_progress`（想改方向先显式停止）；Fork READY/ENDED 源会建立独立
目标身份，并以 `resume=源身份 + fork_session=true` 生成独立 transcript。
会话为 `ClaudeSDKClient` 真实现（`web/sdk.py` 经工厂注入）；测试注入脚本化
假实现（`web/fake.py`），不触网、不启动真 SDK。

## 运行（本机直跑，单进程单 worker）

```bash
# Ubuntu 24.04 起 pip 受 PEP 668 管控，二选一：
pip install --break-system-packages -r web/requirements.txt   # 装进系统（本机现状）
python3 -m venv .venv && . .venv/bin/activate \
  && pip install -r web/requirements.txt                      # 或 venv 隔离
python -m web            # 默认 127.0.0.1:8123
WEB_PORT=8765 python -m web             # 换端口
WEB_HOST=0.0.0.0 WEB_PORT=8123 python -m web   # 外部可访问（见下）
WEB_STATE_PATH=/tmp/x.json python -m web       # 簿记隔离（同 HOME 多实例并行）
```

登录制多用户：`users.yaml`（范本 `users.yaml.example`）放仓库根，用户名 +
PBKDF2 哈希 + 启用状态，按 mtime 热载；Cookie 登录态（HttpOnly、
SameSite=Lax、HMAC 签名、7 天绝对过期）。会话控制面按 owner 隔离：登录
用户的会话列表、摘要、事件快照与发送、停止、Fork、结束都按 owner 授权
（他人与未知 run 同一 404，不泄露存在性）；Fork 继承源 owner；重启后归属
随簿记恢复（簿记带显式 version：v2 现代格式的 owner 归属可信，单条缺
owner 记录不补默认——保持未知归属对一切用户隐藏；无 version 的旧字符串
映射为 legacy，仅初始化标记缺席的首次启动把无 owner 历史会话与直跑
CLI transcript 统一迁移归 `WEB_DEFAULT_OWNER`（缺省 `admin`）；state
损坏或初始化完成后缺失进受限恢复——未知归属会话隐藏、控制动作 503、
读取不受影响，不自动归 `admin`）。owner 映射或簿记无法安全落盘时发送、
停止、Fork、结束与 OBS 配置修改 503 且不执行。owner 转移只能停服用
`tools/transfer_ownership.py`（自动备份 state、动作入控制审计），Web API
无转移入口。未知、禁用或已删除的 owner 字符串保留在簿记，对应会话对
当前用户隐藏，原用户名重新启用后恢复可见。控制审计落
`~/.auto-image-web/audit/`（按天轮转、默认留
90 天）；审计写失败时控制动作 503 不执行。改密/禁用即时撤销该用户登录
态；`WEB_AUTH_SECRET` 轮换全体失效。产物与 OBS 对所有登录用户共享：清
单、内容、下载、归档（本地产物与 zip 打包直传）不按 owner 过滤，归档与
OBS 配置修改先审计后执行；OBS 全局配置（凭据/桶/endpoint）只有部署管理
员（用户条目显式设置 `role: admin`）可写，普通用户 403、配置视图只读
（`can_write: false`）。全局事件流按 owner 逐帧过滤（只发当前用户自己会话
的事件；帧发送时查 run 表归属，不引入连接订阅表与全局 seq），连接存续期间
按心跳周期重验登录身份——禁用、改密（密码版本指纹漂移）或 Cookie 过期即
关流；`GET /api/runs` 随列表下发匿名全局容量（`running_count` /
`max_parallel`，跨用户合计，不含他人 run id、标题、prompt、owner 或目标
机器），前端标签栏「运行中 n/m」据此展示。

## 角色与用户清单升级

升级前停止 Web 服务，备份实际使用的用户清单（`WEB_USERS_PATH`，缺省项目根
`users.yaml`）、`~/.auto-image-web/state.json` 及其初始化标记和审计目录。
在用户清单中给选定的已有账号人工添加 `role: admin`，保留原用户名、密码哈希、
启用状态和其他字段，再重启单进程服务。不要改写会话 owner 或批量重置密码。

角色仅支持 `admin`、`user`；缺失按普通用户，未知值或错误类型使整份清单
无效，登录被拒。用户名为 `admin` 或匹配 `WEB_DEFAULT_OWNER` 都不会授予权限；
后者仅用于旧无归属会话迁移。角色和管理员密码须停服维护，维护前备份，
不要在线手工编辑用户清单。用户名原样匹配、区分大小写，旧密码继续有效。

管理员登录后可从侧栏“用户”进入用户管理，读取用户名、启用状态、管理员只读标记和
创建时间（`GET /api/admin/users`）；当前身份通过 `can_manage_users` 下发能力。
管理员可展开“新增用户”表单，或确认后启用、禁用普通用户；所有管理员记录均只读，此版本尚无重置、
删除、改名或角色编辑入口。

### 启用与禁用普通用户

`POST /api/admin/users/disable` 和 `POST /api/admin/users/enable` 接受 `username`、
`expected_version`；版本取自清单每行的 `user_version`，用户名原样传回，兼容旧名称。
仅有效管理员可调用，所有管理员（包括自己）均不可被启停。过期版本返回 409，
必须刷新清单并重新选择、确认；不同用户的变更互不冲突。

禁用前展示目标用户名及影响：永久撤销既有登录，下次 API 鉴权或最迟下次 SSE
心跳拒绝访问；执行中回合、会话 owner 和已提交的云操作不受影响，匿名容量继续计数。
启用仅恢复原使用者资格，必须重新登录，旧 Cookie 跨重启仍无效。待改密状态和创建
时间保留，缺失时间仍为“未知”；账号不能转交新人。

成功返回 200、`outcome: committed`；`audit_status: failed` 仍表示变更已生效，只是
结果审计异常，不回滚、不重复提交。文件、准备审计或状态簿记失败返回 503、
`not_committed`，启用状态和登录版本均不改变。网络结果未知时先点击“刷新清单核实”，
刷新成功前禁用启停按钮；读取当前状态后仍需重新选择并确认，不自动重试写请求。

### 新增普通用户

`POST /api/admin/users` 接受 `username`、`password`，仅管理员可调用。用户名为
1–64 位 ASCII 字母、数字、下划线、短横线或点，精确匹配、区分大小写、不裁剪；
重名返回 409，不覆盖已有用户。初始密码沿用下述 8–128 位可见 ASCII 规则。
服务端固定创建启用、待改密的普通用户，并生成 UTC 创建时间一同持久化；客户端
传入的角色、启用状态、创建时间等字段不生效。创建不撤销其他用户的登录。

管理员自行交付初始密码，系统只保存哈希、不提供回看。新用户完成首次改密后
必须重新登录才能进入业务；创建时间跨改密与重启保持。用户名不可转交新人。

成功返回 201、`outcome: committed`；`audit_status: failed` 表示用户已经创建，
仅结果审计异常，不回滚、不重提。文件或准备审计失败返回 503、`not_committed`。
网络结果未知时前端先刷新清单核实；刷新失败期间阻止继续新增，不自动重试 POST。
同名存在只能确认身份已存在，不能证明本次请求成功或密码匹配；清单没有该用户时，
管理员可重新填写并明确提交。密码在创建成功或结果未知后从表单清空。
`created_at` 已有值应为带时区的 ISO 8601 UTC 时间，例如
`"2026-09-01T02:00:00Z"`；旧记录缺失时保留缺失，页面显示“未知”，
不要用升级时刻或文件 mtime 补造。页面按浏览器本地时区显示并标明时区。

清单有效但没有启用的管理员时，普通用户仍可正常使用业务，无法从 Web
自救或自动提权。恢复管理能力必须停服备份，再由运维修正管理员角色或启用
状态。管理员仍无权查看或控制他人的会话；OBS 读取与匿名全局容量保持共享。

## 普通用户强制改密

停服备份后，可为普通用户配置 `must_change_password: true` 以独立使用此流程。
缺失字段按 `false` 处理，升级不会强制旧用户改密；管理员忽略此字段，密码继续
部署侧维护。字段必须为布尔值，非法类型使清单无效。运行期间 Web 独占用户
文件写入，禁止外部编辑器并发修改；缺失或损坏时不会重建清单或恢复管理员。

待改密用户登录或刷新后只显示改密表单，不加载业务数据或建立 SSE。服务端仅
允许 `GET /api/auth/me`、`POST /api/auth/change-password`、`POST /api/auth/logout`；
其余业务路径和请求方法均拒绝。登录验证入口仍可使用，Cookie 与同源写保护保持。
提交当前密码、新密码及确认密码；新密码为 8–128 位可见 ASCII（U+0021–U+007E），
不含任何空白、控制字符或非 ASCII，不要求组合、不裁剪，且须不同于当前密码。
原有密码不受新设规则限制，仍可用于登录与当前密码验证。

成功后待改密状态清除，全部旧登录永久撤销；服务重启后也不能恢复。必须使用
新密码重新登录，其他用户登录与创建时间保持不变。改密页过期或用户状态变化
时拒绝提交，重新登录确认最新状态。用户名持续代表同一使用者，不得转交新人。

### 用户写入复用契约

- `users.UserRoster.update_user` 是串行提交入口。锁内强制读取完整有效清单，执行
  命令的身份/启用/权限复核，检查目标版本，再写准备审计和原子替换。后续管理
  写入须复用这一入口，不得在锁外完成复核后自行写文件。新增使用同一入口的
  `create=True` 条件：仅当目标不存在才提交，并发同名只有一个成功。
- `user_changes.UserChanges` 负责命令输入、审计和结果语义。`valid_new_password`
  是新增、重置及强制改密共用的新设密码规则。新增与启停复核有效管理员身份，启停
  另复核目标为普通用户；改密复核 Cookie 有效、用户启用且仍待改密，均遵守状态簿记受限恢复和落盘门。
- `user_version` 是不透明的目标记录指纹；用户清单、待改密身份查询和登录响应提供此值，
  提交以 `expected_version` 原样带回。不同用户互不冲突。每次 Web 修改生成新的
  随机 `revision` 并写入用户记录，使编辑版本和登录指纹同时更新。撤销不依赖
  密码文本或启用状态是否恢复，旧记录不需预先补版本。后续重置必须保留
  此机制，不得删除或恢复旧版本；停服人工撤销也须换为全新值。
- 文件写入使用同目录临时文件、flush/fsync、原子替换；替换完成是提交点。保留
  其他用户、顶层配置和创建时间，只更新目标记录；文件写失败返回 503。输出为
  YAML（也可读取 JSON），注释与排版不保留；临时/替换文件使用仅属主读写权限。
- `create_user`、`change_password`、`enable_user`、`disable_user` 审计含 `actor`、`target_username`、`request_id`、`result`。
  同次操作的准备/结果共用请求标识：`prepared` 后才尝试写入，提交后记 `success`，
  拒绝或冲突记 `denied`，文件失败记 `failure`。沿用按天轮转和默认 90 天保留，
  不写密码、哈希、Cookie 或请求体。单独的 `prepared` 不能证明已提交。

| HTTP/结果 | 含义与恢复 |
| --- | --- |
| 200，`outcome: committed`、`audit_status: recorded` | 密码已生效，使用新密码重新登录 |
| 200，`outcome: committed`、`audit_status: failed` | 密码已生效但结果审计异常；仍使用新密码登录，联系管理员检查审计，不回滚或重试 |
| 422/409/503，`outcome: not_committed` | 验证、版本或文件/准备审计/状态簿记失败，密码未修改；按 `detail` 修正或重新确认 |
| 401/403 | 身份失效或无权限（含跨源），请求被拒；重新登录确认身份 |
| 网络中断、无法解析或未识别的响应 | 结果未知；先用新密码登录，失败可试原密码，两者均失败联系管理员；不自动重提 |

## v1 运行约束（多用户形态的边界承诺）

- **单进程单 worker**：全局并发计数（`running_count`/`max_parallel`）、
  409 冲突判定、SSE 全局广播与 owner ACL 都只在单进程内存里成立。禁止
  uvicorn/gunicorn 多 worker、禁止多实例共享同一 state 文件——多 worker
  下全局并发是每进程各数各的假全局语义。
- **执行资源保持共享**：repo、云凭据、产物目录、OBS 配置与目标机器
  全用户共享，多用户只隔离会话控制面（谁能看/谁能操作），不提供执行
  隔离；并行会话的产物冲突按「并行运行约定」人工规避。
- **不引入数据库**：状态是内存 RunManager/EventStore + 本地簿记
  （`state.json`）+ 控制审计文件（JSONL 追加）。单实例低写入量下数据库
  不会让这三者自动跨进程一致；未来多实例需重新设计共享状态、分布式
  并发锁与事件广播。
- 状态簿记与审计文件只落在服务端本地（`~/.auto-image-web/`），无 Web
  下载能力。

默认只监听 127.0.0.1。需要外部机器的浏览器访问时，`WEB_HOST=0.0.0.0`
绑定全部网卡，经 `http://<本机IP>:<端口>/` 访问——暴露面由运行者的网络
策略（安全组/防火墙）控制，风险自担。

前端两种打开方式：

- 生产形态：`cd web-ui && npm run build` 后直接访问 `http://127.0.0.1:8123/`（FastAPI 挂载 `web-ui/dist`）；
- 开发形态：`cd web-ui && npm run dev` 后访问 `http://127.0.0.1:5173/`（`/api` 由 Vite 代理到 FastAPI 的 8123 端口）。

## 前端交互边界

- 主区是一层混合标签栏，会话与产物文件同栏；左侧「会话 | 产物」只负责
  导航。侧栏会话按最后活动降序，打开的标签保持自己的工作区次序。
- header 与底部操作条绑定最后激活的会话。激活文件标签页时，输入框折叠为
  会话作用对象行，需先点回会话再输入；若该会话正在执行，「■ 停止」仍可用。
- 快照与全局实时流都按 per-run `seq` 寻址、排序、去重并重算派生态；摘要
  只补尚未加载的事实，不能用旧状态覆盖更新事件。
- RPM 等二进制产物在 Markdown 解析前分流，只显示元数据、不可预览说明与
  原始文件下载；Markdown 与 JSON 仍按文本方式显示。

## 并行运行约定（产物冲突，人工规避）

产物目录 `deploy/{{software}}/{{version}}` 全实例共享、无服务端隔离（指令
文本里的软件/版本表述不可穷尽，服务端拦截必漏且给「会拦」的错觉）。并行
会话时的约定（真部署实测，同软件同版本并行 7 个产物文件全量覆盖）：

- **同软件 + 同版本不要同时运行**：后写入者覆盖先写入者（指南、meta、
  结果、清单全部），以最后落盘内容为准，无合并。
- 同目录并行时 verify 读 output_dir 全目录，会看到对方中途落盘的文件；
  交叉读取的结论只对各自的机器安装有效，产物归属已乱。
- 同一目标机器 / SSH alias / ECS / 镜像任务不要交叉使用——两路对同一台
  机器并发安装会产生半成品系统。
- 前端「正在跑：…」RUNNING 标题提示是唯一防线，新建会话时肉眼避开。

## 测试（主缝：HTTP 进、SSE 出）

```bash
python web/tests/test_api.py        # ASGI 主缝（假会话驱动）
python web/tests/test_artifacts.py  # 产物端点（临时目录造桩）
python web/tests/test_auth.py       # 登录/登出/过期/禁用/密钥轮换、同源校验、审计文件
python -m web.tests.test_password_change # 强制改密、全面限制、并发、重启与文件/审计故障
python -m web.tests.test_create_user # 管理员新增、首次登录闭环、同名竞争和故障结果
python -m web.tests.test_user_access # 管理员启停、永久撤销、SSE/回合语义、并发和故障结果
python web/tests/test_ecs_api.py    # ECS 面板端点（假云函数注入）
python web/tests/test_events.py     # 事件存储、快照与全局订阅
python web/tests/test_fixture_isolation.py # 通用 fixture（含验收装配）的生产依赖哨兵
python web/tests/test_history.py    # 列表摘要、可续聊约束、假 transcript 驱动的重启重放
python web/tests/test_multi_user_acceptance.py # 多用户集成验收（三客户端端到端 + 重启/撤销回归）
python web/tests/test_normalize.py  # 消息映射与阶段推导纯函数断言
python web/tests/test_obs_api.py    # OBS 端点（假云函数注入）
python web/tests/test_obs_config.py # OBS 配置加密落盘与脱敏视图
python web/tests/test_owner_acl.py  # owner 会话隔离（双认证客户端互不可见/不可控）
python web/tests/test_redact.py     # 事件出口脱敏（形状正则 + 已知值清单）
python web/tests/test_shared_resources.py # 产物/OBS 共享读取、归档审计、OBS 配置 admin-only
python web/tests/test_stream_isolation.py # 全局流 owner 逐帧过滤、心跳身份撤销、匿名容量
python web/tests/test_sdk.py        # options 契约（身份、Fork、系统提示词、无值守写权限）
python web/tests/test_recovery.py # 安全恢复（版本化簿记/legacy 迁移/受限恢复/落盘门/人工转移）
python web/tests/test_state.py      # 身份映射、墓碑与 Fork 来源簿记
python web/tests/test_tasks.py      # 任务面板与手动建任务端点
python web/tests/test_title.py      # 标题生成（prompt/清洗/一次性会话/幂等/写回）
python web/tests/test_transcript_times.py # transcript 时刻读取
cd web-ui && npm test && npm run build    # 前端完整测试与生产构建
```

## 模块

| 文件 | 职责 |
| --- | --- |
| `app.py` | FastAPI 应用工厂、API 路由（含 `GET /api/runs` 列表，按当前登录用户过滤 owner，随列表下发匿名全局容量 running_count/max_parallel）、SSE 通道两条（全局流常驻广播 + owner 逐帧过滤 + 心跳周期重验身份；per-run 快照：id=seq、Last-Event-ID 重放、重放完即断）、启动接线（重放恢复 + 簿记状态分类——legacy 首启迁移 / 受限恢复判定 + 初始化标记落盘 + 残留 CLI 告警）、控制动作双门（审计前置 503 + 簿记落盘门 503，受限恢复下整体阻断） |
| `auth.py` | PBKDF2 口令校验、HMAC 签名 Cookie（携带用户版本指纹）、认证密钥解析 |
| `users.py` | 文件用户清单读取、目标版本、串行复核与原子替换，持久 revision 撤销旧 Cookie |
| `user_changes.py` | 用户变更命令、新设密码规则、准备/结果审计、未提交与已提交反馈 |
| `audit.py` | 控制审计（本地追加 JSONL，按天轮转默认留 90 天）：登录/登出与会话控制动作，带 actor、动作、run id、run owner、结果、拒绝原因与 request id；写失败抛 `AuditWriteError` 由控制动作 503 阻断 |
| `runs.py` | 会话状态机（READY/RUNNING/ENDED 三态、无全局门禁）、owner 归属（创建注入、Fork 继承）、校验/置位分离（check_* 纯校验 + commit 置位，控制动作审计前置用）、回合计数（`WEB_MAX_PARALLEL_RUNS`）、Fork/end 校验与 409 判定收敛（turn_in_progress / session_running / parallel_limit_reached / session_not_active） |
| `events.py` | 进程内事件存储：seq 递增、快照重放、全局订阅唤醒 |
| `session.py` | 回合执行（send 起回合级 asyncio.Task，SDK 连接只包住一个回合；停止意图覆盖连接建立前与 query 前的启动窗口） |
| `normalize.py` | SDK 消息 → 内部事件映射、阶段推导 |
| `artifacts.py` | deploy/ + rpm/ 多根全量产物浏览（目录分组 + 最新落盘排序，约定文件带阶段徽标）、内容读取、单文件下载与批量 zip、路径约束 |
| `redact.py` | 事件出口脱敏（运行时已知值清单 + AK/SK、密码字段、私钥块形状正则） |
| `rebuild.py` | 服务重启后的恢复（单一流程）：全量 transcript 按 session 粒度重放 + state 簿记叠加——身份映射命中的沿用原 run_id 并恢复 owner 归属，墓碑会话标 ENDED；其余重放会话 READY 可续聊，未收尾回合（transcript 推导 turn_open）补 `turn.interrupted` 不伪造完成 |
| `state.py` | 恢复簿记（`~/.auto-image-web/state.json`，全量原子替换，带显式 version）：墓碑（用户 ENDED 的 session_id 集合）+ 身份映射（run_id ↔ session_id）+ Fork 来源镜像（session_id → 来源 run_id）+ owner 归属（run_id → 用户；与身份映射同生命周期登记）（stage/title/first_prompt 从 transcript 重放推导）；读取按状态分类（modern/legacy/missing/corrupt）支撑受限恢复判定，strict 落盘失败抛 `StatePersistError`（控制动作 503 门）；初始化标记（state 同名 `.initialized`）区分首次启动与初始化后缺失 |
| `title.py` | 会话标题 LLM 生成（Codex 同构，research/codex-session-title.md）：首条指令到达即起一次性无工具会话生成，成功落 run.title + `session.title_changed` 事件 + transcript custom-title 行；失败静默维持截断标题；Fork 会话继承源标题不再生成 |
| `sdk.py` | ClaudeSDKClient 生产实现：目标身份/上下文来源/Fork 启动意图、options 全配、消息形状适配、工厂、历史读取包装 |
| `fake.py` | 脚本化假会话（默认剧本含敏感样例），测试注入用 |

## SDK 与浏览器实测记录（claude-agent-sdk 0.2.144 + CLI 2.1.220）

接入真实现时逐项实测的结论，均为实际运行观察、非文档推断：

1. **消息形状**：`receive_response` 产出 dataclass（`AssistantMessage` 等），
   `sdk.to_dict` 适配成 CLI JSON 形状 dict 后进 `normalize_message`，
   与假剧本同一条映射路径。Assistant / Result / partial 直接携带的
   `session_id` 及 System init 内的同名字段都会保留，供回合尽早确认身份；
   每回合终止于 `ResultMessage`。SDK 本身允许同连接继续 `query`，Web 则在
   回合收尾后关闭连接，下一回合以自身 `session_id` 新建 resume 连接。
2. **子 agent thinking 转发**（方案风险点一）：`forward_subagent_text=True`
   下子 agent 的 thinking 块**会**随文本一并转发（parent_tool_use_id 非空的
   assistant 消息里实测出现 ThinkingBlock），子 agent 思维链在前端可见。
3. **子 agent 工具名**：CLI 现名 `Agent`（system init 的工具注册表里仍可见
   旧名 `Task`）。阶段推导两者都接受（`normalize.SUBAGENT_TOOL_NAMES`），
   真实 `Agent` 调用带四类 subagent_type 时已实测发出 `stage.changed`。
4. **tools 必须显式给 `claude_code` 预设**：SDK 不配 `tools` 时 CLI 基础
   工具集不含子 agent 工具（agent 自查工具目录无 Task/Agent），四阶段
   流水线无从推进——`--tools default` 后才有。
5. **interrupt 行为**：回合执行中 `interrupt()` 后消息流**自然终止**
   （`receive_response` 迭代器结束），尾随一条 `subtype="error_during_execution"`、
   `is_error=True`、`result=None` 的 Result（后接的 UserMessage 为被中断
   工具的错误 tool_result，照常走 tool_finished 映射）；同一会话随后
   `query` 续聊正常，打断前的上下文保留。服务端以自己的 `stop_requested`
   标记区分 `turn.stopped` 与 `turn.completed`，不解析该 Result 的文案。
6. **联网链路**（方案风险点三）：SDK 会话内内置 `WebFetch` 被域名安全校验
   拦截（"Unable to verify if domain ... is safe to fetch"）、`WebSearch`
   在权限层被拒——与仓库 CLAUDE.md 记录一致。已按方案经 options 的
   `mcp_servers` 接入既有 exa MCP，并因非交互会话无人批准 MCP 工具而
   配 `allowed_tools=["mcp__exa-search__*"]` 放行；复测抓取 nginx.org
   成功。内置工具中只读 Bash 随 `claude_code` 预设放行；写路径需
   `permission_mode`（见第 8 条）。
7. **回合上限**：`max_turns=200` 是终局语义：Result 的错误 subtype（兜底
   判定：只有 `success` 是正常完成）以 `turn.failed` 收尾、会话回 READY
   可续聊（重试 = 下一条指令新连接）。**无 wall-clock 超时**（曾有 3600 秒
   上限，已删）：单回合即完整部署流水线，四阶段串行 + 云操作轮询（IMS 制
   镜像）可超小时级，服务端主动掐断会把已提交的云操作留在中间态。

8. **无值守会话的写权限**（真部署实测）：`claude_code` 工具预设只放行
   只读 Bash，Write 与 Bash 写路径一律被权限系统拦截（guide 只能把指南
   全文以文本返回）——options 必须配 `permission_mode="bypassPermissions"`。
   信任边界由运行形态承担：只监听 127.0.0.1 + 系统提示词任务边界。
9. **凭据值的两层脱敏**（真部署实测）：形状正则防不住自然语言内联
   （`` password `pcb…@@` ``、`password is set (…)` 出现在 thinking），
   redact 层除形状外还维护运行时已知值清单（启动时从 scope.yaml 登记
   ak/sk/password 值，任意上下文整值遮蔽）；实测华为云 SK 为 38 位大小写
   混合，非注释曾以为的 40 位小写。
10. **子 agent 的异步派发失稳**（真部署实测）：CLI 的 Agent 工具支持异步
   启动，模型可能派发后结束回合「等通知」——通知无处投递、回合提前收尾；更早一轮还出现过并发派发上百次 guide 的调度风暴（20 实例
   触发 429，agent 自行终止后恢复）。系统提示词以执行纪律约束：至多一个
   子 agent 在跑、派发后 TaskOutput 阻塞等待、四阶段完成才收尾回合。
11. **interrupt 的终止边界**（干预语义实测，脚本经 `web.sdk` 工厂走生产路径）：
   回合执行中的本地 Bash 子进程**随打断被终止**（实测 `sleep 222` 在
   interrupt 后即刻消失），CLI 子进程保留、连接可续聊。已提交的云操作
   （HTTP API 类：创建 ECS、制镜像等）不受任何影响——打断只作用于后续
   动作，界面在 `turn.stopped` 块与停止按钮上如实提示「已提交的云操作
   不受停止影响，无法撤销」。
12. **cancel（断连）的终止边界**：回合执行中直接断开 SDK 连接（= run_task
   取消后 `__aexit__` 的路径）后 3 秒内：CLI 子进程**全部退出、无残留**
   （配合服务重启语义中的 pgrep 告警兜底），正在执行的本地 Bash 子进程
   同样被终止（实测 `sleep 333` 消失）。远程命令经 ssh 转发：客户端进程
   被杀断开连接，远端进程是否终止取决于远端 shell 配置，**不保证**——
   按「已提交的云操作不可撤销」对待。断连后以 `resume=session_id` 新建
   会话实测可续接，上下文完整（能复述被打断前的指令）。
13. **重启重放的 transcript 形状**（历史列表实测，本机 119 条真实会话、
   全量重放约 2 秒）：`get_session_messages` 只回可见的 user/assistant 链
   （isMeta / isSidechain 已滤），user 行 content 可为字符串（含 CLI 命令
   包装）或块列表（tool_result 回填），无 Result 消息——回合边界由「下一
   条真实用户输入」推导、回合汇总取该回合最后一条 agent 文本；重放的
   run 状态 READY（可续聊可 Fork），流无终态收尾事件、快照重放完即断
   等待续聊。`list_sessions(directory=项目根)` 的 first_prompt 即任务名来源。
14. **服务重启的恢复**（state 簿记 + 真 SDK 实测）：簿记只存墓碑（用户
   ENDED 的 session_id）、身份映射（run_id ↔ session_id）与 Fork 来源镜像
   （session_id → 来源 run_id），每次状态变更即全量原子写。首回合一经接受
   便在异步任务启动和首次落盘前预分配 UUID；从未接受回合、没有 transcript
   的空会话仍不恢复。重启后全量
   transcript 重放：映射命中的以原 run_id 恢复可聊——send 起的回合以
   自身 session resume 新连接（实测恢复后发消息，agent 记得重启前的
   约定）；墓碑会话保持 ENDED 不复活。未收尾回合（末回合无下一条输入
   收口）补 `turn.interrupted` 不自动重跑（已提交的云操作不可重复执行）。
   簿记损坏/缺失一律降级为无墓碑无映射的重放，不阻断启动。kill -9 实测：
   崩溃窗口内丢失的最后一次状态变更由 transcript 存在性校验兜底（读不到
   即丢弃）。
15. **按回合开合的连接生命周期**（真部署并行实测，两路 nginx/redis 全
   流水线 + 调研回合）：
   - **每回合 CLI 启动开销 5-7 秒**（指令发出 → 首条 assistant 响应，
     transcript 时间戳实测；含 CLI 冷启动 + exa MCP stdio 握手），续聊
     回合同量级——按回合开合没有摊薄启动的复用红利，也换来回合间零
     进程；秒级成本对分钟级部署流水线可忽略。
   - **挂起会话零 CLI 进程**：全实例无 RUNNING 回合时，web 进程名下
     CLI 子进程为 0（ps 归属实测）；执行中每回合恰一个 CLI 主进程 +
     一个回合级 exa MCP（npm exec）子进程，标题生成的一次性会话约 8
     秒即退、不常驻。
   - **异常回合后新连接续聊完整**：kill -9 服务截断的回合，重启重放呈
     `turn.interrupted`，随后 resume 同 session 发消息实测可续接（agent
     记得截断前在做什么）；interrupt 停止后的回合续聊同样完整。回合异
     常不污染 session 身份——身份在首回合接受时预分配，并由最早携带
     session_id 的 SDK 消息确认；不一致会让回合失败而不会改写目标身份。
   - **并行资源形态**：双部署回合并行 = 两个独立 CLI 进程树（互不共享
     MCP/连接），内存开销随执行中回合线性增长，并发上限即资源护栏。
16. **Fork 的 transcript 归属**（受控真实 SDK 复验）：生产 adapter 以
    `session_id=目标`、`resume=源`、`fork_session=true` 建立首个 Fork 回合。
    源 SID `9e27cff8-fa01-46f9-97a6-a683a733badc` 与 Fork SID
    `10925496-3941-4bf8-8076-32ad8d13915a` 对应两份不同 JSONL；两边共享
    分叉前的 BASE 指令，分叉后源只含 SOURCE_ONLY/RESTART_SOURCE，Fork
    只含 FORK_ONLY/FORK_FOLLOW/RESTART_FORK。Fork 后续回合和服务重启后
    都只 resume 自身 SID；结束源会话再重启，源保持 ENDED、Fork 保持 READY，
    两个原 run_id 均稳定且没有 `run_hist_*` 副本。
17. **SDK 启动窗口内立即停止**（浏览器 + slow-enter adapter）：发送成功后
    在 `__aenter__` 尚未放行时停止，放开连接后 `query_calls=0`、
    `mock_cloud_actions=0`、`interrupt_calls=0`。事件恰为
    `session.started → turn.started → user.message → turn.stopped`，会话回
    READY，下一条指令正常完成；这证明停止意图由回合握手消费，而非依赖
    当时是否已有可中断连接。已提交的云操作不可撤销边界不变。
18. **快照与实时流收敛**（真实浏览器）：tail→旧 snapshot 与
    snapshot→tail 两种到达顺序都严格收敛为 seq `[1,2,3,4,5]`，断开全局
    SSE 后由快照补齐至 `[1..9]`，重复为 0；状态与服务端一致。客户端按
    per-run seq 寻址、排序、去重并重算派生态，快照游标取最大已见 seq。
19. **二进制产物**（真实浏览器）：无 `content` 的 RPM 在 Markdown 解析前
    分流，显示文件名、`12.0 KB`、不支持在线预览说明与下载动作；下载得到
    12,292 bytes，魔数 `ed ab ee db`。Markdown 与 JSON 视图保持原行为。

- CLI stderr 对本环境网关模型名报 `[claude-code:unrecognized_model]`
  警告，不影响会话执行，服务日志如实记录。
- `include_partial_messages=True` 带来大量 partial/system 消息
  （`thinking_tokens` 估算等），`to_dict` 只保留其中可用的 session_id；无
  `type` 的结果仍不进事件流。
