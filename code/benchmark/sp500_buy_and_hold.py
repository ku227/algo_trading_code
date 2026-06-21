from AlgorithmImports import *


class BuyAndHoldSpyBenchmark(QCAlgorithm):
    """
    Alternative Model 1 — Buy-and-Hold SPY Benchmark

    Purpose:
    Provides a passive US equity benchmark against which the governed ETF
    momentum rotation strategy can be compared.

    Governance constraints:
    - Long-only
    - No leverage
    - No shorting
    - No market timing
    - No parameter optimisation
    """

    def Initialize(self):
        self.SetStartDate(2007, 1, 1)
        self.SetEndDate(2025, 12, 31)
        self.SetCash(100000)

        security = self.AddEquity("SPY", Resolution.Daily)
        security.SetLeverage(1.0)
        security.SetDataNormalizationMode(DataNormalizationMode.Adjusted)

        self.spy = security.Symbol
        self.SetBenchmark(self.spy)

        self.invested = False

        self.Debug("Initialized Buy-and-Hold SPY benchmark.")

    def OnData(self, data: Slice):
        if self.invested:
            return

        if not data.ContainsKey(self.spy):
            return

        self.SetHoldings(self.spy, 1.0)
        self.invested = True

        self.Debug(f"{self.Time.date()} | Invested 100% in SPY.")