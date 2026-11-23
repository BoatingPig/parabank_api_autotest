# ParaBank 接口自动化测试

基于开源演示网银 [ParaBank](https://parasoft.github.io/parabank/) 的接口自动化项目。
做法和工作里常见的「照文档写用例」不同：**先用一次性探针把接口契约测出来，再从契约提炼回归用例。**

```
$ python -m pytest -q
73 passed, 46 xfailed in 10.10s
```

10 个业务接口 / 121 条用例（119 条默认执行，2 条并发默认跳过）/ 16 条根因级问题（12 个实现缺陷 + 4 个契约空白）。

## 为什么先做「探针」而不是先写用例

`openapi.yaml` 列了 27 个路径，但**只有形状，没有语义**：参数什么格式、非法值该拒还是该忽略、写接口有什么副作用、错误码怎么分层，全都没写。

一开始我也想照着文档直接写断言，试了两条就发现行不通：

- 文档写 `month` 是月份，实测只认中文 `11月`（`SimpleDateFormat("MMM")` + zh_CN），传 `Nov` 返回 200 + 全量数据
- 文档写 `amount` 是数字，实测流水查询的 `amount` 是 `int`（`1.5` 非法），资金接口的是 `BigDecimal`（`1.5` 合法、`1e3` 等于 1000）
- 文档没说 `transfer` 成功会生成几条流水、描述文案是什么、失败会不会回滚

**照文档写出来的用例会全部通过，而且全是假的。** 所以流程改成三段：

```
探针（一次性实验）        契约表（规则）           用例（回归资产）
106 + 25 + 3 条请求   →   5 份文档 2600 行    →   121 条
matrix.csv / findings.csv   docs/*.md              tests/test_*.py
```

探针只回答「发生了什么」，契约表回答「本该是什么」，用例固化「已经确知的事实」。三段产物都在仓库里，可回溯：每条结论都能顺着 `case_id` 查回原始状态码和响应片段。

## 测试范围

| 模块 | 接口 | 用例 | 契约表 |
|---|---|---:|---|
| 流水查询 | `/transactions/{id}`、`/accounts/{id}/transactions` 及 `amount` / `month+type` / `fromDate+toDate` / `onDate` 四个过滤接口 | 44 | [流水查询接口契约表](docs/流水查询接口契约表.md) |
| 资金操作 | `/deposit`、`/withdraw`、`/transfer` | 46 | [资金类接口契约表](docs/资金类接口契约表.md) |
| 账单支付 | `/billpay` | 31 | — |

辅助接口：`GET /accounts/{id}`（观测余额）、`POST /initializeDB`（重置数据）。

## 快速开始

```bash
# 1. 启动被测系统（双击，弹出的 Tomcat 窗口不要关）
D:\parabank\start-parabank.bat

# 2. 装依赖
cd D:\parabank\autotest
pip install -r requirements.txt

# 3. 跑全量（119 条，约 10 秒）
python -m pytest

# 换环境 / 换 mock 服务
python -m pytest --base-url http://localhost:8080/parabank/services/bank
```

并发用例默认不跑（会改数据、必须串行、结果不稳定），需要时显式指定：

```bash
python -m pytest tests/test_transfer.py -m concurrency
```

按测试目的筛选：

```bash
python -m pytest -m happy          # 正常路径 20 条，可当冒烟集
python -m pytest -m boundary       # 金额与数值边界
python -m pytest -m validation     # 参数校验
python -m pytest -m consistency    # 幂等与一致性
```

## 目录结构

```
autotest/
├── api/                        接口层：只拼 URL、发请求，不做断言
│   ├── http_client.py          协议层：Resp 包装（JSON 解析失败返回 None）
│   ├── transaction_api.py      流水查询 6 个接口
│   ├── bank_api.py             资金 3 个接口（参数在 query，不在 body）
│   ├── bill_pay_api.py         /billpay
│   └── account_api.py          账户查询，供断言回查用
├── conftest.py                 7 个夹具：环境自检、每用例重置、各 API 包装
├── data/payee_data.py          Payee 工厂函数
├── tests/
│   ├── test_transactions.py    44 条，9 组
│   ├── test_transfer.py        46 条，7 组
│   └── test_bill_pay.py        31 条，6 组
├── probe/                      读接口探针工具链（config/values/render/runner/expect/analyze）
│   └── out/                    matrix.csv（106×13）、findings.csv（26 条）
├── probe_write/                写接口探针（reset → 单发 → 回查）
│   └── out/                    write_matrix.csv（25×15）、concurrency.csv（3×11）
├── docs/                       契约表与用例清单
├── openapi.yaml                被测服务的接口定义
└── pytest.ini                  配置与自定义 marker
```

## 发现了什么

### 三个最严重的

**1. 三个小数位让账户永久不可用（P0）**

```
POST /deposit?accountId=12456&amount=0.001   →  200 "Successfully deposited $0.001"
GET  /accounts/12456                          →  500
GET  /accounts/12456/transactions             →  500
```

写路径不校验精度，照写入库；读路径 `JdbcAccountDao.java:29` 的 `balance.setScale(2)` 没传 `RoundingMode`，抛 `ArithmeticException: Rounding necessary`。
**一次客户端输入错误，换来账户永久不可读**，只能 `initializeDB` 恢复。写的时候没报错、事务正常提交，所以事务保护不了它。

**2. 并发转账凭空造钱（P0）**

10 线程各转 10 元，应扣 100：

| | 应扣 | 实扣 | 收款方 | 差额 | 流水条数 |
|---|---:|---:|---:|---:|---|
| 10 × 10 元 | -100 | **-30** | +100 | **凭空多出 70** | 10 / 10 正确 |
| 20 × 5 元 | -100 | **-35** | +100 | 凭空多出 65 | 20 / 20 正确 |

根因是 `transfer` = `withdraw` + `deposit`，而 `withdraw` 是读-改-写：

```java
// 绝对值赋值，不是 balance = balance - ?
UPDATE Account SET ... balance = :balance
```

两个事务同时读到旧值，后写的覆盖先写的。**事务保证了原子性（中途失败能完整回滚，已验证），但管不了并发。**

同一场景（10 × 10）三次跑分别丢 50 / 60 / 70 元——量级不收敛，这就是竞态本身的性质，也意味着**不能靠「再跑一次看是否复现」来确认结论**。

**3. 月份参数 fail-open，静默返回全量（P1）**

```
month=11月   → 200 / 5 条   （正确过滤 11 月）
month=Nov    → 200 / 7 条   （全量，参数等于没传）
month=11     → 200 / 7 条
month=2026-11→ 200 / 7 条
```

`parse()` 抛异常被 `catch` 吞掉，**查询条件根本没拼进 SQL**，接口照样返回 200。

对照另一个参数：`type='` 返回 **500**（拼接在 `try` 外，脏值照拼进 SQL，语法错误）。

| 输入 | 现象 | 拼接位置 | 失败形态 |
|---|---|---|---|
| `month='` | 200 / 全量 | `try` 内 | **fail-open**：静默失效 |
| `type='` | 500 | `try` 外 | **fail-through**：直接崩溃 |

同一个「单引号」，两个参数两种命运，根因只差一个 `try` 的位置。**这条最能说明「不报错」和「正确」是两件事。**

### 完整清单

| 模块 | 问题 | 级别 | 性质 |
|---|---|---|---|
| 流水 | SQL 注入：`type=0' OR '1'='1` 返回全表 21 条，绕过账户隔离 | P0 | 缺陷 |
| 流水 | `month` 非中文时静默返回全量（fail-open） | P1 | 缺陷 |
| 流水 | 日期 lenient：`2026-11-11` 被解析成**公元 179 年**，`13-45-2026` 变 2027-02-14 | P1 | 缺陷 |
| 流水 | `type=CREDIT`（大写）→ 500 | P1 | 缺陷 |
| 流水 | 账户不存在时 5 个接口给出两种处理：`/transactions` 返 400，其余四个返 200 + 空 | P2 | 契约空白 |
| 资金 | 3 位小数让账户永久 500 | P0 | 缺陷 |
| 资金 | 并发丢失更新，凭空造钱 | P0 | 缺陷 |
| 资金 | 负数金额方向反转（三个接口一致） | P1 | 缺陷 |
| 资金 | 零金额仍生成流水（余额不动，流水 +1） | P2 | 缺陷 |
| 资金 | 金额无上限，可一次写入 10 亿 | P1 | 缺陷 |
| 资金 | 缺参 / 空参 → 500 而非 400（`BigDecimal` 拆箱 NPE） | P2 | 缺陷 |
| 资金 | 响应声明 `application/json`，body 却是裸文本 | P2 | 缺陷 |
| 资金 | 自己转自己生成 2 条净零流水 | P2 | 缺陷 |
| 资金 | 允许无限透支：零余额账户能转出 100 | — | 契约空白 |
| 资金 | 无幂等：同参数连发两次扣两次，且响应无交易号可供事后核对 | — | 契约空白 |
| 资金 | 跨客户转账无授权校验（客户 12212 → 12323 放行） | — | 契约空白 |

「契约空白」不是缺陷：文档没说清、单看每个接口都能自圆其说。处置方式是**先问预期**，不是提单——把契约空白当 bug 提，是最容易被驳回的一类误报。

## 四条设计决策

**1. 分层：接口层不许写断言**

`tests` → `api` → `http_client`，不越级。`api` 层只拼 URL、发请求、包装响应，不判断对错。
原因是同一个方法在不同用例里期望完全相反：`by_month_type(12345, "Nov")` 在「缺陷固化」用例里期望返回全量，在「正常过滤」用例里期望只返回 11 月。封装层一旦开始判断，第二条用例就没法表达。

**2. 写操作必须回查**

`transfer` 只回一句 `Successfully transferred $100 from account #13122 to account #12567`，没有交易号、没有余额、没有状态。所以每条写用例都是「操作前后各抓一次快照，比增量」：

```
Δ余额      after - before
新增流水   流水 id 集合差（不能用条数差，条数差说不出新增的是哪一条）
```

**3. 一致性校验必须走两条独立路径**

`result_count` 数标签、`result_ids` 用正则抓子元素——两条路径互不知情，一个对不代表另一个对。
资金域同理：并发那组**流水条数 10/10 全对，只有余额错**。只比对条数的对账会 100% 通过。

**4. 不硬编码日期**

种子流水的日期是按 `CURRENT_DATE` 相对生成的（`insert.sql:44-60`），每调一次 `initializeDB` 就按当天重算。昨天写的 `11-09-2026` 今天就是 0 条。
用例里的做法是从接口返回的 epoch 按 UTC+8 反推成 `MM-dd-yyyy` 再用，断言「过滤是否生效」这个性质，而不是「某天返回几条」。

## xfail 怎么读

46 条用例带 `xfail`，**不是写错了**，是在描述「正确行为应该是什么」，当前实现不满足。每条对应上表的一个根因。

`strict=True`（44 条）的作用：缺陷修好后用例会变成 **XPASS 并让流水线变红**，逼人回来移除标记——缺陷单会自己告诉你它被修了。

并发那两条是 `strict=False`：竞态丢失量随机，偶尔可能一次都没丢，那时 XPASS 不该算作「用例坏了」。

**xfail 数量（46）远大于缺陷数量（16）**，因为一个根因有多个暴露面：精度问题在 `deposit` / `withdraw` / `transfer` 各有一次，负数在三个接口各有一条，`month` 的纵向判定和横向等价比较各有一条。

## 已知短板

| 短板 | 表现 | 打算 |
|---|---|---|
| 不能并行 | `clean_state` 每条用例重置**全库**，`pytest-xdist -n 4` 会互踩 | 改成「每条用例独占账户」才能并行 |
| 无 CI | 简历写「自动化」却没有流水线 | 待补，或退化成只跑纯函数单测并说明取舍 |
| 6 项未覆盖 | `transactionId` 负值、`amount` 负数、`month` 空值等 | 探针阶段测过、结论一致，固化的边际收益接近 0 |
| 金额精度测不了 | 契约 6.6 关心 `.xx12` 这类三分位，种子数据全是 `.xx00` | 要改种子 SQL，属于改被测系统，不做 |
| 写接口耗时占比高 | 119 条 10 秒，其中 `initializeDB` 占约 85% | 当前规模不值得优化 |

## 文档索引

| 文档 | 内容 |
|---|---|
| [接口契约探测方法论](docs/接口契约探测方法论.md) | 为什么要探、矩阵 13 列逐列解释、六层过滤、为什么分层 |
| [流水查询接口契约表](docs/流水查询接口契约表.md) | 6 个接口的合法值/非法值实测、错误三层、时区与精度 |
| [资金类接口契约表](docs/资金类接口契约表.md) | 3 个写接口的副作用、幂等、并发行为、缺口 G1–G10 |
| [流水查询测试用例清单](docs/流水查询测试用例清单.md) | 44 条逐条明细 + 三张反查表（按接口/按参数/按 xfail） |
| [接口自动化框架结构说明](docs/接口自动化框架结构说明.md) | 分层职责、夹具 scope 决策、一次请求的 9 步调用链 |

## 环境

- Windows + Python 3.13，pytest 9.2.0 / requests 2.34.2
- 被测系统 ParaBank 5.0（本地 Tomcat 9 + JDK 21 + 内嵌 HSQLDB）
- **每次重启后数据库是空的，必须先 `POST /initializeDB`**（`conftest.py` 的 `clean_state` 夹具自动做了）
