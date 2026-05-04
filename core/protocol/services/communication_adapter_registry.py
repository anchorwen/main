class CommunicationAdapterRegistry:
    def __init__(self, adapters: dict[str, object], default_adapter_name: str | None = None):
        self._adapters = dict(adapters)
        self._default_adapter_name = default_adapter_name

    def resolve(
        self,
        *,
        target: str,
        message_type,
        route_policy: dict | None = None,
        transport_hints: dict | None = None,
        governance: dict | None = None,
    ) -> object:
        route_policy = route_policy or {}
        transport_hints = transport_hints or {}
        governance = governance or {}

        explicit_adapter_name = route_policy.get("adapter")
        if explicit_adapter_name:
            return self._require_adapter(explicit_adapter_name)

        channel_name = route_policy.get("channel")
        if channel_name:
            channel_key = f"channel:{channel_name}"
            if channel_key in self._adapters:
                return self._adapters[channel_key]

        system_mode = governance.get("system_mode")
        if system_mode == "degraded":
            degraded_key = "mode:degraded"
            if degraded_key in self._adapters:
                return self._adapters[degraded_key]

        transport_mode = transport_hints.get("mode")
        if transport_mode:
            transport_key = f"transport:{transport_mode}"
            if transport_key in self._adapters:
                return self._adapters[transport_key]

        if target in self._adapters:
            return self._adapters[target]

        message_type_value = (
            message_type.value if hasattr(message_type, "value") else str(message_type)
        )
        if message_type_value in self._adapters:
            return self._adapters[message_type_value]

        if self._default_adapter_name and self._default_adapter_name in self._adapters:
            return self._adapters[self._default_adapter_name]

        raise KeyError(
            f"no communication adapter registered for target={target}"
            f" or message_type={message_type_value}"
        )

    def _require_adapter(self, adapter_name: str) -> object:
        if adapter_name not in self._adapters:
            raise KeyError(f"no communication adapter registered with name={adapter_name}")
        return self._adapters[adapter_name]
