from __future__ import annotations

import pytest

from app.reply_rules import (
    ReplyLibraryError,
    ReplyRule,
    choose_reply,
    format_reply_library,
    parse_reply_library,
    validate_reply_rules,
)


def test_reply_library_parses_one_rule_per_line() -> None:
    source = """
    # 面试邀约
    可以|有兴趣 => 很高兴收到您的回复，请问明天下午方便面试吗？
    什么时候面试，面试时间 => 我们可以先确认您方便的时间。
    """
    rules = parse_reply_library(source)
    assert len(rules) == 2
    assert rules[0].keywords == ("可以", "有兴趣")
    assert rules[1].keywords == ("什么时候面试", "面试时间")
    assert parse_reply_library(format_reply_library(rules)) == rules


def test_reply_library_rejects_invalid_line() -> None:
    with pytest.raises(ReplyLibraryError, match="缺少 =>"):
        parse_reply_library("可以|有兴趣 这行没有分隔符")


def test_reply_stop_keyword_has_priority() -> None:
    rules = (ReplyRule("愿意沟通", ("可以",), "好的，我们继续沟通"),)
    decision = choose_reply("可以，但我已经找到工作，不用联系了", rules, ("不用联系",))
    assert decision.action == "stop"


def test_reply_requires_one_unambiguous_template() -> None:
    rules = (
        ReplyRule("规则一", ("可以",), "回复一"),
        ReplyRule("规则二", ("有兴趣",), "回复二"),
    )
    assert choose_reply("可以，我有兴趣", rules, ()).action == "manual"
    assert choose_reply("可以", rules, ()).reply == "回复一"
    assert choose_reply("您好", rules, ()).action == "manual"


def test_disabled_rule_never_matches_or_exports_as_active_text() -> None:
    rule = ReplyRule("时间", ("明天",), "明天下午可以", enabled=False)

    assert choose_reply("明天可以吗", (rule,), ()).action == "manual"
    assert format_reply_library((rule,)) == ""


def test_duplicate_enabled_trigger_is_rejected() -> None:
    rules = (
        ReplyRule("时间一", ("明天",), "回复一"),
        ReplyRule("时间二", ("明天",), "回复二"),
    )

    with pytest.raises(ReplyLibraryError, match="触发词.*重复"):
        validate_reply_rules(rules)


def test_disabled_rule_may_share_trigger_with_enabled_rule() -> None:
    rules = (
        ReplyRule("旧话术", ("明天",), "旧回复", enabled=False),
        ReplyRule("新话术", ("明天",), "新回复"),
    )

    validate_reply_rules(rules)
    assert choose_reply("明天可以吗", rules, ()).reply == "新回复"
