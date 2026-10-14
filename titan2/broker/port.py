"""What the engine's broker commands need from a broker, and the shared result vocabulary (spec §3.2, §5.8, §6.1).

Two adapters implement BrokerPort: KiteBroker for account LIVE (broker/kite.py) and PaperExchange for PAPER and
SHADOW (broker/paper.py). KiteData implements DataPort plus the account reads, and has no order methods. Every
method takes the account explicitly; an adapter refuses accounts outside its `accounts`.

Contract
- place() and cancel() never raise for a broker error: they return a classified PlaceResult / CancelResult (§5.8).
  A refusal made before anything was sent is REJECTED with error_class "LOCAL" and a message starting "LOCAL: ".
- Every read raises on any failure (preferably ReadFailed, carrying format_error() text). The lanes turn that into
  an ok=False event and nothing is inferred from it.

Imports only titan2.model and titan2.core.events, so core code may import this module.
"""

from __future__ import annotations

from datetime import datetime
from typing import AbstractSet, Mapping, Protocol, Sequence, runtime_checkable

from titan2.core.events import CancelResult, ContractNoteOrder, ContractNoteRow, OrderSnapshot, PlaceResult
from titan2.model import (
    Account, Bar, BrokerOrder, BrokerPosition, BrokerTrade, Margins, OrderRequest, Quote,
)

__all__ = [
    "ORDER_LANE_THREAD", "READS_LANE_THREAD", "OK", "REJECTED", "AMBIGUOUS", "FATAL", "BENIGN", "LOCAL",
    "NormalizationError", "ReadFailed", "format_error", "error_outcome", "snapshot_of",
    "AccountReads", "BrokerPort", "DataPort",
    # normalized broker types (defined in model/events; re-exported as the port's vocabulary)
    "BrokerOrder", "BrokerTrade", "BrokerPosition", "Margins", "ContractNoteOrder", "ContractNoteRow",
]

# Thread names. KiteBroker refuses to run off ORDER_LANE_THREAD (I2) unless built with thread_check=False.
ORDER_LANE_THREAD = "lane-live-orders"
READS_LANE_THREAD = "lane-reads"

# Outcome classes (§5.8)
OK = "OK"
REJECTED = "REJECTED"      # the broker (or a local check) definitely did not accept the request
AMBIGUOUS = "AMBIGUOUS"    # unknown whether it was accepted: look the tag up (§5.2)
FATAL = "FATAL"            # token or permission failure: HALT(TOKEN) (§9.7)
BENIGN = "BENIGN"          # a cancel that failed because the order is already terminal: poll
LOCAL = "LOCAL"            # error_class of a refusal made before anything was sent

ERROR_TEXT_MAX = 300


class NormalizationError(ValueError):
    """A broker response did not have the documented shape (API drift). The whole read fails; nothing is guessed."""


class ReadFailed(Exception):
    """A broker read failed. text is format_error() output, so its outcome class survives into the event."""

    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.text = text

    @property
    def outcome(self) -> str | None:
        return error_outcome(self.text)


def format_error(outcome: str, name: str, message: str) -> str:
    """'<OUTCOME>:<name>: <message>', e.g. 'FATAL:TokenException(403): Incorrect api_key or access_token.'"""
    return f"{outcome}:{name}: {message}"[:ERROR_TEXT_MAX]


def error_outcome(text: str | None) -> str | None:
    """The outcome class at the front of a format_error() text (FATAL means the token is dead), else None."""
    if not text:
        return None
    head = text.split(":", 1)[0]
    return head if head in (REJECTED, AMBIGUOUS, FATAL) else None


def snapshot_of(order: BrokerOrder, source: str, recv_mono: float) -> OrderSnapshot:
    """The OrderSnapshot event for a normalized broker order (POLL / LOOKUP / SIM / RECOVERY / WS)."""
    return OrderSnapshot(order.account, order.broker_order_id, order.tag, order.status, order.filled_qty,
                         order.avg_px, order.status_msg, order.exch_wall, source, recv_mono)


@runtime_checkable
class AccountReads(Protocol):
    """Account state reads: lane-reads serves them for LIVE (KiteData), paper-sim for PAPER/SHADOW."""

    accounts: AbstractSet[Account]

    def orders_today(self, account: Account) -> tuple[BrokerOrder, ...]: ...

    def trades_today(self, account: Account) -> tuple[BrokerTrade, ...]: ...

    def positions(self, account: Account) -> tuple[BrokerPosition, ...]: ...

    def margins(self, account: Account) -> Margins: ...

    def contract_note(self, account: Account,
                      orders: Sequence[ContractNoteOrder]) -> tuple[ContractNoteRow, ...]: ...


@runtime_checkable
class BrokerPort(AccountReads, Protocol):
    """The full account interface: KiteBroker (LIVE) and PaperExchange (PAPER, SHADOW). The engine never knows which."""

    def place(self, req: OrderRequest) -> PlaceResult: ...

    def cancel(self, account: Account, order_pk: str, broker_order_id: str | None) -> CancelResult: ...

    def order_history(self, account: Account, broker_order_id: str) -> BrokerOrder | None:
        """The order's latest state, or None when the broker returns no history for it."""

    def order_trades(self, account: Account, broker_order_id: str) -> tuple[BrokerTrade, ...]: ...


@runtime_checkable
class DataPort(Protocol):
    """Market data (KiteData on lane-reads)."""

    def quote(self, symbols: Sequence[str]) -> Mapping[str, Quote]:
        """Depth quotes for at most 500 symbols; symbols the broker omits (or sends malformed) are absent."""

    def ltp(self, symbols: Sequence[str]) -> Mapping[str, float]: ...

    def historical(self, symbol: str, token: int, frm: datetime, to: datetime, interval: str) -> tuple[Bar, ...]: ...

    def instruments(self, exchange: str) -> list[dict]: ...

    def profile(self) -> Mapping: ...
