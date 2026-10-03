import pytest
from support.gateway_fakes import FakeClock

from rlworld.entitlements import (
    AuditLog,
    EntitlementBroker,
    EntitlementRequest,
    EphemeralSecretIssuer,
    IdentityPoolConfig,
    PoolExhaustedError,
    SeatPoolConfig,
    SignedSessionMinter,
    UnknownPoolError,
    guest_injection_payload,
    seat_hours,
)


class FailingSecretIssuer:
    def issue(self, name, scope, ttl_seconds):
        raise RuntimeError("vault unavailable")

    def renew(self, issued_secret, ttl_seconds):
        raise AssertionError("nothing was issued")

    def revoke(self, issued_secret):
        raise AssertionError("nothing was issued")


def build_broker(clock, office_seats=2, windows_hosts=2, accounts=("finance_manager_01", "finance_manager_02"),
                 secret_issuer=None):
    minter = SignedSessionMinter(b"signing key", "crm.example", clock.time)
    issuer = secret_issuer or EphemeralSecretIssuer(clock.time)
    broker = EntitlementBroker(
        seat_pools=[
            SeatPoolConfig("office_ltsc", office_seats, issues_token=True),
            SeatPoolConfig("windows_server_dc", windows_hosts, scope="per_host"),
        ],
        identity_pools=[IdentityPoolConfig("finance_manager", accounts)],
        session_minter=minter,
        secret_issuer=issuer,
        audit_log=AuditLog(),
        lease_ttl_seconds=600,
        clock=clock.time,
    )
    return broker, minter, issuer


def request(episode_id, host_id="host_a", licenses=("windows_server_dc", "office_ltsc"),
            personas=("finance_manager",), secrets=("crm_api_key",)):
    return EntitlementRequest(episode_id=episode_id, run_id="run_1", tenant="tenant_a", host_id=host_id,
                              licenses=licenses, personas=personas, secrets=secrets)


def test_a_lease_holds_seats_an_identity_a_session_and_secrets():
    clock = FakeClock()
    broker, minter, issuer = build_broker(clock)
    lease = broker.acquire(request("episode_1"))
    assert [grant.product for grant in lease.licenses] == ["windows_server_dc", "office_ltsc"]
    assert lease.licenses[1].token.startswith("office_ltsc_seat_00000.")
    assert lease.sessions[0].account == "finance_manager_01"
    assert minter.verify(lease.sessions[0].token)
    assert issuer.is_valid(lease.secrets[0].value, "episode_1")
    assert broker.availability() == {"office_ltsc": 1, "windows_server_dc": 1, "identity:finance_manager": 1}
    payload = guest_injection_payload(lease)
    assert payload["browser_cookies"] == {"session_crm.example": lease.sessions[0].token}
    assert payload["license_tokens"] == {"office_ltsc": lease.licenses[1].token}
    assert payload["environment"] == {"crm_api_key": lease.secrets[0].value}


def test_every_episode_gets_its_own_session_even_on_the_same_account():
    clock = FakeClock()
    broker, minter, _ = build_broker(clock, accounts=("finance_manager_01",))
    first_lease = broker.acquire(request("episode_1"))
    broker.release(first_lease.lease_id)
    second_lease = broker.acquire(request("episode_2"))
    assert first_lease.sessions[0].account == second_lease.sessions[0].account
    assert first_lease.sessions[0].token != second_lease.sessions[0].token
    assert not minter.verify(first_lease.sessions[0].token)
    assert minter.verify(second_lease.sessions[0].token)


def test_acquisition_is_all_or_nothing():
    clock = FakeClock()
    broker, _, _ = build_broker(clock, office_seats=1)
    broker.acquire(request("episode_1"))
    with pytest.raises(PoolExhaustedError) as raised:
        broker.acquire(request("episode_2"))
    assert raised.value.pool_name == "office_ltsc"
    assert broker.availability()["identity:finance_manager"] == 1
    assert broker.audit_log.snapshot()[-1].action == "deny"
    with pytest.raises(UnknownPoolError):
        broker.acquire(request("episode_3", licenses=("photoshop",)))


def test_host_scoped_licenses_are_held_once_per_host():
    clock = FakeClock()
    broker, _, _ = build_broker(clock, office_seats=10, windows_hosts=2, accounts=tuple(f"fm_{i}" for i in range(5)))
    first_lease = broker.acquire(request("episode_1", host_id="host_a"))
    second_lease = broker.acquire(request("episode_2", host_id="host_a"))
    assert first_lease.licenses[0].seat_id == second_lease.licenses[0].seat_id
    assert broker.availability()["windows_server_dc"] == 1
    broker.acquire(request("episode_3", host_id="host_b"))
    with pytest.raises(PoolExhaustedError):
        broker.acquire(request("episode_4", host_id="host_c"))
    broker.release(first_lease.lease_id)
    assert broker.availability()["windows_server_dc"] == 0
    broker.release(second_lease.lease_id)
    assert broker.availability()["windows_server_dc"] == 1


def test_a_failed_secret_issue_returns_everything():
    clock = FakeClock()
    broker, minter, _ = build_broker(clock, secret_issuer=FailingSecretIssuer())
    with pytest.raises(RuntimeError):
        broker.acquire(request("episode_1"))
    assert broker.availability() == {"office_ltsc": 2, "windows_server_dc": 2, "identity:finance_manager": 2}
    assert minter.revoked_tokens


def test_leases_expire_without_heartbeats():
    clock = FakeClock()
    broker, minter, issuer = build_broker(clock)
    kept_lease = broker.acquire(request("episode_1"))
    dropped_lease = broker.acquire(request("episode_2", host_id="host_b"))
    clock.now += 500
    broker.renew(kept_lease.lease_id)
    clock.now += 200
    assert broker.reap_expired() == [dropped_lease.lease_id]
    assert not issuer.is_valid(dropped_lease.secrets[0].value, "episode_2")
    assert issuer.is_valid(kept_lease.secrets[0].value, "episode_1")
    assert minter.verify(kept_lease.sessions[0].token)
    assert not minter.verify(dropped_lease.sessions[0].token)
    assert any(event.action == "expire" for event in broker.audit_log.snapshot())
    assert not broker.release(dropped_lease.lease_id)


def test_seat_hours_come_from_the_audit_log():
    clock = FakeClock()
    broker, _, _ = build_broker(clock)
    period_start = clock.now
    lease = broker.acquire(request("episode_1"))
    clock.now += 2 * 3600
    broker.release(lease.lease_id)
    open_lease = broker.acquire(request("episode_2"))
    clock.now += 3600
    hours = seat_hours(broker.audit_log.snapshot(), period_start, clock.now)
    assert hours == {"windows_server_dc": pytest.approx(3.0), "office_ltsc": pytest.approx(3.0)}
    assert all(open_lease.secrets[0].value not in event.detail for event in broker.audit_log.snapshot())
