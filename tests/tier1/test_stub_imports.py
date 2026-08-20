"""Tier 1 smoke tests: the real plugin modules import through the stub.

No DB access — this tier proves the AD-11 substrate routes
`portal.plugins.TapelessIngest.*` imports to this repo's code while every
Portal/Cantemo-only dependency resolves to tests/portal_stub.
"""

import importlib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _import_and_assert_in_repo(dotted):
    module = importlib.import_module(dotted)
    assert module.__file__ is not None, f"{dotted} resolved as a namespace package"
    module_file = Path(module.__file__).resolve()
    assert module_file.is_relative_to(
        REPO_ROOT
    ), f"{dotted} resolved outside this repo: {module_file}"
    return module


def test_portal_in_sys_modules_is_the_stub():
    assert getattr(
        sys.modules["portal"], "__portal_stub__", False
    ), "sys.modules['portal'] is not the tests/portal_stub package"


def test_scan_tapeless_dir_imports_through_stub():
    module = _import_and_assert_in_repo(
        "portal.plugins.TapelessIngest.management.commands.scan_tapeless_dir"
    )
    assert hasattr(module, "Command")


def test_helpers_imports_through_stub():
    _import_and_assert_in_repo("portal.plugins.TapelessIngest.helpers")


def test_models_folder_imports_through_stub():
    module = _import_and_assert_in_repo("portal.plugins.TapelessIngest.models.folder")
    assert hasattr(module, "Folder")
