"""`/deposit`、`/withdraw`、`/transfer` 三个资金写接口的封装。

和 `bill_pay_api` 的两处不同（都已实测）：
1. 参数**全部在 query string**，没有 JSON body。`billpay` 是「query 放账户金额、
   body 放收款人」，这三个接口只有 query。
2. 响应体没有信息量 —— 一句 `Successfully deposited $1 to account #12456`，
   没有交易号、没有余额、没有状态字段。**判据只能靠写后回查**，
   所以本层只负责发请求，余额与流水交给 `AccountApi` 读。

不在这里断言，理由和 `transaction_api` 相同：
同一个方法在「正常存款」和「负数存款」两个用例里的期望完全相反
（前者余额 +100，后者余额 -100 才是缺陷），封装层一旦开始判断，
用例就没法表达「这里本来就该失败」。
"""
from __future__ import annotations

from typing import Any

from .http_client import HttpClient, Resp


class BankApi:
    def __init__(self, client: HttpClient):
        self.client = client

    # ---- 正常调用：位置参数，参数名固定 ----
    def deposit(self, account_id: Any, amount: Any) -> Resp:
        return self.client.post("/deposit", params={"accountId": account_id, "amount": amount})

    def withdraw(self, account_id: Any, amount: Any) -> Resp:
        return self.client.post("/withdraw", params={"accountId": account_id, "amount": amount})

    def transfer(self, from_account_id: Any, to_account_id: Any, amount: Any) -> Resp:
        return self.client.post(
            "/transfer",
            params={
                "fromAccountId": from_account_id,
                "toAccountId": to_account_id,
                "amount": amount,
            },
        )

    # ---- 异常入参：原样透传 query，用于「缺参」「非法值」场景 ----
    # 缺参不能靠传 None —— requests 会把 None 直接从 query 里丢掉，
    # 看起来像「缺参」，实际是「客户端替你做了一次静默清洗」，测不到真实行为。
    # 所以这里必须能精确控制「哪些键在、哪些键不在」。
    def deposit_raw(self, params: dict) -> Resp:
        return self.client.post("/deposit", params=params)

    def withdraw_raw(self, params: dict) -> Resp:
        return self.client.post("/withdraw", params=params)

    def transfer_raw(self, params: dict) -> Resp:
        return self.client.post("/transfer", params=params)
