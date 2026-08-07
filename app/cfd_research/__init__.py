"""
CFD prop-firm research tooling.

Turns raw backtest trades into the answer that actually matters for prop trading:
"would this pass the firm's 2-step evaluation, and survive the funded account,
without ever breaching the daily or max drawdown?"

Modules:
  challenge_sim  — account-size-agnostic prop-firm challenge simulator + a
                   Monte-Carlo-over-history harness (pass rate, blow-up rate,
                   days-to-pass, worst drawdown), with a configurable generic
                   ruleset and a per-trade risk sweep.
"""
