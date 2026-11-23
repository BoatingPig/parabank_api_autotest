"""
接口定义：只描述「接口是什么」，不含任何请求逻辑。

字段说明
--------
BASE_URL   : 服务根地址，runner 拼接用
name       : 用例编号前缀，同时用于 CSV 分组
method     : HTTP 方法，v1 全是 GET；保留字段是为了 v2 探 POST 时 runner 不用改
path       : 路径模板，{xxx} 为占位符，必须与 params[].name 完全一致
params     : 被测参数列表
    name   : 参数名
    kind   : 值类别，决定从 values.VALUES 里取哪一套候选值
baseline   : 每个参数的「合法值」
    1. 测某个参数时，其余参数固定为 baseline —— 这是控制变量，保证归因唯一
    2. 生成器应把 baseline 值本身也作为第一条 case 产出，作为正向对照

注意
----
1. path 里**不要**带 query string，v1 只探路径参数
2. baseline 必须是库里真实存在、且返回非空结果的值；
   否则整张矩阵失去对照基准（例如 12345 有 7 条流水，12456 有 0 条，不能用 12456 做 baseline）
"""

BASE_URL = "http://localhost:8080/parabank"

ENDPOINTS = [
    # ------------------------------------------------------------------
    # 1. 按流水 ID 查单条 —— 返回单个对象，不是数组
    #    基线：12145 存在；期望 200 + 1 条
    # ------------------------------------------------------------------
    {
        "name": "tx_by_id",
        "method": "GET",
        "path": "/services/bank/transactions/{transactionId}",
        "params": [
            {"name": "transactionId", "kind": "int"},
        ],
        "baseline": {"transactionId": "12145"},
    },

    # ------------------------------------------------------------------
    # 2. 按账户查全部流水 —— 返回数组
    #    基线：12345 有 7 条；账户 12456 存在但零流水，可作空结果对照
    #    实测排序规则：ORDER BY date, id（不是按 id）
    # ------------------------------------------------------------------
    {
        "name": "tx_by_account",
        "method": "GET",
        "path": "/services/bank/accounts/{accountId}/transactions",
        "params": [
            {"name": "accountId", "kind": "int"},
        ],
        "baseline": {"accountId": "12345"},
    },

    # ------------------------------------------------------------------
    # 3. 按金额查 —— amount 为精确等值匹配（不是范围）
    #    基线：账户 12345 + 金额 300 → 1 条
    #    注意：amount 是 BigDecimal，路径参数转换失败返回 404，而非 400
    # ------------------------------------------------------------------
    {
        "name": "tx_by_account_amount",
        "method": "GET",
        "path": "/services/bank/accounts/{accountId}/transactions/amount/{amount}",
        "params": [
            {"name": "accountId", "kind": "int"},
            {"name": "amount", "kind": "decimal"},
        ],
        "baseline": {"accountId": "12345", "amount": "300"},
    },

    # ------------------------------------------------------------------
    # 4. 按月份 + 类型查
    #    基线特意设为 All / All → 7 条，这样单独测 month 或 type 时都有非零对照
    #
    #    为什么不用 month=11月 做基线：11 月的 5 条全是 Debit，没有 Credit，
    #    那么测 type=Credit 会得到 0 条 —— 这是「正确行为」，却容易被误判成静默失败
    #
    #    实测：month 只接受中文缩写（11月/12月），且只比 MONTH(date)、忽略年份
    #    实测：type 只接受 Credit/Debit/All，传 CREDIT 直接 500
    # ------------------------------------------------------------------
    {
        "name": "tx_by_account_month_type",
        "method": "GET",
        "path": "/services/bank/accounts/{accountId}/transactions/month/{month}/type/{type}",
        "params": [
            {"name": "accountId", "kind": "int"},
            {"name": "month", "kind": "month"},
            {"name": "type", "kind": "enum"},
        ],
        "baseline": {"accountId": "12345", "month": "All", "type": "All"},
    },

    # ------------------------------------------------------------------
    # 5. 按日期范围查
    #    基线：2026-11-01 ~ 2026-11-30 → 5 条
    #    实测：日期格式 MM-dd-yyyy；反向范围、错误格式都返回 200 + 空，不报错
    # ------------------------------------------------------------------
    {
        "name": "tx_by_account_daterange",
        "method": "GET",
        "path": "/services/bank/accounts/{accountId}/transactions"
                "/fromDate/{fromDate}/toDate/{toDate}",
        "params": [
            {"name": "accountId", "kind": "int"},
            {"name": "fromDate", "kind": "date"},
            {"name": "toDate", "kind": "date"},
        ],
        "baseline": {
            "accountId": "12345",
            "fromDate": "11-01-2026",
            "toDate": "11-30-2026",
        },
    },

    # ------------------------------------------------------------------
    # 6. 按单日查
    #    基线：2026-11-09 → 2 条（该账户 7 条流水里唯一有多条的日期）
    #
    #    账户 12345 的真实流水日期：
    #        2025-12-11 / 2025-12-12 / 2026-11-05 / 2026-11-09 ×2 / 2026-11-10 / 2026-11-12
    #
    #    坑记录：最初用 11-11-2026 做基线，但库里根本没有这一天，
    #    结果本组 17 条 case（accountId 9 + onDate 8）全部返回 200 + 0 条，
    #    「0 条」无法归因 —— 分不清是参数有问题，还是这天本来就没数据，整组数据作废。
    #    教训：baseline 必须**实测确认非空**，不能凭印象写。
    #
    #    实测：非法日期（02-30 / 00-00-0000）不报错，返回 200 + 空
    # ------------------------------------------------------------------
    {
        "name": "tx_by_account_ondate",
        "method": "GET",
        "path": "/services/bank/accounts/{accountId}/transactions/onDate/{onDate}",
        "params": [
            {"name": "accountId", "kind": "int"},
            {"name": "onDate", "kind": "date"},
        ],
        "baseline": {"accountId": "12345", "onDate": "11-09-2026"},
    },
]
