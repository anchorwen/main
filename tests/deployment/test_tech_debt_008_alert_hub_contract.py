"""TECH_DEBT-008 清偿回归测试 — LiveAlertHub 签名契约锁 (zombie-fuse 告警静默根治).

根因 (FIX-20260819-003, DQAF-20260819-003): live_intent_loop.py zombie-fuse 告警块
调用 LiveAlertHub 时三重错配:
  1. 构造参数签名漂移 — log_dir/ding_webhook_url 不存在, 应为 base_dir/symbol/
     dingtalk_url/dingtalk_secret;
  2. 告警方法漂移 — .fire() 不存在, 应为 .send_critical(reason, detail);
  3. state._alert_hub 恒 None (从未赋值) → fallback 构造每次触发 TypeError+AttributeError
     → 被 BLE001:FOG except 吞掉 → 熔断告警从未送达, 只剩本地 watchdog_kill.log.

本测试锁死 LiveAlertHub 契约 (防再次漂移):
  - 构造接受 base_dir/symbol/dingtalk_url/dingtalk_secret 关键字 (zombie-fuse fallback 用).
  - send_critical(reason, detail) 是现行告警入口 (取代已漂移的 .fire()).
  - fire() 不存在 — 若未来恢复旧调用面, 此断言即红旗.
  - 无 webhook 时 send_critical 不抛异常 (fail-open 落盘, 不阻塞 fuse 后 exit 路径).
"""

from core.observability.live_alert_hub import LiveAlertHub


def _make_hub(tmp_path) -> LiveAlertHub:
    return LiveAlertHub(
        base_dir=str(tmp_path),
        symbol="XAUUSDc",
        dingtalk_url="",
        dingtalk_secret="",
    )


def test_live_alert_hub_constructor_accepts_fallback_kwargs(tmp_path):
    """zombie-fuse fallback 构造参数 (base_dir/symbol/dingtalk_url/dingtalk_secret)."""
    hub = _make_hub(tmp_path)
    try:
        assert hub is not None
    finally:
        hub.shutdown()


def test_live_alert_hub_send_critical_is_current_entry(tmp_path):
    """send_critical(reason, detail) 取代 .fire() — 无 webhook 时 fail-open 不抛异常."""
    hub = _make_hub(tmp_path)
    try:
        hub.send_critical(
            "zombie_cycle_fuse_blown",
            detail={
                "consecutive_errors": 5,
                "last_error_type": "ValueError",
                "last_error": "test-error",
                "last_traceback": "test-traceback",
                "cycle_count": 1,
            },
        )
    finally:
        hub.shutdown()


def test_live_alert_hub_fire_method_absent(tmp_path):
    """旧调用面 .fire() 不存在 — 恢复即签名漂移复发."""
    hub = _make_hub(tmp_path)
    try:
        assert not hasattr(hub, "fire")
    finally:
        hub.shutdown()
