from AlgorithmImports import *


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

        # US-listed ETFs in this strategy are USD-denominated.
        return OrderFee(CashAmount(spread_cost, "USD"))


class GovernedEtfMomentumRotation(QCAlgorithm):
    """
    Governed Multi-Asset ETF Momentum Rotation

    Long-only monthly ETF rotation across SPY, QQQ, TLT, and GLD.
    Selects up to the top 3 risk assets with positive 126-trading-day momentum.
    Residual investable capital is allocated to SHY as the defensive sleeve.

    Execution-constrained extension:
    - Conservative bid-ask spread / slippage cost model through a custom fee model
    - Buying-power buffer so the strategy does not target 100% capital deployment
    - Per-order notional cap as a percentage of portfolio value
    - Per-order notional cap as a percentage of estimated average daily dollar volume
    - Audit logs for capped, skipped, and submitted orders

    Baseline design remains intact:
    - No leverage
    - No shorting
    - Monthly turnover only
    - Missing/incomplete history excluded from ranking
    - No parameter optimization inside the algorithm
    """

    def Initialize(self):
        # -----------------------------
        # Backtest and account settings
        # -----------------------------
        self.SetStartDate(2007, 1, 1)
        self.SetEndDate(2009, 12, 31)
        self.SetCash(100000)

        # -----------------------------
        # Strategy parameters
        # -----------------------------
        # Change this value to 63, 126, or 252 for robustness tests.
        # Do not optimize it inside this algorithm.
        self.lookback_days = 126

        self.max_risk_assets = 3

        # Buying-power / cash buffer.
        # A 2% cash buffer reduces the risk of over-investment caused by fees,
        # spread costs, price movement, or fill differences.
        self.buying_power_buffer = 0.02
        self.target_gross_exposure = 1.0 - self.buying_power_buffer
        self.max_gross_exposure = self.target_gross_exposure
        self.risk_asset_weight = self.target_gross_exposure / self.max_risk_assets

        # Execution constraints.
        # These are intentionally conservative controls, not alpha parameters.
        self.max_order_pct_of_portfolio = 0.20       # no single order above 20% of portfolio value
        self.adv_lookback_days = 20                 # ADV lookback for liquidity estimate
        self.max_order_pct_of_adv = 0.01            # no single order above 1% of estimated ADV
        self.minimum_trade_value = 250.0            # ignore tiny rebalance differences

        # One-way effective spread/slippage assumptions in basis points.
        # These are reduced-form estimates for governance/stress testing rather
        # than a live quote-based order-book model.
        self.spread_bps_by_ticker = {
            "SPY": 1.0,
            "QQQ": 1.0,
            "TLT": 2.0,
            "GLD": 2.0,
            "SHY": 1.0,
        }

        # -----------------------------
        # Approved ETF universe
        # -----------------------------
        risk_tickers = ["SPY", "QQQ", "TLT", "GLD"]
        defensive_ticker = "SHY"

        self.symbols_by_ticker = {}

        for ticker in risk_tickers + [defensive_ticker]:
            security = self.AddEquity(ticker, Resolution.Daily)
            security.SetLeverage(1.0)
            security.SetDataNormalizationMode(DataNormalizationMode.Adjusted)

            # Spread-cost model. This replaces a frictionless assumption with an
            # explicit reduced-form cost for crossing the bid-ask spread.
            spread_bps = self.spread_bps_by_ticker.get(ticker, 2.0)
            security.SetFeeModel(SpreadCostFeeModel(spread_bps))

            self.symbols_by_ticker[ticker] = security.Symbol

        self.risk_symbols = [self.symbols_by_ticker[ticker] for ticker in risk_tickers]
        self.defensive_symbol = self.symbols_by_ticker[defensive_ticker]
        self.all_symbols = self.risk_symbols + [self.defensive_symbol]

        self.spy = self.symbols_by_ticker["SPY"]
        self.SetBenchmark(self.spy)

        # Warm up with at least the full momentum lookback window.
        # Extra bars help avoid edge cases around holidays and incomplete history.
        self.SetWarmUp(self.lookback_days + 5, Resolution.Daily)

        # -----------------------------
        # Monthly rebalance schedule
        # -----------------------------
        # Uses SPY as the scheduling symbol. The rebalance occurs shortly after
        # market open at the start of each month. Daily History calls at this time
        # use completed historical bars, avoiding future information.
        self.Schedule.On(
            self.DateRules.MonthStart(self.spy),
            self.TimeRules.AfterMarketOpen(self.spy, 30),
            self.Rebalance
        )

        # Daily diagnostics for drawdown monitoring.
        self.portfolio_high_water_mark = 0
        self.Schedule.On(
            self.DateRules.EveryDay(self.spy),
            self.TimeRules.BeforeMarketClose(self.spy, 1),
            self.RecordDailyDiagnostics
        )

        self.Debug(
            f"Initialized execution-constrained governed ETF momentum rotation. "
            f"Lookback={self.lookback_days}, RiskAssets={risk_tickers}, Defensive={defensive_ticker}, "
            f"TargetGross={self.target_gross_exposure:.2%}, CashBuffer={self.buying_power_buffer:.2%}, "
            f"MaxOrderPctPortfolio={self.max_order_pct_of_portfolio:.2%}, "
            f"MaxOrderPctADV={self.max_order_pct_of_adv:.2%}"
        )

    def OnData(self, data: Slice):
        """
        Trading is intentionally scheduled monthly in Rebalance().
        OnData is kept minimal to avoid accidental daily trading.
        """
        pass

    def Rebalance(self):
        """
        Monthly rebalance:
        1. Calculate trailing lookback momentum for each risk asset.
        2. Exclude assets with incomplete history.
        3. Rank assets by momentum.
        4. Select up to top 3 assets with positive momentum.
        5. Allocate equal risk weights to each selected risk asset.
        6. Allocate residual investable weight to SHY.
        7. Keep the buying-power buffer unallocated as cash.
        8. Send orders through execution constraints rather than direct SetHoldings().
        """

        if self.IsWarmingUp:
            self.Debug(f"{self.Time.date()} | Rebalance skipped: algorithm is warming up.")
            return

        momentum_scores = {}

        for symbol in self.risk_symbols:
            momentum = self.CalculateMomentum(symbol)

            if momentum is None:
                self.Debug(
                    f"{self.Time.date()} | WARNING: {symbol.Value} excluded from ranking "
                    f"because required history is missing or incomplete."
                )
                continue

            momentum_scores[symbol] = momentum

        # Positive momentum gate: only assets above zero are eligible.
        positive_momentum_assets = [
            symbol for symbol, momentum in momentum_scores.items()
            if momentum > 0
        ]

        # Rank by momentum descending and select up to top 3.
        selected_symbols = sorted(
            positive_momentum_assets,
            key=lambda symbol: momentum_scores[symbol],
            reverse=True
        )[:self.max_risk_assets]

        # Build target weights. The total investable target is deliberately below
        # 100%, leaving a buying-power/cash buffer.
        target_weights = {symbol: 0.0 for symbol in self.all_symbols}

        for symbol in selected_symbols:
            target_weights[symbol] = self.risk_asset_weight

        defensive_weight = self.target_gross_exposure - sum(
            target_weights[symbol] for symbol in self.risk_symbols
        )
        defensive_weight = max(0.0, min(self.target_gross_exposure, defensive_weight))
        target_weights[self.defensive_symbol] = defensive_weight

        # Governance check: long-only and max gross exposure.
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

        # Log audit trail.
        momentum_log = ", ".join(
            f"{symbol.Value}: {momentum_scores[symbol]:.2%}"
            for symbol in sorted(momentum_scores.keys(), key=lambda s: s.Value)
        )

        selected_log = ", ".join(symbol.Value for symbol in selected_symbols) if selected_symbols else "None"

        weights_log = ", ".join(
            f"{symbol.Value}: {target_weights[symbol]:.2%}"
            for symbol in self.all_symbols
        )

        self.Debug(
            f"{self.Time.date()} | Momentum: [{momentum_log}] | "
            f"Selected: [{selected_log}] | "
            f"SHY Defensive Weight: {defensive_weight:.2%} | "
            f"Cash Buffer Target: {self.buying_power_buffer:.2%} | "
            f"Targets: [{weights_log}]"
        )

        # Execute target allocations through order-size and liquidity constraints.
        self.ExecuteConstrainedRebalance(target_weights)

        # Plot target weights for governance diagnostics.
        for symbol, weight in target_weights.items():
            self.Plot("Target Weights", symbol.Value, weight)

    def ExecuteConstrainedRebalance(self, target_weights):
        """
        Converts target weights into manually sized market orders subject to:
        - minimum trade value threshold,
        - buying-power buffer,
        - maximum order size as % of portfolio value,
        - maximum order size as % of estimated ADV.

        Sells are submitted before buys to reduce the chance of buying-power pressure.
        """

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

            capped_quantity = self.ApplyOrderSizeLimits(
                symbol=symbol,
                requested_quantity=requested_quantity,
                price=price,
                portfolio_value=portfolio_value
            )

            if capped_quantity == 0:
                self.Debug(
                    f"{self.Time.date()} | {symbol.Value} order blocked by execution constraints. "
                    f"RequestedQty={requested_quantity}."
                )
                continue

            planned_orders.append((symbol, capped_quantity, requested_quantity, price))

        # Submit sells first, then buys.
        planned_orders.sort(key=lambda item: item[1])

        for symbol, quantity, requested_quantity, price in planned_orders:
            # Additional buying-power buffer check for buy orders.
            if quantity > 0:
                quantity = self.ApplyBuyingPowerBuffer(symbol, quantity, price, portfolio_value)
                if quantity == 0:
                    self.Debug(
                        f"{self.Time.date()} | {symbol.Value} buy order blocked by buying-power buffer."
                    )
                    continue

            tag = (
                f"Execution-constrained rebalance | RequestedQty={requested_quantity} | "
                f"SubmittedQty={quantity}"
            )
            self.MarketOrder(symbol, quantity, False, tag)

            self.Debug(
                f"{self.Time.date()} | ORDER: {symbol.Value} | RequestedQty={requested_quantity} | "
                f"SubmittedQty={quantity} | EstNotional=${abs(quantity) * price:,.2f}"
            )

    def ApplyOrderSizeLimits(self, symbol: Symbol, requested_quantity: int, price: float, portfolio_value: float) -> int:
        """
        Caps order size using both portfolio-level and ADV-based constraints.
        """

        requested_notional = abs(requested_quantity) * price
        portfolio_notional_limit = portfolio_value * self.max_order_pct_of_portfolio

        average_dollar_volume = self.EstimateAverageDollarVolume(symbol)
        if average_dollar_volume is None or average_dollar_volume <= 0:
            adv_notional_limit = portfolio_notional_limit
            self.Debug(
                f"{self.Time.date()} | WARNING: {symbol.Value} ADV unavailable; "
                f"using portfolio notional limit only."
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
                f"RequestedNotional=${requested_notional:,.2f}, "
                f"MaxAllowedNotional=${max_allowed_notional:,.2f}, "
                f"RequestedQty={requested_quantity}, SubmittedQty={capped_quantity}."
            )

        return capped_quantity

    def ApplyBuyingPowerBuffer(self, symbol: Symbol, quantity: int, price: float, portfolio_value: float) -> int:
        """
        Caps buy orders so that cash is not intentionally driven below the buffer.
        """

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
                f"RequestedQty={quantity}, AdjustedQty={adjusted_quantity}, "
                f"Cash=${current_cash:,.2f}, RequiredBuffer=${required_cash_buffer:,.2f}."
            )

        return adjusted_quantity

    def EstimateAverageDollarVolume(self, symbol: Symbol):
        """
        Estimates ADV from completed daily close and volume history.
        Returns None if the history is unavailable or malformed.
        """

        try:
            history = self.History(symbol, self.adv_lookback_days, Resolution.Daily)
        except Exception as error:
            self.Debug(
                f"{self.Time.date()} | WARNING: ADV history request failed for "
                f"{symbol.Value}: {error}"
            )
            return None

        if history is None or history.empty:
            return None

        if "close" not in history.columns or "volume" not in history.columns:
            return None

        try:
            closes = history["close"].dropna()
            volumes = history["volume"].dropna()
        except Exception as error:
            self.Debug(
                f"{self.Time.date()} | WARNING: Could not extract ADV fields for "
                f"{symbol.Value}: {error}"
            )
            return None

        if len(closes) == 0 or len(volumes) == 0:
            return None

        # Align by index before multiplication to avoid accidental length mismatch.
        dollar_volume = (closes * volumes).dropna()
        if len(dollar_volume) == 0:
            return None

        average_dollar_volume = float(dollar_volume.mean())
        self.Plot("Liquidity", f"{symbol.Value} ADV", average_dollar_volume)
        return average_dollar_volume

    def CalculateMomentum(self, symbol: Symbol):
        """
        Momentum formula:
            current_price / price_N_trading_days_ago - 1

        Uses completed daily close prices from QuantConnect History.
        Returns None when history is unavailable, incomplete, malformed, or invalid.
        """

        try:
            history = self.History(symbol, self.lookback_days + 1, Resolution.Daily)
        except Exception as error:
            self.Debug(
                f"{self.Time.date()} | WARNING: History request failed for "
                f"{symbol.Value}: {error}"
            )
            return None

        if history is None or history.empty:
            return None

        if "close" not in history.columns:
            self.Debug(
                f"{self.Time.date()} | WARNING: History for {symbol.Value} "
                f"does not contain a close column."
            )
            return None

        # History may be indexed either by time or by a multi-index depending on request shape.
        try:
            closes = history["close"].dropna()
        except Exception as error:
            self.Debug(
                f"{self.Time.date()} | WARNING: Could not extract closes for "
                f"{symbol.Value}: {error}"
            )
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
        """
        Tracks portfolio drawdown for reporting and stress-test review.
        This does not affect trading decisions.
        """

        portfolio_value = float(self.Portfolio.TotalPortfolioValue)

        if portfolio_value <= 0:
            return

        if self.portfolio_high_water_mark == 0:
            self.portfolio_high_water_mark = portfolio_value

        self.portfolio_high_water_mark = max(
            self.portfolio_high_water_mark,
            portfolio_value
        )

        drawdown = portfolio_value / self.portfolio_high_water_mark - 1.0

        self.Plot("Risk", "Drawdown", drawdown)
        self.Plot("Portfolio", "Value", portfolio_value)
        self.Plot("Execution", "Cash", float(self.Portfolio.Cash))
        self.Plot("Execution", "Gross Target", self.target_gross_exposure)
