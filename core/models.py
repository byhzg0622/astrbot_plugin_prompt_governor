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
        )




def _iter_parts_text(req: Any) -> list[tuple[int, Any]]:
    """列出 extra_user_content_parts（与 audit.collect_text_parts 同口径）。"""
    parts = getattr(req, "extra_user_content_parts", None)
    if not parts:
        return []
    out: list[tuple[int, Any]] = []
    for index, part in enumerate(parts):
        text = getattr(part, "text", None)
        if text is None and isinstance(part, dict):
            text = part.get("text")
        if isinstance(text, str) and text.strip():
            out.append((index, text))
    return out


def _channel_texts(req: Any, full_channel: bool = True) -> list[tuple[str, str]]:
    """全通道取文本：[(通道名, 文本)]。

    只取真正会进 provider 的 5 个通道。full_channel=False 时退回旧版行为
    （只扫 system_prompt / prompt / 附加段）。历史消息（contexts）与工具
    结果（tool_calls_result）在全通道模式下也扫 —— 它们同样可能携带
    注入（被压缩后的指令、被改写的工具输出等）。
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
    # 历史消息（OpenAI 风格 dict 列表，content 可能是 str 或 list）
    for item in getattr(req, "contexts", None) or []:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "")
        content = item.get("content")
        if isinstance(content, str) and content.strip():
            out.append((f"{CHANNEL_HISTORY}:{role}", content))
        elif isinstance(content, list):
            for block in content:
                text = block.get("text") if isinstance(block, dict) else None
                if isinstance(text, str) and text.strip():
                    out.append((f"{CHANNEL_HISTORY}:{role}", text))
    # 工具调用结果
    tool_results = getattr(req, "tool_calls_result", None)
    if tool_results:
        for result in (tool_results if isinstance(tool_results, list) else [tool_results]):
            for call in getattr(result, "calls", None) or []:
                content = getattr(call, "content", None) or getattr(call, "text", None)
                if isinstance(content, str) and content.strip():
                    out.append((CHANNEL_TOOL_RESULT, content))
    return out


def _anchor_match(text: str) -> tuple[str, int] | None:
    """返回第一个命中的已知锚点 (kind, 起始下标)；没有则 None。"""
    for pattern, name in KNOWN_ANCHORS:
        match = pattern.search(text)
        if match:
            return name, match.start()
    return None


def scan_injection_blocks(
    req: Any,
    baseline: "RequestBaseline | None" = None,
    *,
    full_channel: bool = True,
    anchor_scan: bool = True,
    do_diff: bool = True,
) -> tuple[list["InjectBlock"], dict[str, Any]]:
    """全通道扫描一轮请求的注入块。

    返回 (块列表, 统计 dict)。每个块的来源：
    - kind=tag：带 <tag> 标记的注入块（旧版已有的强检测）；
    - kind=anchor：命中已知无标记锚点的一段（system_reminder / persona /
      kb_result / 等等）；
    - kind=appended：相对基线 diff 出的新增段 —— 没有任何标记、也不在锚点
      表里的裸文本注入（插件直接 += / EXTRA_PROMPT.md / prompt_prefix…）。
      这是旧版漏掉的那部分，现在靠最上游基线补回来。

    统计 dict 里有 system_chars/prompt_chars/parts_chars 以及各通道的
    命中数，面板直接用。
    """
    blocks: list[InjectBlock] = []
    stats: dict[str, int] = {}

    for where, text in _channel_texts(req):
        # 通道 1：强检测 <tag> 块
        blocks.extend(parse_tag_blocks(text, where))
        # 通道 2：已知无标记锚点
        hit = _anchor_match(text)
        if hit:
            kind, start = hit
            segment = text[start:]
            # 锚点段尽量取到下一个一级标题 / 段落边界，避免吞半篇
            next_heading = re.search(r"(?m)^#\s", segment)
            if next_heading:
                segment = segment[: next_heading.start()]
            blocks.append(
                InjectBlock(
                    kind=f"anchor:{kind}",
                    tag="",
                    owner="(anchor)",
                    text=segment,
                    chars=len(segment),
                    where=where,
                    removable=False,
                )
            )
        stats[f"{where}_chars"] = stats.get(f"{where}_chars", 0) + len(text)

    # 通道 3：基线 diff —— 找出相对最上游基线被追加的裸文本注入
    if baseline is not None:
        blocks.extend(_diff_appended(req, baseline))

    return blocks, stats


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
    """一段文本里没被 <tag> 块与已知锚点覆盖的剩余部分（去空白段）。"""
    residual = text
    for match in re.finditer(r"<[a-z][a-z0-9_-]{1,40}[^>]*>.*?</\1>", residual, re.DOTALL):
        residual = residual.replace(match.group(0), "", 1)
    for pattern, _name in KNOWN_ANCHORS:
        for match in pattern.finditer(residual):
            residual = residual.replace(match.group(0), "", 1)
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

    kind: str                 # tag / persona / other
    tag: str                  # tag 名（kind=tag 时有效，否则为空串）
    owner: str                # 归因到的插件名（尽力而为）
    text: str                 # 全文
    chars: int                # 字符数
    where: str                # system / content_part
    removable: bool           # 是否允许摘除
    protected: bool = False   # 安全底线，永不摘除


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
    persona_card_used: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "t": self.t,
            "umo": self.umo,
            "before_chars": self.before_chars,
            "after_chars": self.after_chars,
            "parts_chars": self.parts_chars,
            "removed_tags": self.removed_tags,
            "persona_card_used": self.persona_card_used,
            "blocks": [
                {
                    "kind": b.kind,
                    "tag": b.tag,
                    "owner": b.owner,
                    "chars": b.chars,
                    "where": b.where,
                    "removable": b.removable,
                    "protected": b.protected,
                    "text": b.text if b.chars <= 4000 else b.text[:4000] + "…",
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

