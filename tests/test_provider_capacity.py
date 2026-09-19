"""Preserve pending transactions and share provider cooldown across wallet clients."""

import pytest
from test_coordinator import Provider, prepared

from defi_kernel.coordinator import Coordinator
from defi_kernel.providers import EvidenceUnavailable, Koios, ProviderError


def test_empty_indexed_bytes_wait_without_releasing_or_repeating_submission(tmp_path):
    journal, tx, context = prepared(tmp_path)
    journal.claim_submission("test")
    before = dict(journal.outbox_entry("test"))
    indexed = []
    koios = Koios.__new__(Koios)
    info = {"tx_hash": str(tx.id), "block_height": 101, "block_hash": "a" * 64}
    koios.request = lambda endpoint, **kw: (
        [info.copy()] if endpoint == "tx_info" else indexed
    )
    provider = Provider(context)
    provider.transaction_info = koios.transaction_info
    provider.canonical = {"hash": "a" * 64}
    c = Coordinator(provider, journal)
    for _ in range(2):
        with pytest.raises(EvidenceUnavailable):
            c.reconcile("test")
        assert dict(journal.outbox_entry("test")) == before
        assert len(journal.status()["reservations"]) == 4
    indexed.append({"tx_hash": str(tx.id), "cbor": tx.to_cbor_hex()})
    assert c.reconcile("test") == "confirmed"
    assert journal.outbox_entry("test")["attempts"] == 1 and provider.attempts == 0
    indexed[0]["tx_hash"] = "b" * 64
    with pytest.raises(ProviderError) as error:
        koios.transaction_cbor(str(tx.id))
    assert not isinstance(error.value, EvidenceUnavailable)
    journal.close()


def test_wallet_clients_share_cooldown_and_exponential_backoff():
    import httpx
    from test_provider import PROFILE

    from defi_kernel.providers import RateLimited

    now, calls = [0.0], []

    def clock():
        return now[0]

    def reply(request):
        calls.append(request.url.path)
        return httpx.Response(429, json={})

    def make(**kwargs):
        return Koios(
            PROFILE,
            client=httpx.Client(transport=httpx.MockTransport(reply)),
            clock=clock,
            monotonic=clock,
            sleep=lambda _: None,
            **kwargs,
        )

    source = make()
    maker = make(share_requests_with=source)
    with pytest.raises(RateLimited) as first:
        source.request("tip")
    assert first.value.retry_after_seconds == 60
    with pytest.raises(RateLimited):
        maker.request("credential_utxos")
    assert len(calls) == 1
    now[0] = 60
    with pytest.raises(RateLimited) as second:
        maker.request("credential_utxos")
    assert second.value.retry_after_seconds == 120 and len(calls) == 2
    with pytest.raises(RateLimited):
        source.request("genesis")
    assert len(calls) == 2
    source.close()
    maker.close()
