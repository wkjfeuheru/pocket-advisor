# 投顾答案「先过滤、再流式」改造方案

> 目标：把合规闸门从「事后整篇判定」改造成**流式安全（append-only）的过滤器**，从而支持「过滤后的增量流式输出」，同时保持落库文本与流式文本完全一致。

---

## ⚠️ 重要更正：合规节点有两套，生产只跑 supervisor 那套

`task_agent.build_advisor_graph()` 只是转发到 [build_supervisor_graph](file:///f:/python project/FA/FinanceAgent/app/agents/graphs/task_agent.py#L476-L479)，`runtime.py` 加载的是它。因此：

- **生产合规节点** = [supervisor.compliance_node](file:///f:/python project/FA/FinanceAgent/app/agents/graphs/supervisor.py#L408-L463)。
- 本文档 §1.1/§3 早期版本描述的 `compliance_node`（`parse_recommendations` + `_TRADE_LANGUAGE` + `ReviewState` 自环 + `model.astream`）属于 **`build_legacy_advisor_graph`**，仅被 `tests/test_agents/test_advisor_graph.py` 使用，**不在生产路径**。

生产节点的真实机制（决定了改造方式完全不同）：

| 维度 | 生产（supervisor） | 旧版（legacy，本文档早期假设） |
|------|--------------------|------------------------------|
| 生成方式 | `structured(model, AnswerDraft)`（function-calling，**无自然语言流**） | `model.astream`（有增量文本） |
| 合规手段 | **整篇校验失败 → 带 feedback 重生成**（最多 3 次），三次失败退化模板句 | 正则改写 + 一次图内自环重写 |
| 数字约束 | 正文**只允许占位符** `{{fact}}`，数字由服务端用已核验事实渲染 | 无 |
| 代码约束 | 代码**只放结构化 `recommendations` 字段**，正文禁止出现 | `推荐代码:` 文本行重解析 |
| `推荐代码:` 行 | **服务端拼接**（非模型输出） | 模型输出，再被重解析 |

**含义**：生产节点既"不可流式"（结构化输出），合规也**不是"过滤后可放行"**，而是"不合规就重写整篇"。因此早期方案的 P2/P3（在 `messages` 流上套 guard）**对生产无效**，需按下文 §3 重新选型。

---

## 1. 问题与约束

### 1.1 现状（结论）
- [public_token()](file:///f:/python project/FA/FinanceAgent/app/agents/graphs/task_agent.py#L106-L122) 已定义"只放行 `compliance_node` 增量"的**源过滤**，但从未在生产路径调用。
- [agent_service._stream_item()](file:///f:/python project/FA/FinanceAgent/app/services/agent_service.py#L847-L860) 丢弃全部 `messages` 流，只在落库后发**一个**完整 `token`。
- [compliance_node](file:///f:/python project/FA/FinanceAgent/app/agents/graphs/task_agent.py#L365-L440) 的三道闸里，两道是**全文函数**：
  - [_CODE_LINE + parse_recommendations](file:///f:/python project/FA/FinanceAgent/app/agents/graphs/task_agent.py#L50-L50)：解析「推荐代码: …」**整行**，与 `suitability.allowed/blocked` 比对，越权则触发一次**重写回环**。
  - [_TRADE_LANGUAGE.sub()](file:///f:/python project/FA/FinanceAgent/app/agents/graphs/task_agent.py#L43-L43)：全局正则删除交易性用语。
  - 末尾追加 `SIMULATED_DISCLAIMER`（后缀操作，可流式）。

### 1.2 为什么现状不能直接流式
流式要求输出是**单调前缀**（发出即不可变）；而当前闸门需要"看到整段/整行"才能判定，且判定失败会**回溯重写**。二者语义冲突。

### 1.3 关键有利事实（决定了改造可行性）
1. **白名单事前已知**：[suitability_guard](file:///f:/python project/FA/FinanceAgent/app/agents/graphs/task_agent.py#L327-L356) 在 `compliance_node` **之前**执行，`suitability.allowed/blocked` 在生成答案时已经确定。
2. **推荐裁定其实是"逐码可判"**：语义上只要求"每个出现的推荐代码 ∈ allowed"，这是**点式判定**，不需要整行；现在必须整行，只是解析实现的选择。
3. **allowed 只含商品代码**（[AGGREGATOR_SYSTEM_PROMPT](file:///f:/python project/FA/FinanceAgent/app/agents/prompts/task_agent_prompts.py#L63-L70)：禁止写入股票代码）；且这些代码来自 worker **经服务端核验**的 `recommended_codes`，集合很小且有界。
4. **前端已就绪**：[reduceTurn 累加 token](file:///f:/python project/FA/FinanceAgent/frontend/src/features/chat/state.ts#L159-L161) + [renderMarkdownStreaming](file:///f:/python project/FA/FinanceAgent/frontend/src/shared/markdown.ts#L63-L103) 已实现"已闭合块渲染 HTML、未闭合块保持纯文本"。前端**零改动**。

---

## 2. 核心设计：可证明的流式安全过滤器

新增 `app/agents/compliance_guard.py`：`StreamingGuard`（纯函数式，无 IO，便于单测）。

### 2.1 契约
- **(P1) 前缀单调**：输出只追加，永不修改已输出内容。
- **(P2) 切分无关**：任意 chunk 切分下，`concat(guard(chunk_i))` 恒等于 `guard(concat(chunks))`。
- **(P3) 与一次性过滤一致**：`guard.finalize()` 的结果与对全文调用同一规则一致。

### 2.2 规则与 holdback
| 规则 | 内容 | 可流式化方式 |
|------|------|--------------|
| R1 禁词删除 | 词典 `D_TRADE`（现有 7 个词，maxlen=4） | holdback = `maxlen - 1 = 3`；任何长度 ≤ maxlen 的匹配若跨边界，起点必落在 holdback 内，不会被提前放出 |
| R2 协议行处理 | `推荐代码:` 到行尾 | 行级延迟：命中起始标记后，把该行内容留在缓冲，仅在行结束（`\n` 或 `finalize`）时按白名单裁剪后放出 |

R2 的两种产品形态（见 §4 Q1）：
- **(a) 不展示**：整行从用户可见流中剥离，仅服务端抽取用于裁定 → 实现最简单。
- **(b) 展示**：延迟到行末，按 `allowed` 逐码裁剪（丢弃不在 allowed 的码），再放出。

### 2.3 接口草图
```python
class StreamingGuard:
    def __init__(self, allowed_codes: Iterable[str], *, strip_recommendation_line: bool): ...

    def push(self, delta: str) -> str:
        """吃入原始增量，返回本次可安全放出的合规增量（append-only）。"""

    def finalize(self, tail: str = "") -> str:
        """流结束：冲掉缓冲（含未换行的协议行），返回尾部合规增量。"""

    def warnings(self) -> list[str]:
        """本次被拦截的项（禁词命中数、越权代码），供 warning 事件使用。"""
```

### 2.4 一致性铁律
`final.answer`（落库、历史、`done` 事件）**必须**等于 `"".join(所有 token 增量)`。
做法：整条链路只用**同一个 guard 实例**驱动的结果——
- service 侧用 guard 处理 `compliance_node` 的原始 chunk → 产出 `token` 事件；
- 循环结束后用 `guard.finalize()` 对原始 `final.answer` 做**同源归一**，得到权威答案。

> 由于 P2（切分无关）+ 纯函数，两次运行结果必然一致。

---

## 3. 架构选型（已按生产节点重写）

生产 `compliance_node` 的两个硬约束：**① 结构化输出 → 无自然语言流；② 合规 = 整篇重生成**。因此"给现有节点套一个 guard"这条路不存在，只能在下列三条里选。

### S1｜不做 token 流式（保持现状，最小风险）
- 维持 `structured(AnswerDraft)` + 三次重生成 + 占位符事实替换，**不合规就不放行任何文本**。
- 等待体感交给前端（阶段徽标 + "生成中"占位，已实现）+ 已加的 SSE 心跳。
- 代价：等待期仍无增量文字。**推荐作为默认**，因为它完整保留防幻觉保证。

### S3｜段级流式（推荐，若确需增量）
> **已被 §9「V-b」取代**：S3 是思路原型，V-b 是选定方案（两阶段：隐藏骨架 + 受约束流式成文）。以 §9 为准。

把"整篇生成 + 整篇校验"改为**逐段生成 + 逐段校验 + 逐段放行**：

1. 让模型按固定分段协议输出（例如分节/分句的 `AnswerDraft` 列表），每段独立生成。
2. 每段**沿用现有全部校验**（`_FORBIDDEN`、占位符 ⊆ `facts`、正文无其他数字/代码、`recommendations ⊆ allowed`）。
3. 合规段立即 `token` 放行；不合规段**只重生成该段**（局部重试，不回溯已放行内容）。
4. 每段校验需要**有限前瞻**（等待 `}}` 闭合占位符、等待禁语词长窗口）——复用 §2 的 holdback 技巧，但作用域是**段内**而非整篇。
5. `推荐代码:` 行仍由服务端拼接，且**不进用户可见流**（Q1：不展示）。

- 收益：真正的"先过滤、再流式"，且**不牺牲**校验强度。
- 代价：多次模型调用（每段一次），端到端延迟上升；需定义分段协议与"段内重试"上限。
- 这一条才是与 Q2「取消重写」兼容的形态：取消的是**整篇重写**，改为**段级重试**。

### S2｜草稿先行 + 定稿覆盖（不推荐）
先流式展示未过校验的自然语言草稿，定稿后覆盖（需 `reset` 事件）。
- 直接把"未过校验内容"投给用户，与本项目合规取向冲突；且与 Q2 相悖。**排除**。

### 3.4 与早期 §2 的关系
§2 的 `StreamingGuard`（holdback + 协议行延迟）**仍然有效**，但作用域从"整篇自由度"收窄为 **S3 的段内过滤**；`_CODE_LINE`/`parse_recommendations`/`_TRADE_LANGUAGE` 属于 legacy，生产不需要迁移。

### 3.5 淘汰的旧结论
- ❌ "`_stream_item` 接入 `public_token` + guard 即可流式" —— 生产节点不产生 `messages` 增量，接线也不会有点。
- ❌ "删除 `route_after_compliance` 自环" —— 生产图本就没有该自环（`compliance_node → END`）。
- ❌ "取消 `ReviewState` 重写 = 合规改为屏蔽" —— 生产没有 `ReviewState`，重写是 `for _ in range(3)`。

---

## 4. 待拍板事项

### Q1「推荐代码: …」行是否展示给用户？
- **(a) 不展示（推荐）**：它是给校验器的**协议行**，展示价值低且是流式风险源。选它 → guard 只需 R1 + 整行剥离，`_CODE_LINE` 退役。
- **(b) 展示**：保留行级延迟 + 逐码裁剪，复杂度略高但可控。
- **默认 (a)**，保持后端裁定能力不变，只改"是否进入用户可见流"。

### Q2 是否保留"重写"能力？
- 推荐：**取消**由文本越权触发的重写（改为屏蔽），保留一次结构性问题重写为可选。
- 影响：`ReviewState.needs_rewrite` 语义收窄；相关测试同步。

### Q3 灰度开关
在 `settings_models` 增加 `advisor_stream_tokens: bool`（默认 false=保持 C1 现状），便于线上灰度与回滚。

---

## 5. 触点清单（按文件）

| 文件 | 改动 | 原因 |
|------|------|------|
| `app/agents/compliance_guard.py` | **新增** `StreamingGuard` | 流式安全过滤的核心，纯函数便于测试 |
| [task_agent.py](file:///f:/python project/FA/FinanceAgent/app/agents/graphs/task_agent.py) | `compliance_node` 用 guard 归一答案；`route_after_compliance` 恒 `END`；`parse_recommendations`/`_TRADE_LANGUAGE` 职责迁至 guard（保留导出以兼容测试，标注 deprecated） | 去掉全文/回溯语义 |
| [agent_service.py](file:///f:/python project/FA/FinanceAgent/app/services/agent_service.py) | `_stream_item` 接入 `public_token` + guard → 发增量 `token`；`_consume_graph` 持有 guard 并归一 `final.answer`；受 `advisor_stream_tokens` 开关控制 | 打通源过滤 + 内容过滤 |
| `app/core/settings_models.py` | 新增 `advisor_stream_tokens` | 灰度/回滚 |
| 前端 | **无改动** | 已支持增量 token 与分块渲染 |

---

## 6. 测试计划

### 6.1 新增 `tests/test_agents/test_compliance_stream_guard.py`
- **切分无关性**（P2）：同一文本按随机/逐字切分，输出拼接 == 一次性输出。
- **跨 chunk 禁词**：`"买"` + `"入"` 两个 chunk 必须被拦截。
- **协议行延迟**：`推荐代码: 600519, 000001` 分片输入时，行未结束前不放出；行结束后按 allowed 裁剪。
- **幂等与 finalize**：`finalize()` 补齐尾部；空输入不产出空串。
- **越权代码**：不在 allowed 的商品码被丢弃并计入 warnings。

### 6.2 需同步修改的既有测试
- `test_final_answer_is_streamed_once_after_validation`、`test_only_guard_approved_final_answer_is_streamed_as_tokens`：断言从 `len(tokens) == 1` 改为 **"token 合计 == 权威答案"** 且不含 `EXPERT_SECRET`。
- `test_stream_event_order_hides_expert_output`：保留 `run < status < token < done` 与 `EXPERT_SECRET not in body`（顺序契约不变）。
- `test_artifact_event_is_emitted_before_token_and_restored_in_metadata`：语义保持（artifact 在 `done` 之前）。
- [FakeGraph.astream](file:///f:/python project/FA/FinanceAgent/tests/test_agents/test_advisor_service.py#L86-L92)：改为能产出**多个** `compliance_node` chunk，以覆盖真实增量路径。

### 6.3 手工联调
- 发起一次含商品推荐的问答，观察：文字逐段出现、Markdown 表格/列表实时成形、`**` 不裸露、`推荐代码:` 行按 Q1 决策处理。
- 完成/刷新后比对：历史消息内容 == 流式结束时屏幕内容（一致性铁律）。
- 触发行内长任务验证"停止"仍可中断。

---

## 7. 风险与回滚

| 风险 | 缓解 |
|------|------|
| holdback 漏拦跨边界禁词 | holdback 放大到 8（远大于 maxlen-1）；专项单测（§6.1） |
| 流式文本与落库文本不一致 | 单一 guard 实例 + P2 切分无关性单测；`done` 事件与流式同源 |
| 协议行裁剪导致语句不通顺 | 沿用兜底话术 + warning；Q1(a) 下彻底规避 |
| 线上异常 | `advisor_stream_tokens` 开关一键回退到 C1（只发一个 token） |
| 事件顺序被打破 | `run → status* → (token)* → artifact* → warning* → done` 顺序由 `_consume_graph` 保证，测试锁定 |

---

## 8. 分阶段落地

1. **P1｜guard 独立可测**：新增 `compliance_guard.py` + 单测（不接线，零行为变化）。
2. **P2｜图内归一**：`compliance_node` 用 guard 归一 `final.answer`，删除重写回环；跑通既有后端测试（此时仍只发一个 token）。
3. **P3｜打通流式**：`_stream_item` 接入 `public_token` + guard 发增量 token，受开关控制；同步更新 §6.2 测试。
4. **P4｜前端增强（可选）**：生成中光标、逐段淡入；无需改数据流。
5. **P5｜灰度**：`advisor_stream_tokens` 默认关 → 小流量开启 → 全量。

每阶段结束即跑：`pytest tests/test_agents/test_advisor_service.py tests/test_agents/test_advisor_graph.py tests/test_routers -q`。

> 注：§8 的 P2/P3 基于 legacy 节点，已被 §9 的落地顺序取代。

---

## 9. 选定方案 V-b 详细设计（隐藏骨架 + 受约束流式成文）

> 决策前提：Q1 协议行不展示；Q2 取消**整篇重写**（改为骨架字段级重试）；Q3 无灰度开关，直接切换。
> 已验证：`langgraph 1.2.9` 提供 `from langgraph.config import get_stream_writer`，故传输层用官方 `custom` 流模式，无需自建 sink/队列。

### 9.0 一句话
把合规对象**从"自然语言文本"下沉到"结构化骨架"**：隐藏阶段产出并校验骨架，可见阶段在骨架约束下做自然语言**流式**生成，并用**预防式过滤器**保证流出去的每个前缀都合规。

### 9.1 两阶段流水

| 阶段 | 节点 | 是否可见 | 生成方式 | 合规手段 |
|------|------|----------|----------|----------|
| 草稿（骨架） | `plan_node` | **否**（只发 `status`） | `structured(model, AnswerPlan)` | **字段级**校验 + 字段级局部重试；不合规字段丢弃 |
| 正式（成文） | `answer_node` | **是** | `model.astream` | **预防式过滤器**（占位符渲染 + 数字/代码/禁语拦截） |

图结构变化（`supervisor.py`）：
```
suitability_guard → plan_node → answer_node → END
```
（原 `compliance_node` 拆成这两个节点；`for _ in range(3)` 整篇重写退役。）

### 9.2 骨架 Schema（`state.py` 新增）

```python
class PlanPoint(BaseModel):
    point: str = ""                    # 要点；禁含数字/代码/禁语
    fact_keys: list[str] = []          # 必须 ⊆ facts 的键

class PlanSection(BaseModel):
    heading: str = ""                  # 段标题，可空
    points: list[PlanPoint] = []

class AnswerPlan(BaseModel):
    sections: list[PlanSection] = []
    recommendations: list[str] = []    # 必须 ⊆ suitability.allowed
```
`AnswerDraft` 标注为 legacy（legacy 图仍用，不删）。

### 9.3 草稿阶段校验（字段级，不重写整篇）

对 `plan_node` 的输出逐字段判定，**只重试出错字段**（每个字段 ≤1 次），判定项：

| 校验 | 违规处置 |
|------|----------|
| `point.fact_keys ⊆ facts.keys()` | 剔除越权键 + 该字段带 feedback 重试一次；仍越权则键置空 |
| `recommendations ⊆ suitability.allowed` | 直接剔除越权码（不重试）+ 记 `warning` |
| `heading` / `point` 命中 `_FORBIDDEN` / `_NUMBER` / `_CHINESE_NUMBER` | 该字段带 feedback 重试一次；仍违规则**丢弃该 point/section** |
| 最终 `plan` 为空 | 走现有兜底模板（保持今天行为，不放 token） |

> 收益：整篇重写被替换为字段级修补，且与"隐藏阶段不显示"天然一致（用户看不到任何重试痕迹）。

### 9.4 正式阶段：受约束流式 + 预防式过滤器

**Prompt 约束**（喂 `plan` + 允许的 `facts`，含 placeholder 与已渲染值）：
- 任何数字 / 日期 / 代码**必须写 `{{key}}`**，逐字取自 facts；
- 正文不得出现其他阿拉伯数字、中文数词、证券代码、禁语；
- 按 `plan.sections` 顺序成文。

**`StreamingGuard`（新增 `app/agents/compliance_guard.py`，纯函数）**

| 规则 | 处理 | 可流式化依据 |
|------|------|--------------|
| `{{key}}` 占位符 | 缓冲至 `}}`；`key ∈ facts` → 放出渲染文本；否则整段丢弃 + warning | 需看到 `}}`，前瞻有界 |
| ASCII 数字（占位符之外） | 丢弃 + warning | 单字符判定，无需前瞻 |
| `_CHINESE_NUMBER` 中文数词 | holdback 后丢弃 + warning | 固定词典，holdback = maxlen-1 |
| `_FORBIDDEN` 禁语 | holdback 后丢弃 + warning | 同上 |

- holdback 取值：所有词典/模式 `maxlen` 的最大值（保守取 **12**），保证跨 chunk 命中不被提前放出。
- 契约（单测锁定）：**P1 前缀单调**、**P2 切分无关**、**P3 与一次性过滤一致**。

**服务端后缀**（流结束后由服务端拼接，**不进 guard 作用域**；内容全部来自已校验字段，天然安全）：
`pending question` → `turn_warnings` → `推荐代码: …`（取自 `plan.recommendations`，已 ⊆ `allowed`）→ `SIMULATED_DISCLAIMER`。

> **对 Q1 的更正（重要）**：`ProductResult` 没有 artifact 投影器（[artifacts.py](file:///f:/python project/FA/FinanceAgent/app/agents/artifacts.py#L16-L20) 只登记了 `stock_agent` / `portfolio_agent`），**`推荐代码:` 行是商品推荐码唯一对用户可见的出口**。生产节点里该行本就由服务端从白名单拼接、**非模型文本**，因此**保留展示既安全又不损失功能**。
> 结论：**撤回 Q1 的"不展示"**，改为"保留展示，且因内容来自已校验字段，无需行级延迟过滤"。guard 因此更简单：只需拦截**散文里**的数字/代码/禁语（prompt 本就禁止散文出现代码）。
> 若确实不想展示推荐码，则需另建结构化出口（给 `ProductResult` 加 projector + 前端卡片），工作量明显更大——请另行确认。

### 9.5 传输：LangGraph `custom` 流模式

- `answer_node` 内：
  ```python
  writer = get_stream_writer()
  async for chunk in model.astream(messages, get_config()):
      delta = guard.push(text_of(chunk))
      if delta:
          writer({"text": delta})      # 已过滤的增量
          parts.append(delta)
  ```
- `agent_service._consume_graph`：`stream_mode=["updates", "messages", "custom"]`；
  `_stream_item` 处理 `("custom", {"text": ...})` → 发 `token` 事件。
- 未启用 `custom` 模式时 `get_stream_writer()` 为 no-op（`_no_op_stream_writer`），老调用路径安全。
- 因**节点内已完成过滤**：service **不需要 facts**，也**不做二次过滤** ⇒ 单一 guard 实例，天然一致。

### 9.6 事件契约

```
run → status("核对事实与结构")        # plan 阶段（含字段级重试，全程无 token）
    → status("生成回答")
    → token*                          # answer 阶段，受约束流式，经 guard
    → artifact → warning* → done
```
与现有前端（`reduceTurn` 累加 token、`renderMarkdownStreaming` 分块渲染）**零改动兼容**；`_sse` 心跳（已加）覆盖 plan 阶段的静默期。

### 9.7 一致性铁律

`"".join(所有 token 增量)` **恒等于** `FinalAnswer.answer`（落库、历史、`done`）。做法：`answer_node` 用**同一 guard 实例**既推送增量又累积 `parts`，`answer = "".join(parts) + suffix`。

### 9.8 降级路径

- `plan` 失败/最终为空 → 兜底模板，零 token（= 今天行为）。
- `answer_node` 中途异常：已推 token 保留（append-only，不可撤）；补 `warning`；若 `parts` 为空 → 兜底模板。
- 三次重生成退役 ⇒ 由「骨架字段级重试 + 流式预防守卫」取代。

### 9.9 触点清单

| 文件 | 改动 |
|------|------|
| `app/agents/compliance_guard.py` | **新增** `StreamingGuard`（纯函数）+ 占位符渲染 |
| `app/agents/graphs/state.py` | 新增 `AnswerPlan/PlanSection/PlanPoint`；`AnswerDraft` 标记 legacy |
| `app/agents/graphs/supervisor.py` | `compliance_node` 拆为 `plan_node` + `answer_node`；改图边；退役整篇重写 |
| `app/services/agent_service.py` | `stream_mode` 增加 `custom`；`_stream_item` 处理 custom → `token` |
| 前端 | **无改动** |
| settings | **无开关**（Q3 直接切换） |

### 9.10 测试改造

- **新增** `tests/test_agents/test_compliance_stream_guard.py`：P1/P2/P3、跨 chunk 禁语（`"买"+"入"` 类分裂）、占位符渲染与非法键丢弃、ASCII 数字拦截、中文数词拦截。
- `tests/test_agents/test_supervisor_v2.py`：`Model` fake 需支持 `AnswerPlan`（`with_structured_output` 分支）与 `Model.astream`（逐 token）；`test_unreferenced_numbers_and_promises_never_reach_final_answer` 改为断言"**token 合计**不含 `99` / `保证收益`"。
- `tests/test_agents/test_advisor_service.py`：`FakeGraph.astream` 增加 `("custom", {...})` 产出；`test_final_answer_is_streamed_once_after_validation` 等改为"**token 合计 == 权威答案**"。
- `tests/test_routers/test_agent_api.py::test_stream_event_order_hides_expert_output`：顺序契约不变，补一条 custom 分支不泄漏专家内容。
- legacy（`task_agent.build_legacy_advisor_graph` 与 `test_advisor_graph.py`）**不动**。

### 9.11 成本与延迟

- 模型调用：**1 次结构化（骨架）+ 1 次流式（成文）= 2 次**（今天是 1 次结构化）。
- 首字延迟 ≈ 骨架时间 + 首句时间，**显著优于**今天"整篇 + 校验"后才出字。
- 残余风险：散文层仍可能出现**语义层面**的越权（用允许的 fact 讲出不合适的结论）—— 这部分过滤器不负责，继续靠 prompt + `suitability` 白名单约束。

### 9.12 落地顺序（每步跑测试）

1. **P1**：新增 `compliance_guard.py` + 单测（不接线，零行为变化）。
2. **P2**：`plan_node` 接入并替换整篇重写；`answer_node` 先"一次性输出"（暂不流式）→ 验证正确性与骨架校验。
3. **P3**：`answer_node` 改 `model.astream` + `get_stream_writer` + `custom` 流 → 真流式。
4. **P4**：测试改造（§9.10）+ 手工联调（观察逐段成文、占位符渲染、数字不泄漏、`推荐代码:` 行不出现）。

每步：`pytest tests/test_agents/test_supervisor_v2.py tests/test_agents/test_advisor_service.py tests/test_routers -q`。

---

## 9.13 实施记录（已落地）与契约变更

已按 V-b 实现，全量后端测试 **678 passed / 9 skipped**。

**落地形态**
- 新增 `app/agents/compliance_guard.py`：`StreamingGuard`（holdback=24，占位符渲染 + 数字/中文数词/禁语删除），契约 P1/P2/P3 由 `tests/test_agents/test_compliance_stream_guard.py`（48 条）锁定。
- [supervisor.compliance_node](file:///f:/python project/FA/FinanceAgent/app/agents/graphs/supervisor.py#L514-L559) 内部拆为 `build_plan`（隐藏，`structured(AnswerPlan)` + 字段级校验，最多一次局部修补）+ `render_answer`（可见，`model.astream` + `StreamingGuard` + `get_stream_writer`）。
- 图结构不变（仍是 `suitability_guard → compliance_node → END` 单节点），两阶段在节点内部完成 —— **无需新增 state 通道**，`agent_service._graph_input` 免改。
- `agent_service`：`stream_mode=["updates","custom"]`；`_stream_item` 把 custom 增量映射为 `token`，`messages` 模式不再订阅（从源头杜绝草稿外泄）；不再在落库后补发整段 token（否则前端渲染两遍）。
- 尾部（未完成项/告警/免责声明）由节点作为最后一段 token 下发，保证 **`"".join(tokens) == final.answer`**。
- `AnswerDraft` 与 `_PLACEHOLDER` 已删除（无引用）。

**两处契约变更（原测试同步改写）**
1. **正文 token 早于落库**：真流式与"落库前不得下发任何内容"互斥。新增约束：`artifact/warning/done` 仍严格晚于原子落库；`token` 允许先行。若落库被跳过，已流出的正文**不可撤回**，只以 `error(turn_state_changed)` 收尾（前端呈现"本轮没有完成"）。对应测试：`test_completion_payloads_are_emitted_only_after_atomic_context_completion`、`test_skipped_completion_emits_no_completion_payloads`。
2. **骨架取代整篇重生成**：合规由"整篇校验失败就重跑（≤3 次）"改为"骨架字段级校验 + 流式预防式过滤"；违规内容被**拦截/剔除**而非触发重写。

**验证**
- 单测：`python -m pytest -q` → 678 passed / 9 skipped。
- 前端：无改动（已支持增量 token 与分块渲染），此前 46 条前端测试与构建保持通过。
- 未做：真实 LLM 端到端联调（需配置模型与数据源）；`evals/runners/_fakes.py` 已同步到 `AnswerPlan`+`astream`，但评测脚本未实跑。

## 9.14 数字校验精度与正文引用修复

保留隐藏骨架与流式成文流程，骨架、正文和股票摘要共用业务数值识别规则。列表编号、五日／一季等分析周期以及“四舍五入”允许直接表达；未引用的价格、收益和财务数值继续拦截。周期与编号白名单不能掩盖日元等金额。

`StreamingGuard` 缓冲到完整句界后核验并发布，违规时移除该语句，保留其他语句。不再逐个删除数字留下残句，也不依赖固定字符窗口。无句界的内容在流结束时核验，首个增量因此可能等待完整句子。股票摘要同样仅剔除无法核验的语句，全篇没有可用内容时才采用固定摘要。

正文使用 `{{key|metric}}` 引用，服务端核验声明指标及附近的指标标签；旧 `{{key}}` 仍兼容。正文默认仅展开数值与单位，例如“现价 297.75元”，详细事实摘要才重复标的、指标与截止时间。成文输入保留事实截止时间。相同尾随单位去重，不同单位或指标拒绝整个语句并记录告警。

确定性验收见 `tests/test_agents/test_compliance_precision.py`，覆盖无需重试的分析周期骨架、股票摘要分层保留、引用错配、单位处理、白名单边界及任意流式切分一致性。
