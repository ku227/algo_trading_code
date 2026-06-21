from AlgorithmImports import *
from datetime import timedelta
from math import sqrt

class SpreadCostFeeModel(FeeModel):
    """
    Reduced-form bid-ask spread model.

    QuantConnect daily ETF backtests do not provide a full intraday order book in this
    strategy configuration, so this model treats spread crossing as an additional
    transaction cost charged on order notional. The parameter is a one-way effective
    spread/slippage cost in basis points.
    """

    def __init__(self, one_way_spread_bps: float):
        self.one_way_spread_bps = float(one_way_spread_bps)
        self.one_way_spread_rate = self.one_way_spread_bps / 10000.0

    def GetOrderFee(self, parameters):
        security = parameters.Security
        order = parameters.Order

        price = float(security.Price)
        if price <= 0:
            return OrderFee(CashAmount(0, "USD"))

        order_notional = abs(float(order.Quantity)) * price
        spread_cost = order_notional * self.one_way_spread_rate
        return OrderFee(CashAmount(spread_cost, "USD"))

class GovernedEtfMomentumRotation(QCAlgorithm):
    """
    V3 Governed Multi-Asset ETF Momentum Rotation

    This version preserves the V1/V2 core alpha logic:
    - Fixed ETF universe: SPY, QQQ, TLT, GLD, SHY
    - 126-trading-day momentum ranking for SPY, QQQ, TLT, and GLD
    - Monthly rebalance
    - Long-only, no leverage, no shorting
    - Missing/incomplete history excluded from ranking

    V3 adds active risk-management controls that were limitations in V1/V2:
    - Portfolio-level hard drawdown stop with cooldown
    - Portfolio-level soft drawdown exposure reduction
    - Realized-volatility targeting using trailing portfolio returns
    - Daily risk override between monthly rebalances
    - Asset-level entry stop-loss and trailing stop-loss
    - Conditional defensive sleeve: SHY receives residual weight only when it has positive momentum;
      otherwise residual risk budget remains in cash

    V2 execution realism is retained:
    - Custom spread-cost fee model
    - 2% buying-power buffer
    - Per-order notional cap as percentage of portfolio value
    - Per-order notional cap as percentage of estimated ADV
    - Minimum trade-value filter
    - Audit logs for capped, skipped, submitted, and risk-override orders
    """

    def Initialize(self):
        self.SetStartDate(2007, 1, 1)
        self.SetEndDate(2025, 12, 31)
        self.SetCash(100000)

        self.lookback_days = 126
        self.max_risk_assets = 3

        self.buying_power_buffer = 0.02
        self.base_target_gross_exposure = 1.0 - self.buying_power_buffer
        self.max_gross_exposure = self.base_target_gross_exposure

        self.soft_drawdown_threshold = -0.10          # reduce exposure after 10% drawdown
        self.hard_drawdown_stop = -0.15               # flatten portfolio after 15% drawdown
        self.soft_drawdown_exposure_scale = 0.50      # halve risk budget in soft drawdown state
        self.portfolio_stop_cooldown_days = 21        # calendar-day cooldown before re-entry
        self.portfolio_stop_until = None

        self.realized_vol_lookback_days = 63
        self.target_realized_vol = 0.10               # 10% annualized realized-vol target
        self.minimum_vol_scale = 0.25                 # vol targeting can reduce, but not zero, exposure by itself
        self.daily_returns = []
        self.previous_portfolio_value = None

        self.gross_exposure_tolerance = 0.02

        self.entry_stop_loss_pct = -0.12              # stop if current price is 12% below average entry
        self.trailing_stop_loss_pct = -0.15           # stop if current price is 15% below position peak
        self.asset_stop_cooldown_days = 21
        self.stop_blocked_until = {}
        self.position_peak_prices = {}

        self.max_order_pct_of_portfolio = 0.20        # no single order above 20% of portfolio value
        self.adv_lookback_days = 20                  # ADV lookback for liquidity estimate
        self.max_order_pct_of_adv = 0.01             # no single order above 1% of estimated ADV
        self.minimum_trade_value = 250.0             # ignore tiny rebalance differences

        self.spread_bps_by_ticker = {
            "SPY": 1.0,
            "QQQ": 1.0,
            "TLT": 2.0,
            "GLD": 2.0,
            "SHY": 1.0,
        }

        risk_tickers = ["SPY", "QQQ", "TLT", "GLD"]
        defensive_ticker = "SHY"

        self.symbols_by_ticker = {}

        for ticker in risk_tickers + [defensive_ticker]:
            security = self.AddEquity(ticker, Resolution.Daily)
            security.SetLeverage(1.0)
            security.SetDataNormalizationMode(DataNormalizationMode.Adjusted)
            security.SetFeeModel(SpreadCostFeeModel(self.spread_bps_by_ticker.get(ticker, 2.0)))
            self.symbols_by_ticker[ticker] = security.Symbol

        self.risk_symbols = [self.symbols_by_ticker[ticker] for ticker in risk_tickers]
        self.defensive_symbol = self.symbols_by_ticker[defensive_ticker]
        self.all_symbols = self.risk_symbols + [self.defensive_symbol]

        self.spy = self.symbols_by_ticker["SPY"]
        self.SetBenchmark(self.spy)

        self.SetWarmUp(max(self.lookback_days + 5, self.realized_vol_lookback_days + 5), Resolution.Daily)

        self.Schedule.On(
            self.DateRules.MonthStart(self.spy),
            self.TimeRules.AfterMarketOpen(self.spy, 30),
            self.Rebalance
        )

        self.Schedule.On(
            self.DateRules.EveryDay(self.spy),
            self.TimeRules.BeforeMarketClose(self.spy, 5),
            self.DailyRiskManagement
        )

        self.portfolio_high_water_mark = 0
        self.Schedule.On(
            self.DateRules.EveryDay(self.spy),
            self.TimeRules.BeforeMarketClose(self.spy, 1),
            self.RecordDailyDiagnostics
        )

        self.Debug(
            f"Initialized V3 risk-managed ETF momentum rotation. "
            f"Lookback={self.lookback_days}, RiskAssets={risk_tickers}, Defensive={defensive_ticker}, "
            f"BaseTargetGross={self.base_target_gross_exposure:.2%}, CashBuffer={self.buying_power_buffer:.2%}, "
            f"VolTarget={self.target_realized_vol:.2%}, SoftDD={self.soft_drawdown_threshold:.2%}, "
            f"HardDD={self.hard_drawdown_stop:.2%}, EntryStop={self.entry_stop_loss_pct:.2%}, "
            f"TrailingStop={self.trailing_stop_loss_pct:.2%}, MaxOrderPctPortfolio={self.max_order_pct_of_portfolio:.2%}, "
            f"MaxOrderPctADV={self.max_order_pct_of_adv:.2%}"
        )

    def OnData(self, data: Slice):
        """
        Alpha trading is intentionally scheduled monthly in Rebalance().
        Daily risk actions are handled by DailyRiskManagement().
        """
        pass

    def Rebalance(self):
        """
        Monthly alpha rebalance with V3 risk overlays:
        1. Calculate trailing momentum for risk assets.
        2. Exclude assets with missing history or active stop-loss cooldowns.
        3. Select up to top 3 positive-momentum risk assets.
        4. Determine active risk budget from drawdown and realized-volatility controls.
        5. Allocate equal risk weights to selected assets using the active risk budget.
        6. Allocate residual to SHY only if SHY has positive momentum and is not blocked.
        7. Leave residual in cash when the defensive sleeve is not attractive.
        8. Execute via V2 constrained order sizing.
        """

        if self.IsWarmingUp:
            self.Debug(f"{self.Time.date()} | Rebalance skipped: algorithm is warming up.")
            return

        momentum_scores = {}
        for symbol in self.risk_symbols:
            if self.IsSymbolStopBlocked(symbol):
                self.Debug(
                    f"{self.Time.date()} | {symbol.Value} excluded: active stop-loss cooldown until "
                    f"{self.stop_blocked_until.get(symbol).date()}."
                )
                continue

            momentum = self.CalculateMomentum(symbol)
            if momentum is None:
                self.Debug(
                    f"{self.Time.date()} | WARNING: {symbol.Value} excluded from ranking "
                    f"because required history is missing or incomplete."
                )
                continue
            momentum_scores[symbol] = momentum

        positive_momentum_assets = [
            symbol for symbol, momentum in momentum_scores.items()
            if momentum > 0
        ]

        selected_symbols = sorted(
            positive_momentum_assets,
            key=lambda symbol: momentum_scores[symbol],
            reverse=True
        )[:self.max_risk_assets]

        active_target_gross = self.GetActiveTargetGrossExposure()
        target_weights = {symbol: 0.0 for symbol in self.all_symbols}

        if active_target_gross > 0 and len(selected_symbols) > 0:
            risk_asset_weight = active_target_gross / self.max_risk_assets
            for symbol in selected_symbols:
                target_weights[symbol] = risk_asset_weight

        used_risk_weight = sum(target_weights[symbol] for symbol in self.risk_symbols)
        residual_weight = max(0.0, active_target_gross - used_risk_weight)

        defensive_momentum = self.CalculateMomentum(self.defensive_symbol)
        defensive_eligible = (
            residual_weight > 0 and
            defensive_momentum is not None and
            defensive_momentum > 0 and
            not self.IsSymbolStopBlocked(self.defensive_symbol)
        )

        if defensive_eligible:
            target_weights[self.defensive_symbol] = residual_weight
        else:
            target_weights[self.defensive_symbol] = 0.0

        gross_exposure = sum(abs(weight) for weight in target_weights.values())
        if gross_exposure > self.max_gross_exposure + 0.0001:
            self.Error(
                f"{self.Time.date()} | Gross exposure check failed: "
                f"{gross_exposure:.4f}. Rebalance skipped."
            )
            return

        for symbol, weight in target_weights.items():
            if weight < -0.0001:
                self.Error(
                    f"{self.Time.date()} | Short target detected for {symbol.Value}: "
                    f"{weight:.4f}. Rebalance skipped."
                )
                return

        momentum_log = ", ".join(
            f"{symbol.Value}: {momentum_scores[symbol]:.2%}"
            for symbol in sorted(momentum_scores.keys(), key=lambda s: s.Value)
        )
        selected_log = ", ".join(symbol.Value for symbol in selected_symbols) if selected_symbols else "None"
        weights_log = ", ".join(f"{symbol.Value}: {target_weights[symbol]:.2%}" for symbol in self.all_symbols)
        defensive_text = (
            f"SHY momentum={defensive_momentum:.2%}" if defensive_momentum is not None else "SHY momentum unavailable"
        )

        self.Debug(
            f"{self.Time.date()} | V3 Rebalance | Momentum: [{momentum_log}] | "
            f"Selected: [{selected_log}] | ActiveGross={active_target_gross:.2%} | "
            f"Residual={residual_weight:.2%} | DefensiveEligible={defensive_eligible} ({defensive_text}) | "
            f"Targets: [{weights_log}]"
        )

        self.ExecuteConstrainedRebalance(target_weights)

        for symbol, weight in target_weights.items():
            self.Plot("Target Weights", symbol.Value, weight)

    def DailyRiskManagement(self):
        """
        Active daily risk-management layer:
        - Applies portfolio hard drawdown stop.
        - Applies asset-level entry and trailing stops.
        - Scales down current holdings if realized volatility or soft drawdown reduces the allowed gross exposure.
        """

        if self.IsWarmingUp:
            return

        portfolio_value = float(self.Portfolio.TotalPortfolioValue)
        if portfolio_value <= 0:
            return

        self.UpdatePortfolioHighWaterMark(portfolio_value)
        drawdown = self.GetCurrentDrawdown(portfolio_value)

        if drawdown <= self.hard_drawdown_stop and not self.IsPortfolioStopActive():
            self.portfolio_stop_until = self.Time + timedelta(days=self.portfolio_stop_cooldown_days)
            self.Debug(
                f"{self.Time.date()} | PORTFOLIO HARD DRAWDOWN STOP TRIGGERED. "
                f"Drawdown={drawdown:.2%}, StopUntil={self.portfolio_stop_until.date()}. Flattening all positions."
            )
            self.FlattenAllPositions("Portfolio hard drawdown stop")
            return

        self.ApplyAssetStopLosses()

        allowed_gross = self.GetActiveTargetGrossExposure()
        current_gross = self.GetCurrentGrossExposure()

        if current_gross > allowed_gross + self.gross_exposure_tolerance:
            self.ReduceGrossExposure(
                allowed_gross=allowed_gross,
                reason=(
                    f"Daily risk override | CurrentGross={current_gross:.2%}, "
                    f"AllowedGross={allowed_gross:.2%}, Drawdown={drawdown:.2%}, "
                    f"RealizedVol={self.GetRealizedVolatilityText()}"
                )
            )

    def GetActiveTargetGrossExposure(self) -> float:
        """
        Combines the base 98% gross target with:
        - portfolio stop state,
        - soft drawdown exposure scaling,
        - realized-volatility targeting.
        """

        if self.IsPortfolioStopActive():
            return 0.0

        portfolio_value = float(self.Portfolio.TotalPortfolioValue)
        drawdown = self.GetCurrentDrawdown(portfolio_value)

        if drawdown <= self.hard_drawdown_stop:
            self.portfolio_stop_until = self.Time + timedelta(days=self.portfolio_stop_cooldown_days)
            return 0.0

        drawdown_scale = 1.0
        if drawdown <= self.soft_drawdown_threshold:
            drawdown_scale = self.soft_drawdown_exposure_scale

        vol_scale = self.GetVolatilityScale()
        combined_scale = min(drawdown_scale, vol_scale)
        active_gross = self.base_target_gross_exposure * combined_scale

        active_gross = max(0.0, min(self.base_target_gross_exposure, active_gross))
        return active_gross

    def GetVolatilityScale(self) -> float:
        """
        Realized-volatility target.
        If realized volatility exceeds target, gross exposure is scaled down by target/realized_vol.
        """

        realized_vol = self.GetRealizedVolatility()
        if realized_vol is None or realized_vol <= 0:
            return 1.0

        if realized_vol <= self.target_realized_vol:
            return 1.0

        scale = self.target_realized_vol / realized_vol
        return max(self.minimum_vol_scale, min(1.0, scale))

    def GetRealizedVolatility(self):
        if len(self.daily_returns) < max(2, self.realized_vol_lookback_days):
            return None

        window = self.daily_returns[-self.realized_vol_lookback_days:]
        mean_return = sum(window) / len(window)
        variance = sum((r - mean_return) ** 2 for r in window) / max(1, len(window) - 1)
        daily_vol = sqrt(max(0.0, variance))
        return daily_vol * sqrt(252.0)

    def GetRealizedVolatilityText(self) -> str:
        realized_vol = self.GetRealizedVolatility()
        return "n/a" if realized_vol is None else f"{realized_vol:.2%}"

    def IsPortfolioStopActive(self) -> bool:
        return self.portfolio_stop_until is not None and self.Time < self.portfolio_stop_until

    def ApplyAssetStopLosses(self):
        """
        Applies asset-level entry and trailing stop-losses.
        Stops liquidate the asset and block re-entry for a cooldown period.
        """

        for symbol in self.all_symbols:
            holding = self.Portfolio[symbol]
            if not holding.Invested:
                self.position_peak_prices.pop(symbol, None)
                continue

            if symbol not in self.Securities:
                continue

            price = float(self.Securities[symbol].Price)
            if price <= 0:
                continue

            previous_peak = self.position_peak_prices.get(symbol, price)
            peak_price = max(previous_peak, price)
            self.position_peak_prices[symbol] = peak_price

            average_price = float(holding.AveragePrice)
            entry_return = price / average_price - 1.0 if average_price > 0 else 0.0
            trailing_return = price / peak_price - 1.0 if peak_price > 0 else 0.0

            entry_stop_hit = entry_return <= self.entry_stop_loss_pct
            trailing_stop_hit = trailing_return <= self.trailing_stop_loss_pct

            if entry_stop_hit or trailing_stop_hit:
                self.stop_blocked_until[symbol] = self.Time + timedelta(days=self.asset_stop_cooldown_days)
                reason = "entry stop" if entry_stop_hit else "trailing stop"
                self.Debug(
                    f"{self.Time.date()} | ASSET STOP-LOSS TRIGGERED: {symbol.Value} | "
                    f"Reason={reason}, Price={price:.2f}, AvgPrice={average_price:.2f}, Peak={peak_price:.2f}, "
                    f"EntryReturn={entry_return:.2%}, TrailingReturn={trailing_return:.2%}, "
                    f"BlockedUntil={self.stop_blocked_until[symbol].date()}."
                )
                self.Liquidate(symbol, f"V3 asset stop-loss: {reason}")
                self.position_peak_prices.pop(symbol, None)

    def IsSymbolStopBlocked(self, symbol: Symbol) -> bool:
        blocked_until = self.stop_blocked_until.get(symbol)
        if blocked_until is None:
            return False

        if self.Time < blocked_until:
            return True

        self.stop_blocked_until.pop(symbol, None)
        return False

    def UpdatePortfolioHighWaterMark(self, portfolio_value: float):
        if self.portfolio_high_water_mark == 0:
            self.portfolio_high_water_mark = portfolio_value
        self.portfolio_high_water_mark = max(self.portfolio_high_water_mark, portfolio_value)

    def GetCurrentDrawdown(self, portfolio_value: float = None) -> float:
        if portfolio_value is None:
            portfolio_value = float(self.Portfolio.TotalPortfolioValue)

        if portfolio_value <= 0:
            return 0.0

        if self.portfolio_high_water_mark == 0:
            self.portfolio_high_water_mark = portfolio_value

        return portfolio_value / self.portfolio_high_water_mark - 1.0

    def GetCurrentGrossExposure(self) -> float:
        portfolio_value = float(self.Portfolio.TotalPortfolioValue)
        if portfolio_value <= 0:
            return 0.0

        gross_value = 0.0
        for symbol in self.all_symbols:
            gross_value += abs(float(self.Portfolio[symbol].HoldingsValue))

        return gross_value / portfolio_value

    def FlattenAllPositions(self, reason: str):
        for symbol in self.all_symbols:
            if self.Portfolio[symbol].Invested:
                self.Liquidate(symbol, f"V3 risk flatten: {reason}")

    def ReduceGrossExposure(self, allowed_gross: float, reason: str):
        """
        Reduces current holdings proportionally when current gross exposure exceeds the
        allowed risk budget. This is a risk override, so sell orders are not blocked by
        the ADV/order-size caps used for ordinary rebalances.
        """

        current_gross = self.GetCurrentGrossExposure()
        if current_gross <= 0:
            return

        if allowed_gross <= 0:
            self.Debug(f"{self.Time.date()} | {reason}. Allowed gross is zero; flattening all positions.")
            self.FlattenAllPositions(reason)
            return

        scale = max(0.0, min(1.0, allowed_gross / current_gross))
        portfolio_value = float(self.Portfolio.TotalPortfolioValue)

        self.Debug(f"{self.Time.date()} | {reason}. Scaling holdings by {scale:.2%}.")

        for symbol in self.all_symbols:
            holding_value = float(self.Portfolio[symbol].HoldingsValue)
            if abs(holding_value) <= 0:
                continue

            price = float(self.Securities[symbol].Price)
            if price <= 0:
                continue

            target_value = holding_value * scale
            delta_value = target_value - holding_value

            if abs(delta_value) < self.minimum_trade_value:
                continue

            quantity = int(delta_value / price)
            if quantity == 0:
                continue

            if quantity > 0:
                continue

            self.MarketOrder(symbol, quantity, False, f"V3 daily risk override | {reason}")
            self.Debug(
                f"{self.Time.date()} | RISK OVERRIDE ORDER: {symbol.Value} | Qty={quantity} | "
                f"EstNotional=${abs(quantity) * price:,.2f} | PortfolioValue=${portfolio_value:,.2f}"
            )

    def ExecuteConstrainedRebalance(self, target_weights):
        portfolio_value = float(self.Portfolio.TotalPortfolioValue)
        if portfolio_value <= 0:
            self.Debug(f"{self.Time.date()} | Execution skipped: invalid portfolio value.")
            return

        planned_orders = []

        for symbol in self.all_symbols:
            if symbol not in self.Securities:
                self.Debug(f"{self.Time.date()} | WARNING: {symbol.Value} missing from Securities collection.")
                continue

            security = self.Securities[symbol]
            price = float(security.Price)
            if price <= 0:
                self.Debug(f"{self.Time.date()} | WARNING: {symbol.Value} skipped: invalid current price {price}.")
                continue

            current_value = float(self.Portfolio[symbol].HoldingsValue)
            target_value = float(target_weights[symbol]) * portfolio_value
            delta_value = target_value - current_value

            if abs(delta_value) < self.minimum_trade_value:
                self.Debug(
                    f"{self.Time.date()} | {symbol.Value} trade skipped: delta value "
                    f"${delta_value:,.2f} below minimum ${self.minimum_trade_value:,.2f}."
                )
                continue

            requested_quantity = int(delta_value / price)
            if requested_quantity == 0:
                continue

            capped_quantity = self.ApplyOrderSizeLimits(symbol, requested_quantity, price, portfolio_value)
            if capped_quantity == 0:
                self.Debug(
                    f"{self.Time.date()} | {symbol.Value} order blocked by execution constraints. "
                    f"RequestedQty={requested_quantity}."
                )
                continue

            planned_orders.append((symbol, capped_quantity, requested_quantity, price))

        planned_orders.sort(key=lambda item: item[1])

        for symbol, quantity, requested_quantity, price in planned_orders:
            if quantity > 0:
                quantity = self.ApplyBuyingPowerBuffer(symbol, quantity, price, portfolio_value)
                if quantity == 0:
                    self.Debug(f"{self.Time.date()} | {symbol.Value} buy order blocked by buying-power buffer.")
                    continue

            tag = f"V3 constrained rebalance | RequestedQty={requested_quantity} | SubmittedQty={quantity}"
            self.MarketOrder(symbol, quantity, False, tag)
            self.Debug(
                f"{self.Time.date()} | ORDER: {symbol.Value} | RequestedQty={requested_quantity} | "
                f"SubmittedQty={quantity} | EstNotional=${abs(quantity) * price:,.2f}"
            )

    def ApplyOrderSizeLimits(self, symbol: Symbol, requested_quantity: int, price: float, portfolio_value: float) -> int:
        requested_notional = abs(requested_quantity) * price
        portfolio_notional_limit = portfolio_value * self.max_order_pct_of_portfolio

        average_dollar_volume = self.EstimateAverageDollarVolume(symbol)
        if average_dollar_volume is None or average_dollar_volume <= 0:
            adv_notional_limit = portfolio_notional_limit
            self.Debug(
                f"{self.Time.date()} | WARNING: {symbol.Value} ADV unavailable; using portfolio notional limit only."
            )
        else:
            adv_notional_limit = average_dollar_volume * self.max_order_pct_of_adv

        max_allowed_notional = min(portfolio_notional_limit, adv_notional_limit)
        max_allowed_quantity = int(max_allowed_notional / price)

        if max_allowed_quantity <= 0:
            return 0

        if requested_quantity > 0:
            capped_quantity = min(requested_quantity, max_allowed_quantity)
        else:
            capped_quantity = max(requested_quantity, -max_allowed_quantity)

        if abs(capped_quantity) < abs(requested_quantity):
            self.Debug(
                f"{self.Time.date()} | {symbol.Value} order capped by execution limits. "
                f"RequestedNotional=${requested_notional:,.2f}, MaxAllowedNotional=${max_allowed_notional:,.2f}, "
                f"RequestedQty={requested_quantity}, SubmittedQty={capped_quantity}."
            )

        return capped_quantity

    def ApplyBuyingPowerBuffer(self, symbol: Symbol, quantity: int, price: float, portfolio_value: float) -> int:
        if quantity <= 0:
            return quantity

        current_cash = float(self.Portfolio.Cash)
        required_cash_buffer = portfolio_value * self.buying_power_buffer
        spendable_cash = max(0.0, current_cash - required_cash_buffer)
        max_affordable_quantity = int(spendable_cash / price)
        adjusted_quantity = min(quantity, max_affordable_quantity)

        if adjusted_quantity < quantity:
            self.Debug(
                f"{self.Time.date()} | {symbol.Value} buy capped by cash buffer. "
                f"RequestedQty={quantity}, AdjustedQty={adjusted_quantity}, Cash=${current_cash:,.2f}, "
                f"RequiredBuffer=${required_cash_buffer:,.2f}."
            )

        return adjusted_quantity

    def EstimateAverageDollarVolume(self, symbol: Symbol):
        try:
            history = self.History(symbol, self.adv_lookback_days, Resolution.Daily)
        except Exception as error:
            self.Debug(f"{self.Time.date()} | WARNING: ADV history request failed for {symbol.Value}: {error}")
            return None

        if history is None or history.empty:
            return None
        if "close" not in history.columns or "volume" not in history.columns:
            return None

        try:
            closes = history["close"].dropna()
            volumes = history["volume"].dropna()
        except Exception as error:
            self.Debug(f"{self.Time.date()} | WARNING: Could not extract ADV fields for {symbol.Value}: {error}")
            return None

        if len(closes) == 0 or len(volumes) == 0:
            return None

        dollar_volume = (closes * volumes).dropna()
        if len(dollar_volume) == 0:
            return None

        average_dollar_volume = float(dollar_volume.mean())
        self.Plot("Liquidity", f"{symbol.Value} ADV", average_dollar_volume)
        return average_dollar_volume

    def CalculateMomentum(self, symbol: Symbol):
        try:
            history = self.History(symbol, self.lookback_days + 1, Resolution.Daily)
        except Exception as error:
            self.Debug(f"{self.Time.date()} | WARNING: History request failed for {symbol.Value}: {error}")
            return None

        if history is None or history.empty:
            return None
        if "close" not in history.columns:
            self.Debug(f"{self.Time.date()} | WARNING: History for {symbol.Value} does not contain a close column.")
            return None

        try:
            closes = history["close"].dropna()
        except Exception as error:
            self.Debug(f"{self.Time.date()} | WARNING: Could not extract closes for {symbol.Value}: {error}")
            return None

        if len(closes) < self.lookback_days + 1:
            return None

        current_price = float(closes.iloc[-1])
        lookback_price = float(closes.iloc[-(self.lookback_days + 1)])

        if current_price <= 0 or lookback_price <= 0:
            self.Debug(
                f"{self.Time.date()} | WARNING: Invalid price for {symbol.Value}. "
                f"Current={current_price}, Lookback={lookback_price}"
            )
            return None

        return current_price / lookback_price - 1.0

    def RecordDailyDiagnostics(self):
        portfolio_value = float(self.Portfolio.TotalPortfolioValue)
        if portfolio_value <= 0:
            return

        if self.previous_portfolio_value is not None and self.previous_portfolio_value > 0:
            daily_return = portfolio_value / self.previous_portfolio_value - 1.0
            self.daily_returns.append(daily_return)
            if len(self.daily_returns) > self.realized_vol_lookback_days * 3:
                self.daily_returns = self.daily_returns[-self.realized_vol_lookback_days * 3:]

        self.previous_portfolio_value = portfolio_value
        self.UpdatePortfolioHighWaterMark(portfolio_value)
        drawdown = self.GetCurrentDrawdown(portfolio_value)
        realized_vol = self.GetRealizedVolatility()
        active_gross = self.GetActiveTargetGrossExposure()
        current_gross = self.GetCurrentGrossExposure()

        self.Plot("Risk", "Drawdown", drawdown)
        if realized_vol is not None:
            self.Plot("Risk", "Realized Vol", realized_vol)
            self.Plot("Risk", "Vol Target", self.target_realized_vol)
        self.Plot("Risk", "Active Gross Target", active_gross)
        self.Plot("Risk", "Current Gross", current_gross)
        self.Plot("Portfolio", "Value", portfolio_value)
        self.Plot("Execution", "Cash", float(self.Portfolio.Cash))
        self.Plot("Execution", "Gross Target", active_gross)
