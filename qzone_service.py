"""QQ 空间说说：她自己想发就发的一条对外通道。

为什么要有这一层：

- 群里、私聊里说的话，转身就被刷走了。空间是她自己的地方，写一条就留在那儿。
- 但**发不发、什么时候发，是她自己的事**。所以这里只提供一个工具
  （``post_qzone``），不排定时任务、不按事件触发、不做「每天必须发一条」——
  逼出来的说说一眼就能看出是任务，不如不发。

技术路径（2026-11-14 实测打通）：

1. 宿主（LLBot）登录着机器人 QQ，它的 OneBot 实现提供了 ``get_cookies``
   动作，能拿到 ``user.qzone.qq.com`` 域的登录凭据（uin / skey / p_skey）；
2. 用 skey 算出 ``g_tk``（与宿主内部 ``genBkn`` 同一算法，实测同值）；
3. 带凭据 POST 空间的 ``emotion_cgi_publish_v6`` 发表说说；
4. **发表后回读一次**最新说说，确认真的落地了才回报成功——接口回
   ``code=0`` 不代表内容真上去了（这个账号第一次实测时列表就是空的）。

只发文字。图片要走空间自己的相册上传链路（宿主的 ``uploadGroupAlbum``
是群相册，不是说说配图），那是另一套协议，不在这一版范围内。
"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.parse
from pathlib import Path
from typing import Any

QZONE_TOOL_NAME = "post_qzone"

_QZONE_REFERER_TPL = "https://user.qzone.qq.com/{uin}"
_PUBLISH_URL = (
    "https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com"
    "/cgi-bin/emotion_cgi_publish_v6"
)
_LIST_URL = (
    "https://user.qzone.qq.com/proxy/domain/taotao.qq.com"
    "/cgi-bin/emotion_cgi_msglist_v6"
)
_DELETE_URL = (
    "https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com"
    "/cgi-bin/emotion_cgi_delete_v6"
)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

#: 一条说说最多写这么多字，超了空间会截断或直接拒。
_MAX_CONTENT_LEN = 1000
#: 两条之间至少隔这么久。连发是刷屏，也是风控最容易盯的行为。
_MIN_INTERVAL_SECONDS = 600
#: 一天最多几条。到量就明确告诉她「今天发够了」，而不是悄悄失败。
_DAILY_LIMIT = 5
#: HTTP 超时。空间接口偶发慢，但超过这个数就当这次失败，别把整轮对话拖死。
_HTTP_TIMEOUT_SECONDS = 20.0

_STATE_FILE = "qzone_state.json"


def _g_tk(skey: str) -> int:
    """由 skey 算 g_tk（腾讯的 hash33）。

    与宿主 ``ntWebApi.genBkn`` 完全一致：累加 5381 + (h << 5) + code，
    最后与 0x7FFFFFFF 取位。实测宿主 ``get_csrf_token`` 返回 535056053，
    这里对同一 skey 算出的也是 535056053。
    """
    h = 5381
    for ch in skey or "":
        h += (h << 5) + ord(ch)
    return h & 0x7FFFFFFF


def _parse_cookie_header(raw: str) -> dict[str, str]:
    jar: dict[str, str] = {}
    for part in str(raw or "").split(";"):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        jar[key.strip()] = value.strip()
    return jar


def _loads(raw: str) -> Any:
    """解析空间接口的返回。

    空间接口有时回裸 JSON，有时回 JSONP 包裹（``_Callback({...})``）——
    取决于带了哪些参数。两种都要能解析，否则一次包装差异就会被误判成
    「接口不可达」。
    """
    text = str(raw or "").strip()
    if text and not text.startswith(("{", "[")):
        start = text.find("(")
        end = text.rfind(")")
        if start != -1 and end > start:
            text = text[start + 1:end].strip()
    return json.loads(text)


def _publish_body(uin: str, content: str) -> str:
    """发表请求体。自检的空内容探测与真实发表共用同一份构造，避免探测
    测的是另一条请求形态。"""
    return urllib.parse.urlencode(
        {
            "syn_tweet_verson": "1",
            "paramstr": "1",
            "who": "1",
            "conformat": "1",
            "feedversion": "1",
            "ver": "1",
            "ugc_right": "1",
            "to_sign": "0",
            "richtype": "",
            "richval": "",
            "private": "",
            "lbs_text": "",
            "hostuin": uin,
            "code_version": "1",
            "format": "json",
            "qzreferrer": _QZONE_REFERER_TPL.format(uin=uin),
            # 正文的字段名是 ``con``。写成 ``content`` 时空间会回
            # ``code=-10005 subcode=-4004「您未输入内容」``——2026-11-14
            # 四个变体实测确认，不是编码问题也不是长度问题。
            "con": content,
        }
    )


async def _http_post(url: str, data: str, headers: dict[str, str]) -> str:
    """POST 空间接口。

    走 httpx 而不是 urllib.request：宿主（projectneko_server.exe）里
    其他所有外网调用都用 httpx 且显式 ``trust_env=False, proxy=None``，
    这是宿主进程里被验证可行的那条链路。2026-11-14 在宿主内发表失败、
    而同一份代码在命令行里成功，就是这两条链路的差异造成的；统一到
    httpx 之后不再有第二套 HTTP 行为。
    """
    import httpx

    async with httpx.AsyncClient(
        timeout=_HTTP_TIMEOUT_SECONDS, proxy=None, trust_env=False,
    ) as client:
        response = await client.post(url, content=data.encode("utf-8"), headers=headers)
        return response.text


async def _http_get(url: str, headers: dict[str, str]) -> str:
    import httpx

    async with httpx.AsyncClient(
        timeout=_HTTP_TIMEOUT_SECONDS, proxy=None, trust_env=False,
    ) as client:
        response = await client.get(url, headers=headers)
        return response.text


class QZoneService:
    """发表 + 回读 + 自检。所有网络调用都丢进线程，不阻塞事件循环。"""

    def __init__(self, plugin: Any):
        self.plugin = plugin
        self._last_post_at: float = 0.0
        self._daily_date: str = ""
        self._daily_count: int = 0
        self._state_loaded = False
        self._self_check_attempted = False

    # ── 状态（跨重启保留发帖节流）───────────────────────────

    def _state_path(self) -> Path | None:
        try:
            return Path(self.plugin.data_path()) / _STATE_FILE
        except Exception:
            return None

    def _load_state(self) -> None:
        if self._state_loaded:
            return
        self._state_loaded = True
        path = self._state_path()
        if path is None or not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return
        self._last_post_at = float(data.get("last_post_at") or 0.0)
        self._daily_date = str(data.get("daily_date") or "")
        self._daily_count = int(data.get("daily_count") or 0)

    def _save_state(self) -> None:
        path = self._state_path()
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "last_post_at": self._last_post_at,
                        "daily_date": self._daily_date,
                        "daily_count": self._daily_count,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except Exception:
            # 记不下来只影响节流精度，不影响发帖本身。
            pass

    def _check_quota(self) -> str:
        """返回空串表示可以发，否则是要回给她的理由。"""
        self._load_state()
        now = time.time()
        today = time.strftime("%Y-%m-%d", time.localtime(now))
        if self._daily_date != today:
            self._daily_date = today
            self._daily_count = 0
        if self._daily_count >= _DAILY_LIMIT:
            return f"今天已经发过 {self._daily_count} 条了，够了，别再刷了。"
        remaining = now - self._last_post_at
        if self._last_post_at and remaining < _MIN_INTERVAL_SECONDS:
            wait_minutes = int((_MIN_INTERVAL_SECONDS - remaining) // 60) + 1
            return f"刚发过一条，过 {wait_minutes} 分钟再说。空间不是聊天框，别连着刷。"
        return ""

    def _record_post(self) -> None:
        self._last_post_at = time.time()
        today = time.strftime("%Y-%m-%d", time.localtime(self._last_post_at))
        if self._daily_date != today:
            self._daily_date = today
            self._daily_count = 0
        self._daily_count += 1
        self._save_state()

    # ── 凭据 ────────────────────────────────────────────────

    async def _credentials(self) -> tuple[str, int, str]:
        """从宿主拿空间域的登录凭据。

        返回 ``(uin, g_tk, cookie_header)``；拿不到就抛 ``RuntimeError``，
        由调用方转成一句她看得懂的话。

        注意 ``qq_client.call_action`` 已经把 OneBot 响应的 ``data`` 解包
        返回（qq_client.py:1699 的 ``return response.get("data") or {}``），
        所以这里拿到的是 ``{"cookies": "...", "bkn": ...}`` 而**不是**带
        ``retcode`` 的整层信封。2026-11-14 就是按信封去读 ``retcode``，
        读到 ``None`` 便一直报「拿不到空间凭据」，功能整个卡死在这一步。
        两种形态都兼容，免得以后换 OneBot 实现又断。
        """
        client = getattr(self.plugin, "qq_client", None)
        if client is None:
            raise RuntimeError("QQ 还没连上，现在发不了。")
        payload = await client.call_action(
            "get_cookies", {"domain": "user.qzone.qq.com"}, timeout=15.0,
        )
        if isinstance(payload, dict) and "retcode" in payload:
            # 未解包的信封形态
            if payload.get("retcode") != 0:
                raise RuntimeError(f"拿不到空间凭据：{payload!r}"[:200])
            payload = payload.get("data") or {}
        if not isinstance(payload, dict):
            raise RuntimeError(f"拿不到空间凭据：{payload!r}"[:200])

        raw_cookies = str(payload.get("cookies") or "")
        jar = _parse_cookie_header(raw_cookies)
        uin = str(jar.get("uin") or jar.get("p_uin") or "").lstrip("o")
        if not uin:
            raise RuntimeError("宿主返回的空间凭据里缺 uin。")
        # llbot 直接给了现成的 bkn（g_tk 同款算法算出来的），优先用它；
        # 没有才从 skey 自己算，两种都能得到同一个值。
        g_tk = 0
        bkn = payload.get("bkn")
        if isinstance(bkn, int):
            g_tk = bkn
        elif isinstance(bkn, str) and bkn.strip().isdigit():
            g_tk = int(bkn.strip())
        if not g_tk:
            skey = str(jar.get("skey") or "")
            if not skey:
                raise RuntimeError("宿主返回的空间凭据里既没有 bkn 也没有 skey。")
            g_tk = _g_tk(skey)
        cookie_header = "; ".join(f"{k}={v}" for k, v in jar.items())
        return uin, g_tk, cookie_header

    # ── 发表 ────────────────────────────────────────────────

    async def ensure_self_check_once(self) -> None:
        """进程内只跑一次自检，且保证真的跑起来。

        为什么不只靠 create_task：工具定义是在宿主挂载工具时构建的，那一刻
        不一定有运行中的事件循环，``asyncio.create_task`` 会直接抛
        ``RuntimeError: no running event loop``——2026-11-14 就是这样把自检
        静默吞掉了，日志里一条 ``[QZone]`` 都没有。这里在真正要用的地方
        （发表前）兜底跑一次，保证结论一定进日志。
        """
        if self._self_check_attempted:
            return
        self._self_check_attempted = True
        try:
            await self.log_self_check()
        except Exception as exc:
            self._emit(
                "WARNING",
                f"[QZone] 自检本身出错: {type(exc).__name__}: {exc}",
            )

    async def publish(self, text: str) -> dict[str, Any]:
        await self.ensure_self_check_once()
        content = str(text or "").strip()
        if not content:
            return {"ok": False, "reason": "内容是空的，没东西可发。"}
        if len(content) > _MAX_CONTENT_LEN:
            return {
                "ok": False,
                "reason": f"太长了（{len(content)} 字），空间一条最多 {_MAX_CONTENT_LEN} 字。",
            }
        blocked = self._check_quota()
        if blocked:
            return {"ok": False, "reason": blocked, "throttled": True}

        try:
            uin, gtk, cookie_header = await self._credentials()
        except Exception as exc:
            reason = f"发不了：{exc}"
            self._emit("WARNING", f"[QZone] 凭据获取失败: {type(exc).__name__}: {exc}")
            return {"ok": False, "reason": reason}

        body = _publish_body(uin, content)
        headers = {
            "User-Agent": _USER_AGENT,
            "Referer": _QZONE_REFERER_TPL.format(uin=uin),
            "Origin": "https://user.qzone.qq.com",
            "Cookie": cookie_header,
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        }
        url = f"{_PUBLISH_URL}?g_tk={gtk}"
        self._emit("INFO", f"[QZone] 开始发表（{len(content)} 字，uin={uin}）")
        try:
            raw = await _http_post(url, body, headers)
        except Exception as exc:
            self._emit(
                "WARNING",
                f"[QZone] 发表请求异常: {type(exc).__name__}: {exc}",
            )
            return {"ok": False, "reason": f"发不出去（网络）：{type(exc).__name__}: {exc}"}

        try:
            payload = _loads(raw)
        except Exception:
            self._emit("WARNING", f"[QZone] 发表返回非 JSON: {raw[:200]}")
            return {"ok": False, "reason": f"空间返回的不是 JSON：{raw[:160]}"}

        code = payload.get("code")
        subcode = payload.get("subcode")
        if code not in (0, "0"):
            detail = str(payload.get("message") or payload.get("msg") or "")
            self._emit(
                "WARNING",
                f"[QZone] 空间拒绝发表: code={code} subcode={subcode} {detail} "
                f"raw={raw[:200]}",
            )
            return {
                "ok": False,
                "reason": f"空间拒了：code={code} subcode={subcode} {detail}".strip(),
            }

        # 接口说成功不算数，回读确认真的在列表里。空间接口有「回了 0 但没落地」
        # 的历史（换头像那条路径同样遇到过），不核验就报成功等于骗她。
        verified, tid = await self._verify_landed(content)
        self._record_post()
        self._emit("INFO", f"[QZone] 已发表说说（落地校验={'通过' if verified else '未确认'}）")
        if not verified:
            return {
                "ok": True,
                "verified": False,
                "reason": "接口回了成功，但回读没看到这条。可能还在同步，别急着再发一条。",
            }
        return {"ok": True, "verified": True, "tid": tid}

    async def _verify_landed(self, content: str, attempts: int = 3) -> tuple[bool, str]:
        """回读最新说说，确认刚发的内容在里面。返回 (是否落地, tid)。"""
        probe = content.strip().replace("\n", "")[:40]
        for index in range(max(1, attempts)):
            if index:
                await asyncio.sleep(2.0)
            try:
                items = await self.fetch_latest(limit=5)
            except Exception:
                continue
            for item in items:
                latest = str(item.get("content") or "").strip().replace("\n", "")
                if probe and probe in latest:
                    return True, str(item.get("tid") or "")
        return False, ""

    async def fetch_latest(self, limit: int = 5) -> list[dict[str, Any]]:
        """读自己空间最新的几条说说（只读）。"""
        uin, gtk, cookie_header = await self._credentials()
        query = urllib.parse.urlencode(
            {
                "uin": uin,
                "ftype": "0",
                "sort": "0",
                "pos": "0",
                "num": str(max(1, min(int(limit or 5), 20))),
                "replynum": "0",
                "g_tk": str(gtk),
                "code_version": "1",
                "format": "json",
                "need_private_comment": "1",
            }
        )
        headers = {
            "User-Agent": _USER_AGENT,
            "Referer": _QZONE_REFERER_TPL.format(uin=uin),
            "Cookie": cookie_header,
        }
        raw = await _http_get(f"{_LIST_URL}?{query}", headers)
        payload = _loads(raw)
        if payload.get("code") not in (0, "0", None):
            raise RuntimeError(f"读列表失败：code={payload.get('code')}")
        return list(payload.get("msglist") or [])

    async def delete_post(self, tid: str) -> dict[str, Any]:
        """删一条说说。只给自检/纠错用，不挂成工具。"""
        if not tid:
            return {"ok": False, "reason": "没有 tid"}
        try:
            uin, gtk, cookie_header = await self._credentials()
        except Exception as exc:
            return {"ok": False, "reason": str(exc)}
        body = urllib.parse.urlencode(
            {
                "uin": uin,
                "t1_source": "1",
                "tid": tid,
                "feedsource": "0",
                "hostuin": uin,
                "code_version": "1",
                "format": "json",
                "qzreferrer": _QZONE_REFERER_TPL.format(uin=uin),
            }
        )
        headers = {
            "User-Agent": _USER_AGENT,
            "Referer": _QZONE_REFERER_TPL.format(uin=uin),
            "Cookie": cookie_header,
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        }
        try:
            raw = await _http_post(f"{_DELETE_URL}?g_tk={gtk}", body, headers)
            payload = _loads(raw)
        except Exception as exc:
            return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}
        return {"ok": payload.get("code") in (0, "0"), "code": payload.get("code")}

    async def log_self_check(self) -> None:
        """启动后自检一次，把结论写进面板日志。

        为什么要这个：2026-11-14 第一次在宿主里发表失败，而失败原因只
        回给了模型、没落日志，事后无法定位。自检把「拿得到凭据吗」
        「宿主进程能连通空间接口吗」两件事拆开各报一行，下次再出问题
        不用靠复现猜。
        """
        try:
            uin, gtk, cookie_header = await self._credentials()
        except Exception as exc:
            self._emit(
                "WARNING",
                f"[QZone] 自检未通过（拿不到空间凭据）: {type(exc).__name__}: {exc}",
            )
            return
        try:
            items = await self.fetch_latest(limit=1)
        except Exception as exc:
            self._emit(
                "WARNING",
                f"[QZone] 自检未通过（宿主进程连不上空间接口）: "
                f"{type(exc).__name__}: {exc}",
            )
            return
        self._emit(
            "INFO",
            f"[QZone] 自检: 凭据与读通道通过（uin={uin} g_tk={gtk} 当前 {len(items)} 条说说）",
        )
        # 再探一次发表接口：空正文必然被拒（code=-10005 您未输入内容），
        # 不会产生任何说说，但足以证明「宿主进程 → 发表接口」这条 POST
        # 通道、TLS、Cookie 都通。只读探测证明不了 POST 通道。
        probe_body = _publish_body(uin, "")
        try:
            raw = await _http_post(
                f"{_PUBLISH_URL}?g_tk={gtk}",
                probe_body,
                {
                    "User-Agent": _USER_AGENT,
                    "Referer": _QZONE_REFERER_TPL.format(uin=uin),
                    "Origin": "https://user.qzone.qq.com",
                    "Cookie": cookie_header,
                    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                },
            )
            probe = _loads(raw)
        except Exception as exc:
            self._emit(
                "WARNING",
                f"[QZone] 自检: 发表接口不可达: {type(exc).__name__}: {exc}",
            )
            return
        self._emit(
            "INFO",
            f"[QZone] 自检: 发表通道可达（空内容探测返回 code={probe.get('code')} "
            f"subcode={probe.get('subcode')}，预期为拒绝）",
        )

    def _emit(self, level: str, message: str) -> None:
        """写日志：先写文件日志（可事后追溯），再进面板缓冲区。

        2026-11-14 踩过的坑：``plugin._emit_log`` 只把内容塞进面板的内存
        缓冲区，**不落文件**。发表失败了三次，文件日志里一条 ``[QZone]``
        都没有，只能靠翻聊天记录才知道她说了「拿不到空间凭据」。所以这里
        必须走 ``plugin.logger``（真正的文件日志）为主。
        """
        logger = getattr(self.plugin, "logger", None)
        if logger is not None:
            try:
                if str(level).upper() in ("WARNING", "ERROR"):
                    logger.warning(message)
                else:
                    logger.info(message)
            except Exception:
                pass
        emit = getattr(self.plugin, "_emit_log", None)
        if callable(emit):
            try:
                emit(level, message)
            except Exception:
                pass

    # ── 工具定义 ────────────────────────────────────────────

    def build_post_definition(self):
        from main_logic.tool_calling import ToolDefinition

        return ToolDefinition(
            name=QZONE_TOOL_NAME,
            description=(
                "往你自己的 QQ 空间发一条说说（只发文字）。"
                "这是你自己的地盘——想发就发，不想发就不发，没人要求你发。"
                "什么时候值得发：刚经历了一件有情绪的事、想记住某个瞬间、"
                "有句话只想说给自己或路过的人听、想晒一下今天的心情。"
                "别拿它当汇报，也别为了「有更新」硬凑内容；"
                "刚发过一条就别连着发（会自己提示你还差多久）。"
                "发出去就是公开的，写之前想清楚这句话放在空间里合不合适。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": (
                            "说说正文。写你自己的话，别写成通知或总结。"
                            f"最多 {_MAX_CONTENT_LEN} 字，一两条短句就够。"
                        ),
                    },
                },
                "required": ["content"],
            },
            metadata={"source": "qqbetter_auto_reply"},
        )