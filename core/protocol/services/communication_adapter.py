from typing import Protocol


class CommunicationAdapter(Protocol):
    adapter_name: str

    def dispatch(self, request, envelope): ...
