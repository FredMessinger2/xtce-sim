"""Parser diagnostic warnings for empty parses and unresolved base refs."""

import logging

from xtce_sim.parser import XTCEParser

NS = 'xmlns:xtce="http://www.omg.org/spec/XTCE/20250214"'


def _write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text)
    return p


def test_warns_on_empty_parse_unnamespaced(tmp_path, caplog):
    # Un-namespaced file: the parser looks for namespaced tags, matches nothing,
    # and would silently return an empty definition — it must warn instead.
    f = _write(
        tmp_path,
        "plain.xml",
        '<SpaceSystem name="X"><TelemetryMetaData><ParameterTypeSet/>'
        "</TelemetryMetaData></SpaceSystem>",
    )
    with caplog.at_level(logging.WARNING):
        defn = XTCEParser().parse(f)
    assert not defn.parameter_types  # nothing matched
    assert any("no commands or telemetry" in r.message for r in caplog.records)


def test_warns_on_unresolved_base_command(tmp_path, caplog):
    f = _write(
        tmp_path,
        "cmd.xml",
        f'<xtce:SpaceSystem {NS} name="X"><xtce:CommandMetaData><xtce:MetaCommandSet>'
        '<xtce:MetaCommand name="C">'
        '<xtce:BaseMetaCommand metaCommandRef="Ghost"/>'
        "</xtce:MetaCommand>"
        "</xtce:MetaCommandSet></xtce:CommandMetaData></xtce:SpaceSystem>",
    )
    with caplog.at_level(logging.WARNING):
        XTCEParser().parse(f)
    assert any("unknown base command" in r.message for r in caplog.records)


def test_warns_on_unresolved_base_container(tmp_path, caplog):
    f = _write(
        tmp_path,
        "tlm.xml",
        f'<xtce:SpaceSystem {NS} name="X"><xtce:TelemetryMetaData><xtce:ContainerSet>'
        '<xtce:SequenceContainer name="P"><xtce:EntryList/>'
        '<xtce:BaseContainer containerRef="Ghost"/>'
        "</xtce:SequenceContainer>"
        "</xtce:ContainerSet></xtce:TelemetryMetaData></xtce:SpaceSystem>",
    )
    with caplog.at_level(logging.WARNING):
        XTCEParser().parse(f)
    assert any("unknown base container" in r.message for r in caplog.records)


def _int_param_type(name, size):
    return (
        f'<xtce:IntegerParameterType name="{name}" signed="false">'
        f'<xtce:IntegerDataEncoding sizeInBits="{size}" encoding="unsigned"/>'
        "</xtce:IntegerParameterType>"
    )


def test_parse_multiple_merges_override_and_reresolves(tmp_path, caplog):
    """Two files with a cross-file base ref: later file wins, ref re-resolves,
    and NO spurious 'unknown base container' warning fires (regression guard)."""
    # File 1: base container + a type at 8 bits.
    base = _write(
        tmp_path,
        "base.xml",
        f'<xtce:SpaceSystem {NS} name="Sat"><xtce:TelemetryMetaData>'
        f"<xtce:ParameterTypeSet>{_int_param_type('T', 8)}</xtce:ParameterTypeSet>"
        '<xtce:ParameterSet><xtce:Parameter name="P" parameterTypeRef="T"/></xtce:ParameterSet>'
        '<xtce:ContainerSet><xtce:SequenceContainer name="Base"><xtce:EntryList>'
        '<xtce:ParameterRefEntry parameterRef="P"/></xtce:EntryList>'
        "</xtce:SequenceContainer></xtce:ContainerSet>"
        "</xtce:TelemetryMetaData></xtce:SpaceSystem>",
    )
    # File 2: overrides type T (now 16 bits) and adds a container whose BASE is
    # defined in file 1 — only resolvable after the merge.
    overlay = _write(
        tmp_path,
        "overlay.xml",
        f'<xtce:SpaceSystem {NS} name="Sat"><xtce:TelemetryMetaData>'
        f"<xtce:ParameterTypeSet>{_int_param_type('T', 16)}</xtce:ParameterTypeSet>"
        '<xtce:ContainerSet><xtce:SequenceContainer name="Derived"><xtce:EntryList/>'
        '<xtce:BaseContainer containerRef="Base">'
        "<xtce:RestrictionCriteria>"
        '<xtce:Comparison parameterRef="CCSDS_APID" value="42"/>'
        "</xtce:RestrictionCriteria></xtce:BaseContainer>"
        "</xtce:SequenceContainer></xtce:ContainerSet>"
        "</xtce:TelemetryMetaData></xtce:SpaceSystem>",
    )
    with caplog.at_level(logging.WARNING):
        merged = XTCEParser().parse_multiple([base, overlay])

    # Override won: T is now 16 bits.
    assert merged.parameter_types["T"].size_in_bits == 16
    # Cross-file base ref resolved after merge.
    assert merged.containers["Derived"].base_container is merged.containers["Base"]
    # No spurious unresolved-base-ref warning for the cross-file case.
    assert not any("unknown base container" in r.message for r in caplog.records)


def test_parse_multiple_warns_per_empty_file(tmp_path, caplog):
    """A bad-namespace file that contributes nothing warns even when another
    file populates the merged definition."""
    good = _write(
        tmp_path,
        "good.xml",
        f'<xtce:SpaceSystem {NS} name="Sat"><xtce:TelemetryMetaData>'
        f"<xtce:ParameterTypeSet>{_int_param_type('T', 8)}</xtce:ParameterTypeSet>"
        "</xtce:TelemetryMetaData></xtce:SpaceSystem>",
    )
    # Un-namespaced -> matches nothing -> contributes nothing.
    empty = _write(
        tmp_path,
        "empty.xml",
        '<SpaceSystem name="Sat"><TelemetryMetaData><ParameterTypeSet/>'
        "</TelemetryMetaData></SpaceSystem>",
    )
    with caplog.at_level(logging.WARNING):
        merged = XTCEParser().parse_multiple([good, empty])
    assert "T" in merged.parameter_types  # merged is non-empty overall
    # ...but the empty overlay still got flagged.
    assert any(
        "no commands or telemetry" in r.message and "empty.xml" in r.message
        for r in caplog.records
    )
