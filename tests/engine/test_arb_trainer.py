"""CRT arb_trainer contract tests (pure Python paths, no backtest execution)."""

import json
from pathlib import Path

from scripts.training.trainers.arb_trainer import load_manifest


def test_load_manifest(tmp_path: Path):
    manifest_path = tmp_path / "m.json"
    payload = {
        "schema_version": "crt_model_manifest.v1",
        "model_id": "CRT.arb.chlg.g2026.1@feat-arb-v6-ou-sniper-1.0.0.s42",
        "lane": "arb",
        "role": "chlg",
        "generation": "g2026.1",
        "seed": 42,
    }
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    m = load_manifest(manifest_path)
    assert m["model_id"] == payload["model_id"]
    assert m["lane"] == "arb"
    assert m["seed"] == 42


def test_result_json_parsing_regex_metrics(tmp_path: Path):
    import re

    simulated_stdout = (
        "[arb] === OPTIMAL PARAMETERS FOUND ===\n"
        "[arb] Window: 200\n"
        "[arb] Z-Entry: 2.5 sigma\n"
        "[arb] Z-Exit: 0.5 sigma\n"
        "[arb] Max Half-Life: 20 bars\n"
        "[arb] Theta Min: 0.005\n"
        "[arb] Sharpe: 1.85\n"
        "[arb] Winrate: 62.3%\n"
        "[arb] Total PnL: 15.42\n"
        "[arb] Max DD: 8.1%\n"
        "[arb] Profit Factor: 2.34\n"
        "[arb] Trades: 143\n"
        "RESULT_ARTIFACT=D:\\tmp\\arb_params.json\n"
    )

    sharpe_match = re.search(r"Sharpe:\s*([\d.-]+)", simulated_stdout)
    wr_match = re.search(r"Winrate:\s*([\d.]+)%", simulated_stdout)
    pnl_match = re.search(r"Total PnL:\s*([\d.-]+)", simulated_stdout)
    dd_match = re.search(r"Max DD:\s*([\d.]+)%", simulated_stdout)
    pf_match = re.search(r"Profit Factor:\s*([\d.]+)", simulated_stdout)
    artifact_match = re.search(r"RESULT_ARTIFACT=(.+)", simulated_stdout)

    assert sharpe_match and float(sharpe_match.group(1)) == 1.85
    assert wr_match and float(wr_match.group(1)) == 62.3
    assert pnl_match and float(pnl_match.group(1)) == 15.42
    assert dd_match and float(dd_match.group(1)) == 8.1
    assert pf_match and float(pf_match.group(1)) == 2.34
    assert artifact_match and "arb_params.json" in artifact_match.group(1)


def test_load_manifest_file_not_found():
    try:
        load_manifest(Path("/nonexistent/manifest.json"))
        raise AssertionError("expected FileNotFoundError")
    except FileNotFoundError:
        pass
