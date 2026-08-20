"""Golden-doc recorder: prints today's Folder.build_search_doc output verbatim.

Run BY PATH, never via `-m` (tests/ is deliberately not a package):

    PYTHONHASHSEED=0 .venv/bin/python tests/tier1/build_golden_doc.py

`build_search_doc` list order comes from `list(set(...))`, so byte-identity
requires a seeded interpreter — the golden test runs this script in a
subprocess with PYTHONHASHSEED=0, and the fixture was recorded the same way
(two seeded runs, empty diff). AD-3: the output is recorded verbatim — no
sorting, normalization, or cleanup, ever. Re-recording after this story
requires human sign-off.
"""

import json
import os
import sys
from pathlib import Path

# Run-by-path has no import context: repo root first on sys.path so
# `tests.portal_stub` resolves, then the same bootstrap order as conftest.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.portal_stub import install  # noqa: E402

install()

os.environ["DJANGO_SETTINGS_MODULE"] = "tests.tier2_settings"

import django  # noqa: E402

django.setup()

from portal.plugins.TapelessIngest.management.commands.scan_tapeless_dir import (  # noqa: E402,E501
    PROVIDERS,
)
from portal.plugins.TapelessIngest.models.clip import Clip  # noqa: E402
from portal.plugins.TapelessIngest.models.folder import Folder  # noqa: E402


def main():
    # Ratified golden constants; the path need not exist — the pin is query
    # construction. Explicit kwargs, NEVER positional: Django model positional
    # args bind in field-declaration order (id, created_on, ...), which would
    # silently produce a wrong-but-deterministic doc.
    folder = Folder(storage_id="VX-41", path="2026/AH_20260101_golden")
    provider_list = Clip._get_provider_list(PROVIDERS)
    print(json.dumps(folder.build_search_doc(provider_list), indent=2))


if __name__ == "__main__":
    main()
