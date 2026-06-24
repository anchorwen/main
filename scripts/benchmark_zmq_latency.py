#!/usr/bin/env python
"""ZeroMQ vs File IPC latency benchmark.

Measures round-trip latency for:
  - ZMQ PUSH/PULL + PUB/SUB (Phase 1 socket bridge)
  - File-based IPC (current production, for comparison)

Usage:
  python scripts/benchmark_zmq_latency.py --rounds 1000
"""

from __future__ import annotations

import argparse
import json
import statistics
import tempfile
import threading
import time
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="benchmark_zmq_latency")
    p.add_argument("--rounds", type=int, default=500)
    p.add_argument("--order-endpoint", default="tcp://127.0.0.1:15556")
    p.add_argument("--ack-endpoint", default="tcp://127.0.0.1:15557")
    return p


def bench_zmq(rounds: int, order_ep: str, ack_ep: str) -> dict:
    """Benchmark ZMQ PUSH/PULL + PUB/SUB round-trip."""
    import zmq

    ctx = zmq.Context()  # type: ignore[attr-defined]
    latencies: list[float] = []
    ready = threading.Event()

    def worker():
        pull = ctx.socket(zmq.PULL)  # type: ignore[attr-defined]
        pull.bind(order_ep)
        pub = ctx.socket(zmq.PUB)  # type: ignore[attr-defined]
        pub.bind(ack_ep)
        ready.set()
        while True:
            raw = pull.recv_string()
            msg = json.loads(raw)
            if msg.get("_stop"):
                break
            msg_id = msg["envelope"]["message_id"]
            ack = json.dumps({"message_id": msg_id, "ack_status": "accepted"})
            pub.send_string(f"ack {ack}")
        pull.close()
        pub.close()

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    ready.wait(timeout=2.0)

    # Allow SUB socket to connect (ZMQ slow-joiner mitigation)
    time.sleep(0.05)

    push = ctx.socket(zmq.PUSH)  # type: ignore[attr-defined]
    push.connect(order_ep)
    sub = ctx.socket(zmq.SUB)  # type: ignore[attr-defined]
    sub.connect(ack_ep)
    sub.setsockopt_string(zmq.SUBSCRIBE, "ack")  # type: ignore[attr-defined]
    time.sleep(0.05)  # connection warm-up

    for i in range(rounds):
        msg_id = f"bench_{i}"
        envelope = {"envelope": {"message_id": msg_id}}
        t0 = time.perf_counter()
        push.send_string(json.dumps(envelope, separators=(",", ":")))
        sub.recv_string()  # block until ACK
        t1 = time.perf_counter()
        latencies.append((t1 - t0) * 1_000_000)  # microseconds

    # Stop worker
    push.send_string(json.dumps({"_stop": True, "envelope": {"message_id": "stop"}}))
    push.close()
    sub.close()
    ctx.term()

    arr = sorted(latencies)
    return {
        "transport": "zmq",
        "rounds": rounds,
        "p50_us": round(statistics.median(arr), 1),
        "p99_us": round(arr[int(rounds * 0.99)], 1),
        "mean_us": round(statistics.mean(arr), 1),
        "min_us": round(arr[0], 1),
        "max_us": round(arr[-1], 1),
    }


def bench_file(rounds: int) -> dict:
    """Benchmark file-based IPC (write .json + poll + read)."""
    latencies: list[float] = []
    tmpdir = Path(tempfile.mkdtemp(prefix="zmq_bench_"))
    outbox = tmpdir / "outbox"
    receipts = tmpdir / "receipts"
    outbox.mkdir(parents=True)
    receipts.mkdir(parents=True)

    for i in range(rounds):
        msg_id = f"bench_{i}"
        payload = json.dumps({"envelope": {"message_id": msg_id}})

        t0 = time.perf_counter()
        # Write outbox file
        outbox_file = outbox / f"{msg_id}.mt5.json"
        outbox_file.write_text(payload, encoding="utf-8")

        # Simulate 1s poll interval (bridge worker)
        time.sleep(0.001)  # best-case: 1ms poll

        # Write receipt
        receipt_file = receipts / f"{msg_id}.ack.json"
        receipt_file.write_text(
            json.dumps({"message_id": msg_id, "ack_status": "accepted"}), encoding="utf-8"
        )

        # Simulate 200ms poll interval (ACK consumer)
        time.sleep(0.001)  # best-case: 1ms poll

        t1 = time.perf_counter()
        latencies.append((t1 - t0) * 1_000_000)

    # Cleanup
    for f in outbox.iterdir():
        f.unlink()
    for f in receipts.iterdir():
        f.unlink()
    outbox.rmdir()
    receipts.rmdir()
    tmpdir.rmdir()

    arr = sorted(latencies)
    return {
        "transport": "file",
        "rounds": rounds,
        "p50_us": round(statistics.median(arr), 1),
        "p99_us": round(arr[int(rounds * 0.99)], 1),
        "mean_us": round(statistics.mean(arr), 1),
        "min_us": round(arr[0], 1),
        "max_us": round(arr[-1], 1),
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    print(f"Benchmark: {args.rounds} rounds\n")

    # ZMQ
    try:
        zmq_result = bench_zmq(args.rounds, args.order_endpoint, args.ack_endpoint)
        print(
            f"ZMQ  PUSH/PULL+PUB/SUB  P50={zmq_result['p50_us']:.0f}us  "
            f"P99={zmq_result['p99_us']:.0f}us  mean={zmq_result['mean_us']:.0f}us"
        )
    except (RuntimeError, ValueError, KeyError, TypeError, OSError) as exc:  # BLE001:FOG
        print(f"ZMQ  SKIP: {exc}")
        zmq_result = None
    # File (simulated best-case — real production adds 1s polling)
    file_result = bench_file(max(10, args.rounds // 10))  # fewer rounds (slow)
    print(
        f"File write+poll+read       P50={file_result['p50_us']:.0f}us  "
        f"P99={file_result['p99_us']:.0f}us  mean={file_result['mean_us']:.0f}us"
    )

    # Real production file latency (with 1s polling)
    real_file_mean = 1_000_000  # 1 second polling + file I/O
    print(f"File production (1s poll)  mean={real_file_mean:.0f}us  (estimated)")

    if zmq_result:
        speedup = real_file_mean / zmq_result["mean_us"]
        print(f"\n→ ZMQ is {speedup:.0f}x faster than production file IPC")
        if zmq_result["p99_us"] < 5000:
            print(f"  P99={zmq_result['p99_us']:.0f}us < 5ms target [PASS]")
        else:
            print(f"  P99={zmq_result['p99_us']:.0f}us > 5ms target [WARN]")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
