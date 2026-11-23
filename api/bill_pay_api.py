"""`POST /billpay` 的接口封装。

契约（来自 openapi.yaml 第 34-73 行）：
    POST /billpay?accountId={int32}&amount={number}
    Content-Type: application/json
    body: Payee { name, address{street,city,state,zipCode}, phoneNumber, accountNumber }
    200: BillPayResult { payeeName, amount, accountId }

注意两个和直觉不同的事实（已实测）：
1. 参数在 **query string**，不是 body。只有 Payee 在 body 里。
2. 不含 body 时返回 415，不是 400。
"""
from __future__ import annotations

import json
from typing import Any, Optional

from .http_client import HttpClient, Resp


class BillPayApi:
    PATH = "/billpay"

    def __init__(self, client: HttpClient):
        self.client = client

    def pay(self, account_id: Any, amount: Any, payee: dict) -> Resp:
        """正常调用：参数进 query，payee 走 JSON body。"""
        return self.client.post(
            f"{self.PATH}?accountId={account_id}&amount={amount}",
            data=json.dumps(payee, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )

    def pay_raw(
        self,
        query: str = "",
        body: Optional[Any] = None,
        content_type: Optional[str] = "application/json",
        send_body: bool = True,
    ) -> Resp:
        """异常入参专用：允许自定义 query、body 原文、Content-Type。

        用于测「缺参数」「非法 JSON」「错误 Content-Type」这类场景。
        """
        headers = {}
        if content_type:
            headers["Content-Type"] = content_type
        kwargs: dict[str, Any] = {"headers": headers}
        if send_body:
            if isinstance(body, (dict, list)):
                kwargs["data"] = json.dumps(body, ensure_ascii=False).encode("utf-8")
            else:
                kwargs["data"] = body
        path = self.PATH + (f"?{query}" if query else "")
        return self.client.post(path, **kwargs)
