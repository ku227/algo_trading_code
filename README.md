# Governed Multi-Asset ETF Momentum Rotation Strategy

This repository contains the QuantConnect source code and exported backtest results for a governed multi-asset ETF momentum rotation strategy. The project was developed for an algorithmic trading systems evaluation and focuses on governance, transparency, execution realism, stress testing, and model-risk documentation rather than pure return maximisation.

The final submitted model is **V3**, a risk-managed implementation of a 126-trading-day ETF momentum strategy. Earlier versions are retained as development evidence:

* **V1**: baseline 126-day ETF momentum rotation model.
* **V2**: execution-constrained version of V1 with spread-cost, cash-buffer, order-size, and ADV constraints.
* **V3**: final governed model with V2 execution constraints plus active drawdown controls, realised-volatility targeting, asset-level stop-loss logic, daily risk override, and conditional defensive allocation.

## Strategy overview

The strategy trades a fixed universe of liquid US-listed ETFs:

| Symbol | Asset class                     | Strategy role                                        |
| ------ | ------------------------------- | ---------------------------------------------------- |
| SPY    | US large-cap equities           | Broad US equity exposure and benchmark reference     |
| QQQ    | US growth / technology equities | Higher-growth equity momentum candidate              |
| TLT    | Long-duration US Treasuries     | Duration-sensitive fixed-income exposure             |
| GLD    | Gold                            | Alternative defensive / inflation-sensitive exposure |
| SHY    | Short-duration US Treasuries    | Defensive residual allocation sleeve                 |

The core alpha logic ranks SPY, QQQ, TLT, and GLD by trailing 126-trading-day momentum. Assets with positive momentum are eligible for selection. Up to the top three eligible assets are selected and initially equal-weighted before final risk-management and execution constraints are applied.

## Model versions

### V1 — Baseline 126D momentum model

`v1.py` is the clean research prototype. It implements the original 126-trading-day momentum rotation logic using a fixed ETF universe, monthly rebalancing, long-only exposure, no leverage, and SHY as the residual defensive allocation.

Purpose:

* establish the baseline alpha logic;
* test whether the simple ETF momentum rule produces a plausible multi-asset allocation system;
* provide the original reference case for robustness and stress testing.

Main limitation:

* execution modelling is simple;
* drawdown and volatility are monitored but not actively controlled;
* no explicit stop-loss or realised-volatility target is implemented.

### V2 — Execution-constrained model

`v2_execution_constrained.py` retains the V1 alpha logic but strengthens execution realism.

Additional features:

* manually sized market orders rather than relying only on simple target-weight setting;
* explicit spread-cost assumptions;
* 2% buying-power buffer;
* per-order notional cap;
* ADV-based order-size cap;
* minimum trade-value filter;
* sell-before-buy rebalance sequencing.

Purpose:

* test whether the core V1 strategy remains viable after more realistic execution assumptions;
* document the impact of transaction costs, liquidity constraints, and cash-buffer assumptions.

Main limitation:

* execution realism improves, but portfolio-level drawdown, volatility, and stop-loss controls remain incomplete.

### V3 — Final governed model

`v3.py` is the final submitted model. It preserves the same underlying 126-day ETF momentum idea but adds active risk-management controls.

Additional features:

* portfolio drawdown controls;
* soft and hard drawdown thresholds;
* realised-volatility targeting;
* asset-level stop-loss logic;
* daily risk override between monthly rebalances;
* conditional defensive allocation;
* V2 execution constraints retained.

Purpose:

* convert the earlier governance weaknesses into explicit active controls;
* reduce drawdown exposure;
* provide the final model evaluated in the written report.

Main limitation:

* V3 improves drawdown governance but remains vulnerable to 2022-style inflation/rate-shock regimes where equities, bonds, and duration-sensitive defensive assets weaken together.

## Repository structure

```text
algo_trading_code/
│
├── code/
│   │
│   ├── model/
│   │   ├── v1.py
│   │   ├── v2_execution_constrained.py
│   │   └── v3.py
│   │
│   └── benchmark/
│       ├── equal_weights_etf.py
│       └── sp500_buy_and_hold.py
│
└── backtest_returns/
    │
    └── JSON/
        │
        ├── benchmarks/
        │   ├── equal_weight_ETF_full_window.json
        │   ├── sp500_full_window.json
        │   ├── sp500_stress_covid_2020.json
        │   ├── sp500_stress_gfc_2007-2009.json
        │   └── sp500_stress_rates_2022.json
        │
        ├── v1/
        │   ├── v1_full_window.json
        │   ├── v1_stress_2022_rate_shock.json
        │   ├── v1_stress_covid_2020.json
        │   ├── v1_stress_gfc_2007-2009.json
        │   │
        │   └── lookback robustness tests/
        │       ├── 63d_backtest.json
        │       ├── 126d_backtest.json
        │       └── 252d_backtest.json
        │
        ├── v2/
        │   ├── v2 - full window.json
        │   ├── v2 - GFC 2007-09.json
        │   ├── v2 - 2020 covid shock.json
        │   └── v2 - 2022 rate shock.json
        │
        └── v3/
            ├── v3_full_window.json
            ├── v3_gfc_2007-09.json
            ├── v3_covid_2020.json
            └── v3_rates_2022.json
```

## Directory descriptions

### `code/model/`

Contains the three model-development versions of the ETF momentum strategy.

| File                          | Description                                                                          |
| ----------------------------- | ------------------------------------------------------------------------------------ |
| `v1.py`                       | Baseline 126-day ETF momentum rotation model.                                        |
| `v2_execution_constrained.py` | Execution-constrained version of the baseline model.                                 |
| `v3.py`                       | Final governed model with active risk-management overlays and execution constraints. |

### `code/benchmark/`

Contains benchmark strategies used for comparison.

| File                    | Description                                                            |
| ----------------------- | ---------------------------------------------------------------------- |
| `sp500_buy_and_hold.py` | Passive SPY buy-and-hold benchmark.                                    |
| `equal_weights_etf.py`  | Static equal-weight ETF basket benchmark across the strategy universe. |

### `backtest_returns/JSON/benchmarks/`

Contains exported QuantConnect JSON results for the benchmark strategies.

| File                              | Description                                                     |
| --------------------------------- | --------------------------------------------------------------- |
| `sp500_full_window.json`          | Full-window SPY buy-and-hold benchmark result.                  |
| `sp500_stress_gfc_2007-2009.json` | SPY benchmark over the Global Financial Crisis stress window.   |
| `sp500_stress_covid_2020.json`    | SPY benchmark over the 2020 COVID stress window.                |
| `sp500_stress_rates_2022.json`    | SPY benchmark over the 2022 inflation/rate-shock stress window. |
| `equal_weight_ETF_full_window.json`   | Static equal-weight ETF basket benchmark result.                |

### `backtest_returns/JSON/v1/`

Contains exported QuantConnect JSON results for the V1 baseline model.

| File                             | Description                                      |
| -------------------------------- | ------------------------------------------------ |
| `v1_full_window.json`            | V1 full-window backtest result.                  |
| `v1_stress_gfc_2007-2009.json`   | V1 Global Financial Crisis stress-test result.   |
| `v1_stress_covid_2020.json`      | V1 COVID 2020 stress-test result.                |
| `v1_stress_2022_rate_shock.json` | V1 2022 inflation/rate-shock stress-test result. |

The `lookback robustness tests/` subdirectory contains the V1 lookback-variation tests:

| File                 | Description                                    |
| -------------------- | ---------------------------------------------- |
| `63d_backtest.json`  | Shorter 63-trading-day momentum lookback test. |
| `126d_backtest.json` | Baseline 126-trading-day lookback test.        |
| `252d_backtest.json` | Longer 252-trading-day momentum lookback test. |

### `backtest_returns/JSON/v2/`

Contains exported QuantConnect JSON results for the V2 execution-constrained model.

| File                         | Description                                      |
| ---------------------------- | ------------------------------------------------ |
| `v2 - full window.json`      | V2 full-window backtest result.                  |
| `v2 - GFC 2007-09.json`      | V2 Global Financial Crisis stress-test result.   |
| `v2 - 2020 covid shock.json` | V2 COVID 2020 stress-test result.                |
| `v2 - 2022 rate shock.json`  | V2 2022 inflation/rate-shock stress-test result. |

### `backtest_returns/JSON/v3/`

Contains exported QuantConnect JSON results for the final V3 governed model.

| File                  | Description                                      |
| --------------------- | ------------------------------------------------ |
| `v3_full_window.json` | V3 full-window backtest result.                  |
| `v3_gfc_2007-09.json` | V3 Global Financial Crisis stress-test result.   |
| `v3_covid_2020.json`  | V3 COVID 2020 stress-test result.                |
| `v3_rates_2022.json`  | V3 2022 inflation/rate-shock stress-test result. |

## Backtest windows

The project uses the following evaluation windows:

| Test                    | Date range               | Purpose                                                                         |
| ----------------------- | ------------------------ | ------------------------------------------------------------------------------- |
| Full window             | 2007-01-01 to 2025-12-31 | Long-horizon strategy evaluation across multiple regimes.                       |
| Global Financial Crisis | 2007-01-01 to 2009-12-31 | Stress test for severe equity-market and credit-system stress.                  |
| COVID shock             | 2020-01-01 to 2020-12-31 | Stress test for rapid crash and policy-driven recovery.                         |
| 2022 rate shock         | 2022-01-01 to 2022-12-31 | Stress test for inflation, rising rates, and simultaneous equity/bond pressure. |

## Key interpretation

The final submitted model is **V3**. V1 and V2 are included to preserve an audit trail of model development.

The main development path is:

```text
V1: baseline momentum logic
   ↓
V2: execution realism added
   ↓
V3: active risk controls added
```

V3 should therefore be interpreted as the final governed implementation, not as a return-maximising optimisation of the earlier versions. It deliberately sacrifices some full-window return in exchange for stronger drawdown governance and more explicit risk controls.

The final governance recommendation from the associated report is:

> Revise before promotion.

The strategy is transparent, auditable, and suitable as a governed research prototype. It is not presented as production-ready because the 2022 inflation/rate-shock stress test remains economically negative and further live-execution, parameter-sensitivity, slippage, monitoring, and operational-runbook validation would be required before deployment.

## How to use this repository

1. Open QuantConnect.
2. Create a new Python algorithm project.
3. Copy the relevant model or benchmark file from `code/` into the QuantConnect algorithm editor.
4. Set the desired start and end dates in the QuantConnect project to match the relevant full-window or stress-test period.
5. Run the backtest.
6. Compare the generated QuantConnect output with the exported JSON files in `backtest_returns/JSON/`.

The JSON files are retained as reproducibility evidence and support the performance tables, stress-test analysis, and charts used in the final report.

## Reproducibility notes

The backtests were run in QuantConnect using daily ETF data. The strategy assumes adjusted price data for economically comparable historical prices after corporate actions. Backtest outputs may vary slightly if QuantConnect data, fee modelling, brokerage settings, or execution assumptions change.

The JSON files in this repository should be treated as archived evidence from the specific backtest runs used in the report.

## Limitations

This repository does not claim that the strategy is live-deployable or production-ready. Key limitations include:

* daily-bar backtests do not fully model intraday spread widening, partial fills, or liquidity gaps;
* ETF liquidity can deteriorate during stress periods;
* model performance remains sensitive to regime conditions;
* V3 still loses money in the 2022 inflation/rate-shock stress period;
* further testing would be required before any live deployment.
