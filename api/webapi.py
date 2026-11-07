"""Web API：pages/governor 面板的后端接口。

桥只有 apiGet/apiPost，所有路由 GET+POST 双注册；
参数三处合并（query + JSON body），路由前缀含插件名，另挂兜底路由。
挂载方式：main.py 的 PromptGovernor(WebApiMixin, Star)。

约定：面板能拿到的每个数都要能在页面上找到出处，且任何异常都不能把
宿主带崩 —— 所有 handler 都过 _guard，出错返回 error_response。
"""

from __future__ import annotations

import functools
from typing import Any

from astrbot.api import logger

try:  # astrbot.api.web 在较新版本提供；缺失时降级为直通垫片
    from astrbot.api.web import error_response, json_response, request as web_request
except Exception:  # noqa: BLE001
    web_request = None  # type: ignore[assignment]

    def json_response(data: Any = None, **_kwargs: Any) -> Any:  # type: ignore[misc]
        return data if data is not None else {"ok": True}

    def error_response(message: str, **kwargs: Any) -> Any:  # type: ignore[misc]
        return {"error": message, "status_code": kwargs.get("status_code", 400)}


from ..core.models import (
    persona_section_keys,
    render_persona_card_with_sections,
    split_persona_card,
)
from ..core.persona import get_all_personas, persona_fields, persona_hash

PLUGIN_NAME = "astrbot_plugin_prompt_governor"
SESSION_PLUGIN_KEY = "session_plugin_config"
PLUGIN_MANAGED_FIELDS = ("enabled_plugins", "disabled_plugins")

#: 会话级插件开关拦不住的注入来源：挂在 LLM 请求链上的钩子类 handler
_HOOK_EVENT_NAMES = (
    "OnLLMRequestEvent",
    "OnLLMResponseEvent",
    "OnDecoratingResultEvent",
    "OnAgentBeginEvent",
    "OnAgentDoneEvent",
    "OnWaitingLLMRequestEvent",
    "OnUsingLLMToolEvent",
    "OnLLMToolRespondEvent",
)


def _to_bool(value: Any, default: bool = False) -> bool:
    """宽松解析布尔：桥有时把 true 传成 "true"/1，bool("false") 会误判成真。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "y", "t"}:
        return True
    if text in {"0", "false", "no", "off", "n", "f", ""}:
        return False
    return default


def _to_int(value: Any, default: int, low: int | None = None, high: int | None = None) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    if low is not None:
        number = max(low, number)
    if high is not None:
        number = min(high, number)
    return number


def _guard(handler: Any) -> Any:
    """把任何异常变成 error_response：面板报错可以，宿主 500 不行。"""

    @functools.wraps(handler)
    async def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return await handler(self, *args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            return self._err(exc)

    return wrapper


class WebApiMixin:
    """挂在 Star 类上的面板接口集合。依赖主类先初始化 self.store / self.context。"""

    def _route_defs(self) -> list[tuple[str, Any, str]]:
        return [
            ("overview", self.api_overview, "总览（配置/会话/规则/人格卡统计）"),
            ("history", self.api_history, "某会话最近 N 轮审计记录"),
            ("blocks", self.api_blocks, "最近若干轮的注入块明细"),
            ("trim", self.api_trim, "摘除规则读写"),
            ("persona", self.api_persona, "人格池列表与人格卡缓存"),
            ("persona_generate", self.api_persona_generate, "调 AI 生成/重写分区人格卡"),
            ("plugin_toggle", self.api_plugin_toggle, "会话级插件开关（原生 session_plugin_config）"),
            ("card_rebuild", self.api_card_rebuild, "强制重建人格卡缓存"),
        ]

    def register_web_apis(self) -> None:
        register = getattr(self.context, "register_web_api", None)
        if not callable(register):
            logger.warning(
                "[注入管家] 当前 AstrBot 无 context.register_web_api，"
                "面板不可用（聊天与指令不受影响）"
            )
            return
        ok = 0
        for name, handler, desc in self._route_defs():
            try:
                register(
                    f"/{PLUGIN_NAME}/{name}",
                    handler,
                    ["GET", "POST"],
                    f"注入管家：{desc}",
                )
                ok += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[注入管家] 注册面板接口 {name} 失败: {exc}")
        try:  # 兜底：桥可能带子路径（例如 trim/set）
            register(
                f"/{PLUGIN_NAME}/<path:rest>",
                self._api_fallback,
                ["GET", "POST"],
                "注入管家：兜底路由",
            )
            ok += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[注入管家] 注册兜底路由失败: {exc}")
        logger.info(f"[注入管家] 面板接口已注册 {ok} 个")

    async def _api_fallback(self, rest: str = "") -> Any:
        head = str(rest or "").split("/", 1)[0]
        for name, handler, _desc in self._route_defs():
            if name == head:
                return await handler()
        return error_response(f"未知的面板接口：{rest}", status_code=404)

    # ------------------------------------------------------------------
    # 请求工具
    # ------------------------------------------------------------------
    async def _params(self) -> dict[str, Any]:
        """合并 query 与 JSON body（三处都读，兼容桥的 GET/POST 两种风格）。"""
        merged: dict[str, Any] = {}
        if web_request is None:
            return merged
        try:
            for key in list(web_request.query.keys()):
                merged[key] = web_request.query.get(key)
        except Exception:  # noqa: BLE001
            pass
        body: Any = None
        try:
            body = await web_request.json()
        except Exception:  # noqa: BLE001 - 老版本 json() 不吃默认值/无 body
            try:
                raw = await web_request.body()
                if raw:
                    import json as _json

                    body = _json.loads(raw)
            except Exception:  # noqa: BLE001
                body = None
        if isinstance(body, dict):
            for key, value in body.items():
                merged.setdefault(key, value)
        return merged

    @staticmethod
    def _err(exc: Exception) -> Any:
        if isinstance(exc, (ValueError, KeyError)):
            return error_response(str(exc), status_code=400)
        logger.error(f"[注入管家] 面板接口错误: {exc}", exc_info=True)
        return error_response(f"{type(exc).__name__}: {exc}", status_code=500)

    # ------------------------------------------------------------------
    # 配置 / 插件清单 / 人格池 / 会话规则
    # ------------------------------------------------------------------
    def _cfg_value(self, dotted: str, default: Any = None) -> Any:
        node: Any = getattr(self, "config", None)
        if not isinstance(node, dict):
            return default
        for part in str(dotted).split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def _flag_value(self, dotted: str, default: bool = False) -> bool:
        return _to_bool(self._cfg_value(dotted, default), default)

    def _config_view(self) -> dict[str, Any]:
        """当前生效的插件配置（面板只读展示；开关本身在插件配置页）。"""
        return {
            "enabled": self._flag_value("enabled", True),
            "audit_enabled": self._flag_value("audit.enabled", True),
            "capture_content": self._flag_value("audit.capture_content", True),
            "history_limit": _to_int(
                self._cfg_value("audit.history_limit", 200), 200, 20, 2000
            ),
            "trim_enabled": self._flag_value("trim.enabled", True),
            "persona_card_enabled": self._flag_value("persona_card.enabled", False),
            "persona_card_max_chars": _to_int(
                self._cfg_value("persona_card.max_chars", 200), 200, 40, 2000
            ),
            "debug_log": self._flag_value("debug_log", False),
        }

    def _plugin_list(self) -> list[dict[str, Any]]:
        """已加载插件清单（star_map），带 reserved / 钩子标记。"""
        plugins: list[dict[str, Any]] = []
        hook_modules: set[str] = set()
        try:
            from astrbot.core.star.star_handler import (
                EventType,
                star_handlers_registry,
            )

            hook_types = {
                event_type
                for name in _HOOK_EVENT_NAMES
                for event_type in [getattr(EventType, name, None)]
                if event_type is not None
            }
            if hook_types:
                hook_modules = {
                    str(getattr(handler, "handler_module_path", "") or "")
                    for handler in star_handlers_registry
                    if getattr(handler, "event_type", None) in hook_types
                }
        except Exception as exc:  # noqa: BLE001 - 拿不到就退化成"未知钩子"
            logger.debug(f"[注入管家] 扫描插件钩子失败: {exc}")

        try:
            from astrbot.core.star.star import star_map
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[注入管家] 读取插件清单失败: {exc}")
            return plugins

        seen: set[str] = set()
        for meta in star_map.values():
            try:
                name = str(getattr(meta, "name", "") or "")
                if not name or name in seen:
                    continue
                seen.add(name)
                module_path = str(getattr(meta, "module_path", "") or "")
                has_hooks = bool(module_path) and any(
                    path == module_path or path.startswith(module_path + ".")
                    for path in hook_modules
                    if path
                )
                plugins.append(
                    {
                        "name": name,
                        "desc": str(getattr(meta, "desc", "") or ""),
                        "reserved": bool(getattr(meta, "reserved", False)),
                        "activated": bool(getattr(meta, "activated", True)),
                        "has_hooks": has_hooks,
                    }
                )
            except Exception:  # noqa: BLE001
                continue
        plugins.sort(key=lambda item: (item["reserved"], item["name"]))
        return plugins

    async def _persona_pool(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for persona in await get_all_personas(getattr(self, "context", None)):
            try:
                pid, name, text = persona_fields(persona)
            except Exception:  # noqa: BLE001
                continue
            if not pid:
                continue
            # hash 只用来在面板上判断「档案落后于人格正文」，不参与是否重算的决策
            items.append(
                {
                    "id": pid,
                    "name": name or pid,
                    "chars": len(text),
                    "hash": persona_hash(text),
                }
            )
        items.sort(key=lambda item: item["id"])
        return items

    # ------------------------------------------------------------------
    # 总览
    # ------------------------------------------------------------------
    @_guard
    async def api_overview(self) -> Any:
        params = await self._params()
        umo = str(params.get("umo") or "")
        config = self._config_view()
        sessions = self.store.history_summary()
        rules_sessions = self.store.session_rule_map()
        cards = self.store.cards_info()
        history = self.store.get_history(umo, limit=1) if umo else []
        data: dict[str, Any] = {
            "ok": True,
            "data_dir": str(self.store.data_dir),
            "config": config,
            "counts": {
                "sessions": len(sessions),
                "cards": len(cards),
                "global_rules": len(self.store.global_rules()),
                "session_rules": sum(len(tags) for tags in rules_sessions.values()),
                "plugins": len(self._plugin_list()),
            },
            "rules_global": self.store.global_rules(),
            "rules_sessions": rules_sessions,
            "sessions": sessions,
            "cards": {
                pid: {"chars": info["chars"], "created_at": info["created_at"]}
                for pid, info in cards.items()
            },
            "last_turn": history[-1] if history else None,
        }
        return json_response(data)

    # ------------------------------------------------------------------
    # 审计记录
    # ------------------------------------------------------------------
    @_guard
    async def api_history(self) -> Any:
        params = await self._params()
        action = str(params.get("action") or "list").lower()
        umo = str(params.get("umo") or "")
        if action == "clear":
            cleared = self.store.clear_history(umo or None)
            return json_response({"ok": True, "umo": umo, "cleared": cleared})
        if not umo:
            return error_response("缺少 umo 参数", status_code=400)
        limit = _to_int(params.get("limit"), 20, 1, 200)
        return json_response(
            {
                "ok": True,
                "umo": umo,
                "limit": limit,
                "records": self.store.get_history(umo, limit=limit),
            }
        )

    @_guard
    async def api_blocks(self) -> Any:
        """列出最近若干轮审计里出现过的所有块（跨轮聚合，按 tag/kind 归并）。"""
        params = await self._params()
        umo = str(params.get("umo") or "")
        if not umo:
            return error_response("缺少 umo 参数", status_code=400)
        window = _to_int(params.get("window"), 20, 1, 100)
        history = self.store.get_history(umo, limit=window)
        session_tags = set(self.store.session_rules(umo))
        global_tags = set(self.store.global_rules())

        aggregated: dict[str, dict[str, Any]] = {}
        for record in history:  # 旧 → 新，新值覆盖旧值
            for block in record.get("blocks") or []:
                if not isinstance(block, dict):
                    continue
                key = str(block.get("tag") or block.get("kind") or "other")
                entry = aggregated.get(key)
                if entry is None:
                    entry = {
                        "key": key,
                        "tag": str(block.get("tag") or ""),
                        "kind": str(block.get("kind") or "other"),
                        "owner": str(block.get("owner") or "(unknown)"),
                        "where": str(block.get("where") or "system"),
                        "removable": bool(block.get("removable", False)),
                        "protected": bool(block.get("protected", False)),
                        "chars": 0,
                        "text": "",
                        "seen_turns": 0,
                        "last_t": 0.0,
                    }
                    aggregated[key] = entry
                chars = _to_int(block.get("chars"), 0, 0)
                if chars:
                    entry["chars"] = chars
                text = block.get("text")
                if isinstance(text, str) and text:
                    entry["text"] = text
                entry["seen_turns"] += 1
                entry["last_t"] = float(record.get("t") or 0.0)

        for entry in aggregated.values():
            tag = entry["tag"]
            entry["session_removed"] = bool(tag) and tag in session_tags
            entry["global_removed"] = bool(tag) and tag in global_tags
            entry["effective_removed"] = entry["session_removed"] or entry["global_removed"]

        # 安全底线块置顶（提醒用户它摘不掉），其余按字符数从大到小
        blocks = sorted(
            aggregated.values(),
            key=lambda item: (-int(item["protected"]), -int(item["chars"] or 0)),
        )
        return json_response(
            {
                "ok": True,
                "umo": umo,
                "window": window,
                "turns": len(history),
                "session_tags": sorted(session_tags),
                "global_tags": sorted(global_tags),
                "blocks": blocks,
            }
        )

    # ------------------------------------------------------------------
    # 摘除规则
    # ------------------------------------------------------------------
    @_guard
    async def api_trim(self) -> Any:
        params = await self._params()
        action = str(params.get("action") or "get").lower()
        scope = str(params.get("scope") or "session").lower()
        umo = str(params.get("umo") or "")

        def snapshot() -> dict[str, Any]:
            return {
                "ok": True,
                "umo": umo,
                "scope": scope,
                "session_tags": self.store.session_rules(umo),
                "global_tags": self.store.global_rules(),
                "effective_tags": (
                    sorted(self.store.tags_for(umo)) if umo else self.store.global_rules()
                ),
                "rules": {
                    key: sorted(value) for key, value in self.store.trim_rules.items()
                },
            }

        if action == "get":
            return json_response(snapshot())

        if action == "set":
            tag = str(params.get("tag") or "").strip().lower()
            if not tag:
                return error_response("缺少 tag 参数", status_code=400)
            key = "*" if scope == "global" else (umo or "*")
            remove = _to_bool(params.get("remove"), True)
            self.store.set_rule(key, tag, remove)
            result = snapshot()
            result.update({"tag": tag, "removed": remove, "target": key})
            return json_response(result)

        if action == "clear":
            if _to_bool(params.get("all"), False):
                self.store.clear_rules(None)
            elif scope == "global":
                self.store.clear_rules("*")
            elif umo:
                self.store.clear_rules(umo)
            else:
                return error_response("缺少 umo 参数", status_code=400)
            return json_response(snapshot())

        return error_response(f"未知 action：{action}", status_code=400)

    # ------------------------------------------------------------------
    # 人格卡
    # ------------------------------------------------------------------
    @_guard
    async def api_persona(self) -> Any:
        """人格池 + 人格卡缓存状态（含分区预览）。"""
        config = self._config_view()
        personas = await self._persona_pool()
        cards = self.store.cards_info(self.config)
        known = {item["id"] for item in personas}
        for item in personas:
            info = cards.get(item["id"])
            item["card_cached"] = info is not None
            item["card_chars"] = info["chars"] if info else 0
            item["card_created_at"] = info["created_at"] if info else 0.0
            item["sectioned"] = bool(info and info.get("sectioned"))
            item["sections"] = list(info.get("sections") or []) if info else []
            # 固化语义：档案不会自动跟着人格改，只在这里提示"该重写了"
            item["card_stale"] = bool(
                info and self.store.card_is_stale(item["id"], item.get("hash", ""))
            )
        return json_response(
            {
                "ok": True,
                "persona_card_enabled": config["persona_card_enabled"],
                "persona_card_max_chars": config["persona_card_max_chars"],
                "config": config,
                "personas": personas,
                "cards": cards,
                "orphan_cards": sorted(pid for pid in cards if pid not in known),
                "section_names": {
                    key: name
                    for key, (name, _hint) in persona_section_keys(self.config).items()
                },
            }
        )

    @_guard
    async def api_persona_generate(self) -> Any:
        """在面板里调 AI 生成/重写某个人格的分区人格卡（不需要先去聊天）。

        生成一份卡 = 一次模型调用；结果按人格缓存，之后每轮只注入摘要 + 目录，
        要哪部分由模型用 recall_persona 取哪部分。
        """
        params = await self._params()
        persona_id = str(params.get("persona_id") or "").strip()
        if not persona_id:
            return error_response("缺少 persona_id 参数", status_code=400)

        card, error = await self.card_service.generate_card(self.context, persona_id)
        if not card:
            return error_response(error or "生成失败", status_code=502)

        sections = persona_section_keys(self.config)
        summary, parts = split_persona_card(card, sections)
        preview = render_persona_card_with_sections(summary, list(parts.keys()), sections)
        info = self.store.cards_info(self.config).get(persona_id) or {}
        logger.info(
            f"[注入管家] 面板生成人格卡：{persona_id}（{len(card)} 字，"
            f"{len(parts)} 个部分）"
        )
        return json_response(
            {
                "ok": True,
                "persona_id": persona_id,
                "card": card,
                "summary": summary,
                "sections": info.get("sections") or [],
                "manifest": preview,
            }
        )

    @_guard
    async def api_card_rebuild(self) -> Any:
        """清空人格卡缓存（下次对话时重新压缩）。不传 persona_id 即清全部。"""
        params = await self._params()
        persona_id = str(params.get("persona_id") or "").strip()
        cards = self.store.persona_cards
        if persona_id:
            cleared = [persona_id] if cards.pop(persona_id, None) is not None else []
        else:
            cleared = list(cards.keys())
            cards.clear()
        if cleared:
            self.store.save()
        info = self.store.cards_info(self.config)
        return json_response(
            {
                "ok": True,
                "cleared": cleared,
                "count": len(cleared),
                "cards": {
                    pid: {"chars": item["chars"], "created_at": item["created_at"]}
                    for pid, item in info.items()
                },
            }
        )

    # ------------------------------------------------------------------
    # 会话级插件开关（原生 session_plugin_config）
    # ------------------------------------------------------------------
    @staticmethod
    def _session_sp() -> Any:
        try:
            from astrbot.core import sp

            return sp
        except Exception:  # noqa: BLE001
            return None

    async def _session_get(self, sp: Any, umo: str) -> dict[str, Any]:
        try:
            rules = await sp.session_get(umo, SESSION_PLUGIN_KEY, {})
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[注入管家] 读取会话插件开关失败: {exc}")
            return {}
        return rules if isinstance(rules, dict) else {}

    @_guard
    async def api_plugin_toggle(self) -> Any:
        params = await self._params()
        action = str(params.get("action") or "get").lower()
        umo = str(params.get("umo") or "")
        if not umo:
            return error_response("缺少 umo 参数", status_code=400)

        sp = self._session_sp()
        plugins = self._plugin_list()
        if sp is None:
            if action == "get":
                return json_response(
                    {
                        "ok": True,
                        "umo": umo,
                        "writable": False,
                        "plugins": plugins,
                        "disabled": [],
                        "enabled": [],
                        "message": "当前 AstrBot 版本没有 sp 会话规则存储，只能看不能改",
                    }
                )
            return error_response("当前 AstrBot 版本没有 sp 会话规则存储", status_code=500)

        if action == "get":
            rules = await self._session_get(sp, umo)
            inner = rules.get(umo)
            inner = inner if isinstance(inner, dict) else {}
            disabled = [str(x) for x in (inner.get("disabled_plugins") or []) if str(x)]
            enabled = [str(x) for x in (inner.get("enabled_plugins") or []) if str(x)]
            disabled_set, enabled_set = set(disabled), set(enabled)
            for plugin in plugins:
                plugin["disabled"] = plugin["name"] in disabled_set
                plugin["whitelisted"] = plugin["name"] in enabled_set
            return json_response(
                {
                    "ok": True,
                    "umo": umo,
                    "writable": True,
                    "disabled": disabled,
                    "enabled": enabled,
                    "whitelist_mode": bool(enabled),
                    "plugins": plugins,
                }
            )

        if action == "set":
            plugin_name = str(params.get("plugin") or "").strip()
            if not plugin_name:
                return error_response("缺少 plugin 参数", status_code=400)
            disable = _to_bool(params.get("disable"), True)
            rules = await self._session_get(sp, umo)
            target = dict(rules)
            inner = target.get(umo)
            inner = dict(inner) if isinstance(inner, dict) else {}
            disabled = [str(x) for x in (inner.get("disabled_plugins") or []) if str(x)]
            if disable:
                if plugin_name not in disabled:
                    disabled.append(plugin_name)
            else:
                disabled = [name for name in disabled if name != plugin_name]
            inner["disabled_plugins"] = disabled
            target[umo] = inner
            try:
                await sp.session_put(umo, SESSION_PLUGIN_KEY, target)
            except Exception as exc:  # noqa: BLE001
                return error_response(f"写入会话插件开关失败：{exc}", status_code=500)
            return json_response(
                {
                    "ok": True,
                    "umo": umo,
                    "plugin": plugin_name,
                    "disabled": disable,
                    "disabled_plugins": disabled,
                }
            )

        if action == "clear":
            rules = await self._session_get(sp, umo)
            inner = rules.get(umo)
            if isinstance(inner, dict):
                remaining = dict(rules)
                inner_rest = dict(inner)
                for field in PLUGIN_MANAGED_FIELDS:
                    inner_rest.pop(field, None)
                if inner_rest:
                    remaining[umo] = inner_rest
                else:
                    remaining.pop(umo, None)
                try:
                    if remaining:
                        await sp.session_put(umo, SESSION_PLUGIN_KEY, remaining)
                    else:
                        await sp.session_remove(umo, SESSION_PLUGIN_KEY)
                except Exception as exc:  # noqa: BLE001
                    return error_response(f"清除会话插件开关失败：{exc}", status_code=500)
            return json_response({"ok": True, "umo": umo, "disabled": [], "enabled": []})

        return error_response(f"未知 action：{action}", status_code=400)
