import requests
from datetime import datetime

TRONSCAN_API = "https://apilist.tronscanapi.com/api"
USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"  # USDT TRC20
USDT_DECIMALS = 6


def _fmt_amount(raw: int) -> str:
    amount = raw / (10 ** USDT_DECIMALS)
    return f"{amount:,.2f}"


def _fmt_date(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000).strftime("%d.%m.%Y %H:%M")


def _short_addr(addr: str) -> str:
    return f"{addr[:6]}...{addr[-4:]}" if len(addr) > 10 else addr


def get_usdt_transactions(address: str, limit: int = 50) -> tuple[bool, list, str]:
    """
    Получает последние USDT TRC20 транзакции по адресу кошелька.
    Возвращает (успех, список транзакций, сообщение об ошибке).
    """
    try:
        url = f"{TRONSCAN_API}/token_trc20/transfers"
        params = {
            "contract_address": USDT_CONTRACT,
            "relatedAddress": address,
            "limit": limit,
            "start": 0,
            "sort": "-timestamp",
            "count": "true",
        }
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        txs = data.get("token_transfers", [])
        return True, txs, ""
    except requests.exceptions.Timeout:
        return False, [], "TronScan не отвечает. Попробуй позже."
    except Exception as e:
        return False, [], str(e)


def get_account_balance(address: str) -> str:
    """Баланс USDT TRC20 на кошельке."""
    try:
        url = f"{TRONSCAN_API}/account/tokens"
        params = {"address": address, "token": "USDT"}
        resp = requests.get(url, params=params, timeout=10)
        data = resp.json()
        for token in data.get("data", []):
            if token.get("tokenAbbr", "").upper() == "USDT" or \
               token.get("tokenId", "") == USDT_CONTRACT:
                balance = int(token.get("quantity", 0)) / (10 ** USDT_DECIMALS)
                return f"{balance:,.2f} USDT"
        return "неизвестно"
    except Exception:
        return "неизвестно"


def build_tx_summary(address: str, txs: list) -> str:
    """Строит текстовую сводку транзакций для отправки в Grok."""
    addr_up = address.upper()
    lines = []
    total_in = 0.0
    total_out = 0.0

    for tx in txs:
        from_addr = tx.get("from_address", "")
        to_addr = tx.get("to_address", "")
        amount = int(tx.get("quant", 0)) / (10 ** USDT_DECIMALS)
        ts = tx.get("block_ts", 0)
        date = _fmt_date(ts)
        tx_hash = tx.get("transaction_id", "")[:16] + "..."

        direction = "IN ↓" if to_addr.upper() == addr_up else "OUT ↑"
        if direction == "IN ↓":
            total_in += amount
            counterpart = _short_addr(from_addr)
        else:
            total_out += amount
            counterpart = _short_addr(to_addr)

        lines.append(f"{date} | {direction} | {amount:,.2f} USDT | {counterpart} | {tx_hash}")

    summary = "\n".join([
        f"Адрес: {address}",
        f"Всего транзакций: {len(txs)}",
        f"Пришло: {total_in:,.2f} USDT",
        f"Ушло: {total_out:,.2f} USDT",
        f"Чистый поток: {total_in - total_out:+,.2f} USDT",
        "",
        "СПИСОК ТРАНЗАКЦИЙ (дата | направление | сумма | контрагент | hash):",
    ] + lines)

    return summary, total_in, total_out
