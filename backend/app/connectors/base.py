"""Base connector interface.

Every institution connector — whether a direct API integration, a browser
scraper, or a future aggregator like Flinks/Plaid — implements this
interface.  The base is intentionally minimal: only ``sync()`` is required.
Scraper-specific methods (``login``, ``complete_2fa``) are NOT part of this
base; they live in the separate ``ScraperConnector`` protocol extension.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.connectors.types import SyncResult


class Connector(ABC):
    """Minimal connector contract.

    Parameters
    ----------
    provider : str
        Short provider key (e.g. ``"coinbase"``, ``"questrade"``).
    """

    provider: str

    def __init__(self, provider: str) -> None:
        self.provider = provider

    @abstractmethod
    async def sync(self, user_id: int) -> SyncResult:
        """Run a full sync for *user_id* and return normalised data.

        The connector must:
        - Read its own credentials / settings scoped to *user_id*.
        - Fetch data from the upstream institution.
        - Return a ``SyncResult`` containing normalised accounts, holdings,
          and transactions.  **It must NOT write to the database** — the
          persistence layer handles that.

        Errors should be communicated via ``SyncResult.status`` rather than
        raising, except for truly unexpected failures.
        """
        ...
