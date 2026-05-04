"""Single-shot live stack diagnostic: policy eval (no flag writes), outbox queue, optional MT5 snapshot.

Run from repo root:
  python scripts/live_stack_diagnostic.py --base-dir data --symbol XAUUSDc
  python scripts/live_stack_diagnostic.py --base-dir data --mt5-terminal-path "D:\\MetaTrader 5\\terminal64.exe"
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from scripts.live_dispatch_policy import build_parser as policy_parser
from scripts.live_dispatch_policy import run_policy
from scripts.send_live_order import resolve_protection_flag_path


def _collect_outbox(outbox_root: Path, *, limit: int) -> tuple[int, list[str]]:
    if not outbox_root.exists():
        return 0, []
    paths = sorted(outbox_root.rglob("*.mt5.json"))
    sample = [str(p.as_posix()) for p in paths[:limit]]
    return len(paths), sample


def _zh_verdict(
    *,
    pending: int,
    dispatch_blocked: bool,
    reasons: list[str],
    flag_on_disk: bool,
    flag_blocked_payload: bool | None,
) -> str:
    parts: list[str] = []
    if pending == 0:
        parts.append(
            "当前 mt5_outbox 中没有待处理的 *.mt5.json：自动化链路（bridge worker）只是在「消费」已经写入 outbox 的文件；"
            "若没有任何进程向 MT5 handoff 投递意图（例如引擎 runtime 循环、或手动 send_live_order），则不会产生成交请求，表现为「永远不开单」。"
            "start_live_ops 只负责闸口策略 + bridge 常驻 + P1 日跑，本身不产生交易信号。"
        )
    else:
        parts.append(f"outbox 中有 {pending} 条待 bridge 处理的手递文件。")

    if flag_on_disk:
        parts.append(
            "磁盘上存在 live_dispatch_block.flag（闸口激活）。此时 mt5_bridge_worker 会对每条消息记为 rejected/protection_guard_active，"
            "不会在 MT5 下真实开仓。"
        )
        if flag_blocked_payload is False:
            parts.append(
                "注意：flag 文件内容与本次 --eval-only 策略结论不一致，建议以闸口 JSON 为准或重新跑一次不带 eval-only 的 live_dispatch_policy。"
            )

    if dispatch_blocked:
        parts.append(
            "本次策略评估认为当前应阻塞下发（dispatch_blocked=true），原因包含："
            + "; ".join(reasons)
            + "。在非 eval-only 模式下，policy 会写入/保持上述 flag。"
        )
    elif pending > 0 and not flag_on_disk:
        parts.append(
            "策略评估未阻塞且 flag 不存在：pending 手照应很快被 bridge 消费并尝试下单（仍可能因经纪商/Margin 等原因被拒，见 journal）。"
        )

    return " ".join(parts)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="live_stack_diagnostic")
    p.add_argument("--base-dir", default="data")
    p.add_argument("--symbol", default="XAUUSDc")
    p.add_argument("--journal-path", default=None)
    p.add_argument("--calendar-path", default=None)
    p.add_argument("--flag-path", default=None)
    p.add_argument(
        "--date", default=None, help="UTC date key for journal quality segment of policy"
    )
    p.add_argument(
        "--mt5-terminal-path", default=None, help="If set, runs mt5_positions_snapshot once"
    )
    p.add_argument(
        "--repo-root", default=None, help="Override cwd for mt5_positions_snapshot subprocess"
    )
    p.add_argument(
        "--output",
        default=None,
        help="Write full JSON report UTF-8 (fixes Windows console garbled Chinese)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    base = Path(args.base_dir)
    outbox = base / "mt5_outbox"
    flag_default = args.flag_path or "data/live_dispatch_block.flag"
    flag_path = resolve_protection_flag_path(str(args.base_dir), flag_default)

    pending, sample = _collect_outbox(outbox, limit=5)

    flag_on_disk = flag_path.exists()
    flag_blocked_payload: bool | None = None
    if flag_on_disk:
        try:
            raw = json.loads(flag_path.read_text(encoding="utf-8"))
            flag_blocked_payload = bool(raw.get("blocked", True))
        except (json.JSONDecodeError, OSError):
            flag_blocked_payload = None

    policy_args = policy_parser().parse_args(
        [
            "--base-dir",
            str(base),
            "--symbol",
            args.symbol,
            "--eval-only",
        ]
        + (["--journal-path", args.journal_path] if args.journal_path else [])
        + (["--calendar-path", args.calendar_path] if args.calendar_path else [])
        + ["--flag-path", str(flag_path)]
        + (["--date", args.date] if args.date else [])
    )
    _code, policy_result = run_policy(policy_args, gate_config=None)

    mt5_snapshot: dict | None = None
    if args.mt5_terminal_path:
        repo = Path(args.repo_root or ".").resolve()
        cmd = [
            sys.executable,
            str(repo / "scripts/mt5_positions_snapshot.py"),
            "--mt5-terminal-path",
            args.mt5_terminal_path,
            "--symbol",
            args.symbol,
            "--output",
            str(base / "reports/live_stack_diagnostic_mt5_snapshot.json"),
        ]
        try:
            subprocess.run(
                cmd, cwd=str(repo), check=False, capture_output=True, text=True, timeout=60
            )
            outp = base / "reports/live_stack_diagnostic_mt5_snapshot.json"
            if outp.exists():
                mt5_snapshot = json.loads(outp.read_text(encoding="utf-8"))
        except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError):
            mt5_snapshot = {"error": "snapshot_failed"}

    reasons = list(policy_result.get("reasons") or [])
    report = {
        "schema_version": "live_stack_diagnostic.v1",
        "architecture": {
            "producer": "任何写入 data/mt5_outbox/**/<target>/*.mt5.json 的流程（引擎 dispatch、send_live_order、go_live 脚本等）",
            "consumer": "mt5_bridge_worker 轮询 outbox，存在 protection flag 时拒单但仍归档并写 journal",
            "start_live_ops_role": "闸口 live_dispatch_policy + bridge_supervisor + P1 日跑；不产生 alpha 信号",
        },
        "paths": {
            "base_dir": str(base.resolve()),
            "outbox_dir": str(outbox.resolve()),
            "flag_path": str(flag_path.resolve()),
        },
        "outbox_pending_count": pending,
        "outbox_sample_paths": sample,
        "flag_file_present": flag_on_disk,
        "flag_payload_blocked_guess": flag_blocked_payload,
        "policy_eval_only": policy_result,
        "mt5_positions_snapshot": mt5_snapshot,
        "verdict_zh": _zh_verdict(
            pending=pending,
            dispatch_blocked=bool(policy_result.get("dispatch_blocked")),
            reasons=reasons,
            flag_on_disk=flag_on_disk,
            flag_blocked_payload=flag_blocked_payload,
        ),
        "primary_codes": _primary_codes(
            pending=pending,
            dispatch_blocked=bool(policy_result.get("dispatch_blocked")),
            flag_on_disk=flag_on_disk,
        ),
    }
    text = json.dumps(report, indent=2, ensure_ascii=False, default=str)
    print(text)
    if args.output:
        outp = Path(args.output)
        outp.parent.mkdir(parents=True, exist_ok=True)
        outp.write_text(text, encoding="utf-8")
    return 0


def _primary_codes(*, pending: int, dispatch_blocked: bool, flag_on_disk: bool) -> list[str]:
    codes: list[str] = []
    if pending == 0:
        codes.append("NO_OUTBOX_INTENTS")
    if dispatch_blocked:
        codes.append("POLICY_WOULD_BLOCK")
    if flag_on_disk:
        codes.append("PROTECTION_FLAG_ON_DISK")
    return codes


if __name__ == "__main__":
    raise SystemExit(main())
