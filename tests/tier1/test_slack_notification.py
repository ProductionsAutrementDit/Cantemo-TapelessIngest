"""Tier 1: robust end-of-scan Slack notification (stories 1.5 + 3.2).

Covers the CustomLogger hardening in both command modules: chunking at
SLACK_MAX_MESSAGE_LENGTH, the never-raises error boundary around the send
loop, the absent-config / construction-failure skip paths, and the
byte-identity of the shared block across the two files. Story 3.2 adds
the per-chunk 429 retry (Retry-After honoured, bounded, WARNING per
retry), the retryable-vs-fatal distinction, and paced sends.

AD-11: only the `slack_client` attribute on a CustomLogger instance and
attributes of our own command modules are stubbed — never Portal, never
slack_sdk internals. A 429 double raises the REAL `SlackApiError` with a
duck-typed response (`SimpleNamespace(status_code=429, headers={...})`).
"""

import inspect
import logging
from types import SimpleNamespace

import pytest
from slack_sdk.errors import SlackApiError

from portal.plugins.TapelessIngest.management.commands import (
    check_clips_in_folder as check_mod,
)
from portal.plugins.TapelessIngest.management.commands import (
    scan_tapeless_dir as scan_mod,
)

MODULES = [scan_mod, check_mod]
MODULE_IDS = ["scan_tapeless_dir", "check_clips_in_folder"]

CHANNEL = "pad-notifications-cantemo"
LOGGER_NAME = "portal.plugins.TapelessIngest"


class RecordingClient:
    """Stub for the slack_client instance: records every chat_postMessage."""

    def __init__(self):
        self.calls = []

    def chat_postMessage(self, **kwargs):
        self.calls.append(kwargs)


class RaisingClient:
    """Stub for the slack_client instance: raises on chat_postMessage."""

    def __init__(self, exc):
        self.exc = exc
        self.calls = 0

    def chat_postMessage(self, **kwargs):
        self.calls += 1
        raise self.exc


class ScriptedClient:
    """Stub for the slack_client instance: raises per a call-by-call script.

    ``script[i]`` is either None (call i succeeds) or the exception call i
    raises; calls past the script's end succeed. ``sent`` records only the
    texts that actually went through, in order.
    """

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0
        self.sent = []

    def chat_postMessage(self, **kwargs):
        self.calls += 1
        action = self.script.pop(0) if self.script else None
        if action is not None:
            raise action
        self.sent.append(kwargs["text"])


def rate_limited(retry_after=None, headers=None):
    """A real SlackApiError shaped like Slack's 429 (duck-typed response)."""
    if headers is None:
        headers = {} if retry_after is None else {"Retry-After": retry_after}
    return SlackApiError(
        "ratelimited", SimpleNamespace(status_code=429, headers=headers)
    )


@pytest.fixture(params=MODULES, ids=MODULE_IDS)
def module(request):
    return request.param


@pytest.fixture
def sleeps(module, monkeypatch):
    """Record every send-loop wait (pacing and backoff) instead of sleeping.

    The module seam the spec prescribes: tests run at pace zero and assert
    the exact waits the loop asked for.
    """
    recorded = []
    monkeypatch.setattr(module, "_slack_sleep", recorded.append)
    return recorded


@pytest.fixture
def custom_logger(module, monkeypatch, sleeps):
    """A CustomLogger built with no token (client None); tests attach stubs.

    Depends on ``sleeps`` so no test with a multi-chunk report ever
    really sleeps through the pacing seam.
    """
    monkeypatch.setattr(module, "SLACK_ACCESS_TOKEN", None)
    return module.CustomLogger()


def error_records(caplog):
    return [r for r in caplog.records if r.levelno >= logging.ERROR]


# --- chunking ---------------------------------------------------------------


def test_short_report_sent_as_single_message(module, custom_logger, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    client = RecordingClient()
    custom_logger.slack_client = client
    for message in ["first line", "second line", "third line"]:
        custom_logger.log(message)

    custom_logger.send_messages_to_slack()

    assert client.calls == [
        {"channel": CHANNEL, "text": "first line\nsecond line\nthird line"}
    ]
    # Positive confirmation for cron observability: one success INFO line.
    successes = [
        r
        for r in caplog.records
        if r.getMessage() == "Slack notification sent (1 chunks)"
    ]
    assert len(successes) == 1


def test_long_report_chunked_in_order_and_complete(module, custom_logger):
    limit = module.SLACK_MAX_MESSAGE_LENGTH
    client = RecordingClient()
    custom_logger.slack_client = client
    messages = [f"folder {i:04d}: " + "x" * 90 for i in range(200)]
    custom_logger.messages = list(messages)
    assert len("\n".join(messages)) > limit

    custom_logger.send_messages_to_slack()

    texts = [call["text"] for call in client.calls]
    assert len(texts) > 1
    assert all(len(text) <= limit for text in texts)
    assert all(call["channel"] == CHANNEL for call in client.calls)
    # Chunk boundaries fall between whole messages, so rejoining the chunks
    # with newlines reconstructs the full report, in order, nothing lost.
    assert "\n".join(texts) == "\n".join(messages)


def test_oversized_single_line_is_hard_sliced(module, custom_logger):
    limit = module.SLACK_MAX_MESSAGE_LENGTH
    client = RecordingClient()
    custom_logger.slack_client = client
    big = "".join(f"{i:08d}" for i in range(1200))  # 9600 chars, one message
    custom_logger.messages = [big]

    custom_logger.send_messages_to_slack()

    texts = [call["text"] for call in client.calls]
    assert len(texts) == 3
    assert all(len(text) <= limit for text in texts)
    # Slices of one oversized message are sent as-is: concatenation (no
    # separator) restores the original message exactly, in order.
    assert "".join(texts) == big


def test_mixed_report_preserves_order_and_content(module, custom_logger):
    limit = module.SLACK_MAX_MESSAGE_LENGTH
    client = RecordingClient()
    custom_logger.slack_client = client
    before = "before the big one"
    big = "B" * (limit + 100)
    after = "after the big one"
    custom_logger.messages = [before, big, after]

    custom_logger.send_messages_to_slack()

    texts = [call["text"] for call in client.calls]
    assert all(len(text) <= limit for text in texts)
    stream = "\n".join(texts)
    assert stream.index(before) < stream.index("B" * 100)
    assert stream.rindex("B") < stream.index(after)
    assert stream.count("B") == limit + 100


# --- error boundary ---------------------------------------------------------


@pytest.mark.parametrize(
    "exc",
    [
        Exception("slack api error: invalid_auth"),
        ConnectionError("network down"),
        SlackApiError("invalid_auth", SimpleNamespace(status_code=403, headers={})),
    ],
    ids=["api-error", "network-error", "slack-error-non-429"],
)
def test_send_failure_is_swallowed_and_logged(
    module, custom_logger, caplog, sleeps, exc
):
    """Story 1.5's abort pin, rewritten by 3.2 for the FATAL case only.

    Retryable is `SlackApiError` with `response.status_code == 429` and
    nothing else (waiver in tests/fr4-waivers.md): every other failure —
    a non-429 SlackApiError included — still aborts on its very first
    throw, client called once, exactly one ERROR, never a retry wait.
    """
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    client = RaisingClient(exc)
    custom_logger.slack_client = client
    custom_logger.messages = ["a" * module.SLACK_MAX_MESSAGE_LENGTH] * 3

    # Must return normally: never raises, never alters process state.
    assert custom_logger.send_messages_to_slack() is None

    # Abort on first failure: remaining chunks abandoned after chunk 1.
    assert client.calls == 1
    assert sleeps == []
    errors = error_records(caplog)
    assert len(errors) == 1
    assert errors[0].exc_info is not None
    assert "chunk 1/3" in errors[0].getMessage()


# --- 429 retry and pacing (story 3.2) ---------------------------------------


def test_429_then_success_delivers_every_chunk_in_order(
    module, custom_logger, caplog, sleeps
):
    """A chunk 429'd twice then accepted: nothing lost, zero ERROR records."""
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    limit = module.SLACK_MAX_MESSAGE_LENGTH
    messages = ["a" * limit, "b" * limit, "c" * limit]
    client = ScriptedClient(
        [
            None,  # chunk 1 accepted
            rate_limited("7"),  # chunk 2: 429, Retry-After: 7
            rate_limited("7"),  # chunk 2, retry 1: 429 again
            None,  # chunk 2, retry 2: accepted
            None,  # chunk 3 accepted
        ]
    )
    custom_logger.slack_client = client
    custom_logger.messages = list(messages)

    assert custom_logger.send_messages_to_slack() is None

    # All three chunks arrived, in order — the 429'd chunk was re-sent,
    # not dropped, and each retry waited the server's Retry-After.
    assert client.sent == messages
    pace = module.SLACK_SEND_PACE_SECONDS
    assert sleeps == [pace, 7.0, 7.0, pace]
    assert not error_records(caplog)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2
    assert all("chunk 2/3" in r.getMessage() for r in warnings)
    successes = [
        r
        for r in caplog.records
        if r.getMessage() == "Slack notification sent (3 chunks)"
    ]
    assert len(successes) == 1


def test_429_exhaustion_abandons_the_rest_with_one_error(
    module, custom_logger, caplog, sleeps
):
    """A chunk that 429s SLACK_RATE_LIMIT_RETRIES+1 times is given up on."""
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    client = RaisingClient(rate_limited("0"))
    custom_logger.slack_client = client
    custom_logger.messages = ["a" * module.SLACK_MAX_MESSAGE_LENGTH] * 3

    assert custom_logger.send_messages_to_slack() is None

    # The SAME chunk was tried 1 + SLACK_RATE_LIMIT_RETRIES times; the
    # remaining chunks were never attempted.
    assert client.calls == 1 + module.SLACK_RATE_LIMIT_RETRIES
    # Every retry waited the Retry-After ("0") — and nothing else was
    # slept: no pacing wait ever fired for an undelivered chunk.
    assert sleeps == [0.0] * module.SLACK_RATE_LIMIT_RETRIES
    errors = error_records(caplog)
    assert len(errors) == 1
    assert errors[0].exc_info is not None
    assert "chunk 1/3" in errors[0].getMessage()
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == module.SLACK_RATE_LIMIT_RETRIES


@pytest.mark.parametrize(
    ("headers", "expected_wait"),
    [
        ({}, 1.0),
        ({"Retry-After": "soon"}, 1.0),
        ({"Retry-After": "-5"}, 1.0),
        ({"Retry-After": "NaN"}, 1.0),
        ({"retry-after": "7"}, 7.0),
        ({"Retry-After": "300"}, 30.0),
    ],
    ids=["missing", "garbage", "negative", "nan", "lowercase", "capped"],
)
def test_retry_after_fallback_case_and_cap(
    module, custom_logger, caplog, sleeps, headers, expected_wait
):
    """Retry-After is read case-insensitively; missing/garbage -> 1.0 s,
    capped at 30 s — and the chunk is still retried, at WARNING."""
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    client = ScriptedClient([rate_limited(headers=headers), None])
    custom_logger.slack_client = client
    custom_logger.messages = ["the report"]

    assert custom_logger.send_messages_to_slack() is None

    assert client.sent == ["the report"]
    assert sleeps == [expected_wait]
    assert not error_records(caplog)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1


def test_successful_sends_are_paced_between_chunks_only(module, custom_logger, sleeps):
    """n chunks all accepted: exactly n-1 paced waits, none after the last."""
    limit = module.SLACK_MAX_MESSAGE_LENGTH
    client = RecordingClient()
    custom_logger.slack_client = client
    custom_logger.messages = ["a" * limit, "b" * limit, "c" * limit]

    custom_logger.send_messages_to_slack()

    assert len(client.calls) == 3
    assert sleeps == [module.SLACK_SEND_PACE_SECONDS] * 2


def test_a_single_chunk_send_never_waits(module, custom_logger, sleeps):
    client = RecordingClient()
    custom_logger.slack_client = client
    custom_logger.messages = ["short report"]

    custom_logger.send_messages_to_slack()

    assert len(client.calls) == 1
    assert sleeps == []


def test_the_429_budget_is_per_chunk_not_per_run(module, custom_logger, caplog, sleeps):
    """Two chunks each 429 within their OWN budget: both retried, both sent.

    Kills the hoisted-counter mutation (`retries = 0` moved above the
    chunk loop): a RUN-scoped budget would exhaust on the run's fourth
    429 — chunk 3's second — and abandon a report every chunk of which
    was individually deliverable.
    """
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    limit = module.SLACK_MAX_MESSAGE_LENGTH
    messages = ["a" * limit, "b" * limit, "c" * limit]
    client = ScriptedClient(
        [
            rate_limited("2"),  # chunk 1: 429
            rate_limited("2"),  # chunk 1, retry 1: 429 again
            None,  # chunk 1, retry 2: accepted
            None,  # chunk 2 accepted
            rate_limited("3"),  # chunk 3: 429
            rate_limited("3"),  # chunk 3, retry 1: 429 again
            None,  # chunk 3, retry 2: accepted
        ]
    )
    custom_logger.slack_client = client
    custom_logger.messages = list(messages)

    assert custom_logger.send_messages_to_slack() is None

    assert client.sent == messages
    assert not error_records(caplog)
    pace = module.SLACK_SEND_PACE_SECONDS
    assert sleeps == [2.0, 2.0, pace, pace, 3.0, 3.0]


def test_a_fatal_error_on_a_later_chunk_keeps_the_earlier_sends(
    module, custom_logger, caplog, sleeps
):
    """Chunk 1 sent and paced, chunk 2 of 3 fatal: one ERROR naming it."""
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    limit = module.SLACK_MAX_MESSAGE_LENGTH
    messages = ["a" * limit, "b" * limit, "c" * limit]
    client = ScriptedClient([None, ConnectionError("network down")])
    custom_logger.slack_client = client
    custom_logger.messages = list(messages)

    assert custom_logger.send_messages_to_slack() is None

    assert client.sent == ["a" * limit]
    assert sleeps == [module.SLACK_SEND_PACE_SECONDS]
    errors = error_records(caplog)
    assert len(errors) == 1
    assert errors[0].exc_info is not None
    assert "chunk 2/3" in errors[0].getMessage()


def test_a_429_without_a_headers_attribute_still_retries_with_the_fallback(
    module, custom_logger, caplog, sleeps
):
    """A duck-typed 429 response missing `headers` entirely: 1.0 s fallback."""
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    exc = SlackApiError("ratelimited", SimpleNamespace(status_code=429))
    client = ScriptedClient([exc, None])
    custom_logger.slack_client = client
    custom_logger.messages = ["the report"]

    assert custom_logger.send_messages_to_slack() is None

    assert client.sent == ["the report"]
    assert sleeps == [1.0]
    assert not error_records(caplog)


def test_a_raising_pacing_sleep_never_propagates(module, custom_logger, monkeypatch):
    """The pacing wait sits OUTSIDE the per-chunk try and is guarded on
    its own: even a raising sleep must never escape into the scan run."""
    limit = module.SLACK_MAX_MESSAGE_LENGTH
    client = RecordingClient()
    custom_logger.slack_client = client
    custom_logger.messages = ["a" * limit, "b" * limit, "c" * limit]

    def exploding_sleep(seconds):
        raise RuntimeError("sleep interrupted")

    monkeypatch.setattr(module, "_slack_sleep", exploding_sleep)

    assert custom_logger.send_messages_to_slack() is None

    # Every chunk still went out; the guard swallowed the sleep failure.
    assert len(client.calls) == 3


def test_client_construction_failure_disables_slack(module, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    def broken_web_client(token=None):
        raise Exception("WebClient construction failed")

    monkeypatch.setattr(module, "SLACK_ACCESS_TOKEN", "xoxb-test-token")
    monkeypatch.setattr(module, "WebClient", broken_web_client)

    custom_logger = module.CustomLogger()

    assert custom_logger.slack_client is None
    errors = error_records(caplog)
    assert len(errors) == 1
    assert errors[0].exc_info is not None

    # The send then takes the skip path: no raise, no further error record.
    custom_logger.messages = ["a message"]
    assert custom_logger.send_messages_to_slack() is None
    assert len(error_records(caplog)) == 1


# --- skip paths -------------------------------------------------------------


def test_absent_config_skips_send_without_error(module, custom_logger, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    assert custom_logger.slack_client is None  # token None -> no client built
    custom_logger.messages = ["scan ran fine"]

    assert custom_logger.send_messages_to_slack() is None

    assert not error_records(caplog)
    skips = [
        r
        for r in caplog.records
        if r.levelno == logging.INFO and "skipped" in r.getMessage()
    ]
    assert len(skips) == 1


def test_all_empty_messages_skip_is_logged(module, custom_logger, caplog):
    # Non-empty list that packs to zero chunks (all-empty strings): no call,
    # no error, but the no-send path still leaves a log line.
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    client = RecordingClient()
    custom_logger.slack_client = client
    custom_logger.messages = ["", ""]

    assert custom_logger.send_messages_to_slack() is None

    assert client.calls == []
    assert not error_records(caplog)
    skips = [
        r
        for r in caplog.records
        if r.getMessage() == "Slack notification skipped: nothing to send"
    ]
    assert len(skips) == 1


def test_empty_messages_makes_no_slack_call(module, custom_logger, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    client = RecordingClient()
    custom_logger.slack_client = client
    assert custom_logger.messages == []

    assert custom_logger.send_messages_to_slack() is None

    assert client.calls == []
    assert not error_records(caplog)


# --- cross-module identity --------------------------------------------------


def test_slack_block_identical_across_commands():
    assert inspect.getsource(scan_mod.CustomLogger) == inspect.getsource(
        check_mod.CustomLogger
    )
    assert (
        scan_mod.SLACK_MAX_MESSAGE_LENGTH == check_mod.SLACK_MAX_MESSAGE_LENGTH == 4000
    )
    # Story 3.2: the retry/pacing constants and helpers are part of the
    # shared SLACK band and must never drift between the two commands.
    assert scan_mod.SLACK_RATE_LIMIT_RETRIES == check_mod.SLACK_RATE_LIMIT_RETRIES == 3
    assert scan_mod.SLACK_SEND_PACE_SECONDS == check_mod.SLACK_SEND_PACE_SECONDS == 1.0
    assert inspect.getsource(scan_mod._retry_after_seconds) == inspect.getsource(
        check_mod._retry_after_seconds
    )
    assert inspect.getsource(scan_mod._slack_sleep) == inspect.getsource(
        check_mod._slack_sleep
    )
