# ZMQ Bridge Production Activation Guide

## Prerequisites

- [x] Phase 1 ZMQ code deployed (commit ff9f792, 16566f7)
- [x] pyzmq installed (`pip install pyzmq`)
- [x] Benchmark verified: P50=72us, P99=148us (12,500x faster than file IPC)
- [ ] Low-volatility trading window selected
- [ ] User actively monitoring

## Activation Steps

### 1. Start ZMQ Bridge Worker

Stop the current file-based bridge worker, then start ZMQ mode:

```powershell
# In terminal 1: ZMQ bridge worker (XAU)
python scripts/mt5_bridge_worker.py --zmq `
  --mt5-terminal-path "D:\exness\MetaTrader 5 EXNESS2\terminal64.exe" `
  --default-symbol XAUUSDc `
  --receipt-dir data/receipts `
  --journal-path data/live_trade_journal.jsonl

# In terminal 2: ZMQ bridge worker (BTC) — if BTC trading is active
python scripts/mt5_bridge_worker.py --zmq `
  --mt5-terminal-path "D:\exness\MetaTrader 5 EXNESS2\terminal64.exe" `
  --default-symbol BTCUSDc `
  --receipt-dir data_btc/receipts `
  --journal-path data_btc/live_trade_journal.jsonl `
  --zmq-order-endpoint tcp://127.0.0.1:15558 `
  --zmq-ack-endpoint tcp://127.0.0.1:15559
```

Expected output:
```
[zmq_bridge] PULL bound to tcp://127.0.0.1:5556
[zmq_bridge] PUB  bound to tcp://127.0.0.1:5557
[zmq_bridge] MT5 initialized: D:\exness\MetaTrader 5 EXNESS2\terminal64.exe
```

### 2. Activate in live.yaml

Change `configs/live.yaml` line 4:

```yaml
# Before (file IPC):
adapter:
  name: mt5

# After (ZMQ):
adapter:
  name: mt5_zmq
```

For BTC (`configs/live_btc.yaml`): same change + add zmq extensions:

```yaml
adapter:
  name: mt5_zmq
extensions:
  zmq_order_endpoint: tcp://127.0.0.1:15558
  mt5_terminal_path: D:\exness\MetaTrader 5 EXNESS2\terminal64.exe
```

### 3. Restart live_intent_loop

Restart the live trading loop. Monitor for errors in the first 3 cycles.

### 4. Verify Bridge Health

```bash
cat data/reports/mt5_bridge_health.json
# Expected: {"transport": "zmq", "mt5_connected": true, ...}

cat data_btc/reports/mt5_bridge_health.json
# Expected: same
```

### 5. Verify Orders Execute

Check trade journal for new entries with ZMQ transport:
```bash
tail -3 data/live_trade_journal.jsonl
```

## Rollback (if any issue)

1. Stop ZMQ bridge workers (Ctrl+C)
2. Change `adapter.name` back to `"mt5"` in live.yaml
3. Restart file-based bridge workers:
   ```powershell
   python scripts/mt5_bridge_worker.py --file `
     --mt5-terminal-path "D:\exness\MetaTrader 5 EXNESS2\terminal64.exe"
   ```
4. Restart live_intent_loop

## Latency Expectations

| Metric | File IPC (current) | ZMQ (target) |
|--------|-------------------|--------------|
| Order dispatch | ~1,000,000us | ~72us |
| ACK delivery | 200ms poll | <1ms push |
| CPU idle | Polling (waste) | Blocking recv (zero) |
