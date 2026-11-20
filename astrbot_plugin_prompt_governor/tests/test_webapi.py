"""面板后端接口单测：用 astrbot 替身跑真实的 WebApiMixin 实现。

覆盖历史踩坑：
- 清某个会话的规则不能连全局规则一起清；
- remove="false" 这种字符串必须解析成 False；
- 审计块跨轮聚合、保护块标记；
- 人格卡缓存重建；
- 会话插件开关读写 sp（原生 session_plugin_config 形状 {umo: {...}}）。
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

from astrbot_plugin_prompt_governor.api import webapi as webapi_mod  # noqa: E402
from astrbot_plugin_prompt_governor.main import PromptGovernor  # noqa: E402

SESSION_KEY = "session_plugin_config"


class FakePersonaManager:
    def __init__(self, personas):
        self._personas = list(personas)

    async def get_all_personas(self):
        return list(self._personas)


class FakeProvider:
    """模型替身：返回预先写好的分区人格卡。"""

    def __init__(self, reply: str):
        self.reply = reply
        self.calls = 0
        self.last_prompt = ""

    async def text_chat(self, prompt="", system_prompt="", **_kwargs):
        self.calls += 1
        self.last_prompt = prompt
        return SimpleNamespace(completion_text=self.reply)


class FakeContext:
    def __init__(self, provider=None, personas=None):
        self.registered = []
        self.provider = provider
        self.persona_manager = FakePersonaManager(
            personas
            if personas is not None
            else [
                {"persona_id": "p1", "name": "小雪", "prompt": "你是小雪。"},
                {"person_id": "p2", "personal_name": "阿岚", "system_prompt": "你是阿岚。"},
            ]
        )

    def register_web_api(self, route, handler, methods, desc):
        self.registered.append((route, handler, tuple(methods), desc))

    def get_using_provider(self):
        return self.provider


class FakeRequest:
    """web_request 替身：query + JSON body。"""

    def __init__(self, query=None, body=None):
        self.query = dict(query or {})
        self._body = body

    async def json(self):
        if self._body is None:
            raise ValueError("没有 JSON body")
        return self._body

    async def body(self):
        return b""


class WebApiCase(unittest.IsolatedAsyncioTestCase):
    config = {
        "enabled": True,
        "audit": {"enabled": True, "history_limit": 50, "capture_content": True},
        "persona_card": {"enabled": True, "max_chars": 120},
        "trim": {"enabled": True},
        "debug_log": False,
    }

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        stub.set_data_dir(self.tmp.name)
        self.ctx = FakeContext()
        stub.fake_sp().store.clear()
        self.plugin = PromptGovernor(self.ctx, dict(self.config))

    def tearDown(self):
        webapi_mod.web_request = None
        self.tmp.cleanup()

    async def call(self, handler, body=None, query=None):
        webapi_mod.web_request = FakeRequest(query=query, body=body)
        try:
            return await handler()
        finally:
            webapi_mod.web_request = None

    # ------------------------------------------------------------------
    async def test_routes_registered_with_plugin_prefix(self):
        routes = [route for route, _h, _m, _d in self.ctx.registered]
        self.assertIn("/astrbot_plugin_prompt_governor/overview", routes)
        self.assertIn("/astrbot_plugin_prompt_governor/<path:rest>", routes)
        for _route, _handler, methods, _desc in self.ctx.registered:
            self.assertEqual(methods, ("GET", "POST"))

    async def test_overview_shape(self):
        data = await self.call(self.plugin.api_overview)
        self.assertTrue(data["ok"])
        self.assertTrue(data["config"]["persona_card_enabled"])
        self.assertEqual(data["config"]["persona_card_max_chars"], 120)
        for key in ("sessions", "cards", "global_rules", "session_rules", "plugins"):
            self.assertIn(key, data["counts"])
        self.assertEqual(data["sessions"], [])
        self.assertIsNone(data["last_turn"])

    async def test_trim_scope_and_bool_parsing(self):
        # 全局规则
        res = await self.call(
            self.plugin.api_trim,
            body={"action": "set", "scope": "global", "tag": "emotion_system", "remove": True},
        )
        self.assertEqual(res["global_tags"], ["emotion_system"])
        # 会话规则
        res = await self.call(
            self.plugin.api_trim,
            body={"action": "set", "scope": "session", "umo": "u1", "tag": "long_term_memory", "remove": True},
        )
        self.assertEqual(res["session_tags"], ["long_term_memory"])
        self.assertEqual(res["effective_tags"], ["emotion_system", "long_term_memory"])
        # 字符串 "false" 必须解析成 False（bool("false") 会误判成真）
        res = await self.call(
            self.plugin.api_trim,
            body={"action": "set", "scope": "session", "umo": "u1", "tag": "long_term_memory", "remove": "false"},
        )
        self.assertEqual(res["session_tags"], [])
        # 清会话规则不能动全局规则
        res = await self.call(
            self.plugin.api_trim, body={"action": "clear", "scope": "session", "umo": "u1"}
        )
        self.assertEqual(res["session_tags"], [])
        self.assertEqual(res["global_tags"], ["emotion_system"])
        # get 视图
        res = await self.call(self.plugin.api_trim, query={"action": "get", "umo": "u1"})
        self.assertEqual(res["effective_tags"], ["emotion_system"])

    async def test_trim_requires_tag(self):
        res = await self.call(self.plugin.api_trim, body={"action": "set", "umo": "u1"})
        self.assertIn("error", res)
        self.assertEqual(res["status_code"], 400)

    async def test_blocks_aggregate_across_turns(self):
        self.plugin.store.set_rule("u1", "emotion_system", True)
        self.plugin.store.set_rule("*", "long_term_memory", True)
        self.plugin.store.push_history(
            "u1",
            {
                "t": 1.0,
                "umo": "u1",
                "before_chars": 100,
                "after_chars": 100,
                "parts_chars": 0,
                "removed_tags": [],
                "persona_card_used": False,
                "blocks": [
                    {
                        "kind": "tag",
                        "tag": "emotion_system",
                        "owner": "astrbot_plugin_ai_mind",
                        "chars": 10,
                        "where": "system",
                        "removable": True,
                        "protected": False,
                        "text": "A" * 10,
                    },
                    {
                        "kind": "tag",
                        "tag": "safety_override",
                        "owner": "astrbot_plugin_ai_mind",
                        "chars": 5,
                        "where": "system",
                        "removable": False,
                        "protected": True,
                        "text": "SAFE",
                    },
                ],
            },
        )
        self.plugin.store.push_history(
            "u1",
            {
                "t": 2.0,
                "umo": "u1",
                "before_chars": 120,
                "after_chars": 108,
                "parts_chars": 30,
                "removed_tags": ["emotion_system"],
                "persona_card_used": False,
                "blocks": [
                    {
                        "kind": "tag",
                        "tag": "emotion_system",
                        "owner": "astrbot_plugin_ai_mind",
                        "chars": 12,
                        "where": "system",
                        "removable": True,
                        "protected": False,
                        "text": "B" * 12,
                    }
                ],
            },
        )
        data = await self.call(self.plugin.api_blocks, body={"umo": "u1", "window": 20})
        self.assertEqual(data["turns"], 2)
        # 保护块排在前面，其余按字符数降序
        self.assertEqual(data["blocks"][0]["tag"], "safety_override")
        by_tag = {b["tag"]: b for b in data["blocks"]}
        emotion = by_tag["emotion_system"]
        self.assertEqual(emotion["seen_turns"], 2)
        self.assertEqual(emotion["chars"], 12)
        self.assertEqual(emotion["text"], "B" * 12)
        self.assertTrue(emotion["session_removed"])
        self.assertTrue(emotion["effective_removed"])
        safe = by_tag["safety_override"]
        self.assertTrue(safe["protected"])
        self.assertFalse(safe["removable"])
        self.assertFalse(safe["session_removed"])
        self.assertEqual(data["global_tags"], ["long_term_memory"])

    async def test_blocks_do_not_collapse_untagged_injections(self):
        """历史 bug：所有无 tag 块按 kind 挤成一行，面板只显示最后一段。"""
        def rec(t, blocks):
            return {
                "t": t, "umo": "u1", "before_chars": 0, "after_chars": 0,
                "parts_chars": 0, "removed_tags": [], "persona_card_used": False,
                "blocks": blocks,
            }

        def block(text, kind="appended"):
            return {
                "kind": kind, "tag": "", "owner": "(diff)", "chars": len(text),
                "where": "system", "removable": True, "protected": False,
                "origin": "added", "text": text,
            }

        self.plugin.store.push_history("u1", rec(1.0, [
            block("【注入】回复必须用颜文字。"),
            block("【注入】不许说脏话。"),
            block("核心人设说明。", kind="section"),
        ]))
        self.plugin.store.push_history("u1", rec(2.0, [
            block("【注入】回复必须用颜文字。"),
        ]))
        data = await self.call(self.plugin.api_blocks, body={"umo": "u1"})
        keys = [b["key"] for b in data["blocks"]]
        self.assertEqual(len(keys), len(set(keys)), "无 tag 块不能互相覆盖")
        self.assertEqual(len(data["blocks"]), 3)
        repeated = next(b for b in data["blocks"] if "颜文字" in b["text"])
        self.assertEqual(repeated["seen_turns"], 2)
        self.assertEqual(repeated["origin"], "added")
        self.assertEqual(repeated["trim_kind"], "text")

    async def test_trim_untagged_block_by_text(self):
        import hashlib

        body_text = "【注入】回复必须用颜文字。"
        key = hashlib.sha1(body_text.encode("utf-8")).hexdigest()[:12]
        self.plugin.store.push_history("u1", {
            "t": 1.0, "umo": "u1", "before_chars": 0, "after_chars": 0,
            "parts_chars": 0, "removed_tags": [], "persona_card_used": False,
            "blocks": [{
                "kind": "appended", "tag": "", "owner": "(diff)",
                "chars": len(body_text), "where": "system",
                "removable": True, "protected": False, "origin": "added",
                "text": body_text,
            }],
        })
        res = await self.call(
            self.plugin.api_trim,
            body={
                "action": "set", "scope": "session", "umo": "u1",
                "key": key, "text": body_text, "label": "颜文字",
            },
        )
        self.assertTrue(res["ok"])
        self.assertEqual(res["session_text_rules"][0]["key"], key)
        data = await self.call(self.plugin.api_blocks, body={"umo": "u1"})
        self.assertTrue(data["blocks"][0]["session_removed"])

        # 清本会话：原文规则跟着一起清，但不影响全局
        res = await self.call(self.plugin.api_trim, body={"action": "clear", "scope": "session", "umo": "u1"})
        self.assertEqual(res["session_text_rules"], [])
        # 缺 tag 且缺 key+text 时要报错
        res = await self.call(self.plugin.api_trim, body={"action": "set", "umo": "u1"})
        self.assertIn("error", res)

    async def test_summary_ignores_agent_done_zero_metrics(self):
        """历史 bug：KPI 取到 agent_done 的 0→0，看起来像没读到注入。"""
        self.plugin.store.push_history("u1", {
            "t": 1.0, "umo": "u1", "stage": "final",
            "before_chars": 1500, "after_chars": 1140, "parts_chars": 240,
            "removed_tags": ["emotion_system"], "blocks": [{"tag": "emotion_system"}],
        })
        self.plugin.store.push_history("u1", {
            "t": 2.0, "umo": "u1", "stage": "agent_done",
            "before_chars": 0, "after_chars": 0, "parts_chars": 0,
            "removed_tags": [], "blocks": [],
        })
        row = self.plugin.store.history_summary()[0]
        self.assertEqual(row["turns"], 1)          # agent_done 不算一轮
        self.assertEqual(row["records"], 2)
        self.assertEqual(row["before_chars"], 1500)
        self.assertEqual(row["after_chars"], 1140)
        self.assertEqual(row["blocks"], 1)
        data = await self.call(self.plugin.api_overview, body={"umo": "u1"})
        self.assertEqual(data["last_turn"]["stage"], "final")
        self.assertEqual(data["last_turn"]["before_chars"], 1500)

    async def test_blocks_separate_channels_and_mark_tool_blocks(self):
        """同 tag 不同通道要分行；工具/历史回复块只审计、不给拦截开关。"""
        def block(**kw):
            base = {"kind": "tag", "tag": "emotion_system", "owner": "x", "chars": 10,
                    "where": "system", "removable": True, "protected": False, "text": "T"}
            base.update(kw)
            return base
        self.plugin.store.push_history("u1", {
            "t": 1.0, "umo": "u1", "stage": "final", "before_chars": 1, "after_chars": 1,
            "parts_chars": 0, "removed_tags": [], "persona_card_used": False,
            "blocks": [
                block(where="system"),
                block(where="history:user"),
                block(tag="", kind="section", where="tool_result", text="工具输出", removable=True),
                block(tag="", kind="section", where="history:assistant", text="旧回复", removable=True),
            ],
        })
        data = await self.call(self.plugin.api_blocks, body={"umo": "u1"})
        by_where = {b["where"]: b for b in data["blocks"]}
        self.assertIn("system", by_where)
        self.assertIn("history:user", by_where)
        self.assertTrue(by_where["history:user"]["removable"])
        self.assertFalse(by_where["tool_result"]["removable"])
        self.assertFalse(by_where["history:assistant"]["removable"])

    async def test_trim_tool_rule_preserves_tool_name_case(self):
        """工具规则 tool:<name> 不能像 tag 一样整体小写，否则摘不掉。"""
        res = await self.call(
            self.plugin.api_trim,
            body={
                "action": "set", "scope": "session", "umo": "u1",
                "tag": "tool:uapi_weather",
            },
        )
        self.assertTrue(res["ok"])
        self.assertEqual(res["session_tags"], ["tool:uapi_weather"])
        self.assertEqual(res["effective_tags"], ["tool:uapi_weather"])

    async def test_blocks_tool_channel_and_core_note(self):
        """工具注入块要有独立 channel；核心工具只审计不拦截。"""
        self.plugin.store.push_history("u1", {
            "t": 1.0, "umo": "u1", "stage": "final",
            "before_chars": 0, "after_chars": 0, "parts_chars": 0,
            "removed_tags": [], "persona_card_used": False,
            "blocks": [
                {"kind": "tool", "tag": "tool:uapi_weather",
                 "owner": "astrbot_plugin_uapipro_toolbox", "chars": 100,
                 "where": "tool_schema", "removable": True, "protected": False,
                 "origin": "added", "text": "工具：uapi_weather"},
                {"kind": "tool", "tag": "tool:astrbot_execute_shell",
                 "owner": "(core)", "chars": 80,
                 "where": "tool_schema", "removable": False, "protected": False,
                 "origin": "baseline", "text": "工具：astrbot_execute_shell"},
            ],
        })
        data = await self.call(self.plugin.api_blocks, body={"umo": "u1"})
        by_tag = {b["tag"]: b for b in data["blocks"] if b["tag"].startswith("tool:")}
        self.assertEqual(by_tag["tool:uapi_weather"]["where"], "tool_schema")
        self.assertEqual(by_tag["tool:uapi_weather"]["trim_kind"], "tag")
        self.assertTrue(by_tag["tool:uapi_weather"]["removable"])
        self.assertFalse(by_tag["tool:astrbot_execute_shell"]["removable"])
        self.assertIn("核心工具", by_tag["tool:astrbot_execute_shell"]["note"])

    async def test_blocks_requires_umo(self):
        res = await self.call(self.plugin.api_blocks, body={})
        self.assertIn("error", res)

    async def test_history_list_limit_and_clear(self):
        for index in range(3):
            self.plugin.store.push_history(
                "u1", {"t": float(index), "umo": "u1", "blocks": [], "removed_tags": []}
            )
        data = await self.call(self.plugin.api_history, body={"umo": "u1", "limit": 2})
        self.assertEqual(len(data["records"]), 2)
        res = await self.call(self.plugin.api_history, body={"action": "clear", "umo": "u1"})
        self.assertEqual(res["cleared"], 3)
        data = await self.call(self.plugin.api_history, body={"umo": "u1"})
        self.assertEqual(data["records"], [])
        res = await self.call(self.plugin.api_history, body={})
        self.assertIn("error", res)

    async def test_persona_pool_and_card_rebuild(self):
        from astrbot_plugin_prompt_governor.core.persona import persona_hash

        self.plugin.store.put_card("p1", persona_hash("你是小雪。"), "卡片内容")
        data = await self.call(self.plugin.api_persona)
        self.assertTrue(data["persona_card_enabled"])
        names = {item["id"]: item for item in data["personas"]}
        self.assertEqual(set(names), {"p1", "p2"})
        self.assertTrue(names["p1"]["card_cached"])
        self.assertEqual(names["p1"]["card_chars"], 4)
        self.assertFalse(names["p1"]["card_stale"])          # 档案与人格正文一致
        self.assertFalse(names["p2"]["card_cached"])

        self.assertEqual(names["p1"]["chars"], len("你是小雪。"))
        self.assertEqual(names["p2"]["name"], "阿岚")
        self.assertEqual(data["cards"]["p1"]["card"], "卡片内容")

        # 人格正文改了：档案固化不动，只在面板标"已改"
        self.ctx.persona_manager._personas[0]["prompt"] = "你是小雪，改过的人设。"
        data = await self.call(self.plugin.api_persona)
        names = {item["id"]: item for item in data["personas"]}
        self.assertTrue(names["p1"]["card_stale"])
        self.assertEqual(names["p1"]["chars"], len("你是小雪，改过的人设。"))
        self.assertEqual(data["cards"]["p1"]["card"], "卡片内容")   # 一个字都没变

        res = await self.call(self.plugin.api_card_rebuild, body={"persona_id": "p1"})
        self.assertEqual(res["count"], 1)
        data = await self.call(self.plugin.api_persona)
        self.assertFalse({item["id"]: item for item in data["personas"]}["p1"]["card_cached"])
        self.assertEqual(data["cards"], {})

        self.plugin.store.put_card("p1", "h", "x")
        self.plugin.store.put_card("__inline__", "h", "y")
        res = await self.call(self.plugin.api_card_rebuild, body={})
        self.assertEqual(res["count"], 2)
        self.assertEqual(res["cards"], {})

    async def test_persona_generate_from_panel(self):
        """面板「生成人格卡」：调一次模型，落成摘要 + 分区。"""
        reply = (
            "小雪：猫耳女仆，软糯黏人。\n"
            "【行为举止】\n说话轻声，爱蹭人。\n"
            "【语言风格】\n句尾常带喵。"
        )
        provider = FakeProvider(reply)
        self.plugin.context = FakeContext(provider=provider)

        res = await self.call(self.plugin.api_persona_generate, body={"persona_id": "p1"})
        self.assertTrue(res["ok"])
        self.assertEqual(provider.calls, 1)
        self.assertIn("行为举止", provider.last_prompt)   # 提示词里带了分区表
        self.assertEqual(res["summary"], "小雪：猫耳女仆，软糯黏人。")
        self.assertEqual(
            [s["key"] for s in res["sections"]], ["behavior", "style"]
        )
        self.assertIn("behavior=行为举止", res["manifest"])
        self.assertNotIn("爱蹭人", res["manifest"])        # 正文不进常驻注入

        # 固化：生成后人格正文改了也不重算，面板只标"已改"
        self.plugin.context.persona_manager._personas[0]["prompt"] = "你是小雪，改过的人设。"
        data = await self.call(self.plugin.api_persona)
        item_after = {p["id"]: p for p in data["personas"]}["p1"]
        self.assertTrue(item_after["card_stale"])
        self.assertEqual(data["cards"]["p1"]["card"], res["card"])

        # 落库 + 面板状态跟着变
        data = await self.call(self.plugin.api_persona)
        item = {p["id"]: p for p in data["personas"]}["p1"]
        self.assertTrue(item["card_cached"])
        self.assertTrue(item["sectioned"])
        self.assertEqual([s["key"] for s in item["sections"]], ["behavior", "style"])
        self.assertIn("爱蹭人", data["cards"]["p1"]["sections"][0]["text"])
        self.assertEqual(data["cards"]["p1"]["summary"], "小雪：猫耳女仆，软糯黏人。")

        # 生成后按同一人格再生成不会报错（覆盖写）
        res2 = await self.call(self.plugin.api_persona_generate, body={"persona_id": "p1"})
        self.assertTrue(res2["ok"])
        self.assertEqual(provider.calls, 2)

    async def test_persona_generate_respects_custom_sections(self):
        """自定义分区要同时影响生成提示词和解析（两边用同一张表）。"""
        provider = FakeProvider("阿岚：冷静克制。\n【思维方式】先想再说。\n【口头禅】「嗯。」")
        self.plugin.context = FakeContext(provider=provider)
        self.plugin.config = dict(self.plugin.config)
        self.plugin.config["persona_card"] = {
            "enabled": True,
            "max_chars": 120,
            "sections": "logic=思维方式\ncatchphrase=口头禅:固定用词",
        }
        self.plugin.card_service.config = self.plugin.config

        res = await self.call(self.plugin.api_persona_generate, body={"persona_id": "p2"})

        self.assertIn("【思维方式】", provider.last_prompt)
        self.assertIn("catchphrase", provider.last_prompt)
        self.assertIn("固定用词", provider.last_prompt)
        self.assertEqual(
            [s["key"] for s in res["sections"]], ["logic", "catchphrase"]
        )
        data = await self.call(self.plugin.api_persona)
        self.assertEqual(data["section_names"]["logic"], "思维方式")

    async def test_persona_generate_errors(self):
        res = await self.call(self.plugin.api_persona_generate, body={})
        self.assertIn("error", res)

        # 人格池里没有这个 id
        self.plugin.context = FakeContext(provider=FakeProvider("x"))
        res = await self.call(self.plugin.api_persona_generate, body={"persona_id": "nope"})
        self.assertIn("error", res)
        self.assertEqual(res["status_code"], 502)

        # 拿不到模型
        self.plugin.context = FakeContext(provider=None)
        res = await self.call(self.plugin.api_persona_generate, body={"persona_id": "p1"})
        self.assertIn("error", res)
        self.assertIn("模型", res["error"])

        # 模型返回空
        self.plugin.context = FakeContext(provider=FakeProvider("   "))
        res = await self.call(self.plugin.api_persona_generate, body={"persona_id": "p1"})
        self.assertIn("error", res)

    async def test_plugin_toggle_roundtrip(self):
        res = await self.call(self.plugin.api_plugin_toggle, body={})
        self.assertIn("error", res)

        data = await self.call(self.plugin.api_plugin_toggle, body={"umo": "u1"})
        self.assertTrue(data["writable"])
        self.assertEqual(data["disabled"], [])
        self.assertEqual(data["plugins"], [])

        res = await self.call(
            self.plugin.api_plugin_toggle,
            body={"action": "set", "umo": "u1", "plugin": "astrbot_plugin_x", "disable": True},
        )
        self.assertTrue(res["disabled"])
        sp = stub.fake_sp()
        rules = await sp.session_get("u1", SESSION_KEY, {})
        self.assertEqual(rules, {"u1": {"disabled_plugins": ["astrbot_plugin_x"]}})

        data = await self.call(self.plugin.api_plugin_toggle, body={"umo": "u1"})
        self.assertEqual(data["disabled"], ["astrbot_plugin_x"])

        await self.call(
            self.plugin.api_plugin_toggle,
            body={"action": "set", "umo": "u1", "plugin": "astrbot_plugin_x", "disable": "false"},
        )
        data = await self.call(self.plugin.api_plugin_toggle, body={"umo": "u1"})
        self.assertEqual(data["disabled"], [])

        await self.call(
            self.plugin.api_plugin_toggle,
            body={"action": "set", "umo": "u1", "plugin": "astrbot_plugin_x", "disable": True},
        )
        await self.call(self.plugin.api_plugin_toggle, body={"action": "clear", "umo": "u1"})
        self.assertNotIn(("u1", SESSION_KEY), sp.store)

    async def test_unexpected_error_is_wrapped(self):
        class Boom(FakeContext):
            @property
            def persona_manager(self):
                raise RuntimeError("人格池炸了")

            @persona_manager.setter
            def persona_manager(self, value):
                pass

        self.plugin.context = Boom()
        res = await self.call(self.plugin.api_persona)
        self.assertIn("error", res)
        self.assertEqual(res["status_code"], 500)


if __name__ == "__main__":
    unittest.main()
