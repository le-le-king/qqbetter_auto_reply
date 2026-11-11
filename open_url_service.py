"""open_url 工具 —— 让 QQ 通道里的角色真的能打开一个链接看内容。

为什么需要它：
    search_web 只给标题和一小段摘要。模型拿着标题能"说"这个视频是什么，
    但没读过页面——遇到标题含糊、或者对方直接甩一个链接过来时，它只能靠
    猜。猜出来的内容一旦和实际不符，对方点开一看就穿帮。

    还有一个更现实的原因：``max_tool_iterations=1`` 意味着同一轮里工具只
    能调一次（客户端用完就摘掉 tools）。所以"先搜、再打开第一条确认"这
    种两步走做不到——打开链接必须是独立工具，模型自己决定用哪个。

bilibili 走接口而不是抓页面：
    视频页的 HTML 有 160KB，前 1200 字正文全是导航栏和推荐位，真正有用
    的信息（标题、UP 主、播放量、简介）反而埋在 meta 里。走
    ``api.bilibili.com/x/web-interface/view`` 拿到的 JSON 干净、体积小、
    几百毫秒就回来，而且不需要登录。

边界（很重要）：
    这是唯一一个能把外部内容拉进模型上下文的入口，所以它同时是个 SSRF
    风险点：宿主插件服务就跑在 127.0.0.1:48916，而 /runs 能调任何插件的
    任何入口。如果允许它抓本机地址，模型被一段网页文案诱导就能绕过工具
    边界去碰宿主内部接口。因此这里**硬拒绝**环回/内网/链路本地地址，
    并且只放行 http(s) 的常规端口。

境外站点：
    只对"确定打不开"的名单（youtube / tiktok / facebook / x …）快速失败，
    省掉十几秒等待。github、stackoverflow 这类国内能开的站点照常去抓，
    不搞一刀切。
"""

from __future__ import annotations

import asyncio
import html as html_module
import ipaddress
import re
import time
from typing import Any
from urllib.parse import urlparse

from main_logic.tool_calling import ToolDefinition

OPEN_TOOL_NAME = "open_url"

#: 单次打开的时间预算。这是**整条 open_url 路径**的上限，不是单次 HTTP
#: 的上限——bilibili 那条路最坏是「接口 5s 超时 + 退回抓页面 10s」= 15s，
#: 所以给 16s。生成侧的 turn_timeout 就是按这个数加的。
OPEN_TOOL_HTTP_TIMEOUT_SECONDS = 16.0

#: 正文节选上限。模型一轮要吃下这些内容再写回复，给太多会把必须记住的
#: 对话上下文挤掉。
_MAX_BODY_CHARS = 1000

#: 简介看着已经够用时的正文上限（bilibili 这类站的 meta 里信息很全，
#: 正文基本是推荐位，给多了纯噪音）。
_MAX_BODY_CHARS_WHEN_DESC_RICH = 400

#: 下载上限。超过就不读了——正常图文页 200KB 以内，2MB 已足够宽松，
#: 再大基本是视频流或者前端渲染的巨型 bundle，读了也没正文。
_MAX_DOWNLOAD_BYTES = 2_000_000

#: 同一轮里最多真的去抓几次。模型偶尔会一口气开三四个链接，每个都是
#: 几秒的 HTTP，串起来会顶穿生成侧超时。
_MAX_FETCHES_PER_TURN = 2

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

#: 国内基本打不开、去抓只会白等超时的站点。命中就直接告诉他打不开。
_OVERSEAS_DEAD = (
    "youtube.com", "youtu.be", "ytimg.com",
    "facebook.com", "fb.com", "fb.watch",
    "tiktok.com",
    "instagram.com",
    "twitter.com", "x.com",
    "netflix.com",
    "spotify.com",
    "discord.com", "telegram.org", "t.me",
    "google.com", "googleusercontent.com",
    "pinterest.com",
)

_PORTS_OK = {None, 80, 443, 8080, 8443}

_BILI_BV_RE = re.compile(r"(?:/video/|b23\.tv/)(BV[0-9A-Za-z]{10})", re.I)

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)
_META_TAG_RE = re.compile(r"<meta\b[^>]*>", re.I)
_ATTR_RE = re.compile(
    r"""([\w:.-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))""",
)
_DROP_BLOCKS_RE = re.compile(
    r"<(script|style|noscript|svg|iframe|template)\b[^>]*>.*?</\1\s*>",
    re.S | re.I,
)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_MULTI_NL_RE = re.compile(r"\n{3,}")

#: 正文容器的起始标记。命中后只从那一段往下剥标签，避免把页头导航和
#: 侧边栏当成正文——实测直接剥全文时，新华网首页给出来的"正文"是一长串
#: 省级行政区名单，模型拿着它什么也答不出来。
#: 这些 id/class 是新华网、人民网、新浪、公众号、澎湃等国内站的常见写法。
_ARTICLE_START_RE = re.compile(
    r"<(?:article|main)\b[^>]*>"
    r"|<(?:div|section|td)\b[^>]*(?:id|class)\s*=\s*[\"'][^\"']*"
    r"(?:artibody|article[-_]?content|post[-_]?body|post[-_]?content|"
    r"rich_media_content|main[-_]?content|content[-_]?area|"
    r"detail[-_]?content|TRS_Editor|rm_txt_con|news[-_]?content|"
    r"text[-_]?content|pages_content|b_artical|artical|article)"
    r"[^\"']*[\"'][^>]*>",
    re.I,
)

#: 从正文容器起点往后最多看这么多 HTML 再剥标签。正文开头一般就在容器
#: 前几 KB 里，取长了没意义（反正最终只留 1000 字）。
_ARTICLE_WINDOW = 8000

#: 正文容器剥离后短于这个长度就认为没命中，退回全文剥离。
_ARTICLE_MIN_CHARS = 120

#: 判定"这一段是正文而不是导航项"的长度阈值。导航、按钮、分享菜单都是
#: 三五个字的短行，正文段落很少短于这个数。
_PROSE_LINE_MIN_CHARS = 40


# ── 地址校验 ────────────────────────────────────────────────
def _host_blocked(host: str) -> bool:
    """环回 / 内网 / 链路本地一律拒绝（SSRF 防线）。"""
    lowered = host.lower().strip("[]")
    if not lowered:
        return True
    if lowered in {"localhost", "localhost.localdomain"}:
        return True
    if lowered.endswith(".local") or lowered.endswith(".internal"):
        return True
    try:
        ip = ipaddress.ip_address(lowered)
    except ValueError:
        return False
    return bool(
        ip.is_private or ip.is_loopback or ip.is_link_local
        or ip.is_reserved or ip.is_multicast or ip.is_unspecified
    )


def _reject_reason(url: str) -> str | None:
    """返回拒绝原因；能抓则返回 None。"""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return "只支持 http/https 链接"
    host = parsed.hostname or ""
    if not host:
        return "链接里没有域名"
    if _host_blocked(host):
        return "本机或内网地址，不能打开"
    try:
        port = parsed.port
    except ValueError:
        return "端口写得不对"
    if port is not None and port not in _PORTS_OK:
        return f"端口 {port} 不在允许范围"
    for dead in _OVERSEAS_DEAD:
        if host == dead or host.endswith("." + dead):
            return "这个站在国内打不开（境外站点）"
    return None


def _bili_bvid(url: str) -> str | None:
    match = _BILI_BV_RE.search(url)
    return match.group(1) if match else None


def _ssl_context() -> Any:
    """给 aiohttp 用的 TLS 上下文。

    宿主是嵌入式 Python 发行包，没有系统证书库那套东西，不显式指定
    cafile 的话 HTTPS 握手会报 CERTIFICATE_VERIFY_FAILED。resources/bin
    里带 certifi，优先用它；拿不到就退回默认上下文（至少不会因为缺
    certifi 整个工具起不来）。

    这一层是没法省的：宿主环境里**没有 httpx**，只有 aiohttp——早先按
    httpx 写的版本在插件进程里会直接 ImportError。
    """
    import ssl

    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


# ── HTML 解析（不引第三方库，插件环境不保证有 bs4） ─────────
def _parse_attrs(tag: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for match in _ATTR_RE.finditer(tag):
        key = match.group(1).lower()
        value = match.group(2) or match.group(3) or match.group(4) or ""
        out[key] = value
    return out


def _collect_meta(html: str) -> dict[str, str]:
    """把 meta 里的描述信息抽成扁平字典。"""
    found: dict[str, str] = {}
    wanted = {
        "description", "og:description", "og:title", "og:site_name",
        "og:type", "keywords", "author",
    }
    for tag in _META_TAG_RE.finditer(html):
        attrs = _parse_attrs(tag.group(0))
        key = (attrs.get("property") or attrs.get("name") or "").lower()
        content = (attrs.get("content") or "").strip()
        if key not in wanted or not content:
            continue
        # og:* 一般比普通 description 更准，允许它覆盖
        if key.startswith("og:") or key not in found:
            found[key] = content
    return found


def _visible_text(html: str) -> str:
    text = _COMMENT_RE.sub(" ", html)
    text = _DROP_BLOCKS_RE.sub(" ", text)
    text = _TAG_RE.sub("\n", text)
    text = html_module.unescape(text)
    text = _WS_RE.sub(" ", text)

    kept: list[str] = []
    for raw_line in text.split("\n"):
        line = raw_line.strip()
        # 单字符行基本是图标、分页点、装饰分隔符，正文里没有意义
        if len(line) < 2:
            continue
        kept.append(line)
    return _MULTI_NL_RE.sub("\n\n", "\n".join(kept))


def _clean_title(raw: str) -> str:
    title = _WS_RE.sub(" ", (raw or "").replace("\n", " ")).strip()
    return title[:120]


def _trim_nav_prefix(text: str) -> str:
    """跳过开头的导航堆，从第一段长文本开始。

    正文容器内部通常还压着一层站内导航（"新闻中心 / 国际新闻 / 正文 /
    新浪新闻客户端 / 微博 / 微信 / 分享 …"），实测能占掉三四百字，把真正
    的正文挤到截断线以外。这些项都是短行，所以按行找第一段够长的文字。

    找不到长行就原样返回——列表页、目录页本来就没有长段落，不能一刀切
    成空。
    """
    lines = [line for line in text.split("\n") if line.strip()]
    for index, line in enumerate(lines):
        if len(line.strip()) >= _PROSE_LINE_MIN_CHARS:
            # 留一行上下文，方便模型知道这是哪篇文章的正文
            return "\n".join(lines[max(0, index - 1):])
    return text


def _extract_body(html: str, limit: int) -> str:
    """优先从正文容器里抽，抽不出来再退回全文剥离。

    顺序很关键：**先删 script/style 再找容器**。反过来的话，从容器起点
    截 8000 字窗口时经常正好切在一段内联 JS 中间，``</script>`` 落在窗口
    外，剥离脚本的正则就配不上，整段 JS 会被当成正文交给模型（新浪的
    文章页就是这么翻车的，正文位置全是一堆 addEventListener）。

    只用容器"起点"不用完整闭合标签：div 是嵌套的，正则找 ``</div>``
    必然提前收尾。最终只要开头一千字，从容器起点往后截一段窗口就够。
    """
    cleaned = _DROP_BLOCKS_RE.sub(" ", _COMMENT_RE.sub(" ", html))
    match = _ARTICLE_START_RE.search(cleaned)
    if match:
        window = cleaned[match.start(): match.start() + _ARTICLE_WINDOW]
        candidate = _visible_text(window)
        if len(candidate) >= _ARTICLE_MIN_CHARS:
            return _truncate(_trim_nav_prefix(candidate), limit)
    return _truncate(_trim_nav_prefix(_visible_text(cleaned)), limit)


def _truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def _fmt_duration(seconds: Any) -> str:
    try:
        total = int(seconds)
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
        stamp = int(epoch)
    except (TypeError, ValueError):
        return ""
    if stamp <= 0:
        return ""
    try:
        return time.strftime("%Y-%m-%d", time.localtime(stamp))
    except (OSError, ValueError):
        return ""


class QQOpenUrlToolService:
    """把「打开一个链接读内容」接成 QQ 会话里的 open_url 工具。"""

    def __init__(self, plugin: Any):
        self.plugin = plugin
        self._cache: dict[str, tuple[float, str]] = {}
        #: 本轮已抓次数。由 handler 在每轮开始时置零——同一轮抓太多会把
        #: 生成侧的超时预算吃干净。
        self._fetches_this_turn = 0

    def reset_turn_budget(self) -> None:
        self._fetches_this_turn = 0

    # ── 工具定义 ────────────────────────────────────────────
    def build_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=OPEN_TOOL_NAME,
            description=(
                "打开一个网页链接，读取里面的真实内容（标题、作者、简介、正文）。"
                "两种情况必须用：①对方发来链接，你想知道里面到底是什么；"
                "②你要分享某个链接，先打开确认内容对得上，别只看标题猜。"
                "打不开就直说打不开，不要编造页面内容。"
                "注意：B 站视频链接（bilibili.com/video、b23.tv、BV 号）用 "
                "bilibili_watch，那个能看到弹幕和字幕；这个工具对视频只能拿到标题。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "完整链接，带 http:// 或 https://",
                    },
                },
                "required": ["url"],
            },
            metadata={"source": "qqbetter_auto_reply"},
        )

    # ── 执行 ────────────────────────────────────────────────
    async def execute_open(self, arguments: Any) -> str:
        args = arguments if isinstance(arguments, dict) else {}
        url = args.get("url")
        url = url.strip() if isinstance(url, str) else ""
        if not url:
            return "（没有给链接，这次没打开）"

        # 只有"完全没写协议"才补 https。这里必须区分两种输入：
        #   "bilibili.com/video/BVxxx" → scheme 为空，是模型爱写的半截链接，补上
        #   "file:///C:/Windows/win.ini" → scheme 是 file，属于明文攻击尝试
        # 早先版本只看前缀不以 http 开头就补 https，会把 file:// 改写成
        # "https://file:///..."，于是协议校验被绕过，最后报成 DNS 解析失败。
        scheme = urlparse(url).scheme.lower()
        if not scheme:
            url = "https://" + url.lstrip("/")
        elif scheme not in ("http", "https"):
            self.plugin.logger.info("open_url 拒绝: reason=非 http 协议 scheme=%s", scheme)
            return "（这个链接打不开：只支持 http/https。别假装看过里面的内容。）"

        reason = _reject_reason(url)
        if reason:
            self.plugin.logger.info(
                "open_url 拒绝: reason=%s host=%s",
                reason, urlparse(url).hostname or "",
            )
            return f"（这个链接打不开：{reason}。别假装看过里面的内容。）"

        cached = self._cache_get(url)
        if cached is not None:
            self.plugin.logger.info("open_url 命中缓存")
            return cached

        if self._fetches_this_turn >= _MAX_FETCHES_PER_TURN:
            self.plugin.logger.info("open_url 本轮已达上限，跳过")
            return "（这一轮已经开过几个链接了，先用拿到的内容回答。）"

        self._fetches_this_turn += 1
        started = time.monotonic()
        try:
            # aiohttp 的 ClientTimeout 是分阶段的（sock_connect / sock_read
            # 各自计），碰上持续慢速传输能拖很久。这里再套一层总时限，确保
            # 这条路径的耗时永远不超过预算——生成侧就是按这个数算
            # turn_timeout 的。
            text = await asyncio.wait_for(
                self._open(url), timeout=OPEN_TOOL_HTTP_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            self.plugin.logger.warning(
                "open_url 超时: host=%s", urlparse(url).hostname or "",
            )
            return "（打开超时了，可能是网站太慢或者要登录。别编内容。）"
        except Exception as exc:
            # aiohttp 正常会抛 asyncio.TimeoutError（上面的分支已覆盖），
            # 但 ServerTimeoutError / ConnectionTimeoutError 这类子类名里
            # 带 Timeout 的并不都归到那条分支，按类名再兜一次，免得把
            # "网站太慢"误报成"被拦了"。
            if "Timeout" in type(exc).__name__:
                self.plugin.logger.warning(
                    "open_url 超时: host=%s", urlparse(url).hostname or "",
                )
                return "（打开超时了，可能是网站太慢或者要登录。别编内容。）"
            self.plugin.logger.warning(
                f"open_url 失败: {type(exc).__name__}: {exc}"
            )
            return "（这个链接没打开，可能被网站拦了。别编内容。）"

        self._cache_put(url, text)
        self.plugin.logger.info(
            "open_url 完成: host=%s elapsed=%.0fms len=%s",
            urlparse(url).hostname or "",
            (time.monotonic() - started) * 1000.0, len(text),
        )
        return text

    async def _open(self, url: str) -> str:
        shallow = await self._via_video_service(url)
        if shallow:
            return shallow

        bvid = _bili_bvid(url)
        if bvid:
            rendered = await self._render_bilibili(bvid)
            if rendered:
                return rendered
            # 接口挂了就退回抓页面，别整条路都断掉

        rendered, final_url = await self._render_html(url)

        # b23.tv 短链：302 之后才知道真实地址，这时再走一次接口拿干净数据
        resolved = _bili_bvid(final_url)
        if resolved and resolved != bvid:
            via_api = await self._render_bilibili(resolved)
            if via_api:
                return via_api
        return rendered

    async def _via_video_service(self, url: str) -> str | None:
        """B 站视频交给 ``bilibili_video`` 那条路，能拿到弹幕（和字幕）。

        为什么不让这段留在 open_url 里自己打接口：这里原来只打
        ``web-interface/view``，拿到的是标题/UP/播放量——"知道有个视频"
        而不是"知道视频讲了什么"，对方发链接过来想聊的恰恰是后者。
        两条入口共用一份实现，行为才不会分叉。

        这里传 ``with_subtitle=False``：open_url 总预算只有 16 秒，字幕要
        多两次请求，容易顶穿。宁可少字幕也不能让整条链接打不开。
        """
        service = getattr(self.plugin, "bilibili_video_service", None)
        if service is None:
            return None
        # 先做个便宜的判断，普通网页别进 B 站那条路（虽然里面也会退出，
        # 但没必要每开一个链接都过一遍它的解析）。
        if not re.search(r"bilibili\.com|b23\.tv|BV[0-9A-Za-z]{10}|av\d{3,}", url, re.I):
            return None
        try:
            result = await service.watch(url, with_subtitle=False)
        except Exception as exc:
            self.plugin.logger.info(
                f"open_url 走 bilibili_video 失败，退回原来的路: {type(exc).__name__}"
            )
            return None
        if not result or not result.get("ok"):
            return None
        return str(result.get("text") or "") or None

    async def _render_bilibili(self, bvid: str) -> str | None:
        """走 bilibili 开放接口拿视频元信息。失败返回 None 由调用方降级。"""
        import aiohttp

        timeout = aiohttp.ClientTimeout(
            total=min(5.0, OPEN_TOOL_HTTP_TIMEOUT_SECONDS),
        )
        connector = aiohttp.TCPConnector(ssl=_ssl_context(), limit=2)
        try:
            async with aiohttp.ClientSession(
                timeout=timeout,
                connector=connector,
                headers={
                    "User-Agent": _UA,
                    "Referer": "https://www.bilibili.com",
                },
            ) as session:
                async with session.get(
                    "https://api.bilibili.com/x/web-interface/view",
                    params={"bvid": bvid},
                ) as response:
                    if response.status != 200:
                        return None
                    # content_type=None：接口偶尔回 text/plain，别让 aiohttp
                    # 因为 MIME 不对拒绝解析
                    payload = await response.json(content_type=None)
        except Exception as exc:
            self.plugin.logger.warning(
                f"open_url bilibili 接口失败，退回抓页面: {type(exc).__name__}"
            )
            return None

        if not isinstance(payload, dict) or payload.get("code") != 0:
            return None
        data = payload.get("data")
        if not isinstance(data, dict):
            return None

        title = _clean_title(str(data.get("title") or ""))
        owner = data.get("owner") if isinstance(data.get("owner"), dict) else {}
        stat = data.get("stat") if isinstance(data.get("stat"), dict) else {}

        lines = [f"已打开：{title or '（无标题）'}", "来源：bilibili 视频"]
        if owner.get("name"):
            lines.append(f"UP 主：{owner['name']}")
        numbers = []
        if stat.get("view") is not None:
            numbers.append(f"播放 {stat['view']}")
        if stat.get("danmaku") is not None:
            numbers.append(f"弹幕 {stat['danmaku']}")
        if stat.get("like") is not None:
            numbers.append(f"点赞 {stat['like']}")
        if stat.get("favorite") is not None:
            numbers.append(f"收藏 {stat['favorite']}")
        if numbers:
            lines.append("数据：" + " ｜ ".join(str(n) for n in numbers))
        duration = _fmt_duration(data.get("duration"))
        if duration:
            lines.append(f"时长：{duration}")
        published = _fmt_date(data.get("pubdate"))
        if published:
            lines.append(f"发布时间：{published}")
        desc = _truncate(str(data.get("desc") or ""), 300)
        if desc:
            lines.append(f"简介：{desc}")

        lines.append("")
        lines.append(
            f"这是视频页面的真实信息（{bvid}）。要分享就把链接原样发出去："
            f"https://www.bilibili.com/video/{bvid}/"
        )
        return "\n".join(lines)

    async def _render_html(self, url: str) -> tuple[str, str]:
        import aiohttp

        headers = {
            "User-Agent": _UA,
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,*/*;q=0.8"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
            # 很多国内站只放行"看起来像浏览器导航"的请求；缺这几个头会
            # 直接 403（知乎就是这么挡的）。加了不一定过，但不加必然过不了。
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
        }
        timeout = aiohttp.ClientTimeout(
            total=OPEN_TOOL_HTTP_TIMEOUT_SECONDS,
            sock_connect=8.0,
            sock_read=10.0,
        )
        connector = aiohttp.TCPConnector(ssl=_ssl_context(), limit=4)
        async with aiohttp.ClientSession(
            timeout=timeout, connector=connector, headers=headers,
        ) as session:
            async with session.get(url, allow_redirects=True) as response:
                status = response.status
                content_type = (response.headers.get("content-type") or "").lower()
                declared = response.charset
                final_url = str(response.url)
                chunks: list[bytes] = []
                downloaded = 0
                async for chunk in response.content.iter_chunked(16384):
                    downloaded += len(chunk)
                    if downloaded > _MAX_DOWNLOAD_BYTES:
                        # 读够就停：再读只会占内存，正文也不会更多
                        break
                    chunks.append(chunk)
                raw = b"".join(chunks)

        if status >= 400:
            return (
                f"（这个链接返回了 HTTP {status}，内容没读到。"
                "如果需要登录或者要付费，就直说打不开。）", final_url,
            )

        if "html" not in content_type and "text/" not in content_type:
            kind = content_type.split(";")[0] or "未知类型"
            return (
                f"（这个链接不是网页，是 {kind}。只能把链接发出去，"
                f"我读不了里面的内容。大小约 {downloaded // 1024} KB。）",
                final_url,
            )

        html = self._decode(raw, declared)
        return self._render_html_text(final_url or url, html), final_url

    @staticmethod
    def _decode(raw: bytes, declared: str | None) -> str:
        for encoding in (declared, "utf-8", "gb18030"):
            if not encoding:
                continue
            try:
                return raw.decode(encoding)
            except (LookupError, UnicodeDecodeError):
                continue
        return raw.decode("utf-8", "replace")

    def _render_html_text(self, url: str, html: str) -> str:
        host = urlparse(url).hostname or ""
        meta = _collect_meta(html)
        title_match = _TITLE_RE.search(html)
        title = _clean_title(
            meta.get("og:title") or (title_match.group(1) if title_match else "")
        )
        description = _truncate(
            meta.get("og:description") or meta.get("description") or "", 400,
        )
        body_limit = (
            _MAX_BODY_CHARS_WHEN_DESC_RICH if len(description) >= 150
            else _MAX_BODY_CHARS
        )
        body = _extract_body(html, body_limit)

        lines = [f"已打开：{title or '（这个页面没有标题）'}", f"来源：{host}"]
        if meta.get("og:site_name"):
            lines.append(f"站点：{meta['og:site_name']}")
        if description:
            lines.append(f"简介：{description}")
        if body:
            lines.append("")
            lines.append("正文节选：")
            lines.append(body)
        else:
            lines.append("")
            lines.append(
                "（正文没抓到——这个页面大概要靠浏览器脚本渲染，或者要登录。"
                "只能凭上面的标题和简介判断，不要编造具体内容。）"
            )
        lines.append("")
        lines.append(
            "以上是页面真实内容。要分享就把链接原样发出去，"
            "描述只能用上面读到的信息。"
        )
        return "\n".join(lines)

    # ── 缓存 ────────────────────────────────────────────────
    def _cache_get(self, url: str) -> str | None:
        hit = self._cache.get(url)
        if not hit:
            return None
        stamped, text = hit
        # 页面内容 3 分钟内基本不变，重复打开同一个链接没必要再等一次
        if time.monotonic() - stamped > 180.0:
            self._cache.pop(url, None)
            return None
        return text

    def _cache_put(self, url: str, text: str) -> None:
        if len(self._cache) >= 16:
            oldest = min(self._cache, key=lambda k: self._cache[k][0])
            self._cache.pop(oldest, None)
        self._cache[url] = (time.monotonic(), text)
