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
        import win32gui
        import win32process

        process_ids = {
            process.pid
            for process in psutil.process_iter(["name"])
            if (process.info.get("name") or "").casefold() == self.config.desktop_process_name.casefold()
        }
        candidates: list[tuple[int, int]] = []

        def visit(handle: int, _extra: object) -> None:
            try:
                _, process_id = win32process.GetWindowThreadProcessId(handle)
                if (
                    process_id in process_ids
                    and win32gui.IsWindowVisible(handle)
                    and win32gui.GetClassName(handle).startswith("Chrome_WidgetWin_")
                ):
                    candidates.append((handle, process_id))
            except Exception:
                return

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

        name_types = set(self.config.selectors["candidate_name_control_types"])
        candidate_name = ""
        for node in nodes:
            value = self._name(node)
            if (
                self._role_name(node) in name_types
                and value in meaningful
                and 1 < len(value) <= 30
                and not self._VOLATILE.search(value)
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

    def _scan_entries(self) -> list[_ScannedCandidate]:
        labels = self.config.selectors["greeting_button_texts"]
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

    def _find_exact_control(self, labels: tuple[str, ...]) -> _MsaaNode | None:
        for node in self._walk():
            if exact_action_label(self._name(node), labels) and self._visible_enabled(node):
                action = self._actionable_ancestor(node)
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
            if self._value(composer) == message:
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
                return SendResult(True)
        return SendResult(False, "触发发送后无法验证消息出现在聊天区且输入框清空")

    def click_next_page(self) -> bool:
        button = self._find_exact_control(self.config.selectors["next_page_texts"])
        if button is None:
            return False
        self._invoke(button)
        time.sleep(1)
        return True

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
