# Finance Agent

金融投顾**业务系统**：模拟交易（商品、持仓、账户）与只读智能投顾对话，配 Vue 3 界面与 FastAPI 接口。**模拟盘，不对接券商，不构成投资建议。**

普通用户可以在 `/advisor` 与投顾助手对话。助手只读取商品、账户和持仓，不能充值、下单、赎回或清仓；AI 配置建议只写反事实账本（`ai_*` 表），用户真实持仓与流水仍不被写入。管理员仍只使用管理后台，不能进入投顾。

## 功能特性

- **用户系统**：注册、登录、Bearer Token 认证、会话管理与账户注销（PBKDF2-SHA256 + 随机盐）。
- **商品货架**：基金产品按最新净值展示，含风险等级、费率、建议持有期与上下架状态；取不到净值的商品照常展示但标记不可交易。
- **模拟交易**：按金额/份额申购、持仓按最新净值估值、部分或全部赎回、一键清仓、虚拟资金充值（幂等键防重复入账）。
- **账户面板**：总资产、可用资金、持仓市值、累计与已实现盈亏；任一持仓缺净值时登记 `pricing_issues`，不把市值静默算小。
- **管理后台**：全站用户与持仓总览、商品发行/编辑与软上下架（下架商品不可申购但既有持仓仍可赎回）。
- **单一计算源**：账户与盈亏口径只有一份实现（`app/services/portfolio_service.py`），REST 与管理后台只读该口径。
- **角色互斥**：普通用户走 `/api/portfolio/*`、交易界面和 `/advisor`；管理员走 `/api/admin/*` 与后台。登录共用，登录后立刻分流。管理员 token 调用用户交易或投顾接口返回 403。
- **只读投顾**：LangGraph 工作流做意图识别，再并行股票子图 / 商品 / 持仓 / 问答。股票子图覆盖市场总览、1–3 只个股深研和研究候选池。行情或快照局部故障时按场景降级，不编造数字。
- **持仓配置建议（反事实账本）**：对话中可选择对持仓做配置优化，生成 light / moderate / rebuild 三档方案（含换手率、费用、风险与集中度前后对比）；用户采纳后调仓结果写入**与用户持仓完全隔离的 AI 账本**，不触达 `finance.positions` / `orders` / `cash_transactions`。AI 线可与用户线做归一净值对比。

## 投顾工作流

```text
START → classify_intent → profile_gate → dispatch_tasks
      → stock / product / portfolio / chat（只派发命中的分支）
      → suitability_guard → compliance_node（生成 + 合规校验）→ END
```

- 个性化建议缺少风险、期限、目标或流动性时，原轮次会挂起。用户在表单里确认后，用原来的 `request_id` 恢复，不必重发问题。涉及金额配置时还要预算。模型不能改写长期画像。
- 股票标的歧义、一次深研超过 3 只、或选股缺少策略/周期时，走独立的澄清中断（`clarification_required`），与画像中断分开。
- `portfolio_agent` 节点在服务运行时注入了配置分支（`allocation_runner`）。命中持仓优化意图时，该节点生成三档方案并 `interrupt` 等用户采纳；未注入或缺少可信 `customer_id` 时回退为普通持仓查询分支，避免整轮失败。
- 高于用户风险等级的商品可以解释，但不会进入推荐集合。研究候选股票走 `candidate_stocks`，不会进入商品适配性门禁。
- 会话 id 就是 LangGraph `thread_id`。图在每个节点边界以同步耐久模式写入 PgSaver。重复 `request_id` 不会再保存用户消息，也不会重跑已完成轮次。失败且 `retryable` 的轮次从最近 checkpoint 重试。
- PgSaver 自己的 `checkpoint_*` 表是基础设施，不在业务 ORM 和业务查询里。删除会话时会调用 `adelete_thread` 清掉对应 checkpoint。
- 启动时会把超时仍处于 `pending` / `running` 的轮次标成可重试。画像确认中的轮次不会被超时扫掉。
- 模型或 PgSaver 不可用时，投顾接口返回 503，商品、账户、交易和管理接口照常可用。

## 技术栈

- Python 3.10+
- FastAPI / Uvicorn
- SQLAlchemy 2.x（ORM） + psycopg3
- PostgreSQL，以及 LangGraph `AsyncPostgresSaver`
- APScheduler 3.x（净值同步调度，缺失或启动失败时降级）
- LangGraph / LangChain / OpenAI-compatible Chat Model
- Pydantic v2
- Vue 3 / TypeScript / Vite / Element Plus / Axios

## 项目结构

```text
FinanceAgent/
├── app/
│   ├── main.py                 # FastAPI 入口、路由注册、lifespan 初始化 Agent
│   ├── core/                   # 配置、密码与令牌、鉴权依赖、日志
│   ├── db/                     # Base、session、schema 加载、ORM 模型
│   ├── schemas/                # API 请求/响应
│   ├── crud/                   # 数据访问
│   ├── services/               # 业务逻辑（含盈亏口径、portfolio_advisor 净值/方案/AI 账本/调度）
│   ├── routers/                # HTTP 路由（user / product / portfolio / allocation / admin / agent）
│   ├── agents/                 # 投顾 LangGraph、只读工具、portfolio 配置分支与运行时
│   ├── mcp/                    # Fuyao 白名单客户端、Tushare、MCP 连接
│   └── utils/                  # 错误映射、限流、文本工具
├── scripts/       # 运维/诊断脚本（migrate / bootstrap_admin / 种子 / 漂移诊断）
├── migrations/    # PostgreSQL 编号迁移 SQL（结构唯一事实源）
├── frontend/src/  # Vue 应用（product / portfolio 含 PortfolioCompareView / chat 含 AllocationPlanCards / admin / auth）
├── tests/         # architecture / unit / integration / contract，以及 Agent 骨架测试
├── deploy/        # Nginx 部署资产
├── docker-compose.yml
├── Dockerfile
├── Dockerfile.web
├── pyproject.toml
└── requirements.txt
```

## 快速开始

### 本地运行

```bash
# 1. 安装依赖
pip install -e ".[dev]"

# 2. 配置环境变量
cp .env.example .env            # 填写 POSTGRES_PASSWORD 等

# 3. 应用数据库结构（可选 --seed 灌入产品种子）
python -m scripts.migrate --seed

# 4. 启动 API
python -m app.main              # 或 uvicorn app.main:app --reload --port 8000

# 5. 启动前端
cd frontend && npm ci && npm run dev
```

### Docker Compose

```bash
cp .env.example .env            # 填写 POSTGRES_PASSWORD
docker compose up -d --build
# 前端: http://localhost/     API: http://localhost/api/health
```

### 创建管理员

```bash
python -m scripts.bootstrap_admin --username admin --password '你的密码'
python -m scripts.bootstrap_admin --dry-run          # 只查看角色分布
```

管理员与普通用户**互斥**：管理员不做模拟交易，普通用户不进后台。角色由 `ADMIN_CUSTOMER_IDS` 环境变量白名单与库内 `finance.users.is_admin` **取并集**判定。生产环境管理员密码至少 12 个字符。管理员账号只走 `python -m scripts.bootstrap_admin` 引导。

## 数据模型

表结构以 `migrations/*.sql` 为唯一事实源，ORM（`app/db/models/`，基类在 `app/db/base.py`）只做**映射**，不通过 `create_all` 建表。

| 表 | 说明 |
| --- | --- |
| `finance.users` / `finance.sessions` | 用户与登录会话 |
| `finance.products` / `product_holdings` / `product_performance` | 商品、重仓与业绩/净值 |
| `finance.accounts` / `cash_transactions` | 资金账户与流水账本 |
| `finance.orders` / `positions` | 委托成交与持仓 |
| `finance.agent_sessions` / `agent_messages` / `agent_turns` | 投顾会话、公开消息与轮次 |
| `finance.advisor_profiles` | 用户确认过的风险画像，每个用户一行 |
| `finance.stock_feature_snapshots` / `finance.stock_features` | 选股用的每日特征快照（只读 ready） |
| `finance.agent_tool_evidence` | Stock Agent 工具结果的 24 小时证据句柄（TTL 清理） |
| `finance.product_nav_history` | 日频净值历史，**唯一价格事实源**；`data_source` 区分 real/synthetic，真数据优先覆盖合成 |
| `finance.allocation_plans` / `allocation_plan_items` | 配置方案与明细（权重存 0–1 小数；方案不可逆，可多次采纳） |
| `finance.ai_accounts` / `ai_positions` / `ai_trades` / `ai_portfolio_snapshots` | AI 反事实账本与对比快照，与用户表隔离；`ai_trades` append-only，两级幂等键 |

`checkpoint_*` 由 PgSaver 在首次启动时创建，不是业务表。

迁移清单（`app.db.schema.SCHEMA_APPLY_ORDER`）：

- `001_schema.sql` — 用户、会话、商品、账户、流水、委托、持仓（含旧库补列）
- `003_advisor.sql` — 投顾会话、消息、轮次与风险画像
- `004_stock_features.sql` — 股票特征快照
- `005_agent_evidence.sql` — 股票工具结果证据句柄（`agent_tool_evidence`，24 小时 TTL）
- `006_portfolio_advisor.sql` — 日频净值历史、配置方案、AI 账本与对比快照
- `002_seed.sql` — 产品、演示账号与一笔模拟持仓（仅 `--seed` 时应用）

演示账号（`--seed` 之后可登录）：`demo` / `DemoUser#2026`，`admin` / `AdminDemo#2026`。重复灌种会把这两个账号的口令和 demo 的演示持仓重置为脚本中的数字。


## API

认证与其他接口均以 `Authorization: Bearer <token>` 携带令牌。

| Method | Path | 说明 |
| --- | --- | --- |
| POST | `/api/register` | 注册 |
| POST | `/api/login` | 登录，签发令牌 |
| POST | `/api/logout` | 登出 |
| GET | `/api/me` | 当前用户（含 `is_admin`） |
| DELETE | `/api/account` | 注销账号 |
| GET | `/api/health` | 健康检查 |
| GET | `/api/portfolio/products` | 商品货架 |
| GET | `/api/portfolio/products/{code}` | 商品详情 |
| GET | `/api/portfolio/account` | 账户面板 |
| GET | `/api/portfolio/positions` | 持仓明细 |
| POST | `/api/portfolio/deposit` | 充值 |
| POST | `/api/portfolio/orders` | 买入/卖出 |
| POST | `/api/portfolio/liquidate` | 一键清仓 |
| GET | `/api/portfolio/orders` | 成交记录 |
| GET | `/api/portfolio/transactions` | 资金流水 |
| POST | `/api/portfolio/advice` | 按当前持仓生成三档配置方案（不落用户持仓） |
| GET | `/api/portfolio/allocations` | 配置方案列表 |
| GET | `/api/portfolio/allocations/{plan_id}` | 配置方案详情 |
| POST | `/api/portfolio/allocations/{plan_id}/adopt` | 采纳方案，调仓写入 AI 账本 |
| GET | `/api/portfolio/ai-account` | AI 账户快照与用户/AI 归一净值对比曲线 |
| POST | `/api/portfolio/snapshots` | 固化当日双线快照（幂等，同日重跑覆盖） |
| GET | `/api/admin/users` | 用户总览（管理员） |
| GET | `/api/admin/users/{customer_id}/portfolio` | 单用户账户与持仓（管理员） |
| GET | `/api/admin/products` | 完整货架含下架（管理员） |
| POST | `/api/admin/products` | 新增/更新商品（管理员） |
| POST | `/api/admin/products/{code}/offline` | 下架（管理员） |
| POST | `/api/admin/products/{code}/publish` | 上架（管理员） |
| GET | `/api/agents/health` | 投顾子系统 `ready` 或 `degraded`（普通用户） |
| GET | `/api/agents/sessions` | 投顾会话，按最近更新排序 |
| GET | `/api/agents/sessions/{session_id}/messages` | 会话消息 |
| DELETE | `/api/agents/sessions/{session_id}` | 删除会话并清理 checkpoint |
| GET / PATCH | `/api/agents/profile` | 读取或保存风险画像 |
| POST | `/api/agents/chat/stream` | 发起一轮 SSE 对话 |
| GET | `/api/agents/runs/{request_id}` | 查询轮次，用于断流恢复 |
| POST | `/api/agents/runs/{request_id}/retry/stream` | 从 checkpoint 重试可重试失败 |
| POST | `/api/agents/runs/{request_id}/resume/stream` | 提交画像后恢复挂起轮次 |
| POST | `/api/agents/runs/{request_id}/clarify/stream` | 提交股票澄清后恢复挂起轮次 |

SSE 事件只有 `run`、`status`、`profile_required`、`clarification_required`、`artifact`、`token`、`warning`、`done`、`error`。事件里没有专家原文、提示词、工具参数、原始工具响应或推理过程。`artifact` 为 `StockResultV1` 公开子集。

股票三场景都走同步 SSE。选股读取本地 ready 快照，不在 Web 进程里刷新：

```bash
python -m scripts.refresh_stock_features --as-of 2026-09-26
```

同一快照 + 同一策略 + 同一观察周期会得到同一排序。快照晚于最新交易日 3 个自然日以内仍可用并带 `stock_snapshot_stale_warning`；超过 3 日选股失败且 `retryable=True`。Fuyao 未配置时总览失败并警告；深研/选股按缺失上游降级，不编造涨跌幅。Tushare 只提供新闻事件标签，正文不进入结果。

候选池禁止买卖指令、目标价和仓位比例。数字必须有来源和截止时间。

## 环境变量

| 变量 | 说明 |
| --- | --- |
| `POSTGRES_PASSWORD` | **必填**，数据库密码 |
| `POSTGRES_DSN` | 可选；设置后优先于分项连接参数 |
| `POSTGRES_HOST` / `POSTGRES_PORT` / `POSTGRES_USER` / `POSTGRES_DB` | 连接参数（默认 `localhost:5432` / `postgres` / `advisor`） |
| `APP_ENV` | `production` 时关闭 `/docs` 与 OpenAPI |
| `CORS_ALLOW_ORIGINS` | 允许的跨域来源（逗号分隔） |
| `TRUST_PROXY` | 位于反代之后时置 `true`，限流才信任 `X-Forwarded-For` |
| `ADMIN_CUSTOMER_IDS` | 管理员白名单（逗号分隔），与库内角色取并集 |
| `AUTH_RATE_LIMIT_ATTEMPTS` / `AUTH_RATE_LIMIT_WINDOW_SECONDS` | 登录/注册限流 |
| `PORTFOLIO_MAX_DEPOSIT_AMOUNT` / `PORTFOLIO_MIN_ORDER_AMOUNT` | 单笔充值上限 / 申购下限 |
| `FINANCE_SQL_DIR` | 覆盖 `migrations/` 目录位置（容器内用到） |
| `FINANCE_ADMIN_PASSWORD` | `scripts.bootstrap_admin` 读取的管理员密码 |
| `ADVISOR_MODEL_API_KEY` | 投顾模型密钥。留空时投顾降级，业务 API 仍可用 |
| `ADVISOR_MODEL_BASE_URL` | OpenAI-compatible 地址，默认 `https://api.deepseek.com` |
| `ADVISOR_MODEL_NAME` | 模型名，默认 `deepseek-chat` |
| `FUYAO_API_KEY` | 行情 MCP 密钥。缺失时总览失败，深研/选股降级 |
| `TUSHARE_TOKEN` | Tushare Pro 密钥。缺失时消息面不可用，不进入评分 |
| `PORTFOLIO_NAV_SOURCE` | 日频净值源实现，格式 `module.path:ClassName`，须实现 `NavSource`。留空时每日作业记 `no_source` |
| `AGENT_TURN_TIMEOUT_SECONDS` | 单轮超时，默认 `120`。超时的运行中轮次会标为可重试 |
| `AGENT_MAX_MESSAGE_CHARS` | 单条消息上限，默认 `4000` |
| `AGENT_CONTEXT_MESSAGE_LIMIT` | 带入模型的历史消息条数，默认 `20` |

密钥只放在 `.env` 或进程环境里，不写进镜像。

## 净值与快照

启动时 `nav_scheduler()` 注册进程内每日作业 `portfolio_nav_sync`，**每个交易日 18:00（Asia/Shanghai）**按 `PORTFOLIO_NAV_SOURCE` 同步日频净值；非交易日跳过。APScheduler 未安装或启动失败只降级，不影响业务 API。未配置净值源时作业如实记 `no_source`，不假装成功。每日双线快照由 `POST /api/portfolio/snapshots` 固化（幂等，同日重跑覆盖）。

## 故障恢复

1. 浏览器断流后，前端用 `GET /api/agents/runs/{request_id}` 查看轮次。
2. `completed` 直接显示已保存答案；`interrupted` 按 payload 展示画像表或澄清表；`failed` 且 `retryable` 时调用重试流。
3. 重试不新增用户消息，沿原 `thread_id` 的最近 checkpoint 继续。
4. 进程重启后，超过 `AGENT_TURN_TIMEOUT_SECONDS` 仍为 `running` 或 `pending` 的轮次会被标成可重试。

## 运维命令

```bash
python -m scripts.migrate [--seed]                              # 幂等应用结构（+ 种子）
python -m scripts.bootstrap_admin --username admin [--dry-run]  # 引导管理员
python -m scripts.diagnose_schema_drift [--keep-probe]          # 结构漂移诊断（CI 可用）
python -m scripts.verify_portfolio_live                         # 真实 PostgreSQL 模拟交易端到端校验
python -m scripts.generate_product_seed \
  --input scripts/data/products.sample.json \
  --output migrations/002_product_seed.sql                 # 只生成产品 SQL，不进入自动迁移
python -m scripts.import_nav nav.csv [--lenient]           # 导入真实日频净值（有问题行默认退出码 2）
python -m scripts.seed_nav --days 180 [--seed 7|--clear]   # 播种/清除合成日频净值
```

## 测试

```bash
python -m pytest tests -q
cd frontend && npm test && npm run build
```

- `tests/unit/` — 设置校验、产品库 ORM 行为、种子生成器
- `tests/integration/` — 模拟交易服务与 REST、管理后台、鉴权、生产加固、dotenv 导入顺序
- `tests/architecture/` — 未定义全局名守卫、持久化接缝守卫、生产代码不引用参考实现
- `tests/contract/` — 迁移顺序与 Compose/镜像包装契约
- `tests/test_agents/` — 假模型与假 MCP 下的工作流、只读工具和轮次语义

真实 DeepSeek / Fuyao 冒烟测试带 `network` 标记，只有 `RUN_NETWORK_TESTS=1` 且配置了对应密钥时才运行。默认测试不访问这两处。

## 开发约定

- **不静默降级**：取不到净值即失败（`PortfolioError`，`code=pricing_unavailable`），不用成本价或 1.0 顶替成交。
- **阻塞调用不进事件循环**：既有业务路由保持同步 `def`。投顾 SSE 使用异步生成器，因为响应要边生成边写出。
- **口径唯一**：账户与盈亏只在 `app/services/portfolio_service.py` 计算，路由与管理后台不重算。
