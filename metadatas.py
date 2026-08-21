from lxml import etree
import logging

log = logging.getLogger(__name__)
from os.path import isfile


def _hardened_parser(**kwargs):
    """An lxml parser with the document's own privileges taken away.

    Card sidecars are untrusted input — they arrive on removable media
    and, since FR-12, are stored in the database and re-parsed later.
    lxml's defaults resolve entity declarations, which is the whole
    billion-laughs / file-disclosure family, and will happily fetch a
    remote DTD.

    * ``resolve_entities=False`` — an ``&xxe;`` stays an unexpanded
      reference instead of becoming ``/etc/passwd``;
    * ``no_network=True`` — no external DTD or entity is ever fetched;
    * ``huge_tree=False`` — libxml2's depth/size limits stay on.
    """
    return etree.XMLParser(
        resolve_entities=False, no_network=True, huge_tree=False, **kwargs
    )


class XMLParser:
    def __init__(self, xml_file):
        self.tree = ""
        self.root = ""
        self.nsmap = ""
        self.parse(xml_file)

    @classmethod
    def from_string(cls, xml_string):
        """Parse an already-serialized document (story 2.4, FR-12).

        The counterpart of ``tostring()``: a clip whose XML is stored in
        its ``clip_xml`` column is re-hydrated from the string instead of
        being re-read and re-parsed from the card.

        A ``str`` is encoded to UTF-8 and parsed with the encoding
        FORCED to UTF-8. Both halves matter: lxml refuses a unicode
        string carrying an encoding declaration, and a document declaring
        ``encoding="Shift_JIS"`` would otherwise have its (now UTF-8)
        bytes decoded as Shift-JIS — silent mojibake in a column that is
        authoritative from here on. ``bytes`` are parsed as they are:
        there the declaration is the truth.
        """
        parser = cls.__new__(cls)
        parser.tree = ""
        parser.root = ""
        parser.nsmap = ""
        if isinstance(xml_string, str):
            xml_string = xml_string.encode("utf-8")
            lxml_parser = _hardened_parser(encoding="utf-8")
        else:
            lxml_parser = _hardened_parser()
        parser._bind(etree.fromstring(xml_string, lxml_parser).getroottree())
        return parser

    def parse(self, xml_file):
        if not isfile(xml_file):
            raise FileNotFoundError
        self._bind(etree.parse(xml_file, _hardened_parser()))

    def _bind(self, tree):
        self.tree = tree
        self.root = self.tree.getroot()
        if None in self.root.nsmap:
            self.nsmap = {"h": self.root.nsmap[None]}
        else:
            self.nsmap = None

    def tostring(self):
        """Serialize the parsed document as TEXT.

        ``encoding="unicode"`` returns a ``str``, which is what every
        consumer wants: ``Clip.clip_xml`` is a text column, and a
        ``bytes`` value reaching it would be stored as its ``repr``.
        No XML declaration is emitted (there is no byte encoding to
        declare), so the value round-trips through ``from_string``
        whatever the sidecar's original encoding was.
        """
        return etree.tostring(self.root, encoding="unicode")

    def getValueFromPath(self, metadata_path, raw=False, root=None, return_type="text"):

        if root is None:
            root = self.root

        if self.nsmap is None:
            xpath = metadata_path
            metadata_elements = root.xpath(xpath)
        else:
            xpath_elements = metadata_path.split("/")
            for key, value in enumerate(xpath_elements):
                if not value.startswith("@") and not value.startswith("following"):
                    xpath_elements[key] = "h:" + value

            xpath = "/".join(xpath_elements)

            log.debug("Path is %s" % xpath)

            metadata_elements = root.xpath(xpath, namespaces=self.nsmap)

        if raw or len(metadata_elements) > 1:
            return metadata_elements
        else:
            if len(metadata_elements) == 1:
                if return_type == "text":
                    if isinstance(metadata_elements[0], etree._Element):
                        value = metadata_elements[0].text
                    elif isinstance(metadata_elements[0], str):
                        value = metadata_elements[0]
                    else:
                        log.debug(
                            "Unknown value type for %s (%s)"
                            % (metadata_path, type(metadata_elements[0]))
                        )
                        return False
                    log.debug("Map value %s to %s" % (value, metadata_path))
                else:
                    value = metadata_elements
                return value

            else:
                return False
                log.debug("No metadata found for %s" % metadata_path)
