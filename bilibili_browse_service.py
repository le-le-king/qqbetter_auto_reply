"""让猫娘自己去 B 站"刷视频"，并且真的记住刷到了什么。

这一层解决的是「她凭什么有话说」：
    前面几轮已经把 QQ 对话、联网搜索、主动搭话都打通了。但主动搭话时她
    能说的还是"喂，好久没动静了"这种空话——因为她没有任何属于自己的
    见闻。人不会凭空有话，都是先看了点什么、遇到了什么，才有得聊。

为什么要"刷"而不是"搜"：
    关键词搜索是任务驱动的（你问什么我查什么），而刷是漫无目的的。真人
    刷 B 站的样子是：搜个兴趣词，看几条，然后顺着旁边的推荐一路滑下去。
    所以这里的候选池是三条来源混起来的——兴趣词、全站热门、上一条的相关
    推荐，最后一条是灵魂，它让内容自己长出一条链条。

为什么只对少量视频"深看"：
    一轮 20 条。每条都去抓详情+标签+弹幕+字幕要 80 多个请求，又慢又容易
    吃 412。真正有价值的其实是少数几条——弹幕密、时长合适、刚发出来的。
    所以先按分数挑出 5 条深看（拿到弹幕金句和字幕片段），其余 15 条只留
    标题、UP、播放量这些"扫一眼就知道"的信息。这也更像真人：一屏划过去，
    只有几条会真的停下来看。

为什么不在刷的时候就生成观后感：
    那要多调一次模型，还要把她的语气提前定死。存原始素材、让对话时的
    主模型现场消化更划算——同一个视频，跟你聊的时候和跟同事聊的时候，
    她的说法本来就该不一样。
"""

from __future__ import annotations

import asyncio
import json
import math
import random
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from .bilibili_client import BilibiliClient

#: 对话里给她用的工具名。聊天消息命中视频话题时，她可以调它去"回想"。
BILIBILI_TOOL_NAME = "bilibili_recent"

#: 选片标记。她发消息时在最后单独一行写 [[发:3]]，代码据此知道"她说的就是第 3 条"，
#: 然后替她把第 3 条的链接发出去。
#:
#: 为什么要这个标记，而不是让她自己写链接或者让代码去猜：
#:   2026-10-31 的真实事故——提示词里给的是两条视频，她聊的是《买到一只星期猫》，
#:   对方要链接时她翻了历史里唯一出现过的旧链接发过去，"说的"和"发的"成了两条。
#:   改成让她写编号、代码按编号取链接之后，链接不再经过模型的手，就再也不会串。
#:   编号前后允许全角括号/方括号，模型偶尔会把 [[ ]] 写成 【 】。
PICK_MARK_RE = re.compile(r"[\[【（(]{1,2}\s*发\s*[:：]?\s*(\d{1,2})\s*[\]】）)]{1,2}")

#: 裸编号：整行只有 "[3]" / "【3】"。主动搭话时只请她这么标一次，不加"发"字。
#: 必须独占一行才算——正文里随手写的方括号数字不该被当成选片。
_BARE_PICK_RE = re.compile(r"^\s*[\[【]\s*(\d{1,2})\s*[\]】]\s*[。.]?\s*$")

#: 见闻库上限。再多也没人翻，而且文件会越来越大。
_NOTE_LIMIT = 300

#: 去重表上限。只记 bvid → 时间，3000 条约 100KB。
_SEEN_LIMIT = 3000

#: 一条视频在见闻库里存活多久。超过这个时间就不再往提示词里塞了——
#: 她"上周刷到的"和"刚刷到的"是两种语气，混在一起会显得记性混乱。
_FRESH_HOURS = 72

#: 兴趣词默认值。挑的都是能出弹幕、能出梗、时长适中的分区。
#: 太正经的（新闻、财经）刷完没什么可聊的；太抽象的她转述不出来。
_DEFAULT_KEYWORDS = [
    "猫咪", "猫 搞笑", "做饭 翻车", "恐怖游戏 实况",
    "冷知识", "手工 制作", "萌宠 日常", "AI 绘画",
]


def _parse_hhmm(text: str) -> Optional[int]:
    raw = str(text or "").strip()
    if not raw or ":" not in raw:
        return None
    try:
        hour, minute = raw.split(":", 1)
        hour, minute = int(hour), int(minute)
    except Exception:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour * 60 + minute


def _in_quiet_hours(spec: str, now: datetime) -> bool:
    """跨午夜的静默时段判断（"23:00-08:00" 这种）。"""
    if not spec or "-" not in str(spec):
        return False
    left, right = str(spec).split("-", 1)
    start, end = _parse_hhmm(left), _parse_hhmm(right)
    if start is None or end is None:
        return False
    current = now.hour * 60 + now.minute
    if start <= end:
        return start <= current < end
    return current >= start or current < end


def _gist(subtitle: str, limit: int = 260) -> str:
    """把一整段字幕压成"这条视频大概讲了什么"。

    直接截前 N 字最省事，但字幕开头九成是"大家好，今天我们来……"，截出来的
    全是客套话，模型看了还是不知道内容——她那天"不知道自己发的是什么"，
    一半原因在这里。所以均匀抽几段拼起来：前段留一点（交代主题），中段和后
    段各抽一块（真正的干货通常在这里）。
    """
    text = " ".join(str(subtitle or "").split())
    if len(text) <= limit:
        return text
    if limit < 60:
        return text[:limit]
    head_len = limit // 4
    body_budget = limit - head_len
    body = text[head_len:]
    if not body:
        return text[:limit]
    chunk = max(1, body_budget // 2)
    mid_start = max(0, len(body) // 2 - chunk // 2)
    tail_start = max(0, len(body) - chunk)
    pieces = [text[:head_len], body[mid_start:mid_start + chunk], body[tail_start:]]
    out = "…".join(p.strip() for p in pieces if p.strip())
    return out[:limit]


def _fmt_count(value: Any) -> str:
    """播放量 45231 → '4.5万'。她不看精确数字，看量级就够。"""
    try:
        n = int(value or 0)
    except (TypeError, ValueError):
        return "0"
    if n >= 100000000:
        return f"{n / 100000000:.1f}亿"
    if n >= 10000:
        return f"{n / 10000:.1f}万"
    return str(n)


def _rel_time(pubdate: Any, now_ts: float) -> str:
    """发布时间 → '3 天前发布'。不知道就不返回。"""
    try:
        ts = float(pubdate or 0)
    except (TypeError, ValueError):
        return ""
    if ts <= 0:
        return ""
    days = (now_ts - ts) / 86400.0
    if days < 1:
        return "今天发的"
    if days < 30:
        return f"{int(days)} 天前发布"
    if days < 365:
        return f"{int(days / 30)} 个月前发布"
    return f"{int(days / 365)} 年前发布"


def _bigrams(text: str) -> set[str]:
    """中文短句的一对字片段。她转述时会改词，整串匹配一定失效。"""
    s = "".join(str(text or "").split())
    return {s[i:i + 2] for i in range(max(0, len(s) - 1))}


#: 字幕匹配时要剔掉的通用二元组。这些在两个毫不相干的句子里也会重合，
#: 留着只会把分数搅浑——"一个""什么""视频"这种词到处都是。
_STOP_GRAMS = frozenset({
    "这个", "那个", "一个", "我们", "你们", "他们", "什么", "就是",
    "还是", "真的", "可以", "不是", "没有", "这样", "那样", "然后",
    "所以", "因为", "已经", "现在", "时候", "自己", "东西", "知道",
    "看到", "觉得", "有点", "一直", "其实", "但是", "不过", "还有",
    "这里", "那里", "怎么", "为什么", "视频", "一起", "两个", "最后",
})


class QQBilibiliBrowseService:
    """定时刷 B 站，把见闻落到磁盘，供对话和主动搭话取用。"""

    STATE_FILENAME = "bilibili_memories.json"

    def __init__(self, plugin: Any):
        self.plugin = plugin
        self._logger = plugin.logger
        self.client = BilibiliClient(plugin.logger)
        self._task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        self._state: dict[str, Any] = {
            "version": 1,
            "notes": [],
            "seen": {},
            "last_browse_at": 0.0,
            "last_summary": {},
            "last_diag": {},
            "login_health": {},
            "cursor": {},
        }
        self._state_loaded = False
        self.last_skip_reason = ""
        self.last_error = ""
        #: 循环心跳。见 running 的注释：宿主可能回收后台任务，
        #: 光看 _task 不够。
        self._heartbeat_at = 0.0

    # ── 配置 ──

    def _cfg(self, key: str, default: Any) -> Any:
        settings = getattr(self.plugin, "_qq_settings", None) or {}
        value = settings.get(key, default)
        return default if value is None else value

    def _enabled(self) -> bool:
        return bool(self._cfg("bilibili_browse_enabled", False))

    def _keywords(self) -> list[str]:
        raw = self._cfg("bilibili_interest_keywords", None)
        if isinstance(raw, list):
            words = [str(w).strip() for w in raw if str(w).strip()]
            if words:
                return words
        return list(_DEFAULT_KEYWORDS)

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
                    self._state = {
                        "version": 1,
                        "notes": data.get("notes") if isinstance(data.get("notes"), list) else [],
                        "seen": data.get("seen") if isinstance(data.get("seen"), dict) else {},
                        "last_browse_at": float(data.get("last_browse_at") or 0),
                        "last_summary": data.get("last_summary") if isinstance(data.get("last_summary"), dict) else {},
                        "last_diag": data.get("last_diag") if isinstance(data.get("last_diag"), dict) else {},
                        "login_health": data.get("login_health") if isinstance(data.get("login_health"), dict) else {},
                        "cursor": data.get("cursor") if isinstance(data.get("cursor"), dict) else {},
                    }
        except Exception:
            self._logger.warning("[BiliBrowse] 见闻库读取失败，按空库开始", exc_info=True)

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
            self._logger.warning("[BiliBrowse] 见闻库写入失败", exc_info=True)

    # ── 登录态 ──

    async def _sync_credential(self) -> None:
        """把登录态同步进客户端。

        优先读登录服务那份独立凭据文件；读不到再看配置里有没有手动填的。
        两条路都走空就保持未登录——标题、弹幕、热门照常能刷，只是没有字幕。
        """
        raw: dict[str, Any] = {}
        login = getattr(self.plugin, "bilibili_login_service", None)
        if login is not None:
            try:
                raw = login.load_credential()
            except Exception:
                raw = {}
        if not raw:
            fallback = self._cfg("bilibili_credential", {})
            raw = fallback if isinstance(fallback, dict) else {}
        await self.client.apply_credential(raw)

    # ── 让模型自己挑：看哪几条、发哪一条 ──

    async def _llm_ask(self, prompt: str, *, timeout: float = 30.0, max_tokens: int = 200) -> str:
        """借宿主的对话模型问一句，拿纯文本回答。

        为什么用对话模型而不是另配一套：宿主本来就把 api_key 和 base_url 放在
        get_model_api_config("conversation") 里，再配一遍等于让人多填一次。
        失败一律返回空串，调用方自己兜底——选片这一步挂了不该让整轮刷取停摆。
        """
        from utils.config_manager import get_config_manager
        from utils.llm_client import create_chat_llm_async

        cfg = get_config_manager().get_model_api_config("conversation")
        base_url = str((cfg or {}).get("base_url") or "").strip()
        model = str((cfg or {}).get("model") or "").strip()
        api_key = str((cfg or {}).get("api_key") or "").strip()
        if not base_url or not model:
            return ""
        llm = await create_chat_llm_async(
            model=model,
            base_url=base_url,
            api_key=api_key,
            max_completion_tokens=max_tokens,
            timeout=timeout,
            provider_type=(cfg or {}).get("provider_type"),
        )
        try:
            resp = await asyncio.wait_for(
                llm.ainvoke([{"role": "user", "content": prompt}]),
                timeout=timeout,
            )
            return str(getattr(resp, "content", "") or "").strip()
        finally:
            aclose = getattr(llm, "aclose", None)
            if callable(aclose):
                try:
                    await aclose()
                except Exception:
                    pass

    async def _pick_deep_by_model(
        self, candidates: list[dict[str, Any]], deep_count: int,
    ) -> tuple[list[str], str]:
        """把这一屏的标题信息交给模型，让它自己决定点开哪几条。

        为什么不用 `_score` 代劳：按播放量/弹幕量排序挑出来的，本质是"热门
        推荐"——她深看的永远是百万播放那几条，聊起来也像在念榜单。而真人刷
        B 站是"这条标题有点意思，点进去看看"，判断依据是内容，不是数字。
        把 20 条标题一次给她，让她按"我会不会真好奇、看完有没有话讲"来挑，
        才是把她当人。

        返回 (bvid 列表, 模式说明)。模型不可用/解析不出东西时返回空列表，
        调用方退回到按规则的挑法——功能不能因为模型挂了就整轮不刷。
        """
        if not candidates or deep_count <= 0:
            return [], "skip"
        if not bool(self._cfg("bilibili_pick_by_model", True)):
            return [], "off"

        lines: list[str] = []
        for idx, video in enumerate(candidates, 1):
            dur = int(video.get("duration") or 0)
            length = f"{dur // 60}分{dur % 60:02d}秒" if dur else "时长未知"
            desc = " ".join(str(video.get("desc") or "").split())[:70]
            tags = "、".join(str(t) for t in (video.get("tags") or [])[:5] if str(t).strip())
            line = f"{idx}. 《{video.get('title')}》 UP主:{video.get('up') or '?'} {length}"
            if tags:
                line += f" 标签:{tags}"
            if desc:
                line += f" 简介:{desc}"
            lines.append(line)

        prompt = (
            "你是一个爱在 B 站闲逛的普通观众，刚刷到下面这一屏视频。\n"
            f"现在只能点开其中 {deep_count} 条真正看（看弹幕、看字幕）。点开过的你才记得住"
            "内容，以后跟人聊天才说得出来；没点开的就是个标题，提起了也说不出所以然。\n\n"
            "挑的时候只凭内容，不要看数字：\n"
            "- 有没有具体的事、具体的反应，看完你能不能跟人讲一句「我今天看到个……」；\n"
            "- 你自己会不会真好奇，想看完；\n"
            f"- 尽量别挑同一类的东西（别 {deep_count} 条全是猫、全是同一类游戏）；\n"
            "- 播放量高不高一点都不重要。\n\n"
            "这一屏是：\n" + "\n".join(lines) + "\n\n"
            f"只输出你要点开的 {deep_count} 个编号，用英文逗号隔开，比如：3,7,11,2,15\n"
            "不要解释，不要写标题，不要别的字。"
        )

        try:
            answer = await self._llm_ask(prompt, timeout=30.0, max_tokens=60)
        except Exception:
            self._logger.warning("[BiliBrowse] 模型选片调用失败", exc_info=True)
            return [], "error"
        if not answer:
            return [], "empty"

        picked: list[str] = []
        for raw in re.findall(r"\d{1,2}", answer):
            idx = int(raw)
            if 1 <= idx <= len(candidates):
                bvid = str(candidates[idx - 1].get("bvid") or "")
                if bvid and bvid not in picked:
                    picked.append(bvid)
            if len(picked) >= deep_count:
                break
        if not picked:
            self._logger.info(f"[BiliBrowse] 模型选片没有可用编号: {answer[:80]}")
            return [], "unparsed"

        by_bvid = {str(v.get("bvid") or ""): v for v in candidates}
        titles = [str((by_bvid.get(b) or {}).get("title") or "")[:18] for b in picked]
        self._logger.info(
            f"[BiliBrowse] 模型选了 {len(picked)} 条深看: " + " / ".join(titles)
        )
        return picked, "model"

    # ── 打分：决定哪几条值得"停下来看" ──

    @staticmethod
    def _score(video: dict[str, Any], now_ts: float) -> float:
        """给候选视频打分——**只是模型选片失败时的兜底**，不是主要依据。

        2026-10-31 之前这里是唯一依据，结果是"深看 = 播放量最高的那几条"，
        等于拿热榜当她的兴趣。现在挑哪几条看由 `_pick_deep_by_model` 决定，
        这个函数只在模型调用挂了的时候兜一层，所以热度的权重压到很低，
        主要看"这条适不适合拿来聊"：时长别太短太长、新鲜度够。

        四个维度：
          - 播放量/弹幕量：降权到 0.4/0.8。只用来把"明显没人看"的往后排，
            不再用来把爆款往前排。
          - 新鲜度：一周内的加满，一个月内给一半，更老的不加也不减。
          - 时长：1~12 分钟最舒服。太短的（几十秒）没什么内容可讲，
            太长的（一小时以上）她"看不完"，转述出来也是空的。
        """
        play = max(int(video.get("play") or 0), 0)
        danmaku = max(int(video.get("danmaku_count") or 0), 0)
        duration = int(video.get("duration") or 0)
        pubdate = float(video.get("pubdate") or 0)

        score = math.log10(play + 10) * 0.4
        score += math.log10(danmaku + 1) * 0.8

        age_days = (now_ts - pubdate) / 86400.0 if pubdate else 999.0
        if age_days <= 7:
            score += 1.6
        elif age_days <= 30:
            score += 0.8
        elif age_days > 365:
            score -= 0.6

        if 60 <= duration <= 720:
            score += 1.2
        elif duration < 30:
            score -= 1.0
        elif duration > 3600:
            score -= 1.4

        # 弹幕密度：万播几十条弹幕和万播一条弹幕是两种视频
        if play > 0:
            density = danmaku / play
            if density > 0.02:
                score += 0.8
            elif density > 0.005:
                score += 0.3

        # 热门榜的东西她是"被推着看到"的，常常是数码评测、社会热点这类跟
        # 她人设无关的内容。留在这 20 条里没问题（真人刷首页也会划过去），
        # 但它们播放量天然很高，不压一下就会把深看名额全占满——实测第一轮
        # 5 个深看名额被热门整整齐齐占掉了 5 个。
        if str(video.get("source") or "").startswith("热门"):
            score -= 1.0
        return score

    # ── 候选池 ──

    async def _collect_candidates(self, batch_size: int) -> tuple[list[dict[str, Any]], list[str]]:
        """三条来源混成一个池子：兴趣词、热门、上一条的相关推荐。"""
        keywords = self._keywords()
        pool: list[dict[str, Any]] = []
        used: list[str] = []

        # 1) 兴趣词：随机 1~2 个，避免每小时都是同一批结果
        primary = random.choice(keywords)
        used.append(primary)
        pool.extend(await self.client.search_videos(primary, limit=15))

        if len(keywords) > 1 and random.random() < 0.45:
            secondary = random.choice([k for k in keywords if k != primary])
            used.append(secondary)
            pool.extend(await self.client.search_videos(secondary, limit=8))

        # 2) 热门：按比例掺。全站热门的内容跟她人设无关，但"被推着看"
        #    是真实体验的一部分，完全不掺反而假。
        hot_ratio = float(self._cfg("bilibili_hot_ratio", 0.15) or 0)
        hot_quota = max(0, min(int(round(batch_size * hot_ratio)), 5))
        if hot_quota:
            pool.extend(await self.client.hot_videos(limit=hot_quota + 2))

        # 3) 相关推荐：从上一轮深看的视频往外延伸，形成一条"刷的链条"
        chain_from = str((self._state.get("cursor") or {}).get("chain_from") or "")
        if chain_from and random.random() < 0.7:
            pool.extend(await self.client.related_videos(chain_from, limit=8))

        return pool, used

    def _dedupe_and_pick(
        self, pool: list[dict[str, Any]], batch_size: int,
    ) -> list[dict[str, Any]]:
        now_ts = time.time()
        seen = self._state.get("seen") or {}
        picked: list[dict[str, Any]] = []
        by_bvid: dict[str, dict[str, Any]] = {}

        for video in pool:
            bvid = str(video.get("bvid") or "").strip()
            if not bvid:
                continue
            # 已看过的不再收。这是"她不会每小时刷到同一个视频"的保证。
            if bvid in seen and now_ts - float(seen.get(bvid) or 0) < 86400 * 7:
                continue
            existing = by_bvid.get(bvid)
            if existing:
                # 同一条从两个来源都来了：合并信息，别丢热门那边的播放量
                if not existing.get("play") and video.get("play"):
                    existing["play"] = video["play"]
                if not existing.get("danmaku_count") and video.get("danmaku_count"):
                    existing["danmaku_count"] = video["danmaku_count"]
                if not existing.get("desc") and video.get("desc"):
                    existing["desc"] = video["desc"]
                continue
            video["score"] = round(self._score(video, now_ts), 2)
            by_bvid[bvid] = video
            picked.append(video)

        # 不再按分数取前 N 条。分数只用来给兜底选片排个序，20 条"她这一屏
        # 看到的东西"应该是什么样，取决于三条来源各自贡献了什么，而不是
        # 谁的播放量高——按分数取，一屏里就全是爆款，等于替她订阅了热榜。
        random.shuffle(picked)
        return picked[:batch_size]

    # ── 深看一条 ──

    @staticmethod
    def _is_hollow(note: dict[str, Any]) -> bool:
        """这条"深看"是不是看了个空壳：既没弹幕也没字幕。

        两种情况都不算：新视频本来没人发弹幕；UP 没开字幕。但拿去当谈资的
        素材一点没有，占着深看名额就是浪费——名额该让给别的。
        """
        return not (note.get("danmaku_samples") or note.get("subtitle_excerpt"))

    async def _judge_login_health(
        self, diag: dict[str, int], deep_total: int,
    ) -> dict[str, Any]:
        """根据本轮抓取结果判断登录态还在不在。

        为什么要这么绕：字幕接口在"没登录"和"cookie 过期"时都是**返回空
        列表、不报错**，光看有无异常永远是"一切正常"。唯一靠谱的信号是
        ——带了登录态却一条字幕都拿不到。只有这时候才值得多花一次请求去
        verify_login 确认；正常拿到字幕就什么都不用做。
        """
        if not self.client.has_credential:
            return {
                "state": "nologin",
                "hint": "未登录：标题和弹幕正常，字幕拿不到（B 站要登录才给）",
            }
        got = int(diag.get("subtitle.got_text") or 0)
        if got > 0:
            return {"state": "ok", "hint": "", "subtitle_hits": got}
        if deep_total < 2:
            # 深看太少，样本不够，别急着下结论。
            return {"state": "unknown", "hint": ""}
        if not (diag.get("subtitle.no_track") or diag.get("subtitle.fail")):
            return {"state": "unknown", "hint": ""}
        probe = await self.client.verify_login()
        if probe.get("valid"):
            name = str(probe.get("name") or "")
            if name and name != self.client.login_name:
                self.client.login_name = name
            return {
                "state": "ok",
                "hint": "本轮碰到的视频都没字幕，登录态本身是好的",
                "name": name,
            }
        return {
            "state": "expired",
            "hint": (
                f"登录态已失效（{probe.get('reason')}），字幕会一直缺；"
                "重新扫一次 bilibili_login_start 的二维码即可"
            ),
        }

    async def _watch(self, video: dict[str, Any]) -> dict[str, Any]:
        bvid = video["bvid"]
        detail = await self.client.video_detail(bvid)
        note = {
            "bvid": bvid,
            "title": video.get("title") or "",
            "up": video.get("up") or "",
            "duration": int(video.get("duration") or 0),
            "play": int(video.get("play") or 0),
            "danmaku_count": int(video.get("danmaku_count") or 0),
            "desc": video.get("desc") or "",
            "tags": list(video.get("tags") or []),
            "source": video.get("source") or "",
            "score": video.get("score") or 0,
            "watched_at": time.time(),
            "deep": True,
            "danmaku_samples": [],
            "subtitle_excerpt": "",
        }
        if detail:
            note.update({
                "title": detail.get("title") or note["title"],
                "up": detail.get("up") or note["up"],
                "duration": detail.get("duration") or note["duration"],
                "play": detail.get("play") or note["play"],
                "danmaku_count": detail.get("danmaku_count") or note["danmaku_count"],
                "desc": detail.get("desc") or note["desc"],
                "like": detail.get("like", 0),
                "reply": detail.get("reply", 0),
                "pubdate": detail.get("pubdate", 0),
            })
            cid = int(detail.get("cid") or 0)
            if cid:
                tags = await self.client.video_tags(bvid)
                if tags:
                    note["tags"] = tags
                note["danmaku_samples"] = await self.client.danmaku_samples(
                    bvid, cid, limit=25,
                )
                note["subtitle_excerpt"] = await self.client.subtitle_text(bvid, cid)
        return note

    # ── 主流程：刷一轮 ──

    async def browse_once(self) -> dict[str, Any]:
        async with self._lock:
            self._load_state()
            started = time.monotonic()
            batch_size = max(5, min(int(self._cfg("bilibili_browse_batch_size", 20) or 20), 50))
            deep_count = max(0, min(int(self._cfg("bilibili_browse_deep_count", 5) or 5), batch_size))

            await self._sync_credential()

            pool, keywords = await self._collect_candidates(batch_size)
            if not pool:
                self.last_error = "候选池为空（搜索和热门都没拿到东西）"
                self._logger.warning(f"[BiliBrowse] {self.last_error}")
                return {"ok": False, "reason": self.last_error}

            picked = self._dedupe_and_pick(pool, batch_size)
            if not picked:
                self.last_error = "本轮候选全是看过的"
                self._logger.info(f"[BiliBrowse] {self.last_error}")
                return {"ok": False, "reason": self.last_error, "pool": len(pool)}

            now_ts = time.time()
            notes: list[dict[str, Any]] = []
            deep_ids: list[str] = []

            # 看哪几条由她自己决定：把这一屏的标题/UP/时长/标签/简介交给模型，
            # 让它按"我会不会真好奇、看完有没有话讲"挑，而不是按播放量挑。
            # 这一步是她"知道自己在看什么"的前提——代码挑出来的爆款，她
            # 只知道标题，聊起来就是复读。
            model_picks, pick_mode = await self._pick_deep_by_model(picked, deep_count)
            deep_set: set[str] = set(model_picks)

            if not deep_set:
                # 兜底：模型没配、调用挂了、或者返回的东西解析不出来。
                # 这时按规则的挑法接上，保证每小时还是有东西看。
                hot_cap = max(1, deep_count // 3) if deep_count else 0
                hot_taken = 0
                for video in sorted(picked, key=lambda v: v.get("score") or 0, reverse=True):
                    if len(deep_set) >= deep_count:
                        break
                    is_hot = str(video.get("source") or "").startswith("热门")
                    if is_hot and hot_taken >= hot_cap:
                        continue
                    deep_set.add(video["bvid"])
                    if is_hot:
                        hot_taken += 1

            # 先深看、再拼装。分两步是为了处理"看了个空壳"：弹幕字幕全无的
            # 视频占着名额没意义（新视频没人发弹幕、UP 没开字幕），要能当场
            # 把名额让给备选，而不是白等到下一轮。
            by_bvid = {v["bvid"]: v for v in picked}
            spare = [v for v in picked if v["bvid"] not in deep_set]
            watched: dict[str, dict[str, Any]] = {}
            hollow = 0

            for bvid in [v["bvid"] for v in picked if v["bvid"] in deep_set]:
                note = await self._watch(by_bvid[bvid])
                if self._is_hollow(note):
                    note["deep"] = False
                    note["hollow"] = True
                    hollow += 1
                    self._logger.info(
                        f"[BiliBrowse] 空壳降级（无弹幕无字幕）: {str(note.get('title'))[:30]}"
                    )
                else:
                    note["deep"] = True
                watched[bvid] = note

            # 让出的名额从备选里补。连续两条都空就别补了——多半是登录态
            # 出问题，继续补只是白刷请求。
            deficit = sum(1 for v in picked if v["bvid"] in deep_set) - sum(
                1 for n in watched.values() if n.get("deep")
            )
            if deficit > 0 and hollow < 2:
                for cand in spare:
                    if deficit <= 0:
                        break
                    note = await self._watch(cand)
                    if self._is_hollow(note):
                        note["deep"] = False
                        note["hollow"] = True
                        hollow += 1
                        watched[cand["bvid"]] = note
                        if hollow >= 2:
                            break
                        continue
                    note["deep"] = True
                    watched[cand["bvid"]] = note
                    deficit -= 1

            for video in picked:
                note = watched.get(video["bvid"])
                if note is not None:
                    notes.append(note)
                    if note.get("deep"):
                        deep_ids.append(note["bvid"])
                    continue
                notes.append({
                    "bvid": video["bvid"],
                    "title": video.get("title") or "",
                    "up": video.get("up") or "",
                    "duration": int(video.get("duration") or 0),
                    "play": int(video.get("play") or 0),
                    "danmaku_count": int(video.get("danmaku_count") or 0),
                    "desc": video.get("desc") or "",
                    "tags": list(video.get("tags") or []),
                    "source": video.get("source") or "",
                    "score": video.get("score") or 0,
                    "watched_at": now_ts,
                    "deep": False,
                    "danmaku_samples": [],
                    "subtitle_excerpt": "",
                })

            # 新见闻放最前面，旧的往后滑
            existing = self._state.get("notes") or []
            self._state["notes"] = (notes + existing)[:_NOTE_LIMIT]

            seen = self._state.get("seen") or {}
            for note in notes:
                seen[note["bvid"]] = now_ts
            if len(seen) > _SEEN_LIMIT:
                # 丢掉最老的一半，避免无限膨胀
                ordered = sorted(seen.items(), key=lambda kv: float(kv[1] or 0), reverse=True)
                seen = dict(ordered[:_SEEN_LIMIT // 2])
            self._state["seen"] = seen

            elapsed = time.monotonic() - started
            # 取走本轮各接口的成败计数，交给状态和日志。异常全被 _call 吞了，
            # 不留计数就只剩"这条是空的"这一个事实，分不清原因。
            diag = self.client.take_diag()
            deep_notes = [n for n in notes if n.get("deep")]
            dmk_hits = sum(1 for n in deep_notes if n.get("danmaku_samples"))
            sub_hits = sum(1 for n in deep_notes if n.get("subtitle_excerpt"))
            login_health = await self._judge_login_health(diag, len(deep_notes))

            summary = {
                "at": now_ts,
                "collected": len(notes),
                "deep": len(deep_ids),
                "hollow": sum(1 for n in notes if n.get("hollow")),
                "danmaku_hits": dmk_hits,
                "subtitle_hits": sub_hits,
                "keywords": keywords,
                # 本轮"看哪几条"是谁定的：model=她自己挑的，其余是兜底。
                "pick_mode": pick_mode,
                "elapsed": round(elapsed, 1),
                "highlights": [
                    {"bvid": n["bvid"], "title": n["title"], "up": n["up"]}
                    for n in notes[:5]
                ],
            }
            self._state["last_browse_at"] = now_ts
            self._state["last_summary"] = summary
            self._state["last_diag"] = diag
            # 先取旧状态再覆盖，否则"状态翻转"永远判不出来，告警要么不响
            # 要么每轮都响。
            prev_health = str(((self._state.get("login_health") or {}) or {}).get("state") or "")
            self._state["login_health"] = login_health
            # 下一轮从这轮最值得看的那条往外延伸
            self._state["cursor"] = {"chain_from": deep_ids[0] if deep_ids else ""}
            self._save_state()
            self.last_error = ""

            # 登录失效只在状态翻转时告警一次，不然每小时刷一条 warning，
            # 日志就没法看了。
            if login_health["state"] == "expired" and prev_health != "expired":
                self._logger.warning(f"[BiliBrowse] ⚠ {login_health['hint']}")
            elif login_health["state"] == "nologin" and prev_health != "nologin":
                self._logger.info(f"[BiliBrowse] {login_health['hint']}")

            self._logger.info(
                f"[BiliBrowse] 刷完 收集={len(notes)} 深看={len(deep_ids)}({pick_mode}) "
                f"弹幕={dmk_hits}/{len(deep_notes)} 字幕={sub_hits}/{len(deep_notes)} "
                f"空壳={summary['hollow']} 关键词={keywords} 耗时={elapsed:.1f}s"
            )
            for note in notes:
                if not note.get("deep"):
                    continue
                self._logger.info(f"[BiliBrowse]   · {note['title'][:40]} ({note['up']})")

            if hasattr(self.plugin, "_emit_log"):
                try:
                    self.plugin._emit_log(
                        "INFO",
                        f"B站刷了 {len(notes)} 条，深看 {len(deep_ids)} 条",
                    )
                except Exception:
                    pass

            return {"ok": True, **summary}

    # ── 给对话用 ──

    def recent_notes(self, limit: int = 6, *, deep_only: bool = False) -> list[dict[str, Any]]:
        """取最近刷到的见闻，新的在前。

        只给 72 小时内的：更早的她自己都记不清了，硬塞进提示词会让她把
        "上周看的"当成"刚看的"来说。
        """
        self._load_state()
        now_ts = time.time()
        notes = self._state.get("notes") or []
        out = []
        for note in notes:
            if not isinstance(note, dict):
                continue
            if now_ts - float(note.get("watched_at") or 0) > _FRESH_HOURS * 3600:
                continue
            if deep_only and not note.get("deep"):
                continue
            out.append(note)
            if len(out) >= limit:
                break
        return out

    def candidate_notes(
        self,
        limit: int = 6,
        *,
        exclude: Optional[set[str]] = None,
    ) -> list[dict[str, Any]]:
        """给"她自己挑一条发"用的候选：一批内容各不相同的、真看过的见闻。

        和前几版的区别，是 2026-10-31 那次返工的核心：

        以前给她两条（`prompt_notes(limit=2)`），按时间倒序取最新的，她只能
        在这两条里选，甚至会聊到一个根本没深看过的标题上——因为她手上没有
        内容，只有标题。她"不知道自己发的是什么"，根子在这里。

        现在给一批（默认 6 条），每条都带弹幕、字幕摘要、简介、时长，她有的
        可挑、也知道每条是什么。挑选规则也从"按热度排"改成：
          - 只要深看过的（有弹幕或字幕，她真记得住内容）；
          - 同一个 UP 只留一条，不然 6 条里 4 条是同一个人的；
          - 排除最近已经发过的；
          - 最后随机打散——播放量高低不参与排序，发哪条由她看内容决定。
        """
        self._load_state()
        now_ts = time.time()
        exclude = {str(x) for x in (exclude or set()) if str(x).strip()}
        pool: list[dict[str, Any]] = []
        seen_up: set[str] = set()

        for note in self._state.get("notes") or []:
            if not isinstance(note, dict):
                continue
            if now_ts - float(note.get("watched_at") or 0) > _FRESH_HOURS * 3600:
                continue
            if not note.get("deep"):
                continue
            bvid = str(note.get("bvid") or "").strip()
            title = str(note.get("title") or "").strip()
            if not bvid or not title or bvid in exclude:
                continue
            # 没有弹幕也没有字幕的，她其实说不出内容，不进候选——正是这种
            # "只见过标题"的条目让她把话讲空。
            if not (note.get("danmaku_samples") or note.get("subtitle_excerpt")):
                continue
            up = str(note.get("up") or "").strip()
            if up:
                if up in seen_up:
                    continue
                seen_up.add(up)
            pool.append(note)

        random.shuffle(pool)
        return pool[:limit]

    def prompt_notes(self, limit: int = 5) -> list[dict[str, Any]]:
        """兼容旧调用：挑出要写进提示词的见闻条目。"""
        notes = self.candidate_notes(limit)
        if notes:
            return notes
        return [
            n for n in self.recent_notes(limit=limit, deep_only=True)
            if str(n.get("title") or "").strip()
        ]

    def build_prompt_digest(
        self,
        limit: int = 5,
        notes: Optional[list[dict[str, Any]]] = None,
    ) -> str:
        """把见闻压成一段"她自己回想"的话，用于聊天里调工具回想。

        主动搭话不走这里——那条路用 `build_full_digest`：给它六条的全部信息，
        让她自己决定提不提、提哪条。这里的用途是她聊天中被问"最近看什么"
        时，把最近的见闻连同链接一起回想起来。

        内容部分尽量给具体的：字幕摘要不再截开头 200 字（字幕开头全是
        "大家好今天我们来"，等于没给），而是用 `_gist` 均匀抽前中后三段。
        """
        notes = notes if notes is not None else self.prompt_notes(limit)
        if not notes:
            return ""

        lines = ["你最近在 B 站刷到过这些（是你自己刷的，不是别人告诉你的）："]
        for note in notes:
            title = str(note.get("title") or "").strip()
            up = str(note.get("up") or "").strip()
            bvid = str(note.get("bvid") or "").strip()
            if not title:
                continue
            head = f"- 《{title}》"
            if up:
                head += f"（UP：{up}）"
            duration = int(note.get("duration") or 0)
            if duration:
                head += f" 时长{duration // 60}分{duration % 60:02d}秒"
            line = head
            if bvid:
                line += f"\n  链接：https://www.bilibili.com/video/{bvid}"
            desc = " ".join(str(note.get("desc") or "").split())
            if desc:
                line += f"\n  简介里写着：{desc[:90]}"
            subtitle = str(note.get("subtitle_excerpt") or "").strip()
            if subtitle:
                line += f"\n  里面讲的是：{_gist(subtitle)}"
            danmaku = note.get("danmaku_samples") or []
            if danmaku:
                sample = "、".join(str(d) for d in danmaku[:6] if str(d).strip())
                if sample:
                    line += f"\n  弹幕里有：{sample[:160]}"
            tags = [str(t) for t in (note.get("tags") or [])[:5] if str(t).strip()]
            if tags:
                line += f"\n  标签：{'、'.join(tags)}"
            lines.append(line)

        lines.append(
            "这些是你自己的见闻。别人问你最近看什么、聊到相关话题时，"
            "可以自然地提起来，但别像在汇报清单——一次只说一两条，"
            "用你自己的话说，不要念标题。要发链接就把上面给的原样抄过去。"
        )
        return "\n".join(lines)

    def build_full_digest(
        self,
        notes: list[dict[str, Any]],
        *,
        now_ts: Optional[float] = None,
    ) -> str:
        """把这一批视频的**全部信息**摊开给她看，让她自己决定怎么开口。

        这是 2026-10-31 的第三次返工。前两次的弯都在这：

        - 第一版只给标题和两条，她"不知道自己发的是什么"，只能说空壳；
        - 第二版给了六条但要求她在正文末尾标 `[[发:N]]`，实测一次没标
          （宿主那层"像真人发消息"的约束把机械标记挤掉了），还把手写僵；
        - 第三版拆成两次调用（先单独问她"发哪条，不想发回 0"），她倒是
          老实选了，但那就变成"每次主动搭话都必须发一个视频"——任务感太重，
          不像人在聊天。

        现在一次给全，一条不删：标题、UP、时长、发布时间、播放、弹幕数、
        标签、简介、字幕摘要、弹幕样本。她手上有什么，心里就有数，随口
        提一句还是换个话题，都由她自己定。

        **不给链接**：她记不住网址，手边有现成的就会直接从记忆里翻一条旧的
        贴出去——"说的和发的不是同一条"就是这么来的。链接由调用方按她写的
        编号、或按内容匹配补上，她的手碰不到 URL。
        """
        notes = [n for n in (notes or []) if str(n.get("title") or "").strip()]
        if not notes:
            return ""
        now_ts = time.time() if now_ts is None else now_ts

        lines = ["你最近在 B 站刷到的、点开看过的几支视频，信息都在这儿："]
        for idx, note in enumerate(notes, 1):
            title = str(note.get("title") or "").strip()
            up = str(note.get("up") or "").strip()
            duration = int(note.get("duration") or 0)
            bits: list[str] = []
            if up:
                bits.append(f"UP：{up}")
            if duration:
                bits.append(f"时长{duration // 60}分{duration % 60:02d}秒")
            rel = _rel_time(note.get("pubdate"), now_ts)
            if rel:
                bits.append(rel)
            play = int(note.get("play") or 0)
            if play:
                bits.append(f"播放{_fmt_count(play)}")
            danmaku_count = int(note.get("danmaku_count") or 0)
            if danmaku_count:
                bits.append(f"弹幕{_fmt_count(danmaku_count)}条")

            lines.append(f"[{idx}] 《{title}》")
            if bits:
                lines.append("     " + " ｜ ".join(bits))
            tags = [str(t).strip() for t in (note.get("tags") or [])[:6] if str(t).strip()]
            if tags:
                lines.append(f"     标签：{'、'.join(tags)}")
            desc = " ".join(str(note.get("desc") or "").split())
            if desc:
                lines.append(f"     简介：{desc[:140]}")
            subtitle = str(note.get("subtitle_excerpt") or "").strip()
            if subtitle:
                lines.append(f"     字幕里在讲：{_gist(subtitle, 240)}")
            danmaku = [str(d).strip() for d in (note.get("danmaku_samples") or []) if str(d).strip()]
            if danmaku:
                lines.append(f"     弹幕里有人刷：{'、'.join(danmaku[:10])[:200]}")
        return "\n".join(lines)

    @staticmethod
    def match_note(
        text: str, notes: list[dict[str, Any]], *, threshold: float = 2.0,
    ) -> tuple[dict[str, Any], float]:
        """她没写编号时，按内容猜这句话在说哪一条。

        打分只用三处，都是"命中就加分"：

          - 字幕（最重）：她的话里有多少二元组落在这一条的字幕里。字幕里的
            细节是全网独一份的——"念条例""不给退款""把人赶出去"这些词只可能
            出现在那一条里，串不到别的视频上。
          - 标题二元组：命中 1 个 +1.0，最多算 4 个。
          - 标签词：整词出现在她话里 +1.5。
          - 弹幕样本：单条弹幕的二元组有七成以上落在她话里 +2.0。

        **字幕为什么必须参与**，2026-10-31 晚上实测吃了一记：
        她说「一AI对着订阅用户一本正经念条款，转头就说你违反规定不给退款，
        还把人赶出去」，这些词全在《😾捐款请走正规渠道😾》的字幕里，标题却
        跟一个都对不上；于是那一条只拿到 0 分，链接被配到了另一条标题里带
        "视频"两个字的片子上——"说的和发的不是同一条"当场复现。
        字幕不参与等于逼着代码只认标题，可她转述的恰恰是内容。

        字幕本身很长，任何一句中文都能在里面找到几个重合的二字片段，所以不是
        看命中数，而是看**她这句话有多少比例被字幕覆盖**，并且先剔掉通用词
        （见 `_STOP_GRAMS`）。

        分数不到 `threshold` 就返回空 dict——**宁可不带链接，也不能说一条发
        另一条**。真人在聊天里也不会每提一句就甩个链接。
        """
        body = str(text or "")
        if not body.strip() or not notes:
            return {}, 0.0
        body_grams = _bigrams(body)
        content_grams = body_grams - _STOP_GRAMS
        if not content_grams:
            return {}, 0.0

        best: dict[str, Any] = {}
        best_score = 0.0
        for note in notes:
            score = 0.0

            subtitle = str(note.get("subtitle_excerpt") or "") or str(note.get("desc") or "")
            if subtitle:
                hits = content_grams & _bigrams(subtitle)
                if len(hits) >= 3:
                    ratio = len(hits) / len(content_grams)
                    if ratio >= 0.15:
                        score += 3.0 + min(len(hits), 12) * 0.3

            title = str(note.get("title") or "")
            title_hit = len(_bigrams(title) & body_grams)
            if title_hit:
                score += min(title_hit, 4) * 1.0

            for tag in (note.get("tags") or [])[:8]:
                tag_text = "".join(str(tag).split())
                if len(tag_text) >= 2 and tag_text in body:
                    score += 1.5

            for dm in (note.get("danmaku_samples") or [])[:20]:
                dm_grams = _bigrams(dm)
                # 短弹幕（"笑死我了""哈哈哈"）谁都会说，命中也不算数，
                # 否则随便一句话都能跟某条视频"对上"。
                if len(dm_grams) < 5:
                    continue
                if len(dm_grams & body_grams) / len(dm_grams) >= 0.7:
                    score += 2.0
                    break

            if score > best_score:
                best, best_score = note, score
        if best_score < threshold:
            return {}, best_score
        return best, best_score

    @staticmethod
    def parse_pick(text: str, notes: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
        """从她写的话里取出她标的编号，返回 (去掉标记后的正文, 选中的那条)。

        认两种写法：`[[发:3]]` 和独占一行的 `[3]`。后者不要求"发"字，因为
        提示词里只请她"想发就单写一行 [3]"，多加一个字都是负担。

        只按字面解析，不做相似度猜测——猜是 `match_note` 的事，两条路分开
        走，才不会把"编号指对了但被猜偏"和"没写编号"混成一团。
        """
        raw = str(text or "")
        note: dict[str, Any] = {}
        picked_idx = 0
        for match in PICK_MARK_RE.finditer(raw):
            try:
                idx = int(match.group(1))
            except (TypeError, ValueError):
                continue
            if 1 <= idx <= len(notes):
                picked_idx = idx
                break
        if not picked_idx:
            # 退一步认 "[3]" 这种裸编号，但必须独占一行——正文里随手写的
            # 方括号数字（比如 [1] 引用）不该被当成选片。
            for line in raw.splitlines():
                m = _BARE_PICK_RE.match(line)
                if not m:
                    continue
                idx = int(m.group(1))
                if 1 <= idx <= len(notes):
                    picked_idx = idx
                    break
        if picked_idx:
            note = notes[picked_idx - 1]

        cleaned = PICK_MARK_RE.sub("", raw)
        cleaned = "\n".join(
            ln for ln in cleaned.splitlines() if not _BARE_PICK_RE.match(ln)
        )
        # 标记独占一行，删掉之后会留下空行
        cleaned = re.sub(r"\n{2,}", "\n", cleaned).strip()
        return cleaned, note

    # ── 对话工具：让她能"回想"刷过什么 ──

    def build_definition(self) -> Any:
        """构造 bilibili_recent 工具定义。

        为什么不在每轮提示词里硬塞见闻：会话历史是累积的，每轮塞一段视频
        列表会越滚越长，几百轮后她自己都分不清哪条是刚看的。做成工具，
        她想提的时候自己去取，取到的永远是最新的那几条。
        """
        from main_logic.tool_calling import ToolDefinition

        return ToolDefinition(
            name=BILIBILI_TOOL_NAME,
            description=(
                "回想你最近在 B 站刷到的视频。别人问你最近看什么、聊到视频/"
                "梗/看片相关话题、或者你想找个话头时用它。"
                "返回标题、UP 主、弹幕里的原话，以及能发给对方的链接。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "可选。想看跟什么相关的（比如「猫」「做饭」），不填就返回最近刷的。",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "最多几条（1-6，默认 3）",
                    },
                },
                "required": [],
            },
            metadata={"source": "qqbetter_auto_reply"},
        )

    def recall_recent(self, query: str = "", limit: int = 3) -> str:
        """按关键词过滤见闻，返回直接能喂给模型的文本。"""
        self._load_state()
        try:
            limit = max(1, min(int(limit or 3), 6))
        except (TypeError, ValueError):
            limit = 3

        notes = self.recent_notes(limit=40)
        if not notes:
            return "（你最近没刷到什么视频，别提这事。）"

        keyword = str(query or "").strip()
        if keyword:
            def hit(note: dict[str, Any]) -> bool:
                blob = " ".join([
                    str(note.get("title") or ""),
                    str(note.get("up") or ""),
                    " ".join(str(t) for t in (note.get("tags") or [])),
                    str(note.get("desc") or ""),
                ])
                return keyword in blob

            filtered = [n for n in notes if hit(n)]
            if not filtered:
                filtered = notes
        else:
            filtered = notes

        # 深看过的排前面：那些才有弹幕和字幕，能聊得具体
        filtered.sort(key=lambda n: (1 if n.get("deep") else 0), reverse=True)
        picked = filtered[:limit]

        lines = []
        for note in picked:
            title = str(note.get("title") or "").strip()
            up = str(note.get("up") or "").strip()
            bvid = str(note.get("bvid") or "").strip()
            line = f"- 《{title}》" + (f"，UP 是 {up}" if up else "")
            danmaku = [str(d).strip() for d in (note.get("danmaku_samples") or []) if str(d).strip()]
            if danmaku:
                line += f"。弹幕里有人说：{'、'.join(danmaku[:5])}"
            subtitle = str(note.get("subtitle_excerpt") or "").strip()
            if subtitle:
                line += f"。内容大概是：{_gist(subtitle, 180)}"
            if bvid:
                line += f"。链接：https://www.bilibili.com/video/{bvid}"
            lines.append(line)

        header = (
            f"你最近在 B 站刷到的 {len(picked)} 条（是你自己刷的）："
            if not keyword
            else f"你刷过、跟「{keyword}」有关的 {len(picked)} 条："
        )
        lines.append(
            "用你自己的话说，别念标题。要发链接就把链接原样写进去。"
        )
        return header + "\n" + "\n".join(lines)

    def describe_state(self) -> dict[str, Any]:
        self._load_state()
        notes = self._state.get("notes") or []
        deep = [n for n in notes if isinstance(n, dict) and n.get("deep")]
        last_at = float(self._state.get("last_browse_at") or 0)
        return {
            "enabled": self._enabled(),
            "running": self.running,
            "logged_in": self.client.has_credential,
            "login_name": self.client.login_name,
            "keywords": self._keywords(),
            "interval_minutes": self._cfg("bilibili_browse_interval_minutes", 60),
            "batch_size": self._cfg("bilibili_browse_batch_size", 20),
            "deep_count": self._cfg("bilibili_browse_deep_count", 5),
            "note_count": len(notes),
            "deep_count_total": len(deep),
            "last_browse_at": last_at,
            "last_browse_human": (
                datetime.fromtimestamp(last_at).strftime("%Y-%m-%d %H:%M") if last_at else ""
            ),
            "last_summary": self._state.get("last_summary") or {},
            # 诊断三件套：出问题时不用翻日志就知道缺在哪一环。
            #   last_diag    —— 各接口这轮成了几次、空了几次、挂了几次
            #   login_health —— ok / nologin / expired / unknown
            #   hollow       —— 深看了但什么都没拿到的条数
            "last_diag": self._state.get("last_diag") or {},
            "login_health": self._state.get("login_health")
            or {"state": "unknown", "hint": "还没刷过，刷一轮才有判断"},
            "last_skip_reason": self.last_skip_reason,
            "last_error": self.last_error,
            "state_path": str(self._state_path()),
            "recent": [
                {
                    "title": n.get("title"),
                    "up": n.get("up"),
                    "bvid": n.get("bvid"),
                    "deep": n.get("deep"),
                }
                for n in notes[:5] if isinstance(n, dict)
            ],
            # 下面这段是她真正"看到"的东西，直接看就知道效果好不好。
            # 主动搭话时她就是拿着这一份内容自己决定说什么的。
            # 注意这是**预览**：没有排除已经发过的（真正发送时会在
            # `candidate_notes(exclude=...)` 里按目标排除最近 7 天发过的）。
            "pick_candidates": [
                {
                    "bvid": n.get("bvid"),
                    "title": n.get("title"),
                    "up": n.get("up"),
                    "has_subtitle": bool(n.get("subtitle_excerpt")),
                    "has_danmaku": bool(n.get("danmaku_samples")),
                }
                for n in self.candidate_notes(limit=6)
            ],
            "prompt_digest_preview": self.build_full_digest(
                self.candidate_notes(limit=3)
            ),
        }

    # ── 循环 ──

    async def start(self) -> None:
        if not self._enabled():
            self._logger.info("[BiliBrowse] 开关关闭，不启动")
            return
        if self._task and not self._task.done():
            return
        self._load_state()
        # 启动时就把登录态装上：不然启动日志里写着"登录=无"，而其实凭据
        # 好好地存在文件里，只是还没被读进来。看着像掉登录，白排查。
        try:
            await self._sync_credential()
        except Exception:
            self._logger.warning("[BiliBrowse] 启动时装载登录态失败", exc_info=True)
        self._task = asyncio.create_task(self._loop())
        self._logger.info(
            f"[BiliBrowse] 循环已启动 间隔={self._cfg('bilibili_browse_interval_minutes', 60)}分 "
            f"每轮={self._cfg('bilibili_browse_batch_size', 20)}条 "
            f"深看={self._cfg('bilibili_browse_deep_count', 5)}条 "
            f"登录={'有' if self.client.has_credential else '无'}"
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
                self._logger.warning("[BiliBrowse] 停止时异常", exc_info=True)
        self._logger.info("[BiliBrowse] 循环已停止")

    @property
    def running(self) -> bool:
        # 不只看 _task 是否存在：宿主可能在 startup 返回后回收后台任务，
        # 那时 _task 还在但已经 done。用心跳兜一层——只要最近 10 分钟内
        # 循环还转着，就算在跑。
        if self._task and not self._task.done():
            return True
        return (time.time() - float(self._heartbeat_at or 0)) < 600

    async def _loop(self) -> None:
        # 这条日志是"任务真的活下来了"的唯一硬证据。宿主会在 startup 返回后
        # 清理一批后台任务，光靠 start() 里 create_task 成功返回是不够的——
        # 现象是日志写着"循环已启动"、状态里 running=False，查半天查不出为什么。
        self._logger.info("[BiliBrowse] 循环进入主循环")
        try:
            while True:
                self._heartbeat_at = time.time()
                interval_min = max(5, int(self._cfg("bilibili_browse_interval_minutes", 60) or 60))
                # 用「距上次刷多久」来排下一次，而不是死等满一小时再刷。
                # 这样插件重启、手动触发都不会打乱节奏。
                last_at = float(self._state.get("last_browse_at") or 0)
                wait_seconds = 0.0
                if last_at:
                    elapsed = time.time() - last_at
                    wait_seconds = max(0.0, interval_min * 60 - elapsed)
                else:
                    # 从没刷过：等 30 秒就开始第一轮，别让人干等一小时
                    wait_seconds = 30.0

                if wait_seconds > 0:
                    await asyncio.sleep(min(wait_seconds, 300.0))
                    if wait_seconds > 300.0:
                        continue

                now = datetime.now()
                quiet = str(self._cfg("bilibili_quiet_hours", "02:00-08:00") or "")
                if _in_quiet_hours(quiet, now):
                    self.last_skip_reason = f"静默时段({quiet})"
                    await asyncio.sleep(600)
                    continue

                try:
                    await self.browse_once()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.last_error = "单轮刷取异常"
                    self._logger.warning("[BiliBrowse] 单轮刷取异常", exc_info=True)
                    await asyncio.sleep(300)
        except asyncio.CancelledError:
            raise
