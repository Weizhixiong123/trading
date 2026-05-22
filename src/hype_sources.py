import os
from dataclasses import dataclass
from typing import List

import requests


COINGECKO_TRENDING_URL = "https://api.coingecko.com/api/v3/search/trending"


@dataclass(frozen=True)
class TrendingCoin:
    symbol: str
    name: str
    mc_rank: int | None
    score: int


def load_extra_keywords() -> List[str]:
    raw = os.getenv("EXTRA_HYPE_KEYWORDS", "").strip()
    if not raw:
        return []
    seen: set[str] = set()
    out: List[str] = []
    for x in raw.replace("\n", ",").split(","):
        k = x.strip().upper()
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out


def fetch_coingecko_trending(req_timeout: int) -> List[TrendingCoin]:
    resp = requests.get(
        COINGECKO_TRENDING_URL,
        timeout=req_timeout,
        headers={"accept": "application/json"},
    )
    resp.raise_for_status()
    out: List[TrendingCoin] = []
    for c in resp.json().get("coins", []):
        item = c.get("item") or {}
        sym = (item.get("symbol") or "").upper().strip()
        if not sym:
            continue
        out.append(TrendingCoin(
            symbol=sym,
            name=item.get("name") or sym,
            mc_rank=item.get("market_cap_rank"),
            score=int(item.get("score") or 0),
        ))
    return out


def get_hype_symbols(req_timeout: int) -> List[TrendingCoin]:
    trending = fetch_coingecko_trending(req_timeout)
    seen = {c.symbol for c in trending}
    out = list(trending)
    next_score = len(out)
    for symbol in load_extra_keywords():
        if symbol in seen:
            continue
        seen.add(symbol)
        out.append(TrendingCoin(
            symbol=symbol,
            name=symbol,
            mc_rank=None,
            score=next_score,
        ))
        next_score += 1
    return out
