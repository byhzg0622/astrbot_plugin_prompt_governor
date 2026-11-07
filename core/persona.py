"""人格卡服务：取人格全文 → LLM 压缩 → 缓存 → 渲染注入块。

人格来源（按优先级）：
1. context.persona_manager.get_all_personas() 里按 persona_id 找；
2. 找不到（自定义人格/内联人格）时直接用 system_prompt 里拆出来的原文。

压缩失败绝不阻塞对话：人格卡模式退化为「原文照旧注入」。
"""

from __future__ import annotations

import hashlib
import inspect
from typing import Any

from astrbot.api import logger


def persona_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


async def get_all_personas(context: Any) -> list[Any]:
    """读人格池，兼容 get_all_personas 的同步 / 异步两种实现。

    AstrBot 4.5 前后这个接口从同步改成了异步（group_config 是 `await`
    调的）。这里两种都认，否则面板的人格卡页会整页空白。
    """
    manager = getattr(context, "persona_manager", None)
    if manager is None:
        return []
    getter = getattr(manager, "get_all_personas", None)
    if not callable(getter):
        return []
    try:
        result = getter()
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[注入管家] 读取人格池失败: {exc}")
        return []
    try:
        if inspect.isawaitable(result):
            result = await result
        return list(result or [])
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[注入管家] 解析人格池失败: {exc}")
        return []


def persona_fields(persona: Any) -> tuple[str, str, str]:
    """从人格对象 / dict 里取 (id, 名称, 人格全文)，字段名各版本兜底。"""
    def pick(*keys: str, default: str = "") -> str:
        if isinstance(persona, dict):
            for key in keys:
                value = persona.get(key)
                if value:
                    return str(value)
        for key in keys:
            value = getattr(persona, key, None)
            if value:
                return str(value)
        return default

    pid = pick("person_id", "persona_id", "id")
    name = pick("personal_name", "name", "persona_id", "person_id", default=pid)
    text = pick("prompt", "system_prompt", "text", "content")
    return pid, name, text


class PersonaCardService:
    """人格卡的生成与缓存。"""

    def __init__(self, store: Any, max_chars: int = 200, config: Any = None) -> None:
        self.store = store
        self.max_chars = max_chars
        #: 插件配置（用于读自定义分区表）；没有就用默认四区
        self.config = config
        self._inflight: dict[str, Any] = {}   # persona_key -> Task，防并发重复压缩

    # ------------------------------------------------------------------
    async def find_persona_text(self, context: Any, persona_id: str) -> str:
        """从人格池里找人格全文；找不到返回空串（兼容同步/异步人格池）。"""
        if not persona_id:
            return ""
        for persona in await get_all_personas(context):
            try:
                pid, _name, text = persona_fields(persona)
                if pid and pid == persona_id:
                    return text
            except Exception:  # noqa: BLE001
                continue
        return ""

    # ------------------------------------------------------------------
    async def ensure_card(
        self,
        context: Any,
        provider: Any,
        persona_id: str,
        persona_text: str,
    ) -> str | None:
        """确保有可用人格卡，返回卡片文本；拿不到返回 None。

        persona_text 为空（人格池里找不到、system_prompt 里也没拆出来）时
        返回 None —— 此时人格卡模式自动退化，原文照旧。

        provider 为空时只能用已有缓存，不尝试压缩（拿不到模型不算失败）。
        """
        if not persona_text.strip():
            return None
        key = persona_id or "__inline__"
        phash = persona_hash(persona_text)
        # 生成即固化：有档案就直接用，人格正文改了也不重算（面板可手动重写）
        cached = self.store.get_card(key)
        if cached:
            return cached
        if provider is None:
            return None

        # 并发去重：同一人格只压一次
        task = self._inflight.get(key)
        if task is not None:
            return await task

        import asyncio

        async def _compress() -> str | None:
            try:
                from .models import build_card_prompt

                from .models import persona_section_keys

                response = await asyncio.wait_for(
                    provider.text_chat(
                        prompt=build_card_prompt(
                            persona_text,
                            self.max_chars,
                            persona_section_keys(self.config),
                        ),
                        system_prompt=(
                            "你是提示词整理助手。按要求的格式输出分区人格卡，"
                            "不要任何解释、不要代码块。"
                        ),
                    ),
                    timeout=30.0,
                )
                raw = getattr(response, "completion_text", "") or ""
                card = str(raw).strip().strip("\u201c\u201d\"'").strip()
                if not card:
                    return None
                if len(card) > self.max_chars * 2:
                    card = card[: self.max_chars * 2]
                self.store.put_card(key, phash, card)
                logger.info(f"[注入管家] 人格卡已生成并缓存（{len(card)} 字）")
                return card
            except Exception as exc:  # noqa: BLE001 - 压缩失败不阻塞对话
                logger.warning(f"[注入管家] 人格卡压缩失败，本轮用原文人格: {exc}")
                return None
            finally:
                self._inflight.pop(key, None)

        task = asyncio.ensure_future(_compress())
        self._inflight[key] = task
        return await task

    # ------------------------------------------------------------------
    # 面板生成：直接压一份卡（可能模型当前不在线，面板允许重试）
    # ------------------------------------------------------------------
    async def generate_card(
        self,
        context: Any,
        persona_id: str,
        provider: Any | None = None,
    ) -> tuple[str | None, str | None]:
        """为面板「生成/重写」人格卡：强制重压并写缓存。

        返回 (卡片, 错误)；卡片非 None 即成功。provider 为空时会按会话/全局兜底找。
        """
        text = await self.find_persona_text(context, persona_id)
        if not text.strip():
            return None, "人格池里没找到这个人格（或它没有人格正文）"
        if provider is None:
            provider = await self._resolve_provider(context, "")
        if provider is None:
            return None, "拿不到当前会话的模型（会话还没建过模型连接，先聊一句再试）"
        try:
            from .models import build_card_prompt, persona_section_keys
            import asyncio

            response = await asyncio.wait_for(
                provider.text_chat(
                    prompt=build_card_prompt(
                        text, self.max_chars, persona_section_keys(self.config)
                    ),
                    system_prompt="你是提示词压缩助手。只输出压缩结果本身，不要任何解释。",
                ),
                timeout=60.0,
            )
            raw = getattr(response, "completion_text", "") or ""
            card = str(raw).strip().strip("\u201c\u201d\"'").strip()
            if not card:
                return None, "模型返回为空"
            if len(card) > self.max_chars * 2:
                card = card[: self.max_chars * 2]
            self.store.put_card(persona_id, persona_hash(text), card)
            return card, None
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[注入管家] 面板生成人格卡失败：{exc}")
            return None, f"{type(exc).__name__}: {exc}"

    # ------------------------------------------------------------------
    # 分区召回：recall_persona(section=...) 取某一个部分，而不是每次给整卡
    # ------------------------------------------------------------------
    async def recall_section(
        self,
        context: Any,
        persona_id: str,
        section: str,
        config: Any = None,
    ) -> str:
        """按分区名取人格卡的某一个部分；没这个区/没内容就退回整卡摘要。"""
        from .models import card_sections_text, persona_section_keys

        key = persona_id or "__inline__"
        # 档案是固化的：直接读，不因为人格正文改过就拒绝召回
        card = self.store.get_card(key)
        if not card:
            return ""
        sections = persona_section_keys(config)
        parts = card_sections_text(card, sections)
        sec_key = str(section or "").strip()
        if not sec_key or sec_key.lower() in ("summary", "all", "full"):
            return card
        key = sec_key.lower()
        if key not in sections:
            # 模型可能直接传中文分区名（或带【】），按名字反查 key
            plain = key.strip("【】[]()（） ")
            for candidate, (name, _hint) in sections.items():
                if plain == name.lower() or plain == candidate:
                    key = candidate
                    break
        body = parts.get(key, "")
        return body or card

    @staticmethod
    async def _resolve_provider(context: Any, umo: str) -> Any:
        """面板场景拿不到会话 provider，兜底：context 当前默认 provider。"""
        if context is None:
            return None
        try:
            if umo:
                provider = await context.get_using_provider_async(umo=umo)
                if provider:
                    return provider
        except Exception:  # noqa: BLE001
            pass
        try:
            return context.get_using_provider()
        except Exception:  # noqa: BLE001
            return None
