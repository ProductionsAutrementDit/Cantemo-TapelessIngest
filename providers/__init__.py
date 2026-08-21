"""Canonical provider registry membership.

Pure data, ZERO imports: the single source of truth for which providers
participate in a scan, importable from anywhere (models, adapters, the
management commands, the golden-doc recorder) without dragging Portal,
Django, or a provider module in.

Order is load bearing. It is the production cron order, and it decides
which provider claims a file when several are applicable to it — so it
decides the clip's umid and primary key.

Membership feeds the Elasticsearch discovery query, so adding or
removing a name changes the byte-frozen golden search doc and needs
human sign-off. See docs/adding-a-provider.md.
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
