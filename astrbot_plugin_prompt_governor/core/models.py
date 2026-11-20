"""数据模型与纯函数：全通道注入扫描、标记块解析、基线 diff、人格卡渲染。

这一层不 import astrbot —— 全部逻辑都可以独立单测。

全通道扫描（为什么旧版只检测到一部分）：
- 旧版只扫 system_prompt + extra_user_content_parts，而一轮 LLM 请求真正
  进 provider 的注入通道有 5 个：system_prompt / prompt(用户消息) /
  extra_user_content_parts / contexts(历史消息) / tool_calls_result。
- 旧版只认 <tag>...</tag> 形式，而注入不一定带标记：EXTRA_PROMPT.md、
  插件直接 += 到 system_prompt 的裸文本、prompt_prefix 等都没有 tag。
  现在补上「基线 diff」：凡是相对基线（本钩子链最上游快照）新增/追加的
  文本，即使没有任何标记也记为一个注入块（kind=appended）。
- 最下游检测：agent 工具循环的每一轮 LLM 请求都不再经过 on_llm_request
  钩子，main.py 里补了 on_agent_done 钩子，在 provider 真正发出请求之后
  读取 run_context.messages（最终发给模型的消息）做终态全通道扫描。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# 注入通道：一轮请求里所有会进 provider 的文本位置
# ---------------------------------------------------------------------------

#: 通道的稳定名称（面板 / 审计记录里的 where 字段）
CHANNEL_SYSTEM = "system"
CHANNEL_PROMPT = "prompt"
CHANNEL_PARTS = "content_part"
CHANNEL_HISTORY = "history"
CHANNEL_TOOL_RESULT = "tool_result"
CHANNEL_TOOL_SCHEMA = "tool_schema"

#: 各通道排序优先级（越小越先）：system / prompt / 附加段 / 历史 / 工具结果 /
#: 工具注入。面板按此优先级再按通道内偏移排序，保持注入在请求里的先后顺序。
CHANNEL_PRIORITY = {
    CHANNEL_SYSTEM: 0,
    CHANNEL_PROMPT: 1,
    CHANNEL_PARTS: 2,
    CHANNEL_HISTORY: 3,
    CHANNEL_TOOL_RESULT: 4,
    CHANNEL_TOOL_SCHEMA: 5,
}

#: 已知的「无标记注入锚点」：这些文本出现即视为一段注入，即使没有 <tag>
KNOWN_ANCHORS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"(?m)^#\s*Persona\s*Instructions\s*$", re.IGNORECASE), "persona"),
    (re.compile(r"<system_reminder>"), "system_reminder"),
    (re.compile(r"<image_caption>"), "image_caption"),
    (re.compile(r"<selected_excerpt>"), "selected_excerpt"),
    (re.compile(r"<Quoted Message>"), "quoted_message"),
    (re.compile(r"(?im)^(The user is asking in a side thread)"), "thread_excerpt"),
    (re.compile(r"(?im)^(\[Related Knowledge Base Results\])"), "kb_result"),
    (re.compile(r"(?im)^(You are a conversation title generator)"), "title_gen"),
    (re.compile(r"(?im)^(Current workspace:)"), "workspace_hint"),
    (re.compile(r"(?im)^(Local network policy)"), "network_policy"),
    # AstrBot 的核心技能注入：## Skills / ### Available skills / ### Skill rules
    (re.compile(r"(?m)^#{1,2}\s*Skills\s*$"), "skills"),
    # 工具调用说明：TOOL_CALL_PROMPT / TOOL_CALL_PROMPT_SKILLS_LIKE_MODE
    (re.compile(r"(?m)^When using tools:"), "tool_prompt"),
    (
        re.compile(r"(?m)^You MUST NOT return an empty response, especially after invoking a tool\."),
        "tool_prompt",
    ),
)



# ---------------------------------------------------------------------------
# 全通道扫描与基线 diff
# ---------------------------------------------------------------------------

@dataclass
class RequestBaseline:
    """一轮请求在钩子链最上游（其他插件注入之前）拍下的基线指纹。

    基线 diff 用它来识别「本钩子链里被追加进任意通道」的注入 —— 哪怕是
    没有任何 <tag> 标记的裸文本（EXTRA_PROMPT.md、prompt_prefix、
    插件直接 += 的 system_prompt 等）。

    指纹（sha1）判「有没有变」；prefix_text 是变更前的原文，用来
    diff 出「具体新增了哪段」。
    """

    system_sha1: str = ""
    prompt_sha1: str = ""
    parts_sha1: str = ""
    system_len: int = 0
    prompt_len: int = 0
    parts_len: int = 0
    #: 变更前的通道原文（diff 用；体积可控：只存 3 个通道的全文）
    system_prefix_text: str = ""
    prompt_prefix_text: str = ""
    parts_prefix_texts: tuple[str, ...] = ()
    #: 全通道基线原文（逐通道拼接）：origin 判定「这段是基线就有的核心内容，
    #: 还是本轮插件新注入的」用。历史消息与工具结果也记，才能发现插件改写
    #: 历史 / 塞工具结果的那类注入。
    channel_texts: tuple[tuple[str, str], ...] = ()
    contexts_text: str = ""
    tool_text: str = ""
    #: 本轮请求带上的工具 schema：[(工具名, 渲染文本 sha1)]。
    #: 模型看到的 tools 定义（名称/描述/参数）本身就是一条注入通道 ——
    #: uapipro 之类插件就是靠 add_llm_tools 往这里塞 20+ 个工具描述。
    tool_schemas: tuple[tuple[str, str], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "system_sha1": self.system_sha1,
            "prompt_sha1": self.prompt_sha1,
            "parts_sha1": self.parts_sha1,
            "system_len": self.system_len,
            "prompt_len": self.prompt_len,
            "parts_len": self.parts_len,
        }

    @staticmethod
    def digest(text: str) -> str:
        return hashlib.sha1((text or "").encode("utf-8")).hexdigest()

    @staticmethod
    def make(req: Any) -> "RequestBaseline":
        system = getattr(req, "system_prompt", "") or ""
        prompt = getattr(req, "prompt", "") or ""
        parts = [text for _index, text in _iter_parts_text(req)]
        parts_text = "".join(parts)

        grouped: dict[str, list[str]] = {}
        for where, text in _channel_texts(req):
            grouped.setdefault(where, []).append(text)
        channel_texts = tuple(
            (where, "\n".join(texts)) for where, texts in grouped.items()
        )
        contexts_text = "\n".join(
            text for where, text in channel_texts if where.startswith(CHANNEL_HISTORY)
        )
        tool_text = "\n".join(
            text for where, text in channel_texts
            if where.startswith(CHANNEL_TOOL_RESULT)
        )
        tool_schemas = tuple(
            (item["name"], RequestBaseline.digest(item["text"]))
            for item in iter_tool_schemas(req)
        )
        return RequestBaseline(
            system_sha1=RequestBaseline.digest(system),
            prompt_sha1=RequestBaseline.digest(prompt),
            parts_sha1=RequestBaseline.digest(parts_text),
            system_len=len(system),
            prompt_len=len(prompt),
            parts_len=len(parts_text),
            system_prefix_text=system,
            prompt_prefix_text=prompt,
            parts_prefix_texts=tuple(parts),
            channel_texts=channel_texts,
            contexts_text=contexts_text,
            tool_text=tool_text,
            tool_schemas=tool_schemas,
        )




def _part_text(part: Any) -> str:
    """从 ContentPart / dict / 任意带 text 的对象里取文本。

    真实 AstrBot 的 extra_user_content_parts 里是 pydantic 的 TextPart /
    ImageURLPart 等对象（只有 .text 属性），测试替身里则可能是 SimpleNamespace
    或 dict。这里把三种形态一起兼容。
    """
    if isinstance(part, str):
        return part
    if isinstance(part, dict):
        for key in ("text", "think"):
            value = part.get(key)
            if isinstance(value, str) and value.strip():
                return value
        return ""
    for attr in ("text", "think"):
        value = getattr(part, attr, None)
        if isinstance(value, str) and value.strip():
            return value
    dumped = getattr(part, "model_dump", None)
    if callable(dumped):
        try:
            data = dumped()
        except Exception:  # noqa: BLE001
            data = None
        if isinstance(data, dict):
            for key in ("text", "think"):
                value = data.get(key)
                if isinstance(value, str) and value.strip():
                    return value
    return ""


def content_texts(content: Any) -> list[str]:
    """把 Message.content 的各种形态摊平成文本列表。

    真实 AstrBot（4.x）的 Message.content 可能是：
    - str
    - list[ContentPart]（TextPart / ThinkPart / … pydantic 对象）
    - list[dict]（OpenAI 风格的 {"type": "text", "text": …}）
    - CheckpointData / None（内部检查点，不是注入，跳过）
    """
    if content is None:
        return []
    if isinstance(content, str):
        return [content] if content.strip() else []
    if isinstance(content, (list, tuple)):
        out: list[str] = []
        for item in content:
            text = _part_text(item)
            if text.strip():
                out.append(text)
        return out
    text = _part_text(content)
    return [text] if text.strip() else []


def _iter_parts_text(req: Any) -> list[tuple[int, Any]]:
    """列出 extra_user_content_parts（与 audit.collect_text_parts 同口径）。"""
    parts = getattr(req, "extra_user_content_parts", None)
    if not parts:
        return []
    out: list[tuple[int, Any]] = []
    for index, part in enumerate(parts):
        text = _part_text(part)
        if text.strip():
            out.append((index, text))
    return out


def _tool_result_texts(tool_results: Any) -> list[str]:
    """工具调用结果里的全部文本（兼容对象 / dict / OpenAI 风格消息列表）。"""
    texts: list[str] = []
    if not tool_results:
        return texts
    items = tool_results if isinstance(tool_results, (list, tuple)) else [tool_results]
    for result in items:
        messages: list[Any] = []
        to_openai = getattr(result, "to_openai_messages", None)
        if callable(to_openai):
            try:
                dumped = to_openai()
            except Exception:  # noqa: BLE001
                dumped = None
            if isinstance(dumped, list):
                messages = dumped
        if not messages and isinstance(result, dict):
            messages = [result]
        if not messages:
            segments = getattr(result, "tool_calls_result", None)
            if isinstance(segments, list):
                messages = segments
            elif segments is not None:
                messages = [segments]
        for message in messages:
            if isinstance(message, dict):
                content = message.get("content")
            else:
                content = getattr(message, "content", None)
            for text in content_texts(content):
                texts.append(text)
    return texts


def _channel_texts(req: Any, full_channel: bool = True) -> list[tuple[str, str]]:
    """全通道取文本：[(通道名, 文本)]。

    只取真正会进 provider 的 5 个通道。full_channel=False 时退回旧版行为
    （只扫 system_prompt / prompt / 附加段）。历史消息（contexts）与工具
    结果（tool_calls_result）在全通道模式下也扫 —— 它们同样可能携带
    注入（被压缩后的指令、被改写的工具输出等）。

    contexts 里的消息可能是 dict，也可能是真实 AstrBot 的 Message 对象
    （content 是 list[ContentPart]）；两种都摊平成文本。
    """
    out: list[tuple[str, str]] = []
    system = getattr(req, "system_prompt", "") or ""
    if system.strip():
        out.append((CHANNEL_SYSTEM, system))
    prompt = getattr(req, "prompt", "") or ""
    if prompt.strip():
        out.append((CHANNEL_PROMPT, prompt))
    for _index, text in _iter_parts_text(req):
        out.append((CHANNEL_PARTS, text))
    if not full_channel:
        return out
    for item in getattr(req, "contexts", None) or []:
        if isinstance(item, dict):
            role = str(item.get("role") or "")
            content = item.get("content")
        else:  # Message 对象（pydantic）
            role = str(getattr(item, "role", "") or "")
            content = getattr(item, "content", None)
        for text in content_texts(content):
            out.append((f"{CHANNEL_HISTORY}:{role or 'unknown'}", text))
    for text in _tool_result_texts(getattr(req, "tool_calls_result", None)):
        out.append((CHANNEL_TOOL_RESULT, text))
    return out


def channel_texts(req: Any, full_channel: bool = True) -> list[tuple[str, str]]:
    """公开的全通道取文本入口（审计 / 基线 / 测试共用同一口径）。"""
    return _channel_texts(req, full_channel=full_channel)


# ---------------------------------------------------------------------------
# 工具注入通道：func_tool 里的工具 schema 同样是发给模型的注入
#
# 为什么必须单独扫：AstrBot 的 tools 定义（名称 / 描述 / 参数 JSON Schema）
# 不走 system_prompt，而是 provider payload 里的 "tools" 字段。插件用
# context.add_llm_tools(...)（如 uapipro 注册 20+ 个工具）或 @filter.llm_tool
# 注册的工具，模型看到的全是这里 —— 不扫这一层就会漏掉整类「工具注入」。
# ---------------------------------------------------------------------------

#: 工具名 → 插件 的兜底归因（优先看 handler_module_path）
TOOL_OWNER_HINTS: tuple[tuple[str, str], ...] = (
    ("uapi_", "astrbot_plugin_uapipro_toolbox"),
    ("recall_long_term_memory", "astrbot_plugin_ai_mind"),
    ("memorize_long_term_memory", "astrbot_plugin_ai_mind"),
    ("get_full_dynamic_life_state", "astrbot_plugin_dynamic_life_state"),
)


def _tool_owner(tool: Any) -> str:
    """尽力归因一个工具属于哪个插件 / 核心 / MCP。"""
    module_path = str(getattr(tool, "handler_module_path", "") or "").strip()
    if module_path:
        marker = ".plugins."
        if marker in module_path:
            tail = module_path.split(marker, 1)[1]
            plugin_name = tail.split(".", 1)[0].strip()
            if plugin_name:
                return plugin_name
        if module_path.startswith("astrbot."):
            return "(core)"
        return module_path.split(".", 1)[0].strip() or "(tool)"
    name = str(getattr(tool, "name", "") or "")
    for prefix, owner in TOOL_OWNER_HINTS:
        if name.startswith(prefix):
            return owner
    if name.startswith("astrbot_"):
        return "(core)"
    return "(tool)"


def _tool_is_core(tool: Any) -> bool:
    name = str(getattr(tool, "name", "") or "")
    return name.startswith("astrbot_") or _tool_owner(tool) == "(core)"


def _render_tool_schema(tool: Any) -> str:
    """把工具 schema 渲染成面板可读文本（模型实际看到的就是这些）。"""
    name = str(getattr(tool, "name", "") or "").strip()
    description = str(getattr(tool, "description", "") or "").strip()
    parameters = getattr(tool, "parameters", None)
    try:
        parameters_text = json.dumps(parameters, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        parameters_text = str(parameters)
    return (
        f"工具：{name}\n"
        f"描述：{description or '（无描述）'}\n"
        f"参数：{parameters_text}"
    )


def iter_tool_schemas(req: Any) -> list[dict[str, Any]]:
    """列出本轮请求携带的工具 schema（只算 active=True 的，模型真的看得到）。"""
    tool_set = getattr(req, "func_tool", None)
    tools = getattr(tool_set, "tools", None) if tool_set is not None else None
    if not tools:
        return []
    out: list[dict[str, Any]] = []
    for tool in tools:
        if not getattr(tool, "active", True):
            continue
        name = str(getattr(tool, "name", "") or "").strip()
        if not name:
            continue
        out.append(
            {
                "name": name,
                "owner": _tool_owner(tool),
                "text": _render_tool_schema(tool),
                "removable": not _tool_is_core(tool),
            }
        )
    return out


def _scan_tool_schemas(
    req: Any,
    baseline: "RequestBaseline | None",
    *,
    do_diff: bool,
) -> list["InjectBlock"]:
    base_hashes = (
        dict(getattr(baseline, "tool_schemas", ()) or ()) if baseline is not None else {}
    )
    blocks: list["InjectBlock"] = []
    for item in iter_tool_schemas(req):
        block = InjectBlock(
            kind="tool",
            tag="tool:" + item["name"],
            owner=item["owner"],
            text=item["text"],
            chars=len(item["text"]),
            where=CHANNEL_TOOL_SCHEMA,
            removable=bool(item["removable"]),
        )
        if do_diff and baseline is not None:
            block.origin = (
                "baseline"
                if base_hashes.get(item["name"]) == RequestBaseline.digest(item["text"])
                else "added"
            )
        blocks.append(block)
    return blocks


def _anchor_match(text: str) -> tuple[str, int] | None:
    """返回第一个命中的已知锚点 (kind, 起始下标)；没有则 None。"""
    for pattern, name in KNOWN_ANCHORS:
        match = pattern.search(text)
        if match:
            return name, match.start()
    return None


#: 无标记裸文本段落的 kind（核心注入 / 基线内容 / 未归因文本）
SECTION_KIND = "section"
#: 单通道最多产出多少块；超出部分合并成一块（保证不漏，但别把面板撑爆）
CHANNEL_MAX_BLOCKS = 80
#: 小块阈值：字符数不超过此值且只有一行，视为“注入头/尾”的小块，会并入相邻大块
SMALL_BLOCK_MAX_CHARS = 60
#: 这些 kind 的块不参与小块并入（保持独立可勾选）
MERGE_PROTECTED_KINDS = frozenset({"tag", "persona", "tool"})

#: Markdown 标题行：核心注入用它分节（# Tools / # Persona Instructions …）
_HEADING_RE = re.compile(r"(?m)^#{1,6}\s")


def normalize_text(text: str) -> str:
    """归一化文本，用于判断「这段在基线里是否已经存在」。"""
    return re.sub(r"\s+", " ", (text or "")).strip()


def _split_chunk(chunk: str) -> list[tuple[int, int]]:
    """把一段文本切成段落级区间（标题行一定新起一块，不丢文本）。

    按空行切段；标题行与紧随的一段合并成一块（核心注入多为
    "# Tools\n…" 这种形态）。切得细一点的好处是「按原文拦截」能只摘
    掉真正想摘的那一段，而不是把整篇 system prompt 一起删掉。
    """
    if not chunk.strip():
        return []
    units: list[tuple[int, int]] = []
    position = 0
    for match in re.finditer(r"\r?\n[ \t]*\r?\n", chunk):
        if match.start() > position:
            units.append((position, match.start()))
        position = match.end()
    if position < len(chunk):
        units.append((position, len(chunk)))
    if not units:
        return []

    spans: list[tuple[int, int]] = []
    for start, end in units:
        # 超长段落再按行切一刀，避免单块几万字
        if end - start > 4000:
            cursor = start
            for match in re.finditer(r"\n", chunk[start:end]):
                cut = start + match.start()
                if cut - cursor >= 3000:
                    spans.append((cursor, cut))
                    cursor = start + match.end()
            if cursor < end:
                spans.append((cursor, end))
        else:
            spans.append((start, end))
    return spans


def segment_text(
    text: str,
    where: str,
    *,
    anchor_scan: bool = True,
    max_blocks: int = CHANNEL_MAX_BLOCKS,
) -> list["InjectBlock"]:
    """把一个通道的文本切成**覆盖全部内容**的注入块。

    旧版只产出「tag / anchor / diff 追加」三类块，剩下的裸文本（核心拼的
    system prompt、插件直接改写的 prompt、历史消息里的裸指令）在面板上根本
    看不到。这里改成穷举式分块：tag 块 → 锚点块 → 其余所有文本按标题/空行
    切段（kind=section）。于是任何一段注入都有归属块，字符数可核对覆盖。
    """
    if not text or not text.strip():
        return []

    spans: list[tuple[int, int, InjectBlock]] = []
    covered: list[tuple[int, int]] = []

    # 1) 强检测：<tag>...</tag> 标记块
    for match in TAG_BLOCK_RE.finditer(text):
        tag = match.group(1).lower()
        full = match.group(0)
        protected = tag in PROTECTED_TAGS
        spans.append(
            (
                match.start(),
                match.end(),
                InjectBlock(
                    kind="tag",
                    tag=tag,
                    owner=guess_owner(tag, full),
                    text=full,
                    chars=len(full),
                    where=where,
                    removable=not protected,
                    protected=protected,
                    start=match.start(),
                    end=match.end(),
                ),
            )
        )
        covered.append((match.start(), match.end()))

    # 2) 已知无标记锚点（system_reminder / persona / kb_result …）
    if anchor_scan:
        for pattern, name in KNOWN_ANCHORS:
            for match in pattern.finditer(text):
                start = match.start()
                if any(s <= start < e for s, e in covered):
                    continue
                rest = text[start:]
                after = match.end() - start
                if name == "skills":
                    # 技能块 = ## Skills 到下一个一级/二级标题（### 子标题属于技能块内部）
                    # 或到工具调用说明之前。否则会把后面的工具注入也吞进来。
                    candidates: list[int] = []
                    next_top = re.search(r"(?m)^#{1,2}\s", rest[after:])
                    if next_top:
                        candidates.append(start + after + next_top.start())
                    tool_cut = rest.find("When using tools:", after)
                    if tool_cut >= 0:
                        candidates.append(start + tool_cut)
                    end = min(candidates) if candidates else len(text)
                else:
                    next_heading = _HEADING_RE.search(rest, after)
                    if next_heading:
                        end = start + next_heading.start()
                    elif name == "persona":
                        end = len(text)
                    else:
                        para = re.search(r"\r?\n[ \t]*\r?\n", rest[after:])
                        end = start + after + para.start() if para else len(text)
                if end <= start:
                    end = min(len(text), match.end())
                segment = text[start:end].strip()
                if not segment:
                    continue
                covered.append((start, end))
                spans.append(
                    (
                        start,
                        end,
                        InjectBlock(
                            kind=f"anchor:{name}",
                            tag="",
                            owner="(anchor)",
                            text=segment,
                            chars=len(segment),
                            where=where,
                            removable=True,
                        ),
                    )
                )

    # 3) 其余全部文本：按标题/空行切段，保证覆盖（kind=section）
    covered.sort()
    merged: list[tuple[int, int]] = []
    for start, end in covered:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    gaps: list[tuple[int, int]] = []
    cursor = 0
    for start, end in merged:
        if start > cursor:
            gaps.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < len(text):
        gaps.append((cursor, len(text)))
    for gap_start, gap_end in gaps:
        chunk = text[gap_start:gap_end]
        if not chunk.strip():
            continue
        for rel_start, rel_end in _split_chunk(chunk):
            body = chunk[rel_start:rel_end].strip()
            if not body:
                continue
            kind = SECTION_KIND
            owner = "(untagged)"
            if body.startswith("When using tools:") or body.startswith(
                "You MUST NOT return an empty response, especially after invoking a tool."
            ):
                # AstrBot 核心的工具调用说明（TOOL_CALL_PROMPT）
                kind = "tool_prompt"
                owner = "(core)"
            spans.append(
                (
                    gap_start + rel_start,
                    gap_start + rel_end,
                    InjectBlock(
                        kind=kind,
                        tag="",
                        owner=owner,
                        text=body,
                        chars=len(body),
                        where=where,
                        removable=True,
                    ),
                )
            )

    spans.sort(key=lambda item: item[0])
    blocks = [block for _start, _end, block in spans]
    if len(blocks) > max_blocks:
        keep = blocks[: max_blocks - 1]
        tail = blocks[max_blocks - 1 :]
        merged_text = "\n".join(block.text for block in tail)
        if merged_text.strip():
            sample = tail[-1]
            keep.append(
                InjectBlock(
                    kind=SECTION_KIND,
                    tag="",
                    owner="(merged)",
                    text=merged_text,
                    chars=len(merged_text),
                    where=sample.where,
                    removable=True,
                    start=tail[0].start if tail else 0,
                    end=tail[-1].end if tail else 0,
                )
            )
        blocks = keep
    return blocks


def _baseline_where_text(baseline: "RequestBaseline | None", where: str) -> str:
    """基线里某个通道的原文（origin 判定用）。"""
    if baseline is None:
        return ""
    for cached_where, cached_text in getattr(baseline, "channel_texts", ()) or ():
        if cached_where == where:
            return cached_text
    if where.startswith(CHANNEL_HISTORY):
        return getattr(baseline, "contexts_text", "") or ""
    if where.startswith(CHANNEL_TOOL_RESULT):
        return getattr(baseline, "tool_text", "") or ""
    return ""


def scan_injection_blocks(
    req: Any,
    baseline: "RequestBaseline | None" = None,
    *,
    full_channel: bool = True,
    anchor_scan: bool = True,
    do_diff: bool = True,
    list_untagged: bool = True,
) -> tuple[list["InjectBlock"], dict[str, Any]]:
    """全通道扫描一轮请求的注入块（穷举式，保证不漏）。

    每个块的 kind：
    - tag：带 <tag> 标记的注入块（强检测）；
    - anchor:<name>：命中已知无标记锚点的一段；
    - section：其余所有文本，按标题/空行切段（核心 system prompt 也在这里，
      所以「所有注入」都能在面板上逐段看到，不再只显示带标记的块）；
    - appended：section 块里相对基线**新增**的部分（插件直接 += 的裸文本、
      EXTRA_PROMPT.md、prompt_prefix、改写历史消息…）。

    block.origin 标记该块在基线里是否已经存在（baseline / added / 空=未知）。
    """
    blocks: list[InjectBlock] = []
    stats: dict[str, int] = {}

    for where, text in _channel_texts(req, full_channel=full_channel):
        stats[f"{where}_chars"] = stats.get(f"{where}_chars", 0) + len(text)
        raw_base = _baseline_where_text(baseline, where) if do_diff else ""
        channel_blocks = _scan_channel_text(
            text,
            where,
            baseline_text=raw_base,
            anchor_scan=anchor_scan,
            do_diff=do_diff,
        )
        blocks.extend(channel_blocks)
        stats[f"{where}_block_chars"] = stats.get(f"{where}_block_chars", 0) + sum(
            block.chars for block in channel_blocks
        )

    # 工具注入通道：func_tool 里的 tools schema（插件工具 / MCP / 内置工具）
    tool_blocks = _scan_tool_schemas(req, baseline, do_diff=do_diff)
    if tool_blocks:
        blocks.extend(tool_blocks)
        stats[f"{CHANNEL_TOOL_SCHEMA}_chars"] = sum(
            block.chars for block in tool_blocks
        )
        stats[f"{CHANNEL_TOOL_SCHEMA}_block_chars"] = sum(
            block.chars for block in tool_blocks
        )

    if not list_untagged:
        # 关掉「穷举分段」时退回旧口径：只留 tag / 锚点 / 新增追加段。
        # （默认开启 —— 否则没有标记的注入会整段消失，就是本 bug 的根源。）
        blocks = [block for block in blocks if block.kind != SECTION_KIND]
    else:
        # prompt 里用户自己的原话、history 里的旧消息都属于"上下文"，
        # 不是本轮注入；基线已有的段不往面板上堆（新增/改写的段照样保留）。
        # system / 附加段 / 工具结果不在此列：核心 prompt 与系统提醒本身
        # 就是用户想看的注入。
        blocks = [
            block
            for block in blocks
            if not (
                block.kind == SECTION_KIND
                and block.origin == "baseline"
                and (block.where == CHANNEL_PROMPT or block.where.startswith(CHANNEL_HISTORY))
            )
        ]

    # 小块并入：把「注入头/尾」类的小块并入相邻大块，保持「注入头+身子」在一块
    # 传入该通道原文，供 _merge_blocks 精确切出合并后的原文
    channel_text = dict(_channel_texts(req, full_channel=full_channel)).get(where, "")
    blocks = _merge_blocks(blocks, channel_text)

    # 全局行序：按通道优先级 + 通道内字符偏移，保持注入在请求里的先后顺序
    # （面板按此排序显示；无位置信息的块，如工具 schema，放在对应通道之后）
    seq = 0
    for block in blocks:
        block.seq = seq
        seq += 1

    return blocks, stats


def _merge_blocks(
    blocks: list["InjectBlock"],
    channel_text: str = "",
) -> list["InjectBlock"]:
    """把「注入头/尾」类的小块并入相邻大块，保持「注入头+身子」在一块。

    channel_text：该通道的完整原文，用于按 start/end 偏移精确切出合并后的
    原文，确保「勾选哪行拦哪行」时的原文替换能精确匹配。

    策略：
    - 无标记的 section 块（owner="(untagged)"）各自独立，永远不互相并入；
    - 只有「小块」（单行、≤60 字符）才会并入相邻的同一插件同类大块；
    - 合并后的文本按原始偏移精确切出，保留原始空行等所有字符，以便
      apply_trim 做精确原文替换。
    """
    if not blocks:
        return blocks

    def _is_small(block: InjectBlock) -> bool:
        return (
            block.kind not in MERGE_PROTECTED_KINDS
            and block.chars <= SMALL_BLOCK_MAX_CHARS
            and chr(10) not in block.text
        )

    def _same_plugin_and_type(a: InjectBlock, b: InjectBlock) -> bool:
        """两块属于同一插件的同类注入时，才允许并入。"""
        if a.kind != b.kind:
            return False
        if str(a.owner or "") == "(untagged)" or str(b.owner or "") == "(untagged)":
            return False
        if a.kind == "tool":
            ta = str(a.tag or "")
            tb = str(b.tag or "")
            return ta == tb
        if a.tag or b.tag:
            return str(a.tag or "") == str(b.tag or "")
        return str(a.owner or "") == str(b.owner or "")

    merged: list[InjectBlock] = []
    i = 0
    while i < len(blocks):
        cur = blocks[i]

        # 先尝试把 cur 并入下一个大块
        if i + 1 < len(blocks) and _is_small(cur):
            nxt = blocks[i + 1]
            if nxt.kind not in MERGE_PROTECTED_KINDS and _same_plugin_and_type(cur, nxt):
                nxt.text = channel_text[cur.start:nxt.end] if channel_text else (
                    cur.text + chr(10) + nxt.text
                ).strip()
                nxt.chars = len(nxt.text)
                nxt.start = cur.start if cur.start is not None else nxt.start
                nxt.end = cur.end if cur.end is not None else nxt.end
                nxt.merged_from_small = True
                merged.append(nxt)
                i += 2
                continue

        # 再尝试把 cur 并入刚放入的前一个大块
        if merged and _is_small(cur):
            prev = merged[-1]
            if prev.kind not in MERGE_PROTECTED_KINDS and _same_plugin_and_type(cur, prev):
                prev.text = channel_text[prev.start:cur.end] if channel_text else (
                    prev.text + chr(10) + cur.text
                ).strip()
                prev.chars = len(prev.text)
                prev.start = prev.start if prev.start is not None else cur.start
                prev.end = cur.end if cur.end is not None else prev.end
                prev.merged_from_small = True
                i += 1
                continue

        merged.append(cur)
        i += 1
    return merged


def _mark_added(blocks: list[InjectBlock]) -> None:
    for block in blocks:
        block.origin = "added"
        if not block.tag and block.kind == SECTION_KIND:
            block.kind = "appended"
            block.owner = "(diff)"


def _scan_channel_text(
    text: str,
    where: str,
    *,
    baseline_text: str,
    anchor_scan: bool,
    do_diff: bool,
) -> list[InjectBlock]:
    """扫描一个通道，并把「相对基线新增的部分」单独切出来。

    难点：插件往往只是往 system_prompt **尾部追加**一段裸文本。如果直接
    对全文分段，最后一个核心小节（如 "# Skills"）会和追加文本连成一段，
    整段被误标成「新增」。这里先定位基线原文/文本块在通道里的位置：
    - 基线原文是前缀 / 中间一段时：拆成 [基线前][基线][基线后] 三段分别扫描，
      只有基线前后的部分才标 origin=added；
    - 基线原文完全找不到（被整体改写）：整段按普通扫描处理。
    """
    if not do_diff or not baseline_text:
        return segment_text(text, where, anchor_scan=anchor_scan)

    if normalize_text(text) == normalize_text(baseline_text):
        blocks = segment_text(text, where, anchor_scan=anchor_scan)
        for block in blocks:
            block.origin = "baseline"
        return blocks

    # 附加段 / 历史消息 / 工具结果都是"一条条"的，逐块判断是否在基线里
    # 出现过即可；不要做全文前缀切分，否则会把同通道的其他消息也当成
    # 新增内容重复列出来。
    if where not in (CHANNEL_SYSTEM, CHANNEL_PROMPT):
        blocks = segment_text(text, where, anchor_scan=anchor_scan)
        normalized_base = normalize_text(baseline_text)
        for block in blocks:
            normalized = normalize_text(block.text)
            block.origin = (
                "baseline" if (normalized and normalized in normalized_base) else "added"
            )
            if block.origin == "added" and not block.tag and block.kind == SECTION_KIND:
                block.kind = "appended"
                block.owner = "(diff)"
        return blocks

    index = text.find(baseline_text)
    if index < 0:
        # 基线不是原文子串（历史消息是逐条拼接的，可能是这种情况）：
        # 逐块判断是否在基线里出现过，找不到的就是新增。
        blocks = segment_text(text, where, anchor_scan=anchor_scan)
        normalized_base = normalize_text(baseline_text)
        for block in blocks:
            normalized = normalize_text(block.text)
            block.origin = (
                "baseline" if (normalized and normalized in normalized_base) else "added"
            )
            if block.origin == "added" and not block.tag and block.kind == SECTION_KIND:
                block.kind = "appended"
                block.owner = "(diff)"
        return blocks

    before = text[:index]
    base_part = text[index : index + len(baseline_text)]
    after = text[index + len(baseline_text) :]
    blocks: list[InjectBlock] = []
    if before.strip():
        before_blocks = segment_text(before, where, anchor_scan=anchor_scan)
        _mark_added(before_blocks)
        blocks.extend(before_blocks)
    for block in segment_text(base_part, where, anchor_scan=anchor_scan):
        block.origin = "baseline"
        blocks.append(block)
    if after.strip():
        after_blocks = segment_text(after, where, anchor_scan=anchor_scan)
        _mark_added(after_blocks)
        blocks.extend(after_blocks)
    return blocks


def _diff_appended(req: Any, baseline: RequestBaseline) -> list[InjectBlock]:
    """相对最上游基线，diff 出「被追加/新增」的注入段。

    旧版审计只认 <tag> 与固定锚点；凡是没有任何标记的裸文本注入
    （插件直接 \\+= 到 system_prompt、EXTRA_PROMPT.md、prompt_prefix 改写
    用户 prompt、附加段整体替换）都会漏检。这一层用 base 钩子（priority
    最高、先于其他插件执行）拍下的 RequestBaseline 做指纹对比：

    - system_prompt：前缀长度变长且新前缀 == 旧全文 → 纯追加（插件尾注），
      新增尾部记一块 kind=appended；被整体改写则整段记一块；
    - prompt：prompt_prefix 会改写用户原话，前缀指纹变化即记差异；
    - extra_user_content_parts：整体指纹变化（段数增减 / 段被替换）时，
      逐段与基线段对齐，新增/变化的段各记一块。

    每块都有 text，面板上可直接看全文。
    """
    out: list[InjectBlock] = []

    # 1) system_prompt
    system = getattr(req, "system_prompt", "") or ""
    if system != "" or baseline.system_len > 0:
        if baseline.system_sha1 and RequestBaseline.digest(system) == baseline.system_sha1:
            pass  # 没动
        elif baseline.system_len and system.startswith(baseline.system_prefix_text):
            tail = system[len(baseline.system_prefix_text):].strip()
            if tail:
                out.append(
                    InjectBlock(
                        kind="appended", tag="", owner="(diff)",
                        text=tail, chars=len(tail), where=CHANNEL_SYSTEM,
                        removable=False,
                    )
                )
        else:
            delta = _diff_region(baseline.system_prefix_text, system)
            if delta:
                out.append(
                    InjectBlock(
                        kind="appended", tag="", owner="(diff)",
                        text=delta, chars=len(delta), where=CHANNEL_SYSTEM,
                        removable=False,
                    )
                )

    # 2) prompt（prompt_prefix 改写）
    prompt = getattr(req, "prompt", "") or ""
    if baseline.prompt_sha1 and RequestBaseline.digest(prompt) != baseline.prompt_sha1:
        prefix = ""
        if baseline.prompt_len and prompt.startswith(baseline.prompt_prefix_text):
            prefix = baseline.prompt_prefix_text
        delta = _diff_region(prefix, prompt)
        if delta:
            out.append(
                InjectBlock(
                    kind="appended", tag="", owner="(diff)",
                    text=delta, chars=len(delta), where=CHANNEL_PROMPT,
                    removable=False,
                )
            )

    # 3) 附加段：基线没记录逐段指纹（只有整体指纹），整体变了就全量重扫
    if baseline.parts_sha1:
        current_parts_text = "".join(t for _i, t in _iter_parts_text(req))
        if RequestBaseline.digest(current_parts_text) != baseline.parts_sha1:
            # 只对「基线之后新加的段」做残差检测：整段指纹变了说明有段被
            # 增/改。旧段（基线里就有的）是用户消息与插件的正常内容，不是
            # 「新注入」；但新段的无标记部分必须记下来（保证不丢）。
            # 基线里没有逐段指纹，保守策略：残差非空且该段文本在基线全文
            # 里找不到 → 视为新段。
            base_parts = baseline.parts_prefix_texts
            for _index, text in _iter_parts_text(req):
                if text in base_parts:
                    continue  # 基线里已有，跳过
                residual = _uncovered_text(text)
                if residual:
                    out.append(
                        InjectBlock(
                            kind="appended", tag="", owner="(diff)",
                            text=residual, chars=len(residual),
                            where=CHANNEL_PARTS, removable=False,
                        )
                    )
    return out


def _diff_region(before: str, after: str) -> str:
    """两个文本的差异段：去掉公共前缀后的剩余（追加的主体）。"""
    if not after:
        return ""
    prefix_len = 0
    max_len = min(len(before), len(after))
    while prefix_len < max_len and before[prefix_len] == after[prefix_len]:
        prefix_len += 1
    return after[prefix_len:].strip()


def _uncovered_text(text: str) -> str:
    """一段文本里没被 <tag> 块与已知锚点覆盖的剩余部分（去空白段）。

    历史 bug：旧正则写的是 `<[a-z]…[^>]*>.*?</\1>`，但分组根本不存在，
    Python 会直接抛 re.error: invalid group reference —— 只要附加段指纹
    一变，整次 snapshot 就异常，审计记录直接丢失（面板上表现为「读不到
    注入」）。这里改成复用 TAG_BLOCK_RE，不再有反向引用问题。
    """
    residual = TAG_BLOCK_RE.sub("", text or "")
    for pattern, _name in KNOWN_ANCHORS:
        residual = pattern.sub("", residual)
    residual = re.sub(r"\n{3,}", "\n\n", residual).strip()
    return residual


#: 匹配 <tag ...> ... </tag>（tag 为常见注入块命名：小写字母/数字/下划线）
TAG_BLOCK_RE = re.compile(
    r"<([a-z][a-z0-9_-]{1,40})[^>]*>"      # 开标签（小写起头；避免把
    r"(.*?)"                                # HTML 标签与内容混进注入块）
    r"</\1>",                              # 同名闭标签
    re.DOTALL,
)


#: 这些标记块是安全底线（危机干预等），永远不允许摘除
PROTECTED_TAGS = frozenset({"safety_override"})

#: 常见注入块 → 插件名 的静态归因表（已知插件贡献的标记）
TAG_OWNER_MAP: dict[str, str] = {
    "emotion_system": "astrbot_plugin_ai_mind",
    "long_term_memory_rules": "astrbot_plugin_ai_mind",
    "long_term_memory": "astrbot_plugin_ai_mind",
    "debug_session": "astrbot_plugin_ai_mind",
    "safety_override": "astrbot_plugin_ai_mind",
    "persona_instructions": "(persona)",
}


@dataclass
class InjectBlock:
    """一个注入块：本次审计发现的一段相对独立的注入内容。"""

    kind: str                 # tag / persona / anchor:xxx / section / appended
    tag: str                  # tag 名（kind=tag 时有效，否则为空串）
    owner: str                # 归因到的插件名（尽力而为）
    text: str                 # 全文
    chars: int                # 字符数
    where: str                # system / prompt / content_part / history:role / tool_result
    removable: bool           # 是否允许摘除
    protected: bool = False   # 安全底线，永不摘除
    origin: str = ""          # baseline（基线已有）/ added（本轮新增）/ 空（未知）
    start: int = 0            # 该块在通道文本里的起始字符偏移（用于按行排序）
    end: int = 0              # 该块在通道文本里的结束字符偏移（用于按行排序）
    merged_from_small: bool = False  # True 时表示本块里已并入了相邻的小块，小块本身不再独立勾选


def guess_owner(tag: str, text: str) -> str:
    """尽力归因一个块是哪家插件的。先查静态表，再按内容特征猜。"""
    if tag in TAG_OWNER_MAP:
        return TAG_OWNER_MAP[tag]
    if "长期记忆" in text or "long_term_memory" in text.lower():
        return "astrbot_plugin_ai_mind"
    if "此刻情绪" in text or "心境" in text:
        return "astrbot_plugin_ai_mind"
    if "质检" in text:
        return "astrbot_plugin_humanize_guard"
    return "(unknown)"


def parse_tag_blocks(text: str, where: str) -> list[InjectBlock]:
    """把一段注入文本里所有 <tag>...</tag> 块解析出来。"""
    blocks: list[InjectBlock] = []
    for match in TAG_BLOCK_RE.finditer(text):
        tag = match.group(1).lower()
        full = match.group(0)
        protected = tag in PROTECTED_TAGS
        blocks.append(
            InjectBlock(
                kind="tag",
                tag=tag,
                owner=guess_owner(tag, full),
                text=full,
                chars=len(full),
                where=where,
                removable=not protected,
                protected=protected,
            )
        )
    return blocks


def strip_tag_blocks(text: str, tags: set[str]) -> tuple[str, list[str]]:
    """从文本里摘除指定 tag 的块。返回 (新文本, 实际摘掉的 tag 列表)。

    摘除后把连续三个以上空行压成两个，避免留下一堆空洞。
    """
    removed: list[str] = []
    if not tags or not text:
        return text, removed

    def _sub(match: re.Match) -> str:
        tag = match.group(1).lower()
        if tag in tags and tag not in PROTECTED_TAGS:
            removed.append(tag)
            return ""
        return match.group(0)

    new_text = TAG_BLOCK_RE.sub(_sub, text)
    new_text = re.sub("\n{3,}", "\n\n", new_text).strip()
    return new_text, removed


def strip_text_blocks(text: str, blocks: list[str]) -> tuple[str, list[str]]:
    """从文本里按**原文匹配**摘除任意块（不要求带 <tag> 标记）。

    返回 (新文本, 实际摘掉的块原文列表)。只做一次替换，避免误删重复段；
    摘除后压掉多余空行。
    """
    removed: list[str] = []
    if not text or not blocks:
        return text, removed
    new_text = text
    for block in blocks:
        block_text = str(block or "").strip()
        if not block_text:
            continue
        if block_text in new_text:
            new_text = new_text.replace(block_text, "", 1)
            removed.append(block_text)
    new_text = re.sub(r"\n{3,}", "\n\n", new_text).strip()
    return new_text, removed


def split_persona_block(system_prompt: str) -> tuple[str, str]:
    """把 system_prompt 拆成 (人格块, 其余)。

    AstrBot 核心拼人格的格式固定（astr_main_agent.py:557）：
        \n# Persona Instructions\n\n{prompt}\n
    按这个锚点拆，拆不动就返回 ("", 原文)。
    """
    anchor = "# Persona Instructions"
    idx = system_prompt.find(anchor)
    if idx < 0:
        return "", system_prompt
    start = idx
    body_start = system_prompt.find("\n", system_prompt.find("\n", idx) + 1)
    if body_start < 0:
        body_start = idx
    # 人格块结束于下一个一级标题（"\n# "）或文末
    nxt = system_prompt.find("\n# ", body_start + 1)
    end = nxt if nxt >= 0 else len(system_prompt)
    persona = system_prompt[start:end].strip()
    rest = (system_prompt[:start] + system_prompt[end:]).strip()
    return persona, rest


# ---------------------------------------------------------------------------
# 审计快照
# ---------------------------------------------------------------------------

#: 单块记录文本上限：超出只存前 N 字（面板标记「已截断」）
BLOCK_TEXT_LIMIT = 8000


@dataclass
class TurnSnapshot:
    """一轮 LLM 请求的审计快照。"""

    t: float                                  # 时间戳
    umo: str                                  # 会话
    before_chars: int = 0                     # 注入前 system_prompt 长度
    after_chars: int = 0                      # 注入后 system_prompt 长度
    parts_chars: int = 0                      # extra_user_content_parts 总字符
    blocks: list[InjectBlock] = field(default_factory=list)
    removed_tags: list[str] = field(default_factory=list)
    removed_texts: list[str] = field(default_factory=list)
    persona_card_used: bool = False
    #: 每通道覆盖统计：{where: {chars, block_chars, blocks}}
    coverage: dict[str, dict[str, int]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "t": self.t,
            "umo": self.umo,
            "before_chars": self.before_chars,
            "after_chars": self.after_chars,
            "parts_chars": self.parts_chars,
            "removed_tags": self.removed_tags,
            "removed_texts": self.removed_texts,
            "persona_card_used": self.persona_card_used,
            "coverage": self.coverage,
            "blocks": [
                {
                    "kind": b.kind,
                    "tag": b.tag,
                    "owner": b.owner,
                    "chars": b.chars,
                    "where": b.where,
                    "removable": b.removable,
                    "protected": b.protected,
                    "origin": b.origin,
                    "text": (
                        b.text if b.chars <= BLOCK_TEXT_LIMIT
                        else b.text[:BLOCK_TEXT_LIMIT] + "…"
                    ),
                    "truncated": b.chars > BLOCK_TEXT_LIMIT,
                }
                for b in self.blocks
            ],
        }


def estimate_tokens(chars: int) -> int:
    """粗估 token 数：中文约 1 字/token，英文约 4 字符/token，取折中。"""
    return max(1, chars // 2)


# ---------------------------------------------------------------------------
# 人格卡
# ---------------------------------------------------------------------------

CARD_HEADER = "<persona_card>"
CARD_FOOTER = "</persona_card>"


def render_persona_card(card_text: str) -> str:
    """把压缩好的人格卡文本渲染成注入块。"""
    body = (card_text or "").strip()
    if not body:
        return ""
    return f"{CARD_HEADER}\n{body}\n{CARD_FOOTER}"


def build_card_prompt(
    persona_text: str,
    max_chars: int,
    sections: dict[str, tuple[str, str]] | None = None,
) -> str:
    """构造「把人格整理成分区人格卡」的 LLM 提示词。

    产物格式固定成：第一行摘要 + 若干【分区名】段落。摘要常驻注入，
    分区按需召回 —— 所以摘要要能独立成立，分区要写得细。
    """
    table = sections or DEFAULT_SECTIONS
    lines = []
    for key, (name, hint) in table.items():
        lines.append(f"- 【{name}】（{key}）：{hint or '该部分的要点'}")
    section_block = "\n".join(lines)
    return (
        "把下面这份 AI 人格设定整理成一张分区人格卡，供每轮对话作为常驻提醒使用。\n"
        "输出格式（严格遵守）：\n"
        "第一行：一句话摘要（名字/称呼、身份、最核心的性格，不超过 40 字，不要标题）。\n"
        "后面：用【分区名】作为小标题分段，每段写成 1~3 行要点。\n"
        "可用分区（原文里确实有的才写，没有的整段省略，不要编造）：\n"
        f"{section_block}\n"
        "要求：\n"
        f"1. 全文不超过 {max_chars} 字，用第三人称写「她」。\n"
        "2. 摘要必须能独立看懂：模型只看摘要也知道自己是谁。\n"
        "3. 丢弃示例对话、可从性格自然推出的废话。\n"
        "4. 只输出卡片正文本身：不要解释、不要代码块、不要序号。\n\n"
        "----- 人格设定原文 -----\n"
        f"{persona_text}"
    )


def split_persona_card(
    card: str, sections: dict[str, tuple[str, str]] | None = None
) -> tuple[str, dict[str, str]]:
    """把渲染好的人格卡切回 (摘要, {分区key: 正文})。

    摘要是第一个【分区】之前的内容；没有分区标题时，整卡都是摘要、
    分区为空（召回时会退回整卡，不会丢人格）。
    """
    table = sections or DEFAULT_SECTIONS
    body = (card or "").strip()
    if not body:
        return "", {}
    parts = card_sections_text(body, table)
    if not parts:
        # 没有分区标题 → 整卡就是摘要（老格式的扁平卡也能用）
        return body, {}
    pattern = _section_heading_re(table)
    first = -1
    if pattern is not None:
        match = pattern.search(body)
        if match:
            first = match.start()
    summary = body[:first].strip() if first > 0 else ""
    if not summary:
        # 摘要缺位时用第一个分区兜底，避免注入空卡
        summary = next((v for v in parts.values() if v), "").strip()[:60]
    return summary, parts


# ---------------------------------------------------------------------------
# 人格卡分区：一张卡 = 摘要 + 若干可选部分（行为举止 / 背景故事 / 语言风格 / 关系立场）
#
# 常驻注入的只有摘要（~200 字）+ 可用部分列表；具体某部分由模型用
# recall_persona(section="behavior") 之类的调用按需取回。这样同一个人格
# 不再每轮把全文/整卡重复注入，而是「给目录，要哪部分取哪部分」。
# ---------------------------------------------------------------------------

#: 默认分区：key -> (中文名, 摘要里给模型的提示语)
DEFAULT_SECTIONS: dict[str, tuple[str, str]] = {
    "behavior": ("行为举止", "语气、口头禅、动作、表情包偏好等"),
    "background": ("背景故事", "身世、设定、过去经历"),
    "style": ("语言风格", "句式长短、用词习惯、禁用的说法"),
    "relationship": ("关系立场", "对用户/他人的称呼与态度边界"),
}


def persona_section_keys(config: Any = None) -> dict[str, tuple[str, str]]:
    """解析生效的分区表（可被插件配置 persona_card.sections 覆盖）。

    配置支持三种写法，解析失败就退回默认四区：
    - dict：{"behavior": "行为举止"} 或 {"behavior": {"name": "行为举止", "hint": "…"}}
    - 文本：每行 "behavior=行为举止" 或 "behavior=行为举止:语气与口头禅"
    """
    raw: Any = None
    if isinstance(config, dict) and isinstance(config.get("persona_card"), dict):
        raw = config["persona_card"].get("sections")

    out: dict[str, tuple[str, str]] = {}
    if isinstance(raw, dict) and raw:
        for key, value in raw.items():
            k = str(key).strip().lower()
            if not k:
                continue
            if isinstance(value, dict):
                name = str(value.get("name") or k)
                hint = str(value.get("hint") or "")
            else:
                name = str(value or k)
                hint = ""
            out[k] = (name, hint)
    elif isinstance(raw, str) and raw.strip():
        for line in raw.splitlines():
            text = line.strip().lstrip("-*").strip()
            if not text or "=" not in text:
                continue
            key, _, rest = text.partition("=")
            k = key.strip().lower()
            rest = rest.strip()
            if not k or not rest:
                continue
            name, _, hint = rest.partition(":")
            out[k] = (name.strip() or k, hint.strip())
    return out or dict(DEFAULT_SECTIONS)


def _section_heading_re(sections: dict[str, tuple[str, str]]):
    """分区标题的匹配器（长名优先，避免「语言风格」被「风格」抢先匹配）。"""
    import re

    names = [name for name, _hint in sections.values() if name]
    if not names:
        return None
    alt = "|".join(re.escape(name) for name in sorted(names, key=len, reverse=True))
    # 行首、可带 markdown 标题符号、可带【】或[]、标题后可紧跟正文
    return re.compile(r"^[#>\s]*[【\[]?\s*(" + alt + r")\s*[】\]]?\s*[:：]?[ \t]*", re.M)


def card_sections_text(card: str, sections: dict[str, tuple[str, str]]) -> dict[str, str]:
    """把整卡正文按分区拆成 {key: 文本}。

    模型两种写法都要认（实测两种都会出现）：
        【行为举止】
        说话轻声，爱蹭人。
    或  【行为举止】说话轻声，爱蹭人。

    切不出来的分区直接不返回；整卡切不动就返回空 dict，调用方会退到整卡。
    """
    out: dict[str, str] = {}
    text = (card or "").strip()
    pattern = _section_heading_re(sections)
    if not text or pattern is None:
        return out
    matches = list(pattern.finditer(text))
    if not matches:
        return out
    body_by_name: dict[str, str] = {}
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        chunk = text[start:end].strip()
        if chunk:
            body_by_name[match.group(1)] = chunk
    for key, (name, _hint) in sections.items():
        value = body_by_name.get(name, "")
        if value:
            out[key] = value
    return out


def render_persona_card_with_sections(
    summary: str,
    available: list[str],
    sections: dict[str, tuple[str, str]],
) -> str:
    """渲染注入的人格卡：摘要 + 一行可用部分目录（引导模型按 section 召回）。

    available 里是"这次确实有内容"的分区 key；没有正文的分区不进目录，
    免得模型去召回一个空部分。
    """
    body = (summary or "").strip()
    if not body:
        return ""
    lines = [body]
    if available:
        parts = []
        for key in available:
            name, hint = sections.get(key, (key, ""))
            label = name + (f"（{hint}）" if hint else "")
            parts.append(f"{key}={label}")
        lines.append("可选部分（要哪部分就用 recall_persona 的 section 取）：" + "；".join(parts))
    return render_persona_card("\n".join(lines))


# 分区召回的返回文本由 main.py 组装（要带会话语言的习惯用词），
# 这里只提供切分与渲染，不额外包一层。

