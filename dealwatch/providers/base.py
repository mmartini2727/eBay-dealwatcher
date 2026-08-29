from abc import ABC, abstractmethod
from typing import Any


class MarketplaceProvider(ABC):
    """Interface all marketplace providers will implement."""

    @abstractmethod
    async def search(self, profile: Any) -> list[Any]:
        raise NotImplementedError
