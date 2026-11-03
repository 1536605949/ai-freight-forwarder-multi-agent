"""How much consequence an action carries, as an ordered scale.

The ordering is the whole point: the guard compares levels rather than matching names, so a
new action cannot be made safe by forgetting to add it to a list of "dangerous" ones. It has
to be *declared* at a level, and an undeclared action is refused outright.

Why four levels and not two
---------------------------
A boolean `read_only` cannot tell the difference between "writes a draft row" and "sends a
price to a customer". The old registry only had that boolean, which meant the answer to
"what can this agent do?" was a single bit per tool. Grading lets the policy say "this agent
may draft but may not send", which is the actual rule a freight forwarder needs.

The levels describe *business consequence*, not whether bytes leave the process:

* `READ` -- no state changes. Safe to retry, safe to run unattended.
* `DRAFT` -- writes internal state that a human can still discard. A supplier quote row, a
  generated email body, a computed price. Reversible by deleting a row.
* `EXTERNAL` -- an outside party now knows something and may act on it. An RFQ to a
  supplier, a quote to a customer, a cold email to a prospect. Deleting the row does not
  unsend the mail.
* `COMMITMENT` -- money or capacity is committed and cannot be taken back by editing a row.
  A booking is the example; the carrier has the container.
"""
from __future__ import annotations

from enum import IntEnum


class Risk(IntEnum):
    """Ordered consequence level. Comparisons are the guard's only input."""

    READ = 0
    DRAFT = 1
    EXTERNAL = 2
    COMMITMENT = 3

    @property
    def label(self) -> str:
        return _LABELS[self]


_LABELS = {
    Risk.READ: '只读',
    Risk.DRAFT: '内部草稿',
    Risk.EXTERNAL: '对外发送',
    Risk.COMMITMENT: '不可逆承诺',
}


__all__ = ['Risk']
