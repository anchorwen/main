"""Shadow intent producer: run V9 shadow model and write intents to mt5_shadow_outbox.

Run from repo root:
  python scripts/live_shadow_intent_producer.py --base-dir data --symbol XAUUSDc
  python scripts/live_shadow_intent_producer.py --base-dir data --once --dry-run

Intended to be called periodically (every 60s) by a scheduler. Writes
*.mt5.json files into mt5_shadow_outbox for audit / comparison only.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "shadow_intent.v1"


def _utc_now_iso() -> str:
    return (
        datetime.now(UTC)
        .replace(tzinfo=None)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _load_shadow_config(base_dir: Path) -> dict[str, Any]:
    """Load live_shadow_config.json from configs/ directory."""
    config_path = base_dir.parent / "configs" / "live_shadow_config.json"
    if not config_path.exists():
        raise FileNotFoundError(f"Shadow config not found: {config_path}")
    return json.loads(config_path.read_text(encoding="utf-8"))


def _build_intent_envelope(
    *,
    action: str,
    symbol: str,
    side: str,
    volume: float,
    sl: float | None = None,
    tp: float | None = None,
    position_ticket: int | None = None,
    model_version: str = "v9_shadow",
    scenario: str = "live_shadow",
) -> dict[str, Any]:
    """Create a mt5 handoff envelope for the shadow outbox."""
    message_id = f"shadow_{uuid.uuid4().hex[:12]}"
    payload: dict[str, Any] = {
        "action": action,
        "symbol": symbol,
        "side": side,
        "volume": volume,
        "execution_payload_schema": "mt5_market_open.v1",
        "model": model_version,
        "scenario": scenario,
    }
    if sl is not None:
        payload["sl"] = sl
    if tp is not None:
        payload["tp"] = tp
    if position_ticket is not None:
        payload["position_ticket"] = position_ticket

    return {
        "envelope": {
            "message_id": message_id,
            "target": "shadow_bridge",
            "generated_at": _utc_now_iso(),
            "execution_payload_schema": "mt5_handoff.v1",
            "payload": payload,
        }
    }


def _run_shadow_inference(
    *,
    feature_source_path: str | None = None,
    model_path: str,
    symbol: str,
) -> dict[str, Any]:
    """
    Placeholder: run V9 shadow model inference.
    In production, this loads the live feature snapshot and calls the model.
    Currently returns a default ABSTAIN signal.
    """
    try:
        # Attempt to import the engine for live feature extraction
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from core.features.live_feature_source import (
            snapshot_features,
        )
    except Exception:  # BLE001:REVIEWED
        # Fallback: return abstain if live feature extraction unavailable
        return {
            "action": "abstain",
            "symbol": symbol,
            "side": "none",
            "confidence": 0.0,
            "risk_allowed": True,
            "scenario": "feature_source_unavailable",
            "model": model_path,
        }

    try:
        features = snapshot_features(symbol=symbol, feature_source_path=feature_source_path)
    except Exception:  # BLE001:REVIEWED
        return {
            "action": "abstain",
            "symbol": symbol,
            "side": "none",
            "confidence": 0.0,
            "risk_allowed": True,
            "scenario": "feature_snapshot_failed",
            "model": model_path,
        }

    # Placeholder: apply model
    # In production: import joblib, load model, call predict(features)
    return {
        "action": "abstain",
        "symbol": symbol,
        "side": "none",
        "confidence": 0.0,
        "risk_allowed": True,
        "scenario": "model_not_yet_integrated",
        "model": model_path,
        "features_snapshot": features,
    }


def produce_shadow_intent(
    base_dir: Path,
    symbol: str,
    *,
    model_path: str,
    default_volume: float = 0.01,
    feature_source_path: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Run shadow inference and write an intent to mt5_shadow_outbox if actionable."""
    inference = _run_shadow_inference(
        feature_source_path=feature_source_path,
        model_path=model_path,
        symbol=symbol,
    )

    action = str(inference.get("action", "abstain")).lower()
    str(inference.get("side", "none")).lower()
    risk_allowed = bool(inference.get("risk_allowed", True))
    confidence = float(inference.get("confidence", 0.0))

    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utc_now_iso(),
        "symbol": symbol,
        "model_path": model_path,
        "inference": inference,
        "intent_written": False,
        "intent_path": None,
        "dry_run": dry_run,
    }

    # Only produce intents for open_long / open_short with risk allowed
    if action not in ("open_long", "open_short"):
        result["reason"] = f"non_actionable_action({action})"
        return result
    if not risk_allowed:
        result["reason"] = "risk_blocked"
        return result
    if confidence < 0.5:
        result["reason"] = f"low_confidence({confidence})"
        return result

    envelope = _build_intent_envelope(
        action=action,
        symbol=symbol,
        side="buy" if "long" in action else "sell",
        volume=default_volume,
        model_version="v9_shadow",
        scenario=inference.get("scenario", "live_shadow"),
    )

    if dry_run:
        result["intent_written"] = False
        result["reason"] = "dry_run"
        result["envelope"] = envelope
        return result

    outbox_root = base_dir / "mt5_shadow_outbox"
    date_key = datetime.now(UTC).replace(tzinfo=None).date().isoformat()
    target_dir = outbox_root / date_key / "shadow_bridge"
    target_dir.mkdir(parents=True, exist_ok=True)

    message_id = envelope["envelope"]["message_id"]
    intent_path = target_dir / f"{message_id}.mt5.json"
    intent_path.write_text(json.dumps(envelope, ensure_ascii=False, indent=2), encoding="utf-8")

    result["intent_written"] = True
    result["intent_path"] = str(intent_path.resolve())
    result["message_id"] = message_id
    return result


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="live_shadow_intent_producer")
    p.add_argument("--base-dir", default="data")
    p.add_argument("--symbol", default="XAUUSDc")
    p.add_argument("--model-path", default=None, help="Override model path from shadow config")
    p.add_argument("--default-volume", type=float, default=0.01)
    p.add_argument("--feature-source-path", default=None)
    p.add_argument("--once", action="store_true", help="Run once and exit")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--output", default=None, help="Write JSON result to file")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    base = Path(args.base_dir)

    try:
        config = _load_shadow_config(base)
        shadow = config.get("shadow", {})
    except FileNotFoundError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    except json.JSONDecodeError as exc:
        print(f"[ERROR] Invalid shadow config JSON: {exc}", file=sys.stderr)
        return 1

    model_path = args.model_path or shadow.get("model_path", "models/v9_shadow.pkl")
    default_volume = args.default_volume or shadow.get("default_volume", 0.01)

    result = produce_shadow_intent(
        base_dir=base,
        symbol=args.symbol,
        model_path=model_path,
        default_volume=default_volume,
        feature_source_path=args.feature_source_path,
        dry_run=args.dry_run,
    )

    text = json.dumps(result, indent=2, ensure_ascii=False, default=str)
    print(text)
    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
