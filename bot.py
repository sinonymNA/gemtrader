import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pandas_ta as ta
from dotenv import load_dotenv
from fastapi import FastAPI
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest, TrailingStopOrderRequest


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("mean_reversion_3_bot")


@dataclass
class StrategyConfig:
    symbol: str = "QQQ"
    timeframe: TimeFrame = TimeFrame.Minute
    lookback_bars: int = 120
    rsi_length: int = 14
    volume_sma_length: int = 20
    std_length: int = 20
    volume_multiplier: float = 1.5
    rsi_threshold: float = 30.0
    hard_stop_pct: float = 0.005
    trailing_stop_pct: float = 0.2
    entry_allocation_pct: float = 0.20
    exit_1_pct: float = 0.70
    max_daily_loss: float = 500.0
    max_daily_profit: float = 1000.0
    trading_start_est: time = time(9, 45)
    trading_end_est: time = time(15, 30)


class MeanReversionBot:
    def __init__(self, config: StrategyConfig) -> None:
        load_dotenv()

        self.config = config
        self.est_tz = ZoneInfo("America/New_York")

        api_key = os.getenv("ALPACA_API_KEY")
        secret_key = os.getenv("ALPACA_SECRET_KEY")
        paper = os.getenv("ALPACA_PAPER", "true").lower() == "true"

        if not api_key or not secret_key:
            raise RuntimeError("Missing ALPACA_API_KEY or ALPACA_SECRET_KEY in environment/.env")

        self.trading_client = TradingClient(api_key=api_key, secret_key=secret_key, paper=paper)
        self.data_client = StockHistoricalDataClient(api_key=api_key, secret_key=secret_key)

        self.is_running = False
        self.kill_switch_active = False
        self.day_start_equity = None
        self.last_trade_day = None

    def _now_est(self) -> datetime:
        return datetime.now(tz=ZoneInfo("UTC")).astimezone(self.est_tz)

    def _in_trading_window(self, now_est: datetime) -> bool:
        t = now_est.time()
        return self.config.trading_start_est <= t <= self.config.trading_end_est

    def _refresh_day_baseline(self) -> None:
        now_est = self._now_est()
        if self.last_trade_day != now_est.date():
            account = self.trading_client.get_account()
            self.day_start_equity = float(account.equity)
            self.last_trade_day = now_est.date()
            self.kill_switch_active = False
            logger.info("New trading day initialized | day_start_equity=%.2f", self.day_start_equity)

    def _daily_pnl(self) -> float:
        account = self.trading_client.get_account()
        equity = float(account.equity)
        if self.day_start_equity is None:
            self.day_start_equity = equity
        return equity - self.day_start_equity

    def _enforce_daily_risk_controls(self) -> None:
        pnl = self._daily_pnl()

        if pnl <= -self.config.max_daily_loss or pnl >= self.config.max_daily_profit:
            if not self.kill_switch_active:
                logger.warning(
                    "Daily guardrail hit | pnl=%.2f | loss_limit=-%.2f | profit_limit=%.2f",
                    pnl,
                    self.config.max_daily_loss,
                    self.config.max_daily_profit,
                )
                self._flatten_all_positions()
            self.kill_switch_active = True

    def _flatten_all_positions(self) -> None:
        positions = self.trading_client.get_all_positions()
        for p in positions:
            qty = abs(float(p.qty))
            if qty <= 0:
                continue
            side = OrderSide.SELL if float(p.qty) > 0 else OrderSide.BUY
            order = MarketOrderRequest(
                symbol=p.symbol,
                qty=qty,
                side=side,
                time_in_force=TimeInForce.DAY,
            )
            self.trading_client.submit_order(order)
            logger.info("Flattened %s qty=%s", p.symbol, qty)

    def _fetch_bars(self) -> pd.DataFrame:
        now = datetime.now(tz=ZoneInfo("UTC"))
        start = now - timedelta(minutes=self.config.lookback_bars + 20)
        req = StockBarsRequest(
            symbol_or_symbols=self.config.symbol,
            timeframe=self.config.timeframe,
            start=start,
            end=now,
        )
        bars = self.data_client.get_stock_bars(req).df
        if bars.empty:
            return bars

        if isinstance(bars.index, pd.MultiIndex):
            bars = bars.xs(self.config.symbol)

        bars = bars.sort_index().copy()
        bars.index = bars.index.tz_convert(self.est_tz)
        return bars

    def _compute_indicators(self, bars: pd.DataFrame) -> pd.DataFrame:
        df = bars.copy()
        if df.empty:
            return df

        typical_price = (df["high"] + df["low"] + df["close"]) / 3.0
        cumulative_pv = (typical_price * df["volume"]).cumsum()
        cumulative_vol = df["volume"].cumsum().replace(0, pd.NA)
        df["vwap"] = cumulative_pv / cumulative_vol

        df["std"] = df["close"].rolling(self.config.std_length).std()
        df["lower_3std"] = df["vwap"] - 3.0 * df["std"]
        df["vol_sma20"] = df["volume"].rolling(self.config.volume_sma_length).mean()
        df["rsi"] = ta.rsi(df["close"], length=self.config.rsi_length)

        return df.dropna()

    def _has_position(self) -> bool:
        try:
            position = self.trading_client.get_open_position(self.config.symbol)
            return float(position.qty) != 0
        except Exception:
            return False

    def _submit_entry(self, close_price: float) -> None:
        account = self.trading_client.get_account()
        buying_power = float(account.buying_power)
        notional = buying_power * self.config.entry_allocation_pct
        qty = max(1, int(notional / close_price))

        order = MarketOrderRequest(
            symbol=self.config.symbol,
            qty=qty,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
        )
        self.trading_client.submit_order(order)
        logger.info("Entry BUY submitted | symbol=%s qty=%s", self.config.symbol, qty)

    def _manage_open_position(self, latest_row: pd.Series) -> None:
        try:
            pos = self.trading_client.get_open_position(self.config.symbol)
        except Exception:
            return

        qty = float(pos.qty)
        if qty <= 0:
            return

        current_price = float(latest_row["close"])
        vwap = float(latest_row["vwap"])
        entry_price = float(pos.avg_entry_price)
        stop_price = round(entry_price * (1.0 - self.config.hard_stop_pct), 2)

        # Hard stop loss if price breaches 0.5% below entry.
        if current_price <= stop_price:
            sell_all = MarketOrderRequest(
                symbol=self.config.symbol,
                qty=qty,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
            )
            self.trading_client.submit_order(sell_all)
            logger.info("Hard stop triggered | qty=%s | stop=%.2f", qty, stop_price)
            return

        # Exit 1: sell 70% when price reverts to VWAP.
        if current_price >= vwap:
            qty_exit_1 = max(1, int(qty * self.config.exit_1_pct))
            partial_sell = MarketOrderRequest(
                symbol=self.config.symbol,
                qty=qty_exit_1,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
            )
            self.trading_client.submit_order(partial_sell)
            logger.info("Exit 1 hit VWAP | sold=%s", qty_exit_1)

            remaining_qty = max(1, int(qty - qty_exit_1))

            # Exit 2: trailing stop for remaining 30%.
            trailing_order = TrailingStopOrderRequest(
                symbol=self.config.symbol,
                qty=remaining_qty,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
                trail_percent=self.config.trailing_stop_pct,
            )
            self.trading_client.submit_order(trailing_order)
            logger.info("Exit 2 trailing stop submitted | qty=%s trail=%.3f%%", remaining_qty, self.config.trailing_stop_pct)

    def _entry_signal(self, df: pd.DataFrame) -> bool:
        if len(df) < 2:
            return False

        prev_row = df.iloc[-2]
        row = df.iloc[-1]

        crossed_below = prev_row["close"] >= prev_row["lower_3std"] and row["close"] < row["lower_3std"]
        vol_spike = row["volume"] > (self.config.volume_multiplier * row["vol_sma20"])
        rsi_oversold = row["rsi"] < self.config.rsi_threshold

        logger.info(
            "Signal check | crossed_below=%s vol_spike=%s rsi_oversold=%s close=%.2f lower_3std=%.2f rsi=%.2f",
            crossed_below,
            vol_spike,
            rsi_oversold,
            row["close"],
            row["lower_3std"],
            row["rsi"],
        )
        return bool(crossed_below and vol_spike and rsi_oversold)

    def run_once(self) -> None:
        self._refresh_day_baseline()
        self._enforce_daily_risk_controls()

        if self.kill_switch_active:
            logger.warning("Kill switch active - trading disabled.")
            return

        df = self._compute_indicators(self._fetch_bars())
        if df.empty:
            logger.warning("No bars/indicators available this cycle.")
            return

        latest = df.iloc[-1]

        if self._has_position():
            self._manage_open_position(latest)
            return

        now_est = self._now_est()
        if self._in_trading_window(now_est) and self._entry_signal(df):
            self._submit_entry(float(latest["close"]))

    def status(self) -> dict:
        account = self.trading_client.get_account()
        return {
            "current_balance": float(account.equity),
            "unrealized_pnl": float(account.unrealized_pl),
            "is_running": self.is_running and not self.kill_switch_active,
        }

    async def run_loop(self) -> None:
        self.is_running = True
        logger.info("Mean Reversion 3.0 bot started.")
        while self.is_running:
            try:
                self.run_once()
            except Exception as exc:
                logger.exception("Bot cycle failed: %s", exc)
            await asyncio.sleep(60)

    def stop(self) -> None:
        self.is_running = False


app = FastAPI(title="Mean Reversion 3.0 Bot")
bot = MeanReversionBot(StrategyConfig())


@app.on_event("startup")
async def startup_event() -> None:
    asyncio.create_task(bot.run_loop())


@app.on_event("shutdown")
async def shutdown_event() -> None:
    bot.stop()


@app.get("/status")
def get_status() -> dict:
    return bot.status()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
