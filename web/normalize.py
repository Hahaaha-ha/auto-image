"""SDK 消息 → 内部事件映射与阶段推导。

SDK 流没有独立的 tool started/finished 事件，从消息结构推导：
- Assistant 的 thinking / text / tool_use 块 → agent.thinking / agent.message / agent.tool_started
- User 的 tool_result 块 → agent.tool_finished
- 子 agent 工具调用带流水线 subagent_type 时，先发 stage.changed
  （工具名集合见 SUBAGENT_TOOL_NAMES）

tool_use 与 tool_result 仅以 id 关联，工具名映射由调用方逐回合维护。
"""
import json

from .redact import redact_text

# 流水线阶段的唯一字面值来源（阶段名枚举只写一处）：事件推导与产物
# 徽标（artifacts.CONFIG_KEY_STAGES）都取这些值，加阶段改这里 + config 键
# BUILD 为 RPM 流水线独有（制作 RPM 包），镜像流水线无此阶段
GUIDE, INSTALL, BUILD, VERIFY, ARCHIVE = "GUIDE", "INSTALL", "BUILD", "VERIFY", "ARCHIVE"

STAGE_BY_SUBAGENT = {
    "deploy-guide": GUIDE,
    "deploy-install": INSTALL,
    "deploy-verify": VERIFY,
    "deploy-archive": ARCHIVE,
    "rpm-guide": GUIDE,
    "rpm-build": BUILD,
    "rpm-verify": VERIFY,
    "rpm-archive": ARCHIVE,
}

# 子 agent 工具的 CLI 名：新名 Agent，Task 为旧名/事件重放剧本兼容
SUBAGENT_TOOL_NAMES = {"Agent", "Task"}

# 摘要行的主参数字段：已知工具取最能代表这次调用的入参（Read 的路径、
# Bash 的命令……）；未命中（含 MCP 工具）由 primary_arg 兜底
PRIMARY_INPUT_FIELD = {
    "Read": "file_path",
    "Edit": "file_path",
    "Write": "file_path",
    "NotebookEdit": "notebook_path",
    "Bash": "description",
    "Grep": "pattern",
    "Glob": "pattern",
    "WebFetch": "url",
    "WebSearch": "query",
    "Agent": "description",
    "Task": "description",
}

TOOL_SUMMARY_LIMIT = 120
# 完整内容的截断上限：事件全量驻内存并经 SSE 重放（web/events.py），超长
# Bash 输出须有界；单机单用户量级下每条 4K 可承受
TOOL_DETAIL_LIMIT = 4000


def _plain(value):
    """dict 行内取值：str 原样，其余紧凑 JSON（标量直读，嵌套结构不展开）。"""
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=False, default=str)


def _readable(value):
    """值 → 可读文本：str 原样；内容块列表拼 text；dict 转 k: v 行；
    其余形状 JSON 兜底。入参/出参的展示格式只此一处，不再裸 dumps。"""
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(isinstance(b, dict) for b in value):
        texts = [b["text"] for b in value if isinstance(b.get("text"), str)]
        if texts:
            return "\n".join(texts)
    if isinstance(value, dict):
        return "\n".join(f"{k}: {_plain(v)}" for k, v in value.items())
    return json.dumps(value, ensure_ascii=False, default=str)


def tool_diff(tool, value):
    """Edit/Write 入参 → 行级 diff 文本（---/+++ 头 + 每行 -/+ 前缀），
    经 detail() 同款脱敏与截断；形状不合返回 None（前端回退普通输入块）。"""
    if not isinstance(value, dict):
        return None
    if tool == "Write" and isinstance(value.get("content"), str):
        old, new = "", value["content"]
    else:
        old, new = value.get("old_string"), value.get("new_string")
        if not isinstance(old, str) or not isinstance(new, str):
            return None
    path = value.get("file_path") or value.get("notebook_path") or "(未知路径)"
    lines = [f"--- {path}", f"+++ {path}"]
    lines += [f"-{ln}" for ln in old.splitlines()]
    lines += [f"+{ln}" for ln in new.splitlines()]
    return detail("\n".join(lines))


def primary_arg(tool, value):
    """入参摘要的主参数：已知工具取主字段，未知工具取首个字符串字段，
    再兜底整体可读格式。TodoWrite 摘要为完成计数。"""
    if not isinstance(value, dict):
        return _readable(value)
    if tool == "TodoWrite" and isinstance(value.get("todos"), list):
        done = sum(
            1 for t in value["todos"] if isinstance(t, dict) and t.get("status") == "completed"
        )
        return f"{done}/{len(value['todos'])} 完成"
    field = PRIMARY_INPUT_FIELD.get(tool)
    if isinstance(value.get(field), str) and value[field]:
        return value[field]
    for v in value.values():
        if isinstance(v, str) and v:
            return v
    return _readable(value)


def _clip(text):
    if len(text) > TOOL_SUMMARY_LIMIT:
        return text[:TOOL_SUMMARY_LIMIT] + "…"
    return text


def summarize(value):
    """出参/通用值的脱敏摘要：可读格式化后截断，折叠行展示用。"""
    return _clip(redact_text(_readable(value)))


def summarize_input(tool, value):
    """入参摘要：主参数优先——Tool(路径/命令) 的单行观感。"""
    return _clip(redact_text(primary_arg(tool, value)))


def detail(value):
    """脱敏全文：展开查看用。先整值脱敏再截断——截断不能
    先于脱敏（把已知凭据值截成两半就不再匹配整值替换，半截明文漏出）。"""
    value = redact_text(_readable(value))
    if len(value) > TOOL_DETAIL_LIMIT:
        value = value[:TOOL_DETAIL_LIMIT] + f"\n…（已截断，脱敏后全文 {len(value)} 字符）"
    return value


def detail_input(tool, value):
    """入参全文：k: v 行，但已作摘要主参数的字段不重复（如 Bash 的
    description）；过滤后无剩余字段时保留全量（Read 仅 file_path 一项）。"""
    if isinstance(value, dict):
        rest = {k: v for k, v in value.items() if k != PRIMARY_INPUT_FIELD.get(tool)}
        if rest:
            value = rest
    return detail(value)


def tool_todos(value):
    """TodoWrite 入参 → [{content, status}] 脱敏列表（前端渲染 checkbox）；
    形状不合返回 None（前端回退普通输入块）。"""
    todos = value.get("todos") if isinstance(value, dict) else None
    if not isinstance(todos, list):
        return None
    items = [
        {
            "content": redact_text(t["content"]),
            "status": t["status"] if isinstance(t.get("status"), str) else "pending",
        }
        for t in todos
        if isinstance(t, dict) and isinstance(t.get("content"), str)
    ]
    return items or None


def is_final_result(message):
    return message.get("type") == "result"


def _content_blocks(message):
    """消息内容块列表：content 非列表或元素非 dict 时按无块处理。"""
    content = message.get("message", {}).get("content")
    if not isinstance(content, list):
        return []
    return [b for b in content if isinstance(b, dict)]


def normalize_message(message, tool_names):
    """一条 SDK 消息 → [(事件类型, payload)]；tool_names 为 id→名字映射，随处理更新。

    content 形状不可穷尽（user 行可为纯字符串、块可为未知类型）：
    非列表 content 与非 dict 块一律跳过，不做结构推断。
    """
    events = []
    mtype = message.get("type")
    if mtype == "assistant":
        for block in _content_blocks(message):
            btype = block.get("type")
            if btype == "thinking":
                events.append(("agent.thinking", {"text": redact_text(block.get("thinking", ""))}))
            elif btype == "text":
                events.append(("agent.message", {"text": redact_text(block.get("text", ""))}))
            elif btype == "tool_use":
                if block.get("id"):
                    tool_names[block["id"]] = block.get("name", "")
                # 只有子 agent 工具（Agent/Task）才看 subagent_type——其他
                # 工具入参里撞名同字段也不推阶段
                subagent = ((block.get("input") or {}).get("subagent_type")
                            if block.get("name") in SUBAGENT_TOOL_NAMES else None)
                stage = STAGE_BY_SUBAGENT.get(subagent)
                if stage:
                    # subagent 随行：任务跟踪按它分流镜像/RPM 流水线
                    events.append((
                        "stage.changed",
                        {"stage": stage, "status": "running", "subagent": subagent},
                    ))
                events.append((
                    "agent.tool_started",
                    {
                        # id 供前端把 started/finished 合并为一行
                        "id": block.get("id"),
                        "tool": block.get("name", ""),
                        "summary": summarize_input(block.get("name", ""), block.get("input")),
                        "detail": detail_input(block.get("name", ""), block.get("input")),
                        "diff": tool_diff(block.get("name", ""), block.get("input")),
                        "todos": tool_todos(block.get("input")),
                    },
                ))
    elif mtype == "user":
        for block in _content_blocks(message):
            if block.get("type") == "tool_result":
                events.append((
                    "agent.tool_finished",
                    {
                        "id": block.get("tool_use_id"),
                        "tool": tool_names.get(block.get("tool_use_id"), ""),
                        "summary": summarize(block.get("content")),
                        "detail": detail(block.get("content")),
                    },
                ))
    return events
