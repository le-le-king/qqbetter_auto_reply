"""search_web 工具 —— 让 QQ 通道里的角色能检索国内视频和网页。

为什么要有这一层：
    QQ 回复链路走的是插件自己的 chat client（不是宿主的 agent），每轮只
    挂 recall_memory 一个工具（reply_generation_service 里 set_tools 是
    整表替换）。模型不知道自己能联网，被要视频时就只能顺着话头演
    "本喵刚刷到个视频，喏"——然后什么也发不出来。

怎么调宿主的联网插件：
    宿主对任何插件开放 POST /runs {plugin_id, entry_id, args}，跑完从
    GET /runs/{id}/export 取 json 结果。2026-10-25 实测调用
    web_search.search_summary 约 4-5 秒返回。走 HTTP 而不是
    ctx.trigger_plugin_event 的 adapter_call——后者没有文档，只有
    mcp_adapter 一处在用。

为什么必须按域名分档：
    AnySearch 的 zone=cn 只影响「地区偏好排序」，不是「可达性过滤」。
    实测搜「猫 偷喝牛奶 视频」加 zone=cn，前五条是 facebook / youtube /
    bilibili / douban / tiktok——境外站点占多数。不筛的话模型会很自然
    甩一个 youtube 链接过来，用户在国内点开是白屏。所以这里分三档排序，
    境内有结果时把境外站点直接丢掉，一条都不给模型。
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any
from urllib.parse import urlparse

from main_logic.tool_calling import ToolDefinition

SEARCH_TOOL_NAME = "search_web"

#: 单次搜索的 HTTP 预算（POST + 轮询 + export）。整条链路参考值：
#: 一次搜索约 4-5 秒，补搜再加 4-5 秒，所以外层给的预算要覆盖两次。
SEARCH_TOOL_HTTP_TIMEOUT_SECONDS = 16.0

_BUDGET_FIRST = 8.0
_BUDGET_EXTRA = 7.0
_POLL_INTERVAL = 0.4
_EXPORT_LIMIT = 20

#: 同 query 的缓存：模型在 forced-finalize / 流内重试里会再调一次，
#: 每次都真打一遍接口纯属浪费，而且 web_search 自己有引擎冷却。
_CACHE_TTL_SECONDS = 60.0
_CACHE_MAX_ENTRIES = 32

_MAX_RESULTS = 6
_MIN_RESULTS = 3

#: 补搜只取前两条：百度回退结果里尾部常是 SEO 垃圾，喂多了模型会照着标题编。
_EXTRA_RESULTS = 3
_EXTRA_KEEP = 2

# ── 域名分档 ──────────────────────────────────────────────
# 0 = 国内视频平台（用户点开能看，最优先）
# 1 = 其余国内站点
# 2 = 认不出来，保留但排后面
# 3 = 境外站点（境内打不开，有档 0/1 结果时直接丢弃）
_TIER_DOMESTIC_VIDEO = (
    "bilibili.com", "b23.tv",
    "douyin.com", "iesdouyin.com",
    "kuaishou.com", "gifshow.com",
    "ixigua.com",
    "v.qq.com", "qq.com",
    "youku.com",
    "iqiyi.com",
    "mgtv.com",
    "acfun.cn",
    "huya.com", "douyu.com",
    "xiaohongshu.com", "xhslink.com",
    "weibo.com", "weibo.cn",
    "zhihu.com",
    "pipix.com", "pearvideo.com",
    "bilibili.tv",
)

_TIER_DOMESTIC = (
    "baidu.com", "baijiahao.baidu.com",
    "sohu.com", "sina.com.cn", "sina.cn",
    "163.com", "126.net",
    "toutiao.com", "douban.com",
    "csdn.net", "jianshu.com",
    "36kr.com", "huxiu.com", "sspai.com",
    "ifeng.com", "thepaper.cn", "chinanews.com.cn",
    "people.com.cn", "xinhuanet.com", "cctv.com",
    "cnblogs.com", "gitee.com",
    "taobao.com", "tmall.com", "jd.com",
)

_TIER_OVERSEAS = (
    "youtube.com", "youtu.be", "ytimg.com",
    "facebook.com", "fb.com", "fb.watch",
    "tiktok.com",
    "instagram.com",
    "twitter.com", "x.com",
    "reddit.com",
    "vimeo.com",
    "dailymotion.com",
    "twitch.tv",
    "pinterest.com",
    "linkedin.com",
    "medium.com",
    "github.com",
    "wikipedia.org",
    "google.com", "googleusercontent.com",
    "netflix.com",
    "spotify.com",
    "discord.com", "telegram.org", "t.me",
    "quora.com", "stackoverflow.com",
)

#: 判断这条 query 是不是在要视频——国内结果为空时补搜 bilibili。
_VIDEO_INTENT_KEYWORDS = (
    "视频", "短片", "片段", "录像", "直播", "vlog", "mv",
    "b站", "bilibili", "抖音", "快手", "油管",
)


def _host_matches(host: str, domains: tuple[str, ...]) -> bool:
    if not host:
        return False
    host = host.lower()
    return any(host == d or host.endswith("." + d) for d in domains)


def _tier_of(url: str) -> int:
    host = urlparse(url).hostname or ""
    if _host_matches(host, _TIER_DOMESTIC_VIDEO):
        return 0
    if _host_matches(host, _TIER_DOMESTIC):
        return 1
    if _host_matches(host, _TIER_OVERSEAS):
        return 3
    return 2


def _looks_like_video_query(query: str) -> bool:
    lowered = str(query or "").lower()
    return any(k in lowered for k in _VIDEO_INTENT_KEYWORDS)


def _has_cjk(query: str) -> bool:
    """query 里有没有汉字。

    只用来决定「该不该补搜国内视频平台」：英文 query 在 bilibili / 抖音
    上基本没有对应内容，补搜纯属浪费一轮 HTTP。
    """
    return any("\u4e00" <= ch <= "\u9fff" for ch in str(query or ""))


def _resolve_host_origin() -> str:
    """定位宿主插件服务的 origin。

    端口每次启动都可能变（2026-10-25 是 48916，前一天不是），所以绝不能
    硬编码。这里照抄 music_pusher 的解析顺序：先看宿主注入的 env，再退回
    config 里的 USER_PLUGIN_SERVER_PORT。全都拿不到就返回空串，由调用方
    当作「联网不可用」降级。
    """
    for key in (
        "NEKO_PLUGIN_SERVER_ORIGIN",
        "NEKO_USER_PLUGIN_SERVER_ORIGIN",
        "NEKO_SERVER_ORIGIN",
    ):
        raw = str(os.getenv(key, "") or "").strip().rstrip("/")
        if raw.startswith("http://") or raw.startswith("https://"):
            return raw

    try:
        port = int(str(os.getenv("NEKO_USER_PLUGIN_SERVER_PORT", "")).strip())
        if 1 <= port <= 65535:
            return f"http://127.0.0.1:{port}"
    except Exception:
        pass

    try:
        from config import USER_PLUGIN_SERVER_PORT

        port = int(USER_PLUGIN_SERVER_PORT)
        if 1 <= port <= 65535:
            return f"http://127.0.0.1:{port}"
    except Exception:
        pass

    return ""


class QQSearchToolService:
    """把宿主 web_search 插件接成 QQ 会话里的 search_web 工具。"""

    def __init__(self, plugin: Any):
        self.plugin = plugin
        self._cache: dict[tuple[str, int], tuple[float, str]] = {}

    # ── 工具定义 ────────────────────────────────────────────
    def build_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=SEARCH_TOOL_NAME,
            description=(
                "联网搜索。想知道现在的事、找视频、找新闻、要链接时用它。"
                "结果里带真实 URL，可以直接把 URL 写进回复发给对方。"
                "已经优先返回国内站点的结果，境外站点在你这里也可能被过滤掉。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "搜索词。保留中文原话，不要翻译成英文。",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": f"最多几条（{_MIN_RESULTS}-{_MAX_RESULTS}，默认 5）",
                    },
                },
                "required": ["query"],
            },
            metadata={"source": "qqbetter_auto_reply"},
        )

    # ── 执行 ────────────────────────────────────────────────
    async def execute_search(self, arguments: Any) -> str:
        """跑一次搜索，返回直接喂给模型的文本。

        任何失败都收敛成一句「没搜到」——不抛异常，也不返回空串。模型拿
        到空串会自己发挥，这正是要治的毛病。
        """
        args = arguments if isinstance(arguments, dict) else {}
        query = args.get("query")
        query = query.strip() if isinstance(query, str) else ""
        if not query:
            return "（没有给搜索词，这次没搜）"

        try:
            limit = int(args.get("max_results") or 0)
        except (TypeError, ValueError):
            limit = 0
        limit = max(_MIN_RESULTS, min(limit or 5, _MAX_RESULTS))

        cached = self._cache_get(query, limit)
        if cached is not None:
            self.plugin.logger.info(
                "search_web 命中缓存: query_len=%s", len(query),
            )
            return cached

        started = time.monotonic()
        origin = _resolve_host_origin()
        if not origin:
            self.plugin.logger.warning("search_web 无法定位宿主服务地址，本轮不联网")
            return "（联网功能暂时不可用，这次没搜到东西，别编。）"

        results = await self._search_once(origin, query, limit, _BUDGET_FIRST)

        ranked = self._rank(results)
        wants_video = _looks_like_video_query(query)
        if (
            wants_video
            and _has_cjk(query)
            and not any(t == 0 for t, _ in ranked)
        ):
            # 要的是视频、但国内平台一条都没出来：补一次带 bilibili 的检索。
            # 检索每轮只放一次（闸在 handler 层，第二次调用复用上次结果），
            # 模型自己补不了，只能这里补。
            #
            # 只对中文查询补：实测英文 query（"cat sneezing compilation 视频"）
            # 补搜回来仍然是 youtube/tiktok/facebook 一整套，国内平台根本没有
            # 对应内容，白花五六秒。中文查询才有意义。
            #
            # 补搜只取前两条：百度回退结果尾部常混进 SEO 垃圾，标题和 query
            # 驴唇不对马嘴，全塞给模型只会让她照着标题胡说。
            extra = await self._search_once(
                origin, f"{query} bilibili", _EXTRA_RESULTS, _BUDGET_EXTRA,
            )
            if extra:
                seen = {str(r.get("url") or "") for r in results}
                results.extend(
                    r for r in extra[:_EXTRA_KEEP]
                    if str(r.get("url") or "") not in seen
                )
                ranked = self._rank(results)

        usable = self._pick(ranked, limit, allow_overseas=not wants_video)
        elapsed_ms = (time.monotonic() - started) * 1000.0

        if not usable:
            self.plugin.logger.info(
                "search_web 无可用结果: query_len=%s elapsed=%.0fms raw=%s",
                len(query), elapsed_ms, len(results),
            )
            return (
                "（这次没搜到能用的结果。不要编内容，也不要假装已经发出去了，"
                "就直说没找到。）"
            )

        text = self._format(query, usable)
        self._cache_put(query, limit, text)
        self.plugin.logger.info(
            "search_web 完成: query_len=%s kept=%s dropped_overseas=%s elapsed=%.0fms",
            len(query), len(usable), len(results) - len(usable), elapsed_ms,
        )
        self.plugin.logger.debug("search_web query=%r", query)
        return text

    # ── 调宿主 ──────────────────────────────────────────────
    async def _search_once(
        self, origin: str, query: str, limit: int, budget: float,
    ) -> list[dict[str, str]]:
        try:
            payload = await self._call_entry(
                origin,
                "web_search",
                "search_summary",
                {"query": query, "max_results": max(limit, _MIN_RESULTS)},
                budget,
            )
        except Exception as exc:
            self.plugin.logger.warning(f"search_web 调用失败: {type(exc).__name__}")
            return []

        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            return []
        raw = data.get("results")
        if not isinstance(raw, list):
            return []

        out: list[dict[str, str]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            if not url.startswith(("http://", "https://")):
                continue
            out.append({
                "title": str(item.get("title") or "").strip(),
                "url": url,
                "snippet": str(item.get("snippet") or "").strip(),
            })
        return out

    @staticmethod
    async def _call_entry(
        origin: str,
        plugin_id: str,
        entry_id: str,
        args: dict[str, Any],
        budget: float,
    ) -> Any:
        """POST /runs → 轮询 → export，取回 entry 的 json 结果。

        用 aiohttp 而不是 httpx：宿主是嵌入式 Python 发行包，里面**没有
        httpx**（只有 aiohttp + certifi）。早先按 httpx 写的那版在插件进程
        里会直接 ImportError，被 except 吞成"没搜到"，看起来像是搜索坏了。

        宿主插件服务在 127.0.0.1，走明文，ssl=False 省掉握手。

        另一个历史坑：用同一个连接先 POST /runs 再 GET /runs/{id} 时，
        GET 曾经稳定返回 404（run 记录归属跟连接绑定）。这里每次调用自建
        一个 session，POST 与轮询虽然共用连接池，但实测 aiohttp 的连接
        复用没有触发这个问题；如果哪天又 404，就把三个请求拆成独立
        session。
        """
        import aiohttp

        loop = asyncio.get_running_loop()
        deadline = loop.time() + budget
        timeout = aiohttp.ClientTimeout(total=max(1.0, budget))
        # aiohttp 的 per-request timeout 只认 ClientTimeout，传 float 会
        # 直接 ValueError("timeout parameter cannot be of float type")。
        poll_timeout = aiohttp.ClientTimeout(total=5.0)
        connector = aiohttp.TCPConnector(ssl=False, limit=4)

        async with aiohttp.ClientSession(
            timeout=timeout, connector=connector,
        ) as session:
            async with session.post(
                f"{origin}/runs",
                json={
                    "plugin_id": plugin_id,
                    "entry_id": entry_id,
                    "args": args,
                },
            ) as response:
                response.raise_for_status()
                run = await response.json(content_type=None)

            run_id = str((run or {}).get("run_id") or "").strip()
            if not run_id:
                return None

            status = str((run or {}).get("status") or "")
            while status not in {"succeeded", "failed", "canceled", "cancelled"}:
                if loop.time() >= deadline:
                    # 超时就放弃这一轮。留在宿主那边的 run 会自己跑完，
                    # 只是我们不等了——等下去会顶穿生成侧的 turn_timeout。
                    return None
                await asyncio.sleep(_POLL_INTERVAL)
                async with session.get(
                    f"{origin}/runs/{run_id}", timeout=poll_timeout,
                ) as response:
                    response.raise_for_status()
                    polled = await response.json(content_type=None)
                status = str((polled or {}).get("status") or "")

            if status != "succeeded":
                return None

            async with session.get(
                f"{origin}/runs/{run_id}/export",
                params={"limit": _EXPORT_LIMIT},
                timeout=poll_timeout,
            ) as response:
                response.raise_for_status()
                exported = await response.json(content_type=None)

        items = (exported or {}).get("items")
        if not isinstance(items, list):
            return None
        for item in items:
            if isinstance(item, dict) and isinstance(item.get("json"), dict):
                return item["json"]
        return None

    # ── 排序与裁剪 ──────────────────────────────────────────
    @staticmethod
    def _rank(results: list[dict[str, str]]) -> list[tuple[int, dict[str, str]]]:
        ranked = [(_tier_of(r["url"]), r) for r in results]
        ranked.sort(key=lambda pair: pair[0])
        return ranked

    @staticmethod
    def _pick(
        ranked: list[tuple[int, dict[str, str]]], limit: int, *,
        allow_overseas: bool,
    ) -> list[dict[str, str]]:
        """档 0/1/2 优先，境外站点（档 3）只在允许时才当兜底。

        ``allow_overseas`` 由调用方按 query 意图决定：视频类查询一律不给
        境外——视频内容基本都在 youtube / tiktok / facebook 上，给了也打
        不开，不如老实说没找到；非视频查询（技术文档、英文资料）境外链接
        是真有用，这时允许兜底，但排名永远在国内结果之后。
        """
        domestic = [r for tier, r in ranked if tier <= 2]
        if domestic:
            return domestic[:limit]
        if not allow_overseas:
            return []
        return [r for _, r in ranked][:limit]

    @staticmethod
    def _format(query: str, items: list[dict[str, str]]) -> str:
        lines = [
            f"以下是真实搜索结果（{len(items)} 条，已优先国内可访问站点）：",
        ]
        for index, item in enumerate(items, start=1):
            title = item.get("title") or item.get("url") or ""
            host = urlparse(item["url"]).hostname or ""
            if host.endswith("baidu.com") and "/link" in item["url"]:
                # 百度的结果页跳转包了一层，点开能到 bilibili 但域名看不出
                # 来。标出来，模型才不会以为目标站就是百度。
                host = "百度跳转"
            lines.append(f"{index}. {title}  [{host}]")
            lines.append(f"   {item['url']}")
            snippet = item.get("snippet") or ""
            if snippet:
                lines.append(f"   {snippet}")
        lines.append(
            "只能引用上面的链接和内容。要分享就把 URL 原样写进回复里；"
            "如果这些都不合适，就说没找到。"
        )
        return "\n".join(lines)

    # ── 缓存 ────────────────────────────────────────────────
    def _cache_get(self, query: str, limit: int) -> str | None:
        key = (query, limit)
        hit = self._cache.get(key)
        if not hit:
            return None
        stamped, text = hit
        if time.monotonic() - stamped > _CACHE_TTL_SECONDS:
            self._cache.pop(key, None)
            return None
        return text

    def _cache_put(self, query: str, limit: int, text: str) -> None:
        if len(self._cache) >= _CACHE_MAX_ENTRIES:
            oldest = min(self._cache, key=lambda k: self._cache[k][0])
            self._cache.pop(oldest, None)
        self._cache[(query, limit)] = (time.monotonic(), text)
