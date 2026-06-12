FILE 3 - SCORING AND STABILITY ENGINE

Frequency

Not real-time.

Run manually:

Weekly
Monthly

⸻

Input

Trades Table
+
Exit Results Table

⸻

Grouping

Evaluate by:

Variant_ID

and optionally:

Variant_ID + Instrument

and optionally:

Variant_ID + Regime

⸻

Core Metrics

Trade Count

Number of trades.

Minimum threshold required.

Very low trade count variants are ignored.

⸻

Win Rate

Wins / Total Trades

⸻

Average Win

Mean profit of winning trades.

⸻

Average Loss

Mean loss of losing trades.

⸻

Expectancy

Expectancy =
(Win Rate × Average Win)

(Loss Rate × Average Loss)

Primary metric.

⸻

Profit Factor

Gross Profit
/
Gross Loss

⸻

Net PnL

Total Profit

Total Loss

⸻

Max Drawdown

Largest peak-to-valley decline.

⸻

Recovery Factor

Net Profit
/
Max Drawdown

⸻

Stability Analysis

Split history into periods.

Examples:

Monthly
Quarterly

Check:

* Consistency of expectancy
* Consistency of win rate
* Consistency of profit factor

Penalize variants that earn everything in one short period.

Reward variants that perform across many periods.

⸻

Regime Analysis

Using stored metadata:

Find:

* Best ORB regime
* Worst ORB regime
* Best VPA regime
* Worst VPA regime

Examples:

Gap > 1%
1H Bullish
Morning Session

Determine where variants perform well or poorly.

⸻

Promotion Process

Variant performs:

Backtest
+
Forward Test
+
Live Paper Trading

Then becomes:

Candidate Variant

Candidate Variants are monitored in Telegram.

All 150,000 variants continue running in background.

If deployed variant degrades:

Return to database.

Select new candidate.

Repeat process.

⸻

Final Objective

Build a continuously learning research platform:

Live Feed
→ Entry Discovery

Entry Discovery
→ Trade Database

Trade Database
→ Exit Research

Exit Research
→ Stability Analysis

Stability Analysis
→ Candidate Selection

Candidate Selection
→ Telegram Monitoring

Research Engine never stops running.