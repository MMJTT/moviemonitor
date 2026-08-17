import threading
import uuid
from datetime import timedelta

import pytest
from django.db import close_old_connections, connection
from django.utils import timezone

from core.models import MonitorTask
from core.services.leases import claim_due_task


@pytest.mark.django_db
def test_claim_due_task_claims_earliest_without_advancing_schedule(task_factory):
    """Claiming a later task or advancing its persisted schedule must fail this test."""
    now = timezone.now()
    earliest = task_factory(next_check_at=now - timedelta(minutes=2))
    task_factory(
        next_check_at=now - timedelta(minutes=1),
        movie_id="2222222",
        movie_name="第二部电影",
        query_key="maoyan:10:second-due",
        cinema_name="第二家影院",
        normalized_cinema_name="第二家影院",
    )

    claim = claim_due_task(now=now, lease_seconds=300)

    assert claim is not None
    assert claim.task_id == earliest.pk
    earliest.refresh_from_db()
    assert earliest.claim_token == claim.token
    assert earliest.claim_expires_at == now + timedelta(seconds=300)
    assert earliest.next_check_at == now - timedelta(minutes=2)


@pytest.mark.django_db
def test_active_lease_cannot_be_claimed_twice(task_factory):
    """Ignoring an unexpired lease would dispatch duplicate work."""
    now = timezone.now()
    task = task_factory(next_check_at=now - timedelta(minutes=1))

    first = claim_due_task(now=now, lease_seconds=300)
    second = claim_due_task(now=now, lease_seconds=300)

    assert first is not None
    assert first.task_id == task.pk
    assert second is None


@pytest.mark.django_db
def test_expired_lease_is_reclaimed_with_new_token(task_factory):
    """Treating an expired lease as active would strand due work."""
    now = timezone.now()
    stale_token = uuid.uuid4()
    task = task_factory(
        next_check_at=now - timedelta(minutes=1),
        claim_token=stale_token,
        claim_expires_at=now - timedelta(seconds=1),
    )

    claim = claim_due_task(now=now, lease_seconds=120)

    assert claim is not None
    assert claim.task_id == task.pk
    assert claim.token != stale_token
    task.refresh_from_db()
    assert task.claim_token == claim.token
    assert task.claim_expires_at == now + timedelta(seconds=120)


@pytest.mark.django_db
def test_paused_due_task_is_not_claimed(task_factory):
    """Claiming a paused task would bypass the user's lifecycle transition."""
    now = timezone.now()
    task_factory(
        status=MonitorTask.Status.PAUSED,
        next_check_at=now - timedelta(minutes=1),
    )

    assert claim_due_task(now=now, lease_seconds=300) is None


@pytest.mark.postgres
@pytest.mark.skipif(connection.vendor != "postgresql", reason="requires PostgreSQL")
@pytest.mark.django_db(transaction=True)
def test_postgres_concurrent_claimers_cannot_claim_same_task(task_factory):
    """Dropping skip-locked row serialization would dispatch one task twice."""
    now = timezone.now()
    task = task_factory(next_check_at=now - timedelta(minutes=1))
    barrier = threading.Barrier(2)
    claims = []
    errors = []

    def claim():
        close_old_connections()
        try:
            barrier.wait(timeout=2)
            claims.append(claim_due_task(now=now, lease_seconds=300))
        except BaseException as exc:
            errors.append(exc)
        finally:
            close_old_connections()

    threads = [threading.Thread(target=claim) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert all(thread.is_alive() is False for thread in threads)
    assert errors == []
    claimed_ids = [claim.task_id for claim in claims if claim is not None]
    assert claimed_ids == [task.pk]
