from dataclasses import dataclass
from datetime import datetime


@dataclass
class MutableClock:
    value: datetime

    def __call__(self) -> datetime:
        return self.value
