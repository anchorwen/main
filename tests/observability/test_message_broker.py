"""Tests for message broker abstraction."""

from __future__ import annotations

import pytest

from core.observability.message_broker import (
    InProcessBroker,
    Message,
    MessageBroker,
    get_broker,
    reset_broker_registry,
)


class TestMessage:
    def test_to_dict_roundtrip(self):
        msg = Message(
            event_type="trade.filled",
            payload={"symbol": "XAUUSDc", "volume": 0.01},
            correlation_id="corr_123",
            source="engine",
        )
        d = msg.to_dict()
        assert d["event_type"] == "trade.filled"
        assert d["payload"]["symbol"] == "XAUUSDc"
        assert d["correlation_id"] == "corr_123"

    def test_from_dict(self):
        d = {
            "event_type": "alert.critical",
            "payload": {"msg": "drawdown"},
            "message_id": "abc123",
            "correlation_id": "",
            "timestamp": "2026-01-01T00:00:00",
            "source": "risk",
            "retry_count": 0,
        }
        msg = Message.from_dict(d)
        assert msg.event_type == "alert.critical"
        assert msg.payload["msg"] == "drawdown"
        assert msg.message_id == "abc123"

    def test_default_message_id_unique(self):
        m1 = Message(event_type="test")
        m2 = Message(event_type="test")
        assert m1.message_id != m2.message_id


class TestInProcessBroker:
    def test_publish_delivers_to_subscriber(self):
        broker = InProcessBroker()
        received: list[dict] = []

        def handler(et: str, payload: dict) -> None:
            received.append({"event_type": et, "payload": payload})

        broker.subscribe("test.event", handler)
        count = broker.publish("test.event", {"key": "value"})
        assert count == 1
        assert len(received) == 1
        assert received[0]["payload"]["key"] == "value"

    def test_multiple_subscribers(self):
        broker = InProcessBroker()
        calls = []

        def h1(et, p):
            calls.append("h1")

        def h2(et, p):
            calls.append("h2")

        broker.subscribe("e", h1)
        broker.subscribe("e", h2)
        count = broker.publish("e")
        assert count == 2
        assert calls == ["h1", "h2"]

    def test_unsubscribe_removes_handler(self):
        broker = InProcessBroker()
        calls = []

        def h(et, p):
            calls.append(1)

        broker.subscribe("e", h)
        broker.unsubscribe("e", h)
        count = broker.publish("e")
        assert count == 0
        assert calls == []

    def test_no_subscribers_zero_delivered(self):
        broker = InProcessBroker()
        count = broker.publish("no.such.event")
        assert count == 0

    def test_handler_exception_does_not_crash(self):
        broker = InProcessBroker()
        received = []

        def bad(et, p):
            raise RuntimeError("boom")

        def good(et, p):
            received.append(1)

        broker.subscribe("e", bad)
        broker.subscribe("e", good)
        count = broker.publish("e")
        assert count == 1  # bad failed, good succeeded — only successful counted
        assert received == [1]

    def test_event_log_accumulates(self):
        broker = InProcessBroker()
        broker.publish("a", {"n": 1})
        broker.publish("b", {"n": 2})
        log = broker.event_log
        assert len(log) >= 2

    def test_backend_name(self):
        assert InProcessBroker().backend_name == "inprocess"


class TestRedisFallback:
    """RedisStreamsBroker tests — always use fallback since redis may be absent."""

    def test_redis_broker_falls_back_gracefully(self):
        from core.observability.message_broker import RedisStreamsBroker

        broker = RedisStreamsBroker(url="redis://localhost:9999", use_inprocess_fallback=True)
        assert "fallback" in broker.backend_name

        received = []

        def h(et, p):
            received.append(p)

        broker.subscribe("test", h)
        broker.publish("test", {"x": 1})
        assert len(received) == 1
        assert received[0]["x"] == 1
        broker.close()

    def test_redis_broker_no_fallback_still_works(self):
        from core.observability.message_broker import RedisStreamsBroker

        broker = RedisStreamsBroker(url="redis://localhost:9999", use_inprocess_fallback=False)
        # No crash on publish
        broker.publish("test", {"x": 1})
        received = []

        def h(et, p):
            received.append(p)

        broker.subscribe("test", h)
        broker.publish("test", {"x": 2})
        assert len(received) == 1
        broker.close()


class TestGetBroker:
    def teardown_method(self):
        reset_broker_registry()

    def test_inprocess_singleton(self):
        b1 = get_broker("inprocess")
        b2 = get_broker("inprocess")
        assert b1 is b2

    def test_auto_returns_broker(self):
        broker = get_broker("auto")
        assert isinstance(broker, MessageBroker)
        assert "inprocess" in broker.backend_name or "redis" in broker.backend_name

    def test_unknown_backend_raises(self):
        with pytest.raises(ValueError, match="Unknown broker"):
            get_broker("kafka")
