"""TECH_DEBT-010 清偿回归测试 — Blueprint B (The Journal Firewall).

跨域拒签: XAU 账本只准进 XAUUSD(c), BTC 账本只准进 BTCUSDc。
根因: 7/20-8/4 BTC 227 条 modify_sltp 串台进 data/ (XAU 账本)。

本测试锁死:
  - `_journal_firewall_reason` 纯函数全分支 (跨域拒 / 同域放 / 未知 symbol 放 / 无域放)。
  - `_resolve_journal_domain` 域推导 (显式优先 / default_symbol 前缀推导)。
  - `_append_journal` 集成: 跨域记录写 cross_domain_warnings 且绝不进主账本 SSOT;
    同域记录正常进主账本。
"""

import pytest

from scripts import mt5_bridge_worker as bridge


@pytest.fixture(autouse=True)
def _reset_journal_domain():
    """每个测试后清空模块级域状态, 防止污染其它测试。"""
    yield
    bridge.set_journal_domain(None)


# ── _journal_firewall_reason 纯函数 ─────────────────────────────────────


def test_firewall_xau_domain_rejects_btc_symbol():
    bridge.set_journal_domain("XAU")
    reason = bridge._journal_firewall_reason({"symbol": "BTCUSDc"})
    assert reason is not None
    assert "BTCUSDc" in reason


def test_firewall_xau_domain_admits_xau_symbols():
    bridge.set_journal_domain("XAU")
    assert bridge._journal_firewall_reason({"symbol": "XAUUSDc"}) is None
    assert bridge._journal_firewall_reason({"symbol": "XAUUSD"}) is None


def test_firewall_btc_domain_rejects_xau_symbol():
    bridge.set_journal_domain("BTC")
    assert bridge._journal_firewall_reason({"symbol": "XAUUSDc"}) is not None


def test_firewall_btc_domain_admits_btc_symbol():
    bridge.set_journal_domain("BTC")
    assert bridge._journal_firewall_reason({"symbol": "BTCUSDc"}) is None


def test_firewall_unknown_symbol_admitted_for_legacy():
    """symbol 缺失/未知无法证明跨域 → 告警放行 (backward compat)。"""
    bridge.set_journal_domain("XAU")
    assert bridge._journal_firewall_reason({}) is None


def test_firewall_no_domain_admitted():
    """未武装域 → 全部放行 (Firewall disarmed)。"""
    bridge.set_journal_domain(None)
    assert bridge._journal_firewall_reason({"symbol": "BTCUSDc"}) is None


# ── _resolve_journal_domain 域推导 ──────────────────────────────────────


def test_resolve_domain_explicit_wins():
    assert bridge._resolve_journal_domain("XAU", "BTCUSDc") == "XAU"
    assert bridge._resolve_journal_domain("BTC", "XAUUSDc") == "BTC"


def test_resolve_domain_from_default_symbol_prefix():
    assert bridge._resolve_journal_domain(None, "XAUUSDc") == "XAU"
    assert bridge._resolve_journal_domain(None, "BTCUSDc") == "BTC"


def test_resolve_domain_none_without_signal():
    assert bridge._resolve_journal_domain(None, None) is None
    assert bridge._resolve_journal_domain("weird", None) is None


# ── _append_journal 集成: 跨域拒签 ──────────────────────────────────────


def test_append_journal_blocks_cross_domain(tmp_path):
    journal_path = tmp_path / "live_trade_journal.jsonl"
    bridge.set_journal_domain("XAU")
    record = {
        "message_id": "modify_cross_001",
        "action": "modify_sltp",
        "symbol": "BTCUSDc",  # 跨域 — XAU 账本不收 BTC
        "position_ticket": 1001,
        "recorded_at": "2026-08-19T00:00:00Z",
    }
    bridge._append_journal(journal_path, record)

    # 主账本 SSOT 绝不写入
    assert not journal_path.exists()
    # 拦截记录打入 cross_domain_warnings
    warning_path = tmp_path / "cross_domain_warnings.jsonl"
    assert warning_path.exists()
    blocked = warning_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(blocked) == 1
    payload = __import__("json").loads(blocked[0])
    assert payload["symbol"] == "BTCUSDc"
    assert payload["_cross_domain_blocked_at"]


def test_append_journal_admits_same_domain(tmp_path):
    journal_path = tmp_path / "live_trade_journal.jsonl"
    bridge.set_journal_domain("XAU")
    record = {
        "message_id": "modify_same_domain_001",
        "action": "modify_sltp",
        "symbol": "XAUUSDc",  # 同域 — 放行进入 SSOT
        "position_ticket": 1002,
        "recorded_at": "2026-08-19T00:00:00Z",
    }
    bridge._append_journal(journal_path, record)

    # 主账本写入
    assert journal_path.exists()
    written = journal_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(written) == 1
    # 无跨域告警文件
    assert not (tmp_path / "cross_domain_warnings.jsonl").exists()


def test_append_journal_btc_domain_blocks_xau(tmp_path):
    journal_path = tmp_path / "live_trade_journal.jsonl"
    bridge.set_journal_domain("BTC")
    record = {
        "message_id": "modify_xau_into_btc_001",
        "action": "modify_sltp",
        "symbol": "XAUUSDc",  # 跨域 — BTC 账本不收 XAU
        "position_ticket": 1003,
        "recorded_at": "2026-08-19T00:00:00Z",
    }
    bridge._append_journal(journal_path, record)

    assert not journal_path.exists()
    warning_path = tmp_path / "cross_domain_warnings.jsonl"
    assert warning_path.exists()
    payload = __import__("json").loads(
        warning_path.read_text(encoding="utf-8").strip().splitlines()[0]
    )
    assert payload["symbol"] == "XAUUSDc"
