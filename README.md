# ETF Wheel Income Signals

Research-grade Python tooling for an ETF-only Wheel strategy:

1. Sell cash-secured puts at a value-conscious strike.
2. If assigned, hold 100 ETF shares and sell covered calls above adjusted cost basis.
3. Track premium income, assignment risk, liquidity, IV/HV, and full-cycle performance.

This is not an auto-trader and not financial advice. The script emits signals and backtest reports so a human can decide whether the risk/reward is acceptable. A 10% annualized premium yield is a screening target, not a guaranteed outcome.

## Strategy Frame

The strategy rules are distilled from the two local research articles without republishing their full text:

- Do not chase the highest premium. Start with assets you are willing to hold through assignment.
- Prefer liquid ETF options where bid/ask spread, open interest, and volume support repeatable execution.
- Require a positive volatility setup: implied volatility should be meaningfully above realized volatility.
- Avoid structures where a short option premium cannot plausibly offset drawdown or path risk.
- After assignment, covered call strikes must be at or above adjusted cost basis unless the user explicitly decides to realize a loss.

Default ETF universe:

- Core: `SPY, QQQ, IWM, DIA, XLK, XLF, XLE, XLV, XLP, XLU, XLI, XLY, SMH, SOXX, TLT, GLD`
- High-risk watchlist: `TQQQ, SOXL`, excluded unless `--include-leveraged` is set.

## Data Sources

Primary historical backtests should use a provider with historical option chains and option price history:

- Polygon / Massive via `POLYGON_API_KEY`
- ThetaData via `THETADATA_BASE_URL`
- Alpaca can be used for current signal snapshots, but it is not the default long-history backtest source.
- Yahoo Finance via `--provider yahoo` uses real ETF daily prices but model-prices historical options from realized volatility. Use it only as a proxy when full historical option chains are unavailable.

References:

- [Alpaca historical option data](https://docs.alpaca.markets/us/docs/historical-option-data)
- [Polygon / Massive options docs](https://massive.com/docs/rest/options/overview)
- [ThetaData options data](https://www.thetadata.net/options-data)
- [SEC leveraged and inverse ETF bulletin](https://www.investor.gov/introduction-investing/general-resources/news-alerts/alerts-bulletins/investor-alerts/sec)
- [ProShares TQQQ](https://www.proshares.com/our-etfs/leveraged-and-inverse/tqqq)
- [Direxion SOXL](https://www.direxion.com/product/daily-semiconductor-bull-bear-3x-etfs)

## Setup

```bash
python3 -m pip install -r requirements.txt
cp .env.example .env
```

Set the relevant keys in your shell or `.env` loader. The script reads environment variables directly; it does not parse `.env` by itself.

## Usage

Current CSP/CC signal scan:

```bash
POLYGON_API_KEY=... python3 wheel_income_signals.py signal --provider polygon --out-dir results/signals
```

Include existing assigned positions for covered-call signals:

```bash
python3 wheel_income_signals.py signal --positions positions.csv
```

`positions.csv` columns:

```csv
symbol,shares,cost_basis
QQQ,100,650.25
```

Universe ranking:

```bash
python3 wheel_income_signals.py universe --provider polygon --lookback-years 3
```

Historical Wheel backtest:

```bash
python3 wheel_income_signals.py backtest \
  --provider polygon \
  --symbols QQQ,SPY,TLT,GLD \
  --start 2022-01-01 \
  --end 2026-05-22 \
  --capital 100000 \
  --out-dir results/backtest
```

Yahoo Finance proxy backtest:

```bash
python3 wheel_income_signals.py backtest \
  --provider yahoo \
  --symbols SPY,QQQ,IWM,DIA,XLK,XLF,XLE,XLV,XLP,XLU,XLI,XLY,SMH,SOXX,TLT,GLD \
  --start 2023-05-26 \
  --end 2026-05-22 \
  --capital 100000 \
  --out-dir reports/yahoo_proxy_3y
```

Run tests:

```bash
python3 -m unittest discover -s tests
```

## Signal Fields

Signals include ETF, phase, contract symbol, expiration, strike, bid, ask, mid, DTE, delta, IV, theta, open interest, volume, spread percentage, assignment/call-away probability, POP, collateral, expected monthly yield, annualized yield, adjusted cost basis, IV/HV ratio, and risk flags.

## Backtest Reports

Backtests write:

- `trades.csv`
- `monthly_income.csv`
- `equity_curve.csv`
- `summary.md`

Metrics include CAGR, max drawdown, Sharpe, Sortino, assignment rate, covered-call recovery cycles, win rate, and buy-and-hold comparison.
Backtests size CSP entries by the maximum whole number of cash-secured contracts affordable with available cash; CC entries cover all assigned 100-share lots.

## Important Limitations

- Historical option data quality matters. Sparse chains, stale quotes, and missing open interest can materially distort results.
- Yahoo Finance proxy results are not true historical option-chain backtests; they use Yahoo ETF OHLCV plus Black-Scholes/HV model prices.
- Leveraged ETFs such as `TQQQ` and `SOXL` are path-dependent daily reset products. They are reported as high-risk even when premium yield looks attractive.
- Option assignment is modeled at expiration based on closing price. Early assignment and dividends are not modeled by default.
- Taxes, broker-specific margin rules, hard-to-borrow effects, and user-specific suitability are outside the script.
