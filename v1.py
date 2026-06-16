from AlgorithmImports import *


class GovernedEtfMomentumRotation(QCAlgorithm):
    """
    Governed Multi-Asset ETF Momentum Rotation

    Long-only monthly ETF rotation across SPY, QQQ, TLT, and GLD.
    Selects up to the top 3 risk assets with positive 126-trading-day momentum.
    Residual capital is allocated to SHY as the defensive sleeve.

    Baseline design:
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
        self.SetEndDate(2025, 12, 31)
        self.SetCash(100000)

        # -----------------------------
        # Strategy parameters
        # -----------------------------
        # Change this value to 63, 126, or 252 for robustness tests.
        # Do not optimize it inside this algorithm.
        self.lookback_days = 126

        self.max_risk_assets = 3
        self.risk_asset_weight = 1.0 / self.max_risk_assets
        self.max_gross_exposure = 1.0

        # -----------------------------
        # Approved ETF universe
        # -----------------------------
        risk_tickers = ["SPY", "QQQ", "TLT", "GLD"]
        defensive_ticker = "SHY"

        self.symbols_by_ticker = {}

        for ticker in risk_tickers + [defensive_ticker]:
            security = self.AddEquity(ticker, Resolution.Daily)
            security.SetLeverage(1.0)  # Governance rule: no leverage.
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
            f"Initialized governed ETF momentum rotation. "
            f"Lookback={self.lookback_days}, RiskAssets={risk_tickers}, Defensive={defensive_ticker}"
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
        5. Allocate 1/3 to each selected risk asset.
        6. Allocate residual weight to SHY.
        7. Set all other assets to zero.
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

        # Build target weights.
        target_weights = {symbol: 0.0 for symbol in self.all_symbols}

        for symbol in selected_symbols:
            target_weights[symbol] = self.risk_asset_weight

        defensive_weight = 1.0 - sum(target_weights[symbol] for symbol in self.risk_symbols)
        defensive_weight = max(0.0, min(1.0, defensive_weight))
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
            f"Targets: [{weights_log}]"
        )

        # Execute target allocations.
        # First reduce/remove non-target holdings, then set desired allocations.
        for symbol in self.all_symbols:
            if target_weights[symbol] == 0 and self.Portfolio[symbol].Invested:
                self.SetHoldings(symbol, 0)

        for symbol, weight in target_weights.items():
            if weight > 0:
                self.SetHoldings(symbol, weight)

        # Plot target weights for governance diagnostics.
        for symbol, weight in target_weights.items():
            self.Plot("Target Weights", symbol.Value, weight)

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