"""一次性脚本：用真实 handler 产出面板测试夹具（人工贴进 panel_check.mjs）。"""
import asyncio, json, sys, tempfile, types
from pathlib import Path
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parents[1]))
import _astrbot_stub as stub
stub.install()

# 让 _plugin_list() 能拿到一份插件清单
star_mod = types.ModuleType("astrbot.core.star.star")
class Meta:
    def __init__(self, name, desc="", reserved=False, module_path="", activated=True):
        self.name, self.desc, self.reserved, self.module_path, self.activated = name, desc, reserved, module_path, activated
star_mod.star_map = {
    "a": Meta("astrbot_plugin_ai_mind_simple", "情绪与记忆", module_path="data.plugins.ai_mind"),
    "b": Meta("astrbot_plugin_humanize_guard", "人味守卫", module_path="data.plugins.humanize"),
    "c": Meta("astrbot", "核心", reserved=True),
}
star_pkg = types.ModuleType("astrbot.core.star"); star_pkg.__path__ = []
sys.modules["astrbot.core.star"] = star_pkg
sys.modules["astrbot.core.star.star"] = star_mod

from astrbot_plugin_prompt_governor.api import webapi as webapi_mod
from astrbot_plugin_prompt_governor.main import PromptGovernor

tmp = tempfile.TemporaryDirectory()
stub.set_data_dir(tmp.name)
class DumpProvider:
    """模型替身：面板「生成人格卡」用，返回一份分区卡。"""

    async def text_chat(self, prompt="", system_prompt="", **_kw):
        return types.SimpleNamespace(completion_text=(
            "阿岚：图书馆管理员，冷静克制、话少但很稳。\n"
            "【行为举止】说话慢半拍，习惯先想再说；不主动碰别人。\n"
            "【语言风格】短句、书面语，很少用感叹号。\n"
            "【关系立场】\n对熟人才会主动多说两句，讨厌被催促。"
        ))


ctx = types.SimpleNamespace(
    registered=[],
    persona_manager=type("PM", (), {"get_all_personas": staticmethod(lambda: [
        {"persona_id": "p1", "name": "小雪", "prompt": "你是小雪，说话软软的。没事就喜欢抱着猫在窗台上晒太阳。"},
        {"persona_id": "p2", "name": "阿岚", "prompt": "你是阿岚，图书馆管理员。"},
    ])})(),
    get_using_provider=staticmethod(lambda: DumpProvider()),
)
ctx.register_web_api = lambda *a, **k: ctx.registered.append(a)
plugin = PromptGovernor(ctx, {
    "enabled": True,
    "audit": {"enabled": True, "history_limit": 50, "capture_content": True},
    "persona_card": {"enabled": True, "max_chars": 120},
    "trim": {"enabled": True},
})
UMO = "aiocqhttp:GroupMessage:1001"
plugin.store.set_rule(UMO, "emotion_system", True)
plugin.store.set_rule("*", "long_term_memory", True)
# p1 有一份已经生成好的分区档案（摘要 + 三个部分）。
# 记录的 hash 故意是旧的 → 面板应显示「人格已改，档案未更新」（固化语义，不自动重算）。
plugin.store.put_card("p1", "old-hash-before-persona-edit", (
    "小雪：猫耳女仆，说话软软的，黏人但有点怕生。\n"
    "【行为举止】\n说话轻声，爱蹭人；被夸会害羞地别过头。\n"
    "【语言风格】\n句尾常带「喵」，句子很短。\n"
    "【背景故事】\n在街角咖啡店打工，住阁楼，怕打雷。"
))
plugin.store.push_history(UMO, {"t": 1770000000.0, "umo": UMO, "before_chars": 1500, "after_chars": 1180,
    "parts_chars": 240, "removed_tags": ["emotion_system"], "persona_card_used": True,
    "blocks": [
      {"kind": "persona", "tag": "", "owner": "(persona)", "chars": 1000, "where": "system", "removable": False, "protected": False, "text": "# Persona Instructions\n\n你是小雪。"},
      {"kind": "tag", "tag": "long_term_memory", "owner": "astrbot_plugin_ai_mind", "chars": 260, "where": "system", "removable": True, "protected": False, "text": "<long_term_memory>他喜欢冰美式。</long_term_memory>"},
      {"kind": "tag", "tag": "emotion_system", "owner": "astrbot_plugin_ai_mind", "chars": 120, "where": "system", "removable": True, "protected": False, "text": "<emotion_system>有点困。</emotion_system>"},
      {"kind": "tag", "tag": "safety_override", "owner": "astrbot_plugin_ai_mind", "chars": 60, "where": "content_part", "removable": False, "protected": True, "text": "<safety_override>认真关心。</safety_override>"},
    ]})
plugin.store.push_history(UMO, {"t": 1770000060.0, "umo": UMO, "before_chars": 1500, "after_chars": 1140,
    "parts_chars": 240, "removed_tags": ["emotion_system"], "persona_card_used": True, "blocks": []})
rules = {"aiocqhttp:GroupMessage:1001": {"disabled_plugins": ["astrbot_plugin_humanize_guard"]}}
asyncio.run(stub.fake_sp().session_put(UMO, "session_plugin_config", rules))

class Req:
    def __init__(self, q=None, b=None): self.query = q or {}; self._b = b
    async def json(self):
        if self._b is None: raise ValueError("no body")
        return self._b
    async def body(self): return b""

async def call(fn, **body):
    webapi_mod.web_request = Req(b=body)
    try: return await fn()
    finally: webapi_mod.web_request = None

async def main():
    return {
        "overview": await call(plugin.api_overview, umo=UMO),
        "blocks": await call(plugin.api_blocks, umo=UMO, window=20),
        "trim": await call(plugin.api_trim, umo=UMO, action="get"),
        "history": await call(plugin.api_history, umo=UMO, limit=10),
        "persona": await call(plugin.api_persona),
        "plugin_toggle": await call(plugin.api_plugin_toggle, umo=UMO),
        # 面板「为 p2 生成人格卡」的真实返回（调一次 DumpProvider）
        "persona_generate": await call(plugin.api_persona_generate, persona_id="p2"),
    }

data = asyncio.run(main())

# 归一化易变字段（临时目录 / 当前时间），否则每次重生成夹具都会产生无意义 diff
FIXED_TS = 1767225600.0
data["overview"]["data_dir"] = "<AstrBot data dir>\\astrbot_plugin_prompt_governor"
for info in data["overview"]["cards"].values():
    info["created_at"] = FIXED_TS
for info in data["persona"]["cards"].values():
    info["created_at"] = FIXED_TS
for item in data["persona"]["personas"]:
    item["card_created_at"] = FIXED_TS if item["card_cached"] else 0.0

payload = json.dumps(data, ensure_ascii=False, indent=2)
if len(sys.argv) > 1:
    Path(sys.argv[1]).write_text(payload + "\n", encoding="utf-8")
    print("wrote " + sys.argv[1])
else:
    print(payload)