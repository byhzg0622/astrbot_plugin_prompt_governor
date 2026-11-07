"""注入管家（astrbot_plugin_prompt_governor）。

看清楚每一轮 LLM 请求里注入了什么，把静态人格变成可召回的人格卡，
按会话摘除不需要的标记块 —— 但绝不动别人的代码。

全通道注入检测（v1.3：三层检测 + 最下游钩子，把漏检的注入全捞回来）：
- 强检测：<tag>...</tag> 标记块 —— 扫全部 5 个通道（system / prompt /
  附加段 / 历史消息 / 工具结果），不只是旧版的 2 个；
- 锚点检测：已知无标记注入（# Persona Instructions、<system_reminder>、
  <image_caption>、kb_result、侧栏引文…）；
- 基线 diff：base 钩子（priority=+2000，先于所有其他插件）拍下 3 个
  通道的原文指纹，final 钩子 diff 出被追加/改写的裸文本（无 tag 也拦得住）；
- 最下游钩子 on_agent_done：agent 工具循环的第 2 轮起不再经过
  on_llm_request，这里在 provider 真正发出请求之后读 run_context.messages
  （最终发给模型的每一个字）做终态全通道扫描，保证多轮注入也全部可见。

钩子设计（利用 star_handlers_registry 按 priority 降序执行）：
- on_llm_request_base   priority=+2000：**最先**执行，快照全通道基线指纹
- （其他插件在这里做各自的注入）
- on_llm_request_final  priority=-2000：**最后**执行，三层全通道解析、
  执行摘除、替换人格块为卡片
- on_agent_done         （agent 结束，最下游）：读 messages 终态补审计
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools

from .api.webapi import WebApiMixin
from .core.audit import apply_trim, load_baseline, measure_injection, snapshot_blocks
from .core.models import (
    RequestBaseline,
    persona_section_keys,
    render_persona_card_with_sections,
    split_persona_card,
)
from .core.persona import PersonaCardService
from .core.store import GovernorStore

PLUGIN_NAME = "astrbot_plugin_prompt_governor"
PLUGIN_VERSION = "1.3"

try:
    from astrbot.api.star import register
except ImportError:  # 极老版本没有 register：退化成空装饰器
    def register(*_args: Any, **_kwargs: Any):
        def decorator(cls: Any) -> Any:
            return cls

        return decorator


def _optional_hook(name: str):
    decorator = getattr(filter, name, None)
    if callable(decorator):
        return decorator

    def _noop(*_args: Any, **_kwargs: Any):
        def _wrap(func: Any) -> Any:
            return func

        return _wrap

    return _noop


llm_tool = _optional_hook("llm_tool")
agent_done_hook = _optional_hook("on_agent_done")

# 注入的「人格卡」本体已经自带使用说明（摘要 + 可用部分目录 + 怎么取），
# 所以这里不再额外追加整段提示词 —— 常驻注入越短越好。


@register(PLUGIN_NAME, "dsh", "注入管家：审计注入词、人格卡化、标记块摘除", PLUGIN_VERSION)
class PromptGovernor(WebApiMixin, Star):
    """审计 + 人格卡 + 摘除主管线。"""

    def __init__(self, context: Context, config: Any = None) -> None:
        super().__init__(context, config)
        self.config = config if isinstance(config, dict) else {}
        self.data_dir = self._resolve_data_dir()
        self.store = GovernorStore(
            self.data_dir,
            history_limit=int(self._cfg("audit.history_limit", 200)),
        )
        self.card_service = PersonaCardService(
            self.store,
            max_chars=int(self._cfg("persona_card.max_chars", 200)),
            config=self.config,
        )
        self.register_web_apis()
        logger.info(f"[注入管家] 已加载，数据目录 {self.data_dir}")

    # ------------------------------------------------------------------
    # 基础
    # ------------------------------------------------------------------
    def _resolve_data_dir(self) -> Path:
        try:
            return Path(StarTools.get_data_dir(PLUGIN_NAME))
        except Exception:  # noqa: BLE001
            return Path(__file__).resolve().parent / "data"

    def _cfg(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.config
        for part in str(dotted).split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def _flag(self, dotted: str, default: bool = False) -> bool:
        value = self._cfg(dotted, default)
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    # ------------------------------------------------------------------
    # 钩子 1：最先执行 —— 全通道基线快照（只读，绝无副作用）
    # ------------------------------------------------------------------
    @filter.on_llm_request(priority=2000)
    async def on_llm_request_base(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        try:
            event.set_extra("_pg_baseline", RequestBaseline.make(req))
        except Exception:  # noqa: BLE001 - 基线失败不影响后续（diff 层会退化）
            pass

    # ------------------------------------------------------------------
    # 钩子 2：最后执行 —— 终态解析 + 摘除 + 人格卡
    # ------------------------------------------------------------------
    @filter.on_llm_request(priority=-2000)
    async def on_llm_request_final(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        if not self._flag("enabled", True):
            return
        try:
            umo = event.unified_msg_origin
            baseline = load_baseline(event)
            snap, persona_text = snapshot_blocks(
                req,
                baseline,
                full_channel=self._flag("scan.full_channel", True),
                anchor_scan=self._flag("scan.anchor_scan", True),
                do_diff=self._flag("scan.baseline_diff", True),
            )
            snap.t = time.time()
            snap.umo = umo

            # ---------- 1) 标记块摘除 ----------
            if self._flag("trim.enabled", True):
                tags = self.store.tags_for(umo)
                snap.removed_tags = apply_trim(req, tags)

            # ---------- 2) 人格卡模式 ----------
            if self._flag("persona_card.enabled", False) and persona_text:
                try:
                    replaced = await self._apply_persona_card(event, req, persona_text)
                    snap.persona_card_used = replaced
                except Exception as exc:  # noqa: BLE001 - 人格卡失败退回原文
                    logger.warning(f"[注入管家] 人格卡处理失败，本轮用原文: {exc}")

            # ---------- 3) 重新量终态体积 ----------
            # 摘除 / 人格卡替换都改过 req，必须重测，否则面板上
            # 「注入前 → 注入后」永远相等，看不出这几步到底省了多少。
            snap.after_chars, snap.parts_chars = measure_injection(req)

            # ---------- 4) 记录审计 ----------
            if self._flag("audit.enabled", True):
                record = snap.to_dict()
                if not self._flag("audit.capture_content", True):
                    for block in record.get("blocks", []):
                        block.pop("text", None)
                self.store.push_history(umo, record)
                if self._flag("debug_log", False):
                    logger.debug(
                        f"[注入管家] {umo} 注入审计：system "
                        f"{snap.before_chars}->{snap.after_chars} 字符，"
                        f"块 {len(snap.blocks)} 个，摘除 {snap.removed_tags}"
                    )
        except Exception as exc:  # noqa: BLE001 - 钩子异常绝不能打断对话
            logger.error(f"[注入管家] on_llm_request_final 出错：{exc}", exc_info=True)

    # ------------------------------------------------------------------
    async def _apply_persona_card(
        self, event: AstrMessageEvent, req: ProviderRequest, persona_text: str
    ) -> bool:
        """把 system_prompt 里的人格块换成「人格卡技能」：摘要 + 可用部分目录。

        人格全文不再每轮注入；模型要哪部分就用 recall_persona(section=...)
        取哪部分。返回是否替换成功。
        """
        from .core.models import split_persona_block

        system = req.system_prompt or ""
        _persona, rest = split_persona_block(system)
        if not rest and not _persona:
            return False

        persona_id = ""
        conversation = getattr(req, "conversation", None)
        if conversation is not None:
            persona_id = str(getattr(conversation, "persona_id", "") or "")

        provider = None
        try:
            provider = await self.context.get_using_provider_async(
                umo=event.unified_msg_origin
            )
        except Exception:  # noqa: BLE001
            provider = None
        if provider is None:
            try:
                provider = self.context.get_using_provider()
            except Exception:  # noqa: BLE001
                provider = None

        # provider 可能拿不到 —— 这时只用已有缓存；没缓存就退回原文人格
        card = await self.card_service.ensure_card(
            self.context, provider, persona_id, persona_text
        )
        if not card:
            return False   # 压缩失败：原文人格保持原样

        # 只注入摘要 + 部分目录：分区正文留给 recall_persona 按需取
        sections = persona_section_keys(self.config)
        summary, parts = split_persona_card(card, sections)
        block = render_persona_card_with_sections(summary, list(parts.keys()), sections)
        if not block:
            return False

        # 卡片放原人格块的位置附近：rest 保留其他注入，卡片贴在尾部稳定位置
        new_system = (rest + "\n\n" + block).strip()
        req.system_prompt = new_system
        return True

    # ------------------------------------------------------------------
    # 钩子 3：最下游 —— agent 结束（provider 已发出请求）终态补审计
    # ------------------------------------------------------------------
    @agent_done_hook(priority=-2000)
    async def on_agent_done_final(
        self,
        event: AstrMessageEvent,
        run_context: Any,
        response: Any,
    ) -> None:
        """agent 工具循环（第 2 轮 LLM 起）不经过 on_llm_request，
        在最下游（provider 真正发出请求之后）读 run_context.messages
        —— 最终发给模型的每一个字 —— 做全通道终态扫描，把多轮注入
        也记进审计（只记检测，不改请求；改请求是 final 钩子的活）。"""
        if not self._flag("enabled", True):
            return
        if not self._flag("scan.agent_done_audit", True):
            return
        try:
            messages = getattr(run_context, "messages", None)
            if not messages:
                return
            # 复用全通道扫描：把 messages 包装成一个「伪 req」，
            # 把每条消息按角色映射到 history 通道。
            import types as _types

            fake_req = _types.SimpleNamespace(
                system_prompt="",
                prompt="",
                extra_user_content_parts=[],
                contexts=[
                    {
                        "role": str(getattr(m, "role", "") or ""),
                        "content": getattr(m, "content", ""),
                    }
                    for m in messages
                ],
                tool_calls_result=None,
                conversation=None,
            )
            snap, _persona_text = snapshot_blocks(
                fake_req,
                None,
                full_channel=self._flag("scan.full_channel", True),
                anchor_scan=self._flag("scan.anchor_scan", True),
                do_diff=False,  # 最下游没有基线，diff 无意义
            )
            snap.t = time.time()
            snap.umo = str(event.unified_msg_origin or "")
            # 标记这是最下游补审计（面板上区分「请求前」与「请求后」记录）
            record = snap.to_dict()
            record["stage"] = "agent_done"
            record["message_count"] = len(messages)
            # 截断超长消息全文，只保留条数与块摘要
            for block in record.get("blocks", []):
                if len(str(block.get("text", ""))) > 800:
                    block["text"] = str(block.get("text"))[:800] + "…"
            if self._flag("audit.enabled", True):
                self.store.push_history(snap.umo, record)
                if self._flag("debug_log", False):
                    logger.debug(
                        f"[注入管家] {snap.umo} 最下游补审计：{len(messages)} 条消息，"
                        f"块 {len(snap.blocks)} 个"
                    )
        except Exception as exc:  # noqa: BLE001 - 最下游钩子异常绝不能打断对话
            logger.warning(f"[注入管家] on_agent_done_final 出错：{exc}")

    # ------------------------------------------------------------------
    # LLM 工具：按部分召回人格档案
    # ------------------------------------------------------------------
    @llm_tool(name="recall_persona")
    async def tool_recall_persona(self, event: AstrMessageEvent, section: str = "") -> str:
        """取回你人格档案的某个部分（人格卡上列了有哪些部分）。

        需要贴近人设说话、回忆背景细节、拿不准该怎么表现时调用；
        一次只取需要的那部分，不要每次都把整份档案取回来。

        Args:
            section(string): 要取的部分：behavior/行为举止、background/背景故事、style/语言风格、relationship/关系立场；留空则返回整份档案
        """
        try:
            conversation = getattr(event, "conversation", None)
            persona_id = ""
            if conversation is not None:
                persona_id = str(getattr(conversation, "persona_id", "") or "")
            if not persona_id:
                return "（当前会话没有绑定具名人格。）"

            wanted = str(section or "").strip()
            body = await self.card_service.recall_section(
                self.context, persona_id, wanted, self.config
            )
            if body:
                if not wanted or wanted.lower() in {"all", "full", "summary"}:
                    return "你的完整人格档案如下：\n\n" + body
                # 用中文名回话：模型下一轮看到「人格档案·行为举止」比「behavior」更稳
                table = persona_section_keys(self.config)
                key = wanted.lower()
                if key not in table:
                    plain = wanted.strip("【】[]()（） ")
                    for candidate, (name, _hint) in table.items():
                        if plain == name:
                            key = candidate
                            break
                label = table[key][0] if key in table else wanted
                return f"人格档案·{label}：\n\n" + body

            # 还没生成过分区档案（人格卡模式没开或没生成过）→ 退回人格全文
            text = await self.card_service.find_persona_text(self.context, persona_id)
            if not text:
                return "（人格池里找不到这份人格。）"
            if not wanted:
                return "你的完整人格设定如下：\n\n" + text
            return "（还没为这份人格生成分区档案，先给你全文。）\n\n" + text
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[注入管家] recall_persona 失败：{exc}")
            return "（人格召回失败。）"

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def terminate(self) -> None:
        try:
            self.store.save()
        except Exception:  # noqa: BLE001
            pass
        logger.info("[注入管家] 已卸载")
