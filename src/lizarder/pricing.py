"""Per-token prices for AI providers, and turning token estimates into cost.

A pricing file is JSON:

    {
      "currency": "USD",
      "prices": {
        "<name>": {
          "provider": "anthropic",
          "input_per_million": 4.0,
          "output_per_million": 20.0,
          "cache_read_per_million": 0.20,    # optional
          "cache_write_per_million": 5.0,    # optional
          "source": "https://...", "as_of": "2026-10-06", "note": "..."
        }
      }
    }

Cached input is priced at the cache rates when the token model has seen
caching in its training data (its cache read/write shares). A missing cache
rate falls back to the input rate.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence

BUNDLED_PRICING = Path(__file__).with_name("pricing.json")


@dataclass
class Price:
    name: str
    input_per_million: float
    output_per_million: float
    cache_read_per_million: Optional[float] = None
    cache_write_per_million: Optional[float] = None
    provider: Optional[str] = None
    note: Optional[str] = None

    def cost(self, input_tokens: float, output_tokens: float,
             cache_read_share: float = 0.0, cache_write_share: float = 0.0) -> float:
        cache_read = self.cache_read_per_million
        cache_write = self.cache_write_per_million
        input_rate = (
            (1 - cache_read_share - cache_write_share) * self.input_per_million
            + cache_read_share * (self.input_per_million if cache_read is None else cache_read)
            + cache_write_share * (self.input_per_million if cache_write is None else cache_write)
        )
        return (input_tokens * input_rate + output_tokens * self.output_per_million) / 1e6


class PriceList:
    def __init__(self, data: dict, path: Optional[Path] = None):
        self.path = path
        self.currency: str = data.get("currency", "USD")
        self.raw: Dict[str, dict] = data.get("prices", {})
        self.prices: Dict[str, Price] = {}
        for name, entry in self.raw.items():
            try:
                self.prices[name] = Price(
                    name=name,
                    input_per_million=float(entry["input_per_million"]),
                    output_per_million=float(entry["output_per_million"]),
                    cache_read_per_million=_optional_float(entry.get("cache_read_per_million")),
                    cache_write_per_million=_optional_float(entry.get("cache_write_per_million")),
                    provider=entry.get("provider"),
                    note=entry.get("note"),
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"price {name!r} needs numeric input_per_million and "
                                 f"output_per_million ({exc})") from None

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "PriceList":
        path = Path(path) if path else BUNDLED_PRICING
        return cls(json.loads(path.read_text(encoding="utf-8")), path)

    def select(self, names: Sequence[str]) -> List[Price]:
        if "all" in names:
            return list(self.prices.values())
        unknown = [n for n in names if n not in self.prices]
        if unknown:
            raise ValueError(f"unknown price {', '.join(unknown)}; "
                             f"choose from: {', '.join(self.prices)}")
        return [self.prices[n] for n in dict.fromkeys(names)]

    def describe(self) -> str:
        rows = [("Name", "Provider", "Input/1M", "Output/1M", "Cache read/1M", "Note")]
        for p in self.prices.values():
            rows.append((p.name, p.provider or "", money(p.input_per_million, self.currency),
                         money(p.output_per_million, self.currency),
                         "" if p.cache_read_per_million is None
                         else money(p.cache_read_per_million, self.currency),
                         p.note or ""))
        widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
        lines = ["  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip() for r in rows]
        lines.insert(1, "  ".join("-" * w for w in widths))
        return f"Prices from {self.path} ({self.currency})\n" + "\n".join(lines) + "\n"


def _optional_float(value) -> Optional[float]:
    return None if value is None else float(value)


def money(amount: float, currency: str = "USD") -> str:
    symbol = {"USD": "$", "EUR": "€", "GBP": "£"}.get(currency)
    text = f"{amount:,.2f}" if amount >= 0.01 or amount == 0 else f"{amount:.4f}"
    return f"{symbol}{text}" if symbol else f"{text} {currency}"
