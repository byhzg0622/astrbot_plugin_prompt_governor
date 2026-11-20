"""注入管家（astrbot_plugin_prompt_governor）。

看清楚每一轮 LLM 请求里注入了什么，把静态人格变成可召回的人格卡，
按会话拦截不需要的注入块 —— 但绝不动别人的代码、不改 AstrBot 源码。

全量注入检测与拦截（v1.1.1）：
- 穷举分段：每个通道（system / prompt / 附加段 / 历史消息 / 工具结果）
  的全部文本都会切块列出（kind=section / appended），不再只认 <tag> 与
  固定锚点 —— 没有标记的核心 prompt、插件直接 += 的裸文本、被改写的
  历史消息、工具输出，在面板上都能逐段看到全文；
- 基线 diff：base 钩子（priority=+2000，先于所有其他插件）拍下全通道
  原文，final 钩子把「相对基线新增/改写」的段标成 origin=added，
  核心内容标 baseline，来源一眼可辨；
- 最下游补审计：agent 工具循环第 2 轮起不再经过 on_llm_request，这里在
  on_agent_done 读 run_context.messages（真正发给模型的消息）补记 tag /
  锚点 / tool 轮次内容；修复了 Message.content 是 list[ContentPart] 时
  整段漏检的历史 bug；
- 工具注入通道：func_tool 里每个工具的 name / description / parameters
  schema 也逐条审计（插件 add_llm_tools、@filter.llm_tool、MCP、子代理
  handoff 都算），归属到插件并标出「本轮新增/基线已有」——这类注入走
  provider 的 tools 字段，不经过 system_prompt，旧版完全看不到；
- 技能/工具说明标注：核心的 ## Skills 技能清单标成 kind=skills，
  TOOL_CALL_PROMPT 标成 kind=tool_prompt，不再混成普通段落；
- 拦截：面板上每一段注入都有开关 —— 带 <tag> 的按标记摘，无标记的按
  原文精确摘，工具注入按 tool:<name> 整体摘（核心工具只审计不可摘）；
  <safety_override> 这类安全底线永远不可摘。

钩子设计（star_handlers_registry 按 priority 降序执行）：
- on_llm_request_base   priority=+2000：**最先**执行，快照全通道基线
- （其他插件在这里做各自的注入）
- on_llm_request_final  priority=-2000：**最后**执行，穷举全通道解析、
  执行摘除、替换人格块为卡片
- on_agent_done         （agent 结束，最下游）：读 messages 终态补审计
"""

from __future__ import annotations

import re
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
    InjectBlock,
    RequestBaseline,
    TurnSnapshot,
    content_texts,
    normalize_text,
    persona_section_keys,
    render_persona_card_with_sections,
    split_persona_card,
)
from .core.persona import PersonaCardService
from .core.store import GovernorStore

PLUGIN_NAME = "astrbot_plugin_prompt_governor"
PLUGIN_VERSION = "1.1.2"

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
agent_begin_hook = _optional_hook("on_agent_begin")
agent_done_hook = _optional_hook("on_agent_done")

#: 钩子之后才由 AstrBot 核心注入、但模型确实会看到的内容（图片说明 / 系统通知）。
#: 这些在 on_llm_request 时还不存在，只能在 agent 开始/结束时从消息里捞。
_POST_HOOK_NOTICE_RE = re.compile(
    r"\[Image[^\]]*\]|<system_notice>.*?</system_notice>",
    re.IGNORECASE | re.DOTALL,
)

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
                list_untagged=self._flag("scan.list_untagged", True),
            )
            snap.t = time.time()
            snap.umo = umo

            # ---------- 1) 摘除（tag 块 + 无标记原文块） ----------
            if self._flag("trim.enabled", True):
                tags = self.store.tags_for(umo)
                text_blocks = [
                    rule["text"] for rule in self.store.text_rules_for(umo)
                ]
                snap.removed_tags, snap.removed_texts = apply_trim(
                    req, tags, text_blocks
                )

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
                record["stage"] = "final"
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
            self.context, provider, persona_id, persona_text,
            umo=event.unified_msg_origin
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
    # 钩子 3：agent 首轮开始 —— 记下本轮"开局已有"的消息
    # ------------------------------------------------------------------
    @agent_begin_hook(priority=-2000)
    async def on_agent_begin_snapshot(
        self, event: AstrMessageEvent, run_context: Any
    ) -> None:
        """AstrBot 4.29.4 的 on_agent_begin 在 reset 之后、首轮请求之前触发，
        此时 run_context.messages 已是「本轮开局」的完整消息（含 system 与
        历史）。把它记到 event.extra，供最下游钩子区分「历史旧消息」与
        「本轮工具轮次新产生的内容」，避免把整段历史重复记成注入。"""
        try:
            messages = getattr(run_context, "messages", None) or []
            texts: list[str] = []
            for message in messages:
                for text in content_texts(getattr(message, "content", None)):
                    normalized = normalize_text(text)
                    if normalized:
                        texts.append(normalized)
            if texts:
                event.set_extra("_pg_turn_start_texts", texts)

            # on_llm_request 之后核心还会补图片说明 / <system_notice>（见
            # AstrBot 4.29.4 internal.py → prepare_request_images）。这些内容
            # 在 final 快照里看不到，但模型确实会收到 —— 这里单独捞出来补审计。
            notices: list[str] = []
            for message in messages:
                for text in content_texts(getattr(message, "content", None)):
                    for match in _POST_HOOK_NOTICE_RE.finditer(text):
                        chunk = match.group(0).strip()
                        if chunk:
                            notices.append(chunk)
            if notices and self._flag("audit.enabled", True):
                snap = TurnSnapshot(t=time.time(), umo=str(event.unified_msg_origin or ""))
                snap.blocks = [
                    InjectBlock(
                        kind="post_hook",
                        tag="",
                        owner="(core)",
                        text=notice[:2000],
                        chars=len(notice),
                        where="post_hook",
                        removable=True,
                    )
                    for notice in notices[:40]
                ]
                record = snap.to_dict()
                record["stage"] = "agent_begin"
                record["message_count"] = len(messages)
                self.store.push_history(snap.umo, record)
        except Exception:  # noqa: BLE001 - 快照失败不影响对话
            pass

    # ------------------------------------------------------------------
    # 钩子 4：最下游 —— agent 结束（provider 已发出请求）终态补审计
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

            context_items: list[dict[str, str]] = []
            for message in messages:
                role = str(getattr(message, "role", "") or "")
                content = getattr(message, "content", None)
                texts = content_texts(content)
                if not texts:
                    continue
                # Message.content 可能是 str / list[ContentPart] / dict 列表；
                # 直接塞""进去会让整个 agent 轮次的注入都看不见（历史 bug）。
                context_items.append(
                    {
                        "role": role,
                        "content": "\n".join(texts),
                    }
                )
            fake_req = _types.SimpleNamespace(
                system_prompt="",
                prompt="",
                extra_user_content_parts=[],
                contexts=context_items,
                tool_calls_result=None,
                conversation=None,
            )
            snap, _persona_text = snapshot_blocks(
                fake_req,
                None,
                full_channel=self._flag("scan.full_channel", True),
                anchor_scan=self._flag("scan.anchor_scan", True),
                do_diff=False,  # 最下游没有基线，diff 无意义
                list_untagged=self._flag("scan.list_untagged", True),
            )
            snap.t = time.time()
            snap.umo = str(event.unified_msg_origin or "")
            # 本轮开局时已有的消息（on_agent_begin 拍下），用来排除历史：
            # 否则每一轮都会把整段历史里的 assistant / tool 消息重复列出来。
            start_texts: list[str] = []
            getter = getattr(event, "get_extra", None)
            if callable(getter):
                try:
                    start_texts = list(getter("_pg_turn_start_texts", []) or [])
                except Exception:  # noqa: BLE001
                    start_texts = []
            if not start_texts:
                extra = getattr(event, "extra", None)
                if isinstance(extra, dict):
                    start_texts = list(extra.get("_pg_turn_start_texts") or [])
            start_texts = [str(text) for text in start_texts if str(text)]

            def _is_old(block_text: str) -> bool:
                normalized = normalize_text(block_text)
                if not normalized:
                    return True
                return any(normalized in old for old in start_texts)

            kept = []
            for block in snap.blocks:
                role = ""
                if block.where.startswith("history:"):
                    role = block.where.split(":", 1)[1]
                if _is_old(block.text):
                    # 本轮开局就有的内容（历史 + 首轮请求本身）已在 final 记录里
                    continue
                if block.kind in ("tag", "persona") or block.kind.startswith("anchor"):
                    kept.append(block)
                    continue
                lowered = block.text.lower()
                # 压缩摘要、步数耗尽提示、图片说明等「钩子之后」才由核心注入、
                # 且 role=user 的一段内容，靠特征词捞回来（否则会漏掉）。
                if (
                    "[system notice]" in lowered
                    or "previous history conversation summary" in lowered
                    or "<system_notice>" in lowered
                    or "[image " in lowered
                    or "step budget exhausted" in lowered
                ):
                    kept.append(block)
                elif role in ("tool", "assistant"):
                    kept.append(block)
            if not kept:
                # 没有多轮新增内容就不写空记录（避免面板轮数/记录表被撑爆）
                return
            snap.blocks = kept[:200]
            # 标记这是最下游补审计（面板上区分「请求前」与「请求后」记录）
            record = snap.to_dict()
            record["stage"] = "agent_done"
            record["message_count"] = len(messages)
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
                self.context, persona_id, wanted, self.config,
                umo=event.unified_msg_origin
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
