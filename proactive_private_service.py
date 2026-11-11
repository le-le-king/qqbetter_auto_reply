"""私聊主动搭话服务 —— 用户长时间不说话时，由猫娘自己起一个话头。

为什么单独一个服务：宿主的群聊破冰（attention_gate_service._try_icebreaker）
只看群，而且触发条件是「焦点反复切到冷场群」。私聊根本不进注意力系统，
没有任何路径会让它主动开口。

判定链（全部通过才发）：
1. 开关打开、插件在运行、OneBot 连着
2. 有可发对象（配置的 targets，空了就用 admin）
3. 不在静默时段
4. 距上一次互动（用户发来 / 她主动发出）够久
5. 距上次主动够久（min_gap）
6. 当日主动次数没用完
7. 疲劳没超线
8. 没有待交付的缓冲回复（别在用户刚说完话、回复还没发出去时插一脚）

**给「主动后用户一直没回」加退避**：如果她主动说过话、用户之后一直没回应，
下次等待时间逐级拉长（最多 3 级）。这一条是防“自说自话”的关键——
没有它，人一天不在，她会连发三条，看起来像机器人。

状态落在 <data>/proactive_private_state.json，重启后接着算，
不会因为一次 reload 就把“今天已经说过 3 次”忘掉。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

#: 正文里可能被模型写出来的 B 站网址。发视频时先剔掉再替换成她这次选的那条，
#: 否则会出现"正文聊 A、链接是 B"——模型记住的旧链接比新选的更容易脱口而出。
_URL_RE = re.compile(r"https?://(?:www\.|m\.)?bilibili\.com/video/[A-Za-z0-9?=&_\-]+")
_URL_RE2 = re.compile(r"https?://b23\.tv/[A-Za-z0-9]+")

# ── 场景 / 语气模板 ──
# 场景按时间段给，语气按「多久没说话」给，两两组合后不会重复得太明显。
# 刻意不写「报时」「汇报」这类措辞——那会让她一张嘴就像闹钟。

_SCENES: list[tuple[int, int, str]] = [
    (5, 9, "现在是清晨，天刚亮不久，屋里还很安静。"),
    (9, 12, "现在是上午。"),
    (12, 14, "现在是中午，差不多到饭点了。"),
    (14, 18, "现在是下午，人最容易发困的时候。"),
    (18, 22, "现在是晚上，一天的事情差不多该收了。"),
    (22, 24, "现在是夜里，周围都安静下来了。"),
    (0, 5, "现在是后半夜，大部分人都睡着了。"),
]

# 按闲置时长分成三档语气
_TONES: list[tuple[float, str]] = [
    (6 * 3600, "你有一小会儿没跟他说话了，随口搭一句就好。"),
    (24 * 3600, "你已经挺久没跟他说话了，有点想他，可以问问他最近在忙什么。"),
    (10 ** 9, "你们好久没联系了，你心里有点惦记，可以说说你最近想到的一件小事。"),
]

_HARD_RULES = (
    "要求：就像真人随手发一条消息，1 到 2 句就够，口语，不要分点罗列，"
    "不要总结，不要连着问好几个问题，不要说「有什么可以帮你」，"
    "不要提现在几点，也不要解释你为什么发这条。"
    "绝对不许编造你没真的刷到、看到或发生的事——只能聊给到你的视频素材，"
    "或者你自己的真实状态和想法。"
)

#: 搭话的"开口方式"。用户的原话是"给她搭话的命令，而不是机械地命令她一定要
#: 发一个视频"——所以这里给的是几个口吻样例，不是任务。她照着说也行，说别的
#: 也行；提不提视频、提哪条，都由她自己定。
_HOOK_HINTS = (
    "上面这些是你今天真实刷到的视频，你想提就提，不提也行。\n"
    "想说视频就照平时的口气开口，比如：\n"
    "  「刚刷到一个视频，笑死我了」\n"
    "  「你猜我今天刷到啥」\n"
    "  「刚看了个视频，还没缓过来」\n"
    "挑一条你真有话说的，说说里面到底怎么回事——谁、干了什么、哪句话好笑或"
    "哪句戳人，别只说「一个视频」，也别把上面的标题、简介原样念出来。\n"
    "要是想把那条也发给他看，就在消息最后单独写一行，只写方括号和编号——"
    "你提的是上面第 5 条就写 [5]，第 2 条就写 [2]，照着对应关系写，别照抄例句里的数字。"
    "这行字不是给他看的，不用解释；不写就不发链接。网址你也别自己写。\n"
    "不想提视频，就说你自己真实的事——问他最近在忙什么、说说你自己此刻的状态"
    "（在发呆、有点困、刚醒）都行。\n"
    "【绝对不许编】你没真的刷到、真的看到、真的发生的事，一个字都别编。"
    "禁止编造「我刷到一个视频」「我看到一只猫」「今天我出门遇到」这类"
    "你根本没有真实依据的事件——你只能聊上面列出的视频，或者你自己真实的"
    "状态和想法。没有素材就老实说没有，或者直接问他一句，别硬造。\n"
)


def _parse_hhmm(text: str) -> Optional[int]:
    """'23:00' → 1380（当天分钟数）。解析失败返回 None。"""
    try:
        hh, mm = str(text).strip().replace("：", ":").split(":")
        h, m = int(hh), int(mm)
        if 0 <= h <= 24 and 0 <= m < 60:
            return h * 60 + m
    except Exception:
        return None
    return None


class QQProactivePrivateService:
    STATE_FILENAME = "proactive_private_state.json"

    def __init__(self, plugin: Any):
        self.plugin = plugin
        self._logger = plugin.logger
        self._task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        self._state: dict[str, Any] = {"targets": {}}
        self._state_loaded = False
        self.last_skip_reason = ""

    # ── 配置 ──

    def _cfg(self, key: str, default: Any) -> Any:
        settings = getattr(self.plugin, "_qq_settings", None) or {}
        value = settings.get(key, default)
        return default if value is None else value

    def _enabled(self) -> bool:
        return bool(self._cfg("proactive_private_enabled", True))

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
                if isinstance(data, dict):
                    targets = data.get("targets")
                    self._state = {"targets": targets if isinstance(targets, dict) else {}}
        except Exception:
            self._logger.warning("[ProactivePrivate] 状态文件读取失败，按空状态开始", exc_info=True)
            self._state = {"targets": {}}

    def _save_state(self) -> None:
        path = self._state_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._state, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(path)
        except Exception:
            # 落盘失败不该影响发消息，但要在日志里留下痕迹
            self._logger.warning("[ProactivePrivate] 状态写入失败", exc_info=True)

    def _record(self, target: str) -> dict[str, Any]:
        self._load_state()
        targets = self._state.setdefault("targets", {})
        rec = targets.get(target)
        if not isinstance(rec, dict):
            rec = {}
            targets[target] = rec
        return rec

    # ── 打点（由入站处理调用） ──

    def note_user_message(self, target: str) -> None:
        """用户发来消息 → 更新时间戳，并清掉「主动后无回应」的计数。

        同步写盘：私聊入站频率很低，一个几百字节的 json 不至于成为瓶颈；
        换来的是进程被强杀时也不会丢掉“他刚回过我”这个事实。
        """
        qq = str(target or "").strip()
        if not qq:
            return
        rec = self._record(qq)
        rec["last_user_message_at"] = time.time()
        if rec.get("unanswered_proactive"):
            rec["unanswered_proactive"] = 0
        self._save_state()

    def _note_proactive_sent(self, target: str) -> None:
        rec = self._record(target)
        now = time.time()
        rec["last_proactive_at"] = now
        rec["unanswered_proactive"] = int(rec.get("unanswered_proactive") or 0) + 1
        daily = rec.get("daily")
        today = datetime.now().strftime("%Y-%m-%d")
        if not isinstance(daily, dict) or daily.get("date") != today:
            daily = {"date": today, "count": 0}
            rec["daily"] = daily
        daily["count"] = int(daily.get("count") or 0) + 1
        self._save_state()

    # ── 目标选择 ──

    def _candidate_targets(self) -> list[str]:
        raw = self._cfg("proactive_private_targets", [])
        targets: list[str] = []
        if isinstance(raw, (list, tuple)):
            targets = [str(item).strip() for item in raw if str(item).strip()]
        elif isinstance(raw, str) and raw.strip():
            targets = [part.strip() for part in raw.replace("，", ",").split(",") if part.strip()]
        if not targets:
            admin = str(getattr(self.plugin, "_admin_qq", "") or "").strip()
            if admin:
                targets = [admin]
        return targets

    # ── 时间判定 ──

    @staticmethod
    def _scene_for_now(now: datetime) -> str:
        hour = now.hour
        for start, end, text in _SCENES:
            if start <= hour < end:
                return text
        return "现在。"

    @staticmethod
    def _tone_for_idle(idle_seconds: float) -> str:
        for threshold, text in _TONES:
            if idle_seconds < threshold:
                return text
        return _TONES[-1][1]

    def _in_quiet_hours(self, now: datetime) -> bool:
        spec = str(self._cfg("proactive_private_quiet_hours", "23:00-08:00") or "").strip()
        if not spec or "-" not in spec:
            return False
        start_text, end_text = spec.split("-", 1)
        start = _parse_hhmm(start_text)
        end = _parse_hhmm(end_text)
        if start is None or end is None:
            return False
        minute = now.hour * 60 + now.minute
        if start == end:
            return False
        if start < end:
            return start <= minute < end
        # 跨午夜，例如 23:00-08:00
        return minute >= start or minute < end

    # ── 判定 ──

    def _evaluate(self, target: str) -> tuple[bool, str, dict[str, Any]]:
        """返回 (能不能发, 跳过原因, 该目标的记录)。"""
        now = datetime.now()
        now_ts = time.time()
        rec = self._record(target)

        if self._in_quiet_hours(now):
            return False, f"静默时段({self._cfg('proactive_private_quiet_hours', '23:00-08:00')})", rec

        last_user = float(rec.get("last_user_message_at") or 0)
        last_pro = float(rec.get("last_proactive_at") or 0)
        last_any = max(last_user, last_pro)
        if last_any:
            idle_seconds = now_ts - last_any
        else:
            # 还没见过任何互动：不能把 idle 当成无穷大，否则插件一启动就会
            # 立刻开口。第一次见到这个目标时落一个基线，之后按正常闲置算。
            baseline = float(rec.get("first_seen_at") or 0)
            if not baseline:
                rec["first_seen_at"] = now_ts
                self._save_state()
                return False, "首次记录，等满一个闲置周期后再判断", rec
            idle_seconds = now_ts - baseline

        min_idle = float(self._cfg("proactive_private_min_idle_minutes", 90)) * 60
        min_gap = float(self._cfg("proactive_private_min_gap_minutes", 120)) * 60

        # 主动后一直没回应 → 逐级拉长等待（最多按 3 级算）
        unanswered = int(rec.get("unanswered_proactive") or 0)
        backoff_hours = float(self._cfg("proactive_private_unanswered_backoff_hours", 4))
        extra = 0.0
        if unanswered > 0:
            extra = min(unanswered, 3) * backoff_hours * 3600
        effective_idle = min_idle + extra

        if idle_seconds < effective_idle:
            need = effective_idle / 60
            return False, f"闲置不足(已{idle_seconds / 60:.0f}分/需{need:.0f}分)", rec

        if last_pro and (now_ts - last_pro) < min_gap:
            return False, f"距上次主动过近({(now_ts - last_pro) / 60:.0f}分/需{min_gap / 60:.0f}分)", rec

        daily = rec.get("daily") if isinstance(rec.get("daily"), dict) else {}
        today = now.strftime("%Y-%m-%d")
        sent_today = int(daily.get("count") or 0) if daily.get("date") == today else 0
        limit = int(self._cfg("proactive_private_daily_limit", 3))
        if sent_today >= limit:
            return False, f"今日已主动{sent_today}次(上限{limit})", rec

        fatigue_service = getattr(self.plugin, "fatigue_service", None)
        if fatigue_service:
            try:
                fatigue = float(fatigue_service.calculate_fatigue(f"private:{target}"))
            except Exception:
                fatigue = 0.0
            max_fatigue = float(self._cfg("proactive_private_max_fatigue", 70))
            if fatigue > max_fatigue:
                return False, f"疲劳过高({fatigue:.0f}>{max_fatigue:.0f})", rec

        # 用户在别处刚说完话、回复还没交付 → 别插队
        buffer_service = getattr(self.plugin, "reply_buffer_service", None)
        if buffer_service:
            try:
                if buffer_service.has_pending(f"private:{target}"):
                    return False, "有缓冲回复待交付", rec
            except Exception:
                pass

        return True, "", rec

    # ── 搭话前刷新见闻 ──

    async def _refresh_notes_before_talk(self) -> None:
        """搭话前先刷一批新见闻，让她有新鲜素材可聊。

        设计约束：
        - 刷不动、超时、异常，一律静默放过，绝不挡搭话——退回用见闻库里
          已有的旧素材。她没话聊比「卡住不聊」强得多。
        - 用 wait_for 兜底，防止 browse_once 内部某一步（模型选片、深看）
          卡死把整次搭话也拖死。
        """
        browse = getattr(self.plugin, "bilibili_browse_service", None)
        if browse is None:
            return
        refresh = getattr(browse, "browse_once", None)
        if not callable(refresh):
            return

        timeout_s = float(self._cfg("proactive_private_refresh_timeout", 75) or 75)
        try:
            result = await asyncio.wait_for(refresh(), timeout=timeout_s)
            ok = bool(result.get("ok")) if isinstance(result, dict) else False
            if ok:
                self._logger.info("[ProactivePrivate] 搭话前已刷新见闻")
            else:
                reason = result.get("reason") if isinstance(result, dict) else "未知"
                self._logger.info(f"[ProactivePrivate] 搭话前刷新见闻未成（{reason}），沿用旧素材")
        except asyncio.TimeoutError:
            self._logger.warning(
                f"[ProactivePrivate] 搭话前刷新见闻超时（>{timeout_s:.0f}s），沿用旧素材"
            )
        except Exception:
            self._logger.warning("[ProactivePrivate] 搭话前刷新见闻异常，沿用旧素材", exc_info=True)

    # ── 提示词 ──

    async def _build_prompt(
        self, target: str, idle_seconds: float,
    ) -> tuple[str, list[dict[str, Any]]]:
        now = datetime.now()
        scene = self._scene_for_now(now)
        tone = self._tone_for_idle(idle_seconds)
        nickname = self._target_nickname(target)
        who = f"你对面的这个人是「{nickname}」。" if nickname else ""
        parts = [
            "你现在想主动给主人发一条 QQ 私聊消息。没有人在问你任何事，"
            "这条消息是你自己想说的。\n"
            f"{who}{scene}{tone}\n"
        ]

        # 这一版（2026-10-31 第三次）是"把信息全给她，话怎么说她定"。
        #
        # 前两版的路都走歪了，记在这儿免得再绕回去：
        #   1. 只给两条标题 → 她"不知道自己发的是什么"，只能说空壳；
        #   2. 给六条、要求她在正文末尾标 [[发:N]] → 她一次都没标（宿主那层
        #      "像真人发消息"的约束把机械标记挤掉了），正文还被压僵；
        #   3. 拆成两步，先单独问她"发哪条，不想发回 0" → 她老实选，但那就成了
        #      "每次主动搭话都必须发一个视频"，像完成任务，不像人在说话。
        #
        # 所以现在一次给全：六条视频的全部信息 + 几个自然的开口样例。
        # 提不提视频、提哪条、说成什么样，全部由她自己决定；链接不用她写，
        # 代码按她标的编号或按内容匹配补上。
        picked: list[dict[str, Any]] = []
        browse = getattr(self.plugin, "bilibili_browse_service", None)
        if browse is not None:
            try:
                limit = int(self._cfg("proactive_private_video_candidates", 6) or 6)
                picked = browse.candidate_notes(
                    limit=max(1, min(limit, 10)),
                    exclude=self._sent_videos(target),
                )
                digest = browse.build_full_digest(picked) if picked else ""
            except Exception:
                self._logger.warning("[ProactivePrivate] 取见闻失败", exc_info=True)
                picked, digest = [], ""
            if digest:
                parts.append(digest + "\n")
                parts.append(_HOOK_HINTS)

        parts.append(_HARD_RULES)
        return "".join(parts), picked

    # ── 发过哪些视频（避免连续几天推同一条） ──

    def _sent_videos(self, target: str) -> set[str]:
        """最近 7 天已经发过给他的视频，下一批候选里要排除。

        没有这个的话，见闻库 72 小时有效、候选又是打散的，同一条视频完全
        可能连着两天被发出去——那就不像"随手刷到"，像卡带了。
        """
        rec = self._record(target)
        raw = rec.get("sent_videos")
        if not isinstance(raw, dict):
            return set()
        cutoff = time.time() - 7 * 86400
        return {str(k) for k, v in raw.items() if float(v or 0) >= cutoff}

    def _record_sent_video(self, target: str, bvid: str) -> None:
        video_id = str(bvid or "").strip()
        if not video_id:
            return
        rec = self._record(target)
        raw = rec.get("sent_videos")
        if not isinstance(raw, dict):
            raw = {}
        raw[video_id] = time.time()
        if len(raw) > 60:
            ordered = sorted(raw.items(), key=lambda kv: float(kv[1] or 0), reverse=True)
            raw = dict(ordered[:60])
        rec["sent_videos"] = raw
        self._save_state()

    async def _ensure_video_link(
        self, text: str, picked: list[dict[str, Any]], target: str,
    ) -> str:
        """她提到哪条，就把哪条的链接补上；没提、或者认不出是哪条，就不发。

        2026-10-31 第三次返工的核心：**不再替她决定发不发视频**。以前是先单独
        问她"发哪条"，`picked` 只有一条，走到这里必然发一个链接——于是每次
        主动搭话都变成"发视频任务"。现在 `picked` 是给她的那六条，她说什么由她，
        代码只做两件收尾的事：

        1. **删掉她写的网址**。她手边没有链接，写出来的一定是从历史里翻的旧
           链接——"说的和发的不是同一条"就出在这儿。
        2. **认出她说的是哪条**：先认她自己写的编号 `[3]`；没写就按内容匹配
           （标题二元组、标签、长弹幕）。认不出来就只发正文，宁可不带链接，
           也不能说一条发另一条。

        真人在聊天里也不是每提一句就甩个链接，所以"没有链接"本身是正常的。
        """
        if not text:
            return text

        browse = getattr(self.plugin, "bilibili_browse_service", None)
        note: dict[str, Any] = {}
        clean = text
        if picked and browse is not None and hasattr(browse, "parse_pick"):
            clean, note = browse.parse_pick(text, picked)
            if note:
                self._logger.info(
                    f"[ProactivePrivate] 她自己标了编号 → "
                    f"{note.get('bvid')}（{str(note.get('title'))[:24]}）"
                )

        # 她要是把网址写进正文，先清掉——留着就是串条的源头。
        if _URL_RE.search(clean) or _URL_RE2.search(clean):
            clean = _URL_RE.sub("", clean)
            clean = _URL_RE2.sub("", clean)
            clean = re.sub(r"\n{2,}", "\n", clean).strip()
            self._logger.info("[ProactivePrivate] 正文里她自己写的网址已清掉")

        if not clean:
            return text

        if not note and picked and browse is not None and hasattr(browse, "match_note"):
            note, score = browse.match_note(clean, picked)
            if note:
                self._logger.info(
                    f"[ProactivePrivate] 没写编号，按内容对上 "
                    f"{note.get('bvid')}（{str(note.get('title'))[:24]}）得分={score:.1f}"
                )
            else:
                self._logger.info(
                    f"[ProactivePrivate] 认不出在说哪条（最高分{score:.1f}），这条不发链接"
                )

        if not note:
            return clean

        bvid = str(note.get("bvid") or "").strip()
        if not bvid:
            return clean

        url = f"https://www.bilibili.com/video/{bvid}"
        qq_client = getattr(self.plugin, "qq_client", None)
        sender = getattr(qq_client, "send_private_message_segments", None)
        if not callable(sender):
            return clean
        try:
            await sender(target, [{"type": "text", "data": {"text": url}}])
            self._record_sent_video(target, bvid)
            self._logger.info(
                f"[ProactivePrivate] 补发链接 → {target}: {url}"
                f"（{str(note.get('title'))[:24]}）"
            )
        except Exception:
            self._logger.warning("[ProactivePrivate] 补发链接失败", exc_info=True)
            return clean
        return f"{clean}\n{url}"

    def _target_nickname(self, target: str) -> str:
        try:
            manager = getattr(self.plugin, "permission_mgr", None)
            if manager:
                for user in manager.list_users():
                    if str(user.get("qq") or "").strip() == target:
                        return str(user.get("remark") or user.get("nickname") or "").strip()
        except Exception:
            pass
        return ""

    # ── 发送 ──

    async def try_proactive(self, target: str = "", *, force: bool = False) -> dict[str, Any]:
        """执行一次主动搭话判定。force=True 时跳过所有时间/次数限制（调试用）。

        仍然保留「必须连着 OneBot」这个硬前提——连接断了硬发只会抛异常。
        """
        async with self._lock:
            if not force and not self._enabled():
                self.last_skip_reason = "开关关闭"
                return {"sent": False, "reason": "开关关闭"}

            qq_client = getattr(self.plugin, "qq_client", None)
            if not qq_client or not getattr(qq_client, "is_connected", False):
                self.last_skip_reason = "OneBot 未连接"
                return {"sent": False, "reason": "OneBot 未连接"}

            candidates = self._candidate_targets()
            if target and str(target).strip():
                candidates = [str(target).strip()]
            if not candidates:
                self.last_skip_reason = "没有可发送对象（未配置 targets，也没有 admin）"
                return {"sent": False, "reason": self.last_skip_reason}

            chosen = ""
            reason = ""
            idle_seconds = 0.0
            for candidate in candidates:
                rec = self._record(candidate)
                last_any = max(
                    float(rec.get("last_user_message_at") or 0),
                    float(rec.get("last_proactive_at") or 0),
                )
                if force:
                    # 调试用：只看有没有这个人、连没连上，不看时间窗
                    chosen, idle_seconds = candidate, ((time.time() - last_any) if last_any else 0.0)
                    break
                ok, why, _ = self._evaluate(candidate)
                if ok:
                    chosen, idle_seconds = candidate, ((time.time() - last_any) if last_any else 0.0)
                    break
                reason = why
            if not chosen:
                self.last_skip_reason = reason or "无符合条件的目标"
                return {"sent": False, "reason": self.last_skip_reason}

            # 搭话前先刷一批新见闻，让她有新鲜素材可聊。刷取失败、超时都不该
            # 挡着搭话——退回用见闻库里已有的旧素材，绝不能因为刷不动就卡住
            # 或干脆不搭话。这里的等待时长设成比 browse_once 正常耗时略宽，
            # 真正超时了也照常往下走。
            await self._refresh_notes_before_talk()

            prompt, picked = await self._build_prompt(chosen, idle_seconds)
            self._logger.info(
                f"[ProactivePrivate] 准备主动搭话 target={chosen} 闲置={idle_seconds / 60:.0f}分"
                f"{' (force)' if force else ''}"
            )
            try:
                result = await self.plugin.proactive_message_service.send_private_message(
                    target=chosen, message=prompt,
                )
            except Exception as exc:
                self._logger.warning("[ProactivePrivate] 发送异常", exc_info=True)
                return {"sent": False, "reason": f"发送异常: {exc}"}

            payload = self._find_payload(result)
            text = str(payload.get("generated_message") or "")
            if payload.get("status") != "sent" or not text:
                detail = json.dumps(payload, ensure_ascii=False)[:300] if payload else repr(result)[:300]
                self._logger.warning(f"[ProactivePrivate] 发送失败: {detail}")
                return {"sent": False, "reason": f"发送失败: {detail}"}

            self._note_proactive_sent(chosen)
            text = await self._ensure_video_link(text, picked, chosen)
            # 这一轮现在走的是真实会话（persist_memory=True、非临时会话），
            # 她说的话由主链路自己落库，不再在这里补写。补写只对旧版临时
            # 会话有意义，两手都做会写成两条。
            self._logger.info(f"[ProactivePrivate] 已发送 → {chosen}: {text[:60]}")
            self.plugin._emit_log("INFO", f"主动私聊已发送给 {chosen}")
            return {
                "sent": True,
                "target": chosen,
                "message": text,
                "prompt": prompt,
            }

    # ── SDK 返回值解包 ──
    # 不用猜 Ok/Err 是 namedtuple 还是 dataclass：Ok 里必然包着
    # send_private_message 返回的那个 dict（含 status="sent" 和
    # generated_message）。顺着属性往下挖，挖到 dict 就算数，
    # 挖不到就是失败。这样对 SDK 内部结构改动免疫。

    @classmethod
    def _find_payload(cls, obj: Any, depth: int = 0) -> dict[str, Any]:
        if depth > 3 or obj is None:
            return {}
        if isinstance(obj, dict):
            return obj
        for attr in ("data", "value", "result", "payload", "_data"):
            candidate = getattr(obj, attr, None)
            if candidate is None:
                continue
            if callable(candidate):
                try:
                    candidate = candidate()
                except Exception:
                    continue
            found = cls._find_payload(candidate, depth + 1)
            if found:
                return found
        if isinstance(obj, (tuple, list)):
            for item in obj:
                found = cls._find_payload(item, depth + 1)
                if found:
                    return found
        return {}

    # ── 后台循环 ──

    def describe_state(self) -> dict[str, Any]:
        """给面板/调试入口看的快照：开关、阈值、每个目标的计数。"""
        self._load_state()
        today = datetime.now().strftime("%Y-%m-%d")
        now_ts = time.time()
        targets: dict[str, Any] = {}
        for qq, rec in (self._state.get("targets") or {}).items():
            if not isinstance(rec, dict):
                continue
            last_user = float(rec.get("last_user_message_at") or 0)
            last_pro = float(rec.get("last_proactive_at") or 0)
            daily = rec.get("daily") if isinstance(rec.get("daily"), dict) else {}
            targets[qq] = {
                "last_user_message_ago_minutes": round((now_ts - last_user) / 60, 1) if last_user else None,
                "last_proactive_ago_minutes": round((now_ts - last_pro) / 60, 1) if last_pro else None,
                "unanswered_proactive": int(rec.get("unanswered_proactive") or 0),
                "sent_today": int(daily.get("count") or 0) if daily.get("date") == today else 0,
                "videos_sent_7d": len(self._sent_videos(qq)),
            }
        return {
            "enabled": self._enabled(),
            "loop_running": self.running,
            "last_skip_reason": self.last_skip_reason,
            "config": {
                "targets": self._candidate_targets(),
                "min_idle_minutes": self._cfg("proactive_private_min_idle_minutes", 90),
                "min_gap_minutes": self._cfg("proactive_private_min_gap_minutes", 120),
                "daily_limit": self._cfg("proactive_private_daily_limit", 3),
                "quiet_hours": self._cfg("proactive_private_quiet_hours", "23:00-08:00"),
                "check_interval_seconds": self._cfg("proactive_private_check_interval_seconds", 60),
                "unanswered_backoff_hours": self._cfg("proactive_private_unanswered_backoff_hours", 4),
                "max_fatigue": self._cfg("proactive_private_max_fatigue", 70),
                "video_candidates": self._cfg("proactive_private_video_candidates", 6),
            },
            "targets": targets,
        }

    async def start(self) -> None:
        if not self._enabled():
            self._logger.info("[ProactivePrivate] 开关关闭，不启动循环")
            return
        if self._task and not self._task.done():
            return
        self._load_state()
        self._task = asyncio.create_task(self._loop())
        self._logger.info(
            f"[ProactivePrivate] 循环已启动 闲置阈值={self._cfg('proactive_private_min_idle_minutes', 90)}分 "
            f"每日上限={self._cfg('proactive_private_daily_limit', 3)}次 "
            f"静默={self._cfg('proactive_private_quiet_hours', '23:00-08:00')}"
        )

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
                self._logger.warning("[ProactivePrivate] 停止时异常", exc_info=True)
        self._logger.info("[ProactivePrivate] 循环已停止")

    @property
    def running(self) -> bool:
        return bool(self._task and not self._task.done())

    async def _loop(self) -> None:
        # 启动后别立刻判定：刚连上时插件还没热，而且人可能就在旁边。
        # 先睡一个周期，让"上一轮互动时间"有落点。
        interval = max(20, int(self._cfg("proactive_private_check_interval_seconds", 60)))
        try:
            while True:
                await asyncio.sleep(interval)
                try:
                    await self.try_proactive()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self._logger.warning("[ProactivePrivate] 单次判定异常", exc_info=True)
        except asyncio.CancelledError:
            raise
