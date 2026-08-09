from __future__ import annotations

from dataclasses import dataclass

from app.selectors import normalize_space


class ReplyLibraryError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ReplyRule:
    name: str
    keywords: tuple[str, ...]
    reply: str
    match_mode: str = "any"
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class ReplyDecision:
    action: str
    reason: str
    rule_name: str = ""
    reply: str = ""


def _normalized(value: str) -> str:
    return normalize_space(value).casefold()


def choose_reply(
    incoming_text: str,
    rules: tuple[ReplyRule, ...] | list[ReplyRule],
    stop_keywords: tuple[str, ...] | list[str],
) -> ReplyDecision:
    """Choose one allow-listed template; ambiguous and refusal messages never send."""
    haystack = _normalized(incoming_text)
    for keyword in stop_keywords:
        normalized = _normalized(keyword)
        if normalized and normalized in haystack:
            return ReplyDecision("stop", f"命中停止回复词：{keyword}")

    matches: list[ReplyRule] = []
    for rule in rules:
        if not rule.enabled:
            continue
        keywords = [_normalized(item) for item in rule.keywords if _normalized(item)]
        hits = [item for item in keywords if item in haystack]
        if rule.match_mode == "all" and keywords and len(hits) == len(keywords):
            matches.append(rule)
        elif rule.match_mode == "any" and hits:
            matches.append(rule)

    if not matches:
        return ReplyDecision("manual", "未命中任何已批准话术")
    replies = {rule.reply for rule in matches}
    if len(replies) != 1:
        names = "、".join(rule.name for rule in matches)
        return ReplyDecision("manual", f"同时命中多个不同话术：{names}")
    selected = matches[0]
    return ReplyDecision(
        "reply",
        f"命中话术：{selected.name}",
        rule_name=selected.name,
        reply=selected.reply,
    )


def validate_reply_rules(rules: tuple[ReplyRule, ...] | list[ReplyRule]) -> None:
    """Reject malformed or conflicting enabled rules before they can be saved."""
    seen_names: set[str] = set()
    seen_triggers: dict[str, str] = {}
    for rule in rules:
        name = normalize_space(rule.name)
        if not name:
            raise ReplyLibraryError("话术名称不能为空")
        normalized_name = name.casefold()
        if normalized_name in seen_names:
            raise ReplyLibraryError(f"话术名称“{name}”重复")
        seen_names.add(normalized_name)
        if rule.match_mode not in {"any", "all"}:
            raise ReplyLibraryError(f"话术“{name}”匹配方式无效")
        if not rule.keywords or any(not normalize_space(item) for item in rule.keywords):
            raise ReplyLibraryError(f"话术“{name}”至少需要一个非空触发词")
        if not rule.reply.strip():
            raise ReplyLibraryError(f"话术“{name}”回复不能为空")
        if len(rule.reply) > 1000:
            raise ReplyLibraryError(f"话术“{name}”回复超过 1000 个字符")
        if not rule.enabled:
            continue
        for keyword in rule.keywords:
            normalized = _normalized(keyword)
            previous = seen_triggers.get(normalized)
            if previous is not None:
                raise ReplyLibraryError(
                    f"触发词“{normalize_space(keyword)}”在“{previous}”和“{name}”中重复"
                )
            seen_triggers[normalized] = name


def parse_reply_library(source: str) -> tuple[ReplyRule, ...]:
    """Parse one rule per line: keyword1|keyword2 => approved reply."""
    rules: list[ReplyRule] = []
    for line_number, raw_line in enumerate(source.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=>" not in line:
            raise ReplyLibraryError(f"话术库第 {line_number} 行缺少 =>")
        keyword_source, reply = (part.strip() for part in line.split("=>", 1))
        keywords = tuple(
            normalize_space(item)
            for item in keyword_source.replace("，", "|").replace(",", "|").split("|")
            if normalize_space(item)
        )
        if not keywords:
            raise ReplyLibraryError(f"话术库第 {line_number} 行没有关键词")
        if not reply:
            raise ReplyLibraryError(f"话术库第 {line_number} 行没有回复内容")
        if len(reply) > 1000:
            raise ReplyLibraryError(f"话术库第 {line_number} 行回复超过 1000 个字符")
        rules.append(
            ReplyRule(
                name=f"话术{len(rules) + 1}",
                keywords=keywords,
                reply=reply,
            )
        )
    parsed = tuple(rules)
    validate_reply_rules(parsed)
    return parsed


def format_reply_library(rules: tuple[ReplyRule, ...] | list[ReplyRule]) -> str:
    return "\n".join(
        f"{'|'.join(rule.keywords)} => {rule.reply}"
        for rule in rules
        if rule.enabled
    )
