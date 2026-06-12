FILE - PRODUCTION SAFETY CHECKLIST (150K VARIANT TRADING ENGINE)

⸻

1. ARCHITECTURE VALIDATION

Candle-Based Evaluation (MANDATORY)

* Variants are evaluated ONLY on candle close (5m / 15m / 30m)
* No variant evaluation exists on per-tick basis
* Indicator calculations are shared per instrument per candle
* 150,000 variants are NOT recalculated per tick

⸻

Tick Engine Design (MANDATORY)

* Tick engine uses ONLY ARMED variants
* No full variant scan happens on tick updates
* Trigger conditions are grouped (not per-variant scanning)
* Tick processing uses O(1) or grouped hash lookup logic

⸻

ARMED STATE LIFECYCLE (MANDATORY)

* IDLE → ARMED → TRIGGERED / DISARMED implemented
* ARMED variants have explicit expiry (1–5 candles max recommended)
* No variant remains ARMED indefinitely
* State cleanup runs every candle close

⸻

2. PERFORMANCE SAFETY

Candle Close Performance

* 150k variants per instrument processed within safe candle window
* Total processing time < next candle interval (5 min buffer)
* Indicator computation is executed once per instrument per candle

⸻

System Load Test

* 5 instruments tested simultaneously
* CPU usage remains stable under load (no sustained 100%)
* No degradation over multiple hours of simulation

⸻

Memory Stability

* No unbounded growth in ARMED variant storage
* No accumulation of stale grouping objects
* Memory usage remains stable over long runs (6–12 hours minimum)

⸻

3. DATABASE SAFETY

Write Pattern Rules (CRITICAL)

* No DB writes inside tick loop
* No DB writes per variant evaluation
* DB writes only at:
    * Trade creation
    * End-of-day exit simulation
    * Batch summary updates

⸻

DB Stress Validation

* Backtest of multi-day data runs without slow queries
* Indexes validated for trade queries
* No table locking during peak evaluation

⸻

4. DATA INTEGRITY

Trade Validation

* Each trade linked to exactly one variant ID
* No duplicate trades from same trigger event
* Instrument mapping is always correct

⸻

Exit Simulation Integrity

* Same trade produces identical results on re-run
* RR / SL / trailing models deterministic
* No randomness in exit calculations

⸻

5. ARMED STATE MANAGEMENT

Safety Rules

* ARMED list is bounded per candle cycle
* Expired variants are removed automatically
* Triggered variants are immediately removed from watch list

⸻

Timeout Configuration

Each strategy must have defined validity window:

* ORB → 2–3 candles
* Trend → 3–5 candles
* Mean Reversion → 1–2 candles
* VPA → event-based or 1–2 candles max

⸻

6. MARKET EDGE CASE TESTING

Gap Handling

* Large gap up tested
* Large gap down tested
* Opening range recalculated correctly after gaps

⸻

Volatility Stress

* High volatility spikes tested
* False breakout scenarios tested
* Illiquid period behavior validated

⸻

Session Behavior

* Opening session behavior validated
* Mid-day low volume stability checked
* Closing session volatility handled correctly

⸻

7. LOGGING SAFETY

Logging Rules (CRITICAL)

* No tick-level logging enabled in production
* Only trade-level logs stored
* Debug logs disabled or sampled

⸻

Disk Safety

* Log rotation enabled
* No infinite log growth allowed
* Historical logs archived or compressed

⸻

8. BACKTEST + FORWARD TEST CONSISTENCY

Determinism Check

* Same input produces identical output
* No randomness in variant evaluation logic

⸻

Cross-Validation

* Backtest results match forward test logic
* No divergence between historical and live engine behavior

⸻

9. STRESS TEST REQUIREMENTS

Full System Simulation

* 5 instruments running simultaneously
* 150,000 variants active evaluation
* 1–3 months simulated in compressed time

Must verify:

* CPU remains stable
* Memory remains stable
* DB performance remains stable
* No progressive slowdown

⸻

10. FAILURE RECOVERY

Crash Recovery

* System restarts without losing state
* ARMED state can be reconstructed from latest candle
* Trades remain consistent after restart

⸻

Data Reconstruction

* Candle history can rebuild trade path
* Exit simulation can be rerun anytime
* No dependency on volatile memory state

⸻

FINAL GO / NO-GO CONDITIONS

DO NOT deploy unless ALL are true:

* Candle evaluation is stable and bounded
* Tick engine is grouped (no brute force scanning)
* DB writes are strictly controlled
* ARMED state is bounded and cleaned
* Stress tests pass without degradation
* Memory usage remains stable over long runs
* Exit simulations are deterministic
* System recovers cleanly after restart

⸻

SYSTEM SUMMARY

This engine is safe only when it operates as:

* Batch evaluation system (candle close)
* Event-driven trigger system (ticks)
* Stateless grouping layer (runtime only)
* Post-market analytical engine (exit simulation)

NOT as:

* Continuous brute-force tick evaluator
* Real-time per-variant database engine

⸻

END OF FILE