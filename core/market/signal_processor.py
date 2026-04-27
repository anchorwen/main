from datetime import datetime


class SignalFilter:
    """Filters and deduplicates incoming market signals before
    they enter the decision pipeline.

    Guards against:
    - Duplicate signals (same symbol within cooldown)
    - Stale data (timestamps too old)
    - Missing required fields
    - Symbols not in the allowed universe
    """

    def __init__(
        self,
        *,
        cooldown_seconds: float = 1.0,
        max_staleness_seconds: float = 30.0,
        allowed_symbols: set[str] | None = None,
    ):
        self._cooldown = cooldown_seconds
        self._max_staleness = max_staleness_seconds
        self._allowed_symbols = allowed_symbols
        self._last_seen: dict[str, datetime] = {}
        self._stats = {"accepted": 0, "rejected_duplicate": 0,
                       "rejected_stale": 0, "rejected_invalid": 0,
                       "rejected_symbol": 0}

    def accept(self, signal: dict) -> tuple[bool, str]:
        symbol = signal.get("symbol")
        if not symbol:
            self._stats["rejected_invalid"] += 1
            return False, "missing_symbol"

        if self._allowed_symbols and symbol not in self._allowed_symbols:
            self._stats["rejected_symbol"] += 1
            return False, "symbol_not_allowed"

        ts = signal.get("timestamp")
        if ts and isinstance(ts, datetime):
            age = (datetime.utcnow() - ts).total_seconds()
            if age > self._max_staleness:
                self._stats["rejected_stale"] += 1
                return False, "stale_signal"

        now = datetime.utcnow()
        last = self._last_seen.get(symbol)
        if last and (now - last).total_seconds() < self._cooldown:
            self._stats["rejected_duplicate"] += 1
            return False, "duplicate_cooldown"

        self._last_seen[symbol] = now
        self._stats["accepted"] += 1
        return True, "accepted"

    def get_stats(self) -> dict:
        return dict(self._stats)

    def reset(self) -> None:
        self._last_seen.clear()


class MarketSignalProcessor:
    """Bridges market data feeds to the decision pipeline.

    Receives raw market signals, filters them, updates market
    context, and triggers decision cycles via the SystemFacade.
    """

    def __init__(self, facade, signal_filter: SignalFilter | None = None, market_context=None):
        self._facade = facade
        self._filter = signal_filter or SignalFilter()
        self._market_ctx = market_context
        self._processed = 0
        self._triggered = 0

    def process_tick(self, signal: dict) -> dict:
        self._processed += 1
        accepted, reason = self._filter.accept(signal)
        if not accepted:
            return {"status": "filtered", "reason": reason, "symbol": signal.get("symbol")}

        if self._market_ctx:
            self._market_ctx.update(
                symbol=signal["symbol"],
                bid=signal.get("bid", 0),
                ask=signal.get("ask", 0),
            )

        features = {k: v for k, v in signal.items()
                    if k not in {"symbol", "timestamp", "bid", "ask"}}

        result = self._facade.decide(signal["symbol"], features)
        self._triggered += 1
        return {"status": "triggered", "decision": result, "symbol": signal["symbol"]}

    def process_batch(self, signals: list[dict]) -> dict:
        results = []
        for s in signals:
            results.append(self.process_tick(s))
        triggered = sum(1 for r in results if r["status"] == "triggered")
        filtered = sum(1 for r in results if r["status"] == "filtered")
        return {
            "total": len(signals),
            "triggered": triggered,
            "filtered": filtered,
            "results": results,
        }

    def get_stats(self) -> dict:
        return {
            "processed": self._processed,
            "triggered": self._triggered,
            "filter_stats": self._filter.get_stats(),
        }
