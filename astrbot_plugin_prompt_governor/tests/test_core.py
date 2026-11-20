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
    content_texts,
    parse_tag_blocks,
    persona_section_keys,
    render_persona_card,
    render_persona_card_with_sections,
    segment_text,
    split_persona_block,
    split_persona_card,
    strip_tag_blocks,
    strip_text_blocks,
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


class TestExhaustiveScan(unittest.TestCase):
    """穷举扫描：任何一段注入都要有归属块，不能只认 tag / 锚点。"""

    def test_segment_covers_all_untagged_text(self):
        text = (
            "开头说明。\n\n"
            "# Tools\n可用工具 read_file。\n\n"
            "# Skills\n- 做咖啡。\n\n"
            "结尾裸文本【规则】必须礼貌。"
        )
        blocks = segment_text(text, "system")
        joined = "\n".join(b.text for b in blocks)
        for fragment in ("开头说明", "read_file", "做咖啡", "必须礼貌"):
            self.assertIn(fragment, joined)
        # 每块都带 where / chars / 可摘除
        self.assertTrue(all(b.where == "system" for b in blocks))
        self.assertTrue(all(b.chars == len(b.text) for b in blocks))
        self.assertTrue(any(b.kind == "section" for b in blocks))

    def test_tag_block_still_parsed_inside_scan(self):
        text = "前置\n<emotion_system>她很难过。</emotion_system>\n后置"
        blocks = segment_text(text, "system")
        tags = [b.tag for b in blocks if b.tag]
        self.assertEqual(tags, ["emotion_system"])

    def test_anchor_detected_without_tag(self):
        text = "<system_reminder>User ID: 1</system_reminder>\n后续说明"
        blocks = segment_text(text, "content_part")
        kinds = [b.kind for b in blocks]
        # 闭合标签按 tag 记；锚点表只作为无标签兜底（两种都算命中）
        self.assertTrue(
            "system_reminder" in [b.tag for b in blocks]
            or "anchor:system_reminder" in kinds
        )

    def test_strip_text_blocks(self):
        text = "核心。\n\n【注入】回复必须用颜文字。\n\n尾部。"
        new_text, removed = strip_text_blocks(text, ["【注入】回复必须用颜文字。"])
        self.assertEqual(removed, ["【注入】回复必须用颜文字。"])
        self.assertNotIn("颜文字", new_text)
        self.assertIn("核心", new_text)
        self.assertIn("尾部", new_text)
        # 不存在的块不会被误删
        _, removed2 = strip_text_blocks(text, ["不存在的注入"])
        self.assertEqual(removed2, [])

    def test_content_texts_handles_all_forms(self):
        class Part:
            def __init__(self, text=None, think=None):
                self.text = text
                self.think = think

        self.assertEqual(content_texts("abc"), ["abc"])
        self.assertEqual(content_texts([{"type": "text", "text": "a"}, {"type": "image_url", "image_url": {}}]), ["a"])
        self.assertEqual(content_texts([Part(text="b"), Part(think="c")]), ["b", "c"])
        self.assertEqual(content_texts(None), [])
        self.assertEqual(content_texts("   "), [])



class TestMergeAndOrder(unittest.TestCase):
    """注入块合并与顺序：同一插件同类注入并入并保持独立可勾选，且按请求顺序排列。"""

    def _block(self, kind, text, where="system", start=0, end=0, owner="(plugin_a)", tag=""):
        return InjectBlock(
            kind=kind, tag=tag, owner=owner, text=text, chars=len(text),
            where=where, removable=True, start=start, end=end,
        )

    def setUp(self):
        from core.models import InjectBlock
        self.InjectBlock = InjectBlock

    def test_small_block_merged_into_same_plugin_large_block(self):
        """同一插件的注入头(小块)并入紧随其后的身子(大块)，形成一块可勾选行。"""
        from core.models import _merge_blocks, InjectBlock
        head = InjectBlock(kind="section", tag="", owner="plugin_a", text="[注入A头]", chars=6, where="system", removable=True, start=0, end=5)
        body = InjectBlock(kind="section", tag="", owner="plugin_a", text="[注入A身子]这是一段较长的注入内容。", chars=20, where="system", removable=True, start=6, end=26)
        blocks = _merge_blocks([head, body])
        self.assertEqual(len(blocks), 1)
        self.assertTrue(blocks[0].merged_from_small)
        self.assertIn("[注入A头]", blocks[0].text)
        self.assertIn("[注入A身子]", blocks[0].text)

    def test_different_plugins_not_merged(self):
        """不同插件的注入不会并入同一块。"""
        from core.models import _merge_blocks, InjectBlock
        a = InjectBlock(kind="section", tag="", owner="plugin_a", text="[A]短", chars=4, where="system", removable=True, start=0, end=4)
        b = InjectBlock(kind="section", tag="", owner="plugin_b", text="[B]这是另一个插件的较长注入内容。", chars=20, where="system", removable=True, start=5, end=25)
        blocks = _merge_blocks([a, b])
        self.assertEqual(len(blocks), 2)
        self.assertFalse(blocks[0].merged_from_small)
        self.assertFalse(blocks[1].merged_from_small)

    def test_tool_blocks_not_merged_across_tools(self):
        """不同工具的注入不会并入。"""
        from core.models import _merge_blocks, InjectBlock
        t1 = InjectBlock(kind="tool", tag="tool:uapi_a", owner="uapipro", text="工具a描述短", chars=7, where="tool_schema", removable=True, start=0, end=7)
        t2 = InjectBlock(kind="tool", tag="tool:uapi_b", owner="uapipro", text="工具b描述这是一个较长的工具说明。", chars=20, where="tool_schema", removable=True, start=8, end=28)
        blocks = _merge_blocks([t1, t2])
        self.assertEqual(len(blocks), 2)

    def test_protected_kind_not_merged(self):
        """tag/persona/tool 受保护 kind 不会被当作小块并入。"""
        from core.models import _merge_blocks, InjectBlock
        small_tag = InjectBlock(kind="tag", tag="emotion_system", owner="ai_mind", text="短", chars=2, where="system", removable=True, start=0, end=2)
        body = InjectBlock(kind="section", tag="", owner="ai_mind", text="这是身子段落较长内容。", chars=20, where="system", removable=True, start=3, end=23)
        blocks = _merge_blocks([small_tag, body])
        self.assertEqual(len(blocks), 2)

    def test_seq_ordering_keeps_channel_order(self):
        """scan_injection_blocks 按通道优先级+偏移分配 seq，且 seq 单调递增。"""
        from core.models import scan_injection_blocks, RequestBaseline

        class Req:
            def __init__(self, system):
                self.system_prompt = system
                self.prompt = ""
                self.extra_user_content_parts = []
                self.contexts = []
                self.tool_calls_result = None
                self.conversation = None
                self.func_tool = None

        system = "核心人设。\n\n# Tools\n可用工具。\n\n[插件追加] 尾注内容。"
        req = Req(system)
        base = RequestBaseline.make(Req(system))
        blocks, _stats = scan_injection_blocks(req, base, full_channel=True, anchor_scan=True, do_diff=True, list_untagged=True)
        seqs = [b.seq for b in blocks]
        self.assertEqual(seqs, sorted(seqs), "seq 必须单调递增，保持请求里的先后顺序")
        self.assertEqual(len(seqs), len(set(seqs)), "seq 不能重复")

    def test_each_block_trimmable_independently(self):
        """每个块保留精确来源文本，勾选哪行只摘哪行，不影响其他块。"""
        from core.models import scan_injection_blocks, RequestBaseline
        from core.audit import apply_trim

        class Req:
            def __init__(self, system):
                self.system_prompt = system
                self.prompt = ""
                self.extra_user_content_parts = []
                self.contexts = []
                self.tool_calls_result = None
                self.conversation = None
                self.func_tool = None

        system = "核心人设。\n\n[注入A] 只摘A。\n\n[注入B] 只摘B。"
        req = Req(system)
        base = RequestBaseline.make(Req(system))
        blocks, _stats = scan_injection_blocks(req, base, full_channel=True, anchor_scan=True, do_diff=True, list_untagged=True)
        self.assertGreaterEqual(len(blocks), 2)
        # 只摘第一个块
        first_text = blocks[0].text
        req2 = Req(system)
        removed_tags, removed_texts = apply_trim(req2, set(), [first_text])
        self.assertIn(first_text.strip(), removed_texts)
        self.assertNotIn(first_text.strip(), req2.system_prompt)
        # 第二个块内容应保留
        second_text = blocks[1].text.strip()
        if second_text:
            self.assertIn(second_text, req2.system_prompt)

if __name__ == "__main__":
    unittest.main()

