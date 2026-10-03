"""Identity and entitlements broker (DESIGN.md section 6.5).

One broker leases everything an episode must hold: license seats, synthetic
identities with freshly minted sessions and short lived scoped secrets.
Acquisition is all or nothing, leases carry a TTL renewed by heartbeats and a
reaper releases leases whose env died. Host scoped licenses such as Windows
Server Datacenter are held once per host while any env on it is leased.
"""

import itertools
import secrets
import threading
import time
from collections import Counter, deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from rlworld.entitlements.audit import AuditEvent, AuditLog
from rlworld.entitlements.credentials import IssuedSecret, SecretIssuer, Session, SessionMinter


class PoolExhaustedError(Exception):
    def __init__(self, pool_name: str) -> None:
        super().__init__(f"the {pool_name} pool has no free capacity")
        self.pool_name = pool_name


class UnknownPoolError(KeyError):
    """A request names a license or persona pool that is not configured."""


@dataclass(frozen=True)
class SeatPoolConfig:
    name: str
    capacity: int
    scope: Literal["per_env", "per_host"] = "per_env"
    issues_token: bool = False


@dataclass(frozen=True)
class IdentityPoolConfig:
    persona_key: str
    accounts: tuple[str, ...]


@dataclass(frozen=True)
class EntitlementRequest:
    episode_id: str
    run_id: str
    tenant: str
    host_id: str
    licenses: tuple[str, ...] = ()
    personas: tuple[str, ...] = ()
    secrets: tuple[str, ...] = ()


@dataclass(frozen=True)
class LicenseGrant:
    product: str
    seat_id: str
    token: str | None = None


@dataclass(frozen=True)
class EntitlementLease:
    lease_id: str
    request: EntitlementRequest
    licenses: tuple[LicenseGrant, ...]
    sessions: tuple[Session, ...]
    secrets: tuple[IssuedSecret, ...]
    acquired_at: float
    expires_at: float


@dataclass
class SeatPoolState:
    config: SeatPoolConfig
    free_seat_ids: deque[str]
    leases_per_host: dict[str, int] = field(default_factory=dict)
    seat_id_per_host: dict[str, str] = field(default_factory=dict)


def guest_injection_payload(lease: EntitlementLease) -> dict[str, Any]:
    """What the guest agent writes into the VM after restore: cookies, license tokens and secrets."""
    return {
        "browser_cookies": {name: value for session in lease.sessions for name, value in session.cookies.items()},
        "license_tokens": {grant.product: grant.token for grant in lease.licenses if grant.token},
        "environment": {issued_secret.name: issued_secret.value for issued_secret in lease.secrets},
    }


class EntitlementBroker:
    def __init__(
        self,
        seat_pools: Sequence[SeatPoolConfig],
        identity_pools: Sequence[IdentityPoolConfig],
        session_minter: SessionMinter,
        secret_issuer: SecretIssuer,
        audit_log: AuditLog | None = None,
        lease_ttl_seconds: float = 300.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.seat_pools = {
            pool.name: SeatPoolState(pool, deque(f"{pool.name}_seat_{index:05d}" for index in range(pool.capacity)))
            for pool in seat_pools
        }
        self.free_accounts = {pool.persona_key: deque(pool.accounts) for pool in identity_pools}
        self.session_minter = session_minter
        self.secret_issuer = secret_issuer
        self.audit_log = audit_log or AuditLog()
        self.lease_ttl_seconds = lease_ttl_seconds
        self.clock = clock
        self.leases: dict[str, EntitlementLease] = {}
        self.lease_counter = itertools.count(1)
        self.lock = threading.Lock()

    def availability(self) -> dict[str, int]:
        """Free capacity per pool, for the scheduler and preflight."""
        with self.lock:
            seat_availability = {name: len(state.free_seat_ids) for name, state in self.seat_pools.items()}
            identity_availability = {f"identity:{key}": len(accounts) for key, accounts in self.free_accounts.items()}
        return {**seat_availability, **identity_availability}

    def audit(self, action: str, resource_kind: str, resource_name: str, holder: str, request: EntitlementRequest,
              lease_id: str, detail: str = "") -> None:
        self.audit_log.record(AuditEvent(at=self.clock(), action=action, resource_kind=resource_kind,
                                         resource_name=resource_name, holder=holder, lease_id=lease_id,
                                         run_id=request.run_id, tenant=request.tenant, detail=detail))

    def shortage(self, request: EntitlementRequest) -> str | None:
        """The first pool that cannot satisfy the request, checked before anything is taken."""
        for license_name in request.licenses:
            state = self.seat_pools.get(license_name)
            if state is None:
                raise UnknownPoolError(license_name)
            host_already_holds = state.config.scope == "per_host" and request.host_id in state.leases_per_host
            if not host_already_holds and not state.free_seat_ids:
                return license_name
        for persona_key, wanted_count in Counter(request.personas).items():
            if persona_key not in self.free_accounts:
                raise UnknownPoolError(persona_key)
            if len(self.free_accounts[persona_key]) < wanted_count:
                return f"identity:{persona_key}"
        return None

    def acquire(self, request: EntitlementRequest) -> EntitlementLease:
        lease_id = f"entitlement_{next(self.lease_counter):08d}"
        with self.lock:
            missing_pool = self.shortage(request)
            if missing_pool is not None:
                denied_kind = "identity" if missing_pool.startswith("identity:") else "license"
                self.audit("deny", denied_kind, missing_pool, request.episode_id, request, lease_id,
                           "pool exhausted")
                raise PoolExhaustedError(missing_pool)
            license_grants = [self.take_seat(license_name, request, lease_id) for license_name in request.licenses]
            accounts = [(persona_key, self.free_accounts[persona_key].popleft()) for persona_key in request.personas]
        sessions = []
        issued_secrets = []
        try:
            for persona_key, account in accounts:
                sessions.append(self.session_minter.mint(persona_key, account, request.episode_id,
                                                         self.lease_ttl_seconds))
                self.audit("grant", "identity", persona_key, account, request, lease_id)
            for secret_name in request.secrets:
                issued_secrets.append(self.secret_issuer.issue(secret_name, request.episode_id, self.lease_ttl_seconds))
                self.audit("grant", "secret", secret_name, request.episode_id, request, lease_id)
        except Exception:
            self.return_resources(request, tuple(license_grants), accounts, sessions, issued_secrets, lease_id,
                                  "release")
            raise
        acquired_at = self.clock()
        lease = EntitlementLease(lease_id, request, tuple(license_grants), tuple(sessions), tuple(issued_secrets),
                                 acquired_at, acquired_at + self.lease_ttl_seconds)
        with self.lock:
            self.leases[lease_id] = lease
        return lease

    def take_seat(self, license_name: str, request: EntitlementRequest, lease_id: str) -> LicenseGrant:
        state = self.seat_pools[license_name]
        if state.config.scope == "per_host":
            if request.host_id not in state.leases_per_host:
                seat_id = state.free_seat_ids.popleft()
                state.seat_id_per_host[request.host_id] = seat_id
                state.leases_per_host[request.host_id] = 0
                self.audit("grant", "host_license", license_name, request.host_id, request, lease_id, seat_id)
            state.leases_per_host[request.host_id] += 1
            return LicenseGrant(license_name, state.seat_id_per_host[request.host_id])
        seat_id = state.free_seat_ids.popleft()
        self.audit("grant", "license", license_name, request.episode_id, request, lease_id, seat_id)
        token = f"{seat_id}.{secrets.token_hex(8)}" if state.config.issues_token else None
        return LicenseGrant(license_name, seat_id, token)

    def return_resources(self, request: EntitlementRequest, license_grants: tuple[LicenseGrant, ...],
                         accounts: Sequence[tuple[str, str]], sessions: Sequence[Session],
                         issued_secrets: Sequence[IssuedSecret], lease_id: str, action: str) -> None:
        for session in sessions:
            self.session_minter.revoke(session)
        for issued_secret in issued_secrets:
            self.secret_issuer.revoke(issued_secret)
            self.audit(action, "secret", issued_secret.name, request.episode_id, request, lease_id)
        with self.lock:
            for grant in license_grants:
                state = self.seat_pools[grant.product]
                if state.config.scope == "per_host":
                    state.leases_per_host[request.host_id] -= 1
                    if state.leases_per_host[request.host_id] == 0:
                        del state.leases_per_host[request.host_id]
                        state.free_seat_ids.append(state.seat_id_per_host.pop(request.host_id))
                        self.audit(action, "host_license", grant.product, request.host_id, request, lease_id,
                                   grant.seat_id)
                else:
                    state.free_seat_ids.append(grant.seat_id)
                    self.audit(action, "license", grant.product, request.episode_id, request, lease_id,
                               grant.seat_id)
            for persona_key, account in accounts:
                self.free_accounts[persona_key].append(account)
                self.audit(action, "identity", persona_key, account, request, lease_id)

    def renew(self, lease_id: str) -> EntitlementLease:
        """Heartbeat: push the lease, its sessions and its secrets out by one TTL together."""
        with self.lock:
            lease = self.leases[lease_id]
        renewed_sessions = tuple(self.session_minter.renew(session, self.lease_ttl_seconds) for session in lease.sessions)
        renewed_secrets = tuple(self.secret_issuer.renew(issued_secret, self.lease_ttl_seconds)
                                for issued_secret in lease.secrets)
        renewed_lease = EntitlementLease(lease.lease_id, lease.request, lease.licenses, renewed_sessions,
                                         renewed_secrets, lease.acquired_at, self.clock() + self.lease_ttl_seconds)
        with self.lock:
            if lease_id in self.leases:
                self.leases[lease_id] = renewed_lease
        return renewed_lease

    def release(self, lease_id: str, action: str = "release") -> bool:
        with self.lock:
            lease = self.leases.pop(lease_id, None)
        if lease is None:
            return False
        accounts = [(session.persona_key, session.account) for session in lease.sessions]
        self.return_resources(lease.request, lease.licenses, accounts, lease.sessions, lease.secrets, lease_id,
                              action)
        return True

    def reap_expired(self) -> list[str]:
        """Release leases whose env stopped heartbeating, so seats and identities never leak."""
        now = self.clock()
        with self.lock:
            expired_lease_ids = [lease_id for lease_id, lease in self.leases.items() if lease.expires_at <= now]
        return [lease_id for lease_id in expired_lease_ids if self.release(lease_id, action="expire")]

