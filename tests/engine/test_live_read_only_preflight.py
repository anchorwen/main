import json

from scripts.live_read_only_preflight import build_report, main


def test_live_read_only_preflight_report_ready_for_observation(tmp_path):
    terminal = tmp_path / "terminal64.exe"
    terminal.write_text("", encoding="utf-8")

    report = build_report(
        base_dir=str(tmp_path / "data"),
        mt5_terminal_path=str(terminal),
    )

    assert report["ready_for_observation"] is True
    assert report["status_check"]["live_read_only"] is True
    assert report["status_check"]["mt5_terminal_path"] == str(terminal)
    assert report["selftest"]["all_passed"] is True
    assert report["dispatch_guard"]["blocked"] is True
    assert report["dispatch_guard"]["adapter_name"] == "live_read_only_guard"
    assert report["dispatch_guard"]["failure_reason"] == "live_read_only_enabled"


def test_live_read_only_preflight_main_writes_output(tmp_path, capsys):
    terminal = tmp_path / "terminal64.exe"
    terminal.write_text("", encoding="utf-8")
    output = tmp_path / "reports" / "preflight.json"

    rc = main(
        [
            "--base-dir",
            str(tmp_path / "data"),
            "--mt5-terminal-path",
            str(terminal),
            "--output",
            str(output),
        ]
    )

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ready_for_observation"] is True
    assert output.exists()


def test_live_preflight_main_supports_micro_live_mode(tmp_path, capsys):
    terminal = tmp_path / "terminal64.exe"
    terminal.write_text("", encoding="utf-8")
    output = tmp_path / "reports" / "micro_live.json"

    rc = main(
        [
            "--mode",
            "micro_live",
            "--base-dir",
            str(tmp_path / "data"),
            "--mt5-terminal-path",
            str(terminal),
            "--symbol",
            "XAUUSD",
            "--max-open-positions",
            "1",
            "--max-notional-exposure",
            "5000",
            "--output",
            str(output),
        ]
    )

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["go_for_micro_live"] is True
    assert payload["effective"]["live_allowed_symbols"] == ["XAUUSD"]
    assert output.exists()
