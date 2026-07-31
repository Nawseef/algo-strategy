# MT5 Live Data Feed — Complete Setup Record

> **What this is:** A self-healing MetaTrader 5 (MT5) live market-data feed running on an
> Oracle Cloud 1 GB x86 VM, exposing a Python API (RPyC) that a separate machine (the 12 GB
> ARM VM) will consume to build candles and run strategies. This document records exactly
> what was built, how, and every credential/path needed to operate it.

Last updated: 30 July 2026

---

## 1. Goal & Architecture

We are migrating an Indian-market (NSE, via Groww) algo system to trade **CFDs** (gold,
silver, oil, FX, indices) via **IC Markets** using **MetaTrader 5**.

**This VM is ONLY a data/execution bridge.** All heavy work (research, strategy, candle
building, DB) runs on the *other* 12 GB ARM VM.

```
┌─────────────────────────────────────────────┐
│  Oracle x86 "Micro" VM (1 vCPU / 1 GB RAM)   │   <-- THIS box (144.24.154.233)
│                                             │
│  Docker container (gmag11/metatrader5_vnc): │
│    Wine + MT5 terminal (IC Markets demo)    │
│    Wine Python + MetaTrader5 lib            │
│    mt5linux RPyC server  ->  port 8001      │
│    KasmVNC web UI        ->  port 3000      │
│                                             │
│  Host: cron watchdog, heartbeat, alerts     │
└──────────────────┬──────────────────────────┘
                   │  (future) SSH tunnel on port 22
┌──────────────────┴──────────────────────────┐
│  Oracle ARM VM (2 vCPU / 12 GB) — NOT built  │
│    mt5linux client -> pulls ticks from 8001  │
│    CandleBuilder -> strategy engine -> DB    │
└─────────────────────────────────────────────┘
```

---

## 2. Access & Credentials

> **SECURITY:** These are demo/non-production credentials. Regenerate the MT5 demo password
> in the IC Markets portal before ever using a funded account. Never commit funded creds.

### The x86 feed VM
- **Public IP:** `144.24.154.233`
- **SSH:** `ssh -i ~/Proj/algo-strategy/ssh-key-*.key ubuntu@144.24.154.233`
- **OS:** Ubuntu 22.04.5 LTS, x86_64, Oracle Cloud Always-Free micro
- **Specs:** 1 vCPU (2 threads), 956 MB RAM, 3 GB swap, 45 GB disk (~26 GB free)

### IC Markets MT5 demo account
- **Login:** `52946528`
- **Password:** `vLV&A8som1Rp3l`
- **Server:** `ICMarketsSC-Demo`
- Stored on VM at (chmod 600): `/config/.wine/drive_c/mt5login.ini` inside the container,
  and host backup `/home/ubuntu/.mt5login.ini.bak`

### Web VNC (to see the MT5 GUI in a browser)
- Port `3000` (not exposed publicly — reach via SSH tunnel)
- User `trader` / Password `Trade2026x`
- Access: `ssh -i <key> -L 3000:localhost:3000 -N ubuntu@144.24.154.233` then browse `http://localhost:3000`

### Python API (RPyC / mt5linux)
- Port `8001` (not exposed publicly — Oracle security list allows only port 22)
- rpyc **5.2.3** on both ends (version must match), Wine Python is 3.9 (32-bit)

### Telegram alerts (NEW bot)
- Bot token: `8522382590:AAHadfq6CFDDue4L_ELyYixr58Nvn9tewsw`
- Chat ID: `1655289022`
- Config on VM (chmod 600): `/home/ubuntu/.mt5_telegram.conf`

---

## 3. The 10 Instruments (exact IC Markets symbol names)

| # | Symbol | Instrument |
|---|--------|-----------|
| 1 | XAUUSD | Gold |
| 2 | XAGUSD | Silver |
| 3 | EURUSD | Euro |
| 4 | GBPUSD | Pound |
| 5 | USDJPY | Yen |
| 6 | US30   | Dow Jones |
| 7 | US500  | S&P 500 |
| 8 | USTEC  | Nasdaq 100 |
| 9 | DE40   | DAX |
| 10 | XTIUSD | WTI Crude Oil |

All confirmed live with `trade_mode=4` (full trading). Symbols must be re-selected via
`symbol_select(name, True)` on each client connect (Market Watch does not reliably persist
across restarts).

---

## 4. How It Was Built (chronological, with the "why")

### 4.1 Why Docker (not native Wine)
Native Wine install failed on two hard walls: (a) `wineboot` timed out on 1 GB RAM until we
set `vm.mmap_min_addr=0` + `vm.overcommit_memory=1`; (b) MT5's installer aborts with
"a debugger has been found" under Wine. The **gmag11/metatrader5_vnc** Docker image ships a
pre-built Wine prefix + MT5, sidestepping both. This is the standard community solution.

### 4.2 Base OS tuning (host)
```bash
# 3 GB swap (persistent)
sudo fallocate -l 3G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
# swappiness 60 (persistent)
echo 'vm.swappiness=60' | sudo tee /etc/sysctl.d/99-mt5.conf
```

### 4.3 Docker + container
```bash
sudo apt-get install -y docker.io
sudo systemctl enable --now docker
sudo docker run -d --name mt5 --restart unless-stopped \
  -p 3000:3000 -p 8001:8001 \
  -v /home/ubuntu/mt5config:/config \
  -e CUSTOM_USER=trader -e PASSWORD=Trade2026x \
  gmag11/metatrader5_vnc
# memory cap (host-freeze protection): 750 MB RAM, 2 GB total
sudo docker update --memory 750m --memory-swap 2000m mt5
```
- Docker log rotation via `/etc/docker/daemon.json` (`max-size 10m`, `max-file 3`).

### 4.4 Auto-login (scripted, deterministic)
MT5's GUI-saved login proved fragile. Instead we pass a startup config to `terminal64.exe`:
- File `/config/.wine/drive_c/mt5login.ini` contains `[Common] Login/Password/Server` +
  `[Charts] MaxBars=5000`.
- The container's `/Metatrader/start.sh` (line ~74) was patched to launch:
  `terminal64.exe "/config:C:\mt5login.ini"`.
- Result: MT5 logs into IC Markets automatically on every start, no GUI needed.

### 4.5 The mt5linux server (the real fix)
The image's own server launch is **broken** with mt5linux 1.0.3 (uses removed `-w` flag →
exit 2). We disabled that line and made the **host watchdog** the sole owner of the server,
launched the correct way *inside Wine Python*:
```bash
wine python -m mt5linux --host 0.0.0.0 -p 8001
```
Also fixed: Wine Python numpy was 2.0.2 (breaks MetaTrader5 5.0.36) → downgraded to `1.26.4`.

### 4.6 Key gotchas solved
- **kernel32 load fail / wineboot timeout** → these hit the *abandoned native Wine attempt*
  and were worked around with `vm.mmap_min_addr=0` + `vm.overcommit_memory=1`.
  **IMPORTANT:** the Docker setup does **NOT** need these. They were set at runtime only
  (never persisted) and after the reboot test they reset to defaults
  (`vm.mmap_min_addr=65536`, `vm.overcommit_memory=0`) yet the feed recovered perfectly.
  So do **not** chase these settings for the Docker path — only `vm.swappiness=60` is
  persisted (in `/etc/sysctl.d/99-mt5.conf`) and that's all that matters.
- **Wine socket cache (WinError 10048)** → after killing the server, must wait ~30 s for
  wineserver to release port 8001 before relaunching. The watchdog does this.
- **initialize() hangs / spawns extra terminals** → root cause was MT5 not logged in;
  fixed by scripted auto-login. Never run repeated `initialize()` tests against a
  logged-out terminal (it launches duplicate terminals).
- **rpyc version mismatch** → client must be `rpyc==5.2.3` to match Wine Python.
- **MT5 broker server time is GMT+3** — tick `time`/`time_msc` carry that offset. Real UTC =
  server − 3 h. IST = UTC + 5:30.

---

## 5. Files on the VM (`/home/ubuntu/`)

### Operational (keep — these run the system)
| File | Purpose |
|------|---------|
| `mt5_watchdog.sh` | **v7** — runs every minute via cron. Ensures container up, self-heals start.sh login patch after recreate, ensures port 8001 up (clean relaunch w/ socket-release), deep health check, escalates to full restart after 3 fails, Telegram alerts. |
| `mt5_health.py` | Deep health check: RPyC + initialize + broker `connected` (only enforced when forex market open) + account present. Exit 0 healthy. |
| `mt5_heartbeat.sh` | Daily Telegram status incl. memory (container/host/terminal64). |
| `mt5_weekly_restart.sh` | Sunday memory-hygiene container restart. |
| `mt5_patch_startsh.sh` | Idempotent start.sh auto-login patch (used by watchdog self-heal). |
| `.mt5_telegram.conf` | Bot token + chat id (chmod 600). |
| `.mt5login.ini.bak` | Backup of MT5 login config for recreate self-heal (chmod 600). |
| `.mt5_launch_ts`, `.mt5_restart_ts`, `.mt5_fail_count`, `.mt5_alerted_ts` | Watchdog state files. |
| `mt5_watchdog.log` | Watchdog activity log (logrotate’d weekly). |

### Diagnostic (handy, safe to keep or delete)
`mt5_test.py` (quick feed check), `mt5_health.py`, `mt5_resolve.py` (resolve/select the 10),
`mt5_metals.py`, `mt5_tickrate.py`, `mt5_burst.py` (batch-pull benchmark),
`mt5_loadtest.py` (1/sec consumer simulation), `mt5_maxbars.py`, `mt5_time.py`,
`mt5_diag.py`, `mt5_symbols.py`, `mt5_groups.py`, `mt5_final10.py`, `mt5_listsyms.py`.

### Diagnostic Python venv (on the feed VM)
The diagnostic scripts run from a venv at `/home/ubuntu/mt5test/` (Linux Python 3.10):
- Contains `mt5linux 1.0.3` + `rpyc 5.2.3` (versions **must** match the Wine Python side).
- Recreate if ever lost:
  ```bash
  python3 -m venv ~/mt5test
  ~/mt5test/bin/pip install mt5linux 'rpyc==5.2.3'
  ```
- Run anything with `~/mt5test/bin/python ~/mt5_test.py`.
- (Inside the container, the Wine Python is 3.9 32-bit at
  `C:\Program Files (x86)\Python39-32`, with `MetaTrader5==5.0.36`, `mt5linux 1.0.3`,
  `rpyc 5.2.3`, `numpy 1.26.4`.)

### Cron (`/etc/cron.d/`)
```
# mt5-watchdog
* * * * * root /home/ubuntu/mt5_watchdog.sh
# mt5-extra
30 3 * * * root /home/ubuntu/mt5_heartbeat.sh        # daily 09:00 IST
0 8 * * 0 root /home/ubuntu/mt5_weekly_restart.sh    # Sunday 13:30 IST
```

---

## 6. Resilience — what self-heals (all tested)

| Failure | Recovery |
|---------|----------|
| VM reboot | Docker + cron enabled on boot; swap in fstab. **Tested** — full cold recovery ~6-10 min. |
| Container crash | `--restart unless-stopped` |
| RPyC server dies | Watchdog relaunch (socket-release aware) |
| Broker disconnect / zombie | Deep health check → relaunch → escalate to restart |
| MT5 terminal crash | Deep health fails → escalate to full container restart after 3 fails |
| Container recreate | Watchdog self-heals login patch + restores login ini |
| Memory leak / host freeze | 750 MB container cap → OOMs container only → auto-restart |
| Silent failure | Daily heartbeat (silence = problem) + action alerts |
| Weekend (no ticks) | Health check ignores tick freshness; uses `connected` only when market open |
| Log/disk growth | Docker log rotation + watchdog logrotate |

---

## 7. Verified Performance (measured, not assumed)
- Normal load: ~29 ticks/sec total across 10 symbols; 1 batched pull/sec.
- Under 1/sec consumer load: container ~245 MB RAM (flat), CPU peaks ~165% of 200% ceiling.
- Batch pulls are cheap: **5,418 ticks in 80 ms**, 44,983 ticks in 1.2 s (memory flat).
- Conclusion: 1 vCPU / 1 GB box comfortably handles the feed + a 1/sec batched consumer,
  including volatility bursts — **as long as the consumer uses `copy_ticks_range` with a
  cursor, NOT one-tick-per-call polling.**

---

## 8. Common operations (cheat sheet)

```bash
# SSH in
ssh -i ~/Proj/algo-strategy/ssh-key-*.key ubuntu@144.24.154.233

# quick feed check
~/mt5test/bin/python ~/mt5_test.py

# deep health
~/mt5test/bin/python ~/mt5_health.py

# container status / logs / stats
sudo docker ps
sudo docker stats --no-stream
tail -f ~/mt5_watchdog.log

# see MT5 GUI (from your Mac):
ssh -i ~/Proj/algo-strategy/ssh-key-*.key -L 3000:localhost:3000 -N ubuntu@144.24.154.233
#   then browse http://localhost:3000  (trader / Trade2026x)

# manual server restart (if ever needed)
sudo docker exec -u abc mt5 bash -c 'pkill -9 -f "mt5linux --host"; sleep 30'
sudo docker exec -d -u abc mt5 bash -c 'export WINEPREFIX=/config/.wine; export WINEDEBUG=-all; exec wine python -m mt5linux --host 0.0.0.0 -p 8001'
```

### Change the MT5 login / password / server (e.g., after demo expiry or server rename)
```bash
# 1. edit BOTH the live config and the host backup with new Login/Password/Server:
sudo docker exec -it mt5 bash -c 'cat > /config/.wine/drive_c/mt5login.ini' <<'INI'
[Common]
Login=<NEW_LOGIN>
Password=<NEW_PASSWORD>
Server=<NEW_SERVER>
KeepPrivate=1
NewsEnable=false

[Charts]
MaxBars=5000
PrintColor=false
SaveDeleted=false
INI
sudo docker exec mt5 chmod 600 /config/.wine/drive_c/mt5login.ini
# update host backup too (used by watchdog self-heal on recreate):
sudo cp <new ini> /home/ubuntu/.mt5login.ini.bak   # or edit in place, keep chmod 600
# 2. restart so MT5 picks it up:
sudo docker restart mt5
# 3. wait ~5-6 min, verify:
~/mt5test/bin/python ~/mt5_health.py    # expect HEALTHY ... connected=True
```

### Change Telegram alert bot/chat
Edit `/home/ubuntu/.mt5_telegram.conf` (`TG_TOKEN=`, `TG_CHATS=` comma-separated), chmod 600.
All scripts read from this one file.

### Re-apply the memory cap (only needed after a full container RECREATE, not restart)
```bash
sudo docker update --memory 750m --memory-swap 2000m mt5
```

---

## 9. Known residual gaps (not blockers)
1. **Consumer not built** — the ARM VM does not yet pull data. This is the next task.
2. **Memory over days unproven** — cap guarantees safety; heartbeat now reports memory so
   you can confirm the max-bars cap keeps it flat over a week.
3. **Memory limit not in recreate self-heal** — login self-heals on recreate, the 750 MB cap
   does not (re-apply `docker update --memory 750m --memory-swap 2000m mt5`).
4. **Server rename** — if IC Markets renames the demo server, auto-login breaks; the health
   check will alert (detect-only, manual fix).
5. **Order execution untouched** — `trade_allowed=False`; enabling AutoTrading + order safety
   is future (Option B).
6. **Single points of failure** — one VM, one demo account. Fine for demo/paper, not funded.
