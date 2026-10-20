#!/usr/bin/env python3
"""会话来源过滤直测 —— entrypoint 读取与发现层黑名单（临时目录真 JSONL）。

web 面板只呈现本服务系的程序化会话（sdk-*）与无标记旧会话，交互客户端
（终端 / VS Code 插件 / 桌面端）的人工会话在发现层剔除。判定信号是
transcript 头窗口内的 entrypoint——SDK 的 SessionSummary / SessionMessage
均不透出，只能自读；黑名单语义：未知来源保留。纯 assert，无 pytest。

运行：python web/tests/test_session_entrypoint.py
"""
import json
import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
import web.sdk as sdk  # noqa: E402
from web.sdk import _sanitize_project_dir_name, _session_entrypoint, list_project_sessions  # noqa: E402


@contextmanager
def claude_config():
    """临时 CLAUDE_CONFIG_DIR（读取器与 CLI 的同一约定）；用毕还原。"""
    old = os.environ.get("CLAUDE_CONFIG_DIR")
    cfg = tempfile.mkdtemp()
    os.environ["CLAUDE_CONFIG_DIR"] = cfg
    try:
        yield Path(cfg)
    finally:
        if old is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = old


def fresh_project_root():
    """临时项目根（真实目录，git worktree 探测在此安静失败只剩它自己）。"""
    return str(Path(tempfile.mkdtemp()).resolve())


def write_transcript(cfg, project_root, session_id, lines):
    """临时布局里写 transcript（目录名按 CLI 约定派生）。"""
    d = cfg / "projects" / _sanitize_project_dir_name(project_root)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{session_id}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def row(**fields):
    """transcript 行 JSON；entrypoint 给不给由用例自己定。"""
    return json.dumps(fields)


@contextmanager
def fake_list_sessions(infos):
    """web.sdk.list_sessions 换成脚本化假实现（发现层过滤不触真 SDK）。"""
    saved = sdk.list_sessions
    sdk.list_sessions = lambda directory: infos
    try:
        yield
    finally:
        sdk.list_sessions = saved


def test_reads_first_entrypoint_in_head_window():
    sid = "11111111-2222-3333-4444-555555555555"
    root = fresh_project_root()
    with claude_config() as cfg:
        write_transcript(cfg, root, sid, [
            # 头部元数据行不带 entrypoint；真实 CLI 写在消息行元数据区
            row(type="queue-operation"),
            row(type="user", entrypoint="sdk-py", message={"role": "user", "content": "部署 nginx"}),
            row(type="assistant", entrypoint="sdk-py", message={"role": "assistant"}),
        ])
        assert _session_entrypoint(sid, root) == "sdk-py"


def test_entrypoint_beyond_head_window_is_none():
    sid = "11111111-2222-3333-4444-555555555555"
    root = fresh_project_root()
    with claude_config() as cfg:
        # 首行超头窗口（64KB）且不含 entrypoint，其后的 entrypoint 不参与判定
        write_transcript(cfg, root, sid, [
            row(type="user", message={"role": "user", "content": "x" * 70000}),
            row(type="assistant", entrypoint="cli", message={"role": "assistant"}),
        ])
        assert _session_entrypoint(sid, root) is None


def test_missing_transcript_is_none():
    root = fresh_project_root()
    with claude_config():
        assert _session_entrypoint("99999999-8888-7777-6666-555555555555", root) is None


def test_discovery_filters_interactive_entrypoints():
    """发现层黑名单：四种交互客户端剔除，sdk-* / 无标记 / 缺 transcript 保留。"""
    root = fresh_project_root()
    cases = {  # session_id 前两位编码期望：h=隐藏，k=保留
        "h1000000-0000-0000-0000-00000000000a": "cli",
        "h2000000-0000-0000-0000-00000000000b": "claude-vscode",
        "h3000000-0000-0000-0000-00000000000c": "claude-desktop",
        "h4000000-0000-0000-0000-00000000000d": "claude-desktop-3p",
        "k1000000-0000-0000-0000-00000000000e": "sdk-py",
        "k2000000-0000-0000-0000-00000000000f": "sdk-ts",
        "k3000000-0000-0000-0000-000000000010": None,  # 旧版 CLI 无标记
    }
    missing_sid = "k4000000-0000-0000-0000-000000000011"  # 清单在、文件缺
    with claude_config() as cfg:
        for sid, ep in cases.items():
            fields = {"type": "user", "message": {"role": "user", "content": "指令"}}
            if ep is not None:
                fields["entrypoint"] = ep
            write_transcript(cfg, root, sid, [row(**fields)])
        infos = [type("Info", (), {"session_id": sid})() for sid in (*cases, missing_sid)]
        with fake_list_sessions(infos):
            got = {info.session_id for info in list_project_sessions(root)}
    assert got == {
        "k1000000-0000-0000-0000-00000000000e",
        "k2000000-0000-0000-0000-00000000000f",
        "k3000000-0000-0000-0000-000000000010",
        missing_sid,
    }


def main():
    tests = [fn for name, fn in sorted(globals().items()) if name.startswith("test_")]
    for fn in tests:
        fn()
        print(f"ok {fn.__name__}")
    print(f"{len(tests)} passed")


if __name__ == "__main__":
    main()
