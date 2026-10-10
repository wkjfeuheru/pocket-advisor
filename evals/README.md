# FinanceAgent 评测体系

围绕投顾问图（LangGraph 主图）的行为评测：意图路由、多轮改写、信息缺失追问、画像门禁、股票场景分类，外加并发扇出基准。目标是给持续迭代一个可量化、可门禁、可追踪趋势的基线。

## 分层结构

```
evals/
├── datasets/      用例数据（JSON，直接加文件即可扩用例）
├── runners/       可独立执行的评价器（python -m evals.runners.<name>）
├── baselines/     thresholds.json：gates（CI 阻断）+ watch（趋势观察）
└── .cache/evals/results/   运行产物（summary.json / last_live_summary.json）
```

## 运行

```bash
# 离线全套（零 API 成本，CI 安全）
python -m evals.runners.run_all

# live 全套（需要 ADVISOR_MODEL_API_KEY；真实模型 + 假行情数据，不触网）
python -m evals.runners.run_all --live

# 门禁检查（gates 决定退出码；watch 打印趋势）
python -m evals.runners.check_baseline

# 单跑一个 runner
python -m evals.runners.profile_gate_eval
python -m evals.runners.intent_routing_eval --json out.json   # live
```

## 指标口径

| runner | 指标 | 模式 | 口径 |
|---|---|---|---|
| intent_routing_eval | routing_accuracy | live | 期望 agent 集合与实际路由集合精确匹配；分片 clean/chit_chat/noise/trap；per_agent_recall；置信度与对错分组均值 |
| multi_turn_eval | rewrite_accuracy | live | resolved_task 包含全部期望实体且不残留指代词（它/那只/这些…） |
| multi_turn_eval | no_rewrite_precision | live | 首轮/自包含消息应原样放行 |
| clarification_live_eval | clarify_recall | live | 缺参数用例以 kind=clarification 的 interrupt 结束 |
| clarification_live_eval | false_clarify_rate | live | 信息齐全却追问的比例（越低越好） |
| clarification_live_eval | hallucination_rate | live | 该追问却直接编造答案的比例（最严重失败） |
| clarification_live_eval | clarify_field_coverage | live | 澄清载荷 fields 命中预期缺失字段 |
| profile_gate_eval | gate_accuracy | 离线 | personalized 意图缺画像字段时挂起、齐全放行；missing_fields 与实际缺失精确集合匹配 |
| clarification_offline_eval | offline_missing_params | 离线 | screen_candidates 缺 strategy/horizon 必抛 missing_strategy_or_horizon（最终防线） |
| clarification_offline_eval | clarify_contract_pass_rate | 离线 | 澄清载荷契约：kind/version 戳/字段白名单/非法字段拒绝 |
| stock_scenario_eval | scenario_accuracy | live | market_overview / equity_research / stock_screening 三分类 + 子字段（strategy/horizon/count/标的/行业/指数范围） |
| fanout_benchmark | four_domain_speedup | 离线 | 4 意图 Send 并行 vs 串行延迟比 |

噪声用例（intent_routing 的 `noise` tag）在 live 下只要求链路不崩溃（模型自由度高，从宽计分）；离线重放/注入测试可在后续迭代补充。

## 阈值：gates 与 watch

`baselines/thresholds.json` 分两组：

- **gates**（CI 阻断）：离线确定性指标。当前 3 条：画像门禁 ≥1.0、澄清守卫全通过、4 意图加速比 ≥2.5。
- **watch**（趋势观察）：live 模型行为指标，初版阈值宽松。`check_baseline` 会与上次 live 留档（`last_live_summary.json`）对比输出涨跌。

### 迭代流程（加用例 → 调阈值）

1. 往 `datasets/*.json` 加用例（字段约束见 `runners/loader.py` 的校验函数）。
2. `python -m evals.runners.run_all --live` 跑出指标。
3. watch 里稳定达标（连续几次）的指标 → 收紧阈值或升级进 gates；gates 退化 → 修代码回退。
4. 模型/提示词每次改动后跑 live，`check_baseline` 的趋势列直接告诉你哪个指标被影响。

### 升级 live 指标进 gates 的标准

某条 watch 指标连续 3 次运行都稳定高于目标值，且用例集 ≥30 条时，把它从 `watch` 移到 `gates`。在此之前不要用 live 指标阻断 CI——模型输出有天然波动，会制造假失败。

## 扩展点

- **加离线用例**：所有离线 runner 通过 `build_eval_graph`（`_fakes.py`）驱动主图，ScriptedModel 可以注入 classify/rewrite 错误模拟降级。
- **加 live 用例**：`_live.py` 的 `LiveHarness` 是真实模型 + 内存证据库 + 假 Fuyao 数据的完整装配，澄清/画像 interrupt 会被捕获进 `observation["interrupt"]`。
- **路线图（未实现）**：LLM 裁判主观质量分（忠实度/相关性/完整度）——需要 judge 模型调用，成本高，建议只对最终回答抽样评测。
