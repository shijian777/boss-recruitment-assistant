from __future__ import annotations

import json
import queue
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import AppConfig
from app.database import CandidateRecord, Database
from app.desktop import CandidateSnapshot, DesktopBossClient
from app.safety import RunControl, detect_risk, evaluate_keywords


@dataclass(slots=True)
class RuntimeCounters:
    success: int = 0
    skipped: int = 0
    failed: int = 0
    consecutive_failures: int = 0
    batch_sent: int = 0


class AutomationController:
    """UI 只投递命令；所有 Windows UI Automation 对象均留在工作线程。"""

    def __init__(
        self,
        config: AppConfig,
        database: Database,
        base_dir: Path,
        screenshot_dir: Path,
        event_queue: "queue.Queue[dict[str, object]]",
        logger: Any,
    ) -> None:
        self.config = config
        self.database = database
        self.base_dir = base_dir
        self.screenshot_dir = screenshot_dir
        self.event_queue = event_queue
        self.logger = logger
        self._commands: "queue.Queue[str]" = queue.Queue()
        self._thread: threading.Thread | None = None
        self._thread_lock = threading.RLock()
        self._control = RunControl(initially_paused=True)

    @property
    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _emit(self, event_type: str, **payload: object) -> None:
        self.event_queue.put({"type": event_type, **payload})

    def _ensure_worker(self) -> None:
        with self._thread_lock:
            if self.is_alive:
                return
            self._control = RunControl(initially_paused=True)
            self._commands = queue.Queue()
            self._thread = threading.Thread(
                target=self._worker_main,
                name="boss-desktop-uia-worker",
                daemon=True,
            )
            self._thread.start()

    def connect_client(self) -> None:
        self._ensure_worker()
        self._commands.put("open")

    def diagnose(self) -> None:
        self._ensure_worker()
        self._commands.put("diagnose")

    def resume(self) -> None:
        self._ensure_worker()
        self._control.resume()
        self._commands.put("run")

    def pause(self) -> None:
        self._control.pause()
        self.logger.info("用户请求暂停")
        self._emit("state", state="已暂停")

    def stop(self) -> None:
        self.logger.info("用户请求停止")
        self._control.stop()
        self._commands.put("stop")
        self._emit("state", state="正在停止")

    def shutdown(self, timeout: float = 5.0) -> bool:
        self.stop()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        self.database.close()
        return thread is None or not thread.is_alive()

    def _record(self, snapshot: CandidateSnapshot, status: str, *, error: str = "") -> None:
        self.database.record(
            CandidateRecord(
                candidate_key=snapshot.key,
                candidate_name=snapshot.name,
                candidate_url=snapshot.url,
                candidate_summary=snapshot.summary,
                status=status,
                message=self.config.message if status in {"dry_run_match", "sent"} else "",
                error_message=error,
            )
        )

    def _finalize(self, snapshot: CandidateSnapshot, status: str, *, error: str = "") -> None:
        self.database.finalize(
            CandidateRecord(
                candidate_key=snapshot.key,
                candidate_name=snapshot.name,
                candidate_url=snapshot.url,
                candidate_summary=snapshot.summary,
                status=status,
                message=self.config.message if status == "sent" else "",
                error_message=error,
            )
        )

    def _emit_counts(self, counters: RuntimeCounters) -> None:
        self._emit(
            "counts",
            today_sent=self.database.today_sent_count(),
            success=counters.success,
            skipped=counters.skipped,
            failed=counters.failed,
        )

    def _fatal_risk(self, client: DesktopBossClient, keyword: str) -> None:
        screenshot = client.save_screenshot("risk_stop")
        self.logger.error("检测到风险关键词“%s”，已紧急停止。截图：%s", keyword, screenshot)
        self._emit("state", state=f"风险停止：{keyword}")
        self._control.stop()

    def _page_risk(self, client: DesktopBossClient) -> str | None:
        try:
            return detect_risk(client.body_text(), self.config.risk_keywords)
        except Exception as exc:
            self.logger.warning("读取页面风险文本失败：%s", exc)
            return None

    def _process_candidate(
        self,
        client: DesktopBossClient,
        snapshot: CandidateSnapshot,
        counters: RuntimeCounters,
        session_seen: set[str],
    ) -> None:
        session_seen.add(snapshot.key)
        self._emit("candidate", name=snapshot.name, summary=snapshot.summary[:240])

        if self.database.already_handled(snapshot.key, dry_run=self.config.dry_run):
            self._record(snapshot, "duplicate")
            counters.skipped += 1
            self.logger.info("跳过已处理候选人：%s", snapshot.name)
            self._emit("candidate_result", result="已跳过：数据库中已有处理记录")
            self._emit_counts(counters)
            return

        decision = evaluate_keywords(
            snapshot.summary,
            self.config.include_keywords,
            self.config.exclude_keywords,
            self.config.match_mode,
        )
        if not decision.matched:
            self._record(snapshot, "filtered", error=decision.reason)
            counters.skipped += 1
            self.logger.info("规则跳过 %s：%s", snapshot.name, decision.reason)
            self._emit("candidate_result", result=f"已跳过：{decision.reason}")
            self._emit_counts(counters)
            return

        if self.config.dry_run:
            self._record(snapshot, "dry_run_match")
            counters.success += 1
            counters.consecutive_failures = 0
            self.logger.info("测试模式匹配：%s（绝不点击沟通或发送）", snapshot.name)
            self._emit("candidate_result", result="测试模式匹配：未点击、未发送")
            self._emit_counts(counters)
            return

        if self.database.today_sent_count() >= self.config.daily_limit:
            self.logger.info("今日发送数已达到上限 %s，停止运行", self.config.daily_limit)
            self._emit("state", state="已达到每日上限")
            self._control.stop()
            return

        risk = self._page_risk(client)
        if risk:
            self._fatal_risk(client, risk)
            return

        if not self.database.reserve(snapshot.key):
            self._record(snapshot, "duplicate")
            counters.skipped += 1
            self.logger.info("候选人正在由另一发送流程处理，已跳过：%s", snapshot.name)
            self._emit_counts(counters)
            return

        try:
            result = client.send_message(snapshot, self.config.message)
            if result.success:
                self._finalize(snapshot, "sent")
                counters.success += 1
                counters.batch_sent += 1
                counters.consecutive_failures = 0
                self.logger.info("已验证发送成功：%s", snapshot.name)
                self._emit("candidate_result", result="发送成功并已验证")
                self._emit_counts(counters)
                if not self._control.interruptible_sleep(self.config.interval_seconds):
                    return
                if counters.batch_sent >= self.config.batch_size:
                    counters.batch_sent = 0
                    self.logger.info("批次完成，暂停 %s 秒", self.config.batch_pause_seconds)
                    self._emit("state", state=f"批次暂停 {self.config.batch_pause_seconds} 秒")
                    self._control.interruptible_sleep(self.config.batch_pause_seconds)
                    if not self._control.is_stopped:
                        self._emit("state", state="运行中")
                return

            self._finalize(snapshot, "failed", error=result.error)
            counters.failed += 1
            counters.consecutive_failures += 1
            screenshot = client.save_screenshot("send_unverified")
            self.logger.error("发送失败或无法验证：%s；截图：%s", result.error, screenshot)
            self._emit("candidate_result", result=f"失败：{result.error}")
            self._emit_counts(counters)
            if counters.consecutive_failures >= self.config.max_consecutive_failures:
                self.logger.error("连续失败达到上限，停止运行")
                self._emit("state", state="连续失败达到上限，已停止")
                self._control.stop()
            else:
                self._control.pause()
                self._emit("state", state="发送未验证，等待人工检查")
        except Exception as exc:
            self.database.release(snapshot.key)
            raise RuntimeError(f"发送流程异常：{exc}") from exc

    def _handle_diagnosis(self, client: DesktopBossClient) -> None:
        if not client.is_open:
            self.logger.warning("请先连接 BOSS 客户端，再执行页面诊断")
            self._emit("state", state="请先连接 BOSS 客户端")
            return
        result = client.diagnose(self.screenshot_dir)
        self.logger.info("页面诊断结果：\n%s", json.dumps(result, ensure_ascii=False, indent=2))
        self._emit("diagnosis", result=result)

    def _worker_main(self) -> None:
        client = DesktopBossClient(self.config, self.screenshot_dir, self.logger)
        counters = RuntimeCounters()
        session_seen: set[str] = set()
        active = False
        try:
            self.logger.info("自动化工作线程已启动；Windows UI Automation 将仅在此线程中使用")
            while not self._control.is_stopped:
                try:
                    command = self._commands.get(timeout=0.15 if not active else 0.01)
                except queue.Empty:
                    command = ""

                if command == "stop":
                    break
                if command == "open":
                    client.open()
                    self._control.pause()
                    active = False
                    self.logger.info("已连接 BOSS 客户端。请手动登录并进入候选人页，再点击“开始/继续”")
                    self._emit("state", state="已连接客户端，等待人工登录和定位页面")
                    continue
                if command == "diagnose":
                    self._handle_diagnosis(client)
                    continue
                if command == "run":
                    if not client.is_open:
                        self.logger.warning("请先点击“连接 BOSS 客户端”")
                        self._emit("state", state="请先连接 BOSS 客户端")
                        active = False
                        self._control.pause()
                        continue
                    active = True
                    self._control.resume()
                    self._emit("state", state="运行中")

                if not active:
                    continue
                if self._control.is_paused:
                    continue
                if not client.is_open:
                    self.logger.warning("BOSS 客户端窗口已关闭，安全停止")
                    self._emit("state", state="BOSS 客户端已关闭")
                    self._control.stop()
                    continue

                client.refresh()
                if self.database.today_sent_count() >= self.config.daily_limit:
                    self._emit("state", state="已达到每日上限")
                    self._control.stop()
                    continue
                risk = self._page_risk(client)
                if risk:
                    self._fatal_risk(client, risk)
                    continue

                snapshots = client.scan_candidates()
                remaining = [item for item in snapshots if item.key not in session_seen]
                if remaining:
                    self._process_candidate(client, remaining[0], counters, session_seen)
                    continue
                if snapshots and client.click_next_page():
                    self.logger.info("页面变化后将重新获取候选人 UIA 控件")
                    continue

                screenshot = client.save_screenshot("selector_or_page_end")
                if not snapshots:
                    self.logger.warning("未识别到候选人卡片，已截图并暂停：%s", screenshot)
                    self._emit("state", state="未识别到候选人卡片，已暂停")
                else:
                    self.logger.info("当前页候选人已处理且没有可用下一页，已暂停")
                    self._emit("state", state="当前列表处理完毕")
                self._control.pause()
                active = False
        except Exception as exc:
            self.logger.exception("自动化线程异常：%s", exc)
            try:
                if client.is_open:
                    screenshot = client.save_screenshot("automation_exception")
                    self.logger.error("异常截图：%s", screenshot)
            except Exception:
                self.logger.exception("保存异常截图失败")
            self._emit("state", state=f"异常停止：{exc}")
        finally:
            try:
                client.close()
            except Exception:
                self.logger.exception("关闭浏览器失败")
            self.database.close()
            self._emit("worker_stopped")
            self.logger.info("自动化工作线程已安全退出")
