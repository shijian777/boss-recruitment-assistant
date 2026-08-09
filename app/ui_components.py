from __future__ import annotations

import re
import tkinter as tk
from tkinter import messagebox, ttk

from app.reply_rules import ReplyLibraryError, ReplyRule, validate_reply_rules


_TERM_SEPARATOR = re.compile(r"[，,|、;；\n]+")


def split_terms(value: str) -> tuple[str, ...]:
    """Split user-entered terms, preserving order and removing duplicates."""
    result: list[str] = []
    seen: set[str] = set()
    for part in _TERM_SEPARATOR.split(value):
        item = part.strip()
        normalized = item.casefold()
        if item and normalized not in seen:
            seen.add(normalized)
            result.append(item)
    return tuple(result)


def parse_optional_positive_int(value: str, *, field_name: str) -> int | None:
    stripped = value.strip()
    if not stripped:
        return None
    try:
        parsed = int(stripped)
    except ValueError as exc:
        raise ValueError(f"{field_name}必须为空或填写正整数") from exc
    if parsed <= 0:
        raise ValueError(f"{field_name}必须为空或填写正整数")
    return parsed


class CollapsibleSection(ttk.Frame):
    def __init__(
        self,
        master: tk.Misc,
        title: str,
        *,
        expanded: bool = False,
    ) -> None:
        super().__init__(master)
        self.title = title
        self.expanded = expanded
        self.toggle_button = ttk.Button(self, command=self.toggle)
        self.toggle_button.pack(fill="x")
        self.body = ttk.Frame(self, padding=(10, 8))
        self._render()

    def _render(self) -> None:
        marker = "▼" if self.expanded else "▶"
        self.toggle_button.configure(text=f"{marker}  {self.title}")
        if self.expanded:
            self.body.pack(fill="x")
        else:
            self.body.pack_forget()

    def toggle(self) -> None:
        self.expanded = not self.expanded
        self._render()


class ReplyRuleDialog(tk.Toplevel):
    def __init__(
        self,
        master: tk.Misc,
        initial: ReplyRule | None = None,
    ) -> None:
        super().__init__(master)
        self.result: ReplyRule | None = None
        self._match_mode = initial.match_mode if initial is not None else "any"
        self.title("编辑话术" if initial is not None else "新增话术")
        self.geometry("560x410")
        self.minsize(500, 360)
        self.transient(master.winfo_toplevel())
        self.grab_set()

        container = ttk.Frame(self, padding=18)
        container.pack(fill="both", expand=True)

        self.name_var = tk.StringVar(value=initial.name if initial else "")
        self.keywords_var = tk.StringVar(
            value="，".join(initial.keywords) if initial else ""
        )
        self.enabled_var = tk.BooleanVar(
            value=initial.enabled if initial is not None else True
        )

        ttk.Label(container, text="话术名称").pack(anchor="w")
        name_entry = ttk.Entry(container, textvariable=self.name_var)
        name_entry.pack(fill="x", pady=(4, 12))

        ttk.Label(container, text="触发词").pack(anchor="w")
        ttk.Entry(container, textvariable=self.keywords_var).pack(fill="x", pady=(4, 3))
        ttk.Label(
            container,
            text="多个触发词用逗号分隔；来信命中其中任意一个即可。",
            foreground="#5d6b72",
        ).pack(anchor="w", pady=(0, 12))

        ttk.Label(container, text="固定回复").pack(anchor="w")
        self.reply_text = tk.Text(container, height=7, wrap="word")
        self.reply_text.pack(fill="both", expand=True, pady=(4, 8))
        if initial is not None:
            self.reply_text.insert("1.0", initial.reply)

        ttk.Checkbutton(
            container,
            text="启用这条话术",
            variable=self.enabled_var,
        ).pack(anchor="w")

        actions = ttk.Frame(container)
        actions.pack(fill="x", pady=(14, 0))
        ttk.Button(actions, text="取消", command=self.destroy).pack(side="right")
        ttk.Button(actions, text="保存", command=self._save).pack(
            side="right", padx=(0, 8)
        )

        self.bind("<Escape>", lambda _event: self.destroy())
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        name_entry.focus_set()

    def _save(self) -> None:
        candidate = ReplyRule(
            name=self.name_var.get().strip(),
            keywords=split_terms(self.keywords_var.get()),
            reply=self.reply_text.get("1.0", "end").strip(),
            match_mode=self._match_mode,
            enabled=self.enabled_var.get(),
        )
        try:
            validate_reply_rules((candidate,))
        except ReplyLibraryError as exc:
            messagebox.showerror("话术设置错误", str(exc), parent=self)
            return
        self.result = candidate
        self.destroy()
