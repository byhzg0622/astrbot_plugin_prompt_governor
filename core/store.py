"""持久化：规则存储（摘除规则 / 人格卡缓存）+ 审计历史（内存 LRU）。

存储位置：AstrBot 插件数据目录（StarTools.get_data_dir），文件两个：
- rules.json     摘除规则 + 人格卡缓存（权威数据，重启保留）
- （审计历史只存内存，避免写放大；面板看最近 N 轮足够）
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from collections import OrderedDict, deque
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1


class GovernorStore:
    """rules.json 的读写 + 审计历史缓存。"""

    def __init__(self, data_dir: Path, history_limit: int = 200) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.data_dir / "rules.json"
        self.history_limit = max(20, int(history_limit))
        #: 摘除规则：umo -> set[tag]；umo="*" 表示全局
        self.trim_rules: dict[str, list[str]] = {}
        #: 人格卡缓存：persona_id -> {card, persona_hash, created_at}
        self.persona_cards: dict[str, dict[str, Any]] = {}
        #: 审计历史：umo -> deque[dict]（内存）
        self._history: OrderedDict[str, deque] = OrderedDict()
        self.load()

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------
    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self.trim_rules = raw.get("trim_rules", {}) or {}
            self.persona_cards = raw.get("persona_cards", {}) or {}
        except FileNotFoundError:
            pass
        except Exception as exc:  # noqa: BLE001
            import logging

            logging.getLogger("astrbot").warning(
                f"[注入管家] rules.json 读取失败，按空数据继续: {exc}"
            )

    def save(self) -> None:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "trim_rules": self.trim_rules,
            "persona_cards": self.persona_cards,
        }
        try:
            fd, tmp = tempfile.mkstemp(dir=str(self.data_dir), suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except Exception as exc:  # noqa: BLE001
            import logging

            logging.getLogger("astrbot").warning(
                f"[注入管家] rules.json 写入失败: {exc}"
            )

    # ------------------------------------------------------------------
    # 摘除规则
    # ------------------------------------------------------------------
    def tags_for(self, umo: str) -> set[str]:
        """某会话生效的摘除 tag 集 = 全局规则 ∪ 会话规则。"""
        tags: set[str] = set(self.trim_rules.get("*", []))
        tags.update(self.trim_rules.get(umo, []))
        return tags

    def set_rule(self, umo: str, tag: str, remove: bool) -> None:
        """设置/取消某会话（或 "*" 全局）对某 tag 的摘除。"""
        key = umo or "*"
        current = set(self.trim_rules.get(key, []))
        if remove:
            current.add(tag)
        else:
            current.discard(tag)
        if current:
            self.trim_rules[key] = sorted(current)
        else:
            self.trim_rules.pop(key, None)
        self.save()

    def clear_rules(self, umo: str | None = None) -> None:
        """清空规则。umo=None 清全部；否则只清这一个 key（"*" 即全局）。

        注意：清某个会话的规则时**不能**连全局一起清 —— 全局是别的会话
        共用的，误删会让所有会话的摘除一起失效（面板「清空本会话」踩过）。
        """
        if umo is None:
            self.trim_rules.clear()
        else:
            self.trim_rules.pop(umo or "*", None)
        self.save()

    # ------------------------------------------------------------------
    # 只读视图（面板用；不暴露内部结构，避免面板直接摸 _history）
    # ------------------------------------------------------------------
    def global_rules(self) -> list[str]:
        """全局（"*"）摘除 tag 列表。"""
        return sorted(self.trim_rules.get("*", []))

    def session_rules(self, umo: str) -> list[str]:
        """某会话自己的摘除 tag 列表（不含全局）。"""
        if not umo:
            return []
        return sorted(self.trim_rules.get(umo, []))

    def session_rule_map(self) -> dict[str, list[str]]:
        """所有会话级规则（不含全局）。"""
        return {
            key: sorted(value)
            for key, value in self.trim_rules.items()
            if key != "*" and value
        }

    def cards_info(self, config: Any = None) -> dict[str, dict[str, Any]]:
        """人格卡缓存的可读视图：persona_id -> {card, chars, created_at, persona_hash, sections}。"""
        from .models import persona_section_keys, split_persona_card

        sections = persona_section_keys(config)
        out: dict[str, dict[str, Any]] = {}
        for key, entry in self.persona_cards.items():
            if not isinstance(entry, dict):
                continue
            card = str(entry.get("card") or "")
            summary, parts = split_persona_card(card, sections)
            out[str(key)] = {
                "card": card,
                "chars": len(card),
                "created_at": float(entry.get("created_at") or 0.0),
                "persona_hash": str(entry.get("persona_hash") or ""),
                "summary": summary,
                "sectioned": bool(parts),
                # 卡片本身很小（≤两倍上限），面板要能展开看每个部分，直接给全文
                "sections": [
                    {
                        "key": k,
                        "name": sections[k][0],
                        "chars": len(v),
                        "preview": v[:80],
                        "text": v,
                    }
                    for k, v in parts.items()
                    if v
                ],
            }
        return out

    def history_umos(self) -> list[str]:
        """有审计记录的会话列表（最近活跃在前）。"""
        items = list(self._history.items())
        items.sort(key=lambda kv: kv[1][-1].get("t", 0) if kv[1] else 0, reverse=True)
        return [umo for umo, _bucket in items]

    def history_summary(self) -> list[dict[str, Any]]:
        """每个会话的一行摘要：轮数 + 最近一轮的体积/摘除/人格卡状态。"""
        out: list[dict[str, Any]] = []
        for umo, bucket in self._history.items():
            if not bucket:
                continue
            last = bucket[-1] or {}
            out.append(
                {
                    "umo": umo,
                    "turns": len(bucket),
                    "last_t": float(last.get("t") or 0.0),
                    "before_chars": int(last.get("before_chars") or 0),
                    "after_chars": int(last.get("after_chars") or 0),
                    "parts_chars": int(last.get("parts_chars") or 0),
                    "removed_tags": list(last.get("removed_tags") or []),
                    "persona_card_used": bool(last.get("persona_card_used")),
                    "blocks": len(last.get("blocks") or []),
                }
            )
        out.sort(key=lambda row: row["last_t"], reverse=True)
        return out

    def clear_history(self, umo: str | None = None) -> int:
        """清审计历史（内存）。umo=None 清全部。返回清掉的轮数。"""
        if umo is None:
            total = sum(len(bucket) for bucket in self._history.values())
            self._history.clear()
            return total
        bucket = self._history.pop(umo, None)
        return len(bucket) if bucket else 0

    # ------------------------------------------------------------------
    # 人格卡缓存
    # ------------------------------------------------------------------
    def get_card(self, persona_key: str) -> str | None:
        """取人格卡（**不看人格哈希**）。

        语义是「生成即固化」：不管是在面板里生成的，还是首轮对话自动生成的，
        一旦落库就不再自动改变 —— 人格正文后来改了也不重算，只在面板上标一句
        「人格已改」，要不要更新由用户点「重写人格卡」决定。
        """
        entry = self.persona_cards.get(persona_key)
        if not entry:
            return None
        card = str(entry.get("card") or "").strip()
        return card or None

    def card_is_stale(self, persona_key: str, persona_hash: str) -> bool:
        """档案是不是落后于当前人格正文（只用于面板提示，不触发重算）。"""
        entry = self.persona_cards.get(persona_key)
        if not entry or not persona_hash:
            return False
        recorded = str(entry.get("persona_hash") or "")
        return bool(recorded) and recorded != persona_hash

    def put_card(self, persona_key: str, persona_hash: str, card: str) -> None:
        """写入人格档案。persona_hash 只记录「生成时的人格是什么样」，用于面板提示。"""
        self.persona_cards[persona_key] = {
            "card": card.strip(),
            "persona_hash": persona_hash,
            "created_at": time.time(),
        }
        # 缓存上限：30 份人格卡足够，超出丢最旧的
        if len(self.persona_cards) > 30:
            oldest = sorted(
                self.persona_cards.items(),
                key=lambda kv: kv[1].get("created_at", 0),
            )
            for key, _ in oldest[: len(oldest) - 30]:
                self.persona_cards.pop(key, None)
        self.save()

    # ------------------------------------------------------------------
    # 审计历史（内存）
    # ------------------------------------------------------------------
    def push_history(self, umo: str, record: dict[str, Any]) -> None:
        bucket = self._history.get(umo)
        if bucket is None:
            bucket = deque(maxlen=self.history_limit)
            self._history[umo] = bucket
        bucket.append(record)
        # 防止会话数无限膨胀：只留最近 50 个会话的审计
        while len(self._history) > 50:
            self._history.popitem(last=False)

    def get_history(self, umo: str, limit: int = 20) -> list[dict[str, Any]]:
        bucket = self._history.get(umo)
        if not bucket:
            return []
        items = list(bucket)
        return items[-max(1, limit):]
