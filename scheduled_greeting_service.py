"""定时搭话服务 —— 主人提前定好「几点、说什么」，到点她自己生成一句私聊发过去。

和私聊主动搭话（proactive_private_service）的区别：
- 主动搭话是「他很久没说话了，我自己找个由头开口」，时间不固定；
- 定时搭话是「主人圈定了时刻和主题」，时间固定、内容由模型自由发挥。

设计要点：
1. **发送复用主链路**。生成走 ``proactive_message_service.send_private_message``，
   即完整回复管线（带记忆上下文、落库、真实会话）——定时说的话也是她说的，
   后续聊天要接得上。
2. **当日去重按条目序号记**，状态落盘 <data>/scheduled_greeting_state.json。
   条目内容变了序号不变也没关系：同一条目一天只发一次是硬约束，改了内容
   想立刻再发，删掉重加即可（换到新序号）。
3. **宽限窗**：循环每 30 秒醒一次，判定「目标分钟 <= 现在 < 目标分钟 + 宽限」。
   宽限默认 10 分钟，覆盖循环慢、reload 一下的情况；超过宽限（比如插件停了
   半天再启动）就跳过，不补发——早上八点的早安，中午发出来就吓人了。
   OneBot 没连上时**不记已发**，下一个周期还会重试，直到宽限窗过去。
4. 提示词里明确告诉模型**现在是几点、要做什么**（用户原话要求），并要求她
   别用「闹钟腔」——不要报时、不要解释这是定时任务，就说人话。
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from .proactive_private_service import QQProactivePrivateService

_WEEKDAY_ZH = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")

_MAX_ENTRIES = 20
_MAX_INSTRUCTION_CHARS = 500


def _parse_hhmm(text: str) -> Optional[int]:
    """'08:30' → 510（当天分钟数）。解析失败返回 None。"""
    try:
        hh, mm = str(text).strip().replace("：", ":").split(":")
        h, m = int(hh), int(mm)
        if 0 <= h < 24 and 0 <= m < 60:
            return h * 60 + m
    except Exception:
        return None
    return None


def normalize_entries(raw: Any) -> list[dict[str, str]]:
    """把任意来源的条目列表洗成干净形态：[{time, instruction, target?}]。

    非法条目（时间解析不了、指令空白）直接丢弃，不抛异常——UI 传来的数据
    不能让保存链路炸掉。条数钳到上限，指令钳到长度上限。
    """
    if not isinstance(raw, (list, tuple)):
        return []
    entries: list[dict[str, str]] = []
    for item in raw[:_MAX_ENTRIES]:
        if not isinstance(item, dict):
            continue
        minutes = _parse_hhmm(item.get("time"))
        instruction = str(item.get("instruction") or "").strip()
        if minutes is None or not instruction:
            continue
        entry = {
            "time": f"{minutes // 60:02d}:{minutes % 60:02d}",
            "instruction": instruction[:_MAX_INSTRUCTION_CHARS],
        }
        target = str(item.get("target") or "").strip()
        if target:
            entry["target"] = target
        entries.append(entry)
    return entries


class QQScheduledGreetingService:
    STATE_FILENAME = "scheduled_greeting_state.json"

    def __init__(self, plugin: Any):
        self.plugin = plugin
        self._logger = plugin.logger
        self._task: Optional[asyncio.Task] = None
        self._state: dict[str, Any] = {"entries": {}}
        self._state_loaded = False
        self.last_skip_reason = ""

    # ── 配置 ──

    def _cfg(self, key: str, default: Any) -> Any:
        settings = getattr(self.plugin, "_qq_settings", None) or {}
        value = settings.get(key, default)
        return default if value is None else value

    def _enabled(self) -> bool:
        return bool(self._cfg("scheduled_greeting_enabled", False))

    def entries(self) -> list[dict[str, str]]:
        return normalize_entries(self._cfg("scheduled_greetings", []))

    def _grace_minutes(self) -> int:
        """宽限窗（分钟）。循环间隔拉得很长时按间隔自身兜底。"""
        interval = float(self._cfg("scheduled_greeting_check_interval_seconds", 30) or 30)
        return max(10, int(interval * 3 / 60) + 1)

    # ── 状态持久化 ──

    def _state_path(self) -> Path:
        try:
            base = Path(self.plugin.data_path())
        except Exception:
            base = Path(".")
        return base / self.STATE_FILENAME

    def _load_state(self) -> None:
        if self._state_loaded:
            return
        self._state_loaded = True
        path = self._state_path()
        try:
            if path.is_file():
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and isinstance(data.get("entries"), dict):
                    self._state = {"entries": data["entries"]}
        except Exception:
            self._logger.warning(
                "[ScheduledGreet] 状态文件读取失败，按空状态开始", exc_info=True,
            )
            self._state = {"entries": {}}

    def _save_state(self) -> None:
        path = self._state_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(self._state, ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
            tmp.replace(path)
        except Exception:
            # 落盘失败只影响"重启后还记不记得今天发过"，不该影响本轮发送
            self._logger.warning("[ScheduledGreet] 状态写入失败", exc_info=True)

    def _fired_today(self, index: int, today: str) -> bool:
        self._load_state()
        rec = self._state.get("entries", {}).get(str(index))
        return isinstance(rec, dict) and rec.get("last_date") == today

    def _mark_fired(self, index: int, today: str, text: str) -> None:
        self._state.setdefault("entries", {})[str(index)] = {
            "last_date": today,
            "last_text": str(text or "")[:200],
            "last_fired_at": time.time(),
        }
        self._save_state()

    # ── 到点判定 ──

    def _due_entries(self, now: datetime) -> list[int]:
        """现在这个时刻落在宽限窗内、且今天还没发过的条目序号。"""
        now_min = now.hour * 60 + now.minute
        grace = self._grace_minutes()
        due: list[int] = []
        today = now.strftime("%Y-%m-%d")
        for index, entry in enumerate(self.entries()):
            target = _parse_hhmm(entry["time"])
            if target is None:
                continue
            if target <= now_min < target + grace and not self._fired_today(index, today):
                due.append(index)
        return due

    # ── 提示词 ──

    def _build_prompt(self, entry: dict[str, str], now: datetime) -> str:
        time_text = f"{now.hour:02d}:{now.minute:02d}"
        weekday = _WEEKDAY_ZH[now.weekday()]
        instruction = entry["instruction"]
        return (
            "到点了。这是主人提前给你安排好的定时任务。\n"
            f"现在的时间是 {now.strftime('%Y-%m-%d')}（{weekday}）{time_text}。\n"
            f"主人给这个时刻定的任务是：「{instruction}」\n"
            "按这个任务，用你平时说话的口气给主人发一条 QQ 私聊消息，"
            "任务里让说什么就说什么。\n"
            "要求：像真人随手发的一条消息，1 到 2 句就够，口语，不要分点罗列，"
            "不要总结，不要连着问好几个问题。\n"
            "不要解释这是定时任务，不要说「按照设定」「到点了」「定时提醒」这类话，"
            "也不要报出具体时间——到没到点他看得见。"
            "任务没让你发链接或图片就不要发。"
        )

    # ── 发送 ──

    async def _fire_entry(self, index: int, entry: dict[str, str]) -> dict[str, Any]:
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        target = str(entry.get("target") or "").strip() or str(
            getattr(self.plugin, "_admin_qq", "") or ""
        ).strip()
        if not target:
            self.last_skip_reason = "没有收件人（条目未指定 target，也没有 admin）"
            return {"sent": False, "reason": self.last_skip_reason}

        qq_client = getattr(self.plugin, "qq_client", None)
        if not qq_client or not getattr(qq_client, "is_connected", False):
            # 不记已发：下一个循环周期还在宽限窗里，会再试
            self.last_skip_reason = "OneBot 未连接（本轮不记已发，宽限窗内会重试）"
            return {"sent": False, "reason": self.last_skip_reason}

        prompt = self._build_prompt(entry, now)
        self._logger.info(
            f"[ScheduledGreet] 到点 {entry['time']} target={target} "
            f"任务={entry['instruction'][:30]}"
        )
        try:
            result = await self.plugin.proactive_message_service.send_private_message(
                target=target, message=prompt,
            )
        except Exception as exc:
            self._logger.warning("[ScheduledGreet] 发送异常", exc_info=True)
            return {"sent": False, "reason": f"发送异常: {exc}"}

        payload = QQProactivePrivateService._find_payload(result)
        text = str(payload.get("generated_message") or "")
        if payload.get("status") != "sent" or not text:
            detail = (
                json.dumps(payload, ensure_ascii=False)[:300]
                if payload else repr(result)[:300]
            )
            self._logger.warning(f"[ScheduledGreet] 发送失败: {detail}")
            return {"sent": False, "reason": f"发送失败: {detail}"}

        self._mark_fired(index, today, text)
        self._logger.info(f"[ScheduledGreet] 已发送 → {target}: {text[:60]}")
        self.plugin._emit_log("INFO", f"定时搭话已发送给 {target}（{entry['time']}）")
        return {"sent": True, "target": target, "message": text, "prompt": prompt}

    async def tick(self) -> dict[str, Any]:
        """跑一次到点检查。开关不开、条目为空、没到点都安静返回。"""
        if not self._enabled():
            self.last_skip_reason = "开关关闭"
            return {"sent": False, "reason": self.last_skip_reason}
        due = self._due_entries(datetime.now())
        if not due:
            self.last_skip_reason = ""
            return {"sent": False, "reason": "没有到点的条目"}
        results = []
        for index in due:
            entries = self.entries()
            if index >= len(entries):
                continue
            result = await self._fire_entry(index, entries[index])
            results.append({"index": index, **result})
        return {"results": results}

    # ── 面板快照 ──

    def describe_state(self) -> dict[str, Any]:
        self._load_state()
        today = datetime.now().strftime("%Y-%m-%d")
        entries = self.entries()
        records = []
        for index, entry in enumerate(entries):
            rec = self._state.get("entries", {}).get(str(index))
            records.append({
                **entry,
                "fired_today": bool(isinstance(rec, dict) and rec.get("last_date") == today),
                "last_text": str((rec or {}).get("last_text") or "") if isinstance(rec, dict) else "",
            })
        return {
            "enabled": self._enabled(),
            "loop_running": self.running,
            "entries": records,
            "last_skip_reason": self.last_skip_reason,
        }

    # ── 生命周期 ──

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._load_state()
        entries = self.entries()
        self._task = asyncio.create_task(self._loop())
        if self._enabled() and entries:
            preview = "；".join(f"{e['time']} {e['instruction'][:12]}" for e in entries[:3])
            self._logger.info(
                f"[ScheduledGreet] 循环已启动，{len(entries)} 条定时任务：{preview}"
            )
        else:
            self._logger.info("[ScheduledGreet] 循环已启动（当前无生效条目或开关关闭）")

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                self._logger.warning("[ScheduledGreet] 停止时异常", exc_info=True)

    @property
    def running(self) -> bool:
        return bool(self._task and not self._task.done())

    async def _loop(self) -> None:
        interval = max(10, int(self._cfg("scheduled_greeting_check_interval_seconds", 30)))
        try:
            while True:
                await asyncio.sleep(interval)
                try:
                    await self.tick()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self._logger.warning("[ScheduledGreet] 单次判定异常", exc_info=True)
        except asyncio.CancelledError:
            raise
