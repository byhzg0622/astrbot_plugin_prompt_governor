/**
 * 面板前端的沙箱测试（Node，无第三方依赖）。
 *
 * 为什么需要它：面板跑在不透明源的 iframe 里，只能走宿主注入的 postMessage 桥；
 * pages/governor/index.html 原本只有一段孤立的 JS 片段（没有 <html>/<body>、
 * 没有 id、还有个少括号的语法错），Python 侧完全测不出来。这里用迷你 DOM 把页面
 * 真跑一遍：接桥、加载、渲染、点开关、切页签，全部断言。
 *
 * 夹具 tests/fixtures/panel_api.json 由真实后端 handler 产出：
 *     python tests/_dump_fixtures.py tests/fixtures/panel_api.json
 *
 * 运行：
 *     node tests/panel_check.mjs
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import vm from "node:vm";

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = join(HERE, "..");
const HTML = readFileSync(join(ROOT, "pages", "governor", "index.html"), "utf8");
const FIX = JSON.parse(readFileSync(join(HERE, "fixtures", "panel_api.json"), "utf8"));
const PY_API = readFileSync(join(ROOT, "api", "webapi.py"), "utf8");

const problems = [];
function check(ok, msg) {
  if (!ok) problems.push(msg);
}
function report(title, list) {
  if (list.length) {
    console.log("❌ " + title);
    list.forEach(function (p) { console.log("   " + p); });
    process.exit(1);
  }
  console.log("✅ " + title);
}

// ============================ 静态检查 ============================
const BLOCKS = [...HTML.matchAll(/<script(?![^>]*src=)[^>]*>([\s\S]*?)<\/script>/g)].map((m) => m[1]);
report("页面里有内联主脚本", BLOCKS.length === 1 && BLOCKS[0].length > 5000
  ? [] : ["内联 script 数量 = " + BLOCKS.length]);
const SCRIPT = BLOCKS.reduce((a, b) => (b.length > a.length ? b : a));

report("页面是完整 HTML（doctype/head/body 齐全）",
  /^<!doctype html>/i.test(HTML) && HTML.indexOf("<body>") > 0 && /<\/html>\s*$/.test(HTML)
    ? [] : ["缺少 doctype / body / 收尾标签"]);

report("挂了宿主桥 SDK（/api/plugin/page/bridge-sdk.js）",
  HTML.indexOf('src="/api/plugin/page/bridge-sdk.js"') > 0 ? [] : ["没有 bridge-sdk.js 引用"]);

{
  const dups = [];
  const names = [...SCRIPT.matchAll(/^(?:async )?function (\w+)\s*\(/gm)].map((m) => m[1]);
  names.forEach(function (n, i) { if (names.indexOf(n) !== i && dups.indexOf(n) < 0) dups.push(n); });
  report("脚本里没有重复的函数定义（共 " + names.length + " 个）",
    dups.map(function (n) { return "重复定义：" + n; }));
}

{
  const ids = [...HTML.matchAll(/\sid="([^"]+)"/g)].map((m) => m[1]);
  const dup = [...new Set(ids.filter((id, i) => ids.indexOf(id) !== i))];
  report("面板 id 无重复（共 " + ids.length + " 个）",
    dup.map(function (id) { return "重复 id：" + id; }));
}

{
  const modal = [...SCRIPT.matchAll(/window\.(confirm|alert|prompt)\s*\(/g)];
  report("没有用会被沙箱静默拦掉的 window.confirm/alert/prompt",
    modal.map(function (m) { return "发现 window." + m[1]; }));
}

{
  const cleaned = HTML
    .replace(/<script[\s\S]*?<\/script>/gi, "")
    .replace(/<style[\s\S]*?<\/style>/gi, "")
    .replace(/<!--[\s\S]*?-->/g, "");
  const VOID = new Set(["area", "base", "br", "col", "embed", "hr", "img", "input",
                        "link", "meta", "param", "source", "track", "wbr"]);
  const re = /<(\/?)([a-zA-Z][a-zA-Z0-9-]*)([^>]*?)(\/?)>/g;
  const stack = [];
  let match;
  const bad = [];
  while ((match = re.exec(cleaned)) !== null) {
    const tag = match[2].toLowerCase();
    if (VOID.has(tag) || match[4] === "/") continue;
    if (match[1] !== "/") { stack.push(tag); continue; }
    const top = stack.pop();
    if (top !== tag) { bad.push("</" + tag + "> 对不上（栈顶 " + (top || "空") + "）"); break; }
  }
  if (!bad.length && stack.length) bad.push("未闭合标签：" + stack.join(" > "));
  report("HTML 标签配平", bad);
}

{
  // 每个 $("id") 都要有出处：静态 HTML 或脚本里动态生成的 id
  const staticIds = new Set([...HTML.matchAll(/\sid="([^"]+)"/g)].map((m) => m[1]));
  const dynIds = new Set([...SCRIPT.matchAll(/id="([a-zA-Z][\w-]*)"/g)].map((m) => m[1]));
  const used = new Set([...SCRIPT.matchAll(/\$\("([\w-]+)"\)/g)].map((m) => m[1]));
  const missing = [...used].filter(function (id) { return !staticIds.has(id) && !dynIds.has(id); });
  report("$(\"id\") 引用都能找到对应元素（共 " + used.size + " 个）",
    missing.map(function (id) { return "找不到 id=" + id; }));
}

{
  // 页面调的端点必须在后端 _route_defs 里存在
  const routes = new Set([...PY_API.matchAll(/\("(\w+)", self\.api_/g)].map((m) => m[1]));
  const used = [...new Set([...SCRIPT.matchAll(/api(?:Get|Post)\("(\w+)"/g)].map((m) => m[1]))];
  const unknown = used.filter(function (e) { return !routes.has(e); });
  report("页面调的端点后端都注册了（" + used.join(", ") + "）",
    unknown.map(function (e) { return "后端没有端点：" + e; }));
}

{
  // 页签按钮必须接线到 showPage，否则点哪儿都没反应（真踩过）
  const wired = /querySelectorAll\("nav\.pnav button"\)[\s\S]{0,200}?\.onclick/.test(SCRIPT);
  report("页签按钮接到 showPage", wired ? [] : ["nav.pnav button 没绑 onclick"]);
}

// ============================ 迷你 DOM ============================
function stub(attrs) {
  const el = {
    _attrs: attrs || {},
    id: (attrs && attrs.id) || "",
    className: (attrs && attrs.class) || "",
    checked: !!(attrs && attrs.checked),
    value: "",
    title: "",
    textContent: "",
    innerHTML: "",
    onclick: null, onchange: null, oninput: null, onkeydown: null,
    getAttribute: function (k) { return Object.prototype.hasOwnProperty.call(this._attrs, k) ? this._attrs[k] : null; },
    setAttribute: function (k, v) { this._attrs[k] = String(v); },
    classList: {
      _s: new Set(),
      add: function (c) { this._s.add(c); },
      remove: function (c) { this._s.delete(c); },
      contains: function (c) { return this._s.has(c); },
      toggle: function (c, on) { if (on === undefined) { on = !this._s.has(c); } if (on) { this._s.add(c); } else { this._s.delete(c); } return on; }
    },
    // 同一个宿主 + 同一个选择器要返回同一批元素对象：页面里
    // querySelectorAll 拿到对象后是往它身上挂 onchange/onclick 的，
    // 每次重新造对象就等于"事件丢了"。innerHTML 一变就作废缓存。
    querySelectorAll: function (sel) {
      const s = String(sel);
      if (this._cacheHtml !== this.innerHTML) { this._cacheHtml = this.innerHTML; this._cache = {}; }
      if (!this._cache[s]) { this._cache[s] = parseWithin(this.innerHTML, s); }
      return this._cache[s];
    },
    appendChild: function () {},
    focus: function () {}
  };
  return el;
}
function matchAll(html, re) {
  const out = [];
  let m;
  re.lastIndex = 0;
  while ((m = re.exec(String(html || ""))) !== null) { out.push(m); }
  return out;
}
function parseWithin(html, sel) {
  const s = String(sel);
  if (/input\[data-tag\]/.test(s)) {
    return matchAll(html, /<input[^>]*data-tag="([^"]+)"([^>]*)>/g).map(function (m) {
      const el = stub({ "data-tag": m[1] });
      el.checked = /\bchecked\b/.test(m[2] || "");
      return el;
    });
  }
  if (/\[data-gtag\]/.test(s)) {
    return matchAll(html, /<[a-zA-Z]+[^>]*data-gtag="([^"]+)"[^>]*>/g).map(function (m) {
      return stub({ "data-gtag": m[1] });
    });
  }
  if (/\[data-rebuild\]/.test(s)) {
    return matchAll(html, /<[a-zA-Z]+[^>]*data-rebuild="([^"]+)"[^>]*>/g).map(function (m) {
      return stub({ "data-rebuild": m[1] });
    });
  }
  if (/\[data-gen\]/.test(s)) {
    return matchAll(html, /<[a-zA-Z]+[^>]*data-gen="([^"]+)"[^>]*>/g).map(function (m) {
      return stub({ "data-gen": m[1] });
    });
  }
  if (/input\[data-plug\]/.test(s)) {
    return matchAll(html, /<input[^>]*data-plug="([^"]+)"([^>]*)>/g).map(function (m) {
      const el = stub({ "data-plug": m[1] });
      el.checked = /\bchecked\b/.test(m[2] || "");
      return el;
    });
  }
  if (/\.item/.test(s)) {
    return matchAll(html, /<div class="item[^"]*" data-umo="([^"]+)"/g).map(function (m) {
      const el = stub({ "data-umo": m[1] });
      el.className = "item";
      return el;
    });
  }
  return [];
}

const elements = new Map();
function getEl(id) {
  if (!elements.has(id)) { elements.set(id, stub({ id: id })); }
  return elements.get(id);
}
const NAV = ["audit", "persona", "plugins", "about"].map(function (page) {
  const el = stub({ "data-page": page });
  el.className = page === "audit" ? "active" : "";
  return el;
});
const PAGES = ["audit", "persona", "plugins", "about"].map(function (page) {
  const el = stub({ id: "page-" + page });
  el.className = page === "audit" ? "page active" : "page col";
  return el;
});

const bridgeCalls = [];
const RESPONSES = {
  overview: FIX.overview,
  blocks: FIX.blocks,
  trim: FIX.trim,
  history: FIX.history,
  persona: FIX.persona,
  plugin_toggle: FIX.plugin_toggle,
  persona_generate: FIX.persona_generate
};

const sandbox = {
  console: console,
  setTimeout: function (fn, ms) { return setTimeout(fn, ms || 0); },
  clearTimeout: function (t) { return clearTimeout(t); },
  Promise: Promise, JSON: JSON, Math: Math, Date: Date, Number: Number,
  String: String, Object: Object, Array: Array, Error: Error, RegExp: RegExp,
  Boolean: Boolean, isNaN: isNaN
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
sandbox.document = {
  getElementById: getEl,
  createElement: function () { return stub({}); },
  querySelector: function () { return null; },
  querySelectorAll: function (sel) {
    const s = String(sel);
    if (s === "nav.pnav button") return NAV;
    if (s === ".page") return PAGES;
    return [];
  },
  documentElement: { getAttribute: function () { return "dark"; }, setAttribute: function () {} },
  body: stub({})
};
sandbox.AstrBotPluginPage = {
  ready: function () { return Promise.resolve({ isDark: true }); },
  onContext: function () {},
  apiGet: function (endpoint, params) {
    bridgeCalls.push({ method: "GET", endpoint: endpoint, params: params });
    if (String(endpoint).indexOf("?") >= 0) {
      return Promise.reject(new Error("endpoint 不能带查询串"));
    }
    if (!RESPONSES[endpoint]) { return Promise.reject(new Error("未知 GET 端点 " + endpoint)); }
    return Promise.resolve(RESPONSES[endpoint]);
  },
  apiPost: function (endpoint, body) {
    bridgeCalls.push({ method: "POST", endpoint: endpoint, body: body });
    if (!RESPONSES[endpoint]) { return Promise.reject(new Error("未知 POST 端点 " + endpoint)); }
    if (endpoint === "trim") { return Promise.resolve(FIX.trim); }
    if (endpoint === "history") { return Promise.resolve({ ok: true, cleared: 2 }); }
    if (endpoint === "card_rebuild") { return Promise.resolve({ ok: true, count: 1, cards: {} }); }
    if (endpoint === "persona_generate") { return Promise.resolve(FIX.persona_generate); }
    if (endpoint === "plugin_toggle") { return Promise.resolve({ ok: true }); }
    return Promise.resolve({ ok: true });
  }
};

const tick = function (ms) { return new Promise(function (r) { setTimeout(r, ms || 40); }); };

// ============================ 跑页面 ============================
const context = vm.createContext(sandbox);
try {
  vm.runInContext(SCRIPT, context, { filename: "governor-panel.js" });
} catch (e) {
  console.log("❌ 页面脚本抛异常：" + e.message);
  process.exit(1);
}
await tick(80);

const dynamic = [];
check(getEl("stat").textContent.indexOf("1 个会话") >= 0,
  "顶栏统计没显示会话数：" + JSON.stringify(getEl("stat").textContent));
check(getEl("chipSwitch").textContent.indexOf("人格卡模式") >= 0,
  "顶栏状态芯片不对：" + JSON.stringify(getEl("chipSwitch").textContent));
check(bridgeCalls.some(function (c) { return c.method === "GET" && c.endpoint === "overview"; }),
  "启动没有拉 overview");

const sessionsHtml = getEl("sessionList").innerHTML;
check(sessionsHtml.indexOf("1001") >= 0, "会话列表没有渲染会话短名");
check(sessionsHtml.indexOf("2 轮") >= 0, "会话列表没有渲染轮数");
check(sessionsHtml.indexOf("-360") >= 0, "会话列表没有渲染本轮省下的字符数");
check(sessionsHtml.indexOf('data-umo="aiocqhttp:GroupMessage:1001"') >= 0, "会话条目缺 data-umo");

const detail = getEl("detail").innerHTML;
check(detail.indexOf("1,500") >= 0 && detail.indexOf("1,140") >= 0, "明细没有渲染注入前→后字符数");
check(detail.indexOf("360") >= 0, "明细没有渲染本轮省下的字符数");
check(detail.indexOf("240") >= 0, "明细没有渲染附加段字符数");
check(detail.indexOf("&lt;emotion_system&gt;") >= 0, "明细没有转义渲染 <emotion_system>");
check(detail.indexOf("astrbot_plugin_ai_mind") >= 0, "明细没有渲染来源归因");
check(detail.indexOf("全局已摘") >= 0, "明细没有标出全局已摘除的块");
check(detail.indexOf("安全底线") >= 0, "明细没有标出安全底线块");
check(detail.indexOf("查看注入全文") >= 0, "明细没有折叠的注入全文");
check(detail.indexOf("全局摘除规则") >= 0, "明细缺全局规则卡片");
check(detail.indexOf("&lt;safety_override&gt;") >= 0, "明细没有渲染受保护块");

// 会话级摘除开关状态 + 点击回写
const tagBoxes = getEl("detail").querySelectorAll("input[data-tag]");
const emotionBox = tagBoxes.filter(function (el) { return el.getAttribute("data-tag") === "emotion_system"; })[0];
const memoryBox = tagBoxes.filter(function (el) { return el.getAttribute("data-tag") === "long_term_memory"; })[0];
check(!!emotionBox && emotionBox.checked, "会话已摘除的 emotion_system 开关没勾上");
check(!!memoryBox && !memoryBox.checked, "long_term_memory 只是全局规则，会话开关不该勾上");
if (emotionBox) {
  emotionBox.checked = false;
  emotionBox.onchange();
  await tick(60);
  const call = bridgeCalls.filter(function (c) {
    return c.method === "POST" && c.endpoint === "trim" && c.body && c.body.tag === "emotion_system";
  }).pop();
  check(!!call && call.body.remove === false && call.body.scope === "session" &&
    call.body.umo === "aiocqhttp:GroupMessage:1001",
    "取消会话摘除没有按 {scope:session, remove:false} 回写：" + JSON.stringify(call && call.body));
}

// 全局规则 chip 点击 = 取消全局摘除
const gchip = getEl("detail").querySelectorAll("[data-gtag]").filter(function (el) {
  return el.getAttribute("data-gtag") === "long_term_memory";
})[0];
check(!!gchip, "全局规则没有渲染可点的 chip");
if (gchip) {
  gchip.onclick();
  await tick(60);
  const call = bridgeCalls.filter(function (c) {
    return c.method === "POST" && c.endpoint === "trim" && c.body && c.body.scope === "global";
  }).pop();
  check(!!call && call.body.tag === "long_term_memory" && call.body.remove === false,
    "点全局 chip 没有按 {scope:global, remove:false} 回写：" + JSON.stringify(call && call.body));
}

// ---------------------------- 人格卡页 ----------------------------
NAV[1].onclick();
await tick(80);
const personaHtml = getEl("personaBody").innerHTML;
check(personaHtml.indexOf("怎么工作的") >= 0, "人格卡页没有说明机制（摘要常驻 + 部分按需取）");
check(personaHtml.indexOf("recall_persona") >= 0, "人格卡页没有说明怎么召回部分");
check(personaHtml.indexOf("小雪") >= 0, "人格卡页没有渲染人格名");
check(personaHtml.indexOf("阿岚") >= 0, "人格卡页没有渲染第二个人格");
check(personaHtml.indexOf("3 个部分") >= 0 || personaHtml.indexOf("个部分") >= 0, "人格卡页没有标出分区数量");
check(personaHtml.indexOf("每轮常驻注入") >= 0, "人格卡页没有标出常驻注入的摘要");
check(personaHtml.indexOf("小雪：猫耳女仆") >= 0, "人格卡页没有渲染摘要");
check(personaHtml.indexOf("行为举止") >= 0, "人格卡页没有列出分区名");
check(personaHtml.indexOf("爱蹭人") >= 0, "人格卡页没有展示分区正文");
check(personaHtml.indexOf("为所有人格生成") >= 0, "人格卡页缺「为所有人格生成」按钮");
check(personaHtml.indexOf("清空全部档案") >= 0, "人格卡页缺「清空全部档案」按钮");
check(personaHtml.indexOf("重写人格卡") >= 0, "已生成的人格没有「重写人格卡」按钮");
check(personaHtml.indexOf("生成人格卡") >= 0, "未生成的人格没有「生成人格卡」按钮");
check(personaHtml.indexOf("未生成（首次对话自动生成）") >= 0, "未生成的人格没标出「首次对话自动生成」");
check(personaHtml.indexOf("生成一次就固定下来") >= 0, "面板没有说明「生成即固化」");
check(personaHtml.indexOf("人格已改，档案未更新") >= 0, "人格正文改过时没有提示「档案未更新」");

// 面板调 AI 生成人格卡（要点确认框，再回写 persona_generate）
const genBtn = getEl("personaBody").querySelectorAll("[data-gen]").filter(function (el) {
  return el.getAttribute("data-gen") === "p2";
})[0];
check(!!genBtn, "人格卡页没有生成按钮");
if (genBtn) {
  genBtn.onclick();
  await tick(20);
  check(getEl("askMask").classList.contains("show"), "生成人格卡没有弹确认框");
  getEl("askOk").onclick();
  await tick(80);
  const call = bridgeCalls.filter(function (c) {
    return c.method === "POST" && c.endpoint === "persona_generate";
  }).pop();
  check(!!call && call.body && call.body.persona_id === "p2",
    "确认后没有调 persona_generate：" + JSON.stringify(call && call.body));
}

// 清缓存按钮也要走确认框
const rebuild = getEl("personaBody").querySelectorAll("[data-rebuild]").filter(function (el) {
  return el.getAttribute("data-rebuild") === "p1";
})[0];
check(!!rebuild, "人格卡页没有清缓存按钮");
if (rebuild) {
  rebuild.onclick();
  await tick(20);
  check(getEl("askMask").classList.contains("show"), "清缓存没有弹确认框");
  getEl("askOk").onclick();
  await tick(60);
  const call = bridgeCalls.filter(function (c) {
    return c.method === "POST" && c.endpoint === "card_rebuild";
  }).pop();
  check(!!call && call.body && call.body.persona_id === "p1",
    "确认后没有调 card_rebuild：" + JSON.stringify(call && call.body));
}

// ---------------------------- 插件开关页 ----------------------------
NAV[2].onclick();
await tick(80);
const pluginHtml = getEl("pluginBody").innerHTML;
// 开关行是 bindPlugins 写进 #pluginList 的（真实 DOM 里它是 pluginBody 的子节点）
const pluginRows = getEl("pluginList").innerHTML;
check(getEl("pluginUmo").value === "aiocqhttp:GroupMessage:1001", "插件页没把会话下拉选到当前会话");
check(pluginHtml.indexOf("全部开启") >= 0, "插件页缺「全部开启」按钮");
check(pluginHtml.indexOf("清除本会话开关") >= 0, "插件页缺「清除本会话开关」按钮");
check(pluginRows.indexOf("astrbot_plugin_humanize_guard") >= 0, "插件页没有渲染插件名");
check(pluginRows.indexOf("astrbot_plugin_ai_mind_simple") >= 0, "插件页没有渲染第二个插件");
check(pluginRows.indexOf("核心") >= 0, "插件页没有标出核心插件");
check(pluginRows.indexOf("人味守卫") >= 0, "插件页没有渲染插件说明");
check(pluginRows.indexOf("已关") >= 0 || pluginRows.indexOf("关</span>") >= 0, "插件页没有标出已关闭的插件");
const plugBoxes = getEl("pluginList").querySelectorAll("input[data-plug]");
const humanize = plugBoxes.filter(function (el) {
  return el.getAttribute("data-plug") === "astrbot_plugin_humanize_guard";
})[0];
check(!!humanize && humanize.checked === false, "已被停用的插件开关没显示为关");
if (humanize) {
  humanize.checked = true;
  humanize.onchange();
  await tick(60);
  const call = bridgeCalls.filter(function (c) {
    return c.method === "POST" && c.endpoint === "plugin_toggle" && c.body && c.body.action === "set";
  }).pop();
  check(!!call && call.body.plugin === "astrbot_plugin_humanize_guard" && call.body.disable === false,
    "打开插件没有按 {action:set, disable:false} 回写：" + JSON.stringify(call && call.body));
}

// ---------------------------- 说明页 ----------------------------
NAV[3].onclick();
await tick(40);
const about = getEl("aboutBody").innerHTML;
check(about.indexOf("设计边界") >= 0, "说明页缺设计边界");
check(about.indexOf("session_plugin_config") >= 0, "说明页缺会话插件开关说明");
// 数据目录要按 HTML 转义后显示（夹具里带 <>，正好顺带验一次转义）
const escDir = String(FIX.overview.data_dir).replace(/[&<>"']/g, function (c) {
  return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
});
check(about.indexOf(escDir) >= 0, "说明页没有显示（转义后的）数据目录");

// ---------------------------- 全局检查 ----------------------------
check(getEl("toast").textContent.indexOf("出错") < 0,
  "过程中弹了接口错误：" + JSON.stringify(getEl("toast").textContent));
check(bridgeCalls.every(function (c) { return String(c.endpoint).indexOf("?") < 0; }),
  "有请求把查询串塞进了 endpoint");
const pageGet = new Set(bridgeCalls.filter(function (c) { return c.method === "GET"; }).map(function (c) { return c.endpoint; }));
["overview", "blocks", "trim", "history", "persona", "plugin_toggle"].forEach(function (e) {
  check(pageGet.has(e), "页面从来没拉过 GET " + e);
});

if (dynamic.length) {
  problems.push.apply(problems, dynamic);
}
report("面板行为（渲染 + 交互 + 请求体）", problems);

console.log("✅ 共 " + bridgeCalls.length + " 次桥调用，全部符合后端约定");
console.log("面板检查通过。");
