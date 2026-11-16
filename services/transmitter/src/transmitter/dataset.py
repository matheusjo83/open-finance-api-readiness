"""Seeded synthetic dataset: customers, accounts, transactions and overdraft limits.

The same (seed, today, customers) always produces the same dataset. Properties the
business-rule layer can rely on (docs/transmitter.md):

- every account was opened within the last 12 months, so its whole history fits the
  /transactions query window, and its first transaction is the opening deposit;
- the available balance equals the signed sum (credits minus debits) of the account's
  TRANSACAO_EFETIVADA transactions; LANCAMENTO_FUTURO transactions are in the future and
  do not count; the running balance never goes below zero;
- blocked and automatically invested amounts are zero; overdraft limits exist only for
  CONTA_DEPOSITO_A_VISTA and are unused;
- CPFs and CNPJs carry valid check digits.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

CURRENCY = "BRL"
BRAND_NAME = "Banco Sintetico"
COMPE_CODE = "999"

CREDIT_TYPES = ["PIX", "TED", "TRANSFERENCIA_MESMA_INSTITUICAO", "DEPOSITO", "FOLHA_PAGAMENTO"]
DEBIT_TYPES = ["PIX", "TED", "BOLETO", "CARTAO", "SAQUE", "CONVENIO_ARRECADACAO", "TARIFA_SERVICOS_AVULSOS"]
# Transfers name a counterparty (partie* fields); the other types do not.
TRANSFER_TYPES = {"PIX", "TED", "TRANSFERENCIA_MESMA_INSTITUICAO"}
TRANSACTION_NAMES = {
    "PIX": "Pix",
    "TED": "TED",
    "TRANSFERENCIA_MESMA_INSTITUICAO": "Transferencia entre contas",
    "DEPOSITO": "Deposito",
    "FOLHA_PAGAMENTO": "Salario",
    "BOLETO": "Pagamento de boleto",
    "CARTAO": "Compra no cartao",
    "SAQUE": "Saque",
    "CONVENIO_ARRECADACAO": "Conta de consumo",
    "TARIFA_SERVICOS_AVULSOS": "Tarifa avulsa",
}


@dataclass(frozen=True)
class Customer:
    cpf: str
    name: str


@dataclass(frozen=True)
class Transaction:
    transaction_id: str
    account_id: str
    status: str  # completedAuthorisedPaymentType
    credit_debit: str  # CREDITO | DEBITO
    type: str
    name: str
    amount: Decimal
    timestamp: datetime  # UTC
    partie: dict[str, str] | None = None

    @property
    def booking_date(self) -> date:
        return self.timestamp.date()

    @property
    def signed_amount(self) -> Decimal:
        return self.amount if self.credit_debit == "CREDITO" else -self.amount


@dataclass
class Account:
    account_id: str
    customer_cpf: str
    type: str
    subtype: str
    branch_code: str
    number: str
    check_digit: str
    opened_on: date
    overdraft_limit: Decimal | None
    transactions: list[Transaction] = field(default_factory=list)

    @property
    def available_amount(self) -> Decimal:
        return sum(
            (t.signed_amount for t in self.transactions if t.status == "TRANSACAO_EFETIVADA"),
            Decimal("0.00"),
        )

    @property
    def last_update(self) -> datetime:
        done = [t.timestamp for t in self.transactions if t.status == "TRANSACAO_EFETIVADA"]
        return max(done)


@dataclass
class Dataset:
    seed: int
    today: date
    company_cnpj: str
    customers: list[Customer]
    accounts: dict[str, Account]

    def accounts_of(self, cpf: str) -> list[Account]:
        return [a for a in self.accounts.values() if a.customer_cpf == cpf]


def _check_digit(digits: list[int], weights: list[int]) -> int:
    rest = sum(d * w for d, w in zip(digits, weights)) % 11
    return 0 if rest < 2 else 11 - rest


def cpf(rng: random.Random) -> str:
    digits = [rng.randrange(10) for _ in range(9)]
    while len(set(digits)) == 1:  # 111.111.111-xx and the like are invalid
        digits = [rng.randrange(10) for _ in range(9)]
    digits.append(_check_digit(digits, list(range(10, 1, -1))))
    digits.append(_check_digit(digits, list(range(11, 1, -1))))
    return "".join(map(str, digits))


def cnpj(rng: random.Random) -> str:
    digits = [rng.randrange(10) for _ in range(8)] + [0, 0, 0, 1]
    digits.append(_check_digit(digits, [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]))
    digits.append(_check_digit(digits, [6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]))
    return "".join(map(str, digits))


def is_valid_cpf(value: str) -> bool:
    if len(value) != 11 or not value.isdigit() or len(set(value)) == 1:
        return False
    d = [int(c) for c in value]
    return d[9] == _check_digit(d[:9], list(range(10, 1, -1))) and d[10] == _check_digit(
        d[:10], list(range(11, 1, -1))
    )


def is_valid_cnpj(value: str) -> bool:
    if len(value) != 14 or not value.isdigit() or len(set(value)) == 1:
        return False
    d = [int(c) for c in value]
    return d[12] == _check_digit(d[:12], [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]) and d[13] == _check_digit(
        d[:13], [6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    )


def _money(rng: random.Random, low: int, high: int) -> Decimal:
    return Decimal(rng.randrange(low * 100, high * 100 + 1)) / 100


def _timestamp(rng: random.Random, day: date) -> datetime:
    seconds = rng.randrange(8 * 3600, 22 * 3600)
    return datetime.combine(day, time(), tzinfo=timezone.utc) + timedelta(
        seconds=seconds, milliseconds=rng.randrange(1000)
    )


def _partie(rng: random.Random) -> dict[str, str]:
    natural = rng.random() < 0.7
    return {
        "partieCnpjCpf": cpf(rng) if natural else cnpj(rng),
        "partiePersonType": "PESSOA_NATURAL" if natural else "PESSOA_JURIDICA",
        "partieCompeCode": f"{rng.randrange(1, 1000):03d}",
        "partieBranchCode": f"{rng.randrange(1, 10000):04d}",
        "partieNumber": f"{rng.randrange(10**7, 10**12)}",
        "partieCheckDigit": str(rng.randrange(10)),
    }


def _transactions(rng: random.Random, account: Account, today: date) -> list[Transaction]:
    out: list[Transaction] = []
    seq = 0

    def add(status: str, credit_debit: str, type_: str, amount: Decimal, day: date) -> None:
        nonlocal seq
        seq += 1
        out.append(
            Transaction(
                transaction_id=f"{account.account_id}-T{seq:05d}",
                account_id=account.account_id,
                status=status,
                credit_debit=credit_debit,
                type=type_,
                name=TRANSACTION_NAMES[type_],
                amount=amount,
                timestamp=_timestamp(rng, day),
                partie=_partie(rng) if type_ in TRANSFER_TYPES else None,
            )
        )

    balance = _money(rng, 500, 5000)
    add("TRANSACAO_EFETIVADA", "CREDITO", "DEPOSITO", balance, account.opened_on)
    day = account.opened_on + timedelta(days=1)
    while day <= today:
        for _ in range(rng.choice([0, 0, 0, 1, 1, 2])):
            if rng.random() < 0.4:
                type_ = rng.choice(CREDIT_TYPES)
                amount = _money(rng, 50, 3000)
                add("TRANSACAO_EFETIVADA", "CREDITO", type_, amount, day)
                balance += amount
            else:
                type_ = rng.choice(DEBIT_TYPES)
                amount = min(_money(rng, 5, 800), balance)
                if amount < Decimal("1.00"):
                    continue
                add("TRANSACAO_EFETIVADA", "DEBITO", type_, amount, day)
                balance -= amount
        day += timedelta(days=1)
    for _ in range(rng.randrange(0, 4)):
        add("LANCAMENTO_FUTURO", "DEBITO", "BOLETO", _money(rng, 20, 500), today + timedelta(days=rng.randrange(1, 31)))
    out.sort(key=lambda t: (t.timestamp, t.transaction_id))
    return out


def build(seed: int, today: date, customers: int = 5) -> Dataset:
    rng = random.Random(seed)
    company = cnpj(rng)
    people = [Customer(cpf=cpf(rng), name=f"Cliente {i + 1}") for i in range(customers)]
    accounts: dict[str, Account] = {}
    for index, person in enumerate(people):
        types = ["CONTA_DEPOSITO_A_VISTA"] + rng.sample(
            ["CONTA_POUPANCA", "CONTA_PAGAMENTO_PRE_PAGA", "CONTA_DEPOSITO_A_VISTA"], rng.randrange(0, 3)
        )
        for n, type_ in enumerate(types):
            account_id = f"ACC-{seed}-{index + 1:03d}-{n + 1}"
            account = Account(
                account_id=account_id,
                customer_cpf=person.cpf,
                type=type_,
                subtype="INDIVIDUAL" if rng.random() < 0.85 else "CONJUNTA_SIMPLES",
                branch_code=f"{rng.randrange(1, 10000):04d}",
                number=f"{rng.randrange(10**7, 10**10)}",
                check_digit=str(rng.randrange(10)),
                opened_on=today - timedelta(days=rng.randrange(30, 360)),
                overdraft_limit=Decimal(rng.randrange(5, 51) * 100) if type_ == "CONTA_DEPOSITO_A_VISTA" else None,
            )
            account.transactions = _transactions(rng, account, today)
            accounts[account_id] = account
    return Dataset(seed=seed, today=today, company_cnpj=company, customers=people, accounts=accounts)
