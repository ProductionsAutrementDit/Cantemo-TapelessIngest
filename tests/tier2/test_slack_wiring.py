"""Tier 2: the portal.conf token flows through handle() into the Slack client.

Story 1.5 review gap: with fallback=None a section-name typo now degrades to
a silent "notification skipped" INFO instead of the old loud crash, so this
call_command test pins the happy path — the token the config read returns
(exact ("slack", "ACCESS_TOKEN") ask, raw=True, fallback=None) is the one
the WebClient is constructed with — plus the sibling skip cases: get returns
None (absent section) and "" (present section, empty value).

AD-11: only the command module's OWN attributes (ConfigParser, WebClient)
and one PLUGIN method (Folder.scan_tree) are monkeypatched — never Portal,
never slack_sdk internals. The REAL CustomLogger runs.

Story 2.8 rebound the run seam: `handle()` no longer calls a module-level
`scan_tapeless_dir`, it calls `folder.scan_tree(ctx, emit=logger.log)`. The
stub stands in for that instead. It is deliberately silent — this test is
about the TOKEN reaching the client, and the run's own report would only
add noise; the messages the send loop packs are handle()'s own header line.
"""

import importlib

import pytest
from django.contrib.auth.models import User
from django.core.management import call_command

from portal.plugins.TapelessIngest.models.folder import Folder

COMMANDS = ["scan_tapeless_dir", "check_clips_in_folder"]

BASE_ARGS = ["--storage", "VX-41", "--path", "2026"]

# A literal % in the value: interpolation would raise before the assert-based
# contract below could even matter; raw=True keeps it inert.
SENTINEL_TOKEN = "xoxb-sentinel-100%-raw"


class _RecordingWebClient:
    """Stands in for the module's WebClient: records construction, no I/O."""

    def __init__(self, token=None):
        self.token = token
        self.calls = []

    def chat_postMessage(self, **kwargs):
        self.calls.append(kwargs)


def _stub_config_parser(token):
    class _StubConfigParser:
        """Stands in for the module's ConfigParser and pins the read contract."""

        def read(self, path):
            return []

        def get(self, section, option, raw=False, fallback=None):
            assert (section, option) == ("slack", "ACCESS_TOKEN")
            assert raw is True  # a literal % must not trigger interpolation
            assert fallback is None
            return token if token is not None else fallback

    return _StubConfigParser


@pytest.mark.parametrize("command", COMMANDS)
@pytest.mark.parametrize(
    ("token", "client_built"),
    [
        (SENTINEL_TOKEN, True),
        (None, False),  # absent [slack] section -> fallback engaged
        ("", False),  # present section, empty value -> same skip path
    ],
    ids=["token-set", "config-absent", "token-empty"],
)
def test_config_token_reaches_web_client(
    command, token, client_built, migrated_db, monkeypatch, storage_fake
):
    module = importlib.import_module(
        f"portal.plugins.TapelessIngest.management.commands.{command}"
    )
    # handle() reassigns these module globals; re-setting them to their
    # current values via monkeypatch restores them on teardown.
    monkeypatch.setattr(module, "SLACK_ACCESS_TOKEN", module.SLACK_ACCESS_TOKEN)
    monkeypatch.setattr(module, "logger", module.logger)

    monkeypatch.setattr(Folder, "scan_tree", lambda self, ctx, *, emit: None)
    monkeypatch.setattr(module, "ConfigParser", _stub_config_parser(token))
    monkeypatch.setattr(module, "WebClient", _RecordingWebClient)
    # No cache.set("storage:VX-41", ...) preset any more (story 2.1): tree
    # mode resolves the storage through the counting StorageHelper fake in
    # tests/portal_stub, and handle()'s log line reads the run context.
    storage_fake.set_root("VX-41", "/wired-root")

    user = User.objects.create(pk=4243, username=f"story15-wiring-{command}")
    try:
        call_command(command, *BASE_ARGS, "--userId", "4243")
        constructed = module.logger
    finally:
        user.delete()  # keep the auth table empty for the unknown-user tests

    if client_built:
        # The real CustomLogger built its client from the config token.
        assert isinstance(constructed.slack_client, _RecordingWebClient)
        assert constructed.slack_client.token == SENTINEL_TOKEN
        # And the end-of-run report was sent through that client.
        assert constructed.slack_client.calls
        assert all(
            call["channel"] == "pad-notifications-cantemo"
            for call in constructed.slack_client.calls
        )
    else:
        assert constructed.slack_client is None
