"""Tier 1 import sweep: every epic-relevant plugin module loads off-server.

The providers are imported statically here on purpose: in production they
load dynamically via __import__ at models/clip.py:211, so nothing else pins
their import health.

`views` earns its place here rather than obviously deserving it: it is
the only module that calls `Clip.persist_metadatas()` (the explicit
replacement for the deleted `save()` fan-out), and nothing else imports
it, so a typo in that call — or its quiet deletion — would have shipped.

EXCLUDED (verified failing today, out of Epic 1 scope, tracked as deferred
work — do not add without the stub additions they need):
- update_original_file_metadatas — needs portal.search.models and
  postprocess_search (not stubbed);
- providers.audio_files — pre-existing SyntaxError in production code
  (providers/audio_files.py:98, stray "h" after the def's colon =>
  IndentationError at :100). Production code is read-only in this story;
  the module is absent from every PROVIDERS_LIST so it never loads in
  prod either. Tracked as deferred work.
"""

import importlib
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

SWEEP_MODULES = [
    "portal.plugins.TapelessIngest.plugin",
    "portal.plugins.TapelessIngest.forms",
    "portal.plugins.TapelessIngest.metadatas",
    "portal.plugins.TapelessIngest.serializers",
    "portal.plugins.TapelessIngest.views",
    "portal.plugins.TapelessIngest.filesystem_scanner",
    "portal.plugins.TapelessIngest.models.settings",
    "portal.plugins.TapelessIngest.management.commands.scan_tapeless_dir",
    "portal.plugins.TapelessIngest.management.commands.check_clips_in_folder",
    # Story 4.2: the FR-4 gate. It is the only command with no twin,
    # so nothing else pins its import health.
    "portal.plugins.TapelessIngest.management.commands.verify_discovery_equivalence",
    "portal.plugins.TapelessIngest.providers.providers",
    "portal.plugins.TapelessIngest.providers.atomos",
    "portal.plugins.TapelessIngest.providers.avchd",
    "portal.plugins.TapelessIngest.providers.file",
    "portal.plugins.TapelessIngest.providers.hdslr",
    "portal.plugins.TapelessIngest.providers.ikegami",
    "portal.plugins.TapelessIngest.providers.image_file",
    "portal.plugins.TapelessIngest.providers.jvcprohd",
    "portal.plugins.TapelessIngest.providers.panasonicP2",
    "portal.plugins.TapelessIngest.providers.red",
    "portal.plugins.TapelessIngest.providers.xdcam",
    "portal.plugins.TapelessIngest.providers.zoom",
]


@pytest.mark.parametrize("dotted", SWEEP_MODULES)
def test_module_imports_through_substrate(dotted):
    module = importlib.import_module(dotted)
    assert module.__file__ is not None, f"{dotted} resolved as a namespace package"
    module_file = Path(module.__file__).resolve()
    assert module_file.is_relative_to(
        REPO_ROOT
    ), f"{dotted} resolved outside this repo: {module_file}"


def test_views_calls_only_methods_clips_really_have():
    """Importing views proves it loads; this proves what it calls exists.

    ``views.py`` is the sole caller of ``Clip.persist_metadatas()`` — the
    explicit replacement for the deleted ``save()`` metadata fan-out — and
    a REST endpoint's runtime AttributeError is a production incident, not
    a test failure. Cheap static guard over every ``clip.<name>(`` in the
    module.
    """
    from portal.plugins.TapelessIngest.models.clip import Clip

    source = (REPO_ROOT / "views.py").read_text(encoding="utf-8")
    called = sorted(set(re.findall(r"\bclip\.(\w+)\(", source)))
    assert "persist_metadatas" in called, called
    missing = [name for name in called if not hasattr(Clip, name)]
    assert not missing, f"views.py calls Clip methods that do not exist: {missing}"
