"""Tier 1: the component-wait bounds the docs quote ARE the constants.

README.md and USER_GUIDE.md both state the component-wait triple in
seconds — the scan's per-clip bound, the REST per-clip bound, and the
REST whole-call budget — and USER_GUIDE.md reasons about them at length
(a table, a "50-clip folder cannot cost 50 x 30 s" worked example, a
"much shorter bound" comparison). Nothing asserted any of it against
``models/clip.py``, so changing one constant left two documents quietly
wrong, in a place an operator uses to budget wall-clock time for a cron
run against a wedged Vidispine.

DB-free, so tier 1: it reads module constants and two files on disk.
"""

import os
import re

import pytest

from portal.plugins.TapelessIngest.models import clip as clip_module

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(clip_module.__file__)))

# (constant name, the documents that quote it). The VALUE is deliberately
# not repeated here: a second copy of the number is a third place to be
# wrong. The constant is the single source, and the documents are checked
# against it.
DOCUMENTED_WAIT_CONSTANTS = [
    ("EXTRA_COMPONENT_WAIT_SECONDS", ("README.md", "USER_GUIDE.md")),
    ("REST_EXTRA_COMPONENT_WAIT_SECONDS", ("README.md", "USER_GUIDE.md")),
    ("REST_COMPONENT_WAIT_BUDGET_SECONDS", ("README.md", "USER_GUIDE.md")),
]


def _doc(name):
    with open(os.path.join(PLUGIN_ROOT, name), encoding="utf-8") as handle:
        return handle.read()


@pytest.mark.parametrize("constant, documents", DOCUMENTED_WAIT_CONSTANTS)
def test_the_documented_wait_bounds_are_the_module_constants(constant, documents):
    """Each document quotes the constant's own value, in seconds."""
    value = getattr(clip_module, constant)
    # The docs write "300 s", never "300.0 s": a constant that stopped
    # being a whole number of seconds would render as something no
    # document says, and the honest answer is to fail here rather than to
    # match loosely.
    assert value == int(value), (
        f"{constant} is {value!r}, which no document can state as a whole "
        f"number of seconds — update the docs and this test together"
    )
    # `(?<!\d)` / `(?!\d)`: without them "60 s" matches inside "160 s" and
    # the test passes on a document stating the wrong number.
    pattern = re.compile(rf"(?<!\d){int(value)} s(?!\d)")
    for name in documents:
        assert pattern.search(_doc(name)), (
            f"{name} does not state {constant} = {int(value)} s. Either the "
            f"constant moved and the document was left behind, or the "
            f"document rephrased the number out of reach of this pin."
        )


def test_the_rest_bounds_stay_shorter_than_the_scan_bound():
    """The RELATION both documents reason from, not only the numbers.

    USER_GUIDE.md calls the REST per-clip bound "a much shorter bound"
    and explains the whole-call budget as the thing that stops N clips
    costing N x the per-clip bound; README.md repeats it. Raising the
    REST bound past the scan's, or dropping the budget below one clip's
    bound, would leave both explanations false while every number in
    them still matched.
    """
    assert (
        clip_module.REST_EXTRA_COMPONENT_WAIT_SECONDS
        < clip_module.EXTRA_COMPONENT_WAIT_SECONDS
    )
    assert (
        clip_module.REST_EXTRA_COMPONENT_WAIT_SECONDS
        < clip_module.REST_COMPONENT_WAIT_BUDGET_SECONDS
    )
