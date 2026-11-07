# 注入管家（astrbot_plugin_prompt_governor）

看清每一轮 LLM 请求里注入了什么，把静态人格变成按需召回的人格卡，按会话摘除不需要的注入块。

## 解决什么问题

- **注入太长**：人格全文 + 各插件注入块每轮全量重复，token 花在不变的内容上；
- **AI 太笨**：注入互相打架、位置不当（如把指令塞进对话历史末尾会洗掉对话感）；
- **黑盒**：出了问题不知道是哪家插件的注入词在捣乱。

## 功能

### 1. 注入审计（默认开）

每轮 LLM 请求拍快照：人格块、带 `<tag>` 标记的注入块、位置（system_prompt /
用户消息附加段）、字符数、归因。开启人格档案后，面板上能直接看到
「注入前 1500 → 注入后 1140 字符」这种省下来的体积。面板（WebUI → 插件页 → 注入管家）能看到
「最近 20 轮聚合」的块清单，点开看全文。

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

### 3. 标记块摘除（默认开）

面板里每个带 `<tag>` 的注入块有一个开关：打开 = 本会话每轮摘除该块
（如 `<emotion_system>`、`<long_term_memory>`）。

- 只摘带标记的块；无标记的注入摘不了（这是设计边界，不做运行时代码改写）；
- `<safety_override>`（危机干预）是安全底线，**永远不允许摘**；
- 规则按会话存，也支持 `*` 全局规则。

### 4. 会话级插件开关

面板直接读写 AstrBot 原生 `session_plugin_config`（与官方"自定义规则"同构），
本会话关闭某个插件 —— 所有注入、handler、LLM 工具一起消失。

## 面板

WebUI →「插件管理」→ 注入管家 →「页面」，四个页签：

| 页签 | 能做什么 |
| --- | --- |
| ① 注入审计 | 左边选会话；右边看最近一轮的 system 注入量（前 → 后）、省下多少、附加段字符数；下面是注入块清单：来源归因、位置、字符数、出现轮数，可点开看全文。每个带标记的块有「本会话每轮摘除」开关，<safety_override> 这类安全底线永远禁选 |
| ② 人格卡 | 人格池（标出「未生成／已生成／人格已改」）；点「生成人格卡」直接调 AI 整理（摘要 + 若干部分）；看每份档案的常驻摘要、各分区正文；单份/全部重写或清缓存 |
| ③ 插件开关 | 按会话关/开第三方插件，直接写原生 `session_plugin_config`；核心插件禁改，钩子类插件会打标提醒（会话开关拦不住它的注入，要用摘除） |
| ④ 说明 | 当前生效配置、数据目录、设计边界 |

钩子优先级（priority=+2000 / -2000）让审计看到的是**别的插件注入完之后**的终态；
审计历史只存内存，摘除规则与人格卡缓存在数据目录的 `rules.json`。

## 安装

放到 AstrBot 的 `data/plugins/` 目录下，重启或重载插件即可。零第三方依赖。

## 设计边界（重要）

- **不改 AstrBot 源码，不 monkey-patch 任何插件**：摘除只作用于带 `<tag>` 标记的
  文本；要彻底关掉某个插件请用功能 4（原生会话规则）。
- 审计是纯读操作；人格卡与摘除失败时都自动退回原样，不影响正常对话。
- 兼容 AstrBot 4.5+（钩子优先级、`extra_user_content_parts`、`sp` 会话规则均探测式使用）。

## 机制说明

AstrBot 的插件钩子按 priority 降序执行（`star_handlers_registry` 按 priority
排序）。本插件注册了两个 `on_llm_request` 钩子：

- `priority=2000`：**最先**执行 —— 此时人格已由核心拼好、其他插件尚未注入，
  记录基线；
- `priority=-2000`：**最后**执行 —— 其他插件都注入完了，解析终态、执行摘除、
  人格卡替换，然后记录审计。

## 目录结构

```
astrbot_plugin_prompt_governor/
├── main.py              # 插件入口：两个钩子 + recall_persona 工具
├── core/
│   ├── models.py        # 标记块解析 / 摘除 / 人格拆分（纯函数，可单测）
│   ├── store.py         # 规则与人格卡缓存持久化 + 审计历史
│   ├── audit.py         # 快照解析 + 摘除执行
│   └── persona.py       # 人格档案生成（摘要 + 分区）与分区召回
├── api/webapi.py        # 面板后端（register_web_api）
├── pages/governor/      # 面板页（完整 HTML，走 bridge-sdk.js）
└── tests/               # 单元测试 + 面板沙箱测试
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
