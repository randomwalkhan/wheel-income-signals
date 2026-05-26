#!/usr/bin/env python3
"""ETF Wheel strategy signal and backtest engine.

The script emits research signals only. It does not place trades.
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
import datetime as dt
import hashlib
import json
import math
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import urlencode

try:
    import requests
except ImportError:  # pragma: no cover - exercised only in minimal installs
    requests = None


CORE_ETFS = [
    "SPY",
    "QQQ",
    "IWM",
    "DIA",
    "XLK",
    "XLF",
    "XLE",
    "XLV",
    "XLP",
    "XLU",
    "XLI",
    "XLY",
    "SMH",
    "SOXX",
    "TLT",
    "GLD",
]

LEVERAGED_ETFS = ["TQQQ", "SOXL"]
CONTRACT_SIZE = 100
TRADING_DAYS = 252


class DataProviderError(RuntimeError):
    """Raised when a market data provider cannot satisfy a request."""


def parse_date(value: str | dt.date) -> dt.date:
    if isinstance(value, dt.date):
        return value
    return dt.date.fromisoformat(value)


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def year_fraction(start: dt.date, end: dt.date) -> float:
    return max((end - start).days, 0) / 365.0


def safe_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    if value in (None, "", "null"):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def safe_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    if value in (None, "", "null"):
        return default
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def black_scholes_price(
    option_type: str,
    spot: float,
    strike: float,
    rate: float,
    volatility: float,
    time_to_expiry: float,
    dividend_yield: float = 0.0,
) -> float:
    option_type = option_type.lower()
    if time_to_expiry <= 0 or volatility <= 0 or spot <= 0 or strike <= 0:
        if option_type == "call":
            return max(spot - strike, 0.0)
        return max(strike - spot, 0.0)

    sqrt_t = math.sqrt(time_to_expiry)
    d1 = (
        math.log(spot / strike)
        + (rate - dividend_yield + 0.5 * volatility * volatility) * time_to_expiry
    ) / (volatility * sqrt_t)
    d2 = d1 - volatility * sqrt_t
    df_r = math.exp(-rate * time_to_expiry)
    df_q = math.exp(-dividend_yield * time_to_expiry)

    if option_type == "call":
        return spot * df_q * norm_cdf(d1) - strike * df_r * norm_cdf(d2)
    if option_type == "put":
        return strike * df_r * norm_cdf(-d2) - spot * df_q * norm_cdf(-d1)
    raise ValueError(f"Unknown option_type: {option_type}")


def black_scholes_greeks(
    option_type: str,
    spot: float,
    strike: float,
    rate: float,
    volatility: float,
    time_to_expiry: float,
    dividend_yield: float = 0.0,
) -> dict[str, float]:
    option_type = option_type.lower()
    if time_to_expiry <= 0 or volatility <= 0 or spot <= 0 or strike <= 0:
        intrinsic = 1.0 if (
            option_type == "call" and spot > strike
        ) or (
            option_type == "put" and spot < strike
        ) else 0.0
        delta = intrinsic if option_type == "call" else -intrinsic
        return {
            "delta": delta,
            "gamma": 0.0,
            "theta": 0.0,
            "vega": 0.0,
            "prob_itm": intrinsic,
            "d1": 0.0,
            "d2": 0.0,
        }

    sqrt_t = math.sqrt(time_to_expiry)
    d1 = (
        math.log(spot / strike)
        + (rate - dividend_yield + 0.5 * volatility * volatility) * time_to_expiry
    ) / (volatility * sqrt_t)
    d2 = d1 - volatility * sqrt_t
    df_q = math.exp(-dividend_yield * time_to_expiry)
    df_r = math.exp(-rate * time_to_expiry)

    gamma = df_q * norm_pdf(d1) / (spot * volatility * sqrt_t)
    vega = spot * df_q * norm_pdf(d1) * sqrt_t / 100.0
    if option_type == "call":
        delta = df_q * norm_cdf(d1)
        theta = (
            -spot * df_q * norm_pdf(d1) * volatility / (2 * sqrt_t)
            - rate * strike * df_r * norm_cdf(d2)
            + dividend_yield * spot * df_q * norm_cdf(d1)
        ) / 365.0
        prob_itm = norm_cdf(d2)
    elif option_type == "put":
        delta = -df_q * norm_cdf(-d1)
        theta = (
            -spot * df_q * norm_pdf(d1) * volatility / (2 * sqrt_t)
            + rate * strike * df_r * norm_cdf(-d2)
            - dividend_yield * spot * df_q * norm_cdf(-d1)
        ) / 365.0
        prob_itm = norm_cdf(-d2)
    else:
        raise ValueError(f"Unknown option_type: {option_type}")

    return {
        "delta": delta,
        "gamma": gamma,
        "theta": theta,
        "vega": vega,
        "prob_itm": prob_itm,
        "d1": d1,
        "d2": d2,
    }


def implied_volatility(
    option_type: str,
    target_price: float,
    spot: float,
    strike: float,
    rate: float,
    time_to_expiry: float,
    dividend_yield: float = 0.0,
    low: float = 0.0001,
    high: float = 5.0,
    max_iter: int = 100,
) -> Optional[float]:
    if target_price <= 0 or spot <= 0 or strike <= 0 or time_to_expiry <= 0:
        return None
    intrinsic = black_scholes_price(
        option_type, spot, strike, rate, low, time_to_expiry, dividend_yield
    )
    if target_price < intrinsic - 1e-6:
        return None
    for _ in range(max_iter):
        mid = (low + high) / 2.0
        price = black_scholes_price(
            option_type, spot, strike, rate, mid, time_to_expiry, dividend_yield
        )
        if abs(price - target_price) < 1e-5:
            return mid
        if price < target_price:
            low = mid
        else:
            high = mid
    return (low + high) / 2.0


def realized_volatility(bars: list["DailyBar"], as_of: dt.date, window: int = 63) -> Optional[float]:
    closes = [bar.close for bar in bars if bar.date < as_of and bar.close > 0]
    if len(closes) < max(10, window // 2):
        return None
    closes = closes[-(window + 1) :]
    returns = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    if len(returns) < 2:
        return None
    return statistics.stdev(returns) * math.sqrt(TRADING_DAYS)


def max_drawdown(values: list[float]) -> float:
    if not values:
        return 0.0
    peak = values[0]
    worst = 0.0
    for value in values:
        peak = max(peak, value)
        if peak > 0:
            worst = min(worst, value / peak - 1.0)
    return worst


def cagr(start_value: float, end_value: float, start: dt.date, end: dt.date) -> float:
    years = max((end - start).days / 365.25, 1e-9)
    if start_value <= 0 or end_value <= 0:
        return 0.0
    return (end_value / start_value) ** (1.0 / years) - 1.0


def annualized_sharpe(equity: list[tuple[dt.date, float]]) -> float:
    returns = [
        equity[i][1] / equity[i - 1][1] - 1.0
        for i in range(1, len(equity))
        if equity[i - 1][1] > 0
    ]
    if len(returns) < 2:
        return 0.0
    stdev = statistics.stdev(returns)
    if stdev == 0:
        return 0.0
    return statistics.mean(returns) / stdev * math.sqrt(TRADING_DAYS)


def annualized_sortino(equity: list[tuple[dt.date, float]]) -> float:
    returns = [
        equity[i][1] / equity[i - 1][1] - 1.0
        for i in range(1, len(equity))
        if equity[i - 1][1] > 0
    ]
    downside = [r for r in returns if r < 0]
    if len(downside) < 2:
        return 0.0
    stdev = statistics.stdev(downside)
    if stdev == 0:
        return 0.0
    return statistics.mean(returns) / stdev * math.sqrt(TRADING_DAYS)


def occ_symbol(root: str, expiration: dt.date, option_type: str, strike: float) -> str:
    right = "C" if option_type.lower() == "call" else "P"
    strike_int = int(round(strike * 1000))
    return f"{root.upper()}{expiration:%y%m%d}{right}{strike_int:08d}"


def parse_occ_symbol(symbol: str) -> dict[str, Any]:
    clean = symbol[2:] if symbol.startswith("O:") else symbol
    marker_index = None
    for idx in range(len(clean) - 14):
        chunk = clean[idx : idx + 6]
        right = clean[idx + 6 : idx + 7]
        strike = clean[idx + 7 : idx + 15]
        if chunk.isdigit() and right in {"C", "P"} and strike.isdigit():
            marker_index = idx
            break
    if marker_index is None:
        raise ValueError(f"Cannot parse OCC option symbol: {symbol}")
    root = clean[:marker_index]
    yymmdd = clean[marker_index : marker_index + 6]
    right = clean[marker_index + 6]
    strike = int(clean[marker_index + 7 : marker_index + 15]) / 1000.0
    year = 2000 + int(yymmdd[:2])
    month = int(yymmdd[2:4])
    day = int(yymmdd[4:6])
    return {
        "root": root,
        "expiration": dt.date(year, month, day),
        "option_type": "call" if right == "C" else "put",
        "strike": strike,
    }


@dataclasses.dataclass
class DailyBar:
    date: dt.date
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclasses.dataclass
class OptionQuote:
    underlying: str
    contract_symbol: str
    option_type: str
    expiration: dt.date
    strike: float
    bid: Optional[float] = None
    ask: Optional[float] = None
    last: Optional[float] = None
    close: Optional[float] = None
    delta: Optional[float] = None
    gamma: Optional[float] = None
    theta: Optional[float] = None
    vega: Optional[float] = None
    iv: Optional[float] = None
    open_interest: Optional[int] = None
    volume: Optional[int] = None
    timestamp: Optional[str] = None

    @property
    def mid(self) -> Optional[float]:
        if self.bid is not None and self.ask is not None and self.bid >= 0 and self.ask >= 0:
            if self.ask == 0 and self.bid == 0:
                return None
            if self.ask >= self.bid:
                return (self.bid + self.ask) / 2.0
        return self.close if self.close is not None else self.last

    @property
    def spread_pct(self) -> Optional[float]:
        mid = self.mid
        if mid is None or mid <= 0 or self.bid is None or self.ask is None:
            return None
        return max(self.ask - self.bid, 0.0) / mid

    def dte(self, as_of: dt.date) -> int:
        return max((self.expiration - as_of).days, 0)

    def enrich_greeks(
        self,
        spot: float,
        as_of: dt.date,
        rate: float,
        fallback_vol: Optional[float] = None,
    ) -> "OptionQuote":
        t = year_fraction(as_of, self.expiration)
        mid = self.mid
        iv = self.iv
        if iv is None and mid is not None:
            iv = implied_volatility(self.option_type, mid, spot, self.strike, rate, t)
        if iv is None:
            iv = fallback_vol
        if iv is None:
            return self
        greeks = black_scholes_greeks(self.option_type, spot, self.strike, rate, iv, t)
        self.iv = iv
        self.delta = self.delta if self.delta is not None else greeks["delta"]
        self.gamma = self.gamma if self.gamma is not None else greeks["gamma"]
        self.theta = self.theta if self.theta is not None else greeks["theta"]
        self.vega = self.vega if self.vega is not None else greeks["vega"]
        return self

    def probability_itm(self, spot: float, as_of: dt.date, rate: float) -> Optional[float]:
        vol = self.iv
        if vol is None:
            return abs(self.delta) if self.delta is not None else None
        return black_scholes_greeks(
            self.option_type, spot, self.strike, rate, vol, year_fraction(as_of, self.expiration)
        )["prob_itm"]


@dataclasses.dataclass
class StrategyConfig:
    dte_min: int = 25
    dte_max: int = 45
    put_delta_min: float = -0.30
    put_delta_max: float = -0.20
    call_delta_min: float = 0.25
    call_delta_max: float = 0.35
    max_spread_pct: float = 0.05
    min_open_interest: int = 100
    min_volume: int = 1
    target_annual_yield: float = 0.10
    min_iv_hv_ratio: float = 1.10
    risk_free_rate: float = 0.04
    contract_fee: float = 0.65


@dataclasses.dataclass
class Candidate:
    symbol: str
    phase: str
    quote: OptionQuote
    as_of: dt.date
    spot: float
    annualized_yield: float
    monthly_yield: float
    collateral: float
    assignment_probability: Optional[float]
    pop: Optional[float]
    iv_hv_ratio: Optional[float]
    historical_volatility: Optional[float]
    cost_basis: Optional[float]
    risk_flags: list[str]
    rank_score: float

    def to_row(self) -> dict[str, Any]:
        q = self.quote
        return {
            "as_of": self.as_of.isoformat(),
            "symbol": self.symbol,
            "phase": self.phase,
            "contract_symbol": q.contract_symbol,
            "expiration": q.expiration.isoformat(),
            "strike": round(q.strike, 4),
            "spot": round(self.spot, 4),
            "bid": round(q.bid, 4) if q.bid is not None else "",
            "ask": round(q.ask, 4) if q.ask is not None else "",
            "mid": round(q.mid, 4) if q.mid is not None else "",
            "dte": q.dte(self.as_of),
            "delta": round(q.delta, 4) if q.delta is not None else "",
            "iv": round(q.iv, 4) if q.iv is not None else "",
            "theta": round(q.theta, 4) if q.theta is not None else "",
            "open_interest": q.open_interest if q.open_interest is not None else "",
            "volume": q.volume if q.volume is not None else "",
            "spread_pct": round(q.spread_pct, 4) if q.spread_pct is not None else "",
            "assignment_probability": round(self.assignment_probability, 4)
            if self.assignment_probability is not None
            else "",
            "pop": round(self.pop, 4) if self.pop is not None else "",
            "collateral": round(self.collateral, 2),
            "expected_monthly_yield": round(self.monthly_yield, 4),
            "annualized_yield": round(self.annualized_yield, 4),
            "cost_basis": round(self.cost_basis, 4) if self.cost_basis is not None else "",
            "iv_hv_ratio": round(self.iv_hv_ratio, 4) if self.iv_hv_ratio is not None else "",
            "historical_volatility": round(self.historical_volatility, 4)
            if self.historical_volatility is not None
            else "",
            "rank_score": round(self.rank_score, 4),
            "risk_flags": ";".join(self.risk_flags),
        }


class MarketDataProvider:
    name = "base"

    def get_underlying_price(self, symbol: str, as_of: Optional[dt.date] = None) -> float:
        raise NotImplementedError

    def get_price_history(self, symbol: str, start: dt.date, end: dt.date) -> list[DailyBar]:
        raise NotImplementedError

    def list_option_contracts(
        self,
        underlying: str,
        expiration_gte: dt.date,
        expiration_lte: dt.date,
        option_type: str,
        as_of: Optional[dt.date] = None,
        strike_gte: Optional[float] = None,
        strike_lte: Optional[float] = None,
    ) -> list[OptionQuote]:
        raise NotImplementedError

    def get_option_chain(
        self,
        underlying: str,
        expiration_gte: dt.date,
        expiration_lte: dt.date,
        option_type: str,
    ) -> list[OptionQuote]:
        return self.list_option_contracts(underlying, expiration_gte, expiration_lte, option_type)

    def get_option_eod(self, quote: OptionQuote, as_of: dt.date) -> Optional[OptionQuote]:
        raise NotImplementedError


class HTTPProvider(MarketDataProvider):
    def __init__(self, cache_dir: Path = Path(".cache/wheel_income"), timeout: int = 30) -> None:
        self.cache_dir = cache_dir
        self.timeout = timeout
        ensure_dir(cache_dir)

    def request_json(
        self,
        url: str,
        params: Optional[dict[str, Any]] = None,
        headers: Optional[dict[str, str]] = None,
        cache_seconds: int = 86400,
    ) -> dict[str, Any]:
        if requests is None:
            raise DataProviderError("The requests package is required for HTTP providers.")
        params = {k: v for k, v in (params or {}).items() if v is not None}
        cache_key = hashlib.sha256(
            (url + "?" + urlencode(sorted(params.items()))).encode("utf-8")
        ).hexdigest()
        cache_path = self.cache_dir / f"{cache_key}.json"
        if cache_path.exists() and time.time() - cache_path.stat().st_mtime < cache_seconds:
            return json.loads(cache_path.read_text())
        response = requests.get(url, params=params, headers=headers, timeout=self.timeout)
        if response.status_code >= 400:
            raise DataProviderError(f"HTTP {response.status_code} for {url}: {response.text[:500]}")
        payload = response.json()
        cache_path.write_text(json.dumps(payload), encoding="utf-8")
        return payload


class PolygonProvider(HTTPProvider):
    name = "polygon"

    def __init__(self, api_key: str, base_url: str = "https://api.polygon.io", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        if not api_key:
            raise DataProviderError("POLYGON_API_KEY is required for provider=polygon.")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")

    @classmethod
    def from_env(cls, cache_dir: Path) -> "PolygonProvider":
        return cls(os.getenv("POLYGON_API_KEY", ""), cache_dir=cache_dir)

    def _with_key(self, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        merged = dict(params or {})
        merged["apiKey"] = self.api_key
        return merged

    def get_price_history(self, symbol: str, start: dt.date, end: dt.date) -> list[DailyBar]:
        url = f"{self.base_url}/v2/aggs/ticker/{symbol}/range/1/day/{start}/{end}"
        payload = self.request_json(
            url,
            self._with_key({"adjusted": "true", "sort": "asc", "limit": 50000}),
        )
        bars = []
        for item in payload.get("results", []):
            date = dt.datetime.utcfromtimestamp(item["t"] / 1000).date()
            bars.append(
                DailyBar(
                    date=date,
                    open=float(item["o"]),
                    high=float(item["h"]),
                    low=float(item["l"]),
                    close=float(item["c"]),
                    volume=float(item.get("v", 0)),
                )
            )
        return bars

    def get_underlying_price(self, symbol: str, as_of: Optional[dt.date] = None) -> float:
        end = as_of or dt.date.today()
        bars = self.get_price_history(symbol, end - dt.timedelta(days=10), end)
        eligible = [bar for bar in bars if as_of is None or bar.date <= as_of]
        if not eligible:
            raise DataProviderError(f"No price history for {symbol} as of {end}.")
        return eligible[-1].close

    @staticmethod
    def normalize_option_contract(item: dict[str, Any]) -> OptionQuote:
        ticker = item.get("ticker") or item.get("symbol")
        details = parse_occ_symbol(ticker)
        return OptionQuote(
            underlying=item.get("underlying_ticker") or details["root"],
            contract_symbol=ticker[2:] if ticker.startswith("O:") else ticker,
            option_type=item.get("contract_type") or details["option_type"],
            expiration=parse_date(item.get("expiration_date") or details["expiration"]),
            strike=safe_float(item.get("strike_price"), details["strike"]),
        )

    @staticmethod
    def normalize_option_snapshot(item: dict[str, Any], underlying: Optional[str] = None) -> OptionQuote:
        details = item.get("details", {})
        greeks = item.get("greeks", {}) or {}
        day = item.get("day", {}) or {}
        last_quote = item.get("last_quote", {}) or {}
        ticker = details.get("ticker") or item.get("ticker") or item.get("symbol")
        parsed = parse_occ_symbol(ticker)
        bid = safe_float(last_quote.get("bid"))
        ask = safe_float(last_quote.get("ask"))
        close = safe_float(day.get("close"))
        return OptionQuote(
            underlying=underlying or details.get("underlying_ticker") or parsed["root"],
            contract_symbol=ticker[2:] if ticker.startswith("O:") else ticker,
            option_type=details.get("contract_type") or parsed["option_type"],
            expiration=parse_date(details.get("expiration_date") or parsed["expiration"]),
            strike=safe_float(details.get("strike_price"), parsed["strike"]),
            bid=bid,
            ask=ask,
            last=safe_float(item.get("last_trade", {}).get("price")),
            close=close,
            delta=safe_float(greeks.get("delta")),
            gamma=safe_float(greeks.get("gamma")),
            theta=safe_float(greeks.get("theta")),
            vega=safe_float(greeks.get("vega")),
            iv=safe_float(item.get("implied_volatility")),
            open_interest=safe_int(item.get("open_interest")),
            volume=safe_int(day.get("volume")),
        )

    def list_option_contracts(
        self,
        underlying: str,
        expiration_gte: dt.date,
        expiration_lte: dt.date,
        option_type: str,
        as_of: Optional[dt.date] = None,
        strike_gte: Optional[float] = None,
        strike_lte: Optional[float] = None,
    ) -> list[OptionQuote]:
        url = f"{self.base_url}/v3/reference/options/contracts"
        params = self._with_key(
            {
                "underlying_ticker": underlying,
                "contract_type": option_type.lower(),
                "expiration_date.gte": expiration_gte.isoformat(),
                "expiration_date.lte": expiration_lte.isoformat(),
                "strike_price.gte": strike_gte,
                "strike_price.lte": strike_lte,
                "as_of": as_of.isoformat() if as_of else None,
                "limit": 1000,
            }
        )
        contracts: list[OptionQuote] = []
        while url:
            payload = self.request_json(url, params)
            contracts.extend(self.normalize_option_contract(item) for item in payload.get("results", []))
            next_url = payload.get("next_url")
            if next_url:
                url = next_url
                params = self._with_key({})
            else:
                url = ""
        return contracts

    def get_option_chain(
        self,
        underlying: str,
        expiration_gte: dt.date,
        expiration_lte: dt.date,
        option_type: str,
    ) -> list[OptionQuote]:
        url = f"{self.base_url}/v3/snapshot/options/{underlying}"
        params = self._with_key(
            {
                "contract_type": option_type.lower(),
                "expiration_date.gte": expiration_gte.isoformat(),
                "expiration_date.lte": expiration_lte.isoformat(),
                "limit": 250,
            }
        )
        quotes: list[OptionQuote] = []
        while url:
            payload = self.request_json(url, params, cache_seconds=900)
            quotes.extend(
                self.normalize_option_snapshot(item, underlying) for item in payload.get("results", [])
            )
            next_url = payload.get("next_url")
            if next_url:
                url = next_url
                params = self._with_key({})
            else:
                url = ""
        return quotes

    def get_option_eod(self, quote: OptionQuote, as_of: dt.date) -> Optional[OptionQuote]:
        ticker = quote.contract_symbol
        ticker = ticker if ticker.startswith("O:") else f"O:{ticker}"
        url = f"{self.base_url}/v2/aggs/ticker/{ticker}/range/1/day/{as_of}/{as_of}"
        payload = self.request_json(url, self._with_key({"adjusted": "true", "sort": "asc", "limit": 1}))
        results = payload.get("results", [])
        if not results:
            return None
        item = results[0]
        close = safe_float(item.get("c"))
        if close is None or close <= 0:
            return None
        return dataclasses.replace(
            quote,
            bid=close,
            ask=close,
            close=close,
            volume=safe_int(item.get("v"), quote.volume),
            timestamp=as_of.isoformat(),
        )


class ThetaDataProvider(HTTPProvider):
    name = "theta"

    def __init__(self, base_url: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        if not base_url:
            raise DataProviderError("THETADATA_BASE_URL is required for provider=theta.")
        self.base_url = base_url.rstrip("/")

    @classmethod
    def from_env(cls, cache_dir: Path) -> "ThetaDataProvider":
        return cls(os.getenv("THETADATA_BASE_URL", ""), cache_dir=cache_dir)

    @staticmethod
    def yyyymmdd(value: dt.date) -> str:
        return value.strftime("%Y%m%d")

    @staticmethod
    def rows_from_theta(payload: dict[str, Any]) -> list[dict[str, Any]]:
        if isinstance(payload.get("response"), list):
            rows = payload["response"]
        else:
            rows = payload.get("data", [])
        if not rows:
            return []
        if isinstance(rows[0], dict):
            return rows
        header = payload.get("header", {})
        columns = header.get("format") or header.get("columns")
        if not columns:
            raise DataProviderError("ThetaData response is missing header format.")
        return [dict(zip(columns, row)) for row in rows]

    def get_price_history(self, symbol: str, start: dt.date, end: dt.date) -> list[DailyBar]:
        url = f"{self.base_url}/v2/hist/stock/eod"
        payload = self.request_json(
            url,
            {
                "root": symbol,
                "start_date": self.yyyymmdd(start),
                "end_date": self.yyyymmdd(end),
            },
        )
        bars = []
        for row in self.rows_from_theta(payload):
            date_value = str(row.get("date") or row.get("trade_date"))
            date = dt.datetime.strptime(date_value, "%Y%m%d").date()
            bars.append(
                DailyBar(
                    date=date,
                    open=float(row.get("open")),
                    high=float(row.get("high")),
                    low=float(row.get("low")),
                    close=float(row.get("close")),
                    volume=safe_float(row.get("volume"), 0.0) or 0.0,
                )
            )
        return bars

    def get_underlying_price(self, symbol: str, as_of: Optional[dt.date] = None) -> float:
        end = as_of or dt.date.today()
        bars = self.get_price_history(symbol, end - dt.timedelta(days=10), end)
        if not bars:
            raise DataProviderError(f"No ThetaData stock EOD rows for {symbol}.")
        return bars[-1].close

    def list_option_contracts(
        self,
        underlying: str,
        expiration_gte: dt.date,
        expiration_lte: dt.date,
        option_type: str,
        as_of: Optional[dt.date] = None,
        strike_gte: Optional[float] = None,
        strike_lte: Optional[float] = None,
    ) -> list[OptionQuote]:
        expirations_url = f"{self.base_url}/v2/list/expirations"
        payload = self.request_json(expirations_url, {"root": underlying})
        rows = self.rows_from_theta(payload)
        expirations = []
        for row in rows:
            raw = row.get("expiration") or row.get("exp") or row.get("date")
            if raw is None:
                continue
            exp = dt.datetime.strptime(str(raw), "%Y%m%d").date()
            if expiration_gte <= exp <= expiration_lte:
                expirations.append(exp)

        quotes: list[OptionQuote] = []
        right = "C" if option_type.lower() == "call" else "P"
        for exp in expirations:
            strikes_payload = self.request_json(
                f"{self.base_url}/v2/list/strikes",
                {"root": underlying, "exp": self.yyyymmdd(exp)},
            )
            for row in self.rows_from_theta(strikes_payload):
                raw_strike = row.get("strike") or row.get("strike_price")
                strike = safe_float(raw_strike)
                if strike is None:
                    continue
                if strike_gte is not None and strike < strike_gte:
                    continue
                if strike_lte is not None and strike > strike_lte:
                    continue
                quotes.append(
                    OptionQuote(
                        underlying=underlying,
                        contract_symbol=occ_symbol(underlying, exp, option_type, strike),
                        option_type=option_type.lower(),
                        expiration=exp,
                        strike=strike,
                    )
                )
        return quotes

    def get_option_eod(self, quote: OptionQuote, as_of: dt.date) -> Optional[OptionQuote]:
        parsed = parse_occ_symbol(quote.contract_symbol)
        url = f"{self.base_url}/v2/hist/option/eod"
        payload = self.request_json(
            url,
            {
                "root": parsed["root"],
                "exp": self.yyyymmdd(parsed["expiration"]),
                "right": "C" if parsed["option_type"] == "call" else "P",
                "strike": int(round(parsed["strike"] * 1000)),
                "start_date": self.yyyymmdd(as_of),
                "end_date": self.yyyymmdd(as_of),
            },
        )
        rows = self.rows_from_theta(payload)
        if not rows:
            return None
        row = rows[-1]
        close = safe_float(row.get("close"))
        if close is None or close <= 0:
            return None
        bid = safe_float(row.get("bid"), close)
        ask = safe_float(row.get("ask"), close)
        return dataclasses.replace(
            quote,
            bid=bid,
            ask=ask,
            close=close,
            volume=safe_int(row.get("volume"), quote.volume),
            open_interest=safe_int(row.get("open_interest"), quote.open_interest),
            timestamp=as_of.isoformat(),
        )


class AlpacaProvider(HTTPProvider):
    name = "alpaca"

    def __init__(
        self,
        api_key_id: str,
        api_secret_key: str,
        base_url: str = "https://data.alpaca.markets",
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        if not api_key_id or not api_secret_key:
            raise DataProviderError("ALPACA_API_KEY_ID and ALPACA_API_SECRET_KEY are required.")
        self.base_url = base_url.rstrip("/")
        self.headers = {
            "APCA-API-KEY-ID": api_key_id,
            "APCA-API-SECRET-KEY": api_secret_key,
        }

    @classmethod
    def from_env(cls, cache_dir: Path) -> "AlpacaProvider":
        return cls(
            os.getenv("ALPACA_API_KEY_ID", ""),
            os.getenv("ALPACA_API_SECRET_KEY", ""),
            cache_dir=cache_dir,
        )

    def get_price_history(self, symbol: str, start: dt.date, end: dt.date) -> list[DailyBar]:
        payload = self.request_json(
            f"{self.base_url}/v2/stocks/{symbol}/bars",
            {
                "start": start.isoformat(),
                "end": (end + dt.timedelta(days=1)).isoformat(),
                "timeframe": "1Day",
                "adjustment": "all",
                "limit": 10000,
            },
            headers=self.headers,
        )
        bars = []
        for item in payload.get("bars", []):
            bars.append(
                DailyBar(
                    date=dt.datetime.fromisoformat(item["t"].replace("Z", "+00:00")).date(),
                    open=float(item["o"]),
                    high=float(item["h"]),
                    low=float(item["l"]),
                    close=float(item["c"]),
                    volume=float(item.get("v", 0)),
                )
            )
        return bars

    def get_underlying_price(self, symbol: str, as_of: Optional[dt.date] = None) -> float:
        if as_of is not None:
            bars = self.get_price_history(symbol, as_of - dt.timedelta(days=10), as_of)
            if not bars:
                raise DataProviderError(f"No Alpaca bars for {symbol}.")
            return bars[-1].close
        payload = self.request_json(
            f"{self.base_url}/v2/stocks/{symbol}/snapshot",
            headers=self.headers,
            cache_seconds=300,
        )
        latest_trade = payload.get("latestTrade") or payload.get("latest_trade") or {}
        price = safe_float(latest_trade.get("p") or latest_trade.get("price"))
        if price is None:
            raise DataProviderError(f"No Alpaca latest price for {symbol}.")
        return price

    def list_option_contracts(
        self,
        underlying: str,
        expiration_gte: dt.date,
        expiration_lte: dt.date,
        option_type: str,
        as_of: Optional[dt.date] = None,
        strike_gte: Optional[float] = None,
        strike_lte: Optional[float] = None,
    ) -> list[OptionQuote]:
        payload = self.request_json(
            f"{self.base_url}/v2/options/contracts",
            {
                "underlying_symbols": underlying,
                "expiration_date_gte": expiration_gte.isoformat(),
                "expiration_date_lte": expiration_lte.isoformat(),
                "type": option_type.lower(),
                "strike_price_gte": strike_gte,
                "strike_price_lte": strike_lte,
                "limit": 1000,
            },
            headers=self.headers,
            cache_seconds=900,
        )
        contracts = payload.get("option_contracts") or payload.get("contracts") or []
        quotes = []
        for item in contracts:
            symbol = item.get("symbol")
            if not symbol:
                continue
            parsed = parse_occ_symbol(symbol)
            quotes.append(
                OptionQuote(
                    underlying=underlying,
                    contract_symbol=symbol,
                    option_type=item.get("type") or parsed["option_type"],
                    expiration=parse_date(item.get("expiration_date") or parsed["expiration"]),
                    strike=safe_float(item.get("strike_price"), parsed["strike"]),
                    open_interest=safe_int(item.get("open_interest")),
                    close=safe_float(item.get("close_price")),
                )
            )
        return quotes

    def get_option_chain(
        self,
        underlying: str,
        expiration_gte: dt.date,
        expiration_lte: dt.date,
        option_type: str,
    ) -> list[OptionQuote]:
        contracts = self.list_option_contracts(underlying, expiration_gte, expiration_lte, option_type)
        enriched: list[OptionQuote] = []
        for quote in contracts:
            snapshot = self.get_option_snapshot(quote.contract_symbol)
            if snapshot is not None:
                enriched.append(snapshot)
        return enriched

    def get_option_snapshot(self, symbol: str) -> Optional[OptionQuote]:
        payload = self.request_json(
            f"{self.base_url}/v1beta1/options/snapshots/{symbol}",
            headers=self.headers,
            cache_seconds=300,
        )
        snapshot = payload.get("snapshot") or payload
        parsed = parse_occ_symbol(symbol)
        latest_quote = snapshot.get("latestQuote") or snapshot.get("latest_quote") or {}
        greeks = snapshot.get("greeks") or {}
        return OptionQuote(
            underlying=parsed["root"],
            contract_symbol=symbol,
            option_type=parsed["option_type"],
            expiration=parsed["expiration"],
            strike=parsed["strike"],
            bid=safe_float(latest_quote.get("bp") or latest_quote.get("bid_price")),
            ask=safe_float(latest_quote.get("ap") or latest_quote.get("ask_price")),
            delta=safe_float(greeks.get("delta")),
            gamma=safe_float(greeks.get("gamma")),
            theta=safe_float(greeks.get("theta")),
            vega=safe_float(greeks.get("vega")),
            iv=safe_float(snapshot.get("impliedVolatility") or snapshot.get("implied_volatility")),
        )

    def get_option_eod(self, quote: OptionQuote, as_of: dt.date) -> Optional[OptionQuote]:
        raise DataProviderError("Alpaca provider is intended for current signals, not historical backtests.")


class SyntheticProvider(MarketDataProvider):
    """Deterministic provider for tests and dry-run demos."""

    name = "synthetic"

    def __init__(self, volatility: float = 0.35) -> None:
        self.volatility = volatility

    def _price_for_date(self, symbol: str, date: dt.date) -> float:
        base = {"SPY": 500.0, "QQQ": 400.0, "TLT": 90.0, "GLD": 200.0}.get(symbol, 100.0)
        day = (date - dt.date(2022, 1, 1)).days
        wave = math.sin(day / 23.0) * 0.04
        trend = day / 365.0 * 0.05
        shock = -0.18 if 80 <= day % 365 <= 120 else 0.0
        return round(base * (1.0 + trend + wave + shock), 2)

    def get_price_history(self, symbol: str, start: dt.date, end: dt.date) -> list[DailyBar]:
        bars = []
        current = start
        while current <= end:
            if current.weekday() < 5:
                close = self._price_for_date(symbol, current)
                bars.append(
                    DailyBar(
                        date=current,
                        open=close * 0.995,
                        high=close * 1.01,
                        low=close * 0.99,
                        close=close,
                        volume=5_000_000,
                    )
                )
            current += dt.timedelta(days=1)
        return bars

    def get_underlying_price(self, symbol: str, as_of: Optional[dt.date] = None) -> float:
        return self._price_for_date(symbol, as_of or dt.date.today())

    def list_option_contracts(
        self,
        underlying: str,
        expiration_gte: dt.date,
        expiration_lte: dt.date,
        option_type: str,
        as_of: Optional[dt.date] = None,
        strike_gte: Optional[float] = None,
        strike_lte: Optional[float] = None,
    ) -> list[OptionQuote]:
        as_of = as_of or dt.date.today()
        expirations = []
        current = expiration_gte
        while current <= expiration_lte:
            if current.weekday() == 4:
                expirations.append(current)
            current += dt.timedelta(days=1)
        if not expirations:
            expirations = [expiration_gte + (expiration_lte - expiration_gte) // 2]
        spot = self.get_underlying_price(underlying, as_of)
        quotes = []
        low = strike_gte if strike_gte is not None else spot * 0.65
        high = strike_lte if strike_lte is not None else spot * 1.35
        step = 1.0 if spot < 100 else 5.0
        start = math.floor(low / step) * step
        strike = start
        while strike <= high:
            for exp in expirations:
                quotes.append(
                    OptionQuote(
                        underlying=underlying,
                        contract_symbol=occ_symbol(underlying, exp, option_type, strike),
                        option_type=option_type.lower(),
                        expiration=exp,
                        strike=round(strike, 3),
                        open_interest=1000,
                        volume=250,
                    )
                )
            strike += step
        return quotes

    def get_option_chain(
        self,
        underlying: str,
        expiration_gte: dt.date,
        expiration_lte: dt.date,
        option_type: str,
    ) -> list[OptionQuote]:
        as_of = dt.date.today()
        quotes = self.list_option_contracts(
            underlying, expiration_gte, expiration_lte, option_type, as_of=as_of
        )
        return [self._priced_quote(q, as_of) for q in quotes]

    def _priced_quote(self, quote: OptionQuote, as_of: dt.date) -> OptionQuote:
        spot = self.get_underlying_price(quote.underlying, as_of)
        t = year_fraction(as_of, quote.expiration)
        mid = black_scholes_price(quote.option_type, spot, quote.strike, 0.04, self.volatility, t)
        mid = max(mid, 0.05)
        bid = round(max(mid * 0.985, 0.01), 2)
        ask = round(mid * 1.015, 2)
        priced = dataclasses.replace(quote, bid=bid, ask=ask, close=round(mid, 2), iv=self.volatility)
        return priced.enrich_greeks(spot, as_of, 0.04, fallback_vol=self.volatility)

    def get_option_eod(self, quote: OptionQuote, as_of: dt.date) -> Optional[OptionQuote]:
        if as_of > quote.expiration:
            return None
        return self._priced_quote(quote, as_of)


class StrategyEngine:
    def __init__(self, config: StrategyConfig) -> None:
        self.config = config

    def liquidity_flags(self, quote: OptionQuote, strict: bool = True) -> list[str]:
        flags = []
        mid = quote.mid
        if mid is None or mid <= 0:
            flags.append("no_mid_price")
        spread_pct = quote.spread_pct
        if spread_pct is None:
            if strict:
                flags.append("missing_spread")
        elif spread_pct > self.config.max_spread_pct:
            flags.append("wide_spread")
        if quote.open_interest is None:
            if strict:
                flags.append("missing_open_interest")
        elif quote.open_interest < self.config.min_open_interest:
            flags.append("low_open_interest")
        if quote.volume is None:
            if strict:
                flags.append("missing_volume")
        elif quote.volume < self.config.min_volume:
            flags.append("low_volume")
        return flags

    def select_csp(
        self,
        symbol: str,
        as_of: dt.date,
        spot: float,
        quotes: Iterable[OptionQuote],
        history: list[DailyBar],
        strict_liquidity: bool = True,
    ) -> list[Candidate]:
        hv = realized_volatility(history, as_of, 63)
        fallback_vol = hv * 1.2 if hv else None
        candidates = []
        for quote in quotes:
            if quote.option_type.lower() != "put":
                continue
            quote.enrich_greeks(spot, as_of, self.config.risk_free_rate, fallback_vol)
            dte = quote.dte(as_of)
            mid = quote.mid
            if mid is None or mid <= 0 or dte <= 0:
                continue
            flags = self.liquidity_flags(quote, strict=strict_liquidity)
            if not (self.config.dte_min <= dte <= self.config.dte_max):
                flags.append("outside_dte_window")
            if quote.delta is None:
                flags.append("missing_delta")
            elif not (self.config.put_delta_min <= quote.delta <= self.config.put_delta_max):
                flags.append("outside_put_delta_window")
            iv_hv_ratio = quote.iv / hv if quote.iv is not None and hv and hv > 0 else None
            if iv_hv_ratio is None:
                flags.append("missing_vrp")
            elif iv_hv_ratio < self.config.min_iv_hv_ratio:
                flags.append("weak_vrp")
            collateral = quote.strike * CONTRACT_SIZE
            net_premium = mid * CONTRACT_SIZE - self.config.contract_fee
            annualized_yield = (net_premium / collateral) * (365.0 / dte)
            monthly_yield = net_premium / collateral
            if annualized_yield < self.config.target_annual_yield:
                flags.append("below_target_yield")
            assignment_probability = quote.probability_itm(spot, as_of, self.config.risk_free_rate)
            pop = 1.0 - assignment_probability if assignment_probability is not None else None
            if flags:
                continue
            score = annualized_yield * 100.0
            if iv_hv_ratio:
                score += min(iv_hv_ratio - 1.0, 1.0) * 10.0
            score -= (assignment_probability or abs(quote.delta or 0.0)) * 5.0
            candidates.append(
                Candidate(
                    symbol=symbol,
                    phase="CSP",
                    quote=quote,
                    as_of=as_of,
                    spot=spot,
                    annualized_yield=annualized_yield,
                    monthly_yield=monthly_yield,
                    collateral=collateral,
                    assignment_probability=assignment_probability,
                    pop=pop,
                    iv_hv_ratio=iv_hv_ratio,
                    historical_volatility=hv,
                    cost_basis=None,
                    risk_flags=[],
                    rank_score=score,
                )
            )
        return sorted(candidates, key=lambda c: c.rank_score, reverse=True)

    def select_cc(
        self,
        symbol: str,
        as_of: dt.date,
        spot: float,
        cost_basis: float,
        quotes: Iterable[OptionQuote],
        history: list[DailyBar],
        strict_liquidity: bool = True,
    ) -> list[Candidate]:
        hv = realized_volatility(history, as_of, 63)
        fallback_vol = hv * 1.2 if hv else None
        candidates = []
        fallback_candidates = []
        for quote in quotes:
            if quote.option_type.lower() != "call":
                continue
            quote.enrich_greeks(spot, as_of, self.config.risk_free_rate, fallback_vol)
            dte = quote.dte(as_of)
            mid = quote.mid
            if mid is None or mid <= 0 or dte <= 0:
                continue
            flags = self.liquidity_flags(quote, strict=strict_liquidity)
            if quote.strike < cost_basis:
                flags.append("strike_below_cost_basis")
            if not (self.config.dte_min <= dte <= self.config.dte_max):
                flags.append("outside_dte_window")
            if quote.delta is None:
                flags.append("missing_delta")
            target_delta = (
                quote.delta is not None
                and self.config.call_delta_min <= quote.delta <= self.config.call_delta_max
            )
            if not target_delta:
                flags.append("outside_call_delta_window")
            iv_hv_ratio = quote.iv / hv if quote.iv is not None and hv and hv > 0 else None
            if iv_hv_ratio is None:
                flags.append("missing_vrp")
            collateral = cost_basis * CONTRACT_SIZE
            net_premium = mid * CONTRACT_SIZE - self.config.contract_fee
            annualized_yield = (net_premium / collateral) * (365.0 / dte)
            monthly_yield = net_premium / collateral
            assignment_probability = quote.probability_itm(spot, as_of, self.config.risk_free_rate)
            pop = 1.0 - assignment_probability if assignment_probability is not None else None
            hard_flags = [f for f in flags if f not in {"outside_call_delta_window", "weak_vrp"}]
            if hard_flags:
                continue
            score = annualized_yield * 100.0 - abs(quote.strike - max(cost_basis, spot)) / max(spot, 1)
            candidate = Candidate(
                symbol=symbol,
                phase="CC",
                quote=quote,
                as_of=as_of,
                spot=spot,
                annualized_yield=annualized_yield,
                monthly_yield=monthly_yield,
                collateral=collateral,
                assignment_probability=assignment_probability,
                pop=pop,
                iv_hv_ratio=iv_hv_ratio,
                historical_volatility=hv,
                cost_basis=cost_basis,
                risk_flags=[] if target_delta else ["weak_premium_recovery"],
                rank_score=score,
            )
            if target_delta:
                candidates.append(candidate)
            else:
                fallback_candidates.append(candidate)
        selected = candidates if candidates else fallback_candidates
        return sorted(selected, key=lambda c: c.rank_score, reverse=True)


def read_positions(path: Optional[Path]) -> dict[str, float]:
    if not path:
        return {}
    positions = {}
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            symbol = row["symbol"].strip().upper()
            shares = safe_float(row.get("shares"), 0.0) or 0.0
            cost_basis = safe_float(row.get("cost_basis"))
            if shares >= CONTRACT_SIZE and cost_basis is not None:
                positions[symbol] = cost_basis
    return positions


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    ensure_dir(path.parent)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def provider_from_args(args: argparse.Namespace) -> MarketDataProvider:
    cache_dir = Path(args.cache_dir)
    if args.provider == "polygon":
        return PolygonProvider.from_env(cache_dir)
    if args.provider == "theta":
        return ThetaDataProvider.from_env(cache_dir)
    if args.provider == "alpaca":
        return AlpacaProvider.from_env(cache_dir)
    if args.provider == "synthetic":
        return SyntheticProvider()
    raise ValueError(args.provider)


def parse_symbols(value: Optional[str], include_leveraged: bool = False) -> list[str]:
    if value:
        return [item.strip().upper() for item in value.split(",") if item.strip()]
    symbols = list(CORE_ETFS)
    if include_leveraged:
        symbols.extend(LEVERAGED_ETFS)
    return symbols


def run_signal(args: argparse.Namespace) -> int:
    provider = provider_from_args(args)
    cfg = StrategyConfig(
        target_annual_yield=args.target_annual_yield,
        max_spread_pct=args.max_spread_pct,
        min_open_interest=args.min_open_interest,
        min_volume=args.min_volume,
        min_iv_hv_ratio=args.min_iv_hv_ratio,
    )
    engine = StrategyEngine(cfg)
    as_of = parse_date(args.as_of) if args.as_of else dt.date.today()
    symbols = parse_symbols(args.symbols, args.include_leveraged)
    positions = read_positions(Path(args.positions)) if args.positions else {}
    rows = []
    no_signal_rows = []

    for symbol in symbols:
        try:
            spot = provider.get_underlying_price(symbol, as_of if args.as_of else None)
            history = provider.get_price_history(symbol, as_of - dt.timedelta(days=420), as_of)
            exp_gte = as_of + dt.timedelta(days=cfg.dte_min)
            exp_lte = as_of + dt.timedelta(days=cfg.dte_max)
            if symbol in positions:
                chain = provider.get_option_chain(symbol, exp_gte, exp_lte, "call")
                candidates = engine.select_cc(
                    symbol, as_of, spot, positions[symbol], chain, history, strict_liquidity=True
                )
            else:
                chain = provider.get_option_chain(symbol, exp_gte, exp_lte, "put")
                candidates = engine.select_csp(symbol, as_of, spot, chain, history, strict_liquidity=True)
            if candidates:
                rows.append(candidates[0].to_row())
            else:
                no_signal_rows.append(
                    {
                        "as_of": as_of.isoformat(),
                        "symbol": symbol,
                        "spot": round(spot, 4),
                        "phase": "CC" if symbol in positions else "CSP",
                        "reason": "no eligible contracts after DTE/delta/liquidity/yield/VRP filters",
                    }
                )
        except Exception as exc:  # Keep the scan running across symbols.
            no_signal_rows.append(
                {
                    "as_of": as_of.isoformat(),
                    "symbol": symbol,
                    "spot": "",
                    "phase": "ERROR",
                    "reason": str(exc),
                }
            )

    out_dir = Path(args.out_dir)
    ensure_dir(out_dir)
    write_csv(out_dir / "signals.csv", rows)
    write_csv(out_dir / "no_signal_report.csv", no_signal_rows)
    print(json.dumps({"signals": rows, "no_signal": no_signal_rows}, indent=2, ensure_ascii=False))
    return 0


def close_on_or_before(bars: list[DailyBar], date: dt.date) -> Optional[DailyBar]:
    eligible = [bar for bar in bars if bar.date <= date]
    return eligible[-1] if eligible else None


def next_bar_on_or_after(bars: list[DailyBar], date: dt.date) -> Optional[DailyBar]:
    for bar in bars:
        if bar.date >= date:
            return bar
    return None


def next_bar_after(bars: list[DailyBar], date: dt.date) -> Optional[DailyBar]:
    for bar in bars:
        if bar.date > date:
            return bar
    return None


def build_historical_chain(
    provider: MarketDataProvider,
    symbol: str,
    as_of: dt.date,
    spot: float,
    option_type: str,
    cfg: StrategyConfig,
    cost_basis: Optional[float] = None,
) -> list[OptionQuote]:
    exp_gte = as_of + dt.timedelta(days=cfg.dte_min)
    exp_lte = as_of + dt.timedelta(days=cfg.dte_max)
    if option_type == "put":
        strike_gte, strike_lte = spot * 0.65, spot * 1.02
    else:
        strike_gte = max(cost_basis or spot, spot * 0.95)
        strike_lte = spot * 1.35
    contracts = provider.list_option_contracts(
        symbol,
        exp_gte,
        exp_lte,
        option_type,
        as_of=as_of,
        strike_gte=round(strike_gte, 2),
        strike_lte=round(strike_lte, 2),
    )
    priced = []
    for contract in contracts:
        quote = provider.get_option_eod(contract, as_of)
        if quote is not None:
            priced.append(quote)
    return priced


def run_backtest(args: argparse.Namespace) -> int:
    provider = provider_from_args(args)
    cfg = StrategyConfig(
        target_annual_yield=args.target_annual_yield,
        max_spread_pct=args.max_spread_pct,
        min_open_interest=0,
        min_volume=0,
        min_iv_hv_ratio=args.min_iv_hv_ratio,
        contract_fee=args.contract_fee,
    )
    engine = StrategyEngine(cfg)
    symbols = parse_symbols(args.symbols, args.include_leveraged)
    start = parse_date(args.start)
    end = parse_date(args.end)
    out_dir = Path(args.out_dir)
    ensure_dir(out_dir)

    all_trades: list[dict[str, Any]] = []
    all_equity: list[dict[str, Any]] = []
    monthly_income: dict[tuple[str, str], float] = {}
    summary_rows = []

    for symbol in symbols:
        bars = provider.get_price_history(symbol, start - dt.timedelta(days=420), end)
        test_bars = [bar for bar in bars if start <= bar.date <= end]
        if not test_bars:
            continue
        cash = float(args.capital)
        shares = 0
        cost_basis: Optional[float] = None
        entry = next_bar_on_or_after(test_bars, start)
        if entry is None:
            continue
        current_date = entry.date
        equity_series: list[tuple[dt.date, float]] = []
        assignments = 0
        wins = 0
        closed_cycles = 0
        cc_recovery_cycles = 0

        while current_date <= end:
            entry_bar = next_bar_on_or_after(test_bars, current_date)
            if entry_bar is None:
                break
            current_date = entry_bar.date
            spot = entry_bar.close
            equity_value = cash + shares * spot
            equity_series.append((current_date, equity_value))
            all_equity.append(
                {
                    "date": current_date.isoformat(),
                    "symbol": symbol,
                    "equity": round(equity_value, 2),
                    "cash": round(cash, 2),
                    "shares": shares,
                    "spot": round(spot, 4),
                    "cost_basis": round(cost_basis, 4) if cost_basis is not None else "",
                }
            )

            try:
                if shares == 0:
                    chain = build_historical_chain(provider, symbol, current_date, spot, "put", cfg)
                    candidates = engine.select_csp(
                        symbol, current_date, spot, chain, bars, strict_liquidity=False
                    )
                    if not candidates:
                        next_month = current_date + dt.timedelta(days=28)
                        current_date = next_month
                        continue
                    cand = candidates[0]
                    q = cand.quote
                    expiry_bar = close_on_or_before(test_bars, q.expiration)
                    if expiry_bar is None or expiry_bar.date <= current_date:
                        break
                    premium_cash = (q.mid or 0.0) * CONTRACT_SIZE - cfg.contract_fee
                    cash += premium_cash
                    month_key = (symbol, current_date.strftime("%Y-%m"))
                    monthly_income[month_key] = monthly_income.get(month_key, 0.0) + premium_cash
                    assigned = expiry_bar.close < q.strike
                    trade_pnl = premium_cash
                    if assigned:
                        assignments += 1
                        cash -= q.strike * CONTRACT_SIZE
                        shares = CONTRACT_SIZE
                        cost_basis = q.strike - premium_cash / CONTRACT_SIZE
                        trade_pnl = premium_cash + (expiry_bar.close - q.strike) * CONTRACT_SIZE
                    else:
                        wins += 1
                        closed_cycles += 1
                    all_trades.append(
                        {
                            "symbol": symbol,
                            "phase": "CSP",
                            "entry_date": current_date.isoformat(),
                            "expiration": q.expiration.isoformat(),
                            "contract_symbol": q.contract_symbol,
                            "strike": q.strike,
                            "premium": round(q.mid or 0.0, 4),
                            "entry_spot": round(spot, 4),
                            "expiry_spot": round(expiry_bar.close, 4),
                            "assigned_or_called": assigned,
                            "pnl_mark_to_expiry": round(trade_pnl, 2),
                            "cost_basis": round(cost_basis, 4) if cost_basis is not None else "",
                        }
                    )
                    current_date = expiry_bar.date + dt.timedelta(days=1)
                else:
                    assert cost_basis is not None
                    chain = build_historical_chain(
                        provider, symbol, current_date, spot, "call", cfg, cost_basis=cost_basis
                    )
                    candidates = engine.select_cc(
                        symbol,
                        current_date,
                        spot,
                        cost_basis,
                        chain,
                        bars,
                        strict_liquidity=False,
                    )
                    if not candidates:
                        current_date += dt.timedelta(days=28)
                        continue
                    cand = candidates[0]
                    q = cand.quote
                    expiry_bar = close_on_or_before(test_bars, q.expiration)
                    if expiry_bar is None or expiry_bar.date <= current_date:
                        break
                    premium_cash = (q.mid or 0.0) * CONTRACT_SIZE - cfg.contract_fee
                    cash += premium_cash
                    month_key = (symbol, current_date.strftime("%Y-%m"))
                    monthly_income[month_key] = monthly_income.get(month_key, 0.0) + premium_cash
                    prior_cost_basis = cost_basis
                    cost_basis = cost_basis - premium_cash / CONTRACT_SIZE
                    called = expiry_bar.close > q.strike
                    trade_pnl = premium_cash
                    if called:
                        cash += q.strike * CONTRACT_SIZE
                        trade_pnl = premium_cash + (q.strike - prior_cost_basis) * CONTRACT_SIZE
                        shares = 0
                        cost_basis = None
                        wins += 1 if trade_pnl > 0 else 0
                        closed_cycles += 1
                    else:
                        cc_recovery_cycles += 1
                    all_trades.append(
                        {
                            "symbol": symbol,
                            "phase": "CC",
                            "entry_date": current_date.isoformat(),
                            "expiration": q.expiration.isoformat(),
                            "contract_symbol": q.contract_symbol,
                            "strike": q.strike,
                            "premium": round(q.mid or 0.0, 4),
                            "entry_spot": round(spot, 4),
                            "expiry_spot": round(expiry_bar.close, 4),
                            "assigned_or_called": called,
                            "pnl_mark_to_expiry": round(trade_pnl, 2),
                            "cost_basis": round(cost_basis, 4) if cost_basis is not None else "",
                        }
                    )
                    current_date = expiry_bar.date + dt.timedelta(days=1)
            except DataProviderError as exc:
                all_trades.append(
                    {
                        "symbol": symbol,
                        "phase": "ERROR",
                        "entry_date": current_date.isoformat(),
                        "expiration": "",
                        "contract_symbol": "",
                        "strike": "",
                        "premium": "",
                        "entry_spot": round(spot, 4),
                        "expiry_spot": "",
                        "assigned_or_called": "",
                        "pnl_mark_to_expiry": "",
                        "cost_basis": "",
                        "error": str(exc),
                    }
                )
                break

        final_bar = test_bars[-1]
        final_equity = cash + shares * final_bar.close
        buy_hold = args.capital / test_bars[0].close * final_bar.close
        values = [value for _, value in equity_series] + [final_equity]
        summary_rows.append(
            {
                "symbol": symbol,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "start_equity": round(args.capital, 2),
                "end_equity": round(final_equity, 2),
                "cagr": round(cagr(args.capital, final_equity, start, end), 4),
                "max_drawdown": round(max_drawdown(values), 4),
                "sharpe": round(annualized_sharpe(equity_series), 4),
                "sortino": round(annualized_sortino(equity_series), 4),
                "assignment_rate": round(assignments / max(closed_cycles + assignments, 1), 4),
                "cc_recovery_cycles": cc_recovery_cycles,
                "win_rate": round(wins / max(closed_cycles + assignments + cc_recovery_cycles, 1), 4),
                "buy_hold_end_equity": round(buy_hold, 2),
                "buy_hold_cagr": round(cagr(args.capital, buy_hold, start, end), 4),
            }
        )

    write_csv(out_dir / "trades.csv", all_trades)
    write_csv(out_dir / "equity_curve.csv", all_equity)
    write_csv(
        out_dir / "monthly_income.csv",
        [
            {"symbol": symbol, "month": month, "premium_income": round(income, 2)}
            for (symbol, month), income in sorted(monthly_income.items())
        ],
    )
    write_csv(out_dir / "summary.csv", summary_rows)
    summary_md = ["# Wheel Backtest Summary", ""]
    for row in summary_rows:
        summary_md.extend(
            [
                f"## {row['symbol']}",
                "",
                f"- End equity: ${row['end_equity']:,.2f}",
                f"- CAGR: {row['cagr']:.2%}",
                f"- Max drawdown: {row['max_drawdown']:.2%}",
                f"- Sharpe: {row['sharpe']}",
                f"- Sortino: {row['sortino']}",
                f"- Assignment rate: {row['assignment_rate']:.2%}",
                f"- CC recovery cycles: {row['cc_recovery_cycles']}",
                f"- Win rate: {row['win_rate']:.2%}",
                f"- Buy-and-hold end equity: ${row['buy_hold_end_equity']:,.2f}",
                "",
            ]
        )
    (out_dir / "summary.md").write_text("\n".join(summary_md), encoding="utf-8")
    print(json.dumps({"summary": summary_rows, "out_dir": str(out_dir)}, indent=2))
    return 0


def run_universe(args: argparse.Namespace) -> int:
    provider = provider_from_args(args)
    cfg = StrategyConfig(
        target_annual_yield=args.target_annual_yield,
        max_spread_pct=args.max_spread_pct,
        min_open_interest=args.min_open_interest,
        min_volume=args.min_volume,
        min_iv_hv_ratio=args.min_iv_hv_ratio,
    )
    engine = StrategyEngine(cfg)
    as_of = parse_date(args.as_of) if args.as_of else dt.date.today()
    symbols = parse_symbols(args.symbols, args.include_leveraged)
    start = as_of - dt.timedelta(days=int(args.lookback_years * 365.25))
    rows = []

    for symbol in symbols:
        try:
            bars = provider.get_price_history(symbol, start, as_of)
            if len(bars) < 60:
                raise DataProviderError("insufficient price history")
            spot = provider.get_underlying_price(symbol, as_of if args.as_of else None)
            hv63 = realized_volatility(bars, as_of, 63)
            exp_gte = as_of + dt.timedelta(days=cfg.dte_min)
            exp_lte = as_of + dt.timedelta(days=cfg.dte_max)
            chain = provider.get_option_chain(symbol, exp_gte, exp_lte, "put")
            csp = engine.select_csp(symbol, as_of, spot, chain, bars, strict_liquidity=True)
            best = csp[0] if csp else None
            closes = [bar.close for bar in bars]
            volume_avg = statistics.mean([bar.volume for bar in bars[-63:]]) if bars else 0.0
            drawdown = max_drawdown(closes)
            leveraged_penalty = 25.0 if symbol in LEVERAGED_ETFS else 0.0
            best_yield = best.annualized_yield if best else 0.0
            best_vrp = best.iv_hv_ratio if best else None
            score = best_yield * 100.0 + ((best_vrp or 1.0) - 1.0) * 20.0 + drawdown * 20.0
            score -= leveraged_penalty
            rows.append(
                {
                    "as_of": as_of.isoformat(),
                    "symbol": symbol,
                    "spot": round(spot, 4),
                    "lookback_years": args.lookback_years,
                    "realized_vol_63d": round(hv63, 4) if hv63 else "",
                    "max_drawdown": round(drawdown, 4),
                    "avg_volume_63d": round(volume_avg, 0),
                    "best_csp_contract": best.quote.contract_symbol if best else "",
                    "best_csp_annualized_yield": round(best_yield, 4) if best else "",
                    "best_csp_iv_hv_ratio": round(best_vrp, 4) if best_vrp else "",
                    "risk_bucket": "high_risk_leveraged" if symbol in LEVERAGED_ETFS else "core",
                    "rank_score": round(score, 4),
                    "status": "eligible" if best else "no_eligible_csp",
                }
            )
        except Exception as exc:
            rows.append(
                {
                    "as_of": as_of.isoformat(),
                    "symbol": symbol,
                    "spot": "",
                    "lookback_years": args.lookback_years,
                    "realized_vol_63d": "",
                    "max_drawdown": "",
                    "avg_volume_63d": "",
                    "best_csp_contract": "",
                    "best_csp_annualized_yield": "",
                    "best_csp_iv_hv_ratio": "",
                    "risk_bucket": "high_risk_leveraged" if symbol in LEVERAGED_ETFS else "core",
                    "rank_score": "",
                    "status": f"error: {exc}",
                }
            )

    rows.sort(key=lambda row: row["rank_score"] if isinstance(row["rank_score"], float) else -9999, reverse=True)
    out_dir = Path(args.out_dir)
    ensure_dir(out_dir)
    write_csv(out_dir / "universe_rank.csv", rows)
    print(json.dumps(rows, indent=2, ensure_ascii=False))
    return 0


def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--provider", choices=["polygon", "theta", "alpaca", "synthetic"], default="polygon")
    parser.add_argument("--symbols", help="Comma-separated symbols. Defaults to core ETF universe.")
    parser.add_argument("--include-leveraged", action="store_true", help="Include TQQQ/SOXL high-risk watchlist.")
    parser.add_argument("--out-dir", default="results")
    parser.add_argument("--cache-dir", default=".cache/wheel_income")
    parser.add_argument("--as-of", help="YYYY-MM-DD scan date. Defaults to today.")
    parser.add_argument("--target-annual-yield", type=float, default=0.10)
    parser.add_argument("--max-spread-pct", type=float, default=0.05)
    parser.add_argument("--min-open-interest", type=int, default=100)
    parser.add_argument("--min-volume", type=int, default=1)
    parser.add_argument("--min-iv-hv-ratio", type=float, default=1.10)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ETF Wheel income signal and backtest script.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    signal = subparsers.add_parser("signal", help="Generate current monthly CSP/CC signals.")
    add_common_args(signal)
    signal.add_argument("--positions", help="Optional CSV with symbol,shares,cost_basis for CC signals.")
    signal.set_defaults(func=run_signal)

    universe = subparsers.add_parser("universe", help="Rank ETF universe by Wheel suitability.")
    add_common_args(universe)
    universe.add_argument("--lookback-years", type=float, default=3.0)
    universe.set_defaults(func=run_universe)

    backtest = subparsers.add_parser("backtest", help="Run historical ETF Wheel backtest.")
    add_common_args(backtest)
    backtest.add_argument("--start", required=True)
    backtest.add_argument("--end", required=True)
    backtest.add_argument("--capital", type=float, default=100000.0, help="Capital per symbol.")
    backtest.add_argument("--contract-fee", type=float, default=0.65)
    backtest.set_defaults(func=run_backtest)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
