from scripts.live_micro_rollout_gate import build_report, main


def test_live_micro_rollout_gate_report_go_for_micro_live(tmp_path):
    terminal = tmp_path / "terminal64.exe"
    terminal.write_text("", encoding="utf-8")

    report = build_report(
        base_dir=str(tmp_path / "data"),
        mt5_terminal_path=str(terminal),
        symbol="XAUUSD",
        max_open_positions=1,
        max_notional_exposure=5_000.0,
    )

    assert report["go_for_micro_live"] is True
    assert report["checks"]["single_symbol_allowlist"] is True
    assert report["effective"]["live_dispatch_enabled"] is True
    assert report["effective"]["live_allowed_symbols"] == ["XAUUSD"]
    assert report["checks"]["dispatch_probe_routed_to_mt5_adapter"] is True
    assert report["checks"]["dispatch_probe_delivered"] is True
    assert report["dispatch_probe"]["adapter_name"] == "mt5_adapter"


def test_live_micro_rollout_gate_rejects_loose_limits(tmp_path):
    terminal = tmp_path / "terminal64.exe"
    terminal.write_text("", encoding="utf-8")

    report = build_report(
        base_dir=str(tmp_path / "data"),
        mt5_terminal_path=str(terminal),
        symbol="XAUUSD",
        max_open_positions=2,
        max_notional_exposure=20_000.0,
    )

    assert report["go_for_micro_live"] is False
    assert report["checks"]["max_open_positions_limited"] is False
    assert report["checks"]["max_notional_exposure_limited"] is False


def test_live_micro_rollout_gate_main_writes_output(tmp_path):
    terminal = tmp_path / "terminal64.exe"
    terminal.write_text("", encoding="utf-8")
    output = tmp_path / "reports" / "micro_live.json"

    exit_code = main(
        [
            "--base-dir",
            str(tmp_path / "data"),
            "--mt5-terminal-path",
            str(terminal),
            "--symbol",
            "XAUUSD",
            "--output",
            str(output),
        ]
    )

    assert exit_code == 0
    assert output.exists()
    assert '"go_for_micro_live": true' in output.read_text(encoding="utf-8")
