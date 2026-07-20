"""XML access and introspection bookkeeping for the XTCE parser.

The namespace-aware element toolkit (_tag/_find/_findall) is the choke
point all element access goes through; it records what the parser
consumed so unread elements can be reported (the inspect -v/-vv story).
"""

import logging
import xml.etree.ElementTree as ET
from typing import Optional

# Import all models from the models module

logger = logging.getLogger("xtce_sim.parser")

XTCE_NAMESPACES = [
    "http://www.omg.org/spec/XTCE/20250214",  # XTCE 1.3 (newest)
    "http://www.omg.org/spec/XTCE/20180204",  # XTCE 1.2
    "http://www.omg.org/space/xtce",  # Older format
    "www.omg.org",  # Simplified (used in our telemetry file)
]


class ReaderMixin:
    """Namespace handling, element access, and ignored-element records."""

    def __init__(self):
        self.ns: str = ""  # Namespace URI
        self.ns_prefix: str = ""  # Namespace prefix for ElementTree
        # Diagnostic warnings are suppressed for the intermediate per-file parses
        # inside parse_multiple() — refs may only resolve after the merge.
        self._warn: bool = True
        # Elements the parser actually looked at (by id()), recorded in
        # _find/_findall — the choke points all element access goes through.
        # Swept after each parse to report what the file declared but the
        # parser never consumed. Reset per parse().
        self._touched: set[int] = set()

    def _detect_namespace(self, root: ET.Element) -> str:
        """Detect XTCE namespace from root element."""
        # Check tag for namespace
        if root.tag.startswith("{"):
            ns = root.tag[1 : root.tag.index("}")]
            return ns

        # Check xmlns attribute
        for attr, value in root.attrib.items():
            if attr == "xmlns" or attr.endswith("}xmlns"):
                if any(known_ns in value for known_ns in XTCE_NAMESPACES):
                    return value

        # Default to XTCE 1.2
        return XTCE_NAMESPACES[0]

    def _tag(self, name: str) -> str:
        """Create namespaced tag name."""
        if self.ns:
            return f"{{{self.ns}}}{name}"
        return name

    def _find(self, element: ET.Element, path: str) -> Optional[ET.Element]:
        """Find element with namespace handling, recording it as consumed."""
        # Convert path to namespaced version
        parts = path.split("/")
        ns_path = "/".join(self._tag(p) for p in parts if p)
        found = element.find(ns_path)
        if found is not None:
            self._touched.add(id(found))
        return found

    def _findall(self, element: ET.Element, path: str) -> list[ET.Element]:
        """Find all elements with namespace handling, recording them as consumed."""
        parts = path.split("/")
        ns_path = "/".join(self._tag(p) for p in parts if p)
        found = element.findall(ns_path)
        for child in found:
            self._touched.add(id(child))
        return found

    def _get_attr(self, element: ET.Element, name: str, default: str = "") -> str:
        """Get attribute value with default."""
        return element.attrib.get(name, default)

    def _strip_path_ref(self, ref: str) -> str:
        """Strip XTCE path-qualified reference down to the leaf name.

        XTCE allows cross-SpaceSystem references with relative paths like:
          ../../CCSDSDirectTelecommand
          ../PUS_Structure_ID
          BusElectronics/../BusElectronics/Battery_Charge_Mode

        Since we flatten all SpaceSystems into one definition, we only
        need the final name component.
        """
        if "/" in ref:
            return ref.split("/")[-1]
        return ref

    # Purely documentational elements — reported at DEBUG (the firehose tier)
    # rather than INFO, so a Header on every file doesn't drown real gaps
    # like an unsupported calibrator or container entry type.
    _DOC_ELEMENTS = frozenset({"Header", "LongDescription", "AliasSet"})

    @staticmethod
    def _local_tag(tag: object) -> str:
        return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else str(tag)

    def _report_unconsumed(self, root: ET.Element) -> None:
        """Report elements the file declared but the parse never read.

        Walks the tree, descending only into elements the parser consumed
        (recorded by _find/_findall). The topmost unconsumed element is
        reported once per tag — with a count, its nested-element total, and
        one example location — rather than flooding a line per occurrence.
        """
        ignored = self._collect_unconsumed(root)
        for tag in sorted(ignored, key=lambda t: (-ignored[t][0], t)):
            count, nested, context = ignored[tag]
            level = logging.DEBUG if tag in self._DOC_ELEMENTS else logging.INFO
            logger.log(
                level,
                "~ ignored %d <%s> element(s)%s (e.g. under %s) — present in "
                "the XTCE but not read by this parser",
                count,
                tag,
                f" (+{nested} nested)" if nested else "",
                context,
            )

    def _collect_unconsumed(self, root: ET.Element) -> dict[str, list]:
        """Gather topmost unconsumed elements, grouped by local tag.

        Returns ``{tag: [occurrences, nested descendants, example context]}``.
        """
        ignored: dict[str, list] = {}
        stack: list[ET.Element] = [root]
        while stack:
            elem = stack.pop()
            # reversed() so LIFO pops visit children in document order — the
            # "e.g. under ..." example is then the first occurrence in the file.
            for child in reversed(elem):
                if not isinstance(child.tag, str):  # comments / PIs
                    continue
                if id(child) in self._touched:
                    stack.append(child)
                else:
                    self._record_ignored(ignored, elem, child)
        return ignored

    def _record_ignored(
        self, ignored: dict[str, list], parent: ET.Element, child: ET.Element
    ) -> None:
        """Fold one unconsumed element into the per-tag grouping."""
        entry = ignored.setdefault(self._local_tag(child.tag), [0, 0, ""])
        entry[0] += 1
        entry[1] += sum(1 for _ in child.iter()) - 1  # iter() includes self
        if not entry[2]:
            parent_name = self._get_attr(parent, "name")
            entry[2] = self._local_tag(parent.tag) + (f" {parent_name!r}" if parent_name else "")
