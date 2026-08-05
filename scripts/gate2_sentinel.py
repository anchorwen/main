"""Gate 2 Sentinel — daily OFI accumulation monitor with DingTalk alerts.

IC ruling 2 (Red Gap 1, 2026-08-05): mount inspect_ofi_history.py into
automation so the Gate 2 progress bar (687 -> 711 -> ...) is seen every day
and a stalled accumulation triggers an immediate Sev1 alert.  Deployment:
Windows Task Scheduler (schtasks), daily 12:30 local.  Full design in
docs/runbooks/gate2_sentinel_deployment.md.

Iron Law #11: every number printed here comes from
scripts.inspect_ofi_history.inspect() — the sole legal evidence source.

Design notes:
- Market schedule (IC 2026-08-05 correction): the operating broker halts BTC
  over the weekend AND holds a ~1h daily maintenance break.  Stall detection
  is therefore skipped on Saturday/Sunday (weekend closure window), and
  expected daily accumulation is ~23 H1 windows, not 24.  ETA uses the
  historical average (h1_windows / span_days) which already bakes in both
  closure factors — never the single previous-day delta (skewed when it
  spans a weekend or the daily halt).
- Reuses scripts.alert_dispatcher.dispatch_alert() for DingTalk push
  (cooling + sanitization built in).  Zero new alert logic.
- State file data_btc/state/gate2_sentinel.json records the previous run for
  delta detection.  It is gitignored (data_btc/ is runtime data).

Decision machine (priority order, idempotent):
  1. ofi_history.jsonl missing            -> Sev1  data missing
  2. n_records < prev_n_records           -> Sev1  record rollback (truncated)
  3. gate2_retrain.ready (or --force)     -> INFO  ready (once)
  4. h1_windows == prev AND elapsed >=22h AND today NOT weekend
     -> Sev1  stall (full daily cycle; weekend closure + ~1h daily halt
     factored in — same-day reruns and closure windows are never a stall)
  5. 1000 - h1_windows <= 48              -> Sev2  nearing deadline
  6. otherwise                            -> OK    normal accumulation

Exit codes: 0 = OK/INFO, 1 = Sev1, 2 = Sev2 (mirrors highest-severity state).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

# ── Repo-root bootstrap so `from scripts.X import ...` resolves under any
# ── launch context (schtasks cwd = System32, direct run, -m, pytest). ──
BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from scripts.alert_dispatcher import AlertCard, dispatch_alert  # noqa: E402
from scripts.inspect_ofi_history import inspect  # noqa: E402

# Gate 2 threshold (must match inspect_ofi_history._RETRAIN_H1_THRESHOLD).
_H1_THRESHOLD = 1_000
# "Near deadline" window: warn when fewer than this many H1 windows remain.
_NEAR_DEADLINE_WINDOW = 48


def _resolve_data_dir(raw: str) -> Path:
    """Resolve --data-dir against repo root so relative paths work anywhere."""
    p = Path(raw)
    if not p.is_absolute():
        p = BASE / p
    return p


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, TypeError, OSError):
        return {}


def _save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _in_weekend_closure(now: datetime) -> bool:
    """True on Saturday/Sunday — the broker's weekend market closure window.

    IC 2026-08-05: even though BTC crypto is nominally 24/7, the operating
    broker halts BTC over the weekend (plus a ~1h daily maintenance break).
    A weekend run therefore never fires the stall alarm; the Monday run
    catches any genuine Fri->Mon stall (a full trading period yields
    ~20+ windows, so delta==0 across the weekend is still a real outage).
    """
    return now.weekday() >= 5


def _eta_days(stats: dict[str, Any], h1_windows: int) -> float | None:
    """ETA to 1000 windows from the historical average accumulation rate.

    Uses inspect()'s own span_days/window count (Iron Law #11 — script stdout
    is the evidence source).  The historical average already bakes in the
    weekend closure and the ~1h daily halt (~23 windows/day), so it is robust
    to a single-day delta that happens to span a closure window.
    """
    span = stats.get("span_days")
    if not isinstance(span, int | float) or span <= 1.0 or h1_windows <= 0:
        return None
    rate_per_day = h1_windows / span
    if rate_per_day <= 0:
        return None
    return max(_H1_THRESHOLD - h1_windows, 0) / rate_per_day


def _build_status_line(stats: dict[str, Any], eta: float | None, now: datetime) -> str:
    """ASCII-only progress line (Windows GBK console cannot print U+2713)."""
    n = stats.get("distinct_h1_windows", 0)
    line = f"Gate 2: {n}/{_H1_THRESHOLD} H1 windows" f" ({max(_H1_THRESHOLD - n, 0)} to go)"
    if eta is not None:
        eta_date = (now + timedelta(days=eta)).strftime("%m-%d")
        line += f" | ETA ~{eta_date} ({eta:.1f}d)"
    return line


def run(data_dir: Path, *, dry_run: bool = False, force_ready: bool = False) -> int:
    stats = inspect(data_dir)
    state_path = data_dir / "state" / "gate2_sentinel.json"
    state = _load_state(state_path)
    now = datetime.now(UTC).replace(tzinfo=None)

    hist_path = Path(stats["history_path"])
    n_records = int(stats.get("n_records", 0))
    h1_windows = int(stats.get("distinct_h1_windows", 0))
    prev_n = state.get("prev_n_records")
    prev_h1 = state.get("prev_h1_windows")
    gate_ready = bool(stats.get("gate2_retrain", {}).get("ready", False)) or force_ready

    # Elapsed time since the previous run (drives the stall gate).  H1 windows
    # advance once per hour, so an unchanged count is NOT a stall unless a
    # full daily cycle has passed (>=22h tolerance for schedule jitter).
    elapsed_h: float | None = None
    last_run_raw = state.get("last_run")
    if isinstance(last_run_raw, str):
        try:
            elapsed_h = (now - datetime.fromisoformat(last_run_raw)).total_seconds() / 3600.0
        except (ValueError, TypeError):
            elapsed_h = None

    eta = _eta_days(stats, h1_windows)
    status_line = _build_status_line(stats, eta, now)

    # ── Decision machine (priority order per deployment doc §3.3) ──
    alert: AlertCard | None = None
    status = "OK"
    exit_code = 0

    if n_records == 0 and not hist_path.exists():
        status = "SEV1_DATA_MISSING"
        exit_code = 1
        alert = AlertCard(
            source="gate2",
            title="OFI Gate 2: 数据缺失 — ofi_history.jsonl 消失",
            severity="Sev1",
            checks={"ofi_history_presence": "Sev1"},
            details={
                "h1_windows": h1_windows,
                "n_records": n_records,
                "verdict": stats.get("verdict", "NO_DATA"),
            },
        )
    elif isinstance(prev_n, int) and n_records < prev_n:
        status = "SEV1_RECORD_ROLLBACK"
        exit_code = 1
        alert = AlertCard(
            source="gate2",
            title="OFI Gate 2: 记录数回落 — 文件可能被截断/回写",
            severity="Sev1",
            checks={"ofi_history_integrity": "Sev1"},
            details={
                "h1_windows": h1_windows,
                "n_records": n_records,
                "prev_n_records": prev_n,
                "verdict": stats.get("verdict", ""),
            },
        )
    elif gate_ready:
        status = "GATE2_READY"
        alert = AlertCard(
            source="gate2",
            title="OFI Gate 2 READY — 触发 8/19 决战 Runbook 阶段 3",
            severity="OK",
            checks={"gate2_retrain": "OK"},
            details={
                "h1_windows": h1_windows,
                "n_records": n_records,
                "verdict": stats.get("verdict", ""),
            },
        )
    elif (
        not _in_weekend_closure(now)
        and isinstance(prev_h1, int)
        and prev_h1 > 0
        and h1_windows == prev_h1
        and elapsed_h is not None
        and elapsed_h >= 22.0
    ):
        status = "SEV1_STALL"
        exit_code = 1
        alert = AlertCard(
            source="gate2",
            title="OFI Gate 2: OFI 采集停滞 24h (MT5/bridge 掉线?)",
            severity="Sev1",
            checks={"ofi_collection": "Sev1"},
            details={
                "h1_windows": h1_windows,
                "n_records": n_records,
                "prev_h1_windows": prev_h1,
                "verdict": stats.get("verdict", ""),
            },
        )
    elif (_H1_THRESHOLD - h1_windows) <= _NEAR_DEADLINE_WINDOW:
        status = "SEV2_NEAR_DEADLINE"
        exit_code = 2
        alert = AlertCard(
            source="gate2",
            title=f"OFI Gate 2: 距达标 <{_NEAR_DEADLINE_WINDOW} H1 窗口 — 请就位",
            severity="Sev2",
            checks={"gate2_retrain": "Sev2"},
            details={
                "h1_windows": h1_windows,
                "n_records": n_records,
                "remaining": _H1_THRESHOLD - h1_windows,
                "verdict": stats.get("verdict", ""),
            },
        )

    # ── Emit progress line (Iron Law #11: stdout is the evidence source) ──
    print(f"[{now.isoformat()[:19]}] {status_line}")
    print(f"[STATUS] {status}")

    # ── Dispatch alert (skip on dry-run; skip state write on dry-run) ──
    if alert is not None:
        if dry_run:
            print(f"[DRY-RUN] Would alert: {alert.severity} (status={status})")
            print("[DRY-RUN] no state write")
        else:
            sent = dispatch_alert(alert, dry_run=False)
            # ASCII-only stdout (GBK console); Chinese title lives in the
            # UTF-8 DingTalk payload only.
            print(f"[ALERT] {alert.severity} (status={status}) sent={sent}")

    if not dry_run:
        # Advance monitor state.  Reset stall latch on progress.
        next_state: dict[str, Any] = {
            "last_run": now.isoformat(),
            "prev_h1_windows": h1_windows,
            "prev_n_records": n_records,
            "alerted_stall": bool(state.get("alerted_stall") and h1_windows == prev_h1),
            "alerted_ready": bool(state.get("alerted_ready")),
        }
        if gate_ready and not force_ready:
            next_state["alerted_ready"] = True
        if isinstance(prev_h1, int) and h1_windows > prev_h1:
            next_state["alerted_stall"] = False
        _save_state(state_path, next_state)

    return exit_code


def main() -> None:
    ap = argparse.ArgumentParser(description="Gate 2 OFI accumulation sentinel")
    ap.add_argument("--data-dir", default="data_btc", help="Data directory root")
    ap.add_argument("--json", action="store_true", help="Emit status JSON only (read-only)")
    ap.add_argument("--dry-run", action="store_true", help="Preview: no state write, no alert")
    ap.add_argument(
        "--force-ready-alert",
        action="store_true",
        help="Force the one-shot READY alert (acceptance test; does not latch)",
    )
    args = ap.parse_args()

    data_dir = _resolve_data_dir(args.data_dir)

    if args.json:
        stats = inspect(data_dir)
        state = _load_state(data_dir / "state" / "gate2_sentinel.json")
        payload = {
            "data_dir": str(data_dir),
            "n_records": stats.get("n_records", 0),
            "distinct_h1_windows": stats.get("distinct_h1_windows", 0),
            "gate2_ready": stats.get("gate2_retrain", {}).get("ready", False),
            "verdict": stats.get("verdict", ""),
            "state": state,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    exit_code = run(data_dir, dry_run=args.dry_run, force_ready=args.force_ready_alert)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
