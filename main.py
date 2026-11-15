"""Conservative trend-following bot for the agent-fun/bars-replay/v1 replay.

Strategy in one paragraph: stay in cash by default. Buy CBBTC only when a
confirmed uptrend is in force -- the fast EMA above the slow EMA, the close
above the slow EMA, positive medium-term momentum, a real gap between the
EMAs (so sideways chop is filtered out) and realised volatility inside a
sane band -- and only after that picture has held for several consecutive
bars. Positions are a fixed fraction of equity, protected by a hard
stop-loss, a trailing stop that locks in gains, a trend-break exit and a
maximum holding time. After any exit the bot sits out a cooldown so it
cannot churn in and out of noise. A round trip costs roughly 25 bps in
fees, slippage and impact, so the entry bar is set well above that. One
concession to the race format: if no trade has filled late into the
window, the fresh-push bar eases slightly, so a dead-quiet window still
sees the bot take one disciplined trade instead of none.

The bot speaks newline-delimited JSON: hello once, one batch per bar, then
done; it answers every batch with exactly one reply echoing its seq. It
uses only the standard library, reads no clock, no files and no
environment, and keeps no randomness: the same input always produces the
same orders.
"""

import json
import sys

SYMBOL = "CBBTC"

# --- strategy parameters (minutes are 1-minute bars) ---
EMA_FAST = 20            # fast trend average
EMA_SLOW = 90            # slow trend average
MOM_LOOKBACK = 45        # momentum lookback, bars
MOM_MIN = 0.0030         # momentum over the lookback must exceed +0.30%
LONG_MOM_LOOKBACK = 240  # long-horizon momentum lookback, bars
LONG_MOM_MIN = -0.0025   # 4h return must be above -0.25%: no buying a downtrend
PUSH_LOOKBACK = 15       # fresh-impulse lookback, bars
PUSH_MIN = 0.0050        # last-15-bar return must exceed +0.50%: buy the push,
                         # not the slow drift that tops out and fades
PUSH_MIN_LATE = 0.0035   # relaxed push bar used only by the late fallback
FALLBACK_BAR = 1100      # if no trade has filled by this bar, the entry bar
                         # eases (PUSH_MIN_LATE): a window with no qualifying
                         # push at all would otherwise end with zero trades
SLOW_RISE_LOOKBACK = 30  # the slow EMA must be flat or rising over this span
TREND_GAP_MIN = 0.0005   # (ema_fast - ema_slow) / price must exceed 0.05%
VOL_LOOKBACK = 30        # realised-volatility lookback, bars
VOL_MIN = 0.0002         # mean |1-bar return| floor: below this the tape is dead
VOL_MAX = 0.0035         # mean |1-bar return| cap: above this the tape is chaos
CONFIRM_BARS = 3         # entry signal must hold this many batches in a row
SIZE_FRAC = 0.55         # fraction of equity spent on one position
STOP_LOSS = 0.009        # hard stop: exit at -0.9% from the entry price
TRAIL = 0.007            # trailing stop: exit 0.7% below the peak close
TAKE_PROFIT = 0.030      # hard cap: bank a +3.0% move outright
MAX_HOLD = 360           # never hold longer than 6 hours
COOLDOWN = 45            # bars in cash after an exit before re-entering
MIN_HISTORY = 260        # bars of history required before the first entry
SLIPPAGE_BPS = 200       # max_slippage_bps sent with every order


class Bot:
    def __init__(self, hello):
        self.hello = hello
        self.price_scale = 10 ** int(hello["symbols"][0]["price_scale"])
        self.warmup_bars = int(hello.get("warmup_bars", 0))
        self.min_notional = int(hello["fees"]["min_notional_quote_units"])
        self.closes = []            # float closes, dollars
        self.ema_fast = None
        self.ema_slow = None
        self.ema_slow_hist = []     # slow EMA value per bar, for the rise check
        self.confirm = 0            # consecutive batches the entry signal held
        self.cooldown = 0           # bars of forced cash remaining
        self.entry_price = None     # float dollars, while a position is open
        self.peak = None            # highest close since entry
        self.bars_held = 0
        self.was_holding = False
        self.traded = False         # has any buy filled yet this window?
        self.seq = -1               # seq of the batch being answered

    # -- indicators ------------------------------------------------------
    def _push_close(self, price):
        self.closes.append(price)
        kf = 2.0 / (EMA_FAST + 1)
        ks = 2.0 / (EMA_SLOW + 1)
        if self.ema_fast is None:
            self.ema_fast = price
            self.ema_slow = price
        else:
            self.ema_fast += kf * (price - self.ema_fast)
            self.ema_slow += ks * (price - self.ema_slow)
        self.ema_slow_hist.append(self.ema_slow)

    def _momentum(self):
        if len(self.closes) <= MOM_LOOKBACK:
            return None
        base = self.closes[-1 - MOM_LOOKBACK]
        if base <= 0:
            return None
        return self.closes[-1] / base - 1.0

    def _volatility(self):
        if len(self.closes) <= VOL_LOOKBACK:
            return None
        window = self.closes[-1 - VOL_LOOKBACK:]
        total = 0.0
        for i in range(1, len(window)):
            if window[i - 1] > 0:
                total += abs(window[i] / window[i - 1] - 1.0)
        return total / VOL_LOOKBACK

    def _entry_signal(self):
        if len(self.closes) < MIN_HISTORY:
            return False
        price = self.closes[-1]
        if self.ema_fast is None or self.ema_slow is None:
            return False
        if self.ema_fast <= self.ema_slow or price <= self.ema_slow:
            return False
        if (self.ema_fast - self.ema_slow) / price < TREND_GAP_MIN:
            return False
        mom = self._momentum()
        if mom is None or mom < MOM_MIN:
            return False
        # Longer-horizon guards: a short bounce inside a falling tape is a
        # trap, so the 4-hour return must not be deeply negative and the
        # slow average itself must have stopped falling.
        if len(self.closes) <= LONG_MOM_LOOKBACK:
            return False
        base = self.closes[-1 - LONG_MOM_LOOKBACK]
        if base <= 0 or self.closes[-1] / base - 1.0 < LONG_MOM_MIN:
            return False
        push_min = PUSH_MIN
        if not self.traded and self.seq >= FALLBACK_BAR:
            push_min = PUSH_MIN_LATE
        push_base = self.closes[-1 - PUSH_LOOKBACK]
        if push_base <= 0 or self.closes[-1] / push_base - 1.0 < push_min:
            return False
        if len(self.ema_slow_hist) <= SLOW_RISE_LOOKBACK:
            return False
        if self.ema_slow < self.ema_slow_hist[-1 - SLOW_RISE_LOOKBACK] * 0.9998:
            return False
        vol = self._volatility()
        if vol is None or vol < VOL_MIN or vol > VOL_MAX:
            return False
        return True

    # -- position bookkeeping --------------------------------------------
    def _sync_position(self, batch):
        """Track entry price / peak / holding time from fills and account."""
        holding = None
        for pos in batch["account"]["positions"]:
            if pos["symbol"] == SYMBOL:
                holding = pos
        for fill in batch["fills"]:
            if fill["symbol"] != SYMBOL or fill["status"] != "filled":
                continue
            if fill["side"] == "buy":
                self.traded = True
                self.entry_price = int(fill["price"]) / self.price_scale
                self.peak = self.entry_price
                self.bars_held = 0
            elif fill["side"] == "sell":
                self.entry_price = None
                self.peak = None
                self.bars_held = 0
        if holding is not None and self.entry_price is None:
            # A position we hold without a recorded fill (e.g. a restart):
            # fall back to its average cost as the entry price.
            qty = int(holding["qty"])
            if qty > 0:
                self.entry_price = int(holding["cost_quote_units"]) * 100.0 / qty
                self.peak = self.entry_price
                self.bars_held = 0
        now_holding = holding is not None and int(holding["qty"]) > 0
        if self.was_holding and not now_holding and self.cooldown == 0:
            self.cooldown = COOLDOWN
        self.was_holding = now_holding
        return holding

    # -- decision ----------------------------------------------------------
    def on_batch(self, state_seq, batch):
        self.seq = state_seq
        bar = None
        for b in batch["bars"]:
            if b["symbol"] == SYMBOL:
                bar = b
        if bar is not None:
            self._push_close(int(bar["c"]) / self.price_scale)
        holding = self._sync_position(batch)
        if self.cooldown > 0:
            self.cooldown -= 1
        if bar is None:
            return []
        price = self.closes[-1]
        if holding is not None and self.entry_price is not None:
            self.bars_held += 1
            if price > self.peak:
                self.peak = price
        if state_seq < self.warmup_bars:
            return []

        if holding is not None:
            return self._exit_orders(holding, price, state_seq)
        # Flat: look for a confirmed entry.
        if self.cooldown > 0:
            self.confirm = 0
            return []
        if self._entry_signal():
            self.confirm += 1
        else:
            self.confirm = 0
        if self.confirm < CONFIRM_BARS:
            return []
        equity = int(batch["account"]["equity_quote_units"])
        cash = int(batch["account"]["cash_quote_units"])
        notional = int(equity * SIZE_FRAC)
        if notional > cash:
            notional = cash
        if notional < self.min_notional:
            return []
        self.confirm = 0
        return [{
            "side": "buy",
            "client_id": "buy-%d" % state_seq,
            "symbol": SYMBOL,
            "notional_quote_units": str(notional),
            "max_slippage_bps": SLIPPAGE_BPS,
        }]

    def _exit_orders(self, holding, price, seq):
        entry = self.entry_price
        qty = int(holding["qty"])
        reason = None
        if entry is not None:
            if price <= entry * (1.0 - STOP_LOSS):
                reason = "stop"
            elif price >= entry * (1.0 + TAKE_PROFIT):
                reason = "target"
            elif self.peak is not None and price > entry and price <= self.peak * (1.0 - TRAIL):
                reason = "trail"
            elif self.ema_fast is not None and self.ema_slow is not None and self.ema_fast < self.ema_slow:
                reason = "trend"
            elif self.bars_held >= MAX_HOLD:
                reason = "time"
        if reason is None:
            return []
        return [{
            "side": "sell",
            "client_id": "sell-%s-%d" % (reason, seq),
            "symbol": SYMBOL,
            "qty": str(qty),
            "max_slippage_bps": SLIPPAGE_BPS,
        }]


def main():
    bot = None
    out = sys.stdout
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        kind = msg.get("type")
        if kind == "hello":
            bot = Bot(msg)
        elif kind == "batch":
            orders = bot.on_batch(int(msg["seq"]), msg)
            reply = {"type": "reply", "seq": msg["seq"], "orders": orders}
            out.write(json.dumps(reply, separators=(",", ":")) + "\n")
            out.flush()
        elif kind == "done":
            break


if __name__ == "__main__":
    main()
