"""Signed Kalshi REST client (RSA-PSS), ported from the cle_watcher plugin.

Pricing is the source of truth for implied probability: implied = yes_cents / 100.
Network is isolated here so the rest of the package stays pure + testable.
"""
from __future__ import annotations

import base64
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from .orderbook import OrderBook

# Map a Kalshi prop family to its series ticker prefix.
STAT_SERIES = {
    "points": "KXNBAPTS",
    "rebounds": "KXNBAREB",
    "assists": "KXNBAAST",
    "threes": "KXNBA3PM",
    "minutes": "KXNBAMIN",
}
GAME_SERIES = "KXNBAGAME"

# "Karl-Anthony Towns: 15+ points"  /  "Stephon Castle: 4+ rebounds"
_TITLE_RE = re.compile(
    r"^(?P<name>.+?):\s*(?P<line>\d+)\+\s*(?P<stat>points|rebounds|assists|"
    r"3-?point(?:er)?s?|3pm|threes|three-pointers|minutes|mins?)\b",
    re.IGNORECASE,
)
_STAT_NORM = {
    "points": "points", "rebounds": "rebounds", "assists": "assists",
    "minutes": "minutes", "min": "minutes", "mins": "minutes",
}


def _norm_stat(raw: str) -> str:
    raw = raw.lower()
    if raw in _STAT_NORM:
        return _STAT_NORM[raw]
    if raw.startswith("3") or "three" in raw:
        return "threes"
    return raw


@dataclass(frozen=True)
class Quote:
    bid: int          # cents
    ask: int          # cents
    ticker: str

    @property
    def mid(self) -> int:
        if self.bid and self.ask:
            return round((self.bid + self.ask) / 2)
        return self.bid or self.ask

    @property
    def implied(self) -> float:
        """Fair-ish implied probability from the mid, clamped to (0,1)."""
        return min(max(self.mid / 100.0, 0.0), 1.0)


DEMO_CREDENTIALS_BLOCKED_REASON = (
    "set KALSHI_DEMO_API_KEY / KALSHI_DEMO_PRIVATE_KEY_PATH to use Kalshi demo"
)


class KalshiClient:
    def __init__(
        self,
        api_key: str,
        private_key_path: Path,
        base: str,
        *,
        demo_api_key: str = "",
        demo_private_key_path: Path | None = None,
    ):
        self.api_key = api_key
        self.base = base.rstrip("/")
        self.private_key_path = Path(private_key_path)
        self.demo_api_key = demo_api_key
        self.demo_private_key_path = (
            Path(demo_private_key_path) if demo_private_key_path is not None else None
        )
        self._pk = None
        self._demo_pk = None
        self._s = requests.Session()

    @staticmethod
    def _load_private_key(path: Path):
        return serialization.load_pem_private_key(path.read_bytes(), password=None)

    def _private_key(self):
        if self._pk is None:
            self._pk = self._load_private_key(self.private_key_path)
        return self._pk

    def _demo_private_key(self):
        if not self.demo_credentials_configured():
            raise RuntimeError(DEMO_CREDENTIALS_BLOCKED_REASON)
        if self._demo_pk is None:
            self._demo_pk = self._load_private_key(self.demo_private_key_path)
        return self._demo_pk

    def demo_credentials_configured(self) -> bool:
        return bool(
            str(self.demo_api_key or "").strip()
            and self.demo_private_key_path is not None
            and self.demo_private_key_path.is_file()
        )

    # ── signing ────────────────────────────────────────────────────────────────
    def _headers(self, method: str, path: str, *,
                 api_key: str | None = None, private_key: object | None = None) -> dict[str, str]:
        ts = str(int(time.time() * 1000))
        signing_key = private_key if private_key is not None else self._private_key()
        sig = base64.b64encode(
            signing_key.sign(
                (ts + method + path).encode(),
                padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                            salt_length=padding.PSS.DIGEST_LENGTH),
                hashes.SHA256(),
            )
        ).decode()
        return {
            "KALSHI-ACCESS-KEY": self.api_key if api_key is None else api_key,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": sig,
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, params: dict | None = None,
                 body: dict | None = None, *, base: str | None = None,
                 api_key: str | None = None,
                 private_key: object | None = None) -> dict:
        url = (base or self.base) + path
        last_error = None
        for attempt in range(4):
            try:
                r = self._s.request(
                    method, url,
                    headers=self._headers(
                        method,
                        path,
                        api_key=api_key,
                        private_key=private_key,
                    ),
                    params=params, data=json.dumps(body) if body is not None else None,
                    timeout=8,
                )
                if r.status_code not in (429,) and r.status_code < 500:
                    r.raise_for_status()
                    return r.json() if r.content else {}
                r.raise_for_status()
            except requests.RequestException as e:
                last_error = e
                if attempt == 3:
                    raise
                time.sleep(0.5 * (2 ** attempt))
        if last_error:
            raise last_error
        return {}

    def _get(self, path: str, params: dict | None = None) -> dict:
        return self._request("GET", path, params=params)

    def _post(self, path: str, body: dict | None = None) -> dict:
        return self._request("POST", path, body=body)

    @staticmethod
    def _base_and_signed_path(base: str, api_path: str) -> tuple[str, str]:
        parsed = urlparse(base.rstrip("/"))
        prefix = parsed.path.rstrip("/")
        root = base.rstrip("/")
        if prefix:
            root = root[: -len(prefix)]
        return root, prefix + api_path

    def post_to_base(self, base: str, api_path: str, body: dict) -> dict:
        """POST to a fully-qualified API base, preserving Kalshi's signed path."""
        root, path = self._base_and_signed_path(base, api_path)
        return self._request("POST", path, body=body, base=root)

    def demo_post_to_base(self, base: str, api_path: str, body: dict) -> dict:
        """POST to Kalshi demo using the demo credential pair only."""
        root, path = self._base_and_signed_path(base, api_path)
        return self._request(
            "POST",
            path,
            body=body,
            base=root,
            api_key=self.demo_api_key,
            private_key=self._demo_private_key(),
        )

    def demo_get_to_base(self, base: str, api_path: str, params: dict | None = None) -> dict:
        """GET from Kalshi demo using the demo credential pair only."""
        root, path = self._base_and_signed_path(base, api_path)
        return self._request(
            "GET",
            path,
            params=params,
            base=root,
            api_key=self.demo_api_key,
            private_key=self._demo_private_key(),
        )

    def demo_delete_to_base(self, base: str, api_path: str) -> dict:
        """DELETE from Kalshi demo using the demo credential pair only."""
        root, path = self._base_and_signed_path(base, api_path)
        return self._request(
            "DELETE",
            path,
            base=root,
            api_key=self.demo_api_key,
            private_key=self._demo_private_key(),
        )

    # ── account ─────────────────────────────────────────────────────────────────
    def balance_cents(self) -> int:
        return int(self._get("/trade-api/v2/portfolio/balance").get("balance", 0))

    def positions(self) -> dict[str, int]:
        out: dict[str, int] = {}
        data = self._get("/trade-api/v2/portfolio/positions", {"limit": 500})
        for p in data.get("market_positions", []):
            pos = int(float(p.get("position_fp", "0")))
            if pos:
                out[p.get("ticker", "")] = pos
        return out

    def orders(self, **params: object) -> dict:
        clean = {k: v for k, v in params.items() if v not in (None, "")}
        return self._get("/trade-api/v2/portfolio/orders", clean or None)

    # ── markets ──────────────────────────────────────────────────────────────────
    def list_markets(self, series: str | None = None, game_tag: str | None = None,
                     status: str = "open", max_pages: int = 6) -> list[dict]:
        markets, cursor = [], None
        for _ in range(max_pages):
            params = {"limit": 500, "status": status}
            if series:
                params["series_ticker"] = series
            if cursor:
                params["cursor"] = cursor
            resp = self._get("/trade-api/v2/markets", params)
            for m in resp.get("markets", []):
                if game_tag is None or game_tag in m.get("ticker", ""):
                    markets.append(m)
            cursor = resp.get("cursor")
            if not cursor:
                break
        return markets

    def list_open_markets(self, max_pages: int = 3) -> list[dict]:
        """Return broad open markets without filtering to one sport series."""
        return self.list_markets(None, None, "open", max_pages=max_pages)

    def market(self, ticker: str) -> dict:
        """Return one market document by ticker."""
        data = self._get(f"/trade-api/v2/markets/{ticker}")
        market = data.get("market")
        return market if isinstance(market, dict) else data

    def _list_series(self, series: str, game_tag: str) -> list[dict]:
        return self.list_markets(series, game_tag)

    @staticmethod
    def _quote(m: dict) -> Quote:
        def c(v):
            try:
                return int(round(float(v) * 100)) if v else 0
            except (TypeError, ValueError):
                return 0
        return Quote(bid=c(m.get("yes_bid_dollars")), ask=c(m.get("yes_ask_dollars")),
                     ticker=m.get("ticker", ""))

    def prop_prices(self, game_tag: str) -> dict[tuple[str, str, int], Quote]:
        """(surname_lower, stat, line) -> Quote for every player prop in the game."""
        out: dict[tuple[str, str, int], Quote] = {}
        for stat, series in STAT_SERIES.items():
            for m in self._list_series(series, game_tag):
                title = m.get("title", "")
                mt = _TITLE_RE.match(title)
                if not mt:
                    continue
                surname = mt.group("name").strip().split()[-1].lower()
                line = int(mt.group("line"))
                norm = _norm_stat(mt.group("stat"))
                out[(surname, norm, line)] = self._quote(m)
        return out

    def winner_prices(self, game_tag: str) -> dict[str, Quote]:
        """team_abbr -> Quote for the moneyline (game winner) market."""
        out: dict[str, Quote] = {}
        for m in self._list_series(GAME_SERIES, game_tag):
            team = m.get("ticker", "").split("-")[-1]
            out[team] = self._quote(m)
        return out

    def orderbook(self, ticker: str, depth: int | None = None) -> OrderBook:
        """Return a REST order book snapshot for one market ticker."""
        params = {"depth": depth} if depth is not None else None
        data = self._get(f"/trade-api/v2/markets/{ticker}/orderbook", params)
        return OrderBook.from_kalshi_response(ticker, data)

    def orderbooks(self, tickers: list[str], depth: int | None = None) -> dict[str, OrderBook]:
        return {ticker: self.orderbook(ticker, depth) for ticker in tickers}

    # ── orders (live execution is gated in execution.py / agents/live_execute.py) ─
    def demo_place_order(self, demo_api_base: str, body: dict) -> dict:
        """Submit a gated demo order."""
        return self.demo_post_to_base(demo_api_base, "/portfolio/events/orders", body)

    def demo_balance_cents(self, demo_api_base: str) -> int:
        return int(self.demo_get_to_base(demo_api_base, "/portfolio/balance").get("balance", 0))

    def demo_positions(self, demo_api_base: str) -> dict[str, int]:
        out: dict[str, int] = {}
        data = self.demo_get_to_base(demo_api_base, "/portfolio/positions", {"limit": 500})
        for p in data.get("market_positions", []):
            pos = int(float(p.get("position_fp", "0")))
            if pos:
                out[p.get("ticker", "")] = pos
        return out

    def demo_orders(self, demo_api_base: str, **params: object) -> dict:
        clean = {k: v for k, v in params.items() if v not in (None, "")}
        return self.demo_get_to_base(demo_api_base, "/portfolio/orders", clean or None)

    def demo_order(self, demo_api_base: str, order_id: str) -> dict:
        return self.demo_get_to_base(demo_api_base, f"/portfolio/orders/{order_id}")

    def demo_fills(self, demo_api_base: str, **params: object) -> dict:
        clean = {k: v for k, v in params.items() if v not in (None, "")}
        return self.demo_get_to_base(demo_api_base, "/portfolio/fills", clean or None)

    def demo_cancel_event_order(self, demo_api_base: str, order_id: str) -> dict:
        return self.demo_delete_to_base(demo_api_base, f"/portfolio/events/orders/{order_id}")

    def place_order(self, body: dict) -> dict:
        """Submit a gated live order to the configured production API base."""
        return self._post("/trade-api/v2/portfolio/events/orders", body)
