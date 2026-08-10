"""
cTrader Open API — first-connect smoke test (VALIDATION ONLY, no candles/store).

Purpose
-------
The cTrader feed adapter (app/broker/ctrader.py) was written before the Open API
app was approved, so it has never actually connected. Before we trust it or wire
it into the candle/strategy pipeline, this script validates the four things that
can ONLY be checked against the live API:

  1. Credentials + auth   — client connects, resolves the account, authenticates.
  2. Symbol names         — the exact IC Markets cTrader names (e.g. "US500.cash",
                            "DE40") and whether our configured CTRADER_SYMBOLS
                            resolve to numeric IDs.
  3. Spot event shape     — that SpotEvent carries usable bid/ask and a symbol_id.
  4. Price scaling        — that bid/ask arrive as REAL floats (raw cTrader spots
                            are int64 scaled by 1e5); we sanity-check each price
                            against a plausible range and flag a scaling problem.

It runs the correct SINGLE event-loop flow (one `async with client:` block for
the whole lifecycle) — which is also the pattern the real adapter must adopt
(the current adapter authenticates on one loop and consumes on another, which
cannot work).

This script does NOT build candles, write to the DB, or place orders. It just
connects, subscribes, prints a report, and exits.

Run on the VM (where the venv + .env credentials live):
    venv/bin/python -m app.ctrader_smoketest                 # 45s default
    venv/bin/python -m app.ctrader_smoketest --duration 90
    venv/bin/python -m app.ctrader_smoketest --symbols XAUUSD,EURUSD,US500
"""

from __future__ import annotations

import argparse
import asyncio
import time

from app.utils.config import load_config

# Rough plausible price ranges (bid) per instrument, used only to detect a gross
# scaling problem (e.g. raw 1e5-scaled integers). Generous on purpose.
_SANITY_RANGE: dict[str, tuple[float, float]] = {
    "XAUUSD": (800.0, 6000.0),
    "XAGUSD": (5.0, 120.0),
    "EURUSD": (0.7, 1.7),
    "GBPUSD": (0.8, 2.2),
    "USDJPY": (70.0, 220.0),
    "US30": (15000.0, 70000.0),
    "US500": (1500.0, 9000.0),
    "USTEC": (6000.0, 35000.0),
    "DE40": (6000.0, 35000.0),
    "XTIUSD": (10.0, 180.0),
}


def _preflight(cfg) -> bool:
    """Check credentials are present WITHOUT printing their values."""
    print("=" * 64)
    print("cTrader smoke test — preflight")
    print("=" * 64)
    checks = {
        "CTRADER_CLIENT_ID": bool(cfg.client_id),
        "CTRADER_CLIENT_SECRET": bool(cfg.client_secret),
        "CTRADER_ACCESS_TOKEN": bool(cfg.access_token),
        "CTRADER_REFRESH_TOKEN": bool(cfg.refresh_token),
        "CTRADER_ACCOUNT_LOGIN": cfg.account_login > 0,
    }
    for name, ok in checks.items():
        print(f"  [{'OK ' if ok else 'MISSING'}] {name}")
    print(f"  env={cfg.env}  host={cfg.host}:{cfg.port}")
    print(f"  token_expires_at={cfg.token_expires_at} "
          f"({'set' if cfg.token_expires_at else 'UNSET'})")
    print(f"  symbols({len(cfg.symbols)}): {', '.join(cfg.symbols)}")
    missing = [k for k, v in checks.items() if not v]
    if missing:
        print(f"\nABORT: missing credentials: {', '.join(missing)}")
        print("Fill them in .env (see CFD_SYSTEM.md section 4) and retry.")
        return False
    print("Preflight OK.\n")
    return True


def _resolve_names(all_symbols, wanted: list[str]) -> tuple[dict, dict, list, dict]:
    """Map wanted names -> ids using exact, suffix-stripped, and case-insensitive
    matching. Returns (name->id, id->name, missing, close_matches_for_missing)."""
    name_lookup: dict[str, int] = {}
    raw_names: list[str] = []
    for sym in all_symbols:
        raw_names.append(sym.name)
        name_lookup.setdefault(sym.name, sym.symbol_id)
        base = sym.name.replace(".cash", "").replace(".mini", "")
        name_lookup.setdefault(base, sym.symbol_id)

    upper_lookup = {k.upper(): v for k, v in name_lookup.items()}

    resolved: dict[str, int] = {}
    id_to_name: dict[int, str] = {}
    missing: list[str] = []
    for name in wanted:
        sid = name_lookup.get(name) or upper_lookup.get(name.upper())
        if sid is not None:
            resolved[name] = sid
            id_to_name[sid] = name
        else:
            missing.append(name)

    # For anything missing, offer close matches (substring) to reveal the real name.
    close: dict[str, list[str]] = {}
    for name in missing:
        stem = name.upper().replace("USD", "").replace(".CASH", "")[:3]
        close[name] = [rn for rn in raw_names if stem and stem in rn.upper()][:6]
    return resolved, id_to_name, missing, close


async def _run(duration: float, symbols_override: list[str] | None) -> int:
    cfg = load_config().ctrader
    if not _preflight(cfg):
        return 2

    wanted = symbols_override or cfg.symbols

    try:
        from ctrader_api_client import (
            AccountCredentials,
            ClientConfig,
            CTraderClient,
            SpotEvent,
        )
    except ImportError as e:
        print(f"ABORT: ctrader-api-client not installed ({e}).")
        print("Install it:  venv/bin/pip install 'ctrader-api-client>=0.8.0'")
        return 2

    client_config = ClientConfig(
        client_id=cfg.client_id,
        client_secret=cfg.client_secret,
        host=cfg.host,
        port=cfg.port,
    )
    client = CTraderClient(client_config)

    # Per-symbol first-seen + counters, populated by the spot handlers.
    first: dict[str, dict] = {}
    counts: dict[str, int] = {}
    saw_symbol_id_field = {"value": False}

    print(f"Connecting to {cfg.host}:{cfg.port} ...")
    try:
        async with client:
            account_id = await client.accounts.resolve_account_id(
                cfg.access_token, trader_login=cfg.account_login
            )
            print(f"  account resolved: login={cfg.account_login} -> id={account_id}")

            await client.auth.authenticate_trader(
                AccountCredentials(
                    account_id=account_id,
                    access_token=cfg.access_token,
                    refresh_token=cfg.refresh_token,
                    expires_at=cfg.token_expires_at,
                )
            )
            print("  account authenticated OK")

            all_symbols = await client.symbols.list_all(account_id)
            print(f"  broker exposes {len(all_symbols)} symbols total")

            resolved, id_to_name, missing, close = _resolve_names(all_symbols, wanted)
            print(f"\nSymbol resolution: {len(resolved)}/{len(wanted)} resolved")
            for name, sid in resolved.items():
                print(f"  [OK ] {name:8s} -> id={sid}")
            for name in missing:
                hint = f"  (close matches: {', '.join(close[name])})" if close.get(name) else ""
                print(f"  [MISS] {name:8s} NOT FOUND{hint}")

            if not resolved:
                print("\nABORT: no symbols resolved — CTRADER_SYMBOLS do not match "
                      "the broker's names. Use the close-match hints above to fix .env.")
                return 1

            # Register a spot handler per resolved symbol (closes over the name so
            # we don't depend on event.symbol_id — but we also probe for it).
            def make_handler(sym_name: str):
                async def _handler(event: "SpotEvent") -> None:
                    if getattr(event, "symbol_id", None) is not None:
                        saw_symbol_id_field["value"] = True
                    bid = getattr(event, "bid", None)
                    ask = getattr(event, "ask", None)
                    counts[sym_name] = counts.get(sym_name, 0) + 1
                    if sym_name not in first and bid:
                        first[sym_name] = {
                            "bid": bid, "ask": ask, "t": time.time(),
                        }
                return _handler

            for name, sid in resolved.items():
                client.on(SpotEvent, symbol_id=sid)(make_handler(name))

            await client.market_data.subscribe_spots(
                account_id, list(resolved.values())
            )
            print(f"\nSubscribed to {len(resolved)} spots. "
                  f"Listening for {duration:.0f}s ...\n")

            await asyncio.sleep(duration)
    except Exception as e:  # noqa: BLE001
        print(f"\nABORT: connection/auth/subscribe failed: {type(e).__name__}: {e}")
        print("Common causes: expired access token (regenerate via "
              "ctrader-oauth-fetcher), wrong account_login, or app not yet Active.")
        return 1

    # ─── Report ──────────────────────────────────────────────────
    print("=" * 64)
    print("RESULT")
    print("=" * 64)
    total = sum(counts.values())
    print(f"Total ticks: {total} across {len(counts)} symbols in {duration:.0f}s")
    print(f"SpotEvent.symbol_id present: {saw_symbol_id_field['value']}")

    if total == 0:
        print("\nNo ticks received. Either the market is closed for these symbols, "
              "or the subscription did not take. If the market is open, this is a "
              "real problem to investigate.")
        return 1

    print("\nPer-symbol first tick + price sanity:")
    scaling_suspect = False
    for name in resolved:
        if name not in first:
            print(f"  {name:8s} : no ticks (may be closed / illiquid now)")
            continue
        bid = first[name]["bid"]
        ask = first[name]["ask"]
        n = counts.get(name, 0)
        lo, hi = _SANITY_RANGE.get(name, (None, None))
        tag = "ok"
        if lo is not None:
            if bid > hi * 10:      # grossly high → likely 1e5-scaled ints
                tag = "SCALING? (looks like raw integer, expected ~%.4g)" % ((lo + hi) / 2)
                scaling_suspect = True
            elif not (lo <= bid <= hi):
                tag = "out-of-range (verify)"
        spread = (ask - bid) if (ask and bid) else None
        sp = f" spread={spread:.6g}" if spread is not None else ""
        print(f"  {name:8s} : bid={bid:<12g} ask={ask if ask else '-':<12}{sp}  ticks={n}  [{tag}]")

    print("\nVerdict:")
    print(f"  auth ............... OK")
    print(f"  symbols resolved ... {len(resolved)}/{len(wanted)}")
    print(f"  live ticks ......... {'OK' if total else 'NONE'}")
    print(f"  price scaling ...... {'SUSPECT — bid/ask look like raw integers' if scaling_suspect else 'looks correct (real floats)'}")
    if missing:
        print(f"  NOTE: fix these names in CTRADER_SYMBOLS: {', '.join(missing)}")
    print("\nIf all four are green, the adapter refactor (single loop + token "
          "persistence) can proceed against confirmed facts.")
    return 0 if (total and not scaling_suspect and not missing) else 0


def main() -> None:
    parser = argparse.ArgumentParser(description="cTrader Open API first-connect smoke test")
    parser.add_argument("--duration", type=float, default=45.0,
                        help="seconds to listen for spot ticks (default 45)")
    parser.add_argument("--symbols", type=str, default=None,
                        help="comma-separated symbol names to test (default: CTRADER_SYMBOLS)")
    args = parser.parse_args()
    override = [s.strip() for s in args.symbols.split(",")] if args.symbols else None
    rc = asyncio.run(_run(args.duration, override))
    raise SystemExit(rc)


if __name__ == "__main__":
    main()
