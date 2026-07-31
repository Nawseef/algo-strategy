#!/usr/bin/env node
/*
 * fetch.js — thin dukascopy-node wrapper for one instrument + date range.
 *
 * Fetches 5-minute BID OHLCV candles (flats ignored, so output looks like our
 * tick-built live candles) and writes them as a JSON array to --out.
 *
 * Output shape (array format): [[timestampMsUTC, open, high, low, close, volume], ...]
 * Timestamps are epoch-ms in UTC (bar OPEN time). `to` is EXCLUSIVE.
 *
 * Usage:
 *   node fetch.js --instrument eurusd --from 2020-06-01 --to 2020-07-01 --out /tmp/x.json
 *
 * Optional:
 *   --timeframe m5      (default m5)
 *   --price bid         (default bid)
 *   --cache <dir>       (default ./.dukascache)
 *   --batch <n>         (default 10 concurrent url fetches)
 *   --pause <ms>        (default 100 ms between batches)
 *   --retries <n>       (default 5 network retries per url)
 *
 * Exit codes: 0 = ok (may write "[]" for a closed period), non-zero = hard error.
 */

const fs = require('fs');
const path = require('path');
const { getHistoricalRates } = require('dukascopy-node');

function parseArgs(argv) {
  const out = {};
  for (let i = 2; i < argv.length; i++) {
    const a = argv[i];
    if (a.startsWith('--')) {
      const key = a.slice(2);
      const val = argv[i + 1] && !argv[i + 1].startsWith('--') ? argv[++i] : 'true';
      out[key] = val;
    }
  }
  return out;
}

async function main() {
  const args = parseArgs(process.argv);

  const instrument = args.instrument;
  const from = args.from;
  const to = args.to;
  const outPath = args.out;

  if (!instrument || !from || !to || !outPath) {
    console.error('ERROR: --instrument, --from, --to and --out are all required');
    process.exit(2);
  }

  const timeframe = args.timeframe || 'm5';
  const priceType = args.price || 'bid';
  const cacheFolderPath = args.cache || path.join(__dirname, '.dukascache');
  // Gentle defaults: Dukascopy rate-limits (HTTP 429) aggressive bursts. Low
  // concurrency + patient retries with backoff keep a long multi-year pull from
  // tripping the limiter. We're backgrounded, so throughput isn't the priority.
  const batchSize = parseInt(args.batch || '5', 10);
  const pauseBetweenBatchesMs = parseInt(args.pause || '300', 10);
  const retryCount = parseInt(args.retries || '8', 10);
  const pauseBetweenRetriesMs = parseInt(args.retrypause || '3000', 10);

  let data;
  try {
    data = await getHistoricalRates({
      instrument,
      dates: { from: new Date(from), to: new Date(to) }, // `to` is exclusive
      timeframe,
      priceType,
      utcOffset: 0, // keep timestamps in UTC
      volumes: true, // include volume column
      ignoreFlats: true, // drop synthetic no-trade candles -> matches live feed
      format: 'array',
      useCache: true,
      cacheFolderPath,
      batchSize,
      pauseBetweenBatchesMs,
      retryCount,
      pauseBetweenRetriesMs, // backoff between retries (helps ride out HTTP 429)
      retryOnEmpty: false, // an empty period (weekend/holiday) is legitimate
      failAfterRetryCount: true, // after exhausting retries on a real network error, fail loud
    });
  } catch (err) {
    const msg = err && err.validationErrors ? JSON.stringify(err.validationErrors) : (err && err.message) || String(err);
    console.error('ERROR: dukascopy fetch failed for ' + instrument + ' ' + from + '..' + to + ': ' + msg);
    process.exit(1);
  }

  const rows = Array.isArray(data) ? data : [];
  // Atomic-ish write: temp file then rename, so a partial write is never loaded.
  const tmp = outPath + '.tmp';
  fs.writeFileSync(tmp, JSON.stringify(rows));
  fs.renameSync(tmp, outPath);

  console.error('OK: ' + instrument + ' ' + from + '..' + to + ' -> ' + rows.length + ' candles');
  process.exit(0);
}

main();
