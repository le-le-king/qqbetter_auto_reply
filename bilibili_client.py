"""B 站访问层：把她"刷 B 站"要用到的接口全部收敛到一个类里。

为什么单独一层：
    bilibili_api 这个库的返回结构在不同大版本之间改过好几轮（get_danmaku
    变 get_danmakus、get_cid 多了 page_index、登录模块从 login_v2 拆出去），
    业务层不该跟着它抖动。这里把「拿什么字段」固定下来，上面只认 dict。

登录态为什么必须走全局凭据（2026-10-31 实测，别再想当然）：
    这个版本的 get_danmakus / get_subtitle **不接受 credential 关键字**，
    传了直接 TypeError。而 _call 会吞掉异常返回默认值，现象就是"弹幕字幕
    永远是空的、日志里只有一行 info"，查起来极费劲——我第一版就是这么
    踩的：以为功能坏了，其实是参数传错还被兜底 except 吃掉了。

    所以统一 set_global_credential 一次生效，所有调用都不传 credential。
    另外注意：未登录时字幕接口**不报错，只返回空 subtitles 列表**，
    只判异常永远发现不了。凭据过期同理，数据会静默变空——必须靠下面的
    diag 计数 + verify_login 主动探，不能靠"没报错"当成功。

限速为什么必须有：
    2026-10-31 实测未登录状态连续打接口正常，但 B 站对无 Cookie 的高频
    访问会返 412（-412 风控）。每小时 20 条视频、每条 2-4 个请求，峰值
    大概 60-80 请求，间隔 0.6~1.3 秒随机化足够安全，也顺便让"刷"这个
    动作看起来不像机器扫描。
"""

from __future__ import annotations

import asyncio
import random
import re
import time
from typing import Any, Optional

#: 搜索结果的标题里带 <em class="keyword"> 高亮标签，必须剥掉。
_TAG_RE = re.compile(r"<[^>]+>")

#: 字幕正文单次最多取多少字。整段字幕动辄上万字，全塞进提示词没有意义，
#: 而且会把上下文顶爆——她"看过"不等于要背下来。
_SUBTITLE_MAX_CHARS = 1200

#: 探登录态要用浏览器 UA，B 站对裸请求的 nav 会直接返 -352。
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _clean(text: Any) -> str:
    return _TAG_RE.sub("", str(text or "")).replace("\u200b", "").strip()


def _parse_duration(value: Any) -> int:
    """搜索接口给的是 "12:34" 这种字符串，详情接口给的是秒数。"""
    if isinstance(value, (int, float)):
        return int(value)
    raw = str(value or "").strip()
    if not raw:
        return 0
    if raw.isdigit():
        return int(raw)
    parts = raw.split(":")
    try:
        parts = [int(p) for p in parts]
    except ValueError:
        return 0
    total = 0
    for part in parts:
        total = total * 60 + part
    return total


class BilibiliClient:
    """一个实例给整个插件共用，内部串行化请求节流。"""

    #: 两次请求之间的随机间隔。太密会被 412，太疏一轮刷完要几分钟。
    MIN_GAP_SECONDS = 0.6
    MAX_GAP_SECONDS = 1.3

    #: 单次库调用的上限。库内部有自己的 timeout，但被 wbi 重试拖住时会很久。
    REQUEST_TIMEOUT = 25.0

    def __init__(self, logger: Any):
        self._logger = logger
        self._last_request_at = 0.0
        self._throttle_lock = asyncio.Lock()
        self._credential: Any = None
        self.has_credential = False
        self.login_name = ""
        #: 连续失败计数。用来把"埋在一堆 info 里的代码级错误"顶成 warning。
        self._consecutive_fail = 0
        #: 分项计数：这轮每个接口到底成了几次、空了几次、挂了几次。
        #: 没有它就只能看到"字幕是空的"，分不清是没登录、没字幕轨、还是
        #: 接口挂了——三种情况该做的事完全不同。
        self.diag: dict[str, int] = {}

    # ── 诊断 ────────────────────────────────────────────────
    def _bump(self, key: str, n: int = 1) -> None:
        self.diag[key] = self.diag.get(key, 0) + n

    def take_diag(self) -> dict[str, int]:
        """取走本轮计数并清零，交给调用方汇总。"""
        snap = dict(self.diag)
        self.diag = {}
        return snap

    # ── 凭据 ────────────────────────────────────────────────
    async def apply_credential(self, raw: Optional[dict[str, Any]]) -> bool:
        """把配置里存的 cookie 装进库的全局凭据。

        返回是否真的带上了登录态。cookie 缺 SESSDATA 时不算登录——
        B 站很多接口对"半个登录态"比完全未登录更容易触发风控。
        """
        data = raw if isinstance(raw, dict) else {}
        sessdata = str(data.get("sessdata") or "").strip()
        if not sessdata:
            self._credential = None
            self.has_credential = False
            self.login_name = ""
            return False
        try:
            from bilibili_api import Credential, request_settings

            cred = Credential(
                sessdata=sessdata,
                bili_jct=str(data.get("bili_jct") or ""),
                buvid3=str(data.get("buvid3") or ""),
                buvid4=str(data.get("buvid4") or ""),
                dedeuserid=str(data.get("dedeuserid") or ""),
                ac_time_value=str(data.get("ac_time_value") or ""),
            )
            request_settings.set_global_credential(cred)
            self._credential = cred
            self.has_credential = True
            self.login_name = str(data.get("uname") or "")
            self._logger.info(
                f"[BiliClient] 已装载登录态 user={self.login_name or '?'} "
                f"has_buvid3={bool(data.get('buvid3'))}"
            )
            return True
        except Exception:
            self._logger.warning("[BiliClient] 装载登录态失败", exc_info=True)
            self._credential = None
            self.has_credential = False
            return False

    async def verify_login(self) -> dict[str, Any]:
        """探一次 nav，确认 cookie 还活着。

        ⚠ 别用 `User(mid).get_user_info()` 来判断——那是**公开接口**，不带
        cookie 也能返回昵称，拿它当"登录有效"的判据永远返回有效。我第一版
        就是这么写的，等于没校验。必须打 `/x/web-interface/nav` 看 isLogin，
        那个接口才会真的校验 SESSDATA。
        """
        cred = self._credential
        sessdata = str(getattr(cred, "sessdata", "") or "")
        if not sessdata:
            return {"valid": False, "reason": "没有 SESSDATA"}
        parts = [f"SESSDATA={sessdata}"]
        for attr, name in (
            ("bili_jct", "bili_jct"),
            ("buvid3", "buvid3"),
            ("dedeuserid", "DedeUserID"),
        ):
            value = str(getattr(cred, attr, "") or "")
            if value:
                parts.append(f"{name}={value}")
        cookie = "; ".join(parts)

        async def _fetch():
            import ssl

            import aiohttp
            import certifi

            ctx = ssl.create_default_context(cafile=certifi.where())
            timeout = aiohttp.ClientTimeout(total=self.REQUEST_TIMEOUT)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(
                    "https://api.bilibili.com/x/web-interface/nav",
                    headers={"Cookie": cookie, "User-Agent": _UA},
                    ssl=ctx,
                ) as resp:
                    return await resp.json(content_type=None)

        payload = await self._call("登录态校验", _fetch, None, diag="nav")
        if not isinstance(payload, dict):
            return {"valid": False, "reason": "nav 请求没拿到响应"}
        code = int(payload.get("code") or 0)
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        if code == 0 and data.get("isLogin"):
            name = str(data.get("uname") or "")
            if name:
                self.login_name = name
            return {"valid": True, "mid": str(data.get("mid") or ""), "name": name}
        return {
            "valid": False,
            "reason": f"nav 显示未登录（code={code}）",
        }

    # ── 节流 ────────────────────────────────────────────────
    async def _throttle(self) -> None:
        async with self._throttle_lock:
            gap = random.uniform(self.MIN_GAP_SECONDS, self.MAX_GAP_SECONDS)
            wait = gap - (time.monotonic() - self._last_request_at)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request_at = time.monotonic()

    async def _call(self, what: str, coro_factory, default, diag: str = ""):
        """统一的下手处：节流 → 超时 → 吞异常。

        绝不往上抛：一轮刷 20 条，某一条挂了不该让整轮崩掉，也不该让
        后台循环退出。失败就返回 default，调用方当"这条没拿到"处理。

        diag 给个前缀就把成功/超时/失败记到 self.diag 里，轮末统一汇总。
        吞异常是必要的，但"吞掉就没人知道"是之前最坑的地方，计数补上。

        连续失败还会把日志级别抬到 warning：单条视频挂了是正常的，连着挂
        三个大概率是代码和库对不上（比如参数名改了），这种错埋在 info 里
        能藏一个月。
        """
        await self._throttle()
        try:
            result = await asyncio.wait_for(coro_factory(), timeout=self.REQUEST_TIMEOUT)
        except asyncio.TimeoutError:
            if diag:
                self._bump(f"{diag}.timeout")
            self._note_failure(what, "超时")
            return default
        except Exception as exc:
            if diag:
                self._bump(f"{diag}.fail")
            detail = f"{type(exc).__name__}: {str(exc)[:160]}"
            self._note_failure(what, detail)
            return default
        self._consecutive_fail = 0
        if diag:
            self._bump(f"{diag}.ok")
        return result

    def _note_failure(self, what: str, detail: str) -> None:
        """单条失败只记 info（一轮里挂两三条属正常）；连续失败才升 warning。"""
        self._consecutive_fail += 1
        if self._consecutive_fail >= 3:
            if self._consecutive_fail == 3:
                self._logger.warning(
                    f"[BiliClient] 连续 {self._consecutive_fail} 次接口失败，"
                    f"可能不是网络问题而是库版本不匹配：最近一次 {what} → {detail}"
                )
            return
        self._logger.info(f"[BiliClient] {what} 失败: {detail}")

    # ── 搜索 ────────────────────────────────────────────────
    async def search_videos(self, keyword: str, limit: int = 20) -> list[dict[str, Any]]:
        from bilibili_api import search

        async def _run():
            return await search.search_by_type(
                keyword,
                search_type=search.SearchObjectType.VIDEO,
                page=1,
            )

        payload = await self._call(f"搜索({keyword})", _run, None)
        rows = (payload or {}).get("result") or []
        out: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            bvid = str(row.get("bvid") or "").strip()
            title = _clean(row.get("title"))
            if not bvid or not title:
                continue
            out.append({
                "bvid": bvid,
                "title": title,
                "up": _clean(row.get("author")),
                "play": int(row.get("play") or 0),
                "danmaku_count": int(row.get("video_review") or 0),
                "duration": _parse_duration(row.get("duration")),
                "desc": _clean(row.get("description")),
                "pubdate": int(row.get("pubdate") or 0),
                "tags": [t for t in str(row.get("tag") or "").split(",") if t.strip()][:8],
                "source": f"搜索:{keyword}",
            })
            if len(out) >= limit:
                break
        return out

    # ── 热门 ────────────────────────────────────────────────
    async def hot_videos(self, limit: int = 20) -> list[dict[str, Any]]:
        from bilibili_api import hot

        async def _run():
            return await hot.get_hot_videos(pn=1, ps=max(limit, 10))

        payload = await self._call("热门", _run, None)
        rows = (payload or {}).get("list") or []
        out: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            bvid = str(row.get("bvid") or "").strip()
            title = _clean(row.get("title"))
            if not bvid or not title:
                continue
            owner = row.get("owner") or {}
            stat = row.get("stat") or {}
            out.append({
                "bvid": bvid,
                "title": title,
                "up": _clean(owner.get("name")),
                "play": int(stat.get("view") or 0),
                "danmaku_count": int(stat.get("danmaku") or 0),
                "duration": _parse_duration(row.get("duration")),
                "desc": _clean(row.get("desc")),
                "pubdate": int(row.get("pubdate") or 0),
                "tags": [],
                "source": "热门",
            })
            if len(out) >= limit:
                break
        return out

    # ── 详情 / 标签 / 相关 ──────────────────────────────────
    async def aid_to_bvid(self, aid: int) -> str:
        """av 号转 BV 号。

        老链接和一部分搜索结果给的是 av 号，库那边 bvid/aid 二选一构造对象。
        只有 av 能用的场景很少，但缺了这条路，别人发个 av 链接就完全打不开。
        """
        from bilibili_api import video

        async def _run():
            info = await video.Video(aid=int(aid)).get_info()
            return str((info or {}).get("bvid") or "")

        return await self._call(f"av→BV({aid})", _run, "", diag="aid2bvid") or ""

    async def video_detail(
        self, bvid: str, page_index: int = 0,
    ) -> Optional[dict[str, Any]]:
        from bilibili_api import video

        async def _run():
            return await video.Video(bvid=bvid).get_info()

        info = await self._call(f"详情({bvid})", _run, None)
        if not isinstance(info, dict):
            return None
        stat = info.get("stat") or {}
        owner = info.get("owner") or {}
        pages = info.get("pages") or []

        # 分P：链接带 ?p=2 时要拿第 2 P 的 cid，否则弹幕和字幕会串到第 1 P
        # 去——现象是"看着 P2 的标题，聊着 P1 的内容"，很难发现。
        count = len(pages) if isinstance(pages, list) else 0
        idx = max(0, min(int(page_index or 0), max(0, count - 1))) if count else 0
        page_title = ""
        cid = 0
        try:
            page = pages[idx] if count else {}
            cid = int((page or {}).get("cid") or 0)
            page_title = str((page or {}).get("part") or "")
        except Exception:
            cid = 0
        if not cid:
            try:
                cid = int(await video.Video(bvid=bvid).get_cid(page_index=idx))
            except Exception:
                cid = 0

        return {
            "bvid": bvid,
            "title": _clean(info.get("title")),
            "up": _clean(owner.get("name")),
            "desc": _clean(info.get("desc"))[:400],
            "duration": int(info.get("duration") or 0),
            "cid": cid,
            "play": int(stat.get("view") or 0),
            "like": int(stat.get("like") or 0),
            "coin": int(stat.get("coin") or 0),
            "danmaku_count": int(stat.get("danmaku") or 0),
            "reply": int(stat.get("reply") or 0),
            "pubdate": int(info.get("pubdate") or 0),
            "page_count": count,
            "page_index": idx,
            "page_title": page_title,
        }

    async def video_tags(self, bvid: str) -> list[str]:
        from bilibili_api import video

        async def _run():
            return await video.Video(bvid=bvid).get_tags()

        rows = await self._call(f"标签({bvid})", _run, [])
        out = []
        for row in rows or []:
            name = _clean((row or {}).get("tag_name"))
            if name:
                out.append(name)
        return out[:10]

    async def related_videos(self, bvid: str, limit: int = 10) -> list[dict[str, Any]]:
        """相关推荐。

        这是"刷"的关键：真人不是一个关键词搜到底，而是看完一条点旁边那条。
        有了它，下一轮的候选就能从"她刚看的视频"自然延伸出去。
        """
        from bilibili_api import video

        async def _run():
            return await video.Video(bvid=bvid).get_related()

        rows = await self._call(f"相关({bvid})", _run, [])
        out = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            rid = str(row.get("bvid") or "").strip()
            title = _clean(row.get("title"))
            if not rid or not title or title == "视频已失效":
                continue
            owner = row.get("owner") or {}
            stat = row.get("stat") or {}
            out.append({
                "bvid": rid,
                "title": title,
                "up": _clean(owner.get("name")),
                "play": int(stat.get("view") or 0),
                "danmaku_count": int(stat.get("danmaku") or 0),
                "duration": _parse_duration(row.get("duration")),
                "desc": _clean(row.get("desc")),
                "pubdate": int(row.get("pubdate") or 0),
                "tags": [],
                "source": f"相关:{bvid}",
            })
            if len(out) >= limit:
                break
        return out

    # ── 弹幕 ────────────────────────────────────────────────
    async def danmaku_samples(
        self, bvid: str, cid: int, limit: int = 30, page_index: int = 0,
    ) -> list[str]:
        """抽一批弹幕。

        不取全部（热门视频动辄上万条），也不只取最早那批——前 30 条常是
        "第一""来了"这类无信息量的话。这里均匀抽，并且滤掉纯符号、纯数字
        和太短的，留下的才是"观众真实的反应"。

        ⚠ ``cid`` 目前只用来判"这条有没有分P信息"，真正决定取哪一P的是
        ``page_index``：库的 ``get_danmakus`` 自己按页码取 cid，传进来的
        cid 会被忽略。2026-10-31 实测发现——看 4P 视频的 P3，标题切了、
        弹幕跟 P1 一模一样，就是因为这里写死了 ``page_index=0``。
        """
        if not cid:
            return []
        from bilibili_api import video

        idx = max(0, int(page_index or 0))

        async def _run():
            # 不传 credential：这两个方法在宿主这个版本里不接受该关键字，
            # 登录态走 request_settings.set_global_credential 那一份。
            return await video.Video(bvid=bvid).get_danmakus(page_index=idx)

        # default 用 None 不用 []：只有 None 才代表"这次调用挂了"，
        # 空列表代表"接口通了但这条视频没人发弹幕"。两者处理方式不同。
        rows = await self._call(f"弹幕({bvid})", _run, None, diag="danmaku")
        if rows is None:
            return []
        items = list(rows)
        if not items:
            # 空不一定是错：新视频、冷门视频本来就没人发弹幕。
            self._bump("danmaku.empty")
            return []

        picks: list[str] = []
        seen: set[str] = set()
        step = max(1, len(items) // (limit * 3))
        for idx in range(0, len(items), step):
            text = _clean(getattr(items[idx], "text", ""))
            if len(text) < 4 or len(text) > 40:
                continue
            if text in seen:
                continue
            if text.isdigit() or not any("\u4e00" <= ch <= "\u9fff" or ch.isalpha() for ch in text):
                continue
            seen.add(text)
            picks.append(text)
            if len(picks) >= limit:
                break
        return picks

    # ── 字幕 ────────────────────────────────────────────────
    async def subtitle_text(self, bvid: str, cid: int) -> str:
        """取字幕正文。

        字幕列表接口只给一个 subtitle_url，正文要再请求一次。这里优先中文
        轨，没有中文就退到第一条。返回拼好的纯文本，截断到 1200 字。
        """
        if not cid:
            return ""
        from bilibili_api import video

        async def _run():
            # 同上：走全局凭据，不传 credential 关键字。
            return await video.Video(bvid=bvid).get_subtitle(cid)

        payload = await self._call(f"字幕列表({bvid})", _run, None, diag="subtitle")
        if not isinstance(payload, dict):
            # 上面已经记了 subtitle.fail / subtitle.timeout，这里只标"没拿到结构"
            self._bump("subtitle.bad_payload")
            return ""
        tracks = payload.get("subtitles") or []
        if not tracks:
            # 关键区分：未登录和"这视频真没字幕"返回的东西一模一样（空列表
            # 且不抛异常）。之前不分青红皂白当"没字幕"，结果是登录态过期
            # 半个月都没人发现。这里分开记，轮末才好判。
            self._bump("subtitle.no_track_nologin" if not self.has_credential else "subtitle.no_track")
            return ""

        chosen = None
        for track in tracks:
            lan = str((track or {}).get("lan") or "")
            if lan.startswith("zh"):
                chosen = track
                break
        chosen = chosen or tracks[0]
        url = str((chosen or {}).get("subtitle_url") or "").strip()
        if not url:
            self._bump("subtitle.no_url")
            return ""
        if url.startswith("//"):
            url = "https:" + url
        elif url.startswith("http://"):
            url = "https://" + url[len("http://"):]

        async def _fetch():
            import ssl

            import aiohttp
            import certifi

            ctx = ssl.create_default_context(cafile=certifi.where())
            timeout = aiohttp.ClientTimeout(total=self.REQUEST_TIMEOUT)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, ssl=ctx) as resp:
                    resp.raise_for_status()
                    return await resp.json(content_type=None)

        body = await self._call(f"字幕正文({bvid})", _fetch, None, diag="subtitle_body")
        if not isinstance(body, dict):
            self._bump("subtitle_body.bad_payload")
            return ""
        lines = []
        for seg in body.get("body") or []:
            text = _clean((seg or {}).get("content"))
            if text:
                lines.append(text)
            if sum(len(x) for x in lines) >= _SUBTITLE_MAX_CHARS:
                break
        if not lines:
            self._bump("subtitle_body.empty")
            return ""
        self._bump("subtitle.got_text")
        return " ".join(lines)[:_SUBTITLE_MAX_CHARS]

    # ── 热点关键词（给"随便刷刷"用）─────────────────────────
    async def hot_keywords(self, limit: int = 10) -> list[str]:
        from bilibili_api import search

        async def _run():
            return await search.get_hot_search_keywords()

        payload = await self._call("热搜词", _run, None)
        out = []
        rows = payload if isinstance(payload, list) else ((payload or {}).get("trending") or {}).get("list") or []
        for row in rows:
            name = _clean((row or {}).get("keyword") or (row or {}).get("show_name"))
            if name:
                out.append(name)
        return out[:limit]
