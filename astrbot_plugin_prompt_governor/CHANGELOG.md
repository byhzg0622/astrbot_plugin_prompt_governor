# 更新日志（CHANGELOG）

版本号采用 1.x（插件自身发布版本，与 AstrBot 版本无关）。
当前版本：**v1.1.2**。

## v1.1.2 — 2026-11-18

### 注入顺序与拦截粒度（修复「注入头与身子分开」）
- **注入头与身子合并**：小于一定字符（仅一行）的注入头/尾会并入相邻大块，保持「注入头+身子」在同一行；超出阈值的块仍保持独立可单独勾选。
- **按行排序显示**：面板按注入在请求里的先后顺序（行顺序）排列所有注入条目，并在每块显示序号，方便核对完整注入内容。
- **勾哪行拦哪行**：面板每一行注入都有独立勾选框，勾选哪一行就拦截哪一行的注入内容。

## v1.1.1 — 2026-11-17

### 新增（工具注入 / 技能注入等漏掉的接口）

- **工具注入通道（`func_tool`）**：旧版只扫 system / prompt / 附加段 / 历史 /
  工具结果 5 个**文本**通道，而 AstrBot 的 tools 定义（工具名、description、
  参数 JSON Schema）走 provider payload 的 `tools` 字段，根本不经过
  system_prompt —— 所以 `context.add_llm_tools(...)`（uapipro 注册了 20+ 个
  工具）和 `@filter.llm_tool` 注册的工具完全不可见。现在逐工具审计：
  - 每个工具一块（`kind=tool`、`where=tool_schema`），含名称、描述、参数 schema；
  - 归属到插件（优先 `handler_module_path`，兜底按工具名前缀猜，如 `uapi_*`）；
  - 相对 base 钩子基线标出「本轮新增 / 基线已有」；
  - 面板按 `🔧 工具名` 展示，并提供「工具注入」标签。
- **工具注入拦截**：规则 `tool:<name>`（会话/全局），命中后从本轮
  `req.func_tool` 里摘掉该工具；核心工具（`astrbot_*` / 核心模块）只审计、
  不可拦截，面板如实标注。
- **技能注入标注**：AstrBot 核心的 `## Skills` 技能清单（Available skills /
  Skill rules）识别为 `kind=skills`，不再混进普通 section。
- **工具调用说明标注**：`TOOL_CALL_PROMPT` 与 skills_like 模式版本识别为
  `kind=tool_prompt`（包括「When using tools:」开头的一段）。
- **钩子之后注入的补审计**：
  - `on_agent_begin`（agent 开始）补记 `[Image N: ...]` / `<system_notice>`
    等核心在 `on_llm_request` 之后才补进来的内容（kind=post_hook，
    stage=agent_begin，不污染请求前 KPI）；
  - `on_agent_done`（agent 结束）补记上下文压缩摘要（`Our previous history
    conversation summary`）、步数耗尽提示、工具轮次新内容。

### 变更

- 面板块清单新增「工具注入 / 技能注入 / 工具说明」标签；工具块开关回写
  `{tag: "tool:<name>"}`。
- 版本号：**v1.1.1**。

### 已知边界

- skills_like 模式的「参数 schema 追发」子请求、插件自建的 LLM 调用
  （主动回复 / 记忆抽取等）不在用户轮钩子可观测范围内，仍看不到。
- 工具注入按「整个工具」拦截；参数 schema 的逐字段改写不做。
- 插件自建 LLM 调用（`context.llm_generate` / 直接 `provider.text_chat`）
  与 skills_like 的「参数 schema 追发」子请求不经过用户轮钩子，仍不可见。

## v1.1 — 2026-11-15

### 修复（「无法读取所有注入并显示」）

- **整轮审计记录丢失**：修复 `core/models.py` 里 `_uncovered_text` 的无效反向
  引用正则（`\1` 无捕获组，Python 直接抛 `re.error: invalid group reference`）。
  旧版只要其他插件往附加段追加/改写内容，本轮审计就会异常且被吞掉，面板表现为
  「有时读得到、有时读不到」。现在改用 `TAG_BLOCK_RE.sub`，不再抛异常。
- **面板把不同注入合并成一行**：`api_blocks` 聚合键从 `tag or kind` 改成
  「tag + 通道」/「kind + 通道 + 文本指纹」。所有无标记块不再互相覆盖文本，
  同 tag 出现在不同通道也会分行显示。
- **KPI 永远 0 → 0**：一轮对话会写「请求前」与「agent 后补审计」两条记录，
  旧版面板取最后一条（agent_done 体积恒为 0）。现在 `history_summary`、
  `overview.last_turn` 与面板 KPI 统一只取请求前记录，轮数不再翻倍。
- **通道漏扫**：
  - `tool_calls_result` 旧版读不存在的 `.calls` 字段 → 工具结果永远看不到；
    现兼容 `to_openai_messages()` 与 `tool_calls_info / tool_calls_result`。
  - `run_context.messages` 的 `content` 是 `list[ContentPart]`（pydantic 对象）
    时被整段跳过；现由 `content_texts` 统一摊平（str / list[dict] / ContentPart）。
  - 无 tag、无锚点的裸文本（核心 system prompt、`# Tools`、插件直接 `+=`、
    `EXTRA_PROMPT.md`、`prompt_prefix`、新增历史消息）旧版根本不产生块；
    现在穷举分段，全部可见。
- **「拦截」假能力**：无标记块旧版没有开关、历史/工具块给了开关却摘不掉。
  现在无标记块可按原文精确拦截；工具结果与历史 assistant/tool 消息如实标成
  「只审计、不拦截」。

### 新增

- **穷举分段扫描**：每个通道（system / prompt / 附加段 / 历史 / 工具结果）的
  全部文本按标题/空行切块（`kind=section`），一段一块，并给出每通道字符/块数
  覆盖统计，保证「所有注入」都能在面板逐段看到全文。
- **来源标注**：base 钩子拍全通道基线，final 钩子标出「本轮新增」（origin=added）
  与「基线已有」（origin=baseline），不再依赖任何标记。
- **按轮查看**：面板新增「按轮看全部注入」视图，选某一轮即可看到该轮请求前
  扫描到的每一个块（不受跨轮聚合影响）。
- **原文拦截规则**：新增 `trim_text_rules`（持久化在 `rules.json`），面板每段
  注入都有开关；`<safety_override>` 等安全底线永远不可摘。
- **agent 多轮补审计**：新增 `on_agent_begin` 快照（`_pg_turn_start_texts`），
  `on_agent_done` 只补记本轮工具轮次新产生的内容，不再把整段历史重复记成注入；
  没有新内容时不写空记录。
- **配置项** `scan.list_untagged`（默认 true）：关闭后退回旧口径（只显示 tag /
  锚点 / 相对基线新增的段）。

### 变更

- 审计记录新增 `stage` 字段（`final` / `agent_done`），面板体积表显示阶段。
- 单块正文记录上限由 4000 字提高到 8000 字，并带 `truncated` 标记。
- 插件版本号统一为 **1.1**（`metadata.yaml`、`main.py`、配置说明同步）。

### 兼容性

- AstrBot 4.5+；`on_agent_begin` / `on_agent_done` / `on_llm_request` 均探测式
  注册，缺失时自动降级，不影响对话。
- 旧数据兼容：`rules.json` 没有 `trim_text_rules` 字段时按空处理；旧审计记录
  没有 `stage` 字段时按「请求前」记录统计。

## v1.0 — 初版

- 注入审计：`<tag>` 标记块 + 已知锚点检测，展示位置、字符数与来源归因。
- 分区人格档案：人格全文压缩成「摘要 + 若干部分」，模型用 `recall_persona`
  按需召回，不再每轮重复注入整份人格。
- 标记块摘除：`<tag>...</tag>` 可按会话/全局摘除，安全底线禁摘。
- 会话级插件开关：直接读写 AstrBot 原生 `session_plugin_config`。
- 面板：注入审计 / 人格卡 / 插件开关 / 说明 四个页签。
