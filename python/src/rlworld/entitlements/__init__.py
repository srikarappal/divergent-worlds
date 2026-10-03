"""Identity and entitlements broker: seats, identities, sessions, secrets and audit (DESIGN.md 6.5)."""

from rlworld.entitlements.audit import AuditEvent, AuditLog, seat_hours
from rlworld.entitlements.broker import (
    EntitlementBroker,
    EntitlementLease,
    EntitlementRequest,
    IdentityPoolConfig,
    LicenseGrant,
    PoolExhaustedError,
    SeatPoolConfig,
    UnknownPoolError,
    guest_injection_payload,
)
from rlworld.entitlements.credentials import (
    EphemeralSecretIssuer,
    IssuedSecret,
    SecretIssuer,
    Session,
    SessionMinter,
    SignedSessionMinter,
)

__all__ = [
    "AuditEvent",
    "AuditLog",
    "EntitlementBroker",
    "EntitlementLease",
    "EntitlementRequest",
    "EphemeralSecretIssuer",
    "IdentityPoolConfig",
    "IssuedSecret",
    "LicenseGrant",
    "PoolExhaustedError",
    "SeatPoolConfig",
    "SecretIssuer",
    "Session",
    "SessionMinter",
    "SignedSessionMinter",
    "UnknownPoolError",
    "guest_injection_payload",
    "seat_hours",
]
