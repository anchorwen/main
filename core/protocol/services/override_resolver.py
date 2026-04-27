class OverrideResolver:
    def resolve(self, symbol, regime, mode, active_overrides):
        matched = []

        for item in active_overrides:
            scope = getattr(item, "scope", {}) or {}

            scoped_symbols = scope.get("symbols", [])
            if scoped_symbols and symbol not in scoped_symbols:
                continue

            scoped_modes = scope.get("system_modes", [])
            if scoped_modes and mode not in scoped_modes:
                continue

            scoped_regimes = scope.get("regimes", [])
            if scoped_regimes and regime and regime not in scoped_regimes:
                continue

            matched.append(item)

        return matched


