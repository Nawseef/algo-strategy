"""
Execution engine for the 150K variant system.

Manages:
- Armed state (variants waiting for triggers)
- Dynamic grouping (reducing tick-level work)
- Tick trigger engine (fires trades when price crosses group levels)
- Trade recording (batch writes to DB)
"""
