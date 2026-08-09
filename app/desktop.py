from __future__ import annotations

import ctypes
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from app.candidate import make_candidate_key
from app.config import AppConfig
from app.conversation import (
    ChatMessage,
    ConversationSnapshot,
    InboxItemSnapshot,
    classify_message_direction,
    conversation_key,
    message_key,
)
from app.selectors import exact_action_label, normalize_space


# Microsoft Active Accessibility roles. Chromium/Electron exposes its semantic
# web tree through MSAA/IAccessible even on builds whose outer UIA tree contains
# only anonymous panes.
ROLE_NAMES = {
    10: "Client",
    15: "Document",
    16: "Pane",
    20: "Group",
    30: "Hyperlink",
    33: "List",
    34: "ListItem",
    41: "Text",
    42: "Edit",
    43: "Button",
}

STATE_UNAVAILABLE = 0x1
STATE_FOCUSED = 0x4
STATE_INVISIBLE = 0x8000
STATE_OFFSCREEN = 0x10000
SELFLAG_TAKEFOCUS = 0x1
VT_I4 = 3
VT_DISPATCH = 9


@dataclass(frozen=True, slots=True)
class CandidateSnapshot:
    key: str
    name: str
    url: str
    summary: str
    action_label: str


@dataclass(frozen=True, slots=True)
class SendResult:
    success: bool
    error: str = ""
    message: str = ""
    attempted: bool = False
    platform_blocked: bool = False


@dataclass(slots=True)
class _MsaaNode:
    accessible: Any
    child_id: int
    parent: "_MsaaNode | None" = None
    cache: dict[str, object] = field(default_factory=dict)
    children_cache: list["_MsaaNode"] | None = None


@dataclass(slots=True)
class _ScannedCandidate:
    snapshot: CandidateSnapshot
    action: _MsaaNode


@dataclass(slots=True)
class _ScannedInboxItem:
    snapshot: InboxItemSnapshot
    action: _MsaaNode
    rect: tuple[int, int, int, int]


class _MsaaRuntime:
    def __init__(self) -> None:
        import comtypes
        import comtypes.client
        from comtypes import POINTER

        comtypes.client.GetModule("oleacc.dll")
        from comtypes.gen.Accessibility import IAccessible

        self.comtypes = comtypes
        self.pointer = POINTER
        self.interface = IAccessible
        self.oleacc = ctypes.oledll.oleacc

    def from_window(self, handle: int) -> _MsaaNode:
        accessible = self.pointer(self.interface)()
        result = self.oleacc.AccessibleObjectFromWindow(
            handle,
            ctypes.c_long(-4),  # OBJID_CLIENT
            ctypes.byref(self.interface._iid_),
            ctypes.byref(accessible),
        )
        if result != 0 or not accessible:
            raise RuntimeError(f"无法读取 BOSS 客户端可访问性根节点（HRESULT=0x{result & 0xFFFFFFFF:08x}）")
        return _MsaaNode(accessible, 0)

    def children(self, node: _MsaaNode) -> list[_MsaaNode]:
        if node.child_id != 0:
            return []
        try:
            count = int(node.accessible.accChildCount)
        except Exception:
            return []
        if count <= 0:
            return []

        from comtypes.automation import VARIANT

        variants = (VARIANT * count)()
        obtained = ctypes.c_long()
        result = self.oleacc.AccessibleChildren(
            node.accessible,
            0,
            count,
            variants,
            ctypes.byref(obtained),
        )
        if result not in (0, 1):  # S_OK or S_FALSE (partial result)
            return []
        children: list[_MsaaNode] = []
        for index in range(obtained.value):
            variant = variants[index]
            try:
                if variant.vt == VT_DISPATCH:
                    accessible = variant.value.QueryInterface(self.interface)
                    children.append(_MsaaNode(accessible, 0, node))
                elif variant.vt == VT_I4:
                    children.append(_MsaaNode(node.accessible, int(variant.value), node))
            except Exception:
                continue
        return children


class DesktopBossClient:
    """使用 Win32 宿主句柄 + MSAA 语义树控制 BOSS Electron 客户端。"""

    _MENU_TEXTS = {
        "消息",
        "意向沟通",
        "推荐",
        "搜索",
        "职位",
        "道具",
        "数据",
        "面试",
        "更多",
        "筛选",
        "发送",
        "打招呼",
        "立即沟通",
        "聊一聊",
    }
    _VOLATILE = re.compile(
        r"(?:刚刚|在线|活跃|分钟前|小时前|天前|今日|昨天|本周|本月|\d{1,2}:\d{2})",
        re.IGNORECASE,
    )
    _NON_NAME = re.compile(
        r"^(?:\d+(?:\.\d+)?(?:-\d+(?:\.\d+)?)?\s*(?:K|W|万)(?:/月)?|\d{1,3}岁|\d{4}(?:\.\d{1,2})?)$",
        re.IGNORECASE,
    )

    def __init__(self, config: AppConfig, screenshot_dir: Path, logger: Any):
        self.config = config
        self.screenshot_dir = screenshot_dir
        self.logger = logger
        self._handle: int | None = None
        self._process_id: int | None = None
        self._runtime: _MsaaRuntime | None = None
        self._root: _MsaaNode | None = None
        self._com_initialized = False

    @property
    def is_open(self) -> bool:
        if not self._handle:
            return False
        try:
            import win32gui

            return bool(win32gui.IsWindow(self._handle) and win32gui.IsWindowVisible(self._handle))
        except Exception:
            return False

    def _enumerate_windows(self) -> list[tuple[int, int]]:
        import psutil
        import win32api
        import win32gui
        import win32process

        process_ids = {
            process.pid
            for process in psutil.process_iter(["name"])
            if (process.info.get("name") or "").casefold() == self.config.desktop_process_name.casefold()
        }
        candidates: list[tuple[int, int]] = []

        def visit(handle: int, _extra: object) -> bool:
            try:
                _, process_id = win32process.GetWindowThreadProcessId(handle)
                if (
                    process_id in process_ids
                    and win32gui.IsWindowVisible(handle)
                    and win32gui.GetClassName(handle).startswith("Chrome_WidgetWin_")
                ):
                    candidates.append((handle, process_id))
            except Exception:
                # Windows can disappear between enumeration and inspection.
                # Returning True is required to keep EnumWindows walking; a
                # false-y callback result makes pywin32 surface the current
                # Win32 last-error value as an EnumWindows failure.
                return True
            return True

        # psutil may leave a benign Win32 last-error value behind while it
        # skips processes that disappear during iteration.  pywin32 can then
        # misreport that stale value as an EnumWindows failure, so clear it at
        # the API boundary without hiding genuine EnumWindows errors.
        win32api.SetLastError(0)
        win32gui.EnumWindows(visit, None)
        return candidates

    def open(self) -> None:
        try:
            # pywinauto 先设置 STA COM 模式，避免稍后截图/键盘模块切换线程模型。
            import pywinauto  # noqa: F401
            import comtypes
        except ImportError as exc:
            raise RuntimeError("未安装桌面自动化依赖，请先运行 install.bat") from exc

        windows = self._enumerate_windows()
        if not windows:
            raise RuntimeError(
                f"未找到 {self.config.desktop_process_name} 的可见主窗口。请先手动启动并登录 BOSS 客户端"
            )
        if len(windows) > 1:
            raise RuntimeError("检测到多个 BOSS 直聘主窗口，请只保留一个后重试")

        # 使用 pywinauto 选定的当前线程 COM apartment，避免 STA/MTA 冲突。
        comtypes.CoInitializeEx()
        self._com_initialized = True
        try:
            self._runtime = _MsaaRuntime()
            self._handle, self._process_id = windows[0]
            import win32con
            import win32gui

            # Electron 最小化时会把窗口放到屏幕外并缩成标题栏大小；连接动作
            # 使用标准恢复命令，不依赖坐标。
            win32gui.ShowWindow(self._handle, win32con.SW_RESTORE)
            time.sleep(0.3)
            self._root = self._runtime.from_window(self._handle)
            root_name = self._name(self._root)
            if self.config.desktop_window_title not in root_name:
                raise RuntimeError(f"窗口语义标题不匹配：识别到“{root_name or '空'}”")
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        self._root = None
        self._runtime = None
        self._handle = None
        self._process_id = None
        if self._com_initialized:
            self._com_initialized = False
            try:
                import comtypes

                comtypes.CoUninitialize()
            except Exception:
                pass

    def _require_root(self) -> _MsaaNode:
        if not self.is_open or self._root is None or self._runtime is None:
            raise RuntimeError("尚未连接 BOSS 直聘客户端，或客户端窗口已经关闭")
        return self._root

    def refresh(self) -> None:
        if not self.is_open or self._runtime is None or not self._handle:
            raise RuntimeError("BOSS 客户端窗口已关闭")
        self._root = self._runtime.from_window(self._handle)

    @staticmethod
    def _property(node: _MsaaNode, property_name: str, default: object = "") -> object:
        try:
            member = getattr(node.accessible, property_name)
            return member(node.child_id)
        except Exception:
            return default

    def _name(self, node: _MsaaNode) -> str:
        if "name" not in node.cache:
            node.cache["name"] = normalize_space(str(self._property(node, "accName", "") or ""))
        return str(node.cache["name"])

    def _value(self, node: _MsaaNode) -> str:
        return str(self._property(node, "accValue", "") or "")

    def _location(self, node: _MsaaNode) -> tuple[int, int, int, int] | None:
        try:
            left, top, width, height = node.accessible.accLocation(node.child_id)
            rect = (int(left), int(top), int(width), int(height))
        except Exception:
            return None
        if rect[2] <= 0 or rect[3] <= 0:
            return None
        return rect

    def _description(self, node: _MsaaNode) -> str:
        if "description" not in node.cache:
            node.cache["description"] = normalize_space(
                str(self._property(node, "accDescription", "") or "")
            )
        return str(node.cache["description"])

    def _role_number(self, node: _MsaaNode) -> int:
        if "role" in node.cache:
            return int(node.cache["role"])
        value = self._property(node, "accRole", 0)
        try:
            role = int(value)
        except (TypeError, ValueError):
            role = 0
        node.cache["role"] = role
        return role

    def _role_name(self, node: _MsaaNode) -> str:
        return ROLE_NAMES.get(self._role_number(node), f"Role{self._role_number(node)}")

    def _state(self, node: _MsaaNode) -> int:
        if "state" in node.cache:
            return int(node.cache["state"])
        value = self._property(node, "accState", STATE_UNAVAILABLE)
        try:
            state = int(value)
        except (TypeError, ValueError):
            state = STATE_UNAVAILABLE
        node.cache["state"] = state
        return state

    def _default_action(self, node: _MsaaNode) -> str:
        if "default_action" not in node.cache:
            node.cache["default_action"] = normalize_space(
                str(self._property(node, "accDefaultAction", "") or "")
            )
        return str(node.cache["default_action"])

    def _visible_enabled(self, node: _MsaaNode) -> bool:
        state = self._state(node)
        return not bool(state & (STATE_UNAVAILABLE | STATE_INVISIBLE | STATE_OFFSCREEN))

    def _children(self, node: _MsaaNode) -> list[_MsaaNode]:
        if self._runtime is None:
            return []
        if node.children_cache is None:
            node.children_cache = self._runtime.children(node)
        return node.children_cache

    def _walk(self, root: _MsaaNode | None = None, *, limit: int = 8_000) -> list[_MsaaNode]:
        start = root or self._require_root()
        result: list[_MsaaNode] = []
        stack = [start]
        while stack and len(result) < limit:
            node = stack.pop()
            result.append(node)
            # Chromium leaf roles frequently report a COM child object that only
            # repeats the same accessible name. Skipping those cuts traversal
            # latency substantially without losing actionable semantics.
            children = [] if self._role_number(node) in {30, 40, 41, 42, 43} else self._children(node)
            stack.extend(reversed(children))
        return result

    def _names_under(self, root: _MsaaNode, *, limit: int = 120) -> list[str]:
        names: list[str] = []
        for node in self._walk(root, limit=limit):
            name = self._name(node)
            if name and name not in names:
                names.append(name)
        return names

    def body_text(self, limit: int = 200_000) -> str:
        values: list[str] = []
        for node in self._walk():
            name = self._name(node)
            if name:
                values.append(name)
        return " ".join(values)[:limit]

    def unread_message_count(self) -> int:
        """Read the BOSS sidebar unread badge without opening conversations."""
        counts: list[int] = []
        for node in self._walk():
            if self._role_name(node) not in {"Hyperlink", "ListItem", "Text"}:
                continue
            label = normalize_space(self._name(node))
            match = re.fullmatch(r"消息\s*(\d+)", label)
            if match:
                counts.append(int(match.group(1)))
        return max(counts, default=0)

    def is_messages_page(self) -> bool:
        text = self.body_text()
        return "新招呼" in text and "沟通中" in text and "未读" in text

    def open_messages_page(self) -> bool:
        self.refresh()
        if self.is_messages_page():
            return True
        actions: list[tuple[_MsaaNode, tuple[str, ...]]] = []
        for node in self._walk():
            label = normalize_space(self._name(node))
            matched_labels = tuple(
                nav
                for nav in self.config.selectors["message_nav_texts"]
                if re.fullmatch(rf"{re.escape(nav)}\s*\d*", label)
            )
            if not matched_labels or self._role_name(node) not in {
                "Hyperlink",
                "Button",
                "ListItem",
            }:
                continue
            if all(node is not item[0] for item in actions):
                actions.append((node, matched_labels))
        if len(actions) != 1:
            return False
        self._invoke_navigation(actions[0][0], actions[0][1])
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            time.sleep(0.2)
            self.refresh()
            if self.is_messages_page():
                return True
        return False

    def _scan_inbox_entries(self) -> list[_ScannedInboxItem]:
        sent_statuses = set(self.config.selectors["inbox_sent_status_texts"])
        entries: list[_ScannedInboxItem] = []
        for node in self._walk():
            if self._role_name(node) != "ListItem" or not self._visible_enabled(node):
                continue
            location = self._location(node)
            if location is None:
                continue
            names = self._names_under(node, limit=60)
            unread_values = [
                int(value)
                for value in names
                if re.fullmatch(r"\d{1,2}", value) and 1 <= int(value) <= 99
            ]
            times = [value for value in names if re.fullmatch(r"\d{1,2}:\d{2}", value)]
            if not unread_values or not times or any(value in sent_statuses for value in names):
                continue
            metadata = set(times) | {str(value) for value in unread_values} | sent_statuses
            content = [value for value in names if value not in metadata]
            if len(content) < 3:
                continue
            name, job, preview = content[0], content[1], content[-1]
            if name in self._MENU_TEXTS or not preview:
                continue
            action = self._actionable_ancestor(node)
            if action is None:
                continue
            conv_key = conversation_key(f"{name}|{job}")
            unread_count = max(unread_values)
            incoming_key = message_key(
                conv_key,
                "incoming",
                f"{times[0]}|{preview}",
                unread_count,
            )
            entries.append(
                _ScannedInboxItem(
                    InboxItemSnapshot(
                        conversation_key=conv_key,
                        incoming_key=incoming_key,
                        name=name,
                        job=job,
                        preview=preview,
                        unread_count=unread_count,
                    ),
                    action,
                    location,
                )
            )
        entries.sort(key=lambda item: (item.rect[1], item.rect[0]))
        return entries

    def scan_unread_inbox(self) -> list[InboxItemSnapshot]:
        self.refresh()
        return [entry.snapshot for entry in self._scan_inbox_entries()]

    def open_inbox_conversation(self, snapshot: InboxItemSnapshot) -> bool:
        self._activate_window()
        self.refresh()
        matches = [
            entry
            for entry in self._scan_inbox_entries()
            if entry.snapshot.incoming_key == snapshot.incoming_key
        ]
        if len(matches) != 1:
            return False
        entry = matches[0]
        self._invoke(entry.action)
        list_right = entry.rect[0] + entry.rect[2]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            time.sleep(0.2)
            self.refresh()
            for node in self._walk():
                if self._name(node) != snapshot.name or not self._visible_enabled(node):
                    continue
                location = self._location(node)
                if location is not None and location[0] > list_right:
                    return True
        return False

    def current_url(self) -> str:
        return "desktop://boss-zhipin（桌面客户端不公开页面 URL）"

    def title(self) -> str:
        return self._name(self._require_root()) or self.config.desktop_window_title

    def save_screenshot(self, prefix: str) -> Path:
        if not self._handle:
            raise RuntimeError("尚未连接 BOSS 客户端")
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)
        safe_prefix = re.sub(r"[^a-zA-Z0-9_-]+", "_", prefix).strip("_") or "capture"
        target = self.screenshot_dir / f"{safe_prefix}_{datetime.now():%Y%m%d_%H%M%S_%f}.png"
        self._capture_window_image().save(target)
        return target

    def _capture_window_image(self) -> Any:
        """使用 PrintWindow 离屏捕获，避免把遮挡在前景的其他窗口截入。"""
        if not self._handle:
            raise RuntimeError("尚未连接 BOSS 客户端")
        import win32gui
        import win32ui
        from PIL import Image

        left, top, right, bottom = win32gui.GetWindowRect(self._handle)
        width, height = right - left, bottom - top
        if width < 100 or height < 100:
            raise RuntimeError("BOSS 客户端窗口尺寸异常，无法截图")

        window_dc = win32gui.GetWindowDC(self._handle)
        source_dc = win32ui.CreateDCFromHandle(window_dc)
        memory_dc = source_dc.CreateCompatibleDC()
        bitmap = win32ui.CreateBitmap()
        try:
            bitmap.CreateCompatibleBitmap(source_dc, width, height)
            memory_dc.SelectObject(bitmap)
            rendered = ctypes.windll.user32.PrintWindow(self._handle, memory_dc.GetSafeHdc(), 2)
            if not rendered:
                raise RuntimeError("Windows PrintWindow 未能捕获 BOSS 客户端")
            info = bitmap.GetInfo()
            bits = bitmap.GetBitmapBits(True)
            return Image.frombuffer(
                "RGB",
                (info["bmWidth"], info["bmHeight"]),
                bits,
                "raw",
                "BGRX",
                0,
                1,
            ).copy()
        finally:
            win32gui.DeleteObject(bitmap.GetHandle())
            memory_dc.DeleteDC()
            source_dc.DeleteDC()
            win32gui.ReleaseDC(self._handle, window_dc)

    def _actionable_ancestor(self, node: _MsaaNode) -> _MsaaNode | None:
        current: _MsaaNode | None = node
        for _ in range(4):
            if current is None:
                return None
            if self._default_action(current) and self._visible_enabled(current):
                return current
            current = current.parent
        return None

    def _candidate_container(self, action: _MsaaNode) -> _MsaaNode | None:
        allowed_types = set(self.config.selectors["candidate_container_control_types"])
        current = action.parent
        for _ in range(8):
            if current is None:
                return None
            role = self._role_name(current)
            if role in {"Window", "Document"}:
                return None
            names = self._names_under(current, limit=120)
            joined_length = sum(len(item) for item in names)
            if role in allowed_types and 3 <= len(names) <= 70 and 12 <= joined_length <= 2_000:
                return current
            current = current.parent
        return None

    def _candidate_url(self, container: _MsaaNode) -> str:
        for node in self._walk(container, limit=120):
            if self._role_name(node) != "Hyperlink":
                continue
            value = self._value(node).strip()
            if value.startswith(("https://", "http://")):
                return value
        return ""

    def _snapshot(self, container: _MsaaNode, action_label: str) -> CandidateSnapshot | None:
        nodes = self._walk(container, limit=120)
        names: list[str] = []
        for node in nodes:
            name = self._name(node)
            if name and name not in names:
                names.append(name)
        meaningful = [
            item
            for item in names
            if item not in self._MENU_TEXTS
            and item != action_label
            and not item.startswith("未加标签的图片")
        ]
        summary = normalize_space(" | ".join(meaningful))[:2_000]
        if not summary:
            return None

        # BOSS currently exposes the candidate avatar as a Graphic whose
        # accessible name is the person's name, before the salary Text node.
        # Prefer the first plausible card label regardless of role so a salary
        # such as "2-7K" cannot be mistaken for the candidate's name.  Keep the
        # configured role-based search as a fallback for alternate client builds.
        def plausible_name(value: str) -> bool:
            return (
                1 < len(value) <= 30
                and not self._VOLATILE.search(value)
                and not self._NON_NAME.fullmatch(value)
                and value not in {"期望", "优势", "工作经历", "未填写工作经历"}
            )

        candidate_name = next((value for value in meaningful if plausible_name(value)), "")
        name_types = set(self.config.selectors["candidate_name_control_types"])
        if not candidate_name:
            for node in nodes:
                value = self._name(node)
                if (
                    self._role_name(node) in name_types
                    and value in meaningful
                    and plausible_name(value)
                ):
                    candidate_name = value
                    break
        if not candidate_name:
            candidate_name = meaningful[0][:30]

        url = self._candidate_url(container)
        stable_id = ""
        for node in nodes:
            description = self._description(node)
            match = re.search(r"(?:candidate|geek)[-_ ]?id\s*[:=]\s*([\w-]+)", description, re.I)
            if match:
                stable_id = match.group(1)
                break
        stable_fields = [
            item for item in meaningful if not self._VOLATILE.search(item) and len(item) <= 100
        ][:6]
        key = make_candidate_key(
            stable_id=stable_id,
            candidate_url=url,
            stable_fields=stable_fields,
            fallback_text=summary,
        )
        if not key:
            return None
        return CandidateSnapshot(key, candidate_name, url, summary, action_label)

    def _scan_entries(self, labels: tuple[str, ...] | None = None) -> list[_ScannedCandidate]:
        labels = labels or self.config.selectors["greeting_button_texts"]
        entries: list[_ScannedCandidate] = []
        seen: set[str] = set()
        for node in self._walk():
            label = self._name(node)
            if not exact_action_label(label, labels) or not self._visible_enabled(node):
                continue
            action = self._actionable_ancestor(node)
            if action is None:
                continue
            container = self._candidate_container(action)
            if container is None:
                continue
            snapshot = self._snapshot(container, label)
            if snapshot is not None and snapshot.key not in seen:
                seen.add(snapshot.key)
                entries.append(_ScannedCandidate(snapshot, action))
        return entries

    def scan_candidates(self) -> list[CandidateSnapshot]:
        return [entry.snapshot for entry in self._scan_entries()]

    @staticmethod
    def _invoke(node: _MsaaNode) -> None:
        node.accessible.accDoDefaultAction(node.child_id)

    def _activate_window(self) -> None:
        if not self._handle:
            raise RuntimeError("BOSS 客户端句柄失效")
        from pywinauto.controls.hwndwrapper import HwndWrapper

        wrapper = HwndWrapper(self._handle)
        if wrapper.is_minimized():
            wrapper.restore()
            time.sleep(0.3)
        wrapper.set_focus()
        time.sleep(0.2)

    def _click_candidate_action(self, node: _MsaaNode) -> None:
        """Click one semantically identified action using its live rectangle."""
        import win32gui

        if not self._handle:
            raise RuntimeError("BOSS 客户端句柄失效")
        self._activate_window()
        try:
            left, top, width, height = node.accessible.accLocation(node.child_id)
        except Exception as exc:
            raise RuntimeError(f"无法读取打招呼控件位置：{exc}") from exc
        if width <= 0 or height <= 0:
            raise RuntimeError("打招呼控件尺寸异常，已取消点击")

        window_left, window_top, window_right, window_bottom = win32gui.GetWindowRect(self._handle)
        center_x = int(left + width / 2)
        center_y = int(top + height / 2)
        if not (window_left <= center_x < window_right and window_top <= center_y < window_bottom):
            raise RuntimeError("打招呼控件不在 BOSS 客户端窗口内，已取消点击")

        from pywinauto import mouse

        mouse.click(button="left", coords=(center_x, center_y))

    def _click_navigation_action(
        self, node: _MsaaNode, allowed_labels: tuple[str, ...]
    ) -> None:
        """Click an exact visible BOSS navigation item with bounded coordinates."""
        import win32gui

        label = normalize_space(self._name(node))
        base_label = next(
            (
                allowed
                for allowed in allowed_labels
                if re.fullmatch(rf"{re.escape(allowed)}\s*\d*", label)
            ),
            "",
        )
        if not base_label:
            raise RuntimeError("导航控件名称不匹配，已取消点击")
        if base_label not in self._MENU_TEXTS or not self._visible_enabled(node):
            raise RuntimeError("导航控件不可用，已取消点击")
        location = self._location(node)
        if location is None:
            raise RuntimeError("无法读取导航控件位置，已取消点击")
        if not self._handle:
            raise RuntimeError("BOSS 客户端句柄失效")

        left, top, width, height = location
        center_x = int(left + width / 2)
        center_y = int(top + height / 2)
        window_left, window_top, window_right, window_bottom = win32gui.GetWindowRect(
            self._handle
        )
        if not (window_left <= center_x < window_right and window_top <= center_y < window_bottom):
            raise RuntimeError("导航控件不在 BOSS 窗口内，已取消点击")

        self._activate_window()
        from pywinauto import mouse

        mouse.click(button="left", coords=(center_x, center_y))

    def _invoke_navigation(self, node: _MsaaNode, labels: tuple[str, ...]) -> None:
        default_action = self._default_action(node).casefold()
        reliable_hints = (
            "点击",
            "按下",
            "跳转",
            "选择",
            "打开",
            "click",
            "press",
            "jump",
            "select",
            "open",
        )
        if any(hint in default_action for hint in reliable_hints):
            self._invoke(node)
            return
        self._click_navigation_action(node, labels)

    def _find_navigation_control(self, labels: tuple[str, ...]) -> _MsaaNode | None:
        matches = [
            node
            for node in self._walk()
            if self._role_name(node) in {"Hyperlink", "Button", "ListItem"}
            and exact_action_label(self._name(node), labels)
            and self._visible_enabled(node)
        ]
        return matches[0] if len(matches) == 1 else None

    def _find_message_input(self) -> _MsaaNode | None:
        types = set(self.config.selectors["message_input_control_types"])
        keywords = tuple(item.casefold() for item in self.config.selectors["message_input_name_keywords"])
        for node in self._walk():
            if self._role_name(node) not in types or not self._visible_enabled(node):
                continue
            searchable = f"{self._name(node)} {self._description(node)}".casefold()
            if any(keyword in searchable for keyword in keywords):
                return node
        return None

    def _find_chat_controls(self) -> tuple[_MsaaNode, _MsaaNode] | None:
        """Find one visible composer and the send action in its semantic container."""
        types = set(self.config.selectors["message_input_control_types"])
        keywords = tuple(item.casefold() for item in self.config.selectors["message_input_name_keywords"])
        composers = [
            node
            for node in self._walk()
            if self._role_name(node) in types
            and self._visible_enabled(node)
            and any(
                keyword in f"{self._name(node)} {self._description(node)}".casefold()
                for keyword in keywords
            )
        ]
        pairs: list[tuple[_MsaaNode, _MsaaNode]] = []
        for composer in composers:
            current: _MsaaNode | None = composer.parent
            for _ in range(7):
                if current is None:
                    break
                actions: list[_MsaaNode] = []
                for node in self._walk(current, limit=300):
                    if not exact_action_label(self._name(node), self.config.selectors["send_button_texts"]):
                        continue
                    action = self._actionable_ancestor(node)
                    if action is not None and all(action is not item for item in actions):
                        actions.append(action)
                if len(actions) == 1:
                    pairs.append((composer, actions[0]))
                    break
                current = current.parent
        if len(pairs) == 1:
            return pairs[0]
        if len(pairs) > 1:
            return None

        # Current BOSS Electron builds expose the contenteditable composer as
        # an unnamed Group whose default MSAA action is "激活". Pair that group
        # with the one semantic "发送" action by containment and geometry; no
        # fixed screen coordinate is used.
        send_actions: list[_MsaaNode] = []
        for node in self._walk():
            if not exact_action_label(self._name(node), self.config.selectors["send_button_texts"]):
                continue
            action = self._actionable_ancestor(node)
            if action is not None and all(action is not item for item in send_actions):
                send_actions.append(action)
        if len(send_actions) != 1:
            return None

        send_button = send_actions[0]
        send_rect = self._location(send_button)
        activation_actions = {
            normalize_space(value).casefold()
            for value in self.config.selectors["composer_activation_actions"]
        }
        fallback_pairs: list[tuple[_MsaaNode, _MsaaNode]] = []
        current = send_button.parent
        for _ in range(8):
            if current is None:
                break
            for node in self._walk(current, limit=400):
                if node is send_button or not self._visible_enabled(node):
                    continue
                if self._role_name(node) not in {"Group", "Edit", "Document"}:
                    continue
                if normalize_space(self._default_action(node)).casefold() not in activation_actions:
                    continue
                composer_rect = self._location(node)
                if composer_rect is None or send_rect is None:
                    continue
                left, top, width, height = composer_rect
                send_left, send_top, send_width, send_height = send_rect
                vertical_overlap = min(top + height, send_top + send_height) - max(top, send_top)
                if (
                    width >= max(180, send_width * 3)
                    and left < send_left
                    and vertical_overlap > 0
                ):
                    pair = (node, send_button)
                    if all(node is not item[0] for item in fallback_pairs):
                        fallback_pairs.append(pair)
            if fallback_pairs:
                break
            current = current.parent
        return fallback_pairs[0] if len(fallback_pairs) == 1 else None

    def _composer_text(self, composer: _MsaaNode) -> str:
        value = normalize_space(self._value(composer))
        if value:
            return value
        excluded = {
            *self._MENU_TEXTS,
            *self.config.selectors["message_input_name_keywords"],
            *self.config.selectors["send_button_texts"],
        }
        parts = [
            value
            for value in self._names_under(composer, limit=120)
            if value not in excluded and normalize_space(value)
        ]
        return normalize_space(" ".join(parts))

    def _chat_container(self, composer: _MsaaNode, send_button: _MsaaNode) -> _MsaaNode:
        current = composer.parent
        fallback = self._require_root()
        composer_rect = self._location(composer)
        smallest_shared: _MsaaNode | None = None
        for _ in range(8):
            if current is None:
                break
            fallback = current
            if any(node is send_button for node in self._walk(current, limit=500)):
                if smallest_shared is None:
                    smallest_shared = current
                container_rect = self._location(current)
                if composer_rect is None or container_rect is None:
                    return current
                _left, top, _width, height = container_rect
                _composer_left, composer_top, _composer_width, composer_height = composer_rect
                if (
                    height >= max(240, composer_height * 3)
                    and top < composer_top - max(40, composer_height)
                ):
                    return current
            current = current.parent
        return smallest_shared or fallback

    def _conversation_label(self, chat_root: _MsaaNode, composer_top: int | None) -> str:
        types = set(self.config.selectors["conversation_header_control_types"])
        candidates: list[tuple[int, str]] = []
        chat_location = self._location(chat_root)
        header_max_top: float | None = None
        if chat_location is not None:
            _chat_left, chat_top, _chat_width, chat_height = chat_location
            header_max_top = chat_top + max(60.0, chat_height * 0.18)
        for node in self._walk(chat_root, limit=800):
            if self._role_name(node) not in types or not self._visible_enabled(node):
                continue
            text = self._name(node)
            if not text or text in self._MENU_TEXTS:
                continue
            if any(keyword in text for keyword in self.config.selectors["message_input_name_keywords"]):
                continue
            location = self._location(node)
            if location is None:
                continue
            _left, top, _width, height = location
            if header_max_top is not None and top > header_max_top:
                continue
            if composer_top is not None and top + height >= composer_top:
                continue
            description = self._description(node).casefold()
            direction_markers = (
                *self.config.selectors["incoming_message_markers"],
                *self.config.selectors["outgoing_message_markers"],
            )
            if any(marker.casefold() in description for marker in direction_markers):
                continue
            candidates.append((top, text))
        if not candidates:
            return ""
        candidates.sort(key=lambda item: item[0])
        labels: list[str] = []
        for _top, text in candidates:
            if text not in labels:
                labels.append(text)
            if len(labels) >= 3:
                break
        return " | ".join(labels)

    def read_current_conversation(self) -> ConversationSnapshot:
        """Read the visible current chat without selecting or opening any conversation."""
        self.refresh()
        controls = self._find_chat_controls()
        if controls is None:
            raise RuntimeError("当前页面未识别到唯一的聊天输入框和发送按钮")
        composer, send_button = controls
        chat_root = self._chat_container(composer, send_button)
        chat_rect = self._location(chat_root)
        if chat_rect is None and self._handle:
            try:
                import win32gui

                left, top, right, bottom = win32gui.GetWindowRect(self._handle)
                chat_rect = (left, top, right - left, bottom - top)
            except Exception:
                chat_rect = None
        composer_rect = self._location(composer)
        composer_top = composer_rect[1] if composer_rect is not None else None
        label = self._conversation_label(chat_root, composer_top)
        if not label:
            raise RuntimeError("无法可靠识别当前会话标题，已禁止自动回复")
        conv_key = conversation_key(label)
        types = set(self.config.selectors["chat_message_control_types"])
        raw_messages: list[tuple[int, int, str, str]] = []
        seen: set[tuple[str, str, tuple[int, int, int, int] | None]] = set()
        for node in self._walk(chat_root, limit=2_000):
            if node is chat_root or node is composer or node is send_button:
                continue
            if self._role_name(node) not in types or not self._visible_enabled(node):
                continue
            text = self._name(node)
            if not text or text in self._MENU_TEXTS:
                continue
            if text == label and self._role_name(node) in set(
                self.config.selectors["conversation_header_control_types"]
            ):
                continue
            if any(keyword in text for keyword in self.config.selectors["message_input_name_keywords"]):
                continue
            if re.fullmatch(r"(?:\d{1,2}:\d{2}|今天|昨天|星期[一二三四五六日天])", text):
                continue
            location = self._location(node)
            if composer_top is not None and location is not None and location[1] >= composer_top:
                continue
            # Direction hints must come from accessibility metadata, not the
            # candidate-authored message body. Otherwise a message containing
            # words such as "我发送的消息" could spoof its own direction.
            semantic = self._description(node)
            direction = classify_message_direction(
                semantic_text=semantic,
                message_rect=location,
                chat_rect=chat_rect,
                incoming_markers=self.config.selectors["incoming_message_markers"],
                outgoing_markers=self.config.selectors["outgoing_message_markers"],
            )
            if direction == "unknown" and location is None:
                continue
            identity = (text, direction, location)
            if identity in seen:
                continue
            seen.add(identity)
            top = location[1] if location is not None else len(raw_messages)
            left = location[0] if location is not None else 0
            raw_messages.append((top, left, direction, text))

        raw_messages.sort(key=lambda item: (item[0], item[1]))
        occurrences: dict[tuple[str, str], int] = {}
        messages: list[ChatMessage] = []
        for _top, _left, direction, text in raw_messages:
            occurrence_key = (direction, normalize_space(text))
            occurrence = occurrences.get(occurrence_key, 0) + 1
            occurrences[occurrence_key] = occurrence
            messages.append(
                ChatMessage(
                    key=message_key(conv_key, direction, text, occurrence),
                    direction=direction,
                    text=text,
                )
            )
        return ConversationSnapshot(conv_key, label, tuple(messages))

    def _find_exact_control(self, labels: tuple[str, ...]) -> _MsaaNode | None:
        for node in self._walk():
            if exact_action_label(self._name(node), labels) and self._visible_enabled(node):
                action = self._actionable_ancestor(node)
                if action is not None:
                    return action
        return None

    def dismiss_benign_popups(self) -> bool:
        """Dismiss known informational overlays that can block candidate cards."""
        control = self._find_notice_dismiss(
            self.config.selectors["greeting_success_texts"],
            self.config.selectors["greeting_dismiss_button_texts"],
        )
        if control is not None:
            self._invoke(control)
            time.sleep(0.2)
            self.refresh()
            return True
        control = self._find_exact_control(self.config.selectors["benign_dismiss_button_texts"])
        if control is None:
            return False
        self._invoke(control)
        time.sleep(0.2)
        self.refresh()
        return True

    def _find_notice_dismiss(
        self,
        success_texts: tuple[str, ...],
        dismiss_texts: tuple[str, ...],
    ) -> _MsaaNode | None:
        """Find one dismiss action in the semantic subtree containing success."""
        notices = [node for node in self._walk() if self._name(node) in success_texts]
        for notice in notices:
            current = notice.parent
            for _ in range(5):
                if current is None:
                    break
                actions: list[_MsaaNode] = []
                for node in self._walk(current, limit=160):
                    if not exact_action_label(self._name(node), dismiss_texts):
                        continue
                    action = self._actionable_ancestor(node)
                    if action is not None and all(action is not item for item in actions):
                        actions.append(action)
                if len(actions) == 1:
                    return actions[0]
                if self._role_name(current) in {"Document", "Window"}:
                    break
                current = current.parent

        # Electron sometimes exposes multiple stacked success notices as flat
        # document siblings. Pair each notice with the next dismiss action in
        # document order so stale overlays can be removed one at a time.
        nodes = self._walk()
        for index, node in enumerate(nodes):
            if self._name(node) not in success_texts:
                continue
            for following in nodes[index + 1 :]:
                if self._name(following) in success_texts:
                    break
                if not exact_action_label(self._name(following), dismiss_texts):
                    continue
                action = self._actionable_ancestor(following)
                if action is not None:
                    return action
        return None

    def _focus(self, node: _MsaaNode) -> None:
        if not self._handle:
            raise RuntimeError("BOSS 客户端句柄失效")
        from pywinauto.controls.hwndwrapper import HwndWrapper

        HwndWrapper(self._handle).set_focus()
        node.accessible.accSelect(SELFLAG_TAKEFOCUS, node.child_id)
        if not self._state(node) & STATE_FOCUSED:
            raise RuntimeError("消息输入框未获得可访问性焦点")

    def _set_composer_value(self, composer: _MsaaNode, message: str) -> None:
        # 优先使用 IAccessible value setter；Electron 不提供 setter 时，先通过
        # 语义控件获取并验证焦点，再发送 Unicode 文本。绝不使用固定坐标。
        try:
            composer.accessible.accValue[composer.child_id] = message
            if self._composer_text(composer) == normalize_space(message):
                return
        except Exception:
            pass
        self._focus(composer)
        from pywinauto.keyboard import send_keys

        send_keys("^a", pause=0.02)
        send_keys(message, with_spaces=True, pause=0.01)

    def _message_count(self, message: str) -> int:
        types = set(self.config.selectors["message_item_control_types"])
        expected = normalize_space(message)
        return sum(
            1
            for node in self._walk()
            if self._role_name(node) in types and self._name(node) == expected
        )

    def send_greeting(self, snapshot: CandidateSnapshot) -> SendResult:
        """Click BOSS's native greeting and verify its explicit success state.

        The desktop client sends its job-aware default greeting immediately
        when the greeting button is invoked.  It does not open the chat editor
        first, so this flow must never type or send a second message.
        """
        # Electron can silently ignore an accessibility default action while
        # its top-level window is not active. Activate first, then reacquire
        # the semantic node so the program never relies on a stale control.
        self._activate_window()
        self.refresh()
        matching = [entry for entry in self._scan_entries() if entry.snapshot.key == snapshot.key]
        if len(matching) != 1:
            return SendResult(False, "候选人卡片已变化或不唯一，已取消发送")

        try:
            self._click_candidate_action(matching[0].action)
        except Exception as exc:
            # The native action can have taken effect before COM reports an
            # error.  Treat this as attempted so the caller will not retry.
            return SendResult(False, f"打招呼动作结果未知：{exc}", attempted=True)
        success_texts = self.config.selectors["greeting_success_texts"]
        continuation_labels = self.config.selectors["continuation_button_texts"]
        deadline = time.monotonic() + 15
        try:
            while time.monotonic() < deadline:
                time.sleep(0.25)
                self.refresh()
                page_text = self.body_text()
                blocked_text = next(
                    (
                        text
                        for text in self.config.selectors["greeting_blocked_texts"]
                        if text in page_text
                    ),
                    None,
                )
                if blocked_text is not None:
                    return SendResult(
                        False,
                        error=f"平台未发送：检测到“{blocked_text}”",
                        attempted=True,
                        platform_blocked=True,
                    )
                success_notice = any(text in page_text for text in success_texts)
                continuation = any(
                    entry.snapshot.key == snapshot.key
                    for entry in self._scan_entries(continuation_labels)
                )
                if not success_notice and not continuation:
                    if self.dismiss_benign_popups():
                        continue
                    continue

                # The confirmation overlay blocks the next card.  Dismiss it
                # only inside the semantic subtree containing the success
                # notice. Cleanup failure cannot downgrade a proven send.
                if success_notice:
                    dismiss = self._find_notice_dismiss(
                        success_texts,
                        self.config.selectors["greeting_dismiss_button_texts"],
                    )
                    if dismiss is not None:
                        try:
                            self._invoke(dismiss)
                            time.sleep(0.15)
                            self.refresh()
                        except Exception as exc:
                            if self.logger is not None:
                                self.logger.warning("招呼已发送，但关闭成功提示失败：%s", exc)
                return SendResult(
                    True,
                    message="BOSS 客户端岗位默认招呼",
                    attempted=True,
                )
        except Exception as exc:
            return SendResult(False, f"点击后验证招呼状态失败：{exc}", attempted=True)

        return SendResult(
            False,
            "点击后未检测到‘已向牛人发送招呼’或‘继续沟通’状态",
            attempted=True,
        )

    def send_message(self, snapshot: CandidateSnapshot, message: str) -> SendResult:
        self.refresh()
        matching = [entry for entry in self._scan_entries() if entry.snapshot.key == snapshot.key]
        if len(matching) != 1:
            return SendResult(False, "候选人卡片已变化或不唯一，已取消发送")
        self._invoke(matching[0].action)

        composer = None
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and composer is None:
            time.sleep(0.2)
            self.refresh()
            composer = self._find_message_input()
        if composer is None:
            return SendResult(False, "点击后未识别到聊天消息输入框")
        send_button = self._find_exact_control(self.config.selectors["send_button_texts"])
        if send_button is None:
            return SendResult(False, "聊天区域中未找到明确的“发送”按钮")

        self._set_composer_value(composer, message)
        if self._value(composer) != message:
            return SendResult(False, "输入框未能完整填入邀约话术")
        before_count = self._message_count(message)
        self._invoke(send_button)

        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            time.sleep(0.25)
            self.refresh()
            current_composer = self._find_message_input()
            after_count = self._message_count(message)
            composer_cleared = current_composer is not None and not self._value(current_composer).strip()
            if after_count > before_count and composer_cleared:
                return SendResult(True, message=message)
        return SendResult(False, "触发发送后无法验证消息出现在聊天区且输入框清空")

    def send_current_chat_message(self, message: str, *, dry_run: bool = False) -> SendResult:
        """Send to the one chat already selected by the user; never chooses a conversation."""
        self._activate_window()
        self.refresh()
        controls = self._find_chat_controls()
        if controls is None:
            return SendResult(False, "当前页面未识别到唯一的聊天输入框和发送按钮")
        composer, send_button = controls
        existing = self._composer_text(composer)
        if existing and existing != normalize_space(message):
            return SendResult(False, "当前输入框已有其他草稿，已取消以避免覆盖")
        if dry_run:
            return SendResult(True, message=message)

        before_count = self._message_count(message)
        if not existing:
            self._set_composer_value(composer, message)
            self.refresh()
            refreshed_controls = self._find_chat_controls()
            if refreshed_controls is None:
                return SendResult(False, "填入话术后无法重新识别唯一聊天输入区")
            composer, send_button = refreshed_controls
            if self._composer_text(composer) != normalize_space(message):
                return SendResult(False, "输入框未能完整填入回复话术")
        try:
            self._invoke(send_button)
        except Exception as exc:
            return SendResult(False, f"点击发送后的结果未知：{exc}", attempted=True)

        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            time.sleep(0.25)
            self.refresh()
            current_controls = self._find_chat_controls()
            after_count = self._message_count(message)
            composer_cleared = (
                current_controls is not None
                and not self._composer_text(current_controls[0])
            )
            if after_count > before_count and composer_cleared:
                return SendResult(True, message=message, attempted=True)
        return SendResult(
            False,
            "点击发送后无法验证消息出现在聊天区且输入框清空",
            attempted=True,
        )

    def click_next_page(self) -> bool:
        button = self._find_exact_control(self.config.selectors["next_page_texts"])
        if button is None:
            return False
        self._invoke(button)
        time.sleep(1)
        return True

    def is_candidate_page(self) -> bool:
        """Recognize the candidate feed even when every visible card is contacted."""
        if self.scan_candidates():
            return True
        page_text = self.body_text()
        return "滚动加载更多" in page_text or ("推荐" in page_text and "最新" in page_text and "期望" in page_text)

    def open_candidate_page(self) -> bool:
        """Open the recommendation page when the client starts elsewhere."""
        self.refresh()
        if self.is_candidate_page():
            return True
        button = self._find_navigation_control(("推荐",))
        if button is None:
            return False
        self._invoke_navigation(button, ("推荐",))
        time.sleep(1)
        self.refresh()
        return self.is_candidate_page()

    def _send_page_down(self) -> None:
        if not self._handle:
            raise RuntimeError("BOSS 客户端窗口已关闭")
        from pywinauto.controls.hwndwrapper import HwndWrapper
        from pywinauto.keyboard import send_keys

        HwndWrapper(self._handle).set_focus()
        send_keys("{PGDN}", pause=0.05)

    def scroll_candidates(self, known_keys: set[str], *, max_attempts: int = 6) -> bool:
        """Scroll an infinite candidate list until at least one new card loads."""
        for _ in range(max_attempts):
            self._send_page_down()
            time.sleep(0.8)
            self.refresh()
            if any(snapshot.key not in known_keys for snapshot in self.scan_candidates()):
                return True
        return False

    @staticmethod
    def _sanitize_text(value: str) -> str:
        value = re.sub(r"\b1\d{10}\b", "[已脱敏手机号]", value)
        value = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[已脱敏邮箱]", value)
        return value

    def diagnose(self, diagnostic_dir: Path) -> dict[str, Any]:
        self.refresh()
        nodes = self._walk()
        rows: list[dict[str, object]] = []
        role_counts: dict[str, int] = {}
        visible_buttons: list[str] = []
        for node in nodes:
            if not self._visible_enabled(node):
                continue
            role = self._role_name(node)
            name = self._name(node)
            description = self._description(node)
            action = self._default_action(node)
            role_counts[role] = role_counts.get(role, 0) + 1
            if role in {"Button", "Hyperlink"} and name:
                visible_buttons.append(name)
            if name or description or action:
                rows.append(
                    {
                        "role": role,
                        "name": self._sanitize_text(name)[:300],
                        "description": self._sanitize_text(description)[:300],
                        "default_action": action[:100],
                    }
                )
        action_counts = {
            label: sum(1 for row in rows if normalize_space(str(row["name"])) == label)
            for label in self.config.selectors["greeting_button_texts"]
        }
        screenshot = self.save_screenshot("desktop_diagnostic")
        tree_path = ""
        if self.config.diagnostics_save_tree:
            diagnostic_dir.mkdir(parents=True, exist_ok=True)
            target = diagnostic_dir / f"desktop_controls_{datetime.now():%Y%m%d_%H%M%S_%f}.json"
            target.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
            tree_path = str(target)
        return {
            "url": self.current_url(),
            "title": self.title(),
            "process_id": self._process_id,
            "visible_button_count": len(visible_buttons),
            "visible_buttons": visible_buttons[:200],
            "action_text_counts": action_counts,
            "control_type_counts": role_counts,
            "recognized_candidate_count": len(self.scan_candidates()),
            "screenshot": str(screenshot),
            "accessibility_tree": tree_path,
        }
