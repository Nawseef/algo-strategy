FILE 1 - ENTRY ENGINE DESIGN

Goal

Continuously paper-trade a large number of entry variants across multiple strategies and instruments.

The system does NOT execute exits during market hours.

The system only records entry events and market context.

⸻

Strategies

Initial strategies:

1. ORB
2. VPA
3. Bollinger Band
4. Trend Following
5. Mean Reversion

⸻

Timeframes

1. 5 Minute
2. 15 Minute
3. 30 Minute

⸻

Entry Filters

These participate in combinations.

ATR Filter

* None
* ATR > 10
* ATR > 15
* ATR > 20
* ATR > 25

ADX Filter

* None
* ADX > 15
* ADX > 20
* ADX > 25
* ADX > 30

VIX Filter

* None
* VIX > 12
* VIX > 15
* VIX > 18

Volume Filter

* None
* Volume > 1.2x Average
* Volume > 1.5x Average
* Volume > 2x Average

RSI Filter

* None
* RSI < 30
* RSI < 35
* RSI > 65
* RSI > 70

VWAP Filter

* None
* Above VWAP
* Below VWAP
* Distance > 0.5 ATR
* Distance > 1 ATR

⸻

Approximate Entry Variant Count

5 Strategies
× 3 Timeframes
× ATR Filters
× ADX Filters
× VIX Filters
× Volume Filters
× RSI Filters
× VWAP Filters

≈ 150,000 Variants

⸻

Shared Indicator Computation

For every completed candle:

Calculate ONCE:

* ATR
* ADX
* RSI
* VIX
* Volume Ratio
* VWAP

All variants reuse these values.

No variant recalculates indicators.

⸻

Market Data Flow

Live Feed
→ Build Candles
→ Compute Indicators Once
→ Evaluate Variants
→ Generate Trade Records

⸻

Metadata (NOT Entry Combinations)

Store for every trade:

* Instrument
* Session
* Day Of Week
* Month
* Gap Size
* Gap Direction
* Opening Range Size
* Market Structure
* Volatility Regime
* 1 Hour Trend
* Higher Timeframe Bias
* EMA20 Slope
* EMA50 Slope

These do NOT increase combination count.

They are used later for analysis.

⸻

Trade Trigger

When all conditions for a variant are TRUE:

Create Trade Record.

No exit logic is executed.

No TP.
No SL.
No Trailing.

Only entry is recorded.