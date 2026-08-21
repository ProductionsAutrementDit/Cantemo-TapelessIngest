"""Tier 1: metadatas.XMLParser — the card-sidecar parser, hardened.

Untested until now, and no longer a leaf detail: since FR-12 a sidecar is
parsed, serialized into ``Clip.clip_xml`` and re-parsed from there, so the
parser decides both what an ingest reads and what the database stores.

Covered: the entity/network hardening (card XML is untrusted input that
now reaches the DB), the encoding round-trip (a stored document must come
back the way it went in, whatever the card's own encoding was), and the
path/namespace behavior the providers depend on.

No DB, no Portal: lxml and a tmp file.
"""

import pytest
from lxml import etree

from portal.plugins.TapelessIngest.metadatas import XMLParser

XDCAM_SIDECAR = """<?xml version="1.0" encoding="UTF-8"?>
<NonRealTimeMeta>
  <TargetMaterial umidRef="XDCAM-UMID-C0001"/>
  <Duration value="250"/>
</NonRealTimeMeta>
"""

NAMESPACED_SIDECAR = """<?xml version="1.0" encoding="UTF-8"?>
<NonRealTimeMeta xmlns="urn:schemas-professionalDisc:nonRealTimeMeta">
  <Duration value="99"/>
</NonRealTimeMeta>
"""


def _write(tmp_path, name, payload, encoding="utf-8"):
    path = tmp_path / name
    if isinstance(payload, str):
        payload = payload.encode(encoding)
    path.write_bytes(payload)
    return str(path)


# --------------------------------------------------------------------------
# Security: card XML is untrusted, and since FR-12 it is stored and re-read
# --------------------------------------------------------------------------


def test_external_entities_are_not_resolved_from_a_file(tmp_path):
    """The XXE family, straight out of a card's sidecar.

    lxml's default parser expands entity declarations, so a sidecar
    declaring one that points at a local file would drop that file's
    contents into a metadata value — and, since FR-12, into the database.
    """
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP-SECRET-PAYLOAD")
    sidecar = _write(
        tmp_path,
        "xxe.xml",
        f"""<?xml version="1.0"?>
<!DOCTYPE root [<!ENTITY xxe SYSTEM "file://{secret}">]>
<root><Title>&xxe;</Title></root>
""",
    )

    parsed = XMLParser(sidecar)

    assert "TOP-SECRET-PAYLOAD" not in etree.tostring(parsed.root, encoding="unicode")
    assert parsed.getValueFromPath("Title") in (None, False, "")


def test_external_entities_are_not_resolved_from_a_stored_string(tmp_path):
    """Same guarantee on the re-hydration path (``clip_xml`` is text)."""
    secret = tmp_path / "secret2.txt"
    secret.write_text("TOP-SECRET-PAYLOAD")

    parsed = XMLParser.from_string(f"""<?xml version="1.0"?>
<!DOCTYPE root [<!ENTITY xxe SYSTEM "file://{secret}">]>
<root><Title>&xxe;</Title></root>
""")

    assert "TOP-SECRET-PAYLOAD" not in etree.tostring(parsed.root, encoding="unicode")


def test_a_billion_laughs_document_does_not_expand(tmp_path):
    sidecar = _write(
        tmp_path,
        "lol.xml",
        """<?xml version="1.0"?>
<!DOCTYPE lolz [
 <!ENTITY lol "lol">
 <!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
 <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">
]>
<lolz>&lol3;</lolz>
""",
    )

    parsed = XMLParser(sidecar)

    assert len(etree.tostring(parsed.root, encoding="unicode")) < 500


# --------------------------------------------------------------------------
# Encoding: what goes into clip_xml must come back out
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "encoding, declared, text",
    [
        ("shift_jis", "Shift_JIS", "テスト"),
        ("latin-1", "ISO-8859-1", "café"),
        ("utf-8", "UTF-8", "naïve — ünïcode"),
    ],
)
def test_a_non_utf8_sidecar_round_trips_through_the_stored_column(
    tmp_path, encoding, declared, text
):
    """The FR-12 loop: parse the card, serialize, store, re-parse.

    ``clip_xml`` is authoritative once written, so a lossy step here is
    unrecoverable — the card may be long gone by the next scan.
    """
    sidecar = _write(
        tmp_path,
        f"{encoding}.xml",
        f'<?xml version="1.0" encoding="{declared}"?>\n<Root><Title>{text}</Title></Root>',
        encoding=encoding,
    )

    parsed = XMLParser(sidecar)
    assert parsed.getValueFromPath("Title") == text

    serialized = parsed.tostring()
    # The column is text: a bytes value would be stored as its repr.
    assert isinstance(serialized, str)

    rehydrated = XMLParser.from_string(serialized)
    assert rehydrated.getValueFromPath("Title") == text


def test_from_string_ignores_a_stale_encoding_declaration():
    """A str is UTF-8 by the time lxml sees it — whatever it claims.

    A stored document still carrying ``encoding="Shift_JIS"`` would
    otherwise have its (UTF-8) bytes decoded as Shift-JIS: silent
    mojibake in a column nothing can correct afterwards.
    """
    parsed = XMLParser.from_string(
        '<?xml version="1.0" encoding="Shift_JIS"?><Root><Title>café</Title></Root>'
    )

    assert parsed.getValueFromPath("Title") == "café"


def test_from_string_still_accepts_bytes():
    """Bytes keep their declaration — there it is the truth."""
    parsed = XMLParser.from_string(
        '<?xml version="1.0" encoding="ISO-8859-1"?><Root><Title>café</Title></Root>'.encode(
            "latin-1"
        )
    )

    assert parsed.getValueFromPath("Title") == "café"


# --------------------------------------------------------------------------
# The behavior the providers depend on
# --------------------------------------------------------------------------


def test_values_are_read_by_path_and_attribute(tmp_path):
    parsed = XMLParser(_write(tmp_path, "xdcam.xml", XDCAM_SIDECAR))

    assert parsed.getValueFromPath("TargetMaterial/@umidRef") == "XDCAM-UMID-C0001"
    assert parsed.getValueFromPath("Duration/@value") == "250"


def test_a_default_namespace_is_bound_to_the_h_prefix(tmp_path):
    """Card sidecars declare a default namespace; the providers query
    unprefixed paths and rely on this rewriting."""
    parsed = XMLParser(_write(tmp_path, "ns.xml", NAMESPACED_SIDECAR))

    assert parsed.nsmap == {"h": "urn:schemas-professionalDisc:nonRealTimeMeta"}
    assert parsed.getValueFromPath("Duration/@value") == "99"


def test_a_missing_path_is_false_not_an_exception(tmp_path):
    parsed = XMLParser(_write(tmp_path, "missing.xml", XDCAM_SIDECAR))

    assert parsed.getValueFromPath("NoSuchElement/@value") is False


def test_a_missing_file_raises_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        XMLParser(str(tmp_path / "nope.xml"))


def test_a_round_trip_preserves_the_document_body(tmp_path):
    parsed = XMLParser(_write(tmp_path, "roundtrip.xml", XDCAM_SIDECAR))

    again = XMLParser.from_string(parsed.tostring())

    assert again.tostring() == parsed.tostring()
    assert again.getValueFromPath("TargetMaterial/@umidRef") == "XDCAM-UMID-C0001"
