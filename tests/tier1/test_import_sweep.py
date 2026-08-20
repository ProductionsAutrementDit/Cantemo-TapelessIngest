"""Tier 1 import sweep: every epic-relevant plugin module loads off-server.

The providers are imported statically here on purpose: in production they
load dynamically via __import__ at models/clip.py:211, so nothing else pins
their import health.

EXCLUDED (verified failing today, out of Epic 1 scope, tracked as deferred
work — do not add without the stub additions they need):
- views, serializers — need djangorestframework plus
  portal.generic.baseviews / portal.generic.decorators (not stubbed);
- update_original_file_metadatas — needs portal.search.models and
  postprocess_search (not stubbed);
- providers.audio_files — pre-existing SyntaxError in production code
  (providers/audio_files.py:98, stray "h" after the def's colon =>
  IndentationError at :100). Production code is read-only in this story;
  the module is absent from every PROVIDERS_LIST so it never loads in
  prod either. Tracked as deferred work.
"""

import importlib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

SWEEP_MODULES = [
    "portal.plugins.TapelessIngest.plugin",
    "portal.plugins.TapelessIngest.forms",
    "portal.plugins.TapelessIngest.metadatas",
    "portal.plugins.TapelessIngest.filesystem_scanner",
    "portal.plugins.TapelessIngest.models.settings",
    "portal.plugins.TapelessIngest.management.commands.scan_tapeless_dir",
    "portal.plugins.TapelessIngest.management.commands.check_clips_in_folder",
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
