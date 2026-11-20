# 注入管家（astrbot_plugin_prompt_governor）

看清每一轮 LLM 请求里注入了什么，把静态人格变成按需召回的人格卡，按会话拦截不需要的注入块。

当前版本：**v1.1.2** ｜ 更新日志：[CHANGELOG.md](CHANGELOG.md)

## 解决什么问题

- **注入太长**：人格全文 + 各插件注入块每轮全量重复，token 花在不变的内容上；
- **AI 太笨**：注入互相打架、位置不当（如把指令塞进对话历史末尾会洗掉对话感）；
- **黑盒**：出了问题不知道是哪家插件的注入词在捣乱。

## 功能

### 1. 注入审计（默认开，全量）

每轮 LLM 请求拍快照：**5 个文本通道**（system_prompt / 用户 prompt /
附加段 / 历史消息 / 工具结果）+ **工具注入通道**（`func_tool` 里的
tools schema）都会被审计，不再只认 `<tag>`：

- 带 `<tag>...</tag>` 的标记块（强检测）；
- 已知无标记锚点（`# Persona Instructions`、`<system_reminder>`、
  kb_result、侧栏引文…）；
- **技能注入**（AstrBot 核心的 `## Skills` 清单）与**工具调用说明**
  （`TOOL_CALL_PROMPT`）单独标成 `skills` / `tool_prompt`；
- **工具注入**：`context.add_llm_tools(...)`（如 uapipro）、`@filter.llm_tool`、
  MCP、子代理 handoff 等注册的每个工具，按「工具名 + 描述 + 参数 schema」
  一段一块列出，归属到插件，并标出「本轮新增 / 基线已有」——这类注入
  走 provider payload 的 `tools` 字段，不经过 system_prompt，旧版完全看不到；
- **其余全部文本按标题/空行逐段成块**（核心 system prompt、插件直接
  `+=` 的裸文本、改写的 prompt、新增历史消息等）——一段一块，字符数
  可核对，面板上能逐段点开看全文。

base 钩子（先于其他插件执行）拍下全通道基线，final 钩子把「相对基线
新增/改写」的段标成「本轮新增」，核心内容标「基线已有」，来源一眼可辨。
开启人格档案后，面板上还能直接看到「注入前 1500 → 注入后 1140 字符」
这种省下来的体积。

### 2. 分区人格档案（默认关）

开启后：人格**全文**不再每轮进 system_prompt，取而代之的是一张「人格卡」：
**一句话摘要 + 「这份档案有哪些部分」的目录**（行为举止 / 背景故事 / 语言风格 /
关系立场…）。模型需要贴近人设时，用 `recall_persona` 工具**按需只取某一部分**，
而不是每次都拿整份人格。

- 整份人格只在**生成时**交给模型整理一次，产出摘要 + 若干部分，之后长期复用；
- 常驻注入的只有摘要和目录（很短），分区正文只在被召回时才进上下文；
- **面板可直接生成**：WebUI → 人格卡页 → 「生成人格卡 / 重写」，不用先去聊天；
- **未生成的人格，首次对话自动生成一份**；已生成的**不会每轮重新生成**；
- **生成即固化**：档案一旦落库就固定下来，人格正文后来改了也**不自动重算** ——
  面板会标出「人格已改，档案未更新」，要不要更新由你点「重写人格卡」决定；
- 生成失败退回原文人格，**绝不丢人格**；
- 分区可自定义：插件配置 `persona_card.sections`，每行 `key=中文名`
  （如 `behavior=行为举止:语气、口头禅、动作`），留空用默认四区。

模型侧看到的东西长这样（常驻注入，很短）：

```
<persona_card>
小雪：猫耳女仆，软糯黏人、有点怕生。
可选部分（要哪部分就用 recall_persona 的 section 取）：behavior=行为举止（语气、口头禅、动作…）；style=语言风格（句式长短、用词习惯…）
</persona_card>
```

需要「行为举止」的细节时，它调用 `recall_persona(section="behavior")` 只取那一段。

### 3. 注入拦截（默认开）

面板里**每一段注入**都有一个开关：打开 = 本会话每轮摘除该段。

- 带 `<tag>` 的块按标记摘（如 `<emotion_system>`、`<long_term_memory>`）；
- 无标记的块按**原文精确摘**（插件裸文本、核心 prompt 的某一节、附加段
  里的某条提醒都能单独摘，不用整段删）；
- **工具注入**按 `tool:<name>` 拦截：命中后直接从本轮请求的 `func_tool`
  里摘掉该工具（核心 `astrbot_*` 工具只审计、不可摘，面板会标注）；
- `<safety_override>`（危机干预）是安全底线，**永远不允许摘**；
- 规则按会话存，也支持 `*` 全局规则；拦截作用于 system / prompt /
  附加段，以及历史消息里的 **system/user** 注入；历史 assistant/tool
  消息与工具结果只审计、不拦截（改它们会破坏对话与工具调用链，面板上
  会如实标成不可拦）。

### 4. 会话级插件开关

面板直接读写 AstrBot 原生 `session_plugin_config`（与官方"自定义规则"同构），
本会话关闭某个插件 —— 所有注入、handler、LLM 工具一起消失。

## 面板

WebUI →「插件管理」→ 注入管家 →「页面」，四个页签：

| 页签 | 能做什么 |
| --- | --- |
| ① 注入审计 | 左边选会话；右边看最近一轮的 system 注入量（前 → 后）、省下多少、附加段字符数、每个通道的字符/块数覆盖；下面是逐段注入清单：来源归因、位置、字符数、出现轮数、「本轮新增 / 基线已有」，可点开看全文。工具注入按「🔧 工具名」列出并带「工具注入」标签；技能注入 / 工具调用说明也会单独标注。每段都有「本会话每轮拦截」开关（工具按整个工具摘），<safety_override> 这类安全底线永远禁选。另有「按轮看全部注入」：选某一轮，列出该轮请求前扫描到的每一个块（不受跨轮聚合影响） |
| ② 人格卡 | 人格池（标出「未生成／已生成／人格已改」）；点「生成人格卡」直接调 AI 整理（摘要 + 若干部分）；看每份档案的常驻摘要、各分区正文；单份/全部重写或清缓存 |
| ③ 插件开关 | 按会话关/开第三方插件，直接写原生 `session_plugin_config`；核心插件禁改，钩子类插件会打标提醒（会话开关拦不住它的注入，要用摘除） |
| ④ 说明 | 当前生效配置、数据目录、设计边界 |

钩子优先级（priority=+2000 / -2000）让审计看到的是**别的插件注入完之后**的终态；
审计历史只存内存，摘除规则与人格卡缓存在数据目录的 `rules.json`。

## 版本与更新日志

**v1.1.2（当前）**——重点：注入块合并与顺序、精确拦截。

- 新增**工具注入通道**：`func_tool` 里每个工具的 name / description /
  parameters schema 都会审计，uapipro 这类只有 LLM 工具、没有 system
  prompt 注入的插件终于能在面板看到；
- 工具注入可按 `tool:<name>` 拦截（核心工具只审计、不可摘）；
- AstrBot 的 `## Skills` 技能清单与 `TOOL_CALL_PROMPT` 工具调用说明
  单独标注，不再混成普通段落；
- agent 开始/结束补审计额外覆盖图片说明、`<system_notice>`、压缩摘要、
  步数耗尽提示、工具轮次新内容等「钩子之后」才注入的内容。

v1.1 的改动（记录丢失、面板块覆盖、KPI 0→0、穷举分段、按轮查看、原文拦截）
与更早版本见 [CHANGELOG.md](CHANGELOG.md)。

## 安装

放到 AstrBot 的 `data/plugins/` 目录下，重启或重载插件即可。零第三方依赖。

## 设计边界（重要）

- **不改 AstrBot 源码，不 monkey-patch 任何插件**：拦截只作用于本插件自己
  看到的请求文本（system / prompt / 附加段）；要彻底关掉某个插件请用功能 4
  （原生会话规则）。
- 历史消息与工具结果会被**审计**（能看到），但不参与运行时拦截 —— 改历史
  会破坏对话链与工具调用 id。
- 工具注入（tools schema）是结构化数据，不是文本：拦截粒度是「整个工具」，
  不做参数 schema 的逐字段改写；核心工具不允许摘。
- 插件**自建 LLM 调用**（`context.llm_generate`、直接 `provider.text_chat`，
  如主动回复 / 记忆抽取 / 内容审核）不触发 `on_llm_request` 钩子，本插件
  看不到那一路的注入；skills_like 模式的「参数 schema 追发」子请求同理。
- 审计是纯读操作；人格卡与摘除失败时都自动退回原样，不影响正常对话。
- 兼容 AstrBot 4.5+（钩子优先级、`extra_user_content_parts`、`sp` 会话规则均探测式使用）。

## 机制说明

AstrBot 的插件钩子按 priority 降序执行（`star_handlers_registry` 按
`-priority` 排序，见 `astrbot/core/star/star_handler.py`）。本插件注册了：

- `on_llm_request(priority=2000)`：**最先**执行 —— 此时人格已由核心拼好、
  其他插件尚未注入，记录全通道基线；
- `on_llm_request(priority=-2000)`：**最后**执行 —— 其他插件都注入完了，
  穷举扫描终态、执行拦截、人格卡替换，然后记录审计；
- `on_agent_done(priority=-2000)`：agent 结束时读 `run_context.messages`
  （真正发给模型的消息）补记多轮内容；`on_agent_begin` 先记下本轮开局
  消息，用来把「历史旧消息」和「本轮工具轮次新内容」分开。

已知时序边界（AstrBot 4.29.4 实测）：`on_llm_request` 每个用户消息只触发
一次，tool loop 第 2 轮起不再经过；且该钩子之后核心还会做
`prepare_request_images`（补图片说明、重建附加段）与 reset 的消息合并。
这两步由 `on_agent_begin` 补审计（图片说明 / `<system_notice>`），
压缩摘要 / 步数耗尽 / 工具轮次内容由 `on_agent_done` 补审计。

工具注入是独立通道：`func_tool` 里的 tools schema 由 provider 的
`tools` 字段发给模型，不经过 system_prompt；本插件在 final 钩子按工具
逐条审计，拦截时直接从 `req.func_tool` 摘掉对应工具。

## 目录结构

```
astrbot_plugin_prompt_governor/
├── main.py              # 插件入口：4 个钩子（基线/终态/agent 开始/agent 结束）+ recall_persona 工具
├── core/
│   ├── models.py        # 全通道穷举分段 / 标记块 / 基线 diff / 原文摘除 / 人格卡纯函数
│   ├── store.py         # 摘除规则（tag + 原文）与人格卡缓存持久化 + 审计历史
│   ├── audit.py         # 快照解析 + 摘除执行（system/prompt/附加段/历史 system·user）
│   └── persona.py       # 人格档案生成（摘要 + 分区）与分区召回
├── api/webapi.py        # 面板后端（register_web_api）
├── pages/governor/      # 面板页（完整 HTML，走 bridge-sdk.js）
├── tests/               # 单元测试 + 面板沙箱测试
├── CHANGELOG.md         # 更新日志
├── README.md
├── metadata.yaml
└── _conf_schema.json
```

## 测试

在仓库/工作区根目录（能 import 到 `astrbot_plugin_prompt_governor` 的那一层）跑：

```bash
# 后端：core 纯逻辑 + 钩子（astrbot 用替身）+ 面板接口
python -m unittest astrbot_plugin_prompt_governor.tests.test_core \
                   astrbot_plugin_prompt_governor.tests.test_hooks \
                   astrbot_plugin_prompt_governor.tests.test_webapi

# 前端：迷你 DOM 把面板真跑一遍（渲染 / 点开关 / 切页签 / 请求体）
node astrbot_plugin_prompt_governor/tests/panel_check.mjs
```

`tests/fixtures/panel_api.json` 是真实后端接口产出的夹具，可用
`python astrbot_plugin_prompt_governor/tests/_dump_fixtures.py astrbot_plugin_prompt_governor/tests/fixtures/panel_api.json`
重新生成（改了接口字段后要重跑，面板测试会跟着对账）。
