"""TECH_DEBT-010 清偿回归测试 — Blueprint A (Shadow Veto) + Blueprint C (Death of Defaults).

影子风暴 (8/3-8/7, 281 条 message_ 幽灵单经真 ZMQ 5556 达 XAU 桥) 双裂缝:
  ① service_container 默认 `tcp://127.0.0.1:5556` (XAU) 兜底 → 多品种串台;
  ② modify_trail_dispatch / close 路径漏传 per-symbol endpoint → BTC 落 XAU 桥。

本测试锁死:
  - Shadow Veto: 影子容器遇生产网络适配器 (mt5_zmq) → DataIntegrityError 宕机。
  - 多品种端口解析: mt5_zmq 未显式注入 endpoint → fail-fast (无默认兜底);
    显式注入 → 适配器绑定正确 per-symbol endpoint。
  - zmq_adapter 构造必传 endpoint (无默认)。
  - modify_trail_dispatch 显式注入 endpoint → 透传到 dispatch extensions;
    空 endpoint (mt5 file 适配器) → 不注入 (零行为变化)。
"""

import pytest

from core.contracts.exceptions import DataIntegrityError
from core.deployment.environment_config import EnvironmentConfig
from core.deployment.service_container import ServiceContainer
from core.protocol.services.zmq_communication_adapter import ZMQCommunicationAdapter

# ── Blueprint C1: service_container 多品种端口解析 ──────────────────────


def test_mt5_zmq_without_endpoint_raises_data_integrity_error(tmp_path):
    """mt5_zmq 未显式注入 zmq_order_endpoint → DataIntegrityError (fail-fast, 无默认兜底)."""
    terminal = tmp_path / "terminal64.exe"
    terminal.write_text("", encoding="utf-8")
    (tmp_path / "data").mkdir(exist_ok=True)
    cfg = EnvironmentConfig.production(
        base_dir=str(tmp_path / "data"),
        adapter_name="mt5_zmq",
        extensions={"mt5_terminal_path": str(terminal)},
    )
    with pytest.raises(DataIntegrityError, match="zmq_order_endpoint"):
        ServiceContainer(cfg).build()


def test_mt5_zmq_with_endpoint_binds_correct_symbol_port(tmp_path):
    """显式注入 per-symbol endpoint → 适配器绑定该 endpoint (无 5556 兜底)."""
    terminal = tmp_path / "terminal64.exe"
    terminal.write_text("", encoding="utf-8")
    (tmp_path / "data").mkdir(exist_ok=True)
    cfg = EnvironmentConfig.production(
        base_dir=str(tmp_path / "data"),
        adapter_name="mt5_zmq",
        extensions={
            "mt5_terminal_path": str(terminal),
            "zmq_order_endpoint": "tcp://127.0.0.1:5558",  # BTC 桥
        },
    )
    container = ServiceContainer(cfg).build()
    dispatcher = container.dispatcher
    assert dispatcher is not None
    adapter = dispatcher._adapter
    assert adapter is not None
    assert adapter.adapter_name.startswith("mt5_zmq")
    assert adapter._order_endpoint == "tcp://127.0.0.1:5558"


def test_mt5_file_adapter_requires_no_endpoint(tmp_path):
    """mt5 (file outbox) 适配器不要求 endpoint — 回归锁 (零行为变化)."""
    terminal = tmp_path / "terminal64.exe"
    terminal.write_text("", encoding="utf-8")
    outbox_dir = tmp_path / "mt5_outbox"
    (tmp_path / "data").mkdir(exist_ok=True)
    cfg = EnvironmentConfig.production(
        base_dir=str(tmp_path / "data"),
        adapter_name="mt5",
        extensions={
            "mt5_terminal_path": str(terminal),
            "mt5_outbox_dir": str(outbox_dir),
        },
    )
    container = ServiceContainer(cfg).build()
    dispatcher = container.dispatcher
    assert dispatcher is not None
    assert dispatcher._adapter is not None


# ── Blueprint C2: zmq_adapter 构造必传 endpoint ─────────────────────────


def test_zmq_adapter_constructor_requires_order_endpoint():
    """ZMQCommunicationAdapter 构造必传 order_endpoint — 无默认端口兜底."""
    with pytest.raises(TypeError):
        ZMQCommunicationAdapter()  # type: ignore[call-arg]  # 签名必传, 无默认


# ── Blueprint A: Shadow Veto ────────────────────────────────────────────


def test_shadow_veto_raises_on_network_adapter(tmp_path, monkeypatch):
    """影子容器遇生产网络适配器 (mt5_zmq) → DataIntegrityError 宕机."""
    from apps.engine import bootstrap_v9

    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    (cfg_dir / "live.yaml").write_text("adapter:\n  name: mt5_zmq\n", encoding="utf-8")
    monkeypatch.setattr(bootstrap_v9, "_repo_root", lambda: tmp_path)
    with pytest.raises(DataIntegrityError, match="Shadow Veto"):
        bootstrap_v9.build_v9_shadow_container()


def test_shadow_veto_passes_with_stub_adapter(tmp_path, monkeypatch):
    """stub 适配器 → veto 不触发, 正常走 build (影子合法路径)."""
    from apps.engine import bootstrap_v9

    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    (cfg_dir / "live.yaml").write_text("adapter:\n  name: stub\n", encoding="utf-8")
    monkeypatch.setattr(bootstrap_v9, "_repo_root", lambda: tmp_path)

    built = []

    class _FakeRegistry:
        def register(self, *a, **k):
            return None

    class _FakeGov:
        def register_brain(self, *a, **k):
            return None

    class _FakeContainer:
        def __init__(self):
            self.brain_registry = _FakeRegistry()
            self.governance_service = _FakeGov()

        def build(self):
            return self

    class _FakeServiceContainer:
        def __init__(self, *a, **k):
            built.append(("container", a, k))

        def build(self):
            return _FakeContainer()

    monkeypatch.setattr(bootstrap_v9, "ServiceContainer", _FakeServiceContainer)
    monkeypatch.setattr(bootstrap_v9, "_wire_meta_pipeline", lambda *a, **k: None)

    container = bootstrap_v9.build_v9_shadow_container()
    assert container.brain_registry is not None
    assert container.governance_service is not None
    # 构造时 adapter 从 live.yaml 解析为 stub, 传入 ServiceContainer 的是 stub
    assert built[0][1][0].adapter_name == "stub"


# ── Blueprint C5: modify_trail_dispatch endpoint 注入 ───────────────────


def test_modify_trail_dispatch_injects_endpoint(monkeypatch):
    """dispatch_modify_trail 显式注入 endpoint → 透传到 dispatch_live_order extensions."""
    from core.runtime.modify_trail_dispatch import dispatch_modify_trail

    captured = {}

    def _fake_dispatch_live_order(**kwargs):
        captured.update(kwargs)
        return {"status": "ok", "ticket": 1001}

    import core.execution.live_order_sender as los

    # dispatch_modify_trail 函数体内 `from core.execution.live_order_sender
    # import dispatch_live_order` — 必须 patch 源模块。
    monkeypatch.setattr(los, "dispatch_live_order", _fake_dispatch_live_order)

    dispatch_modify_trail(
        base_dir="data",
        symbol="BTCUSDc",
        adapter_name="mt5_zmq",
        mt5_terminal_path="D:/x/terminal64.exe",
        ignore_protection_flag=True,
        protection_flag_path="",
        pos_side="short",
        pos_ticket=1001,
        new_sl=64000.0,
        new_tp=63000.0,
        strategy_name="test_strategy",
        zmq_order_endpoint="tcp://127.0.0.1:5558",
    )
    assert captured["extensions"]["zmq_order_endpoint"] == "tcp://127.0.0.1:5558"
    assert captured["extensions"]["mt5_terminal_path"] == "D:/x/terminal64.exe"


def test_modify_trail_dispatch_empty_endpoint_omitted(monkeypatch):
    """空 endpoint (mt5 file 适配器) → 不注入 zmq_order_endpoint (零行为变化)."""
    from core.runtime.modify_trail_dispatch import dispatch_modify_trail

    captured = {}

    def _fake_dispatch_live_order(**kwargs):
        captured.update(kwargs)
        return {"status": "ok", "ticket": 1002}

    import core.execution.live_order_sender as los

    monkeypatch.setattr(los, "dispatch_live_order", _fake_dispatch_live_order)

    dispatch_modify_trail(
        base_dir="data",
        symbol="XAUUSDc",
        adapter_name="mt5",
        mt5_terminal_path="D:/x/terminal64.exe",
        ignore_protection_flag=True,
        protection_flag_path="",
        pos_side="long",
        pos_ticket=1002,
        new_sl=4300.0,
        new_tp=4400.0,
        strategy_name="test_strategy",
        zmq_order_endpoint="",  # mt5 file 适配器 — 空串不注入
    )
    assert "zmq_order_endpoint" not in captured["extensions"]
    assert captured["extensions"]["mt5_terminal_path"] == "D:/x/terminal64.exe"
