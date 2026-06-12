FILE 2 - DATABASE DESIGN AND EXIT ENGINE

Table 1 - Trades

One row per triggered trade.

Fields:

Trade_ID
Variant_ID

Strategy
Timeframe
Instrument

Direction

Entry_Time
Entry_Price

ATR_Entry
ADX_Entry
RSI_Entry
VIX_Entry
Volume_Ratio_Entry

Gap_Size
Gap_Direction

Session
Day_Of_Week
Month

Market_Structure
Volatility_Regime

HTF_Trend_1H

EMA20_Slope
EMA50_Slope

No exit information is stored here.

⸻

Temporary Market Data

During market hours:

Store current session candles.

Examples:

NIFTY 5m candles
BANKNIFTY 5m candles
GOLD candles
BTC candles

Temporary only.

Used to reconstruct trade paths.

Can be deleted after exit processing.

⸻

End Of Day Exit Engine

For every trade:

Load:

Entry_Time
Instrument

Load candles:

Entry_Time → Market Close

Generate trade path.

⸻

Exit Experiments

Run all exits against same path.

Examples:

Risk Reward

RR1
RR1.5
RR2
RR2.5
RR3
RR5
RR10

Stop Loss Models

ATR Stop
Swing Stop
Fixed Stop

Trailing Models

ATR Trail
EMA Trail
Swing Trail

Partial Exit Models

Partial A
Partial B
Partial C

⸻

Table 2 - Exit Results

One row per trade.

Fields:

Trade_ID

RR1_Result
RR1.5_Result
RR2_Result
RR2.5_Result
RR3_Result
RR5_Result
RR10_Result

ATR_Stop_Result
Swing_Stop_Result
Fixed_Stop_Result

ATR_Trail_Result
EMA_Trail_Result
Swing_Trail_Result

Partial_A_Result
Partial_B_Result
Partial_C_Result

MFE
MAE

Best_Exit_Model
Best_PnL

Worst_Exit_Model
Worst_PnL

Only one row per trade.

Approximately:

750 Trades

750 Exit Rows

Not 75,000 rows.

⸻

Data Retention

After exit experiments:

Temporary trade paths may be deleted.

Permanent storage:

Trades Table
Exit Results Table

Trade paths are recreated later if needed from historical candles.