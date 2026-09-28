-- 演示数据：产品货架、一个普通用户和一笔模拟持仓、一个管理员。
-- 仅在 `python -m scripts.migrate --seed` 时执行。重复执行不产生第二套账。
--
-- 可登录账号（库内只存 PBKDF2-SHA256、10 万次迭代的哈希）：
--   demo  / DemoUser#2026   普通用户，客户号 DEMO000001（若用户名已存在则保留原客户号）
--   admin / AdminDemo#2026  管理员，没有交易账户
--
-- demo 的账：充值 100000.00，按净值 3.85、申购费 1.5% 买入 110011。
-- 外扣法：费用 147.78，份额 2559.01，投入 9852.19，实际扣款 9999.97，剩余现金 90000.03。

INSERT INTO finance.products (
    code, name, type, establish_date, scale, manager, company,
    management_fee, custody_fee, subscription_fee, redemption_fee,
    risk_level, recommended_holding_period, investment_target, investment_strategy, is_active
) VALUES
    ('110011', '易方达中小盘混合', 'fund', '2008-06-19', 210.5, '张坤', '易方达基金', 1.5, 0.25, 1.5, '0.5%', 'R3 中风险', 'long', '通过投资于具有良好流动性的金融工具，追求长期资本增值。', '自下而上精选个股，集中持有具备长期竞争力的公司。', true),
    ('000001', '华夏成长混合', 'fund', '2001-12-18', 48.2, '示例经理', '华夏基金', 1.5, 0.25, 1.5, '0.5%', 'R3 中风险', 'medium', '投资于成长型上市公司，分享中国经济成长成果。', '以成长性为核心，兼顾估值与行业景气度。', true),
    ('510300', '华泰柏瑞沪深300ETF', 'etf', '2012-05-04', 1200.0, '示例经理', '华泰柏瑞基金', 0.5, 0.1, 0.0, '0', 'R4 中高风险', 'long', '紧密跟踪沪深300指数，追求跟踪偏离度和跟踪误差最小化。', '完全复制法被动跟踪标的指数。', true),
    ('510050', '华夏上证50ETF', 'etf', '2004-12-30', 680.0, '示例经理', '华夏基金', 0.5, 0.1, 0.0, '0', 'R4 中高风险', 'long', '紧密跟踪上证50指数，追求跟踪偏离度和跟踪误差最小化。', '完全复制法被动跟踪标的指数。', true),
    ('512010', '易方达沪深300医药ETF', 'etf', '2013-09-23', 90.0, '示例经理', '易方达基金', 0.5, 0.1, 0.0, '0', 'R4 中高风险', 'long', '紧密跟踪沪深300医药卫生指数。', '完全复制法被动跟踪医药行业指数。', true),
    ('518880', '华安黄金ETF', 'etf', '2013-07-18', 240.0, '示例经理', '华安基金', 0.5, 0.1, 0.0, '0', 'R3 中风险', 'medium', '紧密跟踪国内黄金现货价格。', '投资于上海黄金交易所黄金现货合约。', true),
    ('WM2026001', '示例现金管理理财', 'wealth', '2024-01-08', 80.0, '示例经理', '示例银行', 0.2, 0.05, 0.0, '0', 'R1 低风险', 'short', '在保持流动性和本金相对安全的前提下，争取稳健收益。', '投资于货币市场工具和高流动性债券。', true),
    ('WM2026002', '示例稳健日开理财', 'wealth', '2023-06-15', 45.0, '示例经理', '示例银行', 0.3, 0.05, 0.1, '0', 'R2 中低风险', 'short', '追求低波动下的稳健增值，支持较短持有期。', '以利率债和高等级信用债为主，控制久期。', true),
    ('WM2026003', '示例季开固收理财', 'wealth', '2022-03-21', 120.0, '示例经理', '示例银行', 0.3, 0.05, 0.1, '0', 'R2 中低风险', 'medium', '通过固定收益资产争取中期稳健回报。', '配置利率债、高等级信用债和少量存款。', true),
    ('WM2026004', '示例混合策略理财', 'wealth', '2021-11-02', 36.0, '示例经理', '示例银行', 0.4, 0.05, 0.2, '0.1%', 'R3 中风险', 'medium', '在控制回撤的前提下争取高于纯固收的收益。', '固收打底，少量配置权益类资产。', true),
    ('003003', '华夏现金增利货币A', 'fund', '2004-04-07', 300.0, '示例经理', '华夏基金', 0.33, 0.1, 0.0, '0', 'R1 低风险', 'short', '在保持本金安全与资产流动性的前提下，追求稳定收益。', '投资于短期货币市场工具。', true),
    ('040025', '华安双债添利债券A', 'fund', '2013-03-15', 12.6, '示例经理', '华安基金', 0.7, 0.2, 0.8, '0.1%', 'R2 中低风险', 'medium', '通过债券投资获取稳定收益，控制回撤。', '以中高等级信用债与利率债为主，适度杠杆。', true),
    ('161725', '招商中证白酒指数A', 'fund', '2015-05-27', 480.0, '示例经理', '招商基金', 1.0, 0.2, 1.0, '0.5%', 'R4 中高风险', 'long', '紧密跟踪中证白酒指数，分享白酒行业成长。', '指数化被动投资，行业集中度较高。', true),
    ('999999', '数据缺失示例基金''O', 'fund', '2010-01-01', 1.0, '示例经理', '示例基金', NULL, NULL, NULL, '', '未披露', '', '', '', false)
ON CONFLICT (code) DO UPDATE SET
    name = EXCLUDED.name,
    type = EXCLUDED.type,
    establish_date = EXCLUDED.establish_date,
    scale = EXCLUDED.scale,
    manager = EXCLUDED.manager,
    company = EXCLUDED.company,
    management_fee = EXCLUDED.management_fee,
    custody_fee = EXCLUDED.custody_fee,
    subscription_fee = EXCLUDED.subscription_fee,
    redemption_fee = EXCLUDED.redemption_fee,
    risk_level = EXCLUDED.risk_level,
    recommended_holding_period = EXCLUDED.recommended_holding_period,
    investment_target = EXCLUDED.investment_target,
    investment_strategy = EXCLUDED.investment_strategy,
    is_active = EXCLUDED.is_active;

DELETE FROM finance.product_holdings
WHERE product_code IN (
    '110011', '000001', '510300', '510050', '512010', '518880',
    '003003', '040025', '161725', '999999',
    'WM2026001', 'WM2026002', 'WM2026003', 'WM2026004'
);

INSERT INTO finance.product_holdings (product_code, stock_name, stock_code, weight, rank, report_date) VALUES
    ('110011', '示例股票A', '600519', 9.8, 1, '2026-06-30'),
    ('110011', '示例股票B', '000858', 8.1, 2, '2026-06-30'),
    ('000001', '示例股票C', '600036', 4.2, 1, '2026-03-31'),
    ('161725', '示例股票A', '600519', 15.0, 1, '2026-06-30');

DELETE FROM finance.product_performance
WHERE product_code IN (
    '110011', '000001', '510300', '510050', '512010', '518880',
    '003003', '040025', '161725', '999999',
    'WM2026001', 'WM2026002', 'WM2026003', 'WM2026004'
);

INSERT INTO finance.product_performance (
    product_code, nav, return_1m, return_3m, return_6m, return_1y, return_3y,
    max_drawdown, volatility, sharpe_ratio, update_date
) VALUES
    ('110011', 3.85, 0.021, 0.045, 0.083, 0.126, 0.311, 0.187, 0.162, 0.78, '2026-09-08'),
    ('000001', 1.42, 0.005, 0.012, 0.019, 0.041, 0.088, 0.223, 0.171, 0.24, '2026-02-10'),
    ('510300', 4.12, 0.018, 0.039, 0.071, 0.112, 0.205, 0.254, 0.148, 0.66, '2026-09-08'),
    ('510050', 2.86, 0.012, 0.028, 0.054, 0.091, 0.164, 0.221, 0.136, 0.58, '2026-09-08'),
    ('512010', 0.48, -0.008, 0.016, 0.021, 0.044, -0.032, 0.286, 0.192, 0.21, '2026-09-08'),
    ('518880', 5.62, 0.009, 0.031, 0.067, 0.118, 0.246, 0.143, 0.121, 0.72, '2026-09-08'),
    ('WM2026001', 1.012, 0.0014, 0.0042, 0.0084, 0.017, 0.052, 0.001, 0.003, 1.8, '2026-09-08'),
    ('WM2026002', 1.036, 0.0021, 0.0064, 0.0128, 0.026, 0.081, 0.008, 0.012, 1.35, '2026-09-08'),
    ('WM2026003', 1.084, 0.0032, 0.0096, 0.019, 0.038, 0.121, 0.021, 0.024, 1.12, '2026-09-05'),
    ('WM2026004', 1.127, 0.006, 0.014, 0.027, 0.061, 0.154, 0.064, 0.058, 0.84, '2026-09-08'),
    ('003003', 1.0, 0.0016, 0.0048, 0.0096, 0.0195, 0.0612, 0.0, 0.002, 2.1, '2026-09-08'),
    ('040025', 1.31, 0.004, 0.011, 0.022, 0.045, 0.142, 0.052, 0.038, 1.02, '2026-09-05'),
    ('161725', 1.05, -0.012, -0.028, -0.011, 0.033, -0.086, 0.412, 0.238, 0.09, '2026-09-08');

INSERT INTO finance.users (
    customer_id, username, display_name, password_hash, salt, created_at, is_admin
) VALUES
    (
        'DEMO000001', 'demo', '演示用户',
        'aed6792600686397105099fff705e7d3be09da8fbb3e26c5570f13f092381e32',
        'a1b2c3d4e5f60718293a4b5c6d7e8f90',
        '2026-09-27T00:00:00', false
    ),
    (
        'ADMIN000001', 'admin', '演示管理员',
        'fb4d133c8763abd2001856caa73402f1c25120a11f7a0c47a6b33092b92d889a',
        '0f1e2d3c4b5a69788796a5b4c3d2e1f0',
        '2026-09-27T00:00:00', true
    )
ON CONFLICT (username) DO UPDATE SET
    display_name = EXCLUDED.display_name,
    password_hash = EXCLUDED.password_hash,
    salt = EXCLUDED.salt,
    is_admin = EXCLUDED.is_admin,
    customer_id = COALESCE(finance.users.customer_id, EXCLUDED.customer_id);

INSERT INTO finance.accounts (customer_id, cash_balance, frozen_balance, total_deposit)
SELECT customer_id, 90000.03, 0, 100000.00
FROM finance.users
WHERE username = 'demo'
ON CONFLICT (customer_id) DO UPDATE SET
    cash_balance = EXCLUDED.cash_balance,
    frozen_balance = EXCLUDED.frozen_balance,
    total_deposit = EXCLUDED.total_deposit;

INSERT INTO finance.cash_transactions (
    txn_id, customer_id, kind, amount, balance_after, ref_id, note, idempotency_key
)
SELECT
    '11111111-1111-4111-8111-111111111111',
    customer_id, 'deposit', 100000.00, 100000.00, '', '演示充值', 'seed-demo-deposit'
FROM finance.users
WHERE username = 'demo'
ON CONFLICT (txn_id) DO NOTHING;

INSERT INTO finance.orders (
    order_id, customer_id, product_code, side, shares, price,
    gross_amount, fee, net_amount, realized_pnl, fee_limitations, idempotency_key
)
SELECT
    '22222222-2222-4222-8222-222222222222',
    customer_id, '110011', 'buy', 2559.01, 3.85,
    9852.19, 147.78, 9999.97, NULL, '[]'::jsonb, 'seed-demo-buy-110011'
FROM finance.users
WHERE username = 'demo'
ON CONFLICT (order_id) DO NOTHING;

INSERT INTO finance.cash_transactions (
    txn_id, customer_id, kind, amount, balance_after, ref_id, note, idempotency_key
)
SELECT
    '33333333-3333-4333-8333-333333333333',
    customer_id, 'buy', -9999.97, 90000.03, '110011', '申购 易方达中小盘混合', 'seed-demo-buy-110011'
FROM finance.users
WHERE username = 'demo'
ON CONFLICT (txn_id) DO NOTHING;

INSERT INTO finance.positions (customer_id, product_code, shares, cost_amount, avg_cost)
SELECT customer_id, '110011', 2559.01, 9999.97, 3.9077494812446996
FROM finance.users
WHERE username = 'demo'
ON CONFLICT (customer_id, product_code) DO UPDATE SET
    shares = EXCLUDED.shares,
    cost_amount = EXCLUDED.cost_amount,
    avg_cost = EXCLUDED.avg_cost;
