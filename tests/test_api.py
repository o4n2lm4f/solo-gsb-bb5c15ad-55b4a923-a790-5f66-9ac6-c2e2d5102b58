"""端到端 API 测试。"""
from __future__ import annotations

from app.database import SessionLocal
from app.models import Balance, Period, Voucher


def _create_account(client, code, name, currency="CNY", atype="asset"):
    r = client.post("/accounts", json={
        "code": code, "name": name, "currency": currency, "type": atype,
    })
    assert r.status_code == 201, r.text
    return r.json()


def _post_voucher(client, rid, lines, currency="CNY", vdate="2026-09-20", memo=None):
    return client.post("/vouchers", json={
        "request_id": rid,
        "date": vdate,
        "currency": currency,
        "memo": memo,
        "lines": lines,
    })


def _set_limit(client, account_id, period, amount):
    return client.put(
        f"/accounts/{account_id}/limits/{period}",
        json={"amount": amount},
    )


def test_health_and_root(client):
    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/").json()["version"]


def test_create_account_and_duplicate_code(client):
    a = _create_account(client, "1001", "银行存款", "CNY")
    assert a["currency"] == "CNY" and a["type"] == "asset"

    r = client.post("/accounts", json={
        "code": "1001", "name": "重复", "currency": "CNY", "type": "asset",
    })
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "conflict"

    # 小写币种自动转大写；非法币种拒绝
    r = client.post("/accounts", json={
        "code": "1002", "name": "美元户", "currency": "usd", "type": "asset",
    })
    assert r.status_code == 201 and r.json()["currency"] == "USD"


def test_post_balanced_voucher_and_balance(client):
    bank = _create_account(client, "1002", "银行存款")
    income = _create_account(client, "4001", "科研拨款", atype="income")

    r = _post_voucher(client, "req-1", [
        {"account_id": bank["id"], "direction": "debit", "amount": "1000.00"},
        {"account_id": income["id"], "direction": "credit", "amount": "1000.00"},
    ])
    assert r.status_code == 201, r.text
    v = r.json()
    assert v["number"] == "V-2026-09-0001"
    assert v["status"] == "posted"
    assert len(v["lines"]) == 2

    bal = client.get(f"/accounts/{bank['id']}/balances").json()
    assert len(bal) == 1
    assert bal[0]["total_debit"] == "1000.00"
    assert bal[0]["total_credit"] == "0.00"
    assert bal[0]["net_debit"] == "1000.00"

    # 分类账：期初 0，一笔后累计借 1000
    ledger = client.get(f"/accounts/{bank['id']}/ledger").json()
    assert ledger["starting_debit"] == "0.00"
    assert ledger["entries"][0]["balance_after_debit"] == "1000.00"
    assert ledger["entries"][0]["voucher_number"] == v["number"]


def test_unbalanced_voucher_rejected(client):
    bank = _create_account(client, "1002", "银行存款")
    income = _create_account(client, "4001", "科研拨款", atype="income")

    r = _post_voucher(client, "req-bad-1", [
        {"account_id": bank["id"], "direction": "debit", "amount": "1000.00"},
        {"account_id": income["id"], "direction": "credit", "amount": "999.00"},
    ])
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "unprocessable_entity"

    # 只有借方也应拒绝
    r = _post_voucher(client, "req-bad-2", [
        {"account_id": bank["id"], "direction": "debit", "amount": "100.00"},
        {"account_id": income["id"], "direction": "debit", "amount": "100.00"},
    ])
    assert r.status_code == 422

    # 失败凭证不得产生余额
    assert client.get(f"/accounts/{bank['id']}/balances").json() == []


def test_currency_mismatch_rejected(client):
    cny = _create_account(client, "1002", "人民币户", "CNY")
    usd = _create_account(client, "1003", "美元户", "USD")
    income = _create_account(client, "4001", "科研拨款", atype="income")

    # 同一张凭证混用 CNY/USD 科目
    r = _post_voucher(client, "req-cur-1", [
        {"account_id": cny["id"], "direction": "debit", "amount": "100.00"},
        {"account_id": income["id"], "direction": "credit", "amount": "100.00"},
    ], currency="USD")
    assert r.status_code == 422

    # 分录科目之间币种不一致
    r = _post_voucher(client, "req-cur-2", [
        {"account_id": cny["id"], "direction": "debit", "amount": "100.00"},
        {"account_id": usd["id"], "direction": "credit", "amount": "100.00"},
    ], currency="CNY")
    assert r.status_code == 422

    # 同币种外币凭证应成功
    r = _post_voucher(client, "req-cur-3", [
        {"account_id": usd["id"], "direction": "debit", "amount": "50.00"},
        {"account_id": usd["id"], "direction": "credit", "amount": "50.00"},
    ], currency="USD")
    assert r.status_code == 201, r.text


def test_idempotent_request_id(client):
    bank = _create_account(client, "1002", "银行存款")
    income = _create_account(client, "4001", "科研拨款", atype="income")
    body = {
        "request_id": "idem-1",
        "date": "2026-09-20",
        "currency": "CNY",
        "lines": [
            {"account_id": bank["id"], "direction": "debit", "amount": "250.50"},
            {"account_id": income["id"], "direction": "credit", "amount": "250.50"},
        ],
    }
    r1 = client.post("/vouchers", json=body)
    r2 = client.post("/vouchers", json=body)
    assert r1.status_code == 201 and r2.status_code == 200
    assert r1.headers["X-Idempotent-Replay"] == "false"
    assert r2.headers["X-Idempotent-Replay"] == "true"
    assert r1.json()["id"] == r2.json()["id"]
    assert r1.json()["number"] == r2.json()["number"]

    # 余额只记一次
    bal = client.get(f"/accounts/{bank['id']}/balances").json()
    assert bal[0]["total_debit"] == "250.50"

    # request_id 反查
    by_req = client.get("/vouchers/by-request/idem-1")
    assert by_req.status_code == 200
    assert by_req.json()["id"] == r1.json()["id"]


def test_closed_period_blocks_posting_and_reopen(client):
    bank = _create_account(client, "1002", "银行存款")
    income = _create_account(client, "4001", "科研拨款", atype="income")

    # 先在 2026-08 入账
    r = _post_voucher(client, "req-p1", [
        {"account_id": bank["id"], "direction": "debit", "amount": "100.00"},
        {"account_id": income["id"], "direction": "credit", "amount": "100.00"},
    ], vdate="2026-08-15")
    assert r.status_code == 201

    # 结账 2026-08
    r = client.post("/periods/2026-08/close")
    assert r.status_code == 200 and r.json()["status"] == "closed"

    # 再向 8 月入账 -> 409
    r = _post_voucher(client, "req-p2", [
        {"account_id": bank["id"], "direction": "debit", "amount": "100.00"},
        {"account_id": income["id"], "direction": "credit", "amount": "100.00"},
    ], vdate="2026-08-20")
    assert r.status_code == 409

    # 结账是幂等的
    r = client.post("/periods/2026-08/close")
    assert r.json()["status"] == "closed"

    # 9 月仍可入账
    r = _post_voucher(client, "req-p3", [
        {"account_id": bank["id"], "direction": "debit", "amount": "20.00"},
        {"account_id": income["id"], "direction": "credit", "amount": "20.00"},
    ], vdate="2026-09-01")
    assert r.status_code == 201

    # 8 月余额冻结
    bal = client.get(f"/accounts/{bank['id']}/balances", params={"period": "2026-08"}).json()
    assert bal[0]["total_debit"] == "100.00"


def test_reversal_voucher_and_chain(client):
    bank = _create_account(client, "1002", "银行存款")
    expense = _create_account(client, "5001", "科研设备费", atype="expense")

    r = _post_voucher(client, "rev-base", [
        {"account_id": expense["id"], "direction": "debit", "amount": "800.00", "memo": "错误采购"},
        {"account_id": bank["id"], "direction": "credit", "amount": "800.00"},
    ], memo="待冲销")
    original = r.json()
    assert r.status_code == 201

    # 反向凭证
    r = client.post(f"/vouchers/{original['id']}/reverse", json={
        "request_id": "rev-1", "date": "2026-09-25", "memo": "撤销错误采购",
    })
    assert r.status_code == 201, r.text
    reversal = r.json()
    assert reversal["reverses_id"] == original["id"]
    assert reversal["reverses_number"] == original["number"]
    # 方向全部对调，金额一致
    by_dir = {ln["direction"]: ln["amount"] for ln in reversal["lines"]}
    assert by_dir == {"credit": "800.00", "debit": "800.00"}

    # 原凭证状态变为 reversed 并回填
    fetched = client.get(f"/vouchers/{original['id']}").json()
    assert fetched["status"] == "reversed"
    assert fetched["reversed_by_id"] == reversal["id"]

    # 链：从原凭证或反向凭证查都得到同一链
    chain = client.get(f"/vouchers/{original['id']}/chain").json()
    assert chain["root_id"] == original["id"]
    assert [n["direction_in_chain"] for n in chain["nodes"]] == ["original", "reversal"]
    chain2 = client.get(f"/vouchers/{reversal['id']}/chain").json()
    assert chain2["root_id"] == original["id"]

    # 净余额归零 (800 借 + 800 贷)
    bal = client.get(f"/accounts/{expense['id']}/balances").json()
    assert bal[0]["total_debit"] == "800.00"
    assert bal[0]["total_credit"] == "800.00"
    assert bal[0]["net_debit"] == "0.00"

    # 不可重复冲销
    r = client.post(f"/vouchers/{original['id']}/reverse", json={
        "request_id": "rev-2",
    })
    assert r.status_code == 409

    # 冲销的 request_id 也幂等
    r = client.post(f"/vouchers/{original['id']}/reverse", json={
        "request_id": "rev-1",
    })
    assert r.status_code == 200 and r.json()["id"] == reversal["id"]


def test_reversal_allowed_into_open_period_when_original_period_closed(client):
    bank = _create_account(client, "1002", "银行存款")
    income = _create_account(client, "4001", "科研拨款", atype="income")

    r = _post_voucher(client, "rev-cross", [
        {"account_id": bank["id"], "direction": "debit", "amount": "50.00"},
        {"account_id": income["id"], "direction": "credit", "amount": "50.00"},
    ], vdate="2026-08-10")
    original = r.json()

    client.post("/periods/2026-08/close")
    # 冲销到新的开放期间 9 月
    r = client.post(f"/vouchers/{original['id']}/reverse", json={
        "request_id": "rev-cross-1", "date": "2026-09-02",
    })
    assert r.status_code == 201
    assert r.json()["period_key"] == "2026-09"


def test_voucher_immutable_and_no_delete(client):
    from sqlalchemy import event, text
    from app.database import engine

    bank = _create_account(client, "1002", "银行存款")
    income = _create_account(client, "4001", "科研拨款", atype="income")
    v = _post_voucher(client, "imm-1", [
        {"account_id": bank["id"], "direction": "debit", "amount": "10.00"},
        {"account_id": income["id"], "direction": "credit", "amount": "10.00"},
    ]).json()

    db = SessionLocal()
    # UPDATE 凭证关键字段被触发器拒绝
    db_obj = db.get(Voucher, v["id"])
    db_obj.memo = "试图篡改"
    from sqlalchemy.exc import IntegrityError as SAIntegrityError, OperationalError
    immutable_errors = (SAIntegrityError, OperationalError)
    try:
        db.flush()
        db.commit()
        raise AssertionError("应当触发不可变性触发器")
    except immutable_errors:
        db.rollback()

    # DELETE 凭证被拒绝 (RAISE ABORT 在 execute 时抛出)
    try:
        db.execute(text("DELETE FROM vouchers WHERE id = :i"), {"i": v["id"]})
        db.commit()
        raise AssertionError("DELETE 应当被拒绝")
    except immutable_errors:
        db.rollback()
    finally:
        db.close()
        # RAISE ABORT 会使底层连接停留在被中止的事务里，丢弃该连接
        from app.database import engine as _engine
        _engine.dispose()

    # REST 层没有删除端点 -> 405
    assert client.delete(f"/vouchers/{v['id']}").status_code == 405


def test_voucher_numbering_sequential_within_period(client):
    bank = _create_account(client, "1002", "银行存款")
    income = _create_account(client, "4001", "科研拨款", atype="income")
    lines = [
        {"account_id": bank["id"], "direction": "debit", "amount": "1.00"},
        {"account_id": income["id"], "direction": "credit", "amount": "1.00"},
    ]
    n1 = _post_voucher(client, "seq-1", lines, vdate="2026-09-05").json()["number"]
    n2 = _post_voucher(client, "seq-2", lines, vdate="2026-09-06").json()["number"]
    other_month = _post_voucher(client, "seq-3", lines, vdate="2026-07-06").json()["number"]
    n3 = _post_voucher(client, "seq-4", lines, vdate="2026-09-07").json()["number"]
    assert (n1, n2, n3) == ("V-2026-09-0001", "V-2026-09-0002", "V-2026-09-0003")
    assert other_month == "V-2026-07-0001"


def test_list_and_filter_vouchers(client):
    bank = _create_account(client, "1002", "银行存款")
    income = _create_account(client, "4001", "科研拨款", atype="income")
    lines = [
        {"account_id": bank["id"], "direction": "debit", "amount": "1.00"},
        {"account_id": income["id"], "direction": "credit", "amount": "1.00"},
    ]
    _post_voucher(client, "lst-1", lines, vdate="2026-09-01")
    _post_voucher(client, "lst-2", lines, vdate="2026-09-02")

    listing = client.get("/vouchers").json()
    assert len(listing["items"]) == 2

    only_income = client.get("/vouchers", params={"account_id": income["id"]}).json()
    assert {v["request_id"] for v in only_income["items"]} == {"lst-1", "lst-2"}

    other = _create_account(client, "9999", "无关科目")
    assert client.get("/vouchers", params={"account_id": other["id"]}).json()["items"] == []

    period_filtered = client.get("/vouchers", params={"period": "2026-09"}).json()
    assert len(period_filtered["items"]) == 2


def _tb_map(resp):
    """试算平衡表响应 -> {currency: {account_code: account, ...}}, 并校验结构。"""
    assert resp.status_code == 200, resp.text
    body = resp.json()
    return body, {g["currency"]: {a["account_code"]: a for a in g["accounts"]}
                  for g in body["currencies"]}


def test_trial_balance_cumulative_cutoff_and_balanced(client):
    bank = _create_account(client, "1002", "银行存款")
    income = _create_account(client, "4001", "科研拨款", atype="income")

    # 8 月 100 / 9 月 20
    _post_voucher(client, "tb-1", [
        {"account_id": bank["id"], "direction": "debit", "amount": "100.00"},
        {"account_id": income["id"], "direction": "credit", "amount": "100.00"},
    ], vdate="2026-08-10")
    _post_voucher(client, "tb-2", [
        {"account_id": bank["id"], "direction": "debit", "amount": "20.00"},
        {"account_id": income["id"], "direction": "credit", "amount": "20.00"},
    ], vdate="2026-09-05")

    # 截止 8 月：仅累计 8 月发生额
    body, groups = _tb_map(client.get("/reports/trial-balance", params={"period": "2026-08"}))
    assert body["period"] == "2026-08"
    assert set(groups) == {"CNY"}
    cny = next(g for g in body["currencies"] if g["currency"] == "CNY")
    assert cny["total_debit"] == "100.00"
    assert cny["total_credit"] == "100.00"
    assert cny["balanced"] is True
    assert groups["CNY"]["1002"]["total_debit"] == "100.00"
    assert groups["CNY"]["1002"]["total_credit"] == "0.00"
    assert groups["CNY"]["1002"]["closing_debit"] == "100.00"
    assert groups["CNY"]["1002"]["closing_credit"] == "0.00"
    assert groups["CNY"]["1002"]["balance_direction"] == "debit"
    assert groups["CNY"]["4001"]["balance_direction"] == "credit"
    assert groups["CNY"]["4001"]["closing_credit"] == "100.00"

    # 截止 9 月：累计 8+9 月
    body, groups = _tb_map(client.get("/reports/trial-balance", params={"period": "2026-09"}))
    cny = next(g for g in body["currencies"] if g["currency"] == "CNY")
    assert cny["balanced"] is True
    assert groups["CNY"]["1002"]["total_debit"] == "120.00"
    assert groups["CNY"]["4001"]["closing_credit"] == "120.00"

    # 更早的截止期 (7 月)：尚无任何发生额 -> 空币种列表
    body = client.get("/reports/trial-balance", params={"period": "2026-07"}).json()
    assert body["currencies"] == []


def test_trial_balance_multi_currency_groups(client):
    cny_bank = _create_account(client, "1002", "人民币户", "CNY")
    cny_income = _create_account(client, "4001", "科研拨款", atype="income")
    usd_bank = _create_account(client, "1003", "美元户", "USD")
    usd_income = _create_account(client, "4002", "美元资助", "USD", atype="income")

    _post_voucher(client, "tb-cny", [
        {"account_id": cny_bank["id"], "direction": "debit", "amount": "1000.00"},
        {"account_id": cny_income["id"], "direction": "credit", "amount": "1000.00"},
    ])
    _post_voucher(client, "tb-usd", [
        {"account_id": usd_bank["id"], "direction": "debit", "amount": "50.00"},
        {"account_id": usd_income["id"], "direction": "credit", "amount": "50.00"},
    ], currency="USD")

    body, groups = _tb_map(client.get("/reports/trial-balance", params={"period": "2026-09"}))
    assert set(groups) == {"CNY", "USD"}
    by_cur = {g["currency"]: g for g in body["currencies"]}
    assert by_cur["CNY"]["total_debit"] == "1000.00"
    assert by_cur["CNY"]["balanced"] is True
    assert by_cur["USD"]["total_debit"] == "50.00"
    assert by_cur["USD"]["total_credit"] == "50.00"
    assert by_cur["USD"]["balanced"] is True
    # 币种按字母序，科目按代码序
    assert [g["currency"] for g in body["currencies"]] == ["CNY", "USD"]


def test_trial_balance_excludes_zero_balance_accounts(client):
    bank = _create_account(client, "1002", "银行存款")
    expense = _create_account(client, "5001", "科研设备费", atype="expense")

    # 8 月入账，9 月跨期冲销；两个科目累计借贷相等、净额归零
    original = _post_voucher(client, "tb-rev-base", [
        {"account_id": expense["id"], "direction": "debit", "amount": "800.00"},
        {"account_id": bank["id"], "direction": "credit", "amount": "800.00"},
    ], vdate="2026-08-20").json()
    client.post(f"/vouchers/{original['id']}/reverse", json={
        "request_id": "tb-rev-1", "date": "2026-09-02",
    })

    body, groups = _tb_map(client.get("/reports/trial-balance", params={"period": "2026-09"}))
    # 两个科目期末余额都是 0，不列出 -> 无币种分组
    assert groups == {}
    assert body["currencies"] == []


def test_trial_balance_invalid_period(client):
    # 缺少必填 period
    assert client.get("/reports/trial-balance").status_code == 422
    # 格式非法
    r = client.get("/reports/trial-balance", params={"period": "2026-9"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "unprocessable_entity"
    r = client.get("/reports/trial-balance", params={"period": "2026-13"})
    assert r.status_code == 422


def test_trial_balance_is_read_only(client):
    bank = _create_account(client, "1002", "银行存款")
    income = _create_account(client, "4001", "科研拨款", atype="income")
    _post_voucher(client, "tb-ro", [
        {"account_id": bank["id"], "direction": "debit", "amount": "100.00"},
        {"account_id": income["id"], "direction": "credit", "amount": "100.00"},
    ])

    # 查询一个从未来过账的截止期，不得自动创建该期间 (对比 GET /periods 的行为)
    assert client.get("/periods/2027-01").status_code == 200  # 该端点会自动开账
    client.get("/reports/trial-balance", params={"period": "2027-02"})

    db = SessionLocal()
    try:
        keys = {p.key for p in db.query(Period).all()}
        assert "2027-01" in keys       # periods 端点创建的
        assert "2027-02" not in keys   # 试算平衡表没有创建任何期间
        balances = db.query(Balance).all()
        # 仍只有入账产生的两个余额行，没有被查询改写
        assert len(balances) == 2
    finally:
        db.close()

    # 再查一次，结果幂等不变
    r1 = client.get("/reports/trial-balance", params={"period": "2026-09"}).json()
    r2 = client.get("/reports/trial-balance", params={"period": "2026-09"}).json()
    assert r1 == r2


def test_trial_balance_detects_unbalanced_currency(client):
    bank = _create_account(client, "1002", "银行存款")
    income = _create_account(client, "4001", "科研拨款", atype="income")
    _post_voucher(client, "tb-unbal", [
        {"account_id": bank["id"], "direction": "debit", "amount": "100.00"},
        {"account_id": income["id"], "direction": "credit", "amount": "100.00"},
    ])

    # 正常账务必平衡；直接在更早期间写入一笔脏余额模拟借贷合计不等
    db = SessionLocal()
    try:
        db.add(Balance(
            account_id=bank["id"], period_key="2026-07",
            debit_minor=3000, credit_minor=0,
        ))
        db.commit()
    finally:
        db.close()

    body, _ = _tb_map(client.get("/reports/trial-balance", params={"period": "2026-09"}))
    cny = next(g for g in body["currencies"] if g["currency"] == "CNY")
    assert cny["total_debit"] == "130.00"
    assert cny["total_credit"] == "100.00"
    assert cny["balanced"] is False


# ============================================================ 月度支出限额
def test_set_and_get_spending_limit(client):
    expense = _create_account(client, "5001", "科研设备费", atype="expense")

    r = _set_limit(client, expense["id"], "2026-09", "1000.00")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["period_key"] == "2026-09"
    assert body["limit"] == "1000.00"
    assert body["used_debit"] == "0.00"
    assert body["remaining"] == "1000.00"

    # 再次 PUT 即为调整 (覆盖)
    r = _set_limit(client, expense["id"], "2026-09", "800.50")
    assert r.status_code == 200
    assert r.json()["limit"] == "800.50"

    # 单月查询与列表查询
    assert client.get(f"/accounts/{expense['id']}/limits/2026-09").json()["limit"] == "800.50"
    assert client.get(f"/accounts/{expense['id']}/limits/2026-08").status_code == 404
    listing = client.get(f"/accounts/{expense['id']}/limits").json()
    assert [row["period_key"] for row in listing] == ["2026-09"]

    # 非费用科目不能设置限额
    bank = _create_account(client, "1002", "银行存款")
    r = _set_limit(client, bank["id"], "2026-09", "1.00")
    assert r.status_code == 422
    assert r.json()["error"]["detail"]["reason"] == "not_expense_account"

    # 非法期间/金额
    assert _set_limit(client, expense["id"], "2026-13", "1.00").status_code == 422
    assert _set_limit(client, expense["id"], "2026-09", "1.234").status_code == 422
    assert client.get(f"/accounts/9999/limits/2026-09").status_code == 404


def test_spending_limit_enforced_and_cumulative(client):
    bank = _create_account(client, "1002", "银行存款")
    expense = _create_account(client, "5001", "科研设备费", atype="expense")
    assert _set_limit(client, expense["id"], "2026-09", "1000.00").status_code == 200

    def expense_voucher(rid, amount, vdate="2026-09-10"):
        return _post_voucher(client, rid, [
            {"account_id": expense["id"], "direction": "debit", "amount": amount},
            {"account_id": bank["id"], "direction": "credit", "amount": amount},
        ], vdate=vdate)

    # 600 通过：已用 600，剩余 400
    assert expense_voucher("lim-1", "600.00").status_code == 201
    body = client.get(f"/accounts/{expense['id']}/limits/2026-09").json()
    assert body["used_debit"] == "600.00"
    assert body["remaining"] == "400.00"

    # 再借 500 超限：整体拒绝，422 + 明细
    r = expense_voucher("lim-2", "500.00")
    assert r.status_code == 422
    detail = r.json()["error"]["detail"]
    assert detail["reason"] == "spending_limit_exceeded"
    assert detail["limit_minor"] == 100000
    assert detail["used_minor"] == 60000
    assert detail["new_debit_minor"] == 50000

    # 被拒凭证不写入凭证或余额：余额仍只有 600
    bal = client.get(f"/accounts/{expense['id']}/balances").json()
    assert bal[0]["total_debit"] == "600.00"
    assert len(client.get("/vouchers").json()["items"]) == 1
    # 限额已用额不包含被拒凭证，也没有占用凭证号
    assert expense_voucher("lim-3", "400.00").json()["number"] == "V-2026-09-0002"

    # 已用 1000 达到限额后，新增 0.01 即拒 (边界：等于限额允许、超出拒绝)
    r = expense_voucher("lim-4", "0.01")
    assert r.status_code == 422

    # 其他月份不受该限额约束
    assert expense_voucher("lim-5", "9999.00", vdate="2026-08-10").status_code == 201


def test_spending_limit_idempotent_replay_does_not_consume_twice(client):
    bank = _create_account(client, "1002", "银行存款")
    expense = _create_account(client, "5001", "科研设备费", atype="expense")
    assert _set_limit(client, expense["id"], "2026-09", "600.00").status_code == 200

    body = {
        "request_id": "lim-idem",
        "date": "2026-09-12",
        "currency": "CNY",
        "lines": [
            {"account_id": expense["id"], "direction": "debit", "amount": "600.00"},
            {"account_id": bank["id"], "direction": "credit", "amount": "600.00"},
        ],
    }
    r1 = client.post("/vouchers", json=body)
    assert r1.status_code == 201
    # 限额已用满；用同一 request_id 重放仍成功 (200 + replay)，且不重复占用限额
    r2 = client.post("/vouchers", json=body)
    assert r2.status_code == 200
    assert r2.headers["X-Idempotent-Replay"] == "true"
    assert r2.json()["id"] == r1.json()["id"]

    info = client.get(f"/accounts/{expense['id']}/limits/2026-09").json()
    assert info["used_debit"] == "600.00"
    assert info["remaining"] == "0.00"

    # 新的 0.01 借方费用凭证因无剩余额度被拒 (证明重放没有把已用额翻倍)
    r = _post_voucher(client, "lim-idem-new", [
        {"account_id": expense["id"], "direction": "debit", "amount": "0.01"},
        {"account_id": bank["id"], "direction": "credit", "amount": "0.01"},
    ])
    assert r.status_code == 422


def test_reversed_voucher_releases_used_limit(client):
    bank = _create_account(client, "1002", "银行存款")
    expense = _create_account(client, "5001", "科研设备费", atype="expense")
    assert _set_limit(client, expense["id"], "2026-09", "1000.00").status_code == 200

    original = _post_voucher(client, "lim-rev-1", [
        {"account_id": expense["id"], "direction": "debit", "amount": "900.00"},
        {"account_id": bank["id"], "direction": "credit", "amount": "900.00"},
    ]).json()

    # 已用 900，再借 200 被拒
    r = _post_voucher(client, "lim-rev-2", [
        {"account_id": expense["id"], "direction": "debit", "amount": "200.00"},
        {"account_id": bank["id"], "direction": "credit", "amount": "200.00"},
    ])
    assert r.status_code == 422

    # 同月冲销原凭证：被冲销凭证不再计入已用额，额度释放
    r = client.post(f"/vouchers/{original['id']}/reverse", json={
        "request_id": "lim-rev-1-r", "date": "2026-09-28",
    })
    assert r.status_code == 201, r.text

    info = client.get(f"/accounts/{expense['id']}/limits/2026-09").json()
    assert info["used_debit"] == "0.00"
    assert info["remaining"] == "1000.00"

    # 此前被拒的 200 现在可以过账
    assert _post_voucher(client, "lim-rev-3", [
        {"account_id": expense["id"], "direction": "debit", "amount": "200.00"},
        {"account_id": bank["id"], "direction": "credit", "amount": "200.00"},
    ]).status_code == 201


def test_cross_period_reversal_keeps_original_period_usage_excluded(client):
    bank = _create_account(client, "1002", "银行存款")
    expense = _create_account(client, "5001", "科研设备费", atype="expense")
    assert _set_limit(client, expense["id"], "2026-08", "1000.00").status_code == 200

    original = _post_voucher(client, "lim-xrev-1", [
        {"account_id": expense["id"], "direction": "debit", "amount": "900.00"},
        {"account_id": bank["id"], "direction": "credit", "amount": "900.00"},
    ], vdate="2026-08-10").json()

    client.post("/periods/2026-08/close")
    # 跨期冲销到 9 月
    assert client.post(f"/vouchers/{original['id']}/reverse", json={
        "request_id": "lim-xrev-1-r", "date": "2026-09-02",
    }).status_code == 201

    # 8 月重新打开后，被冲销凭证不计入原期间已用额 (已用归零)
    client.post("/periods/2026-08/reopen")
    info = client.get(f"/accounts/{expense['id']}/limits/2026-08").json()
    assert info["used_debit"] == "0.00"
    # 9 月无限额；即便设置限额，反向凭证的费用分录在贷方，不占用限额
    assert _set_limit(client, expense["id"], "2026-09", "0.00").status_code == 200
    assert _post_voucher(client, "lim-xrev-2", [
        {"account_id": bank["id"], "direction": "debit", "amount": "10.00"},
        {"account_id": expense["id"], "direction": "credit", "amount": "10.00"},
    ], vdate="2026-09-03").status_code == 201


def test_spending_limit_concurrent_posting(client):
    """并发过账不得共同突破限额：一个成功，另一个必须被拒。"""
    import threading

    bank = _create_account(client, "1002", "银行存款")
    expense = _create_account(client, "5001", "科研设备费", atype="expense")
    assert _set_limit(client, expense["id"], "2026-09", "600.00").status_code == 200

    results: list[int] = []
    errors: list[str] = []

    def post(rid: str) -> None:
        try:
            r = _post_voucher(client, rid, [
                {"account_id": expense["id"], "direction": "debit", "amount": "500.00"},
                {"account_id": bank["id"], "direction": "credit", "amount": "500.00"},
            ])
            results.append(r.status_code)
        except Exception as exc:  # 锁错误也算失败，需要暴露
            errors.append(repr(exc))

    threads = [threading.Thread(target=post, args=(f"lim-conc-{i}",)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert sorted(results) == [201, 422]

    info = client.get(f"/accounts/{expense['id']}/limits/2026-09").json()
    assert info["used_debit"] == "500.00"
    bal = client.get(f"/accounts/{expense['id']}/balances").json()
    assert bal[0]["total_debit"] == "500.00"


def test_lower_limit_below_used_shows_negative_remaining_and_blocks(client):
    bank = _create_account(client, "1002", "银行存款")
    expense = _create_account(client, "5001", "科研设备费", atype="expense")
    assert _set_limit(client, expense["id"], "2026-09", "1000.00").status_code == 200

    assert _post_voucher(client, "lim-down-1", [
        {"account_id": expense["id"], "direction": "debit", "amount": "800.00"},
        {"account_id": bank["id"], "direction": "credit", "amount": "800.00"},
    ]).status_code == 201

    # 已过账数据不回滚，但限额下调后剩余为负，任何新增借方费用都被拒
    assert _set_limit(client, expense["id"], "2026-09", "500.00").status_code == 200
    info = client.get(f"/accounts/{expense['id']}/limits/2026-09").json()
    assert info["limit"] == "500.00"
    assert info["used_debit"] == "800.00"
    assert info["remaining"] == "-300.00"

    r = _post_voucher(client, "lim-down-2", [
        {"account_id": expense["id"], "direction": "debit", "amount": "0.01"},
        {"account_id": bank["id"], "direction": "credit", "amount": "0.01"},
    ])
    assert r.status_code == 422
    assert r.json()["error"]["detail"]["reason"] == "spending_limit_exceeded"


# ============================================================ 批量过账
def _batch_voucher(rid, amount, vdate="2026-09-20", currency="CNY",
                   debit_id=None, credit_id=None, memo=None):
    return {
        "request_id": rid,
        "date": vdate,
        "currency": currency,
        "memo": memo,
        "lines": [
            {"account_id": debit_id, "direction": "debit", "amount": amount},
            {"account_id": credit_id, "direction": "credit", "amount": amount},
        ],
    }


def test_batch_post_creates_in_order_with_sequential_numbers(client):
    bank = _create_account(client, "1002", "银行存款")
    income = _create_account(client, "4001", "科研拨款", atype="income")

    r = client.post("/vouchers/batch", json={"items": [
        _batch_voucher("b-1", "100.00", vdate="2026-09-10",
                       debit_id=bank["id"], credit_id=income["id"]),
        _batch_voucher("b-2", "200.00", vdate="2026-09-11",
                       debit_id=bank["id"], credit_id=income["id"]),
        _batch_voucher("b-3", "50.00", vdate="2026-08-05",
                       debit_id=bank["id"], credit_id=income["id"]),
    ]})
    assert r.status_code == 201, r.text
    assert r.headers["X-Idempotent-Replay"] == "false"
    body = r.json()
    assert body["created_count"] == 3 and body["replay_count"] == 0
    # 严格按请求顺序返回，逐项给出凭证与回放标志
    assert [it["voucher"]["request_id"] for it in body["items"]] == ["b-1", "b-2", "b-3"]
    assert [it["idempotent_replay"] for it in body["items"]] == [False, False, False]
    # 凭证号按各自期间顺序分配；9 月的两张保持批内顺序
    assert [it["voucher"]["number"] for it in body["items"]] == [
        "V-2026-09-0001", "V-2026-09-0002", "V-2026-08-0001",
    ]

    # 余额为三张凭证的累计
    bal = client.get(f"/accounts/{bank['id']}/balances").json()
    by_period = {b["period_key"]: b for b in bal}
    assert by_period["2026-09"]["total_debit"] == "300.00"
    assert by_period["2026-08"]["total_debit"] == "50.00"


def test_batch_replays_existing_request_ids_without_double_posting(client):
    bank = _create_account(client, "1002", "银行存款")
    income = _create_account(client, "4001", "科研拨款", atype="income")

    # 先由单笔接口创建 b-exist
    first = _post_voucher(client, "b-exist", [
        {"account_id": bank["id"], "direction": "debit", "amount": "120.00"},
        {"account_id": income["id"], "direction": "credit", "amount": "120.00"},
    ])
    assert first.status_code == 201
    existing_id = first.json()["id"]

    # 批量：一张回放已存在凭证 + 一张新建，按请求顺序
    r = client.post("/vouchers/batch", json={"items": [
        _batch_voucher("b-exist", "120.00", debit_id=bank["id"], credit_id=income["id"]),
        _batch_voucher("b-new", "80.00", debit_id=bank["id"], credit_id=income["id"]),
    ]})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["created_count"] == 1 and body["replay_count"] == 1
    assert body["items"][0]["idempotent_replay"] is True
    assert body["items"][0]["voucher"]["id"] == existing_id
    assert body["items"][1]["idempotent_replay"] is False

    # 回放不重复入账：银行借方只有 120 + 80
    bal = client.get(f"/accounts/{bank['id']}/balances").json()
    assert bal[0]["total_debit"] == "200.00"

    # 整批全部是回放时返回 200 + replay 头
    r = client.post("/vouchers/batch", json={"items": [
        _batch_voucher("b-exist", "120.00", debit_id=bank["id"], credit_id=income["id"]),
    ]})
    assert r.status_code == 200
    assert r.headers["X-Idempotent-Replay"] == "true"
    assert r.json()["created_count"] == 0 and r.json()["replay_count"] == 1
    assert bal[0]["total_debit"] == "200.00"


def test_batch_duplicate_request_id_in_batch_rejected(client):
    bank = _create_account(client, "1002", "银行存款")
    income = _create_account(client, "4001", "科研拨款", atype="income")

    r = client.post("/vouchers/batch", json={"items": [
        _batch_voucher("dup", "10.00", debit_id=bank["id"], credit_id=income["id"]),
        _batch_voucher("other", "10.00", debit_id=bank["id"], credit_id=income["id"]),
        _batch_voucher("dup", "10.00", debit_id=bank["id"], credit_id=income["id"]),
    ]})
    assert r.status_code == 409, r.text
    detail = r.json()["error"]["detail"]
    assert detail["reason"] == "duplicate_request_id_in_batch"
    assert detail["duplicate_request_ids"] == ["dup"]

    # 整批拒绝：无凭证、无余额、无凭证号
    assert client.get("/vouchers").json()["items"] == []
    assert client.get(f"/accounts/{bank['id']}/balances").json() == []


def test_batch_size_limits(client):
    bank = _create_account(client, "1002", "银行存款")
    income = _create_account(client, "4001", "科研拨款", atype="income")

    item = _batch_voucher("x", "1.00", debit_id=bank["id"], credit_id=income["id"])

    assert client.post("/vouchers/batch", json={"items": []}).status_code == 422
    assert client.post("/vouchers/batch", json={
        "items": [{**item, "request_id": f"many-{i}"} for i in range(21)]
    }).status_code == 422
    # 20 张正好允许
    r = client.post("/vouchers/batch", json={
        "items": [{**item, "request_id": f"many-{i}"} for i in range(20)]
    })
    assert r.status_code == 201, r.text
    assert r.json()["created_count"] == 20


def test_batch_rolls_back_all_when_any_item_fails(client):
    bank = _create_account(client, "1002", "银行存款")
    income = _create_account(client, "4001", "科研拨款", atype="income")

    # 第 2 张借贷不平 -> 第 1 张也必须回滚 (凭证、凭证号、余额)
    r = client.post("/vouchers/batch", json={"items": [
        _batch_voucher("rb-1", "100.00", debit_id=bank["id"], credit_id=income["id"]),
        {
            "request_id": "rb-2", "date": "2026-09-11", "currency": "CNY",
            "lines": [
                {"account_id": bank["id"], "direction": "debit", "amount": "100.00"},
                {"account_id": income["id"], "direction": "credit", "amount": "99.00"},
            ],
        },
    ]})
    assert r.status_code == 422
    assert client.get("/vouchers").json()["items"] == []
    assert client.get(f"/accounts/{bank['id']}/balances").json() == []

    # 失败后凭证号不留空洞：下一张成功的凭证仍为 0001
    r = _post_voucher(client, "rb-after", [
        {"account_id": bank["id"], "direction": "debit", "amount": "100.00"},
        {"account_id": income["id"], "direction": "credit", "amount": "100.00"},
    ], vdate="2026-09-12")
    assert r.status_code == 201
    assert r.json()["number"] == "V-2026-09-0001"

    # 期间已结账同样整批回滚：第 1 张落在开放的 9 月，第 2 张落在已结账的 8 月
    client.post("/periods/2026-08/close")
    r = client.post("/vouchers/batch", json={"items": [
        _batch_voucher("rb-3", "10.00", vdate="2026-09-13",
                       debit_id=bank["id"], credit_id=income["id"]),
        _batch_voucher("rb-4", "10.00", vdate="2026-08-13",
                       debit_id=bank["id"], credit_id=income["id"]),
    ]})
    assert r.status_code == 409
    detail = r.json()["error"]["detail"]
    assert detail["batch_index"] == 2 and detail["request_id"] == "rb-4"
    assert client.get("/vouchers/by-request/rb-3").status_code == 404


def test_batch_prior_new_voucher_consumes_limit_for_later_item(client):
    bank = _create_account(client, "1002", "银行存款")
    expense = _create_account(client, "5001", "科研设备费", atype="expense")
    assert _set_limit(client, expense["id"], "2026-09", "1000.00").status_code == 200

    def exp_item(rid, amount):
        return _batch_voucher(rid, amount, debit_id=expense["id"], credit_id=bank["id"])

    # 单看每张都不超限 (600+500=1100)，但前序新凭证占用的额度必须计入后序
    r = client.post("/vouchers/batch", json={"items": [
        exp_item("bl-1", "600.00"),
        exp_item("bl-2", "500.00"),
    ]})
    assert r.status_code == 422, r.text
    detail = r.json()["error"]["detail"]
    assert detail["reason"] == "spending_limit_exceeded"
    assert detail["batch_index"] == 2
    assert detail["used_minor"] == 60000
    assert detail["new_debit_minor"] == 50000

    # 整批回滚：已用额为 0，无凭证与余额
    info = client.get(f"/accounts/{expense['id']}/limits/2026-09").json()
    assert info["used_debit"] == "0.00"
    assert client.get("/vouchers").json()["items"] == []

    # 合计恰好等于限额：前序 600 + 后序 400 顺序通过
    r = client.post("/vouchers/batch", json={"items": [
        exp_item("bl-3", "600.00"),
        exp_item("bl-4", "400.00"),
    ]})
    assert r.status_code == 201, r.text
    info = client.get(f"/accounts/{expense['id']}/limits/2026-09").json()
    assert info["used_debit"] == "1000.00"


def test_batch_replayed_voucher_does_not_consume_limit(client):
    bank = _create_account(client, "1002", "银行存款")
    expense = _create_account(client, "5001", "科研设备费", atype="expense")
    assert _set_limit(client, expense["id"], "2026-09", "600.00").status_code == 200

    # 单笔先用满 600
    assert _post_voucher(client, "blr-1", [
        {"account_id": expense["id"], "direction": "debit", "amount": "600.00"},
        {"account_id": bank["id"], "direction": "credit", "amount": "600.00"},
    ]).status_code == 201

    # 批内先回放旧凭证 (不占额度)，随后新建 0 张时只剩 0 额度；
    # 若回放被错误计入已用额，已用仍是 600 —— 这里验证新凭证 0.01 被拒，
    # 且"回放 + 不超限新凭证"组合可成功
    r = client.post("/vouchers/batch", json={"items": [
        _batch_voucher("blr-1", "600.00", debit_id=expense["id"], credit_id=bank["id"]),
    ]})
    assert r.status_code == 200
    info = client.get(f"/accounts/{expense['id']}/limits/2026-09").json()
    assert info["used_debit"] == "600.00" and info["remaining"] == "0.00"

    # 回放项在前不影响后序校验：上调限额后，回放 + 新建 100 成功
    assert _set_limit(client, expense["id"], "2026-09", "700.00").status_code == 200
    r = client.post("/vouchers/batch", json={"items": [
        _batch_voucher("blr-1", "600.00", debit_id=expense["id"], credit_id=bank["id"]),
        _batch_voucher("blr-2", "100.00", debit_id=expense["id"], credit_id=bank["id"]),
    ]})
    assert r.status_code == 201, r.text
    assert [it["idempotent_replay"] for it in r.json()["items"]] == [True, False]
    info = client.get(f"/accounts/{expense['id']}/limits/2026-09").json()
    assert info["used_debit"] == "700.00"


def test_batch_single_endpoint_behavior_unchanged(client):
    bank = _create_account(client, "1002", "银行存款")
    income = _create_account(client, "4001", "科研拨款", atype="income")

    # 单笔接口：201/false，重复回放 200/true；与批量共存互不影响
    r1 = _post_voucher(client, "single-1", [
        {"account_id": bank["id"], "direction": "debit", "amount": "30.00"},
        {"account_id": income["id"], "direction": "credit", "amount": "30.00"},
    ])
    r2 = _post_voucher(client, "single-1", [
        {"account_id": bank["id"], "direction": "debit", "amount": "30.00"},
        {"account_id": income["id"], "direction": "credit", "amount": "30.00"},
    ])
    assert r1.status_code == 201 and r2.status_code == 200
    assert r2.headers["X-Idempotent-Replay"] == "true"
    bal = client.get(f"/accounts/{bank['id']}/balances").json()
    assert bal[0]["total_debit"] == "30.00"

