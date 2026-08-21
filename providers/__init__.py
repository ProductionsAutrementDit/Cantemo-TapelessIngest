"""Canonical provider registry membership (story 2.3).

Pure data, ZERO imports: this module is the single source of truth for
which providers participate in a scan, and it must stay importable from
anywhere (models, adapters, the management commands, the golden-doc
recorder) without dragging Portal, Django, or a provider module in.

Order is the production cron order (`--providers` default in both
management commands), ratified as canonical: the UI's paged scans move
from the old red-first ordering to this one, which is behaviorally inert
(red guards on ``.R3D``; ``file`` is last in both lists; no other key
overlap). Membership is IDENTICAL to every live list before 2.3 — same
8 names — so the byte-frozen golden search doc is unchanged.

Adding or removing a name here changes the golden doc and needs human
sign-off (spec 2.3, "Ask First").
"""

PROVIDER_NAMES = (
    "panasonicP2",
    "xdcam",
    "hdslr",
    "zoom",
    "red",
    "avchd",
    "atomos",
    "file",
)
