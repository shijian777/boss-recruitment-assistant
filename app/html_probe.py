from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser


@dataclass(frozen=True, slots=True)
class HtmlProbeResult:
    candidate_cards: int
    greeting_buttons: int
    message_inputs: int
    send_buttons: int
    next_buttons: int
    risk_matches: tuple[str, ...]


class _ProbeParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.candidate_cards = 0
        self.message_inputs = 0
        self._interactive_stack: list[tuple[str, list[str]]] = []
        self.labels: list[str] = []
        self.all_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key: value or "" for key, value in attrs}
        classes = set(values.get("class", "").split())
        if "data-candidate-id" in values or classes.intersection({"candidate-card", "recommend-card-box", "geek-card"}):
            self.candidate_cards += 1
        if tag == "textarea" and ("data-message-input" in values or "消息" in values.get("placeholder", "")):
            self.message_inputs += 1
        if tag in {"button", "a"} or values.get("role") == "button":
            self._interactive_stack.append((tag, []))

    def handle_endtag(self, tag: str) -> None:
        if self._interactive_stack and self._interactive_stack[-1][0] == tag:
            _, chunks = self._interactive_stack.pop()
            self.labels.append(" ".join("".join(chunks).split()))

    def handle_data(self, data: str) -> None:
        self.all_text.append(data)
        for _, chunks in self._interactive_stack:
            chunks.append(data)


def probe_html(source: str, risk_keywords: tuple[str, ...] | list[str]) -> HtmlProbeResult:
    parser = _ProbeParser()
    parser.feed(source)
    greeting = {"打招呼", "立即沟通", "聊一聊"}
    labels = [label.strip() for label in parser.labels]
    full_text = " ".join(parser.all_text).casefold()
    risks = tuple(keyword for keyword in risk_keywords if keyword.casefold() in full_text)
    return HtmlProbeResult(
        candidate_cards=parser.candidate_cards,
        greeting_buttons=sum(label in greeting for label in labels),
        message_inputs=parser.message_inputs,
        send_buttons=sum(label == "发送" for label in labels),
        next_buttons=sum(label == "下一页" for label in labels),
        risk_matches=risks,
    )
