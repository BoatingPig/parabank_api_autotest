"""HTTP 底层封装。

设计要点（这是接口自动化里最容易踩的坑，所以单独抽一层）：
1. 被测系统在异常时返回的是 **HTML 错误页** 或 **纯文本**，不是 JSON。
   直接调 resp.json() 会抛 JSONDecodeError，把「断言失败」变成「脚本崩溃」。
   所以统一用 Resp 包装，json 解析失败返回 None，由断言层决定怎么判。
2. 所有请求带 Accept: application/json，但响应 Content-Type 未必是 JSON，需要保留原值供断言。
3. 超时、连接错误统一转成 AssertionError 的语义，避免一个网络抖动把整批用例打红。
"""
from __future__ import annotations

import json
from typing import Any, Optional

import requests

DEFAULT_TIMEOUT = 30


class Resp:
    """统一的响应对象。"""

    def __init__(self, raw: requests.Response):
        self.raw = raw
        self.status_code: int = raw.status_code
        self.headers = raw.headers
        self.text: str = raw.text
        self.content_type: str = (raw.headers.get("Content-Type") or "").split(";")[0].strip()

    @property
    def json(self) -> Optional[Any]:
        """解析 JSON；不是 JSON 就返回 None（不抛异常）。"""
        try:
            return self.raw.json()
        except (json.JSONDecodeError, ValueError):
            return None

    @property
    def is_json(self) -> bool:
        return "json" in self.content_type.lower()

    @property
    def is_html(self) -> bool:
        return "html" in self.content_type.lower()

    def brief(self, limit: int = 200) -> str:
        """给断言消息用的短描述。"""
        body = self.text.replace("\r", " ").replace("\n", " ").strip()
        return f"HTTP {self.status_code} [{self.content_type or '-'}] {body[:limit]!r}"

    def __repr__(self) -> str:
        return f"<Resp {self.brief(120)}>"


class HttpClient:
    """极简 HTTP 客户端，只做 ParaBank 需要的事。"""

    def __init__(self, base_url: str, timeout: int = DEFAULT_TIMEOUT):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})

    def request(self, method: str, path: str, **kwargs) -> Resp:
        url = f"{self.base_url}{path}"
        kwargs.setdefault("timeout", self.timeout)
        raw = self.session.request(method, url, **kwargs)
        return Resp(raw)

    def get(self, path: str, **kwargs) -> Resp:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs) -> Resp:
        return self.request("POST", path, **kwargs)

    def close(self) -> None:
        self.session.close()
