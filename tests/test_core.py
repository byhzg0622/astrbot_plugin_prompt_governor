"""面板页启发性接口所需的 persona_card_enabled 补丁与 smoke 测试。

这里只放不依赖 astrbot 运行时的纯逻辑测试：
- TAG_BLOCK_RE 解析
- strip_tag_blocks 摘除
- split_persona_block 人格拆分
- GovernorStore 规则与缓存
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.models import (  # noqa: E402
    PROTECTED_TAGS,
    TAG_BLOCK_RE,
    build_card_prompt,
    parse_tag_blocks,
    persona_section_keys,
    render_persona_card,
    render_persona_card_with_sections,
    split_persona_block,
    split_persona_card,
    strip_tag_blocks,
)
from core.store import GovernorStore  # noqa: E402


class TestTagBlocks(unittest.TestCase):
    def test_parse(self):
        text = (
            "前置说明\n<emotion_system>你有情绪。</emotion_system>\n"
            "<long_term_memory>记得他喜欢冰美式。</long_term_memory>\n结尾"
        )
        blocks = parse_tag_blocks(text, "system")
        tags = [b.tag for b in blocks]
        self.assertEqual(tags, ["emotion_system", "long_term_memory"])
        self.assertTrue(all(b.removable for b in blocks))
        self.assertTrue(all(b.owner == "astrbot_plugin_ai_mind" for b in blocks))

    def test_protected(self):
        text = '<safety_override priority="highest">认真关心。</safety_override>'
        blocks = parse_tag_blocks(text, "system")
        self.assertEqual(len(blocks), 1)
        self.assertFalse(blocks[0].removable)
        self.assertTrue(blocks[0].protected)

    def test_strip(self):
        text = (
            "A\n<emotion_system>情绪块。</emotion_system>\n"
            "<long_term_memory>记忆块。</long_term_memory>\nB"
        )
        new_text, removed = strip_tag_blocks(text, {"emotion_system"})
        self.assertEqual(removed, ["emotion_system"])
        self.assertNotIn("情绪块", new_text)
        self.assertIn("记忆块", new_text)
        self.assertIn("A", new_text)
        self.assertIn("B", new_text)

    def test_strip_protected_ignored(self):
        text = "<safety_override>别摘我。</safety_override>"
        new_text, removed = strip_tag_blocks(text, {"safety_override"})
        self.assertEqual(removed, [])
        self.assertIn("别摘我", new_text)


class TestPersonaSplit(unittest.TestCase):
    def test_split(self):
        system = (
            "一些前置提醒。\n# Persona Instructions\n\n你是猫娘小雪。\n"
            "# Other Section\n别的内容"
        )
        persona, rest = split_persona_block(system)
        self.assertIn("猫娘小雪", persona)
        self.assertIn("Persona Instructions", persona)
        self.assertNotIn("猫娘小雪", rest)
        self.assertIn("前置提醒", rest)
        self.assertIn("Other Section", rest)

    def test_no_persona(self):
        persona, rest = split_persona_block("只有别的内容")
        self.assertEqual(persona, "")
        self.assertEqual(rest, "只有别的内容")


class TestStore(unittest.TestCase):
    def test_rules(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = GovernorStore(Path(tmp))
            store.set_rule("default:FriendMessage:123", "emotion_system", True)
            self.assertIn("emotion_system", store.tags_for("default:FriendMessage:123"))
            store.set_rule("*", "long_term_memory", True)
            self.assertEqual(
                store.tags_for("default:FriendMessage:123"),
                {"emotion_system", "long_term_memory"},
            )
            store.set_rule("default:FriendMessage:123", "emotion_system", False)
            self.assertNotIn(
                "emotion_system", store.tags_for("default:FriendMessage:123")
            )
            # 重新加载应保留
            store2 = GovernorStore(Path(tmp))
            self.assertIn("long_term_memory", store2.tags_for("anything"))

    def test_card_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = GovernorStore(Path(tmp))
            store.put_card("p1", "hash1", "卡片内容")
            self.assertEqual(store.get_card("p1"), "卡片内容")
            # 生成即固化：人格正文改了也不失效，只提示"已改"
            self.assertFalse(store.card_is_stale("p1", "hash1"))
            self.assertTrue(store.card_is_stale("p1", "hash2"))
            self.assertEqual(store.get_card("p1"), "卡片内容")
            self.assertIsNone(store.get_card("nobody"))
            # 固化的档案重启后仍然在
            store2 = GovernorStore(Path(tmp))
            self.assertEqual(store2.get_card("p1"), "卡片内容")

    def test_clear_session_rules_keeps_global(self):
        """历史 bug：清某个会话的规则会把全局规则一起清掉。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = GovernorStore(Path(tmp))
            store.set_rule("*", "emotion_system", True)
            store.set_rule("u1", "long_term_memory", True)
            store.clear_rules("u1")
            self.assertEqual(store.session_rules("u1"), [])
            self.assertEqual(store.global_rules(), ["emotion_system"])
            self.assertEqual(store.tags_for("u2"), {"emotion_system"})
            # 清全部才真的清干净
            store.clear_rules(None)
            self.assertEqual(store.trim_rules, {})

    def test_history_summary_and_clear(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = GovernorStore(Path(tmp))
            store.push_history("u1", {"t": 1.0, "umo": "u1", "before_chars": 10,
                                      "after_chars": 4, "parts_chars": 2,
                                      "removed_tags": ["emotion_system"],
                                      "persona_card_used": True, "blocks": [{}, {}]})
            store.push_history("u1", {"t": 3.0, "umo": "u1", "before_chars": 12,
                                      "after_chars": 6, "parts_chars": 0,
                                      "removed_tags": [], "persona_card_used": False,
                                      "blocks": [{}]})
            store.push_history("u2", {"t": 2.0, "umo": "u2", "blocks": []})
            summary = store.history_summary()
            self.assertEqual([row["umo"] for row in summary], ["u1", "u2"])
            top = summary[0]
            self.assertEqual(top["turns"], 2)
            self.assertEqual(top["before_chars"], 12)
            self.assertEqual(top["after_chars"], 6)
            self.assertEqual(top["parts_chars"], 0)
            self.assertEqual(top["blocks"], 1)
            self.assertFalse(top["persona_card_used"])
            self.assertEqual(store.history_umos(), ["u1", "u2"])
            self.assertEqual(store.clear_history("u1"), 2)
            self.assertEqual(store.history_umos(), ["u2"])
            self.assertEqual(store.clear_history(), 1)
            self.assertEqual(store.history_umos(), [])

    def test_cards_info(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = GovernorStore(Path(tmp))
            store.put_card("p1", "h", "十二个字的卡片内容啊啊")
            info = store.cards_info()
            self.assertEqual(info["p1"]["card"], "十二个字的卡片内容啊啊")
            self.assertEqual(info["p1"]["chars"], len("十二个字的卡片内容啊啊"))
            self.assertEqual(info["p1"]["persona_hash"], "h")
            self.assertGreater(info["p1"]["created_at"], 0)


class TestPersonaSections(unittest.TestCase):
    """人格卡 = 摘要 + 若干部分：摘要常驻注入，部分按需召回。"""

    CARD = (
        "小雪：猫耳女仆，软糯黏人、有点怕生。\n"
        "【行为举止】\n说话轻声，爱蹭人；被夸会别过头。\n"
        "【语言风格】\n句尾常带「喵」，句子很短。\n"
        "【背景故事】\n在咖啡店打工，住阁楼。"
    )

    def test_default_sections(self):
        keys = persona_section_keys(None)
        self.assertEqual(keys["behavior"][0], "行为举止")
        self.assertIn("background", keys)

    def test_sections_from_text_config(self):
        cfg = {"persona_card": {"sections": "behavior=行为举止:语气、口头禅\nlogic=思维方式"}}
        keys = persona_section_keys(cfg)
        self.assertEqual(set(keys), {"behavior", "logic"})
        self.assertEqual(keys["behavior"], ("行为举止", "语气、口头禅"))
        self.assertEqual(keys["logic"], ("思维方式", ""))

    def test_sections_from_dict_config(self):
        cfg = {"persona_card": {"sections": {"a": {"name": "甲", "hint": "乙"}}}}
        self.assertEqual(persona_section_keys(cfg), {"a": ("甲", "乙")})

    def test_sections_bad_config_falls_back(self):
        self.assertIn("behavior", persona_section_keys({"persona_card": {"sections": "乱写的"}}))

    def test_split_card(self):
        summary, parts = split_persona_card(self.CARD)
        self.assertEqual(summary, "小雪：猫耳女仆，软糯黏人、有点怕生。")
        self.assertEqual(set(parts), {"behavior", "style", "background"})
        self.assertIn("爱蹭人", parts["behavior"])
        self.assertIn("句尾常带", parts["style"])

    def test_heading_and_body_on_same_line(self):
        """模型经常把标题和正文写在同一行，两种写法都要切得出来。"""
        card = (
            "小雪：猫耳女仆。\n"
            "【行为举止】说话轻声，爱蹭人。\n"
            "【语言风格】句尾常带喵。\n"
            "【背景故事】\n在咖啡店打工，住阁楼。"
        )
        summary, parts = split_persona_card(card)
        self.assertEqual(summary, "小雪：猫耳女仆。")
        self.assertEqual(set(parts), {"behavior", "style", "background"})
        self.assertEqual(parts["behavior"], "说话轻声，爱蹭人。")
        self.assertEqual(parts["style"], "句尾常带喵。")
        self.assertEqual(parts["background"], "在咖啡店打工，住阁楼。")

    def test_alt_heading_forms(self):
        """## 标题 / 中文冒号 / [] 也算标题。"""
        card = (
            "摘要行\n"
            "## 行为举止\n轻轻说话。\n"
            "语言风格：句子很短。\n"
            "[背景故事] 住阁楼。"
        )
        summary, parts = split_persona_card(card)
        self.assertEqual(summary, "摘要行")
        self.assertEqual(parts.get("behavior"), "轻轻说话。")
        self.assertEqual(parts.get("style"), "句子很短。")
        self.assertEqual(parts.get("background"), "住阁楼。")

    def test_flat_card_is_all_summary(self):
        summary, parts = split_persona_card("扁平的旧卡：只有一行摘要")
        self.assertEqual(summary, "扁平的旧卡：只有一行摘要")
        self.assertEqual(parts, {})

    def test_manifest_only_carries_summary_and_directory(self):
        sections = persona_section_keys(None)
        summary, parts = split_persona_card(self.CARD, sections)
        block = render_persona_card_with_sections(summary, list(parts), sections)
        self.assertIn("<persona_card>", block)
        self.assertIn("小雪：猫耳女仆", block)          # 摘要常驻
        self.assertIn("behavior=行为举止", block)       # 目录里有这一部分
        self.assertIn("recall_persona", block)          # 告诉模型怎么取
        self.assertNotIn("爱蹭人", block)               # 分区正文不注入
        self.assertNotIn("阁楼", block)
        # 没有正文的分区不进目录，免得模型去召回空部分
        self.assertNotIn("relationship=", block)

    def test_empty_summary_renders_nothing(self):
        self.assertEqual(render_persona_card_with_sections("", ["behavior"], persona_section_keys(None)), "")

    def test_build_card_prompt_lists_sections(self):
        prompt = build_card_prompt("原始人格", 200)
        self.assertIn("【行为举止】", prompt)
        self.assertIn("200", prompt)
        self.assertIn("原始人格", prompt)


class TestRender(unittest.TestCase):
    def test_card_render(self):
        self.assertEqual(
            render_persona_card("要点一"),
            "<persona_card>\n要点一\n</persona_card>",
        )
        self.assertEqual(render_persona_card("  "), "")


if __name__ == "__main__":
    unittest.main()
