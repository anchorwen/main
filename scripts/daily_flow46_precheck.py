"""Daily Flow46 Battle-Readiness Precheck — 8/19 决战每日健康报告.

IC ruling (2026-08-05): mount a ~04:03 Beijing Mon-Fri (weekend excluded)
battle-readiness check during the human's sleep window, so that any
pre-battle problem is seen by the human on waking — instead of only on
8/19.  Deployment: Windows Task Scheduler (schtasks) `Future\DailyFlow46Precheck`
every weekday 04:03, PLUS a Claude-side cron at 04:10 that reads the report
into the dialogue.  Full design in docs/runbooks/daily_flow46_precheck_deployment.md.

What this is NOT:
- It does NOT re-fire the Gate 2 sentinel's own alerts (stall / ready /
  near-deadline).  Those live in the `gate2` channel and are owned by
  scripts/gate2_sentinel.py (daily 12:30).  The precheck only READS and
  PRESENTS the sentinel's verdict.
- It is stateless: no new state file (the sentinel owns OFI delta state).

Iron Law #11: every number printed/written here comes from
scripts.inspect_ofi_history.inspect() (Gate 2 stats), the sentinel's own
state file, mt5_bridge_health.json, or `git status --porcelain` — the
script is the sole evidence source.  All timestamps are normalized to
timezone-aware UTC before any subtraction (IC 2026-08-05 Timezone Hygiene:
naive-local minus naive-UTC would inject a constant 8h bias).

Decision machine (aggregate of five independent checks):
  1. sentinel_liveness: gate2_sentinel.json.last_run older than 36h or
     missing                  -> Sev1  (sentinel runs 12:30; precheck 04:03
                                        gap ~15.5h; a missed day = ~39.5h)
  2. ofi_freshness: inspect().last_ts older than 4h   -> Sev1 (generous,
     absorbs the ~1h daily broker maintenance break)
  3. hash_lock: dirty tracked .py/.yaml/.yml/.json outside data/ -> Sev1
     (replicates _enforce_hash_lock; this is the 8/19 killer)
  4. bridge_health: mt5_bridge_health.json disconnected / heartbeat >10min
     -> Sev2
  5. gate2_progress: informational only (presented, never alerted here)

Exit codes (MATCH the sentinel convention): 0 = OK (silent), 1 = Sev1,
2 = Sev2.  DingTalk is pushed ONLY on anomaly — OK days are quiet.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

# ── Repo-root bootstrap so `from scripts.X import ...` resolves under any ──
# ── launch context (schtasks cwd = System32, direct run, -m, pytest).   ──
BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from scripts.alert_dispatcher import AlertCard, dispatch_alert  # noqa: E402
from scripts.inspect_ofi_history import inspect  # noqa: E402

# Gate 2 threshold (must match inspect_ofi_history._RETRAIN_H1_THRESHOLD).
_H1_THRESHOLD = 1_000
# Sentinel liveness: sentinel runs daily 12:30, precheck 04:03 next morning
# (~15.5h apart).  One missed sentinel day => ~39.5h => clean Sev1.
_SENTINEL_STALE_HOURS = 36.0
# OFI freshness: generous to absorb the ~1h daily broker maintenance break.
_OFI_STALE_HOURS = 4.0
# Bridge heartbeat freshness (minutes).
_BRIDGE_STALE_MINUTES = 10.0
# Battle date (Runbook §0).
_BATTLE_DATE = date(2026, 8, 19)
# hash-lock filter — replicate train_btc_expected_r_institutional.py:92-124
# _enforce_hash_lock() exactly (never drift from it).
_HASH_LOCK_EXTS = (".py", ".yaml", ".yml", ".json")


def _resolve_data_dir(raw: str) -> Path:
    """Resolve --data-dir against repo root so relative paths work anywhere."""
    p = Path(raw)
    if not p.is_absolute():
        p = BASE / p
    return p


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, TypeError, OSError):
        return {}


def _tail(path: Path, n: int) -> list[str]:
    try:
        if not path.exists():
            return []
        return path.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
    except OSError:
        return []


def _in_weekend_closure(now_local: datetime) -> bool:
    """True on Saturday/Sunday.  User requirement: weekend excluded (周末停盘除外).

    Note: the broker halts BTC over the weekend AND holds a ~1h daily break,
    so a weekend run would otherwise read a stale (but legitimate) OFI bar.
    The schtasks Mon-Fri schedule is the first layer; this guard is the
    second (covers manual weekend runs).
    """
    return now_local.weekday() >= 5


def _parse_ts(value: Any) -> datetime | None:
    """Parse an ISO timestamp to timezone-aware UTC (never naive).

    Handles trailing 'Z', explicit offsets, and naive strings (treated as UTC
    by contract — ofi_history / inspect / mt5_bridge_health all emit UTC naive).
    IC 2026-08-05 (Timezone Hygiene): naive timestamps are assumed UTC and
    always converted to aware UTC here, so every age subtraction is UTC-vs-UTC.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    s = value.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _age_hours(value: Any, now_utc: datetime) -> float | None:
    """Age in hours of a timestamp vs timezone-aware now; None if unparseable."""
    parsed = _parse_ts(value)
    if parsed is None:
        return None
    return (now_utc - parsed).total_seconds() / 3600.0


def _run_git_porcelain() -> tuple[list[str], bool]:
    """Run `git status --porcelain` from repo root.

    Returns (lines, ok).  Default --porcelain does NOT list ignored files
    (data/ and data_btc/ are gitignored — the hash-lock filter additionally
    excludes any data-prefixed path).  Fail-open: git unavailable -> ([],
    False), mirroring _enforce_hash_lock's `except` branch.
    """
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            cwd=str(BASE),
        )
    except (OSError, subprocess.TimeoutExpired):
        return [], False
    if proc.returncode != 0:
        return [], False
    return [ln for ln in proc.stdout.splitlines() if ln.strip()], True


def _classify_tree(lines: list[str]) -> dict[str, Any]:
    """Classify porcelain lines the SAME way _enforce_hash_lock does.

    - `?? path`            -> untracked (informational; expected probes)
    - ` M`/`M `/`A `/`D `  -> strip 2-char status, keep tracked source files
                             (.py/.yaml/.yml/.json) whose first path component
                             does NOT start with "data".
    """
    dirty_source: list[str] = []
    untracked: list[str] = []
    for line in lines:
        if line.startswith("??"):
            untracked.append(line[3:].strip())
            continue
        fname = line[2:].strip()
        if fname.endswith(_HASH_LOCK_EXTS) and not Path(fname).parts[0].startswith("data"):
            dirty_source.append(fname)
    return {"dirty_source": sorted(dirty_source), "untracked": sorted(untracked)}


def _eta_days(stats: dict[str, Any], h1_windows: int) -> float | None:
    """ETA to 1000 windows from the historical average accumulation rate.

    Same method as gate2_sentinel._eta_days: uses inspect()'s own
    span_days/window count (the average already bakes in weekend closure and
    the ~1h daily halt).  Never the single previous-day delta.
    """
    span = stats.get("span_days")
    if not isinstance(span, int | float) or span <= 1.0 or h1_windows <= 0:
        return None
    rate_per_day = h1_windows / span
    if rate_per_day <= 0:
        return None
    return max(_H1_THRESHOLD - h1_windows, 0) / rate_per_day


def _check_sentinel(data_dir: Path, now_utc: datetime) -> dict[str, Any]:
    state = _load_json(data_dir / "state" / "gate2_sentinel.json")
    last_run = state.get("last_run")
    age = _age_hours(last_run, now_utc) if isinstance(last_run, str) else None
    if age is not None and age <= _SENTINEL_STALE_HOURS:
        status, severity = "ok", "OK"
    else:
        status, severity = "stale", "Sev1"
    log_tail = _tail(data_dir / "state" / "gate2_sentinel.log", 6)
    last_status = next((ln for ln in reversed(log_tail) if ln.startswith("[STATUS]")), None)
    return {
        "status": status,
        "age_hours": age,
        "last_run": last_run,
        "last_status": last_status,
        "severity": severity,
    }


def _check_ofi(stats: dict[str, Any], now_utc: datetime) -> dict[str, Any]:
    n_records = int(stats.get("n_records", 0))
    hist_path = Path(stats.get("history_path", ""))
    last_ts = stats.get("last_ts")
    missing = n_records == 0 and not hist_path.exists()
    age = _age_hours(last_ts, now_utc) if isinstance(last_ts, str) else None
    if missing:
        status, severity = "missing", "Sev1"
    elif age is not None and age > _OFI_STALE_HOURS:
        status, severity = "stale", "Sev1"
    elif age is None:
        # Records exist but the last timestamp is unparseable — surface it.
        status, severity = "unknown", "Sev2"
    else:
        status, severity = "ok", "OK"
    return {"status": status, "age_hours": age, "last_ts": last_ts, "severity": severity}


def _check_bridge(data_dir: Path, now_utc: datetime) -> dict[str, Any]:
    health = _load_json(data_dir / "reports" / "mt5_bridge_health.json")
    connected = bool(health.get("mt5_connected", False))
    hb = health.get("last_heartbeat_utc")
    age_min: float | None = None
    if isinstance(hb, str):
        age_h = _age_hours(hb, now_utc)
        age_min = age_h * 60.0 if age_h is not None else None
    if not health:
        status, severity = "missing", "Sev2"
    elif not connected:
        status, severity = "disconnected", "Sev2"
    elif age_min is None or age_min > _BRIDGE_STALE_MINUTES:
        status, severity = "stale", "Sev2"
    else:
        status, severity = "ok", "OK"
    return {
        "status": status,
        "mt5_connected": connected,
        "heartbeat_age_min": age_min,
        "severity": severity,
    }


def _countdown(now_local: datetime) -> dict[str, Any]:
    days = (_BATTLE_DATE - now_local.date()).days
    milestones = [
        ("8/17", "数据补给仪式 pre-flight (Runbook §3.2)"),
        ("8/18", "基集重建 + flow46 重建 + Gate 2 终审 (唯一不可预演项)"),
        ("8/19", "决战: Gate 2 终确认 → 迁移重训 → verify_lineage"),
    ]
    return {"days_to_battle": days, "milestones": milestones}


def _render_report(ctx: dict[str, Any]) -> str:
    lines = [
        f"# Flow46 决战每日预检 — {ctx['report_date']}",
        "",
        f"> 生成 {ctx['now_local_iso']} | 距 8/19 决战: {ctx['days_to_battle']} 天 | "
        f"结论: **{ctx['severity']}**",
        "",
        "## 1. Gate 2 进度",
        f"- H1 windows: **{ctx['n_windows']}/1000** ({ctx['remaining']} to go) | "
        f"ETA ~{ctx['eta_date']}",
        f"- 哨兵上次状态: `{ctx['sentinel_last_status'] or 'n/a'}`",
        "",
        "## 2. 哨兵活性 (gate2_sentinel)",
        f"- 上次运行: {ctx['sentinel_last_run'] or 'n/a'} | 距今 " f"{ctx['sentinel_age_h']:.1f}h"
        if ctx["sentinel_age_h"] is not None
        else "- 上次运行: n/a | 状态: 缺失",
        f"- 状态: **{ctx['sentinel_status']}**",
        "",
        "## 3. OFI 数据新鲜度",
        f"- 最后 settle: {ctx['ofi_last_ts'] or 'n/a'} | 距今 " f"{ctx['ofi_age_h']:.1f}h"
        if ctx["ofi_age_h"] is not None
        else "- 最后 settle: n/a | 状态: 缺失/未知",
        f"- 状态: **{ctx['ofi_status']}**",
        "",
        "## 4. 工作树 / hash-lock 就绪",
        f"- 状态: **{'DIRTY (会阻断 8/19 训练)' if ctx['tree_dirty'] else 'CLEAN'}**",
    ]
    if ctx["tree_dirty"]:
        lines.append("- 违规文件:")
        for f in ctx["tree_dirty_files"]:
            lines.append(f"  - `{f}`")
    lines.extend(
        [
            "- untracked (预期探针, 不告警):",
        ]
    )
    if ctx["tree_untracked"]:
        for f in ctx["tree_untracked"]:
            lines.append(f"  - `{f}`")
    else:
        lines.append("  - (无)")
    if ctx["git_ok"] is False:
        lines.append("- ⚠️ git 不可用 — hash-lock 无法验证 (Sev2)")
    lines.extend(
        [
            "",
            "## 5. 桥接 / live 健康",
            f"- mt5_connected: {ctx['bridge_connected']} | heartbeat 距今 "
            f"{ctx['bridge_age_min']:.1f}min"
            if ctx["bridge_age_min"] is not None
            else "- mt5_connected: n/a | heartbeat: 缺失",
            f"- 状态: **{ctx['bridge_status']}**",
            "",
            "## 6. 里程碑倒计时",
            f"- 距 8/19: **{ctx['days_to_battle']} 天**",
        ]
    )
    for m_date, m_desc in ctx["milestones"]:
        lines.append(f"- {m_date}: {m_desc}")
    lines.extend(
        [
            "",
            "## 汇总",
            f"- severity: **{ctx['severity']}** | exit code: {ctx['exit_code']}",
            "- 正常日静默 (无 DingTalk)；异常日推送此报告摘要。",
        ]
    )
    return "\n".join(lines) + "\n"


def run(data_dir: Path, *, dry_run: bool = False) -> int:
    now_local = datetime.now().astimezone()
    now_utc = datetime.now(UTC)

    # Weekend guard — user requirement (周末停盘除外).
    if _in_weekend_closure(now_local):
        print(f"[weekend] daily precheck skipped (Sat/Sun) — {now_local.isoformat()}")
        return 0

    stats = inspect(data_dir)
    sentinel = _check_sentinel(data_dir, now_utc)
    ofi = _check_ofi(stats, now_utc)
    git_lines, git_ok = _run_git_porcelain()
    tree = _classify_tree(git_lines) if git_ok else {"dirty_source": [], "untracked": []}
    bridge = _check_bridge(data_dir, now_utc)
    countdown = _countdown(now_local)

    n_windows = int(stats.get("distinct_h1_windows", 0))
    remaining = max(_H1_THRESHOLD - n_windows, 0)
    eta_days = _eta_days(stats, n_windows)
    eta_date = (now_utc + timedelta(days=eta_days)).strftime("%m-%d") if eta_days else "n/a"

    checks: dict[str, str] = {
        "sentinel_liveness": sentinel["severity"],
        "ofi_freshness": ofi["severity"],
        "hash_lock": "Sev1" if tree["dirty_source"] else ("Sev2" if not git_ok else "OK"),
        "bridge_health": bridge["severity"],
        "gate2_progress": "OK",  # informational — sentinel owns gate2 alerts
    }
    severities = [checks[k] for k in checks if k != "gate2_progress"]
    if "Sev1" in severities:
        severity, exit_code = "Sev1", 1
    elif "Sev2" in severities:
        severity, exit_code = "Sev2", 2
    else:
        severity, exit_code = "OK", 0

    ctx: dict[str, Any] = {
        "report_date": now_local.date().isoformat(),
        "now_local_iso": now_local.isoformat(timespec="seconds"),
        "days_to_battle": countdown["days_to_battle"],
        "milestones": countdown["milestones"],
        "severity": severity,
        "exit_code": exit_code,
        "n_windows": n_windows,
        "remaining": remaining,
        "eta_date": eta_date,
        "sentinel_last_status": sentinel["last_status"],
        "sentinel_last_run": sentinel["last_run"],
        "sentinel_age_h": sentinel["age_hours"],
        "sentinel_status": sentinel["status"],
        "ofi_last_ts": ofi["last_ts"],
        "ofi_age_h": ofi["age_hours"],
        "ofi_status": ofi["status"],
        "tree_dirty": bool(tree["dirty_source"]),
        "tree_dirty_files": tree["dirty_source"],
        "tree_untracked": tree["untracked"],
        "git_ok": git_ok,
        "bridge_connected": bridge["mt5_connected"],
        "bridge_age_min": bridge["heartbeat_age_min"],
        "bridge_status": bridge["status"],
    }

    if dry_run:
        print("[DRY-RUN] daily_flow46_precheck preview (no report write, no alert)")
        for k in (
            "gate2_progress",
            "sentinel_liveness",
            "ofi_freshness",
            "hash_lock",
            "bridge_health",
        ):
            print(f"  {k}: {checks[k]}")
        print(f"[STATUS] {severity}")
        return exit_code

    # Write the dated report (UTF-8 file — this is what the morning cron reads).
    report_dir = data_dir / "state" / "daily_precheck"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"{now_local.date().isoformat()}.md"
    report_path.write_text(_render_report(ctx), encoding="utf-8")
    print(f"[REPORT] {report_path}")

    # ASCII-only stdout summary (GBK-safe, mirrors gate2_sentinel).
    print(f"[STATUS] {severity}")
    print(
        f"Gate 2: {n_windows}/{_H1_THRESHOLD} ({remaining} to go) | ETA ~{eta_date} | "
        f"sentinel={sentinel['status']} ofi={ofi['status']} "
        f"hash_lock={'CLEAN' if not tree['dirty_source'] else 'DIRTY'} "
        f"bridge={bridge['status']}"
    )

    # DingTalk ONLY on anomaly — OK days are silent (user requirement).
    if severity != "OK":
        details: dict[str, Any] = {
            "h1_windows": n_windows,
            "remaining": remaining,
            "eta": eta_date,
        }
        if tree["dirty_source"]:
            details["dirty_sources"] = ", ".join(tree["dirty_source"])
        if not git_ok:
            details["git_status"] = "unavailable"
        if sentinel["status"] == "stale":
            details["sentinel_age_h"] = sentinel["age_hours"]
        if ofi["status"] != "ok":
            details["ofi_age_h"] = ofi["age_hours"]
        if bridge["status"] != "ok":
            details["bridge_status"] = bridge["status"]
            details["bridge_age_min"] = bridge["heartbeat_age_min"]
        title = (
            "Flow46 每日预检: SEV1 异常 — 请晨间处理"
            if severity == "Sev1"
            else "Flow46 每日预检: SEV2 警告"
        )
        card = AlertCard(
            source="daily-precheck",
            title=title,
            severity=severity,
            checks=checks,
            details=details,
        )
        sent = dispatch_alert(card, dry_run=False)
        print(f"[ALERT] {severity} sent={sent}")

    return exit_code


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Daily Flow46 battle-readiness precheck (04:03 Mon-Fri Beijing)"
    )
    ap.add_argument("--data-dir", default="data_btc", help="Data directory root")
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview: no report write, no alert, no state",
    )
    args = ap.parse_args()

    data_dir = _resolve_data_dir(args.data_dir)
    exit_code = run(data_dir, dry_run=args.dry_run)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
