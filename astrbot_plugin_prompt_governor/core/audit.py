"""审计：对一轮 LLM 请求做注入明细解析。

base（早于其他插件钩子）记录全通道基线；
final（晚于其他插件钩子）穷举解析全部注入块 + 执行摘除。
"""

from __future__ import annotations

from typing import Any

from .models import (
    RequestBaseline,
    TurnSnapshot,
    _part_text,
    scan_injection_blocks,
    split_persona_block,
    strip_tag_blocks,
    strip_text_blocks,
)


def load_baseline(event: Any) -> RequestBaseline | None:
    """取 base 钩子挂上去的基线；没有（base 没跑 / 异常）就退回 None。

    真实 AstrMessageEvent 用 get_extra；测试替身（FakeEvent）把 extra 存
    在 event.extra 字典里 —— 两条路都试，取不到就 None。
    """
    baseline: Any = None
    getter = getattr(event, "get_extra", None)
    if callable(getter):
        try:
            baseline = getter("_pg_baseline", None)
        except Exception:  # noqa: BLE001
            baseline = None
    if baseline is None:
        extra = getattr(event, "extra", None)
        if isinstance(extra, dict):
            baseline = extra.get("_pg_baseline")
    return baseline if isinstance(baseline, RequestBaseline) else None


def collect_text_parts(req: Any) -> list[tuple[int, str]]:
    """列出 extra_user_content_parts 里的文本段 [(下标, 文本)]。

    真实对象是 pydantic ContentPart（.text / .think），测试替身可能是
    SimpleNamespace 或 dict —— 统一走 _part_text。
    """
    parts = getattr(req, "extra_user_content_parts", None)
    if not parts:
        return []
    out: list[tuple[int, str]] = []
    for index, part in enumerate(parts):
        text = _part_text(part)
        if text.strip():
            out.append((index, text))
    return out


def measure_injection(req: Any) -> tuple[int, int]:
    """量一次当前注入体积：返回 (system_prompt 字符数, 附加文本段总字符数)。

    摘除 / 人格卡替换之后必须重新量一次，否则面板上「注入前 → 注入后」
    两个数会永远相等，看着像没生效。
    """
    system = getattr(req, "system_prompt", "") or ""
    parts = sum(len(text) for _index, text in collect_text_parts(req))
    return len(system), parts


def snapshot_blocks(
    req: Any,
    baseline: RequestBaseline | None = None,
    *,
    full_channel: bool = True,
    anchor_scan: bool = True,
    do_diff: bool = True,
    list_untagged: bool = True,
) -> tuple[TurnSnapshot, str]:
    """解析当前 req 的**全部通道**注入块。返回 (快照, 人格原文)。

    检测三层（旧版只有第 1 层，且只扫 2 个通道）：
    1. 强检测：<tag>...</tag> 标记块（system / prompt / 附加段 / 历史 / 工具结果）；
    2. 锚点：已知无标记注入（# Persona Instructions / <system_reminder> /
       <image_caption> / kb_result / 侧栏引文 等）；
    3. 穷举分段：每个通道里剩下的全部文本按标题/空行切段，一段一块 ——
       没有 tag 也没有锚点的注入（插件直接 += 的 system prompt、
       EXTRA_PROMPT.md、prompt_prefix、被改写的历史消息、工具输出）
       也一定能看到；origin=added 标出相对基线「本轮新增」的部分。
    """
    system = getattr(req, "system_prompt", "") or ""
    persona_text, _rest = split_persona_block(system)

    snap = TurnSnapshot(t=0.0, umo="")
    snap.before_chars = len(system)

    blocks, stats = scan_injection_blocks(
        req,
        baseline,
        full_channel=full_channel,
        anchor_scan=anchor_scan,
        do_diff=do_diff,
        list_untagged=list_untagged,
    )
    # 去重/归一：persona / skills / tool_prompt 用专门的 kind，面板好识别
    for block in blocks:
        if block.kind == "anchor:persona":
            block.kind = "persona"
            block.owner = "(persona)"
        elif block.kind == "anchor:skills":
            block.kind = "skills"
            block.owner = "(skills)"
        elif block.kind == "anchor:tool_prompt":
            block.kind = "tool_prompt"
            block.owner = "(core)"

    snap.parts_chars = sum(len(text) for _index, text in collect_text_parts(req))
    snap.after_chars = len(system)
    snap.blocks = blocks

    # 覆盖统计：面板能核对「每通道 N 字符，切出了 M 块 / K 字符」
    coverage: dict[str, dict[str, int]] = {}
    for key, value in stats.items():
        if key.endswith("_chars") and not key.endswith("_block_chars"):
            where = key[: -len("_chars")]
            coverage.setdefault(where, {})["chars"] = value
    for block in blocks:
        entry = coverage.setdefault(block.where, {})
        entry["blocks"] = entry.get("blocks", 0) + 1
        entry["block_chars"] = entry.get("block_chars", 0) + block.chars
    snap.coverage = coverage
    return snap, persona_text


def apply_trim(
    req: Any,
    tags: set[str],
    text_blocks: list[str] | None = None,
) -> tuple[list[str], list[str]]:
    """对 req 执行摘除：system_prompt / prompt / 各文本段都过一遍。

    - tags：按 <tag> 摘除（旧行为，带安全底线保护）；
    - text_blocks：按**原文**摘除任意注入段（无 tag 的核心/插件注入也能拦）。

    返回 (实际摘掉的 tag 列表, 实际摘掉的文本块列表)，都去重保序。
    """
    texts = [str(block).strip() for block in (text_blocks or []) if str(block).strip()]
    if not tags and not texts:
        return [], []

    removed_all: list[str] = []
    removed_texts: list[str] = []

    def _trim_channel(original: str) -> tuple[str, list[str], list[str]]:
        updated = original
        removed_tag_list: list[str] = []
        removed_text_list: list[str] = []
        if tags:
            updated, removed_tag_list = strip_tag_blocks(updated, tags)
        if texts:
            updated, removed_text_list = strip_text_blocks(updated, texts)
        return updated, removed_tag_list, removed_text_list

    # 工具注入拦截：规则形如 "tool:<name>" —— 从 req.func_tool 里摘掉该工具。
    # 工具的 name/description/parameters 会作为 provider payload 的 tools 字段
    # 发给模型，属于独立注入通道（uapipro 等插件就是在这里塞工具说明）。
    tool_rules = sorted(
        {
            str(tag)[len("tool:") :].strip()
            for tag in (tags or set())
            if str(tag).lower().startswith("tool:")
        }
        - {""}
    )
    if tool_rules:
        tool_set = getattr(req, "func_tool", None)
        tools = getattr(tool_set, "tools", None) if tool_set is not None else None
        if tools:
            existing = [str(getattr(tool, "name", "") or "") for tool in tools]
            to_remove: list[str] = []
            for rule in tool_rules:
                match = ""
                if rule in existing:
                    match = rule
                else:
                    match = next(
                        (name for name in existing if name.lower() == rule.lower()), ""
                    )
                if match and match not in to_remove:
                    to_remove.append(match)
            remove_tool = getattr(tool_set, "remove_tool", None)
            for name in to_remove:
                if callable(remove_tool):
                    try:
                        remove_tool(name)
                        removed_all.append("tool:" + name)
                        continue
                    except Exception:  # noqa: BLE001
                        pass
                # 兜底：没有 remove_tool 的 ToolSet 替身 → 标记 inactive
                for tool in tools:
                    if str(getattr(tool, "name", "") or "") == name:
                        try:
                            tool.active = False
                            removed_all.append("tool:" + name)
                        except Exception:  # noqa: BLE001
                            pass
                        break

    system = getattr(req, "system_prompt", "") or ""
    if system:
        new_system, tag_list, text_list = _trim_channel(system)
        if new_system != system:
            req.system_prompt = new_system
            removed_all.extend(tag_list)
            removed_texts.extend(text_list)

    prompt = getattr(req, "prompt", "") or ""
    if prompt:
        new_prompt, tag_list, text_list = _trim_channel(prompt)
        if new_prompt != prompt:
            try:
                req.prompt = new_prompt
            except Exception:  # noqa: BLE001
                pass
            else:
                removed_all.extend(tag_list)
                removed_texts.extend(text_list)

    parts = getattr(req, "extra_user_content_parts", None)
    if parts:
        for part in parts:
            text = _part_text(part)
            if not text:
                continue
            new_text, tag_list, text_list = _trim_channel(text)
            if new_text == text:
                continue
            try:
                part.text = new_text
            except Exception:  # noqa: BLE001 - 真改不动就跳过（如只读 part）
                continue
            # 只在真的改成功后才记「已摘除」，避免面板显示已摘、实际没生效
            removed_all.extend(tag_list)
            removed_texts.extend(text_list)

    # 历史消息（contexts）里也可能有插件塞进来的 system/user 注入。只摘这两类
    # 消息的文本，assistant / tool 一律不动（动了会破坏工具调用链与对话结构）。
    contexts = getattr(req, "contexts", None)
    if isinstance(contexts, list) and contexts:
        for item in list(contexts):
            role = ""
            content: Any = None
            is_dict = isinstance(item, dict)
            if is_dict:
                role = str(item.get("role") or "")
                content = item.get("content")
            else:
                role = str(getattr(item, "role", "") or "")
                content = getattr(item, "content", None)
            if role not in ("system", "user"):
                continue

            if isinstance(content, str) and content:
                new_content, tag_list, text_list = _trim_channel(content)
                if new_content == content:
                    continue
                if is_dict:
                    item["content"] = new_content
                    if not new_content.strip():
                        try:
                            contexts.remove(item)
                        except ValueError:
                            pass
                else:
                    try:
                        item.content = new_content
                    except Exception:  # noqa: BLE001
                        continue
                removed_all.extend(tag_list)
                removed_texts.extend(text_list)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        text = block.get("text")
                        if not isinstance(text, str) or not text:
                            continue
                        new_text, tag_list, text_list = _trim_channel(text)
                        if new_text == text:
                            continue
                        block["text"] = new_text
                        removed_all.extend(tag_list)
                        removed_texts.extend(text_list)
                    else:
                        text = _part_text(block)
                        if not text:
                            continue
                        new_text, tag_list, text_list = _trim_channel(text)
                        if new_text == text:
                            continue
                        try:
                            block.text = new_text
                        except Exception:  # noqa: BLE001
                            continue
                        removed_all.extend(tag_list)
                        removed_texts.extend(text_list)

    seen: set[str] = set()
    tags_out = [t for t in removed_all if not (t in seen or seen.add(t))]
    seen_blocks: set[str] = set()
    texts_out = [t for t in removed_texts if not (t in seen_blocks or seen_blocks.add(t))]
    return tags_out, texts_out
