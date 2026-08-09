from __future__ import annotations

from types import SimpleNamespace

import psutil
import pytest
import win32api
import win32gui
import win32process

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
        rect: tuple[int, int, int, int] | None = None,
    ) -> None:
        self.name = name
        self.role = role
        self.value = value
        self.description = description
        self.action = action
        self.rect = rect
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

    def accLocation(self, _child_id: int) -> tuple[int, int, int, int]:
        if self.rect is None:
            raise RuntimeError("location unavailable")
        return self.rect

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
    root = _node("BOSS直聘", 16, rect=(0, 0, 1000, 800))
    card = _node("", 20)
    greeting = _node("打招呼", 43, action="按下")
    _add(
        card,
        _node("张某", 41),
        _node("Python 后端工程师", 41),
        _node("5 年 · 示例科技", 41),
        greeting,
    )
    composer = _node("请输入消息", 42, rect=(320, 700, 480, 40))
    send = _node("发送", 43, action="按下", rect=(820, 700, 60, 40))
    chat = _node("聊天窗口", 20, rect=(200, 80, 700, 680))
    _add(chat, _node("张某", 41, rect=(480, 95, 100, 30)), composer, send)
    next_page = _node("下一页", 43, action="按下")
    _add(root, card, chat, next_page)

    client = DesktopBossClient(AppConfig.defaults(), tmp_path, logger=None)
    client._root = root
    client._runtime = _Runtime()
    client._handle = 1
    client._process_id = 123
    monkeypatch.setattr(DesktopBossClient, "is_open", property(lambda _self: True))
    monkeypatch.setattr(client, "refresh", lambda: None)
    monkeypatch.setattr(client, "_activate_window", lambda: None)
    monkeypatch.setattr(client, "_click_candidate_action", client._invoke)
    return client, root, composer, send


def test_desktop_candidate_recognition(tmp_path, monkeypatch) -> None:
    client, _, _, _ = _client(tmp_path, monkeypatch)
    snapshots = client.scan_candidates()
    assert len(snapshots) == 1
    assert snapshots[0].name == "张某"
    assert snapshots[0].action_label == "打招呼"
    assert snapshots[0].key.startswith("fields:")


def test_desktop_reads_sidebar_unread_message_badge(tmp_path, monkeypatch) -> None:
    client, root, _, _ = _client(tmp_path, monkeypatch)
    _add(root, _node("消息2", 30))
    root.children_cache = None
    assert client.unread_message_count() == 2


def test_desktop_scans_only_incoming_unread_inbox_items(tmp_path, monkeypatch) -> None:
    client, root, _, _ = _client(tmp_path, monkeypatch)
    unread = _node("", 34, action="选择", rect=(80, 160, 300, 90))
    _add(
        unread,
        _node("1", 41),
        _node("19:16", 41),
        _node("莫潘玉", 41),
        _node("AI 产品销售经理", 41),
        _node("您好，我对这个岗位有兴趣", 41),
    )
    outgoing = _node("", 34, action="选择", rect=(80, 260, 300, 90))
    _add(
        outgoing,
        _node("2", 41),
        _node("19:10", 41),
        _node("李某", 41),
        _node("Python 工程师", 41),
        _node("[已读]", 41),
        _node("我发出的消息", 41),
    )
    _add(root, unread, outgoing)
    root.children_cache = None

    items = client.scan_unread_inbox()

    assert len(items) == 1
    assert items[0].name == "莫潘玉"
    assert items[0].job == "AI 产品销售经理"
    assert items[0].preview == "您好，我对这个岗位有兴趣"
    assert items[0].unread_count == 1


def test_desktop_opens_message_page_by_semantic_navigation(tmp_path, monkeypatch) -> None:
    client, root, _, _ = _client(tmp_path, monkeypatch)
    nav = _node("消息1", 30, action="跳转")

    def show_message_page() -> None:
        _add(root, _node("新招呼", 41), _node("沟通中", 41), _node("未读", 41))
        root.children_cache = None

    nav.accessible.on_invoke = show_message_page
    _add(root, nav)
    root.children_cache = None
    monkeypatch.setattr("app.desktop.time.sleep", lambda _seconds: None)

    assert client.open_messages_page() is True


def test_desktop_opens_candidate_page_when_navigation_has_no_default_action(
    tmp_path, monkeypatch
) -> None:
    client, root, _, _ = _client(tmp_path, monkeypatch)
    greeting = next(node for node in client._walk(root) if client._name(node) == "打招呼")
    greeting.accessible.name = "继续沟通"
    greeting.cache.clear()
    nav = _node("推荐", 30, rect=(20, 300, 100, 40))

    def show_candidate_page(_nav_node: _MsaaNode, _labels: tuple[str, ...]) -> None:
        _add(
            root,
            _node("最新", 41),
            _node("期望职位", 41),
            _node("滚动加载更多", 41),
        )
        root.children_cache = None

    _add(root, nav)
    root.children_cache = None
    monkeypatch.setattr(client, "_click_navigation_action", show_candidate_page)
    monkeypatch.setattr("app.desktop.time.sleep", lambda _seconds: None)

    assert client.open_candidate_page() is True


def test_desktop_ignores_unreliable_navigation_default_action(
    tmp_path, monkeypatch
) -> None:
    client, root, _, _ = _client(tmp_path, monkeypatch)
    greeting = next(node for node in client._walk(root) if client._name(node) == "打招呼")
    greeting.accessible.name = "继续沟通"
    greeting.cache.clear()
    nav = _node("推荐", 30, action="略过", rect=(20, 300, 100, 40))
    clicked: list[str] = []

    def show_candidate_page(_nav_node: _MsaaNode, _labels: tuple[str, ...]) -> None:
        clicked.append("coordinate")
        _add(root, _node("滚动加载更多", 41))
        root.children_cache = None

    nav.accessible.on_invoke = lambda: clicked.append("default")
    _add(root, nav)
    root.children_cache = None
    monkeypatch.setattr(client, "_click_navigation_action", show_candidate_page)
    monkeypatch.setattr("app.desktop.time.sleep", lambda _seconds: None)

    assert client.open_candidate_page() is True
    assert clicked == ["coordinate"]


def test_desktop_opens_exact_unread_conversation_and_verifies_right_pane(tmp_path, monkeypatch) -> None:
    client, root, _, _ = _client(tmp_path, monkeypatch)
    unread = _node("", 34, action="选择", rect=(80, 160, 300, 90))
    _add(
        unread,
        _node("1", 41),
        _node("19:16", 41),
        _node("莫潘玉", 41),
        _node("AI 产品销售经理", 41),
        _node("您好，我有兴趣", 41),
    )

    def select_conversation() -> None:
        _add(root, _node("莫潘玉", 41, rect=(520, 90, 120, 30)))
        root.children_cache = None

    unread.accessible.on_invoke = select_conversation
    _add(root, unread)
    root.children_cache = None
    item = client.scan_unread_inbox()[0]
    monkeypatch.setattr("app.desktop.time.sleep", lambda _seconds: None)

    assert client.open_inbox_conversation(item) is True


def test_desktop_candidate_name_precedes_salary_even_when_graphic(tmp_path, monkeypatch) -> None:
    root = _node("BOSS直聘", 16)
    card = _node("", 20)
    greeting = _node("打招呼", 43, action="按下")
    _add(
        card,
        _node("王小明", 40),
        _node("2-7K", 41),
        _node("王小明", 41),
        _node("今日活跃", 41),
        _node("市场营销", 41),
        greeting,
    )
    _add(root, card)

    client = DesktopBossClient(AppConfig.defaults(), tmp_path, logger=None)
    client._root = root
    client._runtime = _Runtime()
    client._handle = 1
    client._process_id = 123
    monkeypatch.setattr(DesktopBossClient, "is_open", property(lambda _self: True))

    snapshots = client.scan_candidates()
    assert len(snapshots) == 1
    assert snapshots[0].name == "王小明"


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


def test_current_chat_reply_dry_run_never_types_or_clicks(tmp_path, monkeypatch) -> None:
    client, _, composer, send = _client(tmp_path, monkeypatch)
    clicked: list[bool] = []
    send.accessible.on_invoke = lambda: clicked.append(True)

    result = client.send_current_chat_message("测试邀约", dry_run=True)

    assert result.success is True
    assert result.attempted is False
    assert composer.accessible.value == ""
    assert clicked == []


def test_current_chat_finds_real_electron_activation_composer(tmp_path, monkeypatch) -> None:
    root = _node("BOSS直聘", 16, rect=(0, 0, 1200, 800))
    chat = _node("聊天区域", 20, rect=(400, 80, 760, 680))
    composer = _node("", 20, action="激活", rect=(430, 680, 560, 70))
    send = _node("发送", 43, action="按下", rect=(1020, 690, 80, 45))
    _add(chat, _node("莫潘玉", 41, rect=(650, 95, 120, 30)), composer, send)
    _add(root, chat)
    client = DesktopBossClient(AppConfig.defaults(), tmp_path, logger=None)
    client._root = root
    client._runtime = _Runtime()
    client._handle = 1
    client._process_id = 123
    monkeypatch.setattr(DesktopBossClient, "is_open", property(lambda _self: True))
    monkeypatch.setattr(client, "refresh", lambda: None)
    monkeypatch.setattr(client, "_activate_window", lambda: None)

    controls = client._find_chat_controls()

    assert controls is not None
    assert controls[0] is composer
    assert controls[1] is send


def test_current_chat_sends_with_real_electron_activation_composer(tmp_path, monkeypatch) -> None:
    root = _node("BOSS直聘", 16, rect=(0, 0, 1200, 800))
    chat = _node("聊天区域", 20, rect=(400, 80, 760, 680))
    composer = _node("", 20, action="激活", rect=(430, 680, 560, 70))
    send = _node("发送", 43, action="按下", rect=(1020, 690, 80, 45))
    _add(chat, _node("莫潘玉", 41, rect=(650, 95, 120, 30)), composer, send)
    _add(root, chat)
    client = DesktopBossClient(AppConfig.defaults(), tmp_path, logger=None)
    client._root = root
    client._runtime = _Runtime()
    client._handle = 1
    client._process_id = 123
    monkeypatch.setattr(DesktopBossClient, "is_open", property(lambda _self: True))
    monkeypatch.setattr(client, "refresh", lambda: None)
    monkeypatch.setattr(client, "_activate_window", lambda: None)
    monkeypatch.setattr(
        client,
        "_set_composer_value",
        lambda _composer, value: setattr(composer.accessible, "value", value),
    )

    def complete_send() -> None:
        _add(root, _node(composer.accessible.value, 41))
        root.children_cache = None
        composer.accessible.value = ""

    send.accessible.on_invoke = complete_send

    result = client.send_current_chat_message("请问明天下午方便沟通吗？")

    assert result.success is True
    assert result.attempted is True


def test_current_chat_reply_requires_message_and_cleared_composer(tmp_path, monkeypatch) -> None:
    client, root, composer, send = _client(tmp_path, monkeypatch)

    def set_message(_composer: _MsaaNode, value: str) -> None:
        composer.accessible.value = value

    def complete_send() -> None:
        _add(root, _node(composer.accessible.value, 41))
        composer.accessible.value = ""

    monkeypatch.setattr(client, "_set_composer_value", set_message)
    send.accessible.on_invoke = complete_send

    result = client.send_current_chat_message("请问明天下午方便面试吗？")

    assert result.success is True
    assert result.attempted is True
    assert composer.accessible.value == ""


def test_current_chat_reply_preserves_existing_draft(tmp_path, monkeypatch) -> None:
    client, _, composer, send = _client(tmp_path, monkeypatch)
    composer.accessible.value = "用户正在编辑的草稿"
    clicked: list[bool] = []
    send.accessible.on_invoke = lambda: clicked.append(True)

    result = client.send_current_chat_message("另一条话术")

    assert result.success is False
    assert result.attempted is False
    assert composer.accessible.value == "用户正在编辑的草稿"
    assert clicked == []


def test_read_current_conversation_distinguishes_incoming_and_outgoing(tmp_path, monkeypatch) -> None:
    client, _, composer, _ = _client(tmp_path, monkeypatch)
    chat = composer.parent
    assert chat is not None
    _add(
        chat,
        _node(
            "您好，我对岗位有兴趣",
            41,
            description="收到的消息",
            rect=(240, 240, 280, 50),
        ),
        _node(
            "感谢回复",
            41,
            description="我发送的消息 已读",
            rect=(620, 320, 240, 50),
        ),
    )
    chat.children_cache = None

    snapshot = client.read_current_conversation()

    assert snapshot.label == "张某"
    assert [(item.direction, item.text) for item in snapshot.messages] == [
        ("incoming", "您好，我对岗位有兴趣"),
        ("outgoing", "感谢回复"),
    ]


def test_read_current_conversation_requires_reliable_header(tmp_path, monkeypatch) -> None:
    client, _, composer, send = _client(tmp_path, monkeypatch)
    chat = composer.parent
    assert chat is not None
    chat.accessible.children = [composer, send]
    chat.children_cache = None

    with pytest.raises(RuntimeError, match="无法可靠识别当前会话标题"):
        client.read_current_conversation()


def test_desktop_native_greeting_verifies_success_and_dismisses_notice(tmp_path, monkeypatch) -> None:
    client, root, _, _ = _client(tmp_path, monkeypatch)
    activated: list[bool] = []
    monkeypatch.setattr(client, "_activate_window", lambda: activated.append(True))
    snapshot = client.scan_candidates()[0]
    greeting = next(node for node in client._walk(root) if client._name(node) == "打招呼")
    dismissed: list[bool] = []

    def complete_greeting() -> None:
        notice = _node("已向牛人发送招呼", 41)
        dismiss = _node("知道了", 43, action="按下")
        dismiss.accessible.on_invoke = lambda: dismissed.append(True)
        _add(root, notice, dismiss)
        root.children_cache = None

    greeting.accessible.on_invoke = complete_greeting
    result = client.send_greeting(snapshot)
    assert result.success is True
    assert result.attempted is True
    assert result.message == "BOSS 客户端岗位默认招呼"
    assert activated == [True]
    assert dismissed == [True]


def test_desktop_native_greeting_reports_platform_quota_block(tmp_path, monkeypatch) -> None:
    client, root, _, _ = _client(tmp_path, monkeypatch)
    snapshot = client.scan_candidates()[0]
    greeting = next(node for node in client._walk(root) if client._name(node) == "打招呼")

    def show_quota_block() -> None:
        _add(root, _node("沟通权益不足，请去充值", 41))
        root.children_cache = None

    greeting.accessible.on_invoke = show_quota_block
    result = client.send_greeting(snapshot)

    assert result.success is False
    assert result.attempted is True
    assert result.platform_blocked is True
    assert "平台未发送" in result.error


def test_desktop_native_greeting_accepts_continuation_without_notice(tmp_path, monkeypatch) -> None:
    client, root, _, _ = _client(tmp_path, monkeypatch)
    snapshot = client.scan_candidates()[0]
    greeting = next(node for node in client._walk(root) if client._name(node) == "打招呼")

    def complete_greeting() -> None:
        greeting.accessible.name = "继续沟通"
        greeting.cache.clear()

    greeting.accessible.on_invoke = complete_greeting
    result = client.send_greeting(snapshot)
    assert result.success is True
    assert result.attempted is True


def test_desktop_dismisses_known_informational_popup(tmp_path, monkeypatch) -> None:
    client, root, _, _ = _client(tmp_path, monkeypatch)
    popup = _node("我知道了", 43, action="按下")
    dismissed: list[bool] = []
    popup.accessible.on_invoke = lambda: dismissed.append(True)
    _add(root, popup)
    root.children_cache = None
    assert client.dismiss_benign_popups() is True
    assert dismissed == [True]


def test_desktop_dismisses_stacked_greeting_success_notices_one_at_a_time(tmp_path, monkeypatch) -> None:
    client, root, _, _ = _client(tmp_path, monkeypatch)
    dismissed: list[str] = []

    def add_notice(label: str) -> None:
        notice = _node("已向牛人发送招呼", 41)
        dismiss = _node("知道了", 43, action="按下")

        def remove_notice() -> None:
            dismissed.append(label)
            root.accessible.children.remove(notice)
            root.accessible.children.remove(dismiss)
            root.children_cache = None

        dismiss.accessible.on_invoke = remove_notice
        _add(root, notice, dismiss)

    add_notice("first")
    add_notice("second")
    root.children_cache = None

    assert client.dismiss_benign_popups() is True
    assert client.dismiss_benign_popups() is True
    assert client.dismiss_benign_popups() is False
    assert dismissed == ["first", "second"]


def test_desktop_native_greeting_unknown_outcome_is_not_retryable(tmp_path, monkeypatch) -> None:
    client, _, _, _ = _client(tmp_path, monkeypatch)
    snapshot = client.scan_candidates()[0]
    clock = iter((0.0, 9.0))
    monkeypatch.setattr("app.desktop.time.monotonic", lambda: next(clock))
    result = client.send_greeting(snapshot)
    assert result.success is False
    assert result.attempted is True


def test_desktop_next_page_control(tmp_path, monkeypatch) -> None:
    client, root, _, _ = _client(tmp_path, monkeypatch)
    next_button = next(node for node in client._walk(root) if client._name(node) == "下一页")
    clicked: list[bool] = []
    next_button.accessible.on_invoke = lambda: clicked.append(True)
    assert client.click_next_page() is True
    assert clicked == [True]


def test_desktop_auto_scroll_detects_new_candidate(tmp_path, monkeypatch) -> None:
    client, root, _, _ = _client(tmp_path, monkeypatch)
    known = {client.scan_candidates()[0].key}
    page_downs: list[int] = []

    def page_down() -> None:
        page_downs.append(1)
        if len(page_downs) == 2:
            card = _node("", 20)
            _add(card, _node("李某", 41), _node("市场营销", 41), _node("打招呼", 43, action="按下"))
            _add(root, card)
            root.children_cache = None

    monkeypatch.setattr(client, "_send_page_down", page_down)
    monkeypatch.setattr("app.desktop.time.sleep", lambda _seconds: None)
    assert client.scroll_candidates(known, max_attempts=3) is True
    assert len(page_downs) == 2


def test_desktop_auto_scroll_stops_when_no_new_candidate(tmp_path, monkeypatch) -> None:
    client, _, _, _ = _client(tmp_path, monkeypatch)
    known = {client.scan_candidates()[0].key}
    page_downs: list[int] = []
    monkeypatch.setattr(client, "_send_page_down", lambda: page_downs.append(1))
    monkeypatch.setattr("app.desktop.time.sleep", lambda _seconds: None)
    assert client.scroll_candidates(known, max_attempts=3) is False
    assert len(page_downs) == 3


def test_desktop_recognizes_candidate_page_with_only_contacted_cards(tmp_path, monkeypatch) -> None:
    client, root, _, _ = _client(tmp_path, monkeypatch)
    greeting = next(node for node in client._walk(root) if client._name(node) == "打招呼")
    greeting.accessible.name = "继续沟通"
    greeting.cache.clear()
    _add(root, _node("滚动加载更多", 41))
    root.children_cache = None
    assert client.scan_candidates() == []
    assert client.is_candidate_page() is True
    assert client.open_candidate_page() is True


def test_window_enumeration_callback_always_continues(tmp_path, monkeypatch) -> None:
    client = DesktopBossClient(AppConfig.defaults(), tmp_path, logger=None)
    process_name = client.config.desktop_process_name
    monkeypatch.setattr(
        psutil,
        "process_iter",
        lambda _attrs: [SimpleNamespace(pid=123, info={"name": process_name})],
    )

    def get_window_process_id(handle: int) -> tuple[int, int]:
        if handle == 20:
            raise OSError("window disappeared")
        return 1, 123

    monkeypatch.setattr(win32process, "GetWindowThreadProcessId", get_window_process_id)
    monkeypatch.setattr(win32gui, "IsWindowVisible", lambda _handle: True)
    monkeypatch.setattr(win32gui, "GetClassName", lambda _handle: "Chrome_WidgetWin_0")
    last_error_resets: list[int] = []
    monkeypatch.setattr(win32api, "SetLastError", last_error_resets.append)

    def enum_windows(callback, extra) -> None:
        assert [callback(handle, extra) for handle in (10, 20, 30)] == [True, True, True]

    monkeypatch.setattr(win32gui, "EnumWindows", enum_windows)
    assert client._enumerate_windows() == [(10, 123), (30, 123)]
    assert last_error_resets == [0]
