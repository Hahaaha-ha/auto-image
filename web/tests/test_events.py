#!/usr/bin/env python3
"""EventStore 契约 —— seq 递增、ts 来源（缺省当下 / 透传源时刻）、转录与广播。

事件时刻透传的地基：append 缺省仍取当下时钟（实时路径零变化），重放/
转录路径传入源时刻——ts 语义从「落流时刻」升级为「事件时刻」。
纯 assert，无 pytest。

运行：python web/tests/test_events.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from web.events import EventStore  # noqa: E402


class FakeRuns:
    """runs.get 同形的最小替身（bind_runs 的对端）。"""

    def __init__(self):
        self.runs = {}

    def get(self, run_id):
        return self.runs.get(run_id)


def test_append_defaults_to_now():
    store = EventStore()
    store.create("r")
    before = time.time()
    event = store.append("r", "user.message", {"text": "hi"})
    after = time.time()
    assert event["seq"] == 1, event
    assert before <= event["ts"] <= after, event  # ts = append 当下（现状不变）


def test_append_with_source_ts_uses_it_verbatim():
    store = EventStore()
    store.create("r")
    source = 1_700_000_000.0  # transcript 行的 timestamp（重启重放透传）
    event = store.append("r", "agent.message", {"text": "ok"}, ts=source)
    assert event["ts"] == source, event
    # 一条消息派生的多条事件共享同一源时刻（重放路径逐条传同值）
    second = store.append("r", "stage.changed", {"stage": "GUIDE"}, ts=source)
    assert second["ts"] == source, second


def test_append_source_ts_syncs_last_event_at():
    runs = FakeRuns()
    runs.runs["r"] = type("R", (), {"last_event_at": None})()
    store = EventStore()
    store.bind_runs(runs)
    store.create("r")
    source = 1_700_000_000.0
    store.append("r", "agent.message", {}, ts=source)
    assert runs.runs["r"].last_event_at == source


def test_replay_from_filters_by_seq():
    store = EventStore()
    store.create("r")
    store.append("r", "session.started", {})
    store.append("r", "turn.started", {})
    store.append("r", "user.message", {})
    assert [e["seq"] for e in store.replay_from("r", 0)] == [1, 2, 3]
    assert [e["seq"] for e in store.replay_from("r", 2)] == [3]


def test_adopt_history_renumbers_seq_and_carries_ts():
    store = EventStore()
    store.create("src")
    source = 1_700_000_000.0
    store.append("src", "user.message", {"text": "hi"}, ts=source)
    store.append("src", "session.ended", {})
    store.create("dst")
    store.adopt_history("dst", "src", skip_types={"session.ended"})
    events = store.replay_from("dst", 0)
    assert [(e["type"], e["seq"]) for e in events] == [("user.message", 1)], events
    assert events[0]["ts"] == source, events  # 转录时刻原样透传


def test_observer_notified_on_append():
    """进程内观察者收完整事件 dict（任务跟踪等旁路消费者的入口）。"""
    store = EventStore()
    seen = []
    store.add_observer(lambda ev: seen.append(ev))
    store.create("r")
    store.append("r", "stage.changed", {"stage": "GUIDE", "status": "running"})
    assert len(seen) == 1, seen
    assert seen[0]["type"] == "stage.changed"
    assert seen[0]["payload"]["stage"] == "GUIDE"
    assert seen[0]["run_id"] == "r" and seen[0]["seq"] == 1


def test_observer_suppressed_for_adopt_history():
    """克隆转录不通知观察者——转录历史里的 stage 派生任务会造成幻影。"""
    store = EventStore()
    seen = []
    store.add_observer(lambda ev: seen.append(ev))
    store.create("src")
    store.append("src", "stage.changed", {"stage": "GUIDE", "status": "running"})
    assert len(seen) == 1
    store.create("dst")
    store.adopt_history("dst", "src", skip_types=set())
    assert len(seen) == 1, seen  # 转录事件广播照常、观察者静默


def test_observer_exception_does_not_break_append():
    """观察者抛错被吞掉只记日志——旁路消费绝不影响事件流本体。"""
    store = EventStore()

    def boom(_ev):
        raise RuntimeError("observer bug")

    store.add_observer(boom)
    seen = []
    store.add_observer(lambda ev: seen.append(ev))
    store.create("r")
    event = store.append("r", "user.message", {"text": "hi"})
    assert event["seq"] == 1  # append 正常返回
    assert len(seen) == 1  # 后续观察者照常收到


def main():
    tests = [fn for name, fn in sorted(globals().items()) if name.startswith("test_")]
    for fn in tests:
        fn()
        print(f"ok {fn.__name__}")
    print(f"{len(tests)} passed")


if __name__ == "__main__":
    main()
