"""只读报表服务：试算平衡表。

基于物化的 ``balances`` 表 (科目 × 期间借贷累计) 聚合截止到指定期间
(含该期间及此前各期间) 的累计发生额与期末余额。本模块只执行 SELECT，
不提交事务、不创建任何期间或余额行，绝不改动账务数据。
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..errors import unprocessable
from ..models import Account, Balance
from ..money import minor_to_amount
from ..schemas import (
    TrialBalanceAccountOut,
    TrialBalanceCurrencyOut,
    TrialBalanceOut,
)
from .master_data import PERIOD_KEY_RE


def trial_balance(db: Session, period_key: str) -> TrialBalanceOut:
    """汇总截止 ``period_key`` 的试算平衡表，按币种分组。

    - 逐科目累计 ``period_key <= 截止期`` 的借/贷发生额；
    - 期末余额：借>贷为借方余额，贷>借为贷方余额；
    - 借/贷净额为 0 (无余额) 的科目不列出；
    - 每个币种给出期末借/贷余额合计及是否平衡。
    """
    if not PERIOD_KEY_RE.match(period_key):
        raise unprocessable(
            f"非法期间 {period_key!r}，格式应为 YYYY-MM", period_key=period_key
        )

    rows = db.execute(
        select(
            Account,
            func.coalesce(func.sum(Balance.debit_minor), 0),
            func.coalesce(func.sum(Balance.credit_minor), 0),
        )
        .join(Account, Balance.account_id == Account.id)
        .where(Balance.period_key <= period_key)
        .group_by(Account.id)
    ).all()

    by_currency: dict[str, list[tuple[TrialBalanceAccountOut, int, int]]] = {}
    for account, debit_minor, credit_minor in rows:
        debit_minor, credit_minor = int(debit_minor), int(credit_minor)
        net = debit_minor - credit_minor
        if net == 0:
            # 无余额科目不列出
            continue
        if net > 0:
            closing_debit, closing_credit, direction = net, 0, "debit"
        else:
            closing_debit, closing_credit, direction = 0, -net, "credit"
        by_currency.setdefault(account.currency, []).append(
            (
                TrialBalanceAccountOut(
                    account_id=account.id,
                    account_code=account.code,
                    account_name=account.name,
                    account_type=account.type,
                    total_debit=minor_to_amount(debit_minor),
                    total_credit=minor_to_amount(credit_minor),
                    closing_debit=minor_to_amount(closing_debit),
                    closing_credit=minor_to_amount(closing_credit),
                    balance_direction=direction,
                ),
                closing_debit,
                closing_credit,
            )
        )

    currencies: list[TrialBalanceCurrencyOut] = []
    for currency in sorted(by_currency):
        group = sorted(by_currency[currency], key=lambda g: (g[0].account_code, g[0].account_id))
        total_debit = sum(closing_debit for _, closing_debit, _ in group)
        total_credit = sum(closing_credit for _, _, closing_credit in group)
        currencies.append(
            TrialBalanceCurrencyOut(
                currency=currency,
                total_debit=minor_to_amount(total_debit),
                total_credit=minor_to_amount(total_credit),
                balanced=total_debit == total_credit,
                accounts=[item for item, _, _ in group],
            )
        )

    return TrialBalanceOut(period=period_key, currencies=currencies)
