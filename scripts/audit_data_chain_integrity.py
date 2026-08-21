#!/usr/bin/env python3
"""
audit_data_chain_integrity.py — 全数据链完整性审计 (Data Chain Integrity Index)
================================================================================

归属: DQAF-20260807-004 Phase 0 (IC 批准立项, 2026-08-07)
场景: Scene E 新建审计工具 | 只读 · 幂等 · 可复现

【统计口径声明】 (Iron Law #11 — 脚本先行, stdout 是唯一合法证据源)
  1. 本脚本只读 --data-dir 下的数据文件, 绝不修改任何 .json/.jsonl (只读审计)。
  2. 去重逻辑: ledger 按 event_id 去重; journal 按 message_id 去重。
  3. 生命周期对账 (孤儿/close-without-open) 用**不可变身份** join (FIX-20260807-001
     resolve_identity): position_identifier 优先, 回退 position_ticket → detail.order → ticket。
     MT5 partial-close/netting 换票不误判孤儿。
  4. 指数定义: 6 段独立健康分 (满分 100, 每 fault 按 Sev 权重扣分, 下限 0)
     → Data Chain Integrity Index = 加权和, 权重: S4记账 0.25 / S3派发 0.20 /
       S2决策 0.15 / S1入口 0.15 / S6对账 0.15 / S5投影 0.10。
  5. Sev 惩罚: Sev1 -25 / Sev2 -12 / Sev3 -5 / Sev4 -1。
  6. 已知基线噪音 (IC 裁决 "绝对视而不见") + 设计行为 + 生成器信号, 标记 baseline=true,
     不计入指数, 单独清单报告。用 --include-baseline 可计入:
       - auto_orphan_rejected 合成收尾 (cleanup_orphan_opens 拒绝单清理, 设计)
       - 影子风暴拒单 (XAUUSD 缺c + magic=null + vol=0.05, 桥防护网挡下的生成器信号)
       - 零开单休眠态快照静默 (journal 无近期 open/close)
       - S6_HEALTH_CRITICAL / S4_QUARANTINE_RESIDUE (IC 2026-08-06 基线噪音)
  7. 输出: 人类可读报告 (stdout) + --json 机器可读 + --baseline-write/--baseline-read 回归比对。

用法:
  python scripts/audit_data_chain_integrity.py --data-dir data_btc
  python scripts/audit_data_chain_integrity.py --data-dir data --json
  python scripts/audit_data_chain_integrity.py --data-dir data_btc --baseline-write gate_audit/dci_baseline_btc.json
  python scripts/audit_data_chain_integrity.py --data-dir data_btc --baseline-read gate_audit/dci_baseline_btc.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.market.calendar import staleness_anchor

# Windows GBK 控制台无法编码 emoji → 强制 UTF-8 输出
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
except (AttributeError, OSError):
    pass

# ---------------------------------------------------------------------------
# 常数定义
# ---------------------------------------------------------------------------

SEGMENT_NAMES = {
    "s1": "S1 事件入口链",
    "s2": "S2 决策链",
    "s3": "S3 派发/成交链",
    "s4": "S4 记账链 (SSOT)",
    "s5": "S5 投影链 (Views)",
    "s6": "S6 对账链",
}

SEGMENT_WEIGHTS = {"s1": 0.15, "s2": 0.15, "s3": 0.20, "s4": 0.25, "s5": 0.10, "s6": 0.15}

SEV_PENALTY = {1: 25, 2: 12, 3: 5, 4: 1}

SEV_LABEL = {1: "SEV1", 2: "SEV2", 3: "SEV3", 4: "SEV4"}

# IC 2026-08-06 裁决的已知基线噪音 — 不计入指数 (视而不见条款)
# S6_HEALTH_CRITICAL: data_health 基线 ~41 CRITICAL
BASELINE_CODES = {
    "S6_HEALTH_CRITICAL",
    "S4_QUARANTINE_RESIDUE",  # journal_orphan_quarantine 历史残留 (FIX-20260807-003 后新增隔离为 0)
}

# 运行中进程可能持锁的文件: 读到 PermissionError 记 SEV3 告警, 不视为数据损坏
LOCK_SENSITIVE = ("mt5_outbox", "mt5_outbox_processed", "locks")


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(value: Any) -> datetime | None:
    """容忍多种时间戳格式 → UTC datetime; 失败返回 None。"""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    for try_fmt in (lambda: datetime.fromisoformat(s),):
        try:
            dt = try_fmt()
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)  # 无时区 → 视为 UTC (系统日志一致)
            return dt
        except ValueError:
            continue
    return None


def _safe_age_minutes(ts: datetime | None, now: datetime) -> float | None:
    if ts is None or ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc) if ts else None
    if ts is None:
        return None
    return (now - ts).total_seconds() / 60.0


# ---------------------------------------------------------------------------
# Calendar-aware staleness (The Calendar Grid — FIX-20260821-001)
# ---------------------------------------------------------------------------
# All hardcoded age thresholds below converge on the single calendar clock
# (core/market/calendar.staleness_anchor).  During a market close a frozen data
# chain is EXPECTED → the anchor shifts to the last market close, killing the
# weekend false positives (TECH_DEBT-011).  BTC 24/7 → never relaxes.


def _market_type_for_data_dir(dd: Path) -> str:
    """data_btc → crypto_24_7 (BTC 24/7, never relaxes); else forex_24_5 (XAU)."""
    return "crypto_24_7" if "btc" in str(dd).lower() else "forex_24_5"


def _market_type_for_symbol(symbol: str) -> str:
    """Per-symbol market type — XAUUSDc features may live under any data dir."""
    return "forex_24_5" if symbol and "XAU" in str(symbol).upper() else "crypto_24_7"


def _is_stale(
    ts: datetime | None, now: datetime, market_type: str, *, base_threshold_min: float
) -> bool:
    """Calendar-aware staleness: data older than the staleness anchor → stale.

    None timestamp → not stale (missing-file handling is a separate fault)."""
    if ts is None:
        return False
    anchor = staleness_anchor(
        now_utc=now, market_type=market_type, base_threshold_min=base_threshold_min
    )
    return ts < anchor


def _iter_jsonl(path: Path, max_lines: int | None = None):
    """流式读 JSONL, 容忍坏行 (返回 (line_no, obj | None, raw))。"""
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except (PermissionError, OSError):
        yield -1, "LOCKED", ""
        return
    try:
        for line_no, raw in enumerate(fh, 1):
            if max_lines is not None and line_no > max_lines:
                break
            raw = raw.strip()
            if not raw:
                yield line_no, None, raw
                continue
            try:
                obj = json.loads(raw)
                yield line_no, obj, raw
            except json.JSONDecodeError:
                yield line_no, "PARSE_ERR", raw[:200]
    finally:
        fh.close()


def _read_json(path: Path) -> tuple[dict | None, str | None]:
    """读单个 JSON 文件 → (obj, err)。"""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            obj = json.load(fh)
        return obj, None
    except (PermissionError, OSError) as e:
        return None, f"IO: {e}"
    except json.JSONDecodeError as e:
        return None, f"JSON_DECODE: {e}"
    except Exception as e:  # noqa: BLE001 — 审计工具须容忍任意畸形输入
        return None, f"ERR: {e}"


def _fault(
    segment: str, code: str, sev: int, title: str, detail: str, baseline: bool = False
) -> dict:
    return {
        "segment": segment,
        "code": code,
        "sev": sev,
        "title": title,
        "detail": detail,
        "baseline": baseline,
    }


def _segment_score(faults: list[dict]) -> int:
    fresh = [f for f in faults if not f.get("baseline")]
    total = 100.0
    for f in fresh:
        total -= SEV_PENALTY.get(f["sev"], 1)
    return max(0, int(round(total)))


def _as_int(value: Any) -> int | None:
    """宽容 int 转换 (容纳字符串形式的 ticket)。"""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _resolve_identity(obj: dict) -> int | None:
    """复刻 resolve_identity() (FIX-20260708-001 / ticket_resolver.py):
    不可变 position_identifier 优先 (跨 MT5 partial-close/netting 换票稳定),
    否则回退 mutable ticket (position_ticket → detail.order → ticket)。

    position_identifier 在 close 腿上等于原始开仓 ticket, 因此在 netting 换票后
    close 腿仍能精确回链到 open 腿 —— 避免把换票误判为孤儿平仓。
    """
    pid = _as_int(obj.get("position_identifier"))
    if pid:
        return pid
    t = _as_int(obj.get("position_ticket"))
    if t:
        return t
    t = _as_int((obj.get("detail") or {}).get("order"))
    if t:
        return t
    return _as_int(obj.get("ticket"))


def _is_shadow_storm_reject(obj: dict, detail: dict) -> bool:
    """影子风暴签名 (incident_shadow_storm_resolved_20260806 / DQAF-20260807-004):
    symbol=XAUUSD(缺 'c') + magic=None + volume=0.05 — v9 shadow 路径幽灵单,
    被桥 Dedup Guard / Symbol 校验挡下 (retcode 10018)。链忠实记录了拒绝结果,
    属生成器行为信号而非链断层 → scoped-out (baseline), 不计入指数。
    """
    sym = str(obj.get("symbol") or "")
    if sym != "XAUUSD":
        return False
    magic = obj.get("magic")
    if magic not in (None, "", 0):
        return False
    try:
        v = obj.get("volume")
        vol = float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return False
    return vol == 0.05


# ---------------------------------------------------------------------------
# S1 事件入口链
# ---------------------------------------------------------------------------


def s1_event_ingress(dd: Path, now: datetime) -> list[dict]:
    faults: list[dict] = []
    # --- feature_store 新鲜度与覆盖 ---
    fs_records = dd / "feature_store" / "records"
    fs_dir = dd / "feature_store"
    schema_ok = False
    if (dd / "feature_store" / "schemas.json").exists():
        schema_ok = True
    if fs_dir.exists() and fs_records.is_dir():
        total_records = 0
        latest_ts: datetime | None = None
        for sym_dir in sorted(fs_records.iterdir()):
            if not sym_dir.is_dir():
                continue
            for tf_dir in sorted(sym_dir.iterdir()):
                f = tf_dir / "features.jsonl"
                if not f.exists():
                    faults.append(
                        _fault(
                            "s1",
                            "S1_FEATURE_MISSING_FILE",
                            3,
                            "特征文件缺失",
                            f"{sym_dir.name}/{tf_dir.name}/features.jsonl",
                        )
                    )
                    continue
                cnt = 0
                last: datetime | None = None
                for _ln, obj, _raw in _iter_jsonl(f):
                    if obj == "LOCKED":
                        faults.append(
                            _fault("s1", "S1_FEATURE_LOCKED", 3, "特征文件被锁", str(f), True)
                        )
                        continue
                    if not isinstance(obj, dict) or "event_time" not in obj:
                        continue
                    cnt += 1
                    t = _parse_dt(obj["event_time"])
                    if t and (last is None or t > last):
                        last = t
                total_records += cnt
                if last and (latest_ts is None or last > latest_ts):
                    latest_ts = last
                if cnt == 0:
                    faults.append(
                        _fault(
                            "s1",
                            "S1_FEATURE_ZERO",
                            3,
                            "特征记录为空",
                            f"{sym_dir.name}/{tf_dir.name}",
                        )
                    )
                age = _safe_age_minutes(last, now)
                _mt = _market_type_for_symbol(sym_dir.name)
                if _is_stale(last, now, _mt, base_threshold_min=12 * 60):  # >12h 视为入口停滞
                    faults.append(
                        _fault(
                            "s1",
                            "S1_FEATURE_STALE",
                            2,
                            "特征记录停滞",
                            f"{sym_dir.name}/{tf_dir.name} last={last} age={age:.0f}min",
                        )
                    )
        if not schema_ok:
            faults.append(
                _fault("s1", "S1_SCHEMA_MISSING", 3, "feature_store/schemas.json 缺失", str(fs_dir))
            )
    # --- bar_sync_state ---
    bar = _read_json(dd / "bar_sync_state.json")
    if bar[0] is None and bar[1] and "NO_SUCH" not in bar[1]:
        faults.append(_fault("s1", "S1_BAR_SYNC_BAD", 3, "bar_sync_state 解析失败", bar[1]))
    elif bar[0] is not None:
        obj = bar[0]
        for key in ("last_sync", "last_updated", "updated_at", "last_bar_time"):
            if isinstance(obj, dict) and key in obj:
                t = _parse_dt(obj[key])
                age = _safe_age_minutes(t, now)
                if _is_stale(t, now, _market_type_for_data_dir(dd), base_threshold_min=12 * 60):
                    faults.append(
                        _fault(
                            "s1",
                            "S1_BAR_SYNC_STALE",
                            2,
                            "bar 同步停滞",
                            f"{key}={obj[key]} age={age:.0f}min",
                        )
                    )
                break
    return faults


# ---------------------------------------------------------------------------
# S2 决策链
# ---------------------------------------------------------------------------


def s2_decision_chain(dd: Path, now: datetime) -> list[dict]:
    faults: list[dict] = []
    votes_dir = dd / "brain_votes"
    decisions_dir = dd / "decisions"
    vote_count = 0
    latest_vote: datetime | None = None
    silent_brains: set[str] = set()

    if votes_dir.is_dir():
        files = sorted(votes_dir.glob("*.jsonl"))
        if not files:
            faults.append(
                _fault("s2", "S2_VOTES_NONE", 3, "brain_votes 无投票文件", str(votes_dir))
            )
        brains_seen: set[str] = set()
        for f in files[-5:]:  # 最近 5 天
            for _ln, obj, _raw in _iter_jsonl(f):
                if not isinstance(obj, dict):
                    continue
                vote_count += 1
                bid = obj.get("brain_id")
                if bid:
                    brains_seen.add(bid)
                t = _parse_dt(obj.get("recorded_at"))
                if t and (latest_vote is None or t > latest_vote):
                    latest_vote = t
        # 覆盖检查仅对已见 brain
        if files:
            for _ln, obj, _raw in _iter_jsonl(files[-1]):
                if isinstance(obj, dict) and obj.get("brain_id"):
                    silent_brains.add(obj["brain_id"])
    elif decisions_dir.is_dir():
        # XAU 域: decisions/YYYY-MM-DD/XAUUSD.decisions.jsonl
        day_dirs = sorted(decisions_dir.iterdir()) if decisions_dir.exists() else []
        if not day_dirs:
            faults.append(
                _fault("s2", "S2_DECISIONS_NONE", 3, "decisions 无日目录", str(decisions_dir))
            )
        for d in day_dirs[-3:]:
            if not d.is_dir():
                continue
            for f in d.glob("*.decisions.jsonl"):
                for _ln, obj, _raw in _iter_jsonl(f):
                    if not isinstance(obj, dict):
                        continue
                    vote_count += 1
                    t = _parse_dt(obj.get("recorded_at") or obj.get("timestamp"))
                    if t and (latest_vote is None or t > latest_vote):
                        latest_vote = t
    else:
        faults.append(
            _fault(
                "s2",
                "S2_DECISION_SOURCE_MISSING",
                3,
                "决策源缺失",
                "无 brain_votes/ 且无 decisions/ 目录",
            )
        )

    if vote_count == 0 and (votes_dir.is_dir() or decisions_dir.is_dir()):
        faults.append(_fault("s2", "S2_DECISION_EMPTY", 3, "决策记录为空", f"count={vote_count}"))

    # regime 连续性
    reg_file = dd / "regime_snapshots.jsonl"
    if reg_file.exists():
        last_reg: datetime | None = None
        for _ln, obj, _raw in _iter_jsonl(reg_file):
            if isinstance(obj, dict):
                t = _parse_dt(obj.get("timestamp") or obj.get("recorded_at"))
                if t:
                    last_reg = t
        age = _safe_age_minutes(last_reg, now)
        if _is_stale(last_reg, now, _market_type_for_data_dir(dd), base_threshold_min=12 * 60):
            faults.append(
                _fault(
                    "s2",
                    "S2_REGIME_STALE",
                    2,
                    "regime 快照停滞",
                    f"last={last_reg} age={age:.0f}min",
                )
            )
    return faults


# ---------------------------------------------------------------------------
# S3 派发/成交链
# ---------------------------------------------------------------------------


def s3_dispatch_execution(dd: Path, now: datetime) -> list[dict]:
    faults: list[dict] = []
    journal = dd / "live_trade_journal.jsonl"
    if not journal.exists():
        return [_fault("s3", "S3_JOURNAL_MISSING", 1, "live_trade_journal 缺失", str(journal))]

    opened: set[int] = set()
    closed: set[int] = set()
    ghost_vol = 0
    rejected_storm = 0
    rejected_genuine = 0
    ack_no_order = 0
    pnl_null_close = 0
    outbox_locked = 0
    rejected_open_ids: set[str] = set()
    last_open_ts: datetime | None = None
    last_close_ts: datetime | None = None
    totals = {"open": 0, "close": 0, "other": 0}
    null_pnl_cats: dict[str, dict] = {
        cat: {"count": 0, "samples": [], "dates": set(), "unlinked": 0}
        for cat in ("legacy_backfill", "auto_orphan_rejected", "corpse_missing", "intent_pending")
    }

    for _ln, obj, _raw in _iter_jsonl(journal):
        if not isinstance(obj, dict):
            continue
        action = obj.get("action")
        ack = obj.get("ack_status")
        vol = obj.get("volume")
        detail = obj.get("detail")
        if not isinstance(detail, dict):
            detail = {}
        ident = _resolve_identity(obj)
        ts = _parse_dt(obj.get("recorded_at"))

        if action == "open":
            totals["open"] += 1
            if ident:
                opened.add(ident)
            if ts and (last_open_ts is None or ts > last_open_ts):
                last_open_ts = ts
            if isinstance(vol, (int, float)) and vol <= 0:
                ghost_vol += 1
                faults.append(
                    _fault(
                        "s3",
                        "S3_GHOST_VOLUME",
                        1,
                        "开仓 ghost 体积",
                        f"ident={ident} volume={vol}",
                    )
                )
            if ack == "rejected":
                if _is_shadow_storm_reject(obj, detail):
                    rejected_storm += 1
                else:
                    rejected_genuine += 1
                if obj.get("message_id"):
                    rejected_open_ids.add(str(obj["message_id"]))
            elif ident is None and ack == "accepted":
                ack_no_order += 1
        elif action == "close":
            totals["close"] += 1
            mid = obj.get("message_id", "") or ""
            is_legacy = mid.startswith("close_orphan_backfill") or mid.startswith("ghost_cleanup")
            if ident and not is_legacy:
                closed.add(ident)
            if ts and (last_close_ts is None or ts > last_close_ts):
                last_close_ts = ts
            if isinstance(vol, (int, float)) and vol <= 0:
                ghost_vol += 1
            pnl = obj.get("pnl")
            if pnl is None and isinstance(detail.get("pnl"), (int, float)):
                pnl = detail.get("pnl")
            if pnl is None:
                pnl_null_close += 1
                reason = detail.get("reason", "") or ""
                if is_legacy:
                    cat = "legacy_backfill"
                elif reason == "auto_orphan_rejected":
                    cat = "auto_orphan_rejected"
                elif ack == "closed":
                    cat = "corpse_missing"
                else:
                    cat = "intent_pending"  # 未成交平仓意图, 属正常, 不报
                null_pnl_cats[cat]["count"] += 1
                if len(null_pnl_cats[cat]["samples"]) < 3 and ident:
                    null_pnl_cats[cat]["samples"].append(f"ident={ident} {mid[:44]}")
                if cat == "corpse_missing" or cat == "auto_orphan_rejected":
                    null_pnl_cats[cat]["dates"].add(str(obj.get("recorded_at", ""))[:10])
                if cat == "auto_orphan_rejected":
                    oid = obj.get("open_message_id")
                    if not oid or str(oid) not in rejected_open_ids:
                        null_pnl_cats[cat]["unlinked"] += 1
        else:
            totals["other"] += 1

    # 孤儿平仓: close 无对应 open (position_identifier 不可变身份回链, FIX-20260708-001)
    orphan_closes = closed - opened
    if orphan_closes:
        faults.append(
            _fault(
                "s3",
                "S3_ORPHAN_CLOSE",
                1,
                "孤儿平仓 (close 无 open)",
                f"identities={sorted(orphan_closes)[:8]} count={len(orphan_closes)}",
            )
        )
    if ghost_vol:
        faults.append(
            _fault("s3", "S3_GHOST_VOLUME_TOTAL", 2, "ghost 体积记录数", f"count={ghost_vol}")
        )
    if rejected_genuine:
        faults.append(
            _fault(
                "s3",
                "S3_DISPATCH_REJECTED",
                2,
                "派发被拒 (真实成交意图, 桥返回 retcode)",
                f"count={rejected_genuine}",
            )
        )
    if rejected_storm:
        faults.append(
            _fault(
                "s3",
                "S3_DISPATCH_REJECTED_STORM",
                2,
                "派发被拒 (影子风暴生成器信号, 桥防护网挡下)",
                f"count={rejected_storm}",
                True,
            )
        )
    if ack_no_order:
        faults.append(
            _fault(
                "s3", "S3_ACK_NO_ORDER", 3, "accepted 但无 order ticket", f"count={ack_no_order}"
            )
        )
    for cat, info in null_pnl_cats.items():
        if cat == "intent_pending" or info["count"] == 0:
            continue
        if cat == "legacy_backfill":
            faults.append(
                _fault(
                    "s3",
                    "S3_PNL_NULL_LEGACY",
                    3,
                    "平仓无 PnL (历史 backfill 假记录)",
                    f"count={info['count']}",
                    True,
                )
            )
        elif cat == "corpse_missing":
            faults.append(
                _fault(
                    "s3",
                    "S3_PNL_NULL_CORPSE",
                    2,
                    "已成交平仓无 PnL 尸体 (FIX-20260807-003 类)",
                    f"count={info['count']} dates={sorted(info['dates'])} samples={info['samples'][:2]}",
                )
            )
        elif cat == "auto_orphan_rejected":
            linked = info["count"] - info["unlinked"]
            if info["unlinked"]:
                faults.append(
                    _fault(
                        "s3",
                        "S3_AUTO_ORPHAN_UNLINKED",
                        2,
                        "auto_orphan_rejected 无法回链到 rejected open (真异常)",
                        f"count={info['unlinked']} dates={sorted(info['dates'])}",
                    )
                )
            faults.append(
                _fault(
                    "s3",
                    "S3_AUTO_ORPHAN_REJECTED",
                    2,
                    "auto_orphan_rejected 合成收尾 (设计, cleanup_orphan_opens 拒绝单清理)",
                    f"count={info['count']} linked={linked} unlinked={info['unlinked']}",
                    True,
                )
            )

    # position_snapshots 生命周期
    snaps = dd / "position_snapshots.jsonl"
    if snaps.exists():
        snap_tickets: set[int] = set()
        last_snap: datetime | None = None
        for _ln, obj, _raw in _iter_jsonl(snaps):
            if isinstance(obj, dict):
                t = obj.get("ticket")
                if t:
                    try:
                        snap_tickets.add(int(t))
                    except (TypeError, ValueError):
                        pass
                ts = _parse_dt(obj.get("time"))
                if ts and (last_snap is None or ts > last_snap):
                    last_snap = ts
        age = _safe_age_minutes(last_snap, now)
        if age is not None and age > 24 * 60:
            # 零开单/休眠防御态: journal 无近期 open/close → 快照按持仓写入 (trail_dispatch),
            # 静默是设计行为 (health_checks 亦视 "no positions open" 为 PASS)。
            # 仅当 journal 仍在活跃管理持仓而快照停写时, 才是真实停滞。
            recent = [t for t in (last_open_ts, last_close_ts) if t is not None]
            last_activity = max(recent) if recent else None
            act_age = _safe_age_minutes(last_activity, now)
            dormant = act_age is None or act_age > 24 * 60
            faults.append(
                _fault(
                    "s3",
                    "S3_POS_SNAPSHOT_STALE",
                    2,
                    "position_snapshots 停滞",
                    f"last={last_snap} age={age:.0f}min dormant={dormant} (journal 休眠=设计)",
                    baseline=dormant,
                )
            )
        unopened = snap_tickets - opened
        if unopened:
            faults.append(
                _fault(
                    "s3",
                    "S3_SNAPSHOT_ORPHAN",
                    3,
                    "snapshot ticket 无 journal open",
                    f"count={len(unopened)}",
                )
            )

    # mt5_outbox 读取 (锁敏感)
    for ob in ("mt5_outbox", "mt5_outbox_processed"):
        d = dd / ob
        if not d.is_dir():
            continue
        for day in sorted(d.iterdir())[-2:]:
            if not day.is_dir():
                continue
            for f in day.glob("exec_bridge"):
                for _ln, obj, _raw in _iter_jsonl(f):
                    if obj == "LOCKED":
                        outbox_locked += 1
                        faults.append(
                            _fault(
                                "s3", "S3_OUTBOX_LOCKED", 3, f"{ob} 文件被运行进程锁", str(f), True
                            )
                        )
    return faults


# ---------------------------------------------------------------------------
# S4 记账链 (SSOT)
# ---------------------------------------------------------------------------


def s4_ledger_ssot(dd: Path, now: datetime) -> list[dict]:
    faults: list[dict] = []
    ledger = dd / "ledger_events.jsonl"
    if ledger.exists():
        seen: set[str] = set()
        dup = 0
        pnl_nan = 0
        event_types: dict[str, int] = {}
        last_ledger: datetime | None = None
        for _ln, obj, _raw in _iter_jsonl(ledger):
            if not isinstance(obj, dict):
                continue
            eid = obj.get("event_id")
            if eid:
                if eid in seen:
                    dup += 1
                seen.add(eid)
            et = obj.get("event_type", "?")
            event_types[et] = event_types.get(et, 0) + 1
            pnl = obj.get("pnl_r")
            if isinstance(pnl, (int, float)) and (pnl != pnl):  # NaN
                pnl_nan += 1
            t = _parse_dt(obj.get("timestamp"))
            if t and (last_ledger is None or t > last_ledger):
                last_ledger = t
        if dup:
            faults.append(_fault("s4", "S4_DUP_EVENT", 2, "重复 event_id", f"count={dup}"))
        if pnl_nan:
            faults.append(_fault("s4", "S4_PNL_NAN", 2, "ledger pnl_r NaN", f"count={pnl_nan}"))
        age = _safe_age_minutes(last_ledger, now)
        if _is_stale(last_ledger, now, _market_type_for_data_dir(dd), base_threshold_min=24 * 60):
            faults.append(
                _fault(
                    "s4",
                    "S4_LEDGER_STALE",
                    2,
                    "ledger 停滞",
                    f"last={last_ledger} age={age:.0f}min",
                )
            )
    else:
        faults.append(_fault("s4", "S4_LEDGER_MISSING", 1, "ledger_events.jsonl 缺失", str(ledger)))

    # 隔离区 (FIX-20260807-003 后应为 0 新增)
    q = dd / "journal_orphan_quarantine.jsonl"
    if q.exists():
        qcount = sum(1 for _ln, _obj, _raw in _iter_jsonl(q))
        if qcount:
            faults.append(
                _fault(
                    "s4",
                    "S4_QUARANTINE_RESIDUE",
                    3,
                    "隔离区残留",
                    f"count={qcount} (历史, IC 裁决 scoped-out)",
                    True,
                )
            )

    # golden_master 新鲜度
    gm = dd / "golden_master.jsonl"
    if gm.exists():
        gm_cnt = 0
        last_gm: datetime | None = None
        for _ln, obj, _raw in _iter_jsonl(gm):
            if not isinstance(obj, dict):
                continue
            gm_cnt += 1
            t = _parse_dt(obj.get("timestamp_utc") or obj.get("timestamp"))
            if t and (last_gm is None or t > last_gm):
                last_gm = t
        age = _safe_age_minutes(last_gm, now)
        if _is_stale(last_gm, now, _market_type_for_data_dir(dd), base_threshold_min=6 * 60):
            faults.append(
                _fault(
                    "s4", "S4_GM_STALE", 2, "golden_master 停滞", f"last={last_gm} age={age:.0f}min"
                )
            )
    else:
        faults.append(_fault("s4", "S4_GM_MISSING", 2, "golden_master.jsonl 缺失", str(gm)))

    # 记账链生命周期完整度 (journal open→close)
    journal = dd / "live_trade_journal.jsonl"
    if journal.exists():
        opened: set[int] = set()
        closed: set[int] = set()
        dup_msg = 0
        msg_seen: set[str] = set()
        for _ln, obj, _raw in _iter_jsonl(journal):
            if not isinstance(obj, dict):
                continue
            mid = obj.get("message_id")
            if mid:
                if mid in msg_seen:
                    dup_msg += 1
                msg_seen.add(mid)
            action = obj.get("action")
            # 生命周期对账用不可变身份 (position_identifier → ticket 回退, FIX-20260708-001)
            ident = _resolve_identity(obj)
            if not ident:
                continue
            if action == "open":
                opened.add(ident)
            elif action == "close":
                mid = str(obj.get("message_id", "") or "")
                # 历史 backfill/cleanup 假记录 (孤儿清账产物) 不参与生命周期对账
                if mid.startswith("close_orphan_backfill") or mid.startswith("ghost_cleanup"):
                    continue
                closed.add(ident)
        if dup_msg:
            faults.append(
                _fault("s4", "S4_DUP_MESSAGE_ID", 2, "重复 message_id", f"count={dup_msg}")
            )
        cwo = closed - opened
        if cwo:
            faults.append(
                _fault(
                    "s4",
                    "S4_CLOSE_WITHOUT_OPEN",
                    1,
                    "close 无对应 open (记账断裂)",
                    f"tickets={sorted(cwo)[:8]} count={len(cwo)}",
                )
            )
        # 未平仓 (opened - closed) 属正常持仓, 不报
    return faults


# ---------------------------------------------------------------------------
# S5 投影链 (Views)
# ---------------------------------------------------------------------------


def s5_views_projection(dd: Path, now: datetime) -> list[dict]:
    faults: list[dict] = []
    state_dir = dd / "state"
    check_files: list[tuple[str, Path]] = []
    if state_dir.is_dir():
        for name in (
            "execution_state.json",
            "governance_state.json",
            "data_health_state.json",
            "daily_ops_state.json",
            "regime_detector_state.json",
            "meta_filter_state.json",
        ):
            p = state_dir / name
            if p.exists():
                check_files.append((name, p))
    for name in (
        "brain_performance.json",
        "alpha_performance.json",
        "conformal_calibrator_state.json",
    ):
        p = dd / name
        if p.exists():
            check_files.append((name, p))

    if not check_files:
        faults.append(_fault("s5", "S5_NO_STATE", 3, "无投影状态文件", str(dd)))

    for name, p in check_files:
        obj, err = _read_json(p)
        if obj is None:
            if err and "IO" in err:
                faults.append(_fault("s5", "S5_STATE_LOCKED", 3, f"{name} 被锁", err, True))
            else:
                faults.append(_fault("s5", "S5_STATE_INVALID", 3, f"{name} 解析失败", str(err)))
            continue
        if isinstance(obj, dict):
            for key in ("updated_at", "last_run_utc", "timestamp"):
                if key in obj:
                    t = _parse_dt(obj[key])
                    age = _safe_age_minutes(t, now)
                    if _is_stale(t, now, _market_type_for_data_dir(dd), base_threshold_min=24 * 60):
                        faults.append(
                            _fault(
                                "s5",
                                "S5_STATE_STALE",
                                3,
                                f"{name} 停滞",
                                f"{key}={obj[key]} age={age:.0f}min",
                            )
                        )
                    break
            # 断路器
            cb = obj.get("circuit_breaker_tripped")
            if cb is True:
                faults.append(
                    _fault(
                        "s5",
                        "S5_CIRCUIT_TRIPPED",
                        2,
                        "执行断路器跳闸",
                        f"{name} circuit_breaker_tripped=true",
                    )
                )
            status = obj.get("overall_status")
            if status and status.lower() not in ("ok", "healthy", "pass"):
                # data_health CRITICAL ~41 为 IC 2026-08-06 裁决基线噪音 → scoped-out
                is_baseline = name == "data_health_state.json"
                faults.append(
                    _fault(
                        "s5",
                        "S5_STATE_STATUS",
                        3,
                        f"{name} 状态异常",
                        f"overall_status={status}",
                        baseline=is_baseline,
                    )
                )
    return faults


# ---------------------------------------------------------------------------
# S6 对账链
# ---------------------------------------------------------------------------


def s6_reconciliation(dd: Path, now: datetime) -> list[dict]:
    faults: list[dict] = []
    # data_health
    health = _read_json(dd / "state" / "data_health_state.json")
    if health[0] is not None:
        status = health[0].get("overall_status")
        if status and status.lower() not in ("ok", "healthy", "pass"):
            faults.append(
                _fault(
                    "s6",
                    "S6_HEALTH_CRITICAL",
                    2,
                    "data_health 非 OK",
                    f"overall_status={status}",
                    True,
                )
            )
    elif health[1]:
        faults.append(_fault("s6", "S6_HEALTH_UNREADABLE", 3, "data_health 不可读", health[1]))

    # 每日预检
    precheck_dir = dd / "state" / "daily_precheck"
    if precheck_dir.is_dir():
        reports = sorted(precheck_dir.glob("*.md"))
        if reports:
            latest = reports[-1]
            text = latest.read_text(encoding="utf-8", errors="replace")
            if "severity: **OK**" not in text and "severity: OK" not in text:
                faults.append(_fault("s6", "S6_PRECHECK_NOT_OK", 2, "最新预检非 OK", str(latest)))
            # 预检新鲜度 (报告标题日期)
            try:
                d = datetime.strptime(latest.stem, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                age = _safe_age_minutes(d, now)
                if _is_stale(d, now, _market_type_for_data_dir(dd), base_threshold_min=30 * 60):
                    faults.append(
                        _fault(
                            "s6",
                            "S6_PRECHECK_STALE",
                            3,
                            "预检报告过期",
                            f"{latest.stem} age={age:.0f}min",
                        )
                    )
            except ValueError:
                pass
        else:
            faults.append(_fault("s6", "S6_PRECHECK_NONE", 3, "无预检报告", str(precheck_dir)))

    # 已知数据损失登记
    dreg = _read_json(dd / "state" / "data_loss_register.json")
    if dreg[0] is not None:
        status = dreg[0].get("status", "")
        if "CLOSED" not in status and "RESOLVED" not in status:
            faults.append(_fault("s6", "S6_LOSS_OPEN", 2, "数据损失登记未关闭", status))
    return faults


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def audit(data_dir: str, now: datetime | None = None) -> dict:
    now = now or _now_utc()
    dd = Path(data_dir)
    if not dd.is_dir():
        return {"error": f"data-dir 不存在: {data_dir}", "segments": {}, "index": 0, "grade": "🔴"}

    runs = {
        "s1": s1_event_ingress(dd, now),
        "s2": s2_decision_chain(dd, now),
        "s3": s3_dispatch_execution(dd, now),
        "s4": s4_ledger_ssot(dd, now),
        "s5": s5_views_projection(dd, now),
        "s6": s6_reconciliation(dd, now),
    }

    segments: dict[str, dict] = {}
    all_faults: list[dict] = []
    for seg_id, faults in runs.items():
        score = _segment_score(faults)
        segments[seg_id] = {"name": SEGMENT_NAMES[seg_id], "score": score, "faults": faults}
        all_faults.extend(faults)

    index = int(round(sum(SEGMENT_WEIGHTS[s] * segments[s]["score"] for s in segments)))
    grade = "🟢" if index >= 90 else ("🟡" if index >= 75 else "🔴")
    fresh = [f for f in all_faults if not f.get("baseline")]
    baseline = [f for f in all_faults if f.get("baseline")]
    return {
        "data_dir": data_dir,
        "audited_at": now.isoformat(),
        "index": index,
        "grade": grade,
        "segments": segments,
        "fault_counts": {
            "fresh": len(fresh),
            "baseline_scoped": len(baseline),
            "by_sev": {
                "sev1": sum(1 for f in fresh if f["sev"] == 1),
                "sev2": sum(1 for f in fresh if f["sev"] == 2),
                "sev3": sum(1 for f in fresh if f["sev"] == 3),
                "sev4": sum(1 for f in fresh if f["sev"] == 4),
            },
        },
    }


def render_report(res: dict, include_baseline: bool = False) -> str:
    if "error" in res:
        return f"🔴 {res['error']}"
    lines: list[str] = []
    lines.append("=" * 74)
    lines.append(f"DATA CHAIN INTEGRITY INDEX — {res['data_dir']}")
    lines.append(f"audited_at: {res['audited_at']}")
    lines.append(f"INDEX: {res['index']}/100  {res['grade']}")
    fc = res["fault_counts"]
    lines.append(
        f"fresh faults: {fc['fresh']} (SEV1={fc['by_sev']['sev1']} SEV2={fc['by_sev']['sev2']} SEV3={fc['by_sev']['sev3']} SEV4={fc['by_sev']['sev4']}) | baseline scoped-out: {fc['baseline_scoped']}"
    )
    lines.append("=" * 74)
    for seg_id, seg in res["segments"].items():
        lines.append(f"[{seg['name']}] score={seg['score']}")
        fresh = [f for f in seg["faults"] if not f.get("baseline") or include_baseline]
        if not fresh:
            lines.append("    ✅ 无断层")
        for f in fresh:
            tag = " [baseline]" if f.get("baseline") else ""
            lines.append(
                f"    {SEV_LABEL[f['sev']]} {f['code']}{tag} — {f['title']}: {f['detail']}"
            )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="全数据链完整性审计")
    ap.add_argument("--data-dir", required=True, help="数据域 (data_btc=BTC | data=XAU)")
    ap.add_argument("--json", action="store_true", help="输出机器 JSON")
    ap.add_argument(
        "--include-baseline", action="store_true", help="计入 IC 裁决 scoped-out 的基线噪音"
    )
    ap.add_argument("--baseline-write", help="写基线 JSON 用于回归比对")
    ap.add_argument("--baseline-read", help="与既有基线 JSON 比对, 报告退化")
    ap.add_argument("--now", help="参考时间 ISO (可复现性)")
    args = ap.parse_args(argv)

    now = _parse_dt(args.now) if args.now else None
    res = audit(args.data_dir, now)
    if "--json" in sys.argv or args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
    else:
        print(render_report(res, include_baseline=args.include_baseline))

    if args.baseline_write:
        base = {
            "data_dir": res["data_dir"],
            "index": res["index"],
            "grade": res["grade"],
            "segment_scores": {k: v["score"] for k, v in res["segments"].items()},
            "fresh_fault_codes": sorted(
                {
                    f["code"]
                    for f in sum((s["faults"] for s in res["segments"].values()), [])
                    if not f.get("baseline")
                }
            ),
            "fault_counts": res["fault_counts"],
        }
        Path(args.baseline_write).parent.mkdir(parents=True, exist_ok=True)
        Path(args.baseline_write).write_text(
            json.dumps(base, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"[baseline-write] -> {args.baseline_write}")

    if args.baseline_read:
        bp = Path(args.baseline_read)
        if not bp.exists():
            print(f"🔴 baseline-read: 文件不存在 {bp}")
            return 2
        prev = json.loads(bp.read_text(encoding="utf-8"))
        deltas = []
        for k, v in res["segments"].items():
            if k in prev.get("segment_scores", {}):
                d = v["score"] - prev["segment_scores"][k]
                deltas.append(f"  {k}: {prev['segment_scores'][k]} -> {v['score']} ({d:+d})")
        new_faults = sorted(
            {
                f["code"]
                for s in res["segments"].values()
                for f in s["faults"]
                if not f.get("baseline")
            }
            - set(prev.get("fresh_fault_codes", []))
        )
        print(f"[baseline-compare] {args.data_dir}: index {prev.get('index')} -> {res['index']}")
        if deltas:
            print("\n".join(deltas))
        if new_faults:
            print(f"🔴 新增断层: {new_faults}")
        if res["index"] < prev.get("index", 0):
            print(f"🔴 指数退化 {(res['index'] - prev['index']):+d} — 阻断")
            return 1
        print(f"✅ 无退化 ({(res['index'] - prev.get('index', 0)):+d})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
