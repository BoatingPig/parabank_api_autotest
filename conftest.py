"""pytest 全局装置。

三条设计原则：
1. **每个用例前重置数据**：`initializeDB` 实测只要 0.07 秒，比"用例之间互相污染再排查"划算得多。
2. **不在 fixture 里断言业务**：fixture 只保证"环境可用"，业务判断留给用例。
3. **被测系统的 500 是 HTML**：所以健康检查判的是能否连上，不是响应格式。
"""
from __future__ import annotations

import os

import pytest

from api import AccountApi, BankApi, BillPayApi, HttpClient, TransactionApi

DEFAULT_BASE_URL = "http://localhost:8080/parabank/services/bank"


def pytest_addoption(parser):
    parser.addoption(
        "--base-url",
        action="store",
        default=os.environ.get("PARABANK_BASE_URL", DEFAULT_BASE_URL),
        help="被测服务基址，默认 %(default)s",
    )


@pytest.fixture(scope="session")
def base_url(request) -> str:
    return request.config.getoption("--base-url").rstrip("/")


@pytest.fixture(scope="session")
def client(base_url) -> HttpClient:
    c = HttpClient(base_url)
    # 环境自检：连不上就直接失败，不要让 30 条用例各报一次连不上
    resp = c.get("/accounts/12345")
    if resp.status_code == 0 or not resp.text:
        pytest.exit(f"ParaBank 未启动或不可达: {base_url}\n请先运行 D:\\parabank\\start-parabank.bat", returncode=1)
    yield c
    c.close()


@pytest.fixture(autouse=True)
def clean_state(client):
    """每个用例前把库恢复干净基线，保证用例可独立重复执行。"""
    resp = client.post("/initializeDB")
    assert resp.status_code == 204, f"数据重置失败: {resp.brief()}"
    yield


@pytest.fixture
def bill_pay(client) -> BillPayApi:
    return BillPayApi(client)


@pytest.fixture
def accounts(client) -> AccountApi:
    return AccountApi(client)


@pytest.fixture
def tx(client) -> TransactionApi:
    return TransactionApi(client)


@pytest.fixture
def bank(client) -> BankApi:
    return BankApi(client)
