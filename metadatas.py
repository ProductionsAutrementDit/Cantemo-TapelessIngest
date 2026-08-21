from lxml import etree
import logging

log = logging.getLogger(__name__)
from os.path import isfile


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
        being re-read and re-parsed from the card. A ``str`` is encoded
        first — lxml refuses a unicode string carrying an encoding
        declaration, which every card sidecar has.
        """
        parser = cls.__new__(cls)
        parser.tree = ""
        parser.root = ""
        parser.nsmap = ""
        if isinstance(xml_string, str):
            xml_string = xml_string.encode("utf-8")
        parser._bind(etree.fromstring(xml_string).getroottree())
        return parser

    def parse(self, xml_file):
        if not isfile(xml_file):
            raise FileNotFoundError
        self._bind(etree.parse(xml_file))

    def _bind(self, tree):
        self.tree = tree
        self.root = self.tree.getroot()
        if None in self.root.nsmap:
            self.nsmap = {"h": self.root.nsmap[None]}
        else:
            self.nsmap = None

    def tostring(self):
        return etree.tostring(self.root)

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
