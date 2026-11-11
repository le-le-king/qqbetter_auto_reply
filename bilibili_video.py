"""看一个指定的 B 站视频——别人发来链接时用这条。

和 bilibili_browse_service 的区别：
    那边是"她自己随便刷"，走搜索/热门/相关推荐，凭兴趣挑；
    这边是"别人递过来一个链接，她得真的看一遍"。入口固定、没有挑选，
    但要求把一条视频看透：简介、标签、观众弹幕、字幕原文。

为什么不复用 open_url：
    open_url 对 B 站已经有专门分支，但只打到 ``web-interface/view``，
    拿的是标题/UP/播放量这类**元信息**。知道"有个视频叫这名"和
    "这视频讲了什么"差着十万八千里，要聊起来必须有弹幕和字幕。
    两个工具各有各的时间预算，混在一起会把 open_url 的 16 秒拖穿。

链接形态比想象的多，都得认：
    https://www.bilibili.com/video/BV1xx411c7mD?p=2
    https://m.bilibili.com/video/BV1xx411c7mD
    https://b23.tv/AbCdEfG          （短链，要跟一次 302）
    BV1xx411c7mD                    （裸号，模型经常这么写）
    av170001                        （老 av 号）
"""

from __future__ import annotations

import asyncio
import re
import ssl
from dataclasses import dataclass
from typing import Any

from main_logic.tool_calling import ToolDefinition

BILIBILI_WATCH_TOOL_NAME = "bilibili_watch"

#: 一次看视频的硬上限。正常 4-5 个请求、节流后 6-12 秒，留足余量。
WATCH_TIMEOUT_SECONDS = 30.0

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

#: BV 号固定 10 位，前后不能粘连字母数字（否则 "xxBV..." 会误判）。
_BV_RE = re.compile(r"(?<![0-9A-Za-z])BV[0-9A-Za-z]{10}(?![0-9A-Za-z])")
_AV_RE = re.compile(r"(?<![0-9A-Za-z])av(\d{3,})(?![0-9])", re.IGNORECASE)
_B23_RE = re.compile(r"https?://b23\.tv/[0-9A-Za-z]+", re.IGNORECASE)
_PAGE_RE = re.compile(r"[?&]p=(\d{1,3})")


@dataclass
class BiliTarget:
    """要把哪一条视频拿下来。三种标识互斥，具体用哪个看谁先命中。"""

    bvid: str = ""
    aid: int = 0
    short_url: str = ""
    page: int = 1
    raw: str = ""

    def __bool__(self) -> bool:
        return bool(self.bvid or self.aid or self.short_url)


def extract_target(raw: Any) -> BiliTarget | None:
    """从一段话里抠出 B 站视频标识。

    用户发链接时经常带一串自己的话（"你看这个 笑得我不行 https://..."），
    所以这里做的是"从文本里找"，不是"必须是纯链接"。
    """
    text = str(raw or "").strip()
    if not text:
        return None

    page = 1
    match = _PAGE_RE.search(text)
    if match:
        page = max(1, min(int(match.group(1)), 200))

    match = _BV_RE.search(text)
    if match:
        return BiliTarget(bvid=match.group(0), page=page, raw=text)

    match = _AV_RE.search(text)
    if match:
        return BiliTarget(aid=int(match.group(1)), page=page, raw=text)

    match = _B23_RE.search(text)
    if match:
        return BiliTarget(short_url=match.group(0), page=page, raw=text)

    return None


async def _resolve_short(url: str) -> str:
    """跟一次 302，把 b23.tv 换成真链接。失败返回空串。"""
    import aiohttp
    import certifi

    ctx = ssl.create_default_context(cafile=certifi.where())
    timeout = aiohttp.ClientTimeout(total=8.0)
    try:
        async with aiohttp.ClientSession(
            timeout=timeout, headers={"User-Agent": _UA},
        ) as session:
            async with session.get(url, ssl=ctx, allow_redirects=True) as resp:
                # 短链的最终地址在 resp.url 里，不依赖 Location 头
                # （aiohttp 跟完重定向后 Location 已经没意义了）。
                return str(resp.url or "")
    except Exception:
        return ""


def _fmt_count(value: Any) -> str:
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        return ""
    if number >= 100_000_000:
        return f"{number / 100_000_000:.1f}亿"
    if number >= 10_000:
        return f"{number / 10_000:.1f}万"
    return str(number)


def _fmt_duration(seconds: Any) -> str:
    try:
        total = int(seconds or 0)
    except (TypeError, ValueError):
        return ""
    if total <= 0:
        return ""
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _fmt_date(epoch: Any) -> str:
    try:
        value = float(epoch or 0)
    except (TypeError, ValueError):
        return ""
    if value <= 0:
        return ""
    from datetime import datetime

    return datetime.fromtimestamp(value).strftime("%Y-%m-%d")


class QQBilibiliVideoService:
    """把"看一条指定视频"收敛成一个工具。

    没有后台循环、没有状态文件——它是被动的，只在对方递链接来的时候干活。
    复用 browse_service 的 client，所以节流和登录态是同一份（不会出现
    "刷的时候有登录、看链接时没登录"这种分裂）。
    """

    def __init__(self, plugin: Any):
        self.plugin = plugin
        self._logger = plugin.logger

    @property
    def client(self) -> Any:
        """借 browse_service 的 client，共用节流与凭据。"""
        browse = getattr(self.plugin, "bilibili_browse_service", None)
        return getattr(browse, "client", None)

    # ── 工具定义 ────────────────────────────────────────────
    def build_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=BILIBILI_WATCH_TOOL_NAME,
            description=(
                "打开一个 B 站视频，真正看它讲了什么：标题、UP 主、简介、标签、"
                "观众弹幕、字幕原文。对方发来 B 站链接（bilibili.com/video/... 、"
                "b23.tv/... 或裸的 BV 号）时必须用这个，不要用 open_url——"
                "open_url 只拿得到标题和播放量，看不到内容，聊起来就会露馅。"
                "看完基于里面的真实内容说，别编造视频里没出现的细节。"
                "注意：如果对方是要「你刚才说的那个视频」的链接，先用 bilibili_recent "
                "查出来（它每条都带链接），把那一条原样发过去；绝对不要凭记忆写 BV 号，"
                "也不要拿历史里别的链接凑数——那样会发错视频。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": (
                            "B 站视频链接，或者 BV 号 / av 号。带 p=2 这种"
                            "分P参数也会认。"
                        ),
                    },
                },
                "required": ["url"],
            },
            metadata={"source": "qqbetter_auto_reply"},
        )

    # ── 执行 ────────────────────────────────────────────────
    async def execute_watch(self, arguments: Any) -> str:
        args = arguments if isinstance(arguments, dict) else {}
        raw = str(args.get("url") or args.get("link") or "").strip()
        if not raw:
            return "（没给链接）"
        try:
            result = await asyncio.wait_for(
                self.watch(raw), timeout=WATCH_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            self._logger.warning(f"[BiliWatch] 看视频超时: {raw[:80]}")
            return "这个视频我打不开（超时了），你直接说讲了啥吧。"
        if not result.get("ok"):
            reason = result.get("reason") or "打不开"
            self._logger.info(f"[BiliWatch] 失败: {raw[:80]} → {reason}")
            return f"这个链接我没打开（{reason}）。"
        return result["text"]

    async def watch(self, raw: str, *, with_subtitle: bool = True) -> dict[str, Any]:
        """把一条视频抓成一段能读的文本。不抛异常，失败在返回值里说原因。

        ``with_subtitle=False`` 是给 open_url 那条入口用的：它自己只有 16 秒
        总预算，而字幕要多两次请求（列表 + CDN 正文）。丢字幕但保住弹幕，
        总比整条超时强。
        """
        client = self.client
        if client is None:
            return {"ok": False, "reason": "B 站功能没启用"}

        target = extract_target(raw)
        if not target:
            return {"ok": False, "reason": "这不像 B 站视频链接"}

        # 短链要跟一次重定向才知道是哪条视频
        if target.short_url and not target.bvid:
            resolved = await _resolve_short(target.short_url)
            if resolved:
                again = extract_target(resolved)
                if again and again.bvid:
                    target.bvid = again.bvid
                    target.page = again.page or target.page
            if not target.bvid:
                return {"ok": False, "reason": "短链解析失败"}

        # av 号转 BV：client 内部走统一节流，不要在这里裸调库
        if not target.bvid and target.aid:
            bvid = await client.aid_to_bvid(target.aid)
            if not bvid:
                return {"ok": False, "reason": "av 号转 BV 失败"}
            target.bvid = bvid

        bvid = target.bvid
        await self._ensure_credential()

        detail = await client.video_detail(bvid, page_index=target.page - 1)
        if not detail:
            return {"ok": False, "reason": "视频不存在或接口没返回"}

        cid = int(detail.get("cid") or 0)
        tags = await client.video_tags(bvid) if cid else []
        danmaku: list[str] = []
        subtitle = ""
        if cid:
            # page_index 必须传：弹幕接口自己按页码取 cid，不传就永远是
            # 第 1 P 的弹幕（实测 4P 视频的 P3 会拿到和 P1 一模一样的弹幕）。
            danmaku = await client.danmaku_samples(
                bvid, cid, limit=20, page_index=target.page - 1,
            )
            if with_subtitle:
                subtitle = await client.subtitle_text(bvid, cid)

        text = self._render(detail, tags, danmaku, subtitle)
        self._logger.info(
            f"[BiliWatch] 看完 {bvid} "
            f"弹幕={len(danmaku)} 字幕={len(subtitle)}字 title={str(detail.get('title'))[:30]}"
        )
        return {
            "ok": True,
            "bvid": bvid,
            "title": detail.get("title") or "",
            "up": detail.get("up") or "",
            "danmaku_count": len(danmaku),
            "subtitle_chars": len(subtitle),
            "text": text,
        }

    async def _ensure_credential(self) -> None:
        """顺手把登录态装上。

        为什么要在这里做：刷取循环启动时才 sync 一次凭据，但"看链接"可能
        发生在循环没开、或者刚重载完的时候——那时 client 是空的，字幕会
        静默变成空字符串。字幕是这条链路最有价值的部分，不能丢。

        直接借 browse 的 `_sync_credential()`：它已经处理了"独立凭据文件
        优先、配置里手填的兜底"两层，这里再写一遍迟早会走岔。
        """
        client = self.client
        if client is None or getattr(client, "has_credential", False):
            return
        browse = getattr(self.plugin, "bilibili_browse_service", None)
        sync = getattr(browse, "_sync_credential", None)
        if not callable(sync):
            return
        try:
            await sync()
        except Exception:
            self._logger.info("[BiliWatch] 同步登录态失败（字幕可能拿不到）")

    # ── 文本渲染 ────────────────────────────────────────────
    def _render(
        self,
        detail: dict[str, Any],
        tags: list[str],
        danmaku: list[str],
        subtitle: str,
    ) -> str:
        title = str(detail.get("title") or "").strip() or "（无标题）"
        lines = [f"B 站视频：《{title}》"]

        head: list[str] = []
        if detail.get("up"):
            head.append(f"UP 主 {detail['up']}")
        play = _fmt_count(detail.get("play"))
        if play:
            head.append(f"播放 {play}")
        dmk = _fmt_count(detail.get("danmaku_count"))
        if dmk:
            head.append(f"弹幕 {dmk}")
        duration = _fmt_duration(detail.get("duration"))
        if duration:
            head.append(f"时长 {duration}")
        published = _fmt_date(detail.get("pubdate"))
        if published:
            head.append(f"发布 {published}")
        if head:
            lines.append(" ｜ ".join(head))

        page_title = str(detail.get("page_title") or "").strip()
        page_count = int(detail.get("page_count") or 0)
        page_index = int(detail.get("page_index") or 0)
        if page_count > 1:
            lines.append(
                f"这是第 {page_index + 1}/{page_count} P"
                + (f"（{page_title}）" if page_title else "")
            )

        if tags:
            lines.append("标签：" + " / ".join(str(t) for t in tags[:10]))

        desc = str(detail.get("desc") or "").strip()
        if desc:
            # 简介里的换行会把上下文结构撑乱，压成一行
            desc = re.sub(r"\s*\n+\s*", " ", desc)
            lines.append(f"简介：{desc[:300]}")

        if danmaku:
            lines.append("")
            lines.append("观众弹幕里在说这些（不是我的评价，是原样抓的）：")
            for item in danmaku[:16]:
                lines.append(f"- {item}")

        if subtitle:
            lines.append("")
            lines.append("字幕原文（节选）：")
            lines.append(subtitle[:1000])

        if not danmaku and not subtitle:
            lines.append("")
            lines.append(
                "（这条视频没抓到弹幕和字幕，只有上面的基本信息。"
                "聊的时候别装作看过里面的内容。）"
            )

        lines.append("")
        lines.append(f"链接：https://www.bilibili.com/video/{detail.get('bvid') or ''}/")
        return "\n".join(lines)


__all__ = [
    "BILIBILI_WATCH_TOOL_NAME",
    "BiliTarget",
    "QQBilibiliVideoService",
    "WATCH_TIMEOUT_SECONDS",
    "extract_target",
]
