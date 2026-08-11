"""Pure-domain tests: no database, no fixtures, no event loop.

This is the payoff for keeping app/domain free of I/O — the rules that are easiest to
get subtly wrong are also the cheapest to test.
"""

from __future__ import annotations

import random

import pytest

from app.domain.job import (
    InvalidTransition, JobStatus, RetryPolicy, assert_transition,
    can_transition, is_terminal,
)


class TestStateMachine:
    def test_happy_path(self):
        assert can_transition(JobStatus.QUEUED, JobStatus.RUNNING)
        assert can_transition(JobStatus.RUNNING, JobStatus.SUCCEEDED)

    def test_terminal_states_are_absorbing(self):
        assert is_terminal(JobStatus.SUCCEEDED)
        assert is_terminal(JobStatus.CANCELLED)
        assert not can_transition(JobStatus.SUCCEEDED, JobStatus.RUNNING)
        assert not can_transition(JobStatus.CANCELLED, JobStatus.QUEUED)

    def test_cannot_skip_running(self):
        assert not can_transition(JobStatus.QUEUED, JobStatus.SUCCEEDED)

    def test_reaper_can_requeue_a_running_job(self):
        # The lease-expiry path: worker died, job goes back to the queue.
        assert can_transition(JobStatus.RUNNING, JobStatus.QUEUED)

    def test_dead_letter_allows_operator_retry(self):
        assert can_transition(JobStatus.DEAD_LETTER, JobStatus.QUEUED)

    def test_assert_raises_with_both_ends_named(self):
        with pytest.raises(InvalidTransition) as e:
            assert_transition(JobStatus.SUCCEEDED, JobStatus.QUEUED)
        assert e.value.src == JobStatus.SUCCEEDED
        assert e.value.dst == JobStatus.QUEUED


class TestRetryPolicy:
    def test_retries_until_max_attempts(self):
        p = RetryPolicy(max_attempts=3)
        assert p.should_retry(1) and p.should_retry(2)
        assert not p.should_retry(3)

    def test_outcome_switches_to_dead_letter_when_exhausted(self):
        p = RetryPolicy(max_attempts=3)
        assert p.outcome_after_failure(1) == JobStatus.FAILED
        assert p.outcome_after_failure(3) == JobStatus.DEAD_LETTER

    def test_backoff_is_capped(self):
        p = RetryPolicy(base_seconds=2.0, max_seconds=10.0)
        rng = random.Random(0)
        for attempt in range(1, 12):
            assert 0.0 <= p.next_delay_seconds(attempt, rng=rng) <= 10.0

    def test_backoff_grows_with_attempts(self):
        """Full jitter is random per call, so compare ceilings over many samples."""
        p = RetryPolicy(base_seconds=1.0, max_seconds=1000.0)
        rng = random.Random(42)
        early = max(p.next_delay_seconds(1, rng=rng) for _ in range(200))
        late = max(p.next_delay_seconds(5, rng=rng) for _ in range(200))
        assert late > early

    def test_jitter_spreads_retries(self):
        """Without jitter every job failed by one outage retries in lockstep."""
        p = RetryPolicy(base_seconds=8.0)
        rng = random.Random(1)
        samples = {round(p.next_delay_seconds(3, rng=rng), 4) for _ in range(50)}
        assert len(samples) > 40, "delays should be well spread, not clustered"

    def test_attempt_zero_is_treated_as_first(self):
        p = RetryPolicy(base_seconds=2.0)
        assert p.next_delay_seconds(0) <= 2.0
