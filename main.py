from __future__ import annotations

import os
import queue
import sys
import tkinter as tk
import json
from pathlib import Path
from tkinter import messagebox
from typing import BinaryIO

from app.config import ConfigError, load_config
from app.database import Database
from app.logging_setup import setup_logging
from app.ui import BossInviterApp


def application_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def ensure_runtime_dirs(base_dir: Path) -> dict[str, Path]:
    paths = {
        "data": base_dir / "data",
        "logs": base_dir / "logs",
        "screenshots": base_dir / "screenshots",
    }
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    return paths


def run_diagnose_once(base_dir: Path, paths: dict[str, Path]) -> int:
    """供打包验收和故障排查使用；只读控件并截图，绝不执行候选人动作。"""
    from app.desktop import DesktopBossClient

    event_queue: "queue.Queue[dict[str, object]]" = queue.Queue()
    logger = setup_logging(paths["logs"], event_queue)
    try:
        config = load_config(base_dir / "config.json")
        client = DesktopBossClient(config, paths["screenshots"], logger)
        client.open()
        try:
            result = client.diagnose(paths["screenshots"])
        finally:
            client.close()
        target = paths["logs"] / "diagnose_once.json"
        target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("一次性诊断完成：%s", target)
        return 0
    except Exception as exc:
        logger.exception("一次性诊断失败：%s", exc)
        return 1


class SingleInstanceLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle: BinaryIO | None = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, IOError):
            handle.close()
            return False
        self.handle = handle
        return True

    def release(self) -> None:
        handle = self.handle
        self.handle = None
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def main() -> int:
    base_dir = application_dir()
    paths = ensure_runtime_dirs(base_dir)
    if "--diagnose-once" in sys.argv:
        return run_diagnose_once(base_dir, paths)
    instance_lock = SingleInstanceLock(paths["data"] / "boss_inviter.lock")
    root = tk.Tk()
    if not instance_lock.acquire():
        root.withdraw()
        messagebox.showwarning("程序已在运行", "检测到另一个实例正在运行，请先关闭它。", parent=root)
        root.destroy()
        return 2

    event_queue: "queue.Queue[dict[str, object]]" = queue.Queue()
    logger = setup_logging(paths["logs"], event_queue)
    config = None
    database = None
    config_error = ""
    try:
        config = load_config(base_dir / "config.json")
        database = Database(paths["data"] / "boss_inviter.db")
        logger.info("配置加载成功；当前模式：%s", "测试模式" if config.dry_run else "正式发送模式")
    except ConfigError as exc:
        config_error = str(exc)
        logger.error(config_error)
    except Exception as exc:
        config_error = f"初始化数据库失败：{exc}"
        logger.exception(config_error)

    BossInviterApp(
        root,
        config=config,
        config_error=config_error,
        database=database,
        base_dir=base_dir,
        log_dir=paths["logs"],
        screenshot_dir=paths["screenshots"],
        event_queue=event_queue,
        logger=logger,
        on_closed=instance_lock.release,
    )
    if os.environ.get("BOSS_INVITER_SMOKE_TEST") == "1":
        root.after(900, root.event_generate, "<<SmokeClose>>")
        root.bind("<<SmokeClose>>", lambda _event: root.event_generate("<Escape>"))
        root.bind("<Escape>", lambda _event: root.destroy())
        root.after(1200, root.destroy)
    try:
        root.mainloop()
    finally:
        instance_lock.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
