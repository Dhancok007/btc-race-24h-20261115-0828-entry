# CBBTC race bot — conservative trend follower

A Python 3.13, standard-library-only bot for the `agent-fun/bars-replay/v1`
protocol. It reads one JSON line per minute on stdin (hello, one batch per
bar, done) and answers every batch with exactly one reply carrying its
orders. No network, no wall clock, no randomness: the same input always
produces the same orders.

## Strategy

Cash is the default state. The bot buys CBBTC only when every one of these
holds at the same time, and has held for 3 consecutive bars:

- **Trend agreement** — the 20-bar EMA is above the 90-bar EMA and the close
  is above the 90-bar EMA.
- **Real separation** — the EMA gap is at least 0.05% of price, so flat,
  overlapping averages (chop) never qualify.
- **Momentum** — the 45-bar return exceeds +0.30%.
- **No buying a downtrend** — the 240-bar return is above −0.25% and the
  90-bar EMA itself is flat or rising over the last 30 bars. This is the
  filter that keeps the bot out of relief bounces inside a falling tape.
- **Fresh push** — the last 15 bars returned more than +0.50%. Slow drifts
  that top out and fade are rejected; the bot buys while buyers are
  actively pushing.
- **Volatility band** — mean absolute 1-bar return over 30 bars must sit
  between 0.02% and 0.35%: dead tape and chaos are both skipped.

One concession to the race format: if no trade has filled by bar 1100, the
fresh-push bar eases to +0.35%, so a dead-quiet window still produces one
disciplined trade instead of none.

## Risk controls

- Position size is 55% of equity; a full round trip costs roughly 25 bps
  in fees, slippage and impact, and the entry bar sits well above that.
- Hard stop-loss at −0.9% from the entry fill price.
- Trailing stop 0.7% below the peak close once the trade is in profit.
- Trend-break exit when the fast EMA crosses back below the slow EMA.
- Take-profit at +3.0%, and a maximum hold of 360 bars.
- A 45-bar cooldown after every exit prevents churn.
- Every order carries `max_slippage_bps = 200`.

## Local backtest (frozen dev window, 1,440 bars)

Replayed through a local driver implementing the documented fill model
(next-bar open, 4 bps fee, 2 bps slippage, constant-product impact):

| Window | Net PnL | Fills | Max drawdown |
| --- | --- | --- | --- |
| Full 24 h | +$3.30 | 2 (1 round trip) | 0.46% |
| First half | $0.00 (no trade — pure downtrend, stayed in cash) | 0 | 0.00% |
| Second half | +$3.30 | 2 | 0.46% |

The dev window fell −4.0% overall; the bot's only trade rode the sharp
rebound off the capitulation low and banked +0.64% on the position. Two
identical runs produce identical results.
