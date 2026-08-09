from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta

from app.automation import RuntimeCounters
from app.candidate_filter import CandidateFilterSettings
from app.config import AppConfig
from app.conversation import ChatMessage, ConversationSnapshot, InboxItemSnapshot
from app.database import Database, ReplyRecord
from app.demo import _DemoClient, _candidate, _controller, run_batch_simulation
from app.desktop import SendResult
from app.reply_rules import ReplyRule


def test_hundred_candidate_batch_flow_and_dashboard_report(tmp_path) -> None:
    report = run_batch_simulation(tmp_path, candidate_count=100)
    happy = report["happy_path"]
    guard = report["uncertain_outcome_guard"]

    assert report["result"] == "PASS"
    assert happy["native_greeting_calls"] == 100
    assert happy["today_contacted"] == 100
    assert happy["verified_sent"] == 100
    assert happy["all_positive_waits_are_five_seconds"] is True
    assert happy["duplicate_resends"] == 0
    assert happy["overflow_candidate_sent"] is False
    assert happy["daily_limit_stopped"] is True
    assert happy["sidebar_unread_messages"] == 12
    assert guard["click_attempts_after_retry"] == 1
    assert guard["second_attempt_blocked"] is True
    assert guard["paused_for_manual_review"] is True

    saved = json.loads((tmp_path / "batch_demo.json").read_text(encoding="utf-8"))
    assert saved["happy_path"]["verified_sent"] == 100
    assert (tmp_path / "batch_demo.html").exists()


def test_session_limit_stops_before_daily_limit(tmp_path) -> None:
    config = replace(
        AppConfig.defaults(),
        dry_run=False,
        session_limit=2,
        daily_limit=10,
        interval_seconds=1,
    )
    database = Database(tmp_path / "session-limit.db")
    controller, _events = _controller(database, config, tmp_path)
    controller._control.interruptible_sleep = lambda seconds, poll_seconds=0.1: True  # type: ignore[method-assign]
    client = _DemoClient()
    counters = RuntimeCounters()
    seen: set[str] = set()

    controller._process_candidate(client, _candidate(1), counters, seen)
    controller._process_candidate(client, _candidate(2), counters, seen)
    controller._process_candidate(client, _candidate(3), counters, seen)

    assert len(client.send_calls) == 2
    assert counters.session_contacted == 2
    assert database.today_sent_count() == 2
    assert controller._control.is_stopped is True


def test_screenshot_failure_does_not_escape_unknown_outcome_guard(tmp_path) -> None:
    config = replace(AppConfig.defaults(), dry_run=False, session_limit=2, daily_limit=10)
    database = Database(tmp_path / "screenshot-failure.db")
    controller, _events = _controller(database, config, tmp_path)
    controller._control.resume()
    client = _DemoClient(unverified=True)
    client.save_screenshot = lambda _prefix: (_ for _ in ()).throw(RuntimeError("window minimized"))  # type: ignore[method-assign]
    counters = RuntimeCounters()

    controller._process_candidate(client, _candidate(1), counters, set())

    assert database.dashboard_stats().unverified == 1
    assert controller._control.is_paused is True


def test_platform_quota_block_is_not_counted_as_contact_or_unverified(tmp_path) -> None:
    config = replace(AppConfig.defaults(), dry_run=False, session_limit=2, daily_limit=10)
    database = Database(tmp_path / "quota-block.db")
    controller, _events = _controller(database, config, tmp_path)
    controller._control.resume()
    client = _DemoClient()
    client.send_greeting = lambda _snapshot: SendResult(  # type: ignore[method-assign]
        False,
        "平台未发送：检测到沟通权益不足",
        attempted=True,
        platform_blocked=True,
    )

    counters = RuntimeCounters()
    controller._process_candidate(client, _candidate(1), counters, set())

    stats = database.dashboard_stats()
    assert stats.contacted == 0
    assert stats.sent == 0
    assert stats.unverified == 0
    assert stats.failed == 1
    assert counters.session_contacted == 0
    assert controller._control.is_paused is True


def test_candidate_must_match_city_and_salary_before_greeting(tmp_path) -> None:
    config = replace(
        AppConfig.defaults(),
        dry_run=False,
        candidate_filter=CandidateFilterSettings(
            keywords=("销售",),
            cities=("杭州",),
            salary_min_k=10,
            salary_max_k=20,
        ),
    )
    database = Database(tmp_path / "filter.db")
    controller, _events = _controller(database, config, tmp_path)
    controller._control.resume()
    controller._control.interruptible_sleep = (  # type: ignore[method-assign]
        lambda seconds, poll_seconds=0.1: True
    )
    client = _DemoClient()
    rejected = replace(
        _candidate(1),
        summary="演示候选人 | 上海 | 销售 | 10-15K",
    )

    controller._process_candidate(client, rejected, RuntimeCounters(), set())

    assert client.send_calls == []
    assert database.status_counts()["filtered"] == 1


class _ReplyClient:
    def __init__(self, snapshot: ConversationSnapshot) -> None:
        self.snapshot = snapshot
        self.calls: list[tuple[str, bool]] = []
        self.opened: list[str] = []
        self.is_open = True

    def read_current_conversation(self) -> ConversationSnapshot:
        return self.snapshot

    def send_current_chat_message(self, message: str, *, dry_run: bool = False) -> SendResult:
        self.calls.append((message, dry_run))
        return SendResult(True, message=message, attempted=not dry_run)

    def open_inbox_conversation(self, item: InboxItemSnapshot) -> bool:
        self.opened.append(item.incoming_key)
        return True

    def body_text(self) -> str:
        return ""

    def unread_message_count(self) -> int:
        return 0


def test_auto_reply_uses_library_once_for_one_incoming_message(tmp_path) -> None:
    rule = ReplyRule("愿意沟通", ("有兴趣",), "感谢回复，请问明天下午方便面试吗？")
    base = AppConfig.defaults()
    config = replace(
        base,
        dry_run=False,
        auto_reply=replace(base.auto_reply, enabled=True, rules=(rule,)),
    )
    database = Database(tmp_path / "auto-reply.db")
    controller, _events = _controller(database, config, tmp_path)
    incoming = ChatMessage("incoming-1", "incoming", "您好，我有兴趣")
    conversation = ConversationSnapshot("conversation-1", "张某", (incoming,))
    client = _ReplyClient(conversation)

    controller._handle_auto_reply(client, conversation, incoming, RuntimeCounters())
    controller._handle_auto_reply(client, conversation, incoming, RuntimeCounters())

    assert client.calls == [("感谢回复，请问明天下午方便面试吗？", False)]
    assert database.today_reply_sent_count() == 1


def test_auto_reply_never_replies_after_refusal(tmp_path) -> None:
    rule = ReplyRule("愿意沟通", ("可以",), "好的，我们继续沟通")
    base = AppConfig.defaults()
    config = replace(
        base,
        dry_run=False,
        auto_reply=replace(
            base.auto_reply,
            enabled=True,
            rules=(rule,),
            stop_keywords=("不要联系",),
        ),
    )
    database = Database(tmp_path / "auto-reply-stop.db")
    controller, _events = _controller(database, config, tmp_path)
    incoming = ChatMessage("incoming-stop", "incoming", "可以，但请不要联系我了")
    conversation = ConversationSnapshot("conversation-1", "张某", (incoming,))
    client = _ReplyClient(conversation)

    controller._handle_auto_reply(client, conversation, incoming, RuntimeCounters())

    assert client.calls == []
    assert database.today_reply_sent_count() == 0


def test_auto_reply_conversation_limit_routes_to_manual_without_send(tmp_path) -> None:
    rule = ReplyRule("愿意沟通", ("有兴趣",), "感谢回复")
    base = AppConfig.defaults()
    config = replace(
        base,
        dry_run=False,
        auto_reply=replace(
            base.auto_reply,
            enabled=True,
            rules=(rule,),
            per_conversation_limit=3,
        ),
    )
    database = Database(tmp_path / "conversation-limit.db")
    for index in range(3):
        database.record_reply(
            ReplyRecord(
                "reply_sent",
                f"历史回复{index}",
                conversation_key="conversation-1",
            )
        )
    controller, _events = _controller(database, config, tmp_path)
    incoming = ChatMessage("incoming-limit", "incoming", "我有兴趣")
    conversation = ConversationSnapshot("conversation-1", "张某", (incoming,))
    client = _ReplyClient(conversation)

    processed = controller._handle_auto_reply(
        client,
        conversation,
        incoming,
        RuntimeCounters(),
    )

    assert processed is True
    assert client.calls == []


def test_auto_reply_interval_wait_keeps_incoming_unprocessed(tmp_path) -> None:
    rule = ReplyRule("愿意沟通", ("有兴趣",), "感谢回复")
    base = AppConfig.defaults()
    config = replace(
        base,
        dry_run=False,
        auto_reply=replace(
            base.auto_reply,
            enabled=True,
            rules=(rule,),
            interval_seconds=30,
        ),
    )
    database = Database(tmp_path / "reply-wait.db")
    database.record_reply(
        ReplyRecord("reply_sent", "上一条", conversation_key="another"),
        when=datetime.now().astimezone() - timedelta(seconds=2),
    )
    controller, _events = _controller(database, config, tmp_path)
    incoming = ChatMessage("incoming-wait", "incoming", "我有兴趣")
    conversation = ConversationSnapshot("conversation-1", "张某", (incoming,))
    client = _ReplyClient(conversation)

    processed = controller._handle_auto_reply(
        client,
        conversation,
        incoming,
        RuntimeCounters(),
    )

    assert processed is False
    assert client.calls == []
    assert database.has_replied_to(incoming.key) is False


def test_inbox_auto_reply_opens_exact_unread_and_sends_once(tmp_path) -> None:
    rule = ReplyRule("愿意沟通", ("有兴趣",), "感谢回复，请问明天下午方便面试吗？")
    base = AppConfig.defaults()
    config = replace(
        base,
        dry_run=False,
        auto_reply=replace(base.auto_reply, enabled=True, rules=(rule,)),
    )
    database = Database(tmp_path / "inbox-auto-reply.db")
    controller, _events = _controller(database, config, tmp_path)
    incoming = ChatMessage("full-incoming-1", "incoming", "您好，我有兴趣")
    client = _ReplyClient(ConversationSnapshot("conversation-1", "张某", (incoming,)))
    item = InboxItemSnapshot(
        "conversation-1",
        "incoming-inbox-1",
        "张某",
        "Python 工程师",
        "您好，我有兴趣",
        1,
    )
    seen: set[str] = set()

    controller._handle_inbox_reply(client, item, RuntimeCounters(), seen)
    controller._handle_inbox_reply(client, item, RuntimeCounters(), seen)

    assert client.opened == ["incoming-inbox-1"]
    assert client.calls == [("感谢回复，请问明天下午方便面试吗？", False)]
    assert database.today_reply_sent_count() == 1
    assert seen == {"incoming-inbox-1"}


def test_inbox_dry_run_matches_without_opening_or_sending(tmp_path) -> None:
    rule = ReplyRule("愿意沟通", ("有兴趣",), "感谢回复")
    base = AppConfig.defaults()
    config = replace(
        base,
        dry_run=True,
        auto_reply=replace(base.auto_reply, enabled=True, rules=(rule,)),
    )
    database = Database(tmp_path / "inbox-dry-run.db")
    controller, _events = _controller(database, config, tmp_path)
    client = _ReplyClient(ConversationSnapshot("conversation-1", "张某", ()))
    item = InboxItemSnapshot(
        "conversation-1", "incoming-dry-1", "张某", "岗位", "我有兴趣", 1
    )

    controller._handle_inbox_reply(client, item, RuntimeCounters(), set())

    assert client.opened == []
    assert client.calls == []


def test_inbox_full_message_stop_word_cancels_preview_match(tmp_path) -> None:
    rule = ReplyRule("愿意沟通", ("有兴趣",), "感谢回复")
    base = AppConfig.defaults()
    config = replace(
        base,
        dry_run=False,
        auto_reply=replace(
            base.auto_reply,
            enabled=True,
            rules=(rule,),
            stop_keywords=("不要联系",),
        ),
    )
    database = Database(tmp_path / "inbox-full-stop.db")
    controller, _events = _controller(database, config, tmp_path)
    full = ChatMessage(
        "full-incoming-stop", "incoming", "我有兴趣了解，但请不要联系我"
    )
    client = _ReplyClient(ConversationSnapshot("conversation-1", "张某", (full,)))
    item = InboxItemSnapshot(
        "conversation-1", "incoming-stop-full", "张某", "岗位", "我有兴趣了解", 1
    )

    controller._handle_inbox_reply(client, item, RuntimeCounters(), set())

    assert client.opened == ["incoming-stop-full"]
    assert client.calls == []
    assert database.today_reply_sent_count() == 0
