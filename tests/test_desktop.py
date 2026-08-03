from __future__ import annotations

from app.config import AppConfig
from app.desktop import DesktopBossClient, _MsaaNode


class _Accessible:
    def __init__(
        self,
        name: str,
        role: int,
        *,
        value: str = "",
        description: str = "",
        action: str = "",
    ) -> None:
        self.name = name
        self.role = role
        self.value = value
        self.description = description
        self.action = action
        self.children: list[_MsaaNode] = []
        self.on_invoke = None

    def accName(self, _child_id: int) -> str:
        return self.name

    def accRole(self, _child_id: int) -> int:
        return self.role

    def accState(self, _child_id: int) -> int:
        return 0

    def accValue(self, _child_id: int) -> str:
        return self.value

    def accDescription(self, _child_id: int) -> str:
        return self.description

    def accDefaultAction(self, _child_id: int) -> str:
        return self.action

    def accDoDefaultAction(self, _child_id: int) -> None:
        if self.on_invoke:
            self.on_invoke()


class _Runtime:
    @staticmethod
    def children(node: _MsaaNode) -> list[_MsaaNode]:
        return node.accessible.children


def _node(name: str, role: int, **kwargs) -> _MsaaNode:
    return _MsaaNode(_Accessible(name, role, **kwargs), 0)


def _add(parent: _MsaaNode, *children: _MsaaNode) -> None:
    for child in children:
        child.parent = parent
        parent.accessible.children.append(child)


def _client(tmp_path, monkeypatch):
    root = _node("BOSS直聘", 16)
    card = _node("", 20)
    greeting = _node("打招呼", 43, action="按下")
    _add(
        card,
        _node("张某", 41),
        _node("Python 后端工程师", 41),
        _node("5 年 · 示例科技", 41),
        greeting,
    )
    composer = _node("请输入消息", 42)
    send = _node("发送", 43, action="按下")
    chat = _node("聊天窗口", 20)
    _add(chat, composer, send)
    next_page = _node("下一页", 43, action="按下")
    _add(root, card, chat, next_page)

    client = DesktopBossClient(AppConfig.defaults(), tmp_path, logger=None)
    client._root = root
    client._runtime = _Runtime()
    client._handle = 1
    client._process_id = 123
    monkeypatch.setattr(DesktopBossClient, "is_open", property(lambda _self: True))
    monkeypatch.setattr(client, "refresh", lambda: None)
    return client, root, composer, send


def test_desktop_candidate_recognition(tmp_path, monkeypatch) -> None:
    client, _, _, _ = _client(tmp_path, monkeypatch)
    snapshots = client.scan_candidates()
    assert len(snapshots) == 1
    assert snapshots[0].name == "张某"
    assert snapshots[0].action_label == "打招呼"
    assert snapshots[0].key.startswith("fields:")


def test_desktop_send_requires_message_appearance_and_empty_composer(tmp_path, monkeypatch) -> None:
    client, root, composer, send = _client(tmp_path, monkeypatch)
    snapshot = client.scan_candidates()[0]

    def set_message(_composer: _MsaaNode, value: str) -> None:
        composer.accessible.value = value

    def complete_send() -> None:
        message = _node(composer.accessible.value, 41)
        _add(root, message)
        composer.accessible.value = ""

    monkeypatch.setattr(client, "_set_composer_value", set_message)
    send.accessible.on_invoke = complete_send
    result = client.send_message(snapshot, "测试邀约")
    assert result.success is True
    assert composer.accessible.value == ""


def test_desktop_next_page_control(tmp_path, monkeypatch) -> None:
    client, root, _, _ = _client(tmp_path, monkeypatch)
    next_button = next(node for node in client._walk(root) if client._name(node) == "下一页")
    clicked: list[bool] = []
    next_button.accessible.on_invoke = lambda: clicked.append(True)
    assert client.click_next_page() is True
    assert clicked == [True]
