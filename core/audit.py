"""审计：对一轮 LLM 请求做注入明细解析。

base（早于其他插件钩子）记录人格与前缀的基线；
final（晚于其他插件钩子）解析全部注入块 + 执行摘除。
"""

from __future__ import annotations

from typing import Any

from .models import (
    InjectBlock,
    RequestBaseline,
    TurnSnapshot,
    parse_tag_blocks,
    scan_injection_blocks,
    split_persona_block,
    strip_tag_blocks,
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
    """列出 extra_user_content_parts 里的文本段 [(下标, 文本)]。"""
    parts = getattr(req, "extra_user_content_parts", None)
    if not parts:
        return []
    out: list[tuple[int, str]] = []
    for index, part in enumerate(parts):
        text = getattr(part, "text", None)
        if text is None and isinstance(part, dict):
            text = part.get("text")
        if isinstance(text, str) and text.strip():
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
) -> tuple[TurnSnapshot, str]:
    """解析当前 req 的**全部通道**注入块。返回 (快照, 人格原文)。

    检测三层（旧版只有第 1 层）：
    1. 强检测：<tag>...</tag> 标记块（system_prompt + 附加段 + 历史 + 工具结果）；
    2. 锚点：已知无标记注入（# Persona Instructions / <system_reminder> /
       <image_caption> / kb_result / 侧栏引文 等）；
    3. 基线 diff：相对最上游基线被追加的裸文本（kind=appended）——
       这是旧版漏掉的「无 tag 注入」（EXTRA_PROMPT.md、prompt_prefix、
       插件直接 += 的 system_prompt 段）。
    """
    system = getattr(req, "system_prompt", "") or ""
    persona_text, _rest = split_persona_block(system)

    snap = TurnSnapshot(t=0.0, umo="")
    snap.before_chars = len(system)

    blocks, _stats = scan_injection_blocks(
        req, baseline, full_channel=full_channel, anchor_scan=anchor_scan, do_diff=do_diff
    )
    # 去重：anchor:persona 与旧版 kind=persona 语义一致，统一成 persona
    for block in blocks:
        if block.kind == "anchor:persona":
            block.kind = "persona"
            block.owner = "(persona)"

    snap.parts_chars = sum(
        len(text) for _index, text in collect_text_parts(req)
    )
    snap.after_chars = len(system)
    snap.blocks = blocks
    return snap, persona_text


def apply_trim(req: Any, tags: set[str]) -> list[str]:
    """对 req 执行摘除：system_prompt + 各文本段都过一遍 strip。

    返回实际摘掉的 tag 列表（去重保序）。
    """
    if not tags:
        return []
    removed_all: list[str] = []

    system = getattr(req, "system_prompt", "") or ""
    if system:
        new_system, removed = strip_tag_blocks(system, tags)
        if removed:
            req.system_prompt = new_system
            removed_all.extend(removed)

    parts = getattr(req, "extra_user_content_parts", None)
    if parts:
        for part in parts:
            text = getattr(part, "text", None)
            if not isinstance(text, str) or not text:
                continue
            new_text, removed = strip_tag_blocks(text, tags)
            if removed:
                try:
                    part.text = new_text
                except Exception:  # noqa: BLE001 - 真改不动就跳过（如只读 part）
                    pass
                removed_all.extend(removed)

    # 去重保序
    seen: set[str] = set()
    return [t for t in removed_all if not (t in seen or seen.add(t))]
