from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Any


VALID_STATUSES = ("pending", "running", "success", "failed")


@dataclass
class Task:
    id: int
    type: str
    payload: dict[str, Any]
    status: str
    priority: int
    attempts: int
    max_attempts: int
    worker_id: str | None
    lease_expires_at: datetime | None
    result: Any
    created_at: datetime
    updated_at: datetime

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for k in ("lease_expires_at", "created_at", "updated_at"):
            v = d[k]
            d[k] = v.isoformat() if isinstance(v, datetime) else v
        return d
