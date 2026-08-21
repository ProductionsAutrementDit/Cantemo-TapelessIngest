# Adding a provider

A provider turns one media file into a dict of metadatas. The scan
pipeline runs providers in two steps — discovery (which files the
Elasticsearch query returns) and extraction (which providers get to
claim each file) — and both steps read declarations off the provider
class. Getting a declaration wrong does not raise; it silently changes
which provider claims a file, which changes the clip's `umid`, which is
its primary key. A clip whose primary key changes is re-ingested as a
duplicate.

## The pieces

Register the name in `providers/__init__.py`'s `PROVIDER_NAMES`. That
tuple is the single source of truth: `models.clip.PROVIDERS_LIST`, both
management commands' `PROVIDERS` default, and
`scan.adapters.build_provider_registry` all read it. Nothing else may
hold a provider list — `helpers.py` and `utilities.py` used to carry
divergent copies and those names are retired, not deprecated: there are
no in-repo importers and this plugin has no external consumers, so a
reference to `helpers.PROVIDERS_LIST` is a bug to fix, not a shim to
add. The two commands' `PROVIDERS` aliases are kept only because the
golden-doc recorder imports one of them.

Order in `PROVIDER_NAMES` is load bearing twice over: it is the order
providers run in, and the merge is last-writer-wins, so an earlier
provider's `umid` survives only because later providers guard against
overwriting it.

Implement, on a subclass of `providers.providers.Provider`:

| method | feeds | must be |
|---|---|---|
| `getExtensions()` | the ES wildcard filter AND the extraction pre-filter | a **superset** of your runtime guard |
| `getSubPaths()` | the ES parent-path filter | the directory shapes you live in |
| `getFilters(escaped_path)` | raw ES clauses OR'd into the query | optional |
| `is_extension_guarded()` | the extraction pre-filter | `False` if your guard ignores the extension |
| `getMetadatasFromFile(media_file, metadatas, context)` | the merge loop | return metadatas only |

Adding or removing a name in `PROVIDER_NAMES`, or changing any of the
first three methods, changes the golden search doc
(`tests/fixtures/golden_search_doc.json`), which is byte-frozen and
needs human sign-off to re-record.

## The superset rule

**Whatever your `getMetadatasFromFile` can claim, `getExtensions()` must
declare — or you must declare yourself not extension-guarded.**

The pre-filter only invokes providers whose declared suffixes match the
filename. If your runtime guard accepts a file your declaration does not
cover, the pre-filter removes you from the running and a different
provider claims that file with a different `umid`.

Two escapes, both already used:

* **Widen the declaration.** `providers/red.py` guards on
  `file_extension == ".R3D"` but historically advertised only
  `"_001.r3d"` to the ES wildcard. It now declares
  `["_001.r3d", ".r3d"]`. The broad form is inert for discovery
  (`providers/file.py` already declares `.r3d` and the query dedupes
  through `set()`) and it makes the declaration a superset of the guard.
* **Declare yourself non-extension-guarded.** The card providers
  (`xdcam`, `panasonicP2`, `ikegami`) guard purely on sidecar presence —
  `{clip}M01.XML`, `../CLIP/{name}.XML`, `../CLIPINF/CLIP{name}.XML` —
  which says nothing about the extension. An `.mov` or `.wav` sitting in
  an XDCAM structure is theirs. They override
  `is_extension_guarded()` to return `False` and the pre-filter treats
  them as applicable to every file.

A provider that declares **no** extensions is also treated as applicable
to everything: an empty declaration means unknown reach, and unknown
reach must not be silently filtered away.

## The extraction contract

```python
def getMetadatasFromFile(self, media_file, metadatas, context):
    if not self._my_guard(media_file):
        return metadatas          # contribute nothing
    metadatas["provider"] = self.machine_name
    metadatas["umid"] = ...
    return metadatas
```

* **Return the metadatas only.** `context` is an argument and is mutated
  in place; returning the old `(metadatas, context)` two-tuple raises a
  `TypeError` in the merge loop.
* **Every applicable provider runs.** There is no break on first match.
  Contribute by mutating `metadatas` in place or by returning a mapping
  of just the keys you own — both are merged.
* **`umid` and `provider` are identity keys.** Overwriting either once
  another provider has set them is logged as an error, because it
  changes the clip's primary key. Guard with
  `if "provider" not in metadatas` if you are a fallback provider.
* **Probe sidecars with `self.probe_is_file(path, context)`**, not
  `os.path.isfile`. It answers from the scan's directory listings, so a
  present sidecar costs no filesystem call and a parent directory is
  listed at most once per scan. Pass absolute paths. Probing a sibling
  directory that does not exist on this card (`../CLIP`, `../CLIPINF`)
  is free and silent — the probe is marked speculative, so "no such
  directory" is an answer, not a folder error. A sibling directory that
  exists but cannot be read is still reported.
* **Resolve absolute paths with `self.get_file_absolute_path(file,
  context)`**, which reads the run's resolved storage roots instead of
  calling the storage API per file.

## Known trap: the `file` provider family

`atomos`, `avchd`, `hdslr`, `zoom` and `image_file` subclass
`providers/file.py` and inherit its guard, which is "claim this file if
no provider has yet" plus a `self.file_types` lookup. They each declare
a narrow `getExtensions()` but inherit `file.py`'s **full** `file_types`
dict, so their runtime reach is wider than their declaration — the
superset rule is violated for the whole family, and the pre-filter
therefore changes which subclass wins for several suffixes. This is
recorded as deferred work; do not model a new provider on them.
