from AlgorithmImports import *


class EqualWeightEtfBasketBenchmark(QCAlgorithm):
    """
    Alternative Model 2 — Equal-Weight ETF Basket Benchmark

    Purpose:
    Tests whether the active momentum rotation rule adds value beyond a simple,
    diversified, static ETF allocation.

    Universe:
    SPY, QQQ, TLT, GLD, SHY

    Governance constraints:
    - Long-only
    - No leverage
    - No shorting
    - Fixed approved ETF universe
    - Monthly rebalancing
    - No alpha signal
    - No parameter optimisation
    """

    def Initialize(self):
        self.SetStartDate(2007, 1, 1)
        self.SetEndDate(2025, 12, 31)
        self.SetCash(100000)

        tickers = ["SPY", "QQQ", "TLT", "GLD", "SHY"]
        self.symbols = []

        for ticker in tickers:
            security = self.AddEquity(ticker, Resolution.Daily)
            security.SetLeverage(1.0)
            security.SetDataNormalizationMode(DataNormalizationMode.Adjusted)
            self.symbols.append(security.Symbol)

        self.spy = self.symbols[0]
        self.SetBenchmark(self.spy)

        self.target_weight = 1.0 / len(self.symbols)

        self.Schedule.On(
            self.DateRules.MonthStart(self.spy),
            self.TimeRules.AfterMarketOpen(self.spy, 30),
            self.Rebalance
        )

        self.Debug("Initialized Equal-Weight ETF Basket benchmark.")

    def OnData(self, data: Slice):
        pass

    def Rebalance(self):
        for symbol in self.symbols:
            self.SetHoldings(symbol, self.target_weight)

        weights_log = ", ".join(
            f"{symbol.Value}: {self.target_weight:.2%}"
            for symbol in self.symbols
        )

        self.Debug(f"{self.Time.date()} | Equal-weight rebalance | Targets: [{weights_log}]")

        for symbol in self.symbols:
            self.Plot("Target Weights", symbol.Value, self.target_weight)
