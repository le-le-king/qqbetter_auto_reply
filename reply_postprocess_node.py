from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any

from .pipeline_models import QQDeliveryPlan, QQMessageBlock, QQModelResult, QQReplyContext, QQReplyOutcome

#: 模型工具调用标记的正则（变体无关）。DSML 会以文本形式漏进正文，具体
#: 形态见 `_strip_tool_leak` 的注释。记忆侧复用同一条——会话历史里存的是
#: 模型原始输出，工具调用轮整段都是标记，不清洗就写进 /cache，她"记得"
#: 的自己发言会变成一堆 `<｜｜DSML｜｜ calls>`。
_DSML_MARKER = (
    r"</?"
    r"(?:[|｜‖]{1,2}\s*)?"
    r"DSML"
    r"(?:\s*[|｜‖]{1,2})?"
    r"(?:\s+[^>]*)?"
    r"\s*>"
)

#: 工具调用的**参数块**整段（连参数值一起）剥掉：
#:
#:     <｜｜DSML｜｜ parameter name="index" string="true">g9</｜｜DSML｜｜ parameter>
#:
#: 只删标记的话，剥完还剩一个孤零零的 `g9`——那既不是她要说的话，也不是
#: 用户看得到的东西，写进记忆只会让她"记得自己说过 g9"。parameter 里的
#: 是参数值，和 invoke 壳里的正文（`<...invoke name="msg">还是403` 的
#: `403`）性质不同，所以只对 parameter 成对挖空，invoke 壳保留。
_DSML_PARAM_BLOCK = (
    r"<[^<>]*DSML[^<>]*parameter[^<>]*>"
    r".*?"
    r"</[^<>]*DSML[^<>]*parameter\s*>"
)

#: `<feeling>` 连内容一起删——`annoyed` 是给表情/动作引擎的指令，不是话。
_FEELING_BLOCK = r"<feeling>.*?</feeling>"

#: 回复结构标签（`<msg>` `<text>` `<record>` …）。发送侧要它们，记忆侧不要：
#: 写进记忆的应当是她**说出口的那句话**，而不是模型输出的 XML 外壳。
_STRUCT_TAG = (
    r"</?(?:msg|text|emoji|mark|forward|record|sticker|poke|keyboard|at|reply|ark)"
    r"\b[^>]*>"
)

#: 整块只有编号（`[6]`）——她自己标视频用的角标，不是话。
_INDEX_ONLY = r"^[\[\(（【]?\s*\d{1,2}\s*[\]\)）】]?$"


class QQReplyPostprocessNode:
    def __init__(self, plugin: Any):
        self.plugin = plugin

    @staticmethod
    def _clean_dynamic_prefix(text: str) -> str:
        """Remove dynamic-format directives surrounding a literal prefix."""
        import re as _re

        cleaned = _re.sub(
            r"<wait>\s*\d+(?:\.\d+)?\s*</wait>",
            "",
            text,
            flags=_re.IGNORECASE,
        )
        # wait 可位于 opening fence 与首个 msg 之间；先拿掉 wait，fence
        # 才会重新成为前缀末尾，避免把 ```xml 当作可见 pre-tool 文本。
        cleaned = _re.sub(
            r"```(?:xml)?\s*$", "", cleaned, flags=_re.IGNORECASE,
        )
        return cleaned.strip()

    @staticmethod
    def _split_dynamic_xml(text: str) -> tuple[str, str] | None:
        """Separate literal assistant text from the dynamic XML document."""
        import re as _re

        msg_start = _re.search(r"<msg(?:\s|>)", text)
        if msg_start is None:
            return None
        leading_raw = text[:msg_start.start()]
        xml_text = _re.sub(r"\s*```\s*$", "", text[msg_start.start():])
        return QQReplyPostprocessNode._clean_dynamic_prefix(leading_raw), xml_text

    @staticmethod
    def _strip_tool_leak(text: str) -> str:
        """抹掉漏进正文的模型工具调用标记。

        2026-11-01 实际事故：模型想调 open_url 又没走通，把 DeepSeek 内部
        的调用语法原样吐进了正文——聊天里出现的就是

            <｜｜DSML｜｜ calls>
            <｜｜DSML｜｜ invoke name="msg">还是403

        这种标记不是给用户看的东西。`_parse_blocks` 因为找不到 `<msg>` 会
        把它当纯文本整段发出去，用户就看到一堆乱码。

        这里只做"拆壳"：把标记本身删掉，壳里的文字留着（上例删完剩
        "还是403"，这是她真正想说的话）。不做内容判断，避免误伤。
        """
        import re as _re

        if not text:
            return text
        # 变体无关的 DSML 标记剥除（2026-11-01 二次事故修正）。
        #
        # DeepSeek 的 DSML 工具调用语法在流式输出里会以文本形式漏进正文，
        # 且形态五花八门，之前按"管道夹 DSML"的固定形状写正则，漏了一堆：
        #   - 管道符号：| (U+007C)、｜ (U+FF5C 全角)、‖ (U+2016 双竖线)
        #   - V4.1 带前导空格：`｜DSML｜ calls`（管道与 DSML、DSML 与标签名
        #     之间都有空格），旧正则要求 `<` 后紧跟管道，匹配不到
        #   - 闭合标签：`</｜DSML｜ invoke>`，斜杠卡在 DSML 前面
        #   - 标签家族不止 calls，还有 invoke / parameter / tool_calls 等
        #
        # 这里统一成一条"变体无关"正则：可选斜杠 + 可选管道（|｜‖ 一到两个）
        # + 可选空白 + 字面 DSML（大小写不敏感）+ 直到 `>` 的任意内容。
        # 只要标记里含 DSML 字样且被尖括号包住，就整体删掉，壳里的文字留着。
        cleaned = _re.sub(_DSML_PARAM_BLOCK, "", text, flags=_re.I | _re.S)
        cleaned = _re.sub(_DSML_MARKER, "", cleaned, flags=_re.I)
        # 删完可能留下孤零零的空壳行
        cleaned = _re.sub(r"[ \t]+\n", "\n", cleaned)
        cleaned = _re.sub(r"\n{3,}", "\n\n", cleaned)
        return cleaned.strip()

    @staticmethod
    def strip_dsml(text: str) -> str:
        """对外的纯文本清洗：只剥工具调用标记，不解析 `<msg>` 结构。

        发送侧到这里就够了——结构化的块要按类型分别投递（文字/表情/语音/
        戳一戳），提前解析等于替她改口。记忆侧要的是另一回事，见
        `memory_text`。
        """
        return QQReplyPostprocessNode._strip_tool_leak(text)

    @staticmethod
    def memory_text(raw: str) -> str:
        """把模型原始输出还原成"用户真正看到的那句话"，用于写记忆。

        会话历史里存的是模型原文，直接落库就会带着 `<msg><text>` 外壳。
        2026-11-05 在 recent.json 里抓到过整条是
        `<msg><text>石楠花味儿的酸奶……</text></msg>` 的记录，还有
        `<feeling>annoyed</feeling>` 和整块只有 `[6]` 的角标块。

        这些都不是话：`<feeling>` 是给表情引擎的指令，`[6]` 是她自己标
        视频用的编号，`<msg>` 是投递格式。解析成纯文本**不改一个字**，
        只是把外壳去掉——多个 `<msg>` 各自一块，按顺序拼回来。
        """
        import re as _re

        text = QQReplyPostprocessNode.strip_dsml(str(raw or ""))
        if not text:
            return ""
        text = _re.sub(_FEELING_BLOCK, "", text, flags=_re.I | _re.S)
        if "<msg>" in text or "<msg " in text:
            try:
                blocks = QQReplyPostprocessNode._parse_blocks(text)
            except Exception:
                blocks = []
            parts = []
            for block in blocks:
                value = str(
                    getattr(block, "record", "") or getattr(block, "text", "") or ""
                ).strip()
                if not value or _re.match(_INDEX_ONLY, value):
                    continue
                parts.append(value)
            if parts:
                return "\n".join(parts).strip()
            if blocks:
                # 只发了表情包/戳一戳/角标：这一轮她没有"说"任何话。
                # 不能退回下面的剥标签路径——那会留下 `<sticker>5</sticker>`
                # 里的 `5`，变成她"记得自己说过 5"。
                return ""
        cleaned = _re.sub(_STRUCT_TAG, "", text, flags=_re.I)
        cleaned = _re.sub(r"[ \t]+\n", "\n", cleaned)
        cleaned = _re.sub(r"\n{3,}", "\n\n", cleaned)
        return cleaned.strip()

    @staticmethod
    def _parse_blocks(raw_text: str) -> list[QQMessageBlock]:
        """KiraAI-style `<msg>` 块解析器。将 LLM 输出解析为消息块列表。

        支持格式:
          <msg><text>文字</text><emoji>277</emoji></msg>
          <msg><sticker>5</sticker></msg>
          <msg><poke>123456</poke></msg>
          <msg><record>语音文本</record></msg>
        纯文本（无 <msg> 标签）回退为单块纯文本。
        """
        text = QQReplyPostprocessNode._strip_tool_leak((raw_text or "").strip())
        if not text:
            return []

        # 检查是否包含 <msg> 标签 → XML 解析
        if "<msg>" not in text and "<msg " not in text:
            # 检查是否使用旧式标签（<reply> <at> <sticker> <poke> <record> <keyboard>）
            if any(tag in text for tag in ("<reply>", "<at>", "<sticker>", "<poke>", "<record>", "<keyboard>")):
                return QQReplyPostprocessNode._parse_legacy_tags(text)
            # 纯文本当作一个块
            block = QQMessageBlock(text=text)
            return [block] if text else []

        # pre-tool 是 XML 文档外的普通文本，必须先切出来；若把它一起交给
        # ElementTree，模型自然输出的未转义 < / & 会让整个结构降级失败。
        import re as _re
        dynamic_parts = QQReplyPostprocessNode._split_dynamic_xml(text)
        if dynamic_parts is None:
            return [QQMessageBlock(text=text)]
        leading_text, xml_text = dynamic_parts

        # XML 解析
        try:
            root = ET.fromstring(f"<root>{xml_text}</root>")
        except ET.ParseError:
            # 解析失败 → 回退纯文本（去除 XML 标签）
            clean = _re.sub(r"<[^>]+>", "", xml_text).strip()
            blocks = []
            if leading_text:
                blocks.append(QQMessageBlock(text=leading_text))
            if clean:
                blocks.append(QQMessageBlock(text=clean))
            return blocks or [QQMessageBlock(text=text)]

        blocks: list[QQMessageBlock] = []
        if leading_text:
            blocks.append(QQMessageBlock(text=leading_text))

        for msg_el in root.findall("msg"):
            block = QQMessageBlock()

            # <text>
            # 必须遍历全部 <text>，不能只用 find 拿第一个。
            # 模型经常把「一句话 + 链接」拆成两个 <text> 放在同一个 <msg> 里，
            # 用 find 的话第二段会被静默丢掉——现象是"她说发了链接，对方只
            # 收到半句"，且日志里一切正常，极难查。这里按顺序拼回去。
            text_parts = [
                (child.text or "").strip()
                for child in msg_el.findall("text")
                if (child.text or "").strip()
            ]
            if text_parts:
                block.text = "\n".join(text_parts)

            # <emoji>
            emoji_el = msg_el.find("emoji")
            if emoji_el is not None and emoji_el.text:
                block.emoji = emoji_el.text.strip()

            # <at>
            at_el = msg_el.find("at")
            if at_el is not None and at_el.text:
                block.at_user = at_el.text.strip()

            # <reply>
            reply_el = msg_el.find("reply")
            if reply_el is not None and reply_el.text:
                block.reply_to = reply_el.text.strip()

            # <sticker>
            sticker_el = msg_el.find("sticker")
            if sticker_el is not None and sticker_el.text:
                block.sticker = sticker_el.text.strip()

            # <poke>
            poke_el = msg_el.find("poke")
            if poke_el is not None and poke_el.text:
                block.poke = poke_el.text.strip()

            # <record>
            record_el = msg_el.find("record")
            if record_el is not None and record_el.text:
                block.record = record_el.text.strip()

            # <keyboard>
            kb_el = msg_el.find("keyboard")
            if kb_el is not None and kb_el.text:
                block.keyboard = QQReplyPostprocessNode._normalize_keyboard(
                    kb_el.text
                )

            # <ark> with attrs
            ark_el = msg_el.find("ark")
            if ark_el is not None:
                for k, v in ark_el.attrib.items():
                    block.ark[k] = str(v)
                if ark_el.text:
                    block.ark["_body"] = ark_el.text.strip()

            # 如果没有任何子元素但有直接文本（裸 <msg>text</msg>）
            if not any([
                block.text, block.emoji, block.at_user, block.reply_to,
                block.sticker, block.poke, block.record, block.keyboard, block.ark,
            ]) and msg_el.text:
                block.text = msg_el.text.strip()

            blocks.append(block)

        # 空块列表 → 回退
        if not blocks:
            return [QQMessageBlock(text=text)]

        return blocks

    @staticmethod
    def _normalize_keyboard(raw: str | None) -> str:
        """Trim, drop empties, and cap at what the platform renders (4).

        Normalizing once at parse time keeps delivery and bookkeeping on
        the same value — the Open Platform sender builds at most four
        buttons, so a fifth option would be recorded but never shown."""
        options = [
            part.strip() for part in str(raw or "").split("|") if part.strip()
        ]
        return "|".join(options[:4])

    @classmethod
    def _parse_legacy_tags(cls, raw_text: str) -> list[QQMessageBlock]:
        """向后兼容：解析旧式散落标签（<reply> <at> <sticker> <poke> <record> <keyboard>）。
        将其转换为单个 QQMessageBlock（含标签属性）+ 可能的 sticker/poke 独立块。
        """
        import re
        text = raw_text

        reply_id = ""
        at_id = ""
        poke_user = ""
        sticker_id = ""
        voice_text = ""
        keyboard = ""

        m = re.search(r"<reply>(.*?)</reply>", text, re.IGNORECASE)
        if m:
            reply_id = m.group(1).strip()
            text = re.sub(r"<reply>.*?</reply>", "", text, flags=re.IGNORECASE)

        m = re.search(r"<at>(.*?)</at>", text, re.IGNORECASE)
        if m:
            at_id = m.group(1).strip()
            text = re.sub(r"<at>.*?</at>", "", text, count=1, flags=re.IGNORECASE)

        m = re.search(r"<poke>(.*?)</poke>", text, re.IGNORECASE)
        if m:
            poke_user = m.group(1).strip()
            text = re.sub(r"<poke>.*?</poke>", "", text, count=1, flags=re.IGNORECASE)

        m = re.search(r"<sticker>(.*?)</sticker>", text, re.IGNORECASE)
        if m:
            sticker_id = m.group(1).strip()
            text = re.sub(r"<sticker>.*?</sticker>", "", text, count=1, flags=re.IGNORECASE)

        m = re.search(r"<record>(.*?)</record>", text, re.IGNORECASE)
        if m:
            voice_text = m.group(1).strip()
            text = re.sub(r"<record>.*?</record>", "", text, count=1, flags=re.IGNORECASE)

        m = re.search(r"<keyboard>(.*?)</keyboard>", text, re.IGNORECASE)
        if m:
            # 旧式标签走同一个归一化：否则老输入能留下第五个按钮或空选项，
            # 而发送端只投前四个——投递与记忆记录又对不上了。
            keyboard = cls._normalize_keyboard(m.group(1))
            text = re.sub(r"<keyboard>.*?</keyboard>", "", text, count=1, flags=re.IGNORECASE)

        clean = text.strip()
        blocks: list[QQMessageBlock] = []

        if poke_user:
            blocks.append(QQMessageBlock(poke=poke_user))
        if clean or reply_id or at_id or voice_text or keyboard:
            blocks.append(QQMessageBlock(
                text=clean, reply_to=reply_id, at_user=at_id,
                record=voice_text, keyboard=keyboard,
            ))
        if sticker_id:
            blocks.append(QQMessageBlock(sticker=sticker_id))
        return blocks if blocks else [QQMessageBlock(text=raw_text)]

    async def _repair_xml(self, broken: str) -> str | None:
        """用 LLM 修复格式错误的 XML 输出（30秒超时，失败则放弃）。"""
        import asyncio
        try:
            from utils.config_manager import get_config_manager
            from utils.llm_client import create_chat_llm_async
            model_config = get_config_manager().get_model_api_config("conversation")
            if not model_config.get("base_url") or not model_config.get("model"):
                return None
            llm = await create_chat_llm_async(
                model=str(model_config["model"]),
                base_url=str(model_config["base_url"]),
                api_key=str(model_config.get("api_key", "")),
                max_completion_tokens=500,
                timeout=15.0,
                provider_type=model_config.get("provider_type"),
            )
            prompt = (
                "以下是一段格式错误的 XML，请修复它使其成为合法的 XML，不要改变任何内容和标签：\n\n"
                f"{broken}\n\n只返回修复后的 XML。"
            )
            response = await asyncio.wait_for(
                llm.ainvoke([{"role": "user", "content": prompt}]),
                timeout=15.0,
            )
            fixed = str(getattr(response, "content", "") or "").strip()
            if fixed and ("<msg>" in fixed or "<msg " in fixed):
                self.plugin._emit_log("INFO", "[XML修复] 成功修复格式错误")
                return fixed
        except Exception:
            pass
        return None

    async def finalize(self, context: QQReplyContext, model_result: QQModelResult) -> QQReplyOutcome:
        raw_reply_text = model_result.reply_text or ""
        reply_text = self.plugin._sanitize_generated_reply(raw_reply_text)
        known_pre_tool = str(
            getattr(model_result, "pre_tool_text", "") or ""
        )
        structural_pre_tool = (
            known_pre_tool
            if known_pre_tool and reply_text.startswith(known_pre_tool)
            else ""
        )
        wait_directive_text = (
            reply_text[len(structural_pre_tool):]
            if structural_pre_tool
            else reply_text
        )
        if raw_reply_text and not reply_text:
            self.plugin._emit_log("INFO", f"[Sanitize] {len(raw_reply_text)}字被清除: {raw_reply_text[:100]}")

        strategy_mode = getattr(self.plugin, "_strategy_mode", "neko_dynamic")
        blocks: list[QQMessageBlock] = []
        emoji_reaction_id = ""
        feeling = ""
        forward_content = ""
        forward_target = ""
        forward_count = 0
        mark_flag = False

        if strategy_mode == "neko_dynamic" and reply_text:
            import re
            # 先提取 <wait> 标签（XML 解析会忽略它），保存到 raw_reply_text 供 buffer 读取
            wm = re.search(r"<wait>(\d+(?:\.\d+)?)</wait>", reply_text, re.IGNORECASE)
            if wm:
                pass  # raw_reply_text 未被 sanitize 处理，保留原始标签
            # --- 提取独立标签（仅限 <msg> 之外的标签，不碰块内内容）---
            # 计算 <msg> 块区间，辅助判断标签是否在块外
            _msg_ranges = [(m.start(), m.end()) for m in re.finditer(r"<msg[\s>][\s\S]*?</msg>", reply_text, re.IGNORECASE)]
            def _outside_msg(pos: int) -> bool:
                return not any(start <= pos < end for start, end in _msg_ranges)
            # extract <emoji>ID</emoji> (standalone reaction tag)
            em = re.search(r"<emoji>(\d+)</emoji>", reply_text, re.IGNORECASE)
            if em and _outside_msg(em.start()):
                emoji_reaction_id = em.group(1).strip()
                reply_text = reply_text[:em.start()] + reply_text[em.end():]
                reply_text = reply_text.strip()
            # extract <mark/> (forward bookmark)
            mk = re.search(r"<mark\s*/>", reply_text, re.IGNORECASE)
            if mk and _outside_msg(mk.start()):
                reply_text = reply_text[:mk.start()] + reply_text[mk.end():]
                reply_text = reply_text.strip()
                mark_flag = True
            # extract <feeling>emotion</feeling> (mood tag)
            fm = re.search(r"<feeling>(\w+)</feeling>", reply_text, re.IGNORECASE)
            if fm and _outside_msg(fm.start()):
                feeling = fm.group(1).strip().lower()
                reply_text = reply_text[:fm.start()] + reply_text[fm.end():]
                reply_text = reply_text.strip()
            # extract <forward to="群号" count="30">content</forward>
            fw = re.search(r"<forward(\s+to\s*=\s*\"(\d+)\")?(\s+count\s*=\s*\"(\d+)\")?\s*>(.*?)</forward>", reply_text, re.DOTALL | re.IGNORECASE)
            if fw and _outside_msg(fw.start()):
                forward_content = fw.group(5).strip() if fw.group(5) else ""
                forward_target = fw.group(2) or ""
                forward_count = int(fw.group(4)) if fw.group(4) else 0
                reply_text = reply_text[:fw.start()] + reply_text[fw.end():]
                reply_text = reply_text.strip()

            # 刷新 wait_directive_text：上面可能剥离了 <feeling>/<emoji>/<mark>/<forward>，
            # 用旧值会导致 buffer 把空 feeling 当成有待投递内容
            wait_directive_text = reply_text

            # --- 处理 pre-tool 文本（core 在 tool-round start 捕获的模型文本）---
            explicit_prefix = ""
            parse_text = reply_text
            if structural_pre_tool:
                # 这是 core 在真实 tool-round start 捕获的模型文本，不是
                # dynamic XML 的格式前缀。完整 Markdown 围栏、<wait> 等
                # 字面内容都属于助手输出，不能再用启发式清理器裁剪。
                explicit_prefix = structural_pre_tool.strip()
                parse_text = reply_text[len(structural_pre_tool):]

            # --- XML 解析 ---
            dynamic_parts = self._split_dynamic_xml(parse_text)
            parse_failed = dynamic_parts is None and "</msg>" in parse_text
            broken_xml = parse_text if parse_failed else ""
            if dynamic_parts is not None:
                try:
                    ET.fromstring(f"<root>{dynamic_parts[1]}</root>")
                except ET.ParseError:
                    parse_failed = True
                    broken_xml = dynamic_parts[1]
            blocks = self._parse_blocks(parse_text)
            if explicit_prefix:
                blocks.insert(0, QQMessageBlock(text=explicit_prefix))
            # 解析失败必须显式进入修复；带 pre-tool 时 fallback 会产生两个
            # 纯文本块，不能再用 len(blocks) == 1 猜测解析状态。
            if parse_failed:
                leading_text = dynamic_parts[0] if dynamic_parts else ""
                repaired = await self._repair_xml(broken_xml)
                if repaired:
                    repaired_blocks = self._parse_blocks(repaired)
                    if repaired_blocks:
                        prefix_blocks = [
                            QQMessageBlock(text=value)
                            for value in (explicit_prefix, leading_text)
                            if value
                        ]
                        blocks = prefix_blocks + repaired_blocks
                        reply_text = repaired
            # 构建人类可读的 reply_text（首个块的文本）
            first_text = blocks[0].text if blocks else ""
            reply_text = first_text or reply_text
            # 日志：LLM 使用的标签
            tags = []
            if emoji_reaction_id:
                tags.append(f"emoji={emoji_reaction_id}")
            if feeling:
                tags.append(f"feeling={feeling}")
            if forward_content:
                tags.append("forward")
            for b in (blocks or []):
                if b.reply_to:
                    tags.append(f"reply={b.reply_to}")
                if b.at_user:
                    tags.append(f"at={b.at_user}")
                if b.sticker:
                    tags.append(f"sticker={b.sticker}")
                if b.poke:
                    tags.append(f"poke={b.poke}")
                if b.record:
                    tags.append("record")
                if b.keyboard:
                    tags.append("keyboard")
            if tags:
                self.plugin._emit_log("INFO", f"[Tags] {' | '.join(tags)}")

        if blocks or reply_text:
            return QQReplyOutcome(
                action="reply",
                reply_text=reply_text,
                raw_reply_text=raw_reply_text,
                pre_tool_text=structural_pre_tool,
                wait_directive_text=wait_directive_text,
                postprocess_reason="reply_xml" if strategy_mode == "neko_dynamic" else "reply",
                blocks=blocks,
                used_fallback=bool(getattr(model_result, "used_fallback", False)),
                feeling=feeling,
                emoji_reaction_id=emoji_reaction_id,
                forward_content=forward_content,
                forward_target=forward_target,
                forward_count=forward_count,
                forward_mark=mark_flag,
            )
        if context.ephemeral_session:
            return QQReplyOutcome(
                action="reply",
                reply_text=None,
                raw_reply_text=raw_reply_text,
                pre_tool_text=structural_pre_tool,
                wait_directive_text=wait_directive_text,
                postprocess_reason="empty",
                used_fallback=bool(getattr(model_result, "used_fallback", False)),
                feeling=feeling,
                emoji_reaction_id=emoji_reaction_id,
                forward_content=forward_content,
                forward_target=forward_target,
                forward_count=forward_count,
                forward_mark=mark_flag,
            )
        strategy_mode = getattr(self.plugin, "_strategy_mode", "neko_dynamic")
        # 管理员强制只适用私聊：群聊里即使主人说话，模型也常会合理选择沉默
        # （主人在跟其他群友聊、话题与她无关）。群轮的 permission_level 现在
        # 可能是 "admin"（发言人是主人时的身份覆盖），若不排除会把「决定不
        # 回复」覆盖成默认回复「嗯嗯~」，变成主人每说话她都插一句。
        is_forced = getattr(context, "force_reply", False) or (
            not context.is_group and context.permission_level == "admin"
        )
        if strategy_mode == "neko_dynamic" and not is_forced:
            return QQReplyOutcome(
                action="reply",
                reply_text=None,
                raw_reply_text=raw_reply_text,
                pre_tool_text=structural_pre_tool,
                wait_directive_text=wait_directive_text,
                postprocess_reason="llm_skip",
                used_fallback=bool(getattr(model_result, "used_fallback", False)),
                feeling=feeling,
                emoji_reaction_id=emoji_reaction_id,
                forward_content=forward_content,
                forward_target=forward_target,
                forward_count=forward_count,
                forward_mark=mark_flag,
            )
        # default/forced 回复同样没有本轮历史 ai 行：标记必须保留，
        # 否则 buffer 会把上一条已投递回复误记成未投递草稿。
        return QQReplyOutcome(
            action="reply",
            reply_text=self.plugin.i18n.t("messages.default_no_reply", default="嗯嗯~"),
            used_default_message=True,
            raw_reply_text=raw_reply_text,
            pre_tool_text=structural_pre_tool,
            wait_directive_text=wait_directive_text,
            postprocess_reason="default",
            used_fallback=bool(getattr(model_result, "used_fallback", False)),
            feeling=feeling,
            emoji_reaction_id=emoji_reaction_id,
            forward_content=forward_content,
            forward_target=forward_target,
            forward_count=forward_count,
        )

    def build_delivery_plan(self, request: Any, outcome: QQReplyOutcome) -> QQDeliveryPlan | None:
        if not outcome.blocks and not outcome.reply_text:
            return None
        # 如果没有 blocks（旧格式回退），用 reply_text 构造一个块
        blocks = outcome.blocks if outcome.blocks else [QQMessageBlock(text=outcome.reply_text or "")]
        if request.is_group:
            return QQDeliveryPlan(
                target_type="group",
                target_id=str(request.group_id or ""),
                blocks=blocks,
                fallback_to_text_on_voice_failure=request.fallback_to_text_on_voice_failure,
            )
        return QQDeliveryPlan(
            target_type="private",
            target_id=request.sender_id,
            blocks=blocks,
            fallback_to_text_on_voice_failure=request.fallback_to_text_on_voice_failure,
        )
