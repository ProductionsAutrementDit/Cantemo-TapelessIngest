"""Tier 1: robust end-of-scan Slack notification (story 1.5).

Covers the CustomLogger hardening in both command modules: chunking at
SLACK_MAX_MESSAGE_LENGTH, the never-raises error boundary around the send
loop, the absent-config / construction-failure skip paths, and the
byte-identity of the shared block across the two files.

AD-11: only the `slack_client` attribute on a CustomLogger instance and
attributes of our own command modules are stubbed — never Portal, never
slack_sdk internals.
"""

import inspect
import logging

import pytest

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


@pytest.fixture(params=MODULES, ids=MODULE_IDS)
def module(request):
    return request.param


@pytest.fixture
def custom_logger(module, monkeypatch):
    """A CustomLogger built with no token (client None); tests attach stubs."""
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
    [Exception("slack api error: invalid_auth"), ConnectionError("network down")],
    ids=["api-error", "network-error"],
)
def test_send_failure_is_swallowed_and_logged(module, custom_logger, caplog, exc):
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    client = RaisingClient(exc)
    custom_logger.slack_client = client
    custom_logger.messages = ["a" * module.SLACK_MAX_MESSAGE_LENGTH] * 3

    # Must return normally: never raises, never alters process state.
    assert custom_logger.send_messages_to_slack() is None

    # Abort on first failure: remaining chunks abandoned after chunk 1.
    assert client.calls == 1
    errors = error_records(caplog)
    assert len(errors) == 1
    assert errors[0].exc_info is not None
    assert "chunk 1/3" in errors[0].getMessage()


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
