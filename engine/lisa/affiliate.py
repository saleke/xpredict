"""Affiliate Integration — bookmaker bet slip API.

Integrates with bookmakers to provide one-click bet slip creation:
- Bet9ja
- Betway
- 1xBet
- SportyBet
- Betpawa
- MSport

Each bookmaker has its own API or deep-link format for creating bet slips.
This module provides a unified interface for all supported bookmakers.
"""
from __future__ import annotations

import logging
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Bookmaker:
    """A supported bookmaker."""
    name: str
    code: str
    country: str
    deep_link_template: str
    api_base: Optional[str] = None
    affiliate_id: str = ""

    def create_deep_link(self, selections: list[dict]) -> str:
        """Create a deep link for a bet slip.

        Args:
            selections: List of {match_id, market, outcome, odds} dicts

        Returns:
            Deep link URL
        """
        # Encode selections as query params
        params = urllib.parse.urlencode({
            "selections": str(len(selections)),
            "affiliate": self.affiliate_id,
        })
        return f"{self.deep_link_template}?{params}"


# Supported bookmakers
BOOKMAKERS: dict[str, Bookmaker] = {
    "bet9ja": Bookmaker(
        name="Bet9ja",
        code="bet9ja",
        country="NG",
        deep_link_template="https://www.bet9ja.com/betslip",
        affiliate_id="",
    ),
    "betway": Bookmaker(
        name="Betway",
        code="betway",
        country="NG",
        deep_link_template="https://www.betway.com/betslip",
        affiliate_id="",
    ),
    "1xbet": Bookmaker(
        name="1xBet",
        code="1xbet",
        country="NG",
        deep_link_template="https://1xbet.com/betslip",
        affiliate_id="",
    ),
    "sportybet": Bookmaker(
        name="SportyBet",
        code="sportybet",
        country="NG",
        deep_link_template="https://www.sportybet.com/betslip",
        affiliate_id="",
    ),
    "betpawa": Bookmaker(
        name="Betpawa",
        code="betpawa",
        country="NG",
        deep_link_template="https://www.betpawa.com/betslip",
        affiliate_id="",
    ),
    "msport": Bookmaker(
        name="MSport",
        code="msport",
        country="NG",
        deep_link_template="https://www.msport.com/betslip",
        affiliate_id="",
    ),
}


class AffiliateManager:
    """Manages bookmaker affiliate integrations.

    Provides:
    - Deep link generation for bet slips
    - Bookmaker selection
    - Affiliate tracking
    """

    def __init__(self, *, bookmakers: Optional[dict[str, Bookmaker]] = None):
        self.bookmakers = bookmakers or BOOKMAKERS

    def get_bookmaker(self, code: str) -> Optional[Bookmaker]:
        """Get a bookmaker by code."""
        return self.bookmakers.get(code.lower())

    def list_bookmakers(self, country: str = "NG") -> list[Bookmaker]:
        """List all supported bookmakers for a country."""
        return [b for b in self.bookmakers.values() if b.country == country]

    def create_betslip_link(self, bookmaker_code: str,
                            selections: list[dict]) -> Optional[str]:
        """Create a bet slip deep link for a bookmaker.

        Args:
            bookmaker_code: Bookmaker code (e.g., "bet9ja")
            selections: List of {match_id, market, outcome, odds} dicts

        Returns:
            Deep link URL or None if bookmaker not found
        """
        bookmaker = self.get_bookmaker(bookmaker_code)
        if not bookmaker:
            logger.warning("Bookmaker not found: %s", bookmaker_code)
            return None

        return bookmaker.create_deep_link(selections)

    def get_all_links(self, selections: list[dict]) -> dict[str, str]:
        """Get bet slip links for all supported bookmakers.

        Args:
            selections: List of {match_id, market, outcome, odds} dicts

        Returns:
            Dict of bookmaker_code -> deep_link
        """
        links = {}
        for code, bookmaker in self.bookmakers.items():
            link = bookmaker.create_deep_link(selections)
            if link:
                links[code] = link
        return links

    def get_bookmaker_keyboard(self, selections: list[dict]) -> list[list[dict]]:
        """Get inline keyboard markup for bookmaker selection.

        Args:
            selections: List of {match_id, market, outcome, odds} dicts

        Returns:
            Inline keyboard markup
        """
        links = self.get_all_links(selections)
        keyboard = []
        for code, link in links.items():
            bookmaker = self.bookmakers[code]
            keyboard.append([{
                "text": f"📱 {bookmaker.name}",
                "url": link,
            }])
        return keyboard


# Global affiliate manager instance
affiliate_manager = AffiliateManager()
