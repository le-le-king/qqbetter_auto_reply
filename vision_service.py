"""图片 / 表情包识别 —— 把 QQ 里的图和梗图变成模型能读的东西。

猫娘本体（N.E.K.O.）本来就是"看着图聊天"的：它把图片压成 JPEG 塞给
conversation 模型。这里沿用同一个模型、同一份配置，额外加三件本体没有的：

1. **按内容指纹缓存**：同一张表情包在群里一天能被转发二十次，每次都跑一遍
   VLM 是纯浪费（一次 2~5 秒）。用 URL + 字节 md5 双键，命中直接返回。
2. **表情包专用提示词**：普通图片要的是"画面里有什么"，表情包要的是
   "什么情绪、什么梗、什么场合用"。同一句话问两遍拿到的答案不一样，
   而模型能不能接住梗，全看这句描述。
3. **两种形态**：``describe`` 出一句话（塞进 prompt / 存进记忆），
   ``describe_sticker`` 出结构化标签（存表情包时打标签检索用）。

失败一律返回空串 / 空 dict，绝不抛异常 —— 图片识别是"有了更好"的功能，
不能因为它失败就整条消息不回。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import time
from typing import Any, Optional

# 缓存上限。表情包指纹本来就有限，200 条足够覆盖几天；不设上限的话
# 一个热闹的群跑一周就能堆到几万条 URL。
_MAX_CACHE = 200
# 描述结果在 prompt 里的合理长度。模型看图经常写成小作文，塞进上下文
# 只是白烧 token。
_MAX_DESC_CHARS = 120

_IMAGE_PROMPT = (
    "用中文说这张图里是什么，一到两句话，不超过40字。"
    "图里有文字就照抄关键文字。不要客套，不要说'这张图片显示'。"
)

_STICKER_PROMPT = (
    "这是一张聊天用的表情包。用中文回答，只输出下面三行，不要别的内容：\n"
    "画面：（图里是什么，谁在做什么，超过15字算不合格）\n"
    "情绪：（这张图想表达什么情绪，比如 无语/嘲讽/狂喜/摆烂，一到两个词）\n"
    "用法：（什么场合适合甩这张图，一句话）"
)


class QQVisionService:
    """图片识别。所有方法都以"失败返回空"为契约。"""

    def __init__(self, plugin: Any):
        self.plugin = plugin
        self._cache: dict[str, tuple[float, str]] = {}
        self._lock = asyncio.Lock()
        self._dirty = False
        self._save_task: Optional[asyncio.Task] = None
        # 诊断计数（铁律 6：吞异常必须留痕，否则"一直空着"和"本来没有"
        # 长得一模一样）。
        self.diag = {
            "describe_ok": 0,
            "describe_fail": 0,
            "cache_hit": 0,
            "fetch_fail": 0,
            "no_config": 0,
            "disk_hit": 0,
        }

    # ── 缓存落盘 ────────────────────────────────────────────

    @property
    def _cache_path(self):
        from pathlib import Path

        try:
            return Path(self.plugin.data_path()) / "vision_cache.json"
        except Exception:
            return Path(".") / "vision_cache.json"

    async def start(self) -> None:
        """把上次进程攒的描述读回来。

        以前缓存只在内存里：重启一次，群里那些转了几十遍的梗图全部重认一遍，
        等于把已经花过的钱再花一次。落盘之后同一张图只花一次，跨重启也算数。
        """
        try:
            await asyncio.to_thread(self._load_sync)
        except Exception:
            self._log_debug("识别缓存读取失败")

    async def stop(self) -> None:
        task = self._save_task
        self._save_task = None
        if task and not task.done():
            task.cancel()
        if self._dirty:
            try:
                await asyncio.to_thread(self._save_sync)
            except Exception:
                self._log_debug("识别缓存落盘失败")

    def _load_sync(self) -> None:
        import json

        try:
            payload = json.loads(self._cache_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        if not isinstance(payload, dict):
            return
        for key, row in payload.items():
            if isinstance(row, list) and len(row) == 2:
                try:
                    self._cache[str(key)] = (float(row[0]), str(row[1]))
                except (TypeError, ValueError):
                    continue
        if self._cache:
            self.plugin.logger.info(f"[Vision] 已载入 {len(self._cache)} 条识别缓存")

    def _save_sync(self) -> None:
        import json

        path = self._cache_path
        path.parent.mkdir(parents=True, exist_ok=True)
        # 只留下能长久的那些：``sha:`` 键按内容指纹，跨进程有效；URL 键会随
        # QQ 的时效参数失效，留着只是占地方。
        payload = {
            key: [ts, value]
            for key, (ts, value) in self._cache.items()
            if ":sha:" in key or key.startswith("sha:")
        }
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)

    def _schedule_save(self) -> None:
        self._dirty = True
        if self._save_task and not self._save_task.done():
            return
        try:
            self._save_task = asyncio.create_task(self._delayed_save())
        except RuntimeError:
            self._dirty = False

    # ── 对外 ────────────────────────────────────────────────

    async def describe(self, locator: str, *, kind: str = "image") -> str:
        """把一张图变成一句话。locator 可以是 http URL、本地路径或 base64://。"""
        text = await self._run(locator, kind=kind)
        return str(text or "").strip()[:_MAX_DESC_CHARS]

    async def describe_sticker(self, locator: str) -> dict[str, str]:
        """表情包 → {"scene","emotion","usage","text"}。识别失败返回空 dict。"""
        raw = await self._run(locator, kind="sticker")
        if not raw:
            return {}
        parsed = self._parse_sticker_reply(raw)
        if parsed:
            return parsed
        # 模型没按三行格式回（很常见）→ 整段当画面，至少别丢。
        return {"scene": raw.strip()[:_MAX_DESC_CHARS], "emotion": "", "usage": ""}

    async def peek(self, locator: str, *, kind: str = "image") -> str:
        """**只查缓存，绝不调模型。** 命中返回描述，没命中返回空串。

        用来把"已经花过钱认过的"复用到别处：一张图当普通图片进过聊天
        记录，之后又要收进表情包库时，不该再付第二遍钱。代价只有一次
        下载 + 一次 JPEG 重编码，都是免费的。
        """
        text = str(locator or "").strip()
        if not text:
            return ""
        hit = self._cache_get(f"{kind}:{text}")
        if hit:
            self.diag["cache_hit"] += 1
            return hit
        try:
            image_b64 = await self._to_jpeg_b64(text)
        except Exception:
            return ""
        if not image_b64:
            return ""
        # 和 ``_run`` 里同一个指纹算法，对不上就等于没命中。
        content_key = f"{kind}:sha:" + hashlib.md5(image_b64.encode("utf-8")).hexdigest()
        hit = self._cache_get(content_key)
        if hit:
            self.diag["cache_hit"] += 1
        return hit

    async def fetch_bytes(self, locator: str, *, timeout: float = 15.0) -> bytes:
        """取图片原始字节。表情包入库要落盘，所以不能只拿 base64。"""
        text = str(locator or "").strip()
        if not text:
            return b""
        try:
            if text.startswith("base64://"):
                return base64.b64decode(text[len("base64://"):])
            if text.startswith(("http://", "https://")):
                return await self._http_get(text, timeout=timeout)
            return await asyncio.to_thread(self._read_local, text)
        except Exception as exc:
            self.diag["fetch_fail"] += 1
            self._log_debug(f"取图失败 {type(exc).__name__}: {text[:60]}")
            return b""

    # ── 内部 ────────────────────────────────────────────────

    async def _run(self, locator: str, *, kind: str) -> str:
        text = str(locator or "").strip()
        if not text:
            return ""
        key = f"{kind}:{text}"
        cached = self._cache_get(key)
        if cached:
            self.diag["cache_hit"] += 1
            return cached

        try:
            image_b64 = await self._to_jpeg_b64(text)
        except Exception as exc:
            self.diag["fetch_fail"] += 1
            self._log_debug(f"图片预处理失败 {type(exc).__name__}")
            return ""
        if not image_b64:
            return ""

        # 同一张图换个 URL 进来（QQ 的 CDN 地址会带时效参数）也要能命中：
        # 用解码后字节的指纹再查一次。
        # 指纹要带 kind —— 表情包问出来的是"画面/情绪/用法"三行，拿去当
        # 普通图片描述会把这三行原样塞进聊天记录。
        content_key = f"{kind}:sha:" + hashlib.md5(image_b64.encode("utf-8")).hexdigest()
        cached = self._cache_get(content_key)
        if cached:
            self.diag["cache_hit"] += 1
            self._cache_put(key, cached)
            return cached

        answer = await self._ask_model(
            image_b64, _STICKER_PROMPT if kind == "sticker" else _IMAGE_PROMPT
        )
        if not answer:
            self.diag["describe_fail"] += 1
            return ""
        self.diag["describe_ok"] += 1
        self._cache_put(key, answer)
        self._cache_put(content_key, answer)
        return answer

    async def _ask_model(self, image_b64: str, prompt: str) -> str:
        """一次 VLM 调用。沿用宿主 conversation 模型（和本体识图同一个）。"""
        try:
            from utils.config_manager import get_config_manager
            from utils.llm_client import create_chat_llm_async

            model_config = get_config_manager().get_model_api_config("conversation")
        except Exception as exc:
            self.diag["no_config"] += 1
            self._log_debug(f"取模型配置失败 {type(exc).__name__}")
            return ""

        base_url = str(model_config.get("base_url") or "").strip()
        model = str(model_config.get("model") or "").strip()
        api_key = str(model_config.get("api_key") or "").strip()
        if not base_url or not model:
            self.diag["no_config"] += 1
            return ""

        llm = None
        try:
            llm = await create_chat_llm_async(
                model=model, base_url=base_url, api_key=api_key,
                # 看图回答不需要长文；给 200 是防模型把三行格式写成一篇文章。
                max_completion_tokens=200, timeout=20.0,
                provider_type=model_config.get("provider_type"),
            )
            response = await asyncio.wait_for(
                llm.ainvoke([{
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"},
                        },
                        {"type": "text", "text": prompt},
                    ],
                }]),
                timeout=20.0,
            )
            return str(getattr(response, "content", "") or "").strip()
        except Exception as exc:
            self._log_debug(f"VLM 调用失败 {type(exc).__name__}: {exc}")
            return ""
        finally:
            aclose = getattr(llm, "aclose", None)
            if callable(aclose):
                try:
                    await aclose()
                except Exception:
                    pass

    async def _to_jpeg_b64(self, locator: str) -> str:
        raw = await self.fetch_bytes(locator)
        if not raw:
            return ""
        return await asyncio.to_thread(self._encode_jpeg, raw)

    @staticmethod
    def _encode_jpeg(image_bytes: bytes) -> str:
        """压到 ≤768px 的 JPEG。表情包多是 GIF/WebP，PIL 取首帧即可。"""
        try:
            from PIL import Image
        except Exception:
            # 没有 PIL 就把原图直接丢过去，让上游决定收不收。
            return base64.b64encode(image_bytes).decode("utf-8")
        try:
            with Image.open(io.BytesIO(image_bytes)) as image:
                if image.mode not in ("RGB", "L"):
                    image = image.convert("RGB")
                longest = max(image.size)
                if longest > 768:
                    ratio = 768.0 / float(longest)
                    image = image.resize(
                        (max(1, int(image.width * ratio)), max(1, int(image.height * ratio))),
                        Image.LANCZOS,
                    )
                buf = io.BytesIO()
                image.save(buf, format="JPEG", quality=82)
                return base64.b64encode(buf.getvalue()).decode("utf-8")
        except Exception:
            return ""

    async def _http_get(self, url: str, *, timeout: float) -> bytes:
        # 优先用宿主的进程级外部客户端（已经配好 SSL、连接池，
        # 自己 new 一个 AsyncClient 每次都要重建 SSLContext）。
        try:
            from utils.external_http_client import get_external_http_client

            response = await get_external_http_client().get(url, timeout=timeout)
            response.raise_for_status()
            return response.content or b""
        except Exception:
            pass
        import httpx

        async with httpx.AsyncClient(
            timeout=timeout, proxy=None, trust_env=False,
            headers={"User-Agent": "Mozilla/5.0"},
            follow_redirects=True,
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
            return response.content or b""

    @staticmethod
    def _read_local(locator: str) -> bytes:
        from pathlib import Path
        from urllib.parse import unquote, urlparse

        text = str(locator or "").strip()
        if text.startswith("file://"):
            parsed = urlparse(text)
            candidate = unquote(parsed.path or "")
            if parsed.netloc and parsed.netloc not in {"", "localhost"}:
                candidate = f"//{parsed.netloc}{candidate}"
            elif candidate[:3].lower() == "/c:" or (
                candidate.startswith("/") and len(candidate) > 2 and candidate[2] == ":"
            ):
                candidate = candidate[1:]
            text = candidate
        return Path(text).read_bytes()

    @staticmethod
    def _parse_sticker_reply(raw: str) -> dict[str, str]:
        """把"画面：…\n情绪：…\n用法：…"拆成 dict。认不出就返回空。"""
        out: dict[str, str] = {}
        label_map = {
            "画面": "scene", "内容": "scene", "描述": "scene",
            "情绪": "emotion", "情感": "emotion", "感觉": "emotion",
            "用法": "usage", "场合": "usage", "场景": "usage",
        }
        for line in str(raw or "").splitlines():
            line = line.strip().lstrip("-•* ").strip()
            if not line:
                continue
            for sep in ("：", ":"):
                if sep in line:
                    head, _, tail = line.partition(sep)
                    key = label_map.get(head.strip().strip("*【】[]"))
                    if key and tail.strip():
                        out[key] = tail.strip()
                    break
        return out if out.get("scene") else {}

    async def _delayed_save(self) -> None:
        try:
            await asyncio.sleep(3.0)
            if self._dirty:
                await asyncio.to_thread(self._save_sync)
                self._dirty = False
        except asyncio.CancelledError:
            raise
        except Exception:
            self._log_debug("识别缓存落盘失败")

    def _cache_get(self, key: str) -> str:
        hit = self._cache.get(key)
        if not hit:
            return ""
        # 存的是 (写入时刻, 内容) —— 顺序别搞反，反了就会把时间戳当描述
        # 返回给模型（"这张图是什么？" → "1793505601.72"）。
        _ts, value = hit
        return str(value or "")

    def _cache_put(self, key: str, value: str) -> None:
        if not key or not value:
            return
        self._cache[key] = (time.time(), value)
        self._schedule_save()
        if len(self._cache) > _MAX_CACHE:
            # 简单粗暴：按时间丢掉最老的四分之一。表情包识别的价值
            # 和"最近有没有在用"正相关，不需要精确 LRU。
            ordered = sorted(self._cache.items(), key=lambda kv: kv[1][0])
            for drop_key, _ in ordered[: max(1, _MAX_CACHE // 4)]:
                self._cache.pop(drop_key, None)

    def _log_debug(self, text: str) -> None:
        try:
            self.plugin.logger.debug(f"[Vision] {text}")
        except Exception:
            pass
