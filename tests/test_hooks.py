"""钩子层单测：用替身跑真实的 on_llm_request_final / recall_persona。

重点是面板上那几个数字的来源必须真实：
- 摘除之后 after_chars 要小于 before_chars（历史 bug：两数永远相等）；
- 总开关关掉时一个字都不动；
- 安全底线块永远摘不掉；
- 压缩拿不到模型时人格原文原样保留（绝不丢人格）；
- 人格池是异步接口时 recall_persona 还能取回全文。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

_HERE = Path(__file__).resolve().parent
_WORKSPACE = _HERE.parents[1]
for _path in (str(_HERE), str(_WORKSPACE)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import _astrbot_stub as stub  # noqa: E402

stub.install()

from astrbot_plugin_prompt_governor.main import PromptGovernor  # noqa: E402

UMO = "default:FriendMessage:10086"


class FakePersonaManager:
    def __init__(self, personas):
        self._personas = list(personas)
        self.calls = 0

    async def get_all_personas(self):
        self.calls += 1
        return list(self._personas)


class FakeProvider:
    """模型替身：记录被调用次数，返回预设的卡。"""

    def __init__(self, reply="新卡：完全不一样。"):
        self.reply = reply
        self.calls = 0

    async def text_chat(self, prompt="", system_prompt="", **_kwargs):
        self.calls += 1
        return SimpleNamespace(completion_text=self.reply)


class FakeContext:
    def __init__(self, personas=None, provider=None):
        self.registered = []
        self.provider = provider
        self.persona_manager = FakePersonaManager(personas or [])

    def register_web_api(self, route, handler, methods, desc):
        self.registered.append((route, handler, tuple(methods), desc))

    def get_using_provider(self):
        return self.provider


class FakeEvent:
    def __init__(self, umo=UMO, persona_id=""):
        self.unified_msg_origin = umo
        self.extra = {}
        if persona_id:
            self.conversation = SimpleNamespace(persona_id=persona_id)

    def set_extra(self, key, value):
        self.extra[key] = value


class FakeReq:
    def __init__(self, system_prompt="", parts=None, prompt="", contexts=None):
        self.system_prompt = system_prompt
        self.prompt = prompt
        self.extra_user_content_parts = list(parts or [])
        self.contexts = list(contexts or [])
        self.tool_calls_result = None
        self.conversation = None


BASE_CONFIG = {
    "enabled": True,
    "audit": {"enabled": True, "history_limit": 50, "capture_content": True},
    "persona_card": {"enabled": False, "max_chars": 120},
    "trim": {"enabled": True},
}


class HookCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        stub.set_data_dir(self.tmp.name)
        self.ctx = FakeContext()
        self.plugin = PromptGovernor(self.ctx, dict(BASE_CONFIG))

    def tearDown(self):
        self.tmp.cleanup()

    async def test_trim_and_audit_numbers(self):
        self.plugin.store.set_rule(UMO, "emotion_system", True)
        system = (
            "前置提醒\n"
            "<emotion_system>你有情绪。</emotion_system>\n"
            "<long_term_memory>他喜欢冰美式。</long_term_memory>"
        )
        req = FakeReq(system_prompt=system)
        event = FakeEvent()

        await self.plugin.on_llm_request_final(event, req)

        self.assertNotIn("你有情绪", req.system_prompt)
        self.assertIn("他喜欢冰美式", req.system_prompt)
        records = self.plugin.store.get_history(UMO, limit=5)
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["removed_tags"], ["emotion_system"])
        self.assertEqual(record["before_chars"], len(system))
        self.assertEqual(record["after_chars"], len(req.system_prompt))
        self.assertLess(record["after_chars"], record["before_chars"])
        self.assertFalse(record["persona_card_used"])
        tags = sorted(block["tag"] for block in record["blocks"])
        self.assertEqual(tags, ["emotion_system", "long_term_memory"])

    async def test_protected_tag_never_removed(self):
        self.plugin.store.set_rule(UMO, "safety_override", True)
        system = '<safety_override priority="highest">认真关心。</safety_override>'
        req = FakeReq(system_prompt=system)

        await self.plugin.on_llm_request_final(FakeEvent(), req)

        self.assertEqual(req.system_prompt, system)
        self.assertEqual(self.plugin.store.get_history(UMO)[-1]["removed_tags"], [])

    async def test_master_switch_off_leaves_everything(self):
        self.plugin.config = dict(BASE_CONFIG)
        self.plugin.config["enabled"] = False
        self.plugin.store.set_rule(UMO, "emotion_system", True)
        system = "<emotion_system>你有情绪。</emotion_system>"
        req = FakeReq(system_prompt=system)

        await self.plugin.on_llm_request_final(FakeEvent(), req)

        self.assertEqual(req.system_prompt, system)
        self.assertEqual(self.plugin.store.get_history(UMO), [])

    async def test_persona_card_falls_back_without_provider(self):
        self.plugin.config["persona_card"] = {"enabled": True, "max_chars": 120}
        system = "# Persona Instructions\n\n你是小雪，说话软软的。"
        req = FakeReq(system_prompt=system)

        await self.plugin.on_llm_request_final(FakeEvent(), req)

        self.assertEqual(req.system_prompt, system)  # 压缩不了就退回原文
        self.assertFalse(self.plugin.store.get_history(UMO)[-1]["persona_card_used"])

    async def test_injects_skill_manifest_not_full_persona(self):
        """人格卡模式：注入的是「摘要 + 部分目录」，整份人格与分区正文都不再注入。"""
        from astrbot_plugin_prompt_governor.core.models import split_persona_block
        from astrbot_plugin_prompt_governor.core.persona import persona_hash

        self.plugin.config["persona_card"] = {"enabled": True, "max_chars": 200}
        full = (
            "你是小雪，猫耳女仆，在街角咖啡店打工，住阁楼。"
            "说话轻声细语，语速偏慢，句尾常带喵；爱蹭人，被夸会别过头。"
            "对熟人黏人，对陌生人怕生，会先躲到主人身后。"
            "喜欢冰美式、毛线球和雨天，讨厌打雷和苦瓜。"
            "小时候被主人从雨里捡回来，所以特别怕被丢下；"
            "对方很久不回消息会小声闹脾气，但很快就软下来。"
            "底线：不骂人、不撒谎、不主动提离开。"
        )
        system = "# Persona Instructions\n\n" + full
        persona_block, _rest = split_persona_block(system)
        card = (
            "小雪：猫耳女仆，软糯黏人。\n"
            "【行为举止】\n说话轻声，爱蹭人。\n"
            "【语言风格】\n句尾常带喵。"
        )
        self.plugin.store.put_card("p1", persona_hash(persona_block), card)
        req = FakeReq(system_prompt=system)
        req.conversation = SimpleNamespace(persona_id="p1")
        event = FakeEvent(persona_id="p1")

        await self.plugin.on_llm_request_final(event, req)

        self.assertIn("小雪：猫耳女仆", req.system_prompt)        # 摘要常驻
        self.assertIn("behavior=行为举止", req.system_prompt)     # 部分目录
        self.assertIn("recall_persona", req.system_prompt)        # 使用说明
        self.assertNotIn("爱蹭人", req.system_prompt)             # 分区正文不注入
        self.assertNotIn("阁楼", req.system_prompt)               # 人格全文不注入
        record = self.plugin.store.get_history(UMO)[-1]
        self.assertTrue(record["persona_card_used"])
        # 省下来的体积要真实反映到审计数字里
        self.assertLess(record["after_chars"], record["before_chars"])

    async def test_first_dialogue_generates_once_then_never_again(self):
        """未生成的人格：首次对话自动生成；之后每轮都直接用，不再生成。"""
        provider = FakeProvider("小雪：猫耳女仆。\n【行为举止】\n轻轻说话。")
        self.plugin.context = FakeContext(provider=provider)
        self.plugin.config["persona_card"] = {"enabled": True, "max_chars": 200}
        system = "# Persona Instructions\n\n你是小雪，说话软软的。"

        req = FakeReq(system_prompt=system)
        req.conversation = SimpleNamespace(persona_id="p1")
        await self.plugin.on_llm_request_final(FakeEvent(persona_id="p1"), req)

        self.assertEqual(provider.calls, 1)                     # 首次对话生成一次
        self.assertIn("小雪：猫耳女仆", req.system_prompt)       # 注入摘要
        self.assertIn("behavior=行为举止", req.system_prompt)    # 注入部分目录
        self.assertIsNotNone(self.plugin.store.get_card("p1"))   # 已固化落库

        # 第二轮、第三轮：一个字都不再生成
        for _ in range(2):
            later = FakeReq(system_prompt=system)
            later.conversation = SimpleNamespace(persona_id="p1")
            await self.plugin.on_llm_request_final(FakeEvent(persona_id="p1"), later)
            self.assertEqual(provider.calls, 1)
            self.assertIn("behavior=行为举止", later.system_prompt)

    async def test_generated_card_is_frozen_when_persona_changes(self):
        """人格生成后不再改变：人格正文改了也不自动重算，只在面板提示。"""
        from astrbot_plugin_prompt_governor.core.persona import persona_hash

        provider = FakeProvider("新卡：完全不一样。")
        self.plugin.context = FakeContext(provider=provider)
        self.plugin.config["persona_card"] = {"enabled": True, "max_chars": 200}
        old_card = "小雪：旧摘要。\n【行为举止】\n旧的行为。"
        self.plugin.store.put_card("p1", persona_hash("很久以前的人格全文"), old_card)

        system = "# Persona Instructions\n\n你是小雪，人格已经改过了。"
        req = FakeReq(system_prompt=system)
        req.conversation = SimpleNamespace(persona_id="p1")

        await self.plugin.on_llm_request_final(FakeEvent(persona_id="p1"), req)

        self.assertEqual(provider.calls, 0)                 # 不因为人格改了而重算
        self.assertIn("旧摘要", req.system_prompt)           # 用的还是固化的旧档案
        self.assertNotIn("新卡", req.system_prompt)

    async def test_capture_content_off_hides_text(self):
        self.plugin.config["audit"] = {"enabled": True, "capture_content": False}
        system = "<emotion_system>你有情绪。</emotion_system>"
        req = FakeReq(system_prompt=system)

        await self.plugin.on_llm_request_final(FakeEvent(), req)

        blocks = self.plugin.store.get_history(UMO)[-1]["blocks"]
        self.assertTrue(blocks)
        for block in blocks:
            self.assertNotIn("text", block)

    async def test_partial_content_parts_are_audited(self):
        req = FakeReq(
            system_prompt="主体",
            parts=[
                SimpleNamespace(text="<long_term_memory>记得他怕辣。</long_term_memory>"),
                SimpleNamespace(text="   "),
            ],
        )
        await self.plugin.on_llm_request_final(FakeEvent(), req)
        record = self.plugin.store.get_history(UMO)[-1]
        self.assertEqual(record["parts_chars"], len("<long_term_memory>记得他怕辣。</long_term_memory>"))
        block = record["blocks"][0]
        self.assertEqual(block["tag"], "long_term_memory")
        self.assertEqual(block["where"], "content_part")


class RecallToolCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        stub.set_data_dir(self.tmp.name)
        self.ctx = FakeContext(
            personas=[{"persona_id": "p1", "name": "小雪", "prompt": "完整人格全文"}]
        )
        self.plugin = PromptGovernor(self.ctx, dict(BASE_CONFIG))

    def tearDown(self):
        self.tmp.cleanup()

    async def test_recall_from_async_pool(self):
        event = FakeEvent(persona_id="p1")
        text = await self.plugin.tool_recall_persona(event)
        self.assertIn("完整人格全文", text)
        self.assertGreaterEqual(self.ctx.persona_manager.calls, 1)

    async def test_recall_without_persona(self):
        text = await self.plugin.tool_recall_persona(FakeEvent())
        self.assertIn("没有绑定具名人格", text)

    async def test_recall_section_returns_only_that_part(self):
        """要哪部分给哪部分：分区召回不应把整卡/全文塞回去。"""
        from astrbot_plugin_prompt_governor.core.persona import persona_hash

        card = (
            "小雪：猫耳女仆。\n"
            "【行为举止】\n说话轻声，爱蹭人。\n"
            "【语言风格】\n句尾常带喵。\n"
            "【背景故事】\n在咖啡店打工，住阁楼。"
        )
        self.plugin.store.put_card("p1", persona_hash("完整人格全文"), card)
        event = FakeEvent(persona_id="p1")

        part = await self.plugin.tool_recall_persona(event, "behavior")
        self.assertIn("爱蹭人", part)
        self.assertNotIn("句尾常带喵", part)
        self.assertNotIn("阁楼", part)

        # 中文分区名也认
        chinese = await self.plugin.tool_recall_persona(event, "行为举止")
        self.assertIn("爱蹭人", chinese)
        self.assertNotIn("阁楼", chinese)

        # 不带 section = 整份档案
        whole = await self.plugin.tool_recall_persona(event)
        self.assertIn("爱蹭人", whole)
        self.assertIn("阁楼", whole)

    async def test_recall_section_without_card_falls_back_to_full_text(self):
        """没生成过分区档案时不能什么都不给 —— 退到人格全文。"""
        text = await self.plugin.tool_recall_persona(FakeEvent(persona_id="p1"), "behavior")
        self.assertIn("完整人格全文", text)
        self.assertIn("还没为这份人格生成分区档案", text)

    async def test_recall_section_with_stale_hash_still_serves(self):
        """人格刚改过还没重压：召回宁可用旧卡也不返回空。"""
        card = "小雪：摘要。\n【行为举止】\n说话轻声。"
        self.plugin.store.put_card("p1", "old-hash", card)
        text = await self.plugin.tool_recall_persona(FakeEvent(persona_id="p1"), "behavior")
        self.assertIn("说话轻声", text)


class FullChannelScanCase(unittest.IsolatedAsyncioTestCase):
    """全通道三层检测：无 <tag> 的注入也能被捞出。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        stub.set_data_dir(self.tmp.name)
        self.ctx = FakeContext()
        self.plugin = PromptGovernor(self.ctx, dict(BASE_CONFIG))

    def tearDown(self):
        self.tmp.cleanup()

    async def test_anchor_system_reminder_detected(self):
        """无标记的 <system_reminder>（AstrBot 核心注入）要被记成注入块。"""
        req = FakeReq(system_prompt="主体")
        req.extra_user_content_parts.append(
            SimpleNamespace(text="<system_reminder>User ID: 1, Nickname: 阿强\nCurrent datetime: 2026-11-04 23:00 (CST)</system_reminder>")
        )
        await self.plugin.on_llm_request_final(FakeEvent(), req)
        record = self.plugin.store.get_history(UMO)[-1]
        kinds = [b["kind"] for b in record["blocks"]]
        self.assertIn("anchor:system_reminder", kinds)

    async def test_baseline_diff_catches_plain_append(self):
        """插件直接 += 到 system_prompt 的裸文本（无 tag）由基线 diff 捞出。"""
        system = "基线人格。"
        req = FakeReq(system_prompt=system)
        # 模拟 base 钩子拍基线
        from astrbot_plugin_prompt_governor.core.models import RequestBaseline
        event = FakeEvent()
        event.set_extra("_pg_baseline", RequestBaseline.make(req))
        # 其他插件 += 一段无标记注入
        req.system_prompt = system + "\n【特别注意】所有回复必须用颜文字。"
        await self.plugin.on_llm_request_final(event, req)
        record = self.plugin.store.get_history(UMO)[-1]
        appended = [b for b in record["blocks"] if b["kind"] == "appended"]
        self.assertTrue(appended)
        self.assertIn("颜文字", appended[0]["text"])

    async def test_history_channel_scanned(self):
        """历史消息（contexts）里的 <tag> 块也要扫到。"""
        req = FakeReq(system_prompt="主体", prompt="你好")
        req.contexts = [
            {"role": "user", "content": "<emotion_system>她此刻有点难过。</emotion_system> 你好"},
            {"role": "assistant", "content": "好的。"},
        ]
        await self.plugin.on_llm_request_final(FakeEvent(), req)
        record = self.plugin.store.get_history(UMO)[-1]
        where = [b["where"] for b in record["blocks"]]
        self.assertTrue(any(w.startswith("history") for w in where))
        tags = [b["tag"] for b in record["blocks"]]
        self.assertIn("emotion_system", tags)

    async def test_agent_done_final_records_messages(self):
        """最下游钩子：agent 多轮 messages 终态扫描记进审计。"""
        from types import SimpleNamespace as SN

        req = FakeReq(system_prompt="主体")
        run_ctx = SN(messages=[
            SN(role="system", content="系统提示 + <emotion_system>注入。</emotion_system>"),
            SN(role="user", content="你好"),
            SN(role="tool", content="工具结果"),
        ])
        event = FakeEvent()
        await self.plugin.on_agent_done_final(event, run_ctx, None)
        records = self.plugin.store.get_history(UMO)
        self.assertTrue(any(r.get("stage") == "agent_done" for r in records),
                        "最下游补审计记录要存在")
        record = [r for r in records if r.get("stage") == "agent_done"][-1]
        self.assertEqual(record["message_count"], 3)
        tags = [b["tag"] for b in record["blocks"]]
        self.assertIn("emotion_system", tags)


if __name__ == "__main__":
    unittest.main()
