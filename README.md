# 科研经费双重记账 API

基于 **FastAPI + SQLite + SQLAlchemy 2.x** 的复式记账（double-entry bookkeeping）服务，面向科研项目经费管理场景。提供 `Dockerfile` 与 `docker-compose.yml`，一条命令启动。

## 功能特性

- **带币种的会计科目**：每个科目绑定 ISO 4217 币种（如 `CNY` / `USD`），类型为资产/负债/净资产/收入/费用。
- **多借贷分录凭证**：一张凭证包含多条分录，金额以**整数分**存储（API 用十进制字符串传输），杜绝浮点误差。
- **事务内强校验**（`BEGIN IMMEDIATE` 单事务，失败整体回滚）：
  - 借方总额 = 贷方总额；
  - 至少一条借方与一条贷方；
  - 凭证币种与所有分录科目币种一致（禁止跨币种凭证）；
  - 入账期间必须存在且**未结账**。
- **不可变凭证**：过账后凭证头/分录不可 `UPDATE`/`DELETE`（服务层 + SQLite 触发器双重保证），并分配期间内顺序凭证号 `V-YYYY-MM-NNNN`。
- **只能反向冲销**：撤销通过提交「反向凭证」完成——借贷方向逐行对调、金额不变，双向保留 `reverses_id` / `reversed_by_id` 关联，可跨期间冲销（原期间已结账也可冲入新的开放期间），禁止重复冲销。
- **request_id 幂等**：客户端为每次提交提供唯一键，网络重试/重复提交返回同一凭证（`200` + `X-Idempotent-Replay: true`），余额不重复累计、**支出限额不重复占用**。
- **批量过账**：`POST /vouchers/batch` 一次按请求顺序提交 1～20 张普通凭证，所有新凭证、凭证号与余额更新共用**一个事务**；响应逐项返回凭证及 `idempotent_replay` 回放标志。批内重复 `request_id` 整批拒绝（`409`）；已存在的 `request_id` 按单笔同样的幂等语义回放；复用单笔的期间、借贷、币种与费用月度限额校验，**同批前序新凭证占用的额度计入后序校验**；任一项失败整批回滚，单笔接口行为不变。
- **费用科目月度支出限额**：可为费用科目按 `YYYY-MM` 设置/调整支出上限；过账时在同一写事务内把本次借方费用与该科目本月已过账（未冲销）借方费用合计比较，超限整体拒绝，不写入凭证、凭证号或余额；被冲销凭证自动释放原期间已用额度。
- **余额物化**：按（科目 × 期间）实时维护借贷累计额，过账/冲销在同一事务内 UPSERT。
- **查询**：科目余额（可按期间）、科目分类账（逐笔累计余额、游标分页）、凭证列表（按科目/期间过滤）、凭证链（原始凭证 ↔ 反向凭证）。
- **只读试算平衡表**：`GET /reports/trial-balance?period=YYYY-MM`，按币种分组汇总截止期间（含此前各期间）的累计借贷发生额，逐科目给出期末借/贷余额，并给出各币种期末借贷合计与 `balanced` 是否平衡；无余额科目不列出，接口只做 SELECT，不创建期间、不改动账务数据。
- **并发安全**：SQLite WAL + `BEGIN IMMEDIATE` + `busy_timeout`，写入在事务起点排队；已验证 16 线程并发无锁错误、凭证号不重号。

## 一键启动（Docker Compose）

```bash
docker compose up --build
```

- API：http://localhost:8000
- 交互式文档（Swagger）：http://localhost:8000/docs
- 健康检查：http://localhost:8000/health
- SQLite 数据持久化在命名卷 `ledger-data`（容器内 `/data`）
- 首次启动自动写入一套科研经费演示账套（`SEED_DEMO=true`，幂等）

不想用演示数据时，把 `docker-compose.yml` 中的 `SEED_DEMO` 改为 `"false"`。

停止并保留数据：

```bash
docker compose down          # 保留 ledger-data 卷
docker compose down -v       # 同时删除数据卷
```

## 本地运行（不用 Docker）

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 可选：初始化演示账套
DATA_DIR=./data python3 -m app.seed

uvicorn app.main:app --reload
```

运行测试：

```bash
python3 -m pytest -q
```

## API 总览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/accounts` | 创建带币种的科目 |
| GET | `/accounts` | 科目列表 |
| GET | `/accounts/{id}` | 科目详情 |
| PUT | `/accounts/{id}/limits/{period}` | 设置/调整费用科目月度支出限额（`YYYY-MM`，幂等覆盖） |
| GET | `/accounts/{id}/limits` | 该科目全部月份限额及已用额/剩余额度 |
| GET | `/accounts/{id}/limits/{period}` | 该科目指定月份限额及已用额/剩余额度 |
| GET | `/accounts/{id}/balances?period=YYYY-MM` | 科目各期间余额 |
| GET | `/accounts/{id}/ledger?period_from=&period_to=&cursor=&limit=` | 科目分类账（逐笔累计） |
| POST | `/vouchers` | 提交凭证（`request_id` 幂等） |
| POST | `/vouchers/batch` | 批量提交 1-20 张普通凭证（单事务、顺序过账、逐项幂等回放） |
| GET | `/vouchers?account_id=&period=&cursor=` | 凭证列表/过滤 |
| GET | `/vouchers/by-request/{request_id}` | 按幂等键反查 |
| GET | `/vouchers/{id}` | 凭证详情 |
| GET | `/vouchers/{id}/chain` | 凭证链 |
| POST | `/vouchers/{id}/reverse` | 提交反向凭证冲销 |
| GET | `/periods` / `/periods/{key}` | 期间列表/详情 |
| POST | `/periods/{key}/close` | 结账（幂等，冻结入账） |
| POST | `/periods/{key}/reopen` | 重新打开期间（纠错用） |
| GET | `/reports/trial-balance?period=YYYY-MM` | 只读试算平衡表（按币种分组，截止期间累计） |

错误统一返回：

```json
{ "error": { "code": "unprocessable_entity", "message": "借贷不平衡",
             "detail": { "reason": "debits_not_equal_credits",
                         "total_debit": 1200000, "total_credit": 999900 } } }
```

## 快速上手示例

### 1. 创建科目

```bash
curl -XPOST localhost:8000/accounts -H 'Content-Type: application/json' -d '{
  "code": "1002", "name": "银行存款", "currency": "CNY", "type": "asset"
}'
curl -XPOST localhost:8000/accounts -H 'Content-Type: application/json' -d '{
  "code": "4001", "name": "科研拨款收入", "currency": "CNY", "type": "income"
}'
```

### 2. 提交拨款到账凭证（借银行 / 贷收入）

```bash
curl -XPOST localhost:8000/vouchers -H 'Content-Type: application/json' -d '{
  "request_id": "grant-2026-0001",
  "date": "2026-09-20",
  "currency": "CNY",
  "memo": "国家自然科学基金拨款到账",
  "lines": [
    {"account_id": 1, "direction": "debit",  "amount": "1000000.00"},
    {"account_id": 2, "direction": "credit", "amount": "1000000.00"}
  ]
}'
```

借贷不平、科目币种与凭证币种不一致、期间已结账等情况会得到 `422`/`409`，且不写入任何数据。用同一 `request_id` 重放得到同一凭证、余额不翻倍。

### 3. 批量提交凭证（1～20 张，单事务）

`POST /vouchers/batch` 按 `items` 的数组顺序逐张过账，整批评语义与单笔一致，适合一次性导入多张凭证（服务通过 `docker compose up --build` 启动后，访问地址为 http://localhost:8000）：

```bash
curl -XPOST localhost:8000/vouchers/batch -H 'Content-Type: application/json' -d '{
  "items": [
    {
      "request_id": "grant-2026-0002",
      "date": "2026-09-21",
      "currency": "CNY",
      "memo": "设备费报销",
      "lines": [
        {"account_id": 5, "direction": "debit",  "amount": "3000.00"},
        {"account_id": 1, "direction": "credit", "amount": "3000.00"}
      ]
    },
    {
      "request_id": "grant-2026-0003",
      "date": "2026-09-21",
      "currency": "CNY",
      "memo": "材料费报销",
      "lines": [
        {"account_id": 6, "direction": "debit",  "amount": "800.00"},
        {"account_id": 1, "direction": "credit", "amount": "800.00"}
      ]
    }
  ]
}'
```

响应严格按请求顺序逐项给出凭证本体（与 `POST /vouchers` 相同的结构）和该项是否为幂等回放：

```json
{
  "items": [
    {"idempotent_replay": false, "voucher": {"id": 2, "number": "V-2026-09-0002", "...": "..."}},
    {"idempotent_replay": false, "voucher": {"id": 3, "number": "V-2026-09-0003", "...": "..."}}
  ],
  "created_count": 2,
  "replay_count": 0
}
```

批量语义：

- **批内顺序、单事务**：1～20 张（`items` 长度限制，越界 `422`）按数组顺序过账，所有新凭证、凭证号（同期间连续分配）与余额 UPSERT 在一个 `BEGIN IMMEDIATE` 事务内提交；任一
  项失败（借贷不平、币种不一致、期间已结账、超月度限额等）整批回滚，失败位置在错误体 `detail.batch_index`（从 1 开始）与 `detail.request_id` 中给出；
- **批内重复 request_id 拒绝**：同一 `request_id` 在 `items` 中出现多次时整批 `409`（`reason=duplicate_request_id_in_batch`），不写入任何数据；
- **已存在凭证幂等回放**：`request_id` 已在库中时按单笔过账同样的语义原样回放（`idempotent_replay: true`，HTTP `200`），不重复入账、不重复占用支出限额；批内既有新建又有回放时整批返回 `201`，全部为回放时返回 `200`（响应头 `X-Idempotent-Replay: true`）；
- **限额顺序累计**：复用单笔的期间、借贷、币种与费用月度限额校验；同批前序新凭证已 flush 的借方费用会被后序项的已用额聚合计入，因此前序占用必然影响后序校验（例如限额 1000，批内连续两张 600/500 的费用凭证会在第 2 张被拒并整批回滚）。

### 4. 结账与冲销

```bash
# 冻结 2026-09，之后不能再向该期间入账（409）
curl -XPOST localhost:8000/periods/2026-09/close

# 唯一允许的撤销方式：反向凭证（方向对调、金额不变、保留关联），可冲入 10 月
curl -XPOST localhost:8000/vouchers/1/reverse -H 'Content-Type: application/json' -d '{
  "request_id": "grant-2026-0001-rev",
  "date": "2026-10-08",
  "memo": "撤销错误入账"
}'
```

### 5. 查询余额与凭证链

```bash
curl localhost:8000/accounts/1/balances
curl localhost:8000/accounts/1/ledger
curl localhost:8000/vouchers/1/chain
```

### 6. 试算平衡表（只读）

汇总截止期间（含本期及此前各期间）的累计发生额与期末余额，按币种分组：

```bash
curl 'localhost:8000/reports/trial-balance?period=2026-09'
```

```json
{
  "period": "2026-09",
  "currencies": [
    {
      "currency": "CNY",
      "total_debit": "1000000.00",
      "total_credit": "1000000.00",
      "balanced": true,
      "accounts": [
        {
          "account_id": 1,
          "account_code": "1002",
          "account_name": "银行存款-人民币",
          "account_type": "asset",
          "total_debit": "1000000.00",
          "total_credit": "350000.00",
          "closing_debit": "650000.00",
          "closing_credit": "0.00",
          "balance_direction": "debit"
        },
        {
          "account_id": 5,
          "account_code": "4001",
          "account_name": "科研拨款收入",
          "account_type": "income",
          "total_debit": "0.00",
          "total_credit": "1000000.00",
          "closing_debit": "0.00",
          "closing_credit": "1000000.00",
          "balance_direction": "credit"
        }
      ]
    }
  ]
}
```

说明：

- 每个科目返回累计借方/贷方发生额（`total_debit` / `total_credit`）及期末借/贷余额（`closing_debit` / `closing_credit`，互斥地只在净额方向有值）和 `balance_direction`；
- 累计借 = 累计贷（期末无余额）的科目**不列出**；
- 币种级 `total_debit` / `total_credit` 是该币种全部列示科目的**期末余额合计**，`balanced` 表示二者是否相等；复式记账下各币种应恒为 `true`，否则意味着账套存在脏数据；
- 该接口只读：不提交事务、不自动创建期间（对比 `GET /periods/{key}` 会自动开账）、不触碰余额与凭证。

### 7. 费用科目月度支出限额

为费用科目（`type=expense`）按月份设置支出上限，限额金额沿用十进制字符串（整数分存储），只有费用科目可以设置，重复 `PUT` 即为调整（覆盖）：

```bash
# 5001 科研设备费 2026-09 月度限额 1000.00
curl -XPUT localhost:8000/accounts/5/limits/2026-09 \
  -H 'Content-Type: application/json' -d '{"amount": "1000.00"}'

# 查询：限额、本月已过账未冲销借方费用合计、剩余额度
curl localhost:8000/accounts/5/limits/2026-09
curl localhost:8000/accounts/5/limits          # 该科目全部月份
```

```json
{
  "account_id": 5, "account_code": "5001", "account_name": "科研设备费",
  "currency": "CNY", "period_key": "2026-09",
  "limit": "1000.00", "used_debit": "600.00", "remaining": "400.00",
  "created_at": "...", "updated_at": "..."
}
```

过账时，系统在**同一个 `BEGIN IMMEDIATE` 写事务**内（位于借贷校验之后、分配凭证号之前）比较：`本次凭证对该费用科目的借方金额 + 本月已过账且未被冲销的借方费用合计`。超限整张凭证被拒绝（`422`），凭证、凭证号、余额均不写入：

```bash
# 已用 600，本次再借 500 -> 422，整体拒绝
curl -XPOST localhost:8000/vouchers -H 'Content-Type: application/json' -d '{
  "request_id": "equip-2026-0007", "date": "2026-09-21", "currency": "CNY",
  "lines": [
    {"account_id": 5, "direction": "debit",  "amount": "500.00"},
    {"account_id": 1, "direction": "credit", "amount": "500.00"}
  ]
}'
```

```json
{ "error": { "code": "unprocessable_entity",
             "message": "费用科目 5001 在 2026-09 的支出限额不足",
             "detail": { "reason": "spending_limit_exceeded",
                         "account_code": "5001", "period_key": "2026-09",
                         "limit_minor": 100000, "used_minor": 60000,
                         "new_debit_minor": 50000, "total_minor": 110000,
                         "available_minor": 40000 } } }
```

限额语义：

- **只约束费用科目的借方发生额**（费用增加方向）；费用科目出现在贷方（如冲销、退回）不受限额约束，非费用科目也不受约束；
- **幂等回放不占限额**：重复 `request_id` 在任何校验之前先回放原凭证（`200` + `X-Idempotent-Replay: true`），不会二次累计已用额；
- **冲销释放额度**：已用额从分录实时聚合，被冲销凭证（`status=reversed`）与反向凭证均不计入。同月冲销后额度立即恢复可用；跨期冲销时，原期间重新打开后该被冲销凭证同样不计入原期间已用额；反向凭证本身永远不做限额校验；
- **限额可随时下调**：若调到已用额以下，`remaining` 以负数展示（如 `"-300.00"`），已过账数据不回滚，但之后任何新增借方费用都会被拒绝，直到上调限额或冲销释放额度；限额为 `"0.00"` 即冻结该月新增费用；
- **并发不共超**：所有过账与限额调整都在事务起点以 `BEGIN IMMEDIATE` 取得保留锁、串行提交，后一个事务看到的已用额必然包含前一个已提交事务，因此两笔并发过账不可能各自"看到旧余额"而共同突破上限；
- 限额不随期间结账冻结：结账后仍可调整限额（但期间已关闭，本就无法再向该期间入账）。

## 关键设计说明

### 复式记账规则

- 分录金额为非负整数分，方向只取 `debit` / `credit`；
- 过账条件：`Σ借方 = Σ贷方`，且两侧都至少有一行；
- 余额字段：`total_debit`、`total_credit`、`net_debit = 借−贷`、`net_credit = 贷−借`（资产/费用正常为借方余额，负债/收入为贷方余额）；
- 反向凭证不是负数金额，而是**金额不变、方向全部对调**的新凭证，因此它本身仍是一张合法的、借贷平衡的凭证。

### 不可变性（两层防护）

1. API 层根本不提供凭证修改/删除端点（`DELETE` 返回 405）；
2. SQLite 触发器拦截凭证头关键字段更新、分录的更新/删除、凭证与余额的删除。过账后唯一允许的变化是登记冲销关系（`status: posted → reversed`、`reversed_by_id`），且状态不可回退。

### 事务与并发

所有写入（单笔校验 → 编号 → 凭证/分录 → 余额 UPSERT → 冲销回填）在单个事务内完成。连接配置：

- pysqlite autocommit 模式由 SQLAlchemy 全权管理事务边界；
- 每个事务以 `BEGIN IMMEDIATE` 开始，进入即取保留锁，写冲突在事务起点排队（`busy_timeout=10s`），而非在提交时抛锁错误；
- WAL 模式允许读写并发。

批量过账 (`POST /vouchers/batch`) 复用同一套逐项流程但**全批共用一个事务**：批内重复 `request_id` 在开启任何写入前先拒绝；逐项按请求顺序处理，每张新凭证写完凭证头/分录后立即 `flush`，使同批后序项的月度限额实时聚合（`voucher_lines` 的 `SUM`）能看到前序新凭证的借方费用占用；任一项抛错则 `rollback` 整批——新凭证、期间顺序凭证号（编号表 UPSERT 同样回滚，不留号段空洞）与余额全部撤销，已存在的回放凭证本就未被修改。

### 金额处理

API 边界使用字符串（正则限定非负、最多两位小数），服务端用 `Decimal` 转为整数分存储（`app/money.py`），比较与汇总全部为整数运算。

### 月度支出限额

- 限额独立存于 `spending_limits` 表（科目 × 期间唯一，整数分，可随时 PUT 覆盖），与凭证不可变性互不影响；
- 已用额不另建计数器，而是在过账事务内对 `voucher_lines JOIN vouchers` 实时 `SUM`，过滤 `status='posted' AND reverses_id IS NULL`——这天然排除被冲销凭证与反向凭证，冲销无需回写任何限额状态，也没有计数器与凭证不一致的可能；
- 限额校验位于过账事务的"校验阶段"（幂等检查、期间、币种、借贷平衡之后，编号与写入之前），失败即整体回滚，不写入凭证、不分配凭证号、不改动余额；
- 并发安全直接复用既有的 `BEGIN IMMEDIATE` 串行写模型：限额调整与过账都是写事务，在 SQLite 层面排队，先提交者的已用额对后提交者必然可见，无需额外加锁。

### 试算平衡表（只读报表）

- 数据直接来自物化的 `balances` 表：`WHERE period_key <= 截止期` 按科目 `SUM(debit_minor)/SUM(credit_minor)`，一次查询拿到全部科目累计额，再在内存按币种分组；
- 期末余额 = 累计借 − 累计贷：为正列借方余额，为负列贷方余额，为 0 的科目不列出；
- 币种级合计只汇总期末余额（不是发生额），`balanced = 借方合计 = 贷方合计`；
- 由于凭证按币种自平衡（一张凭证不允许跨币种），各币种在无脏数据时恒平衡。服务层只做 SELECT、不 commit，也不会像 `GET /periods/{key}` 那样隐式创建期间。

## 项目结构

```
.
├── app/
│   ├── main.py              # FastAPI 入口、生命周期、异常处理
│   ├── config.py            # 配置 (DB URL、金额上限、busy timeout)
│   ├── database.py          # 引擎/会话、BEGIN IMMEDIATE、建表与触发器
│   ├── models.py            # ORM: Account/Period/Voucher/VoucherLine/Balance/SpendingLimit/...
│   ├── schemas.py           # Pydantic 请求/响应模型
│   ├── money.py             # 字符串金额 ↔ 整数分、期间键
│   ├── errors.py            # 统一业务异常
│   ├── serializers.py       # ORM -> 响应模型
│   ├── seed.py              # 演示账套 (幂等)
│   ├── routers/             # accounts (含支出限额) / periods / vouchers / reports
│   └── services/            # master_data（科目/期间）、voucher（过账/冲销/查询）、spending_limits（限额设置/校验）、reports（只读报表）
├── tests/test_api.py        # 33 组端到端测试
├── requirements.txt
├── Dockerfile
└── docker-compose.yml
```
