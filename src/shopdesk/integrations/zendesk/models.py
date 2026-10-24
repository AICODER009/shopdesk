from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Self

from pydantic import BaseModel, Field, SecretStr


class ZendeskToken(BaseModel):
    access_token: SecretStr
    refresh_token: SecretStr | None = None
    scope: str = ""
    expires_at: datetime | None = None
    refresh_expires_at: datetime | None = None

    @classmethod
    def from_token_response(cls, data: dict[str, Any], now: datetime | None = None) -> Self:
        now = now or datetime.now(UTC)
        expires_in = data.get("expires_in")
        refresh_in = data.get("refresh_token_expires_in")
        return cls(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            scope=data.get("scope", ""),
            expires_at=now + timedelta(seconds=expires_in) if expires_in else None,
            refresh_expires_at=now + timedelta(seconds=refresh_in) if refresh_in else None,
        )

    def expiring(self, skew: timedelta = timedelta(minutes=2)) -> bool:
        return self.expires_at is not None and datetime.now(UTC) + skew >= self.expires_at

    def to_plain(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token.get_secret_value(),
            "refresh_token": self.refresh_token.get_secret_value() if self.refresh_token else None,
            "scope": self.scope,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "refresh_expires_at": (
                self.refresh_expires_at.isoformat() if self.refresh_expires_at else None
            ),
        }


class HandoffTicket(BaseModel):
    conversation_id: str
    subject: str = Field(max_length=150)
    internal_note: str
    requester_name: str | None = None
    requester_email: str | None = None
    priority: Literal["low", "normal", "high", "urgent"] = "normal"
    tags: list[str] = Field(default_factory=list)
    idempotency_key: str


class TicketRef(BaseModel):
    id: int
    agent_url: str
    requester_id: int | None = None
