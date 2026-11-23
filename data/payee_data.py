"""测试数据。

Payee 用工厂函数生成，避免多个用例共用一个 dict 被意外修改（可变对象串用例是常见事故）。
"""

# 干净基线数据（initializeDB 之后）。数值来自实测，见《ParaBank 数据库查看指南》。
JOHN_CUSTOMER_ID = 12212

ACCOUNT_CHECKING = 12456   # 余额 10.45，做小额支付刚好
ACCOUNT_ZERO = 12900       # 余额 0，测零余额
ACCOUNT_NEGATIVE = 12345   # 余额 -2300，测负余额
ACCOUNT_RICH = 54321       # 余额 1351.12


def payee(
    name="Test Payee",
    street="1 A St",
    city="LA",
    state="CA",
    zip_code="90001",
    phone="555-0100",
    account_number=123456,
) -> dict:
    return {
        "name": name,
        "address": {"street": street, "city": city, "state": state, "zipCode": zip_code},
        "phoneNumber": phone,
        "accountNumber": account_number,
    }
