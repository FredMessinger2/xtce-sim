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


def test_warns_on_unsupported_string_charset(tmp_path, caplog):
    # The codec always encodes UTF-8; a declared UTF-16 would be silently
    # mis-encoded, so the parser must call it out. UTF-8 (or omitted, which
    # defaults to UTF-8) stays quiet.
    f = _write(
        tmp_path,
        "str.xml",
        f'<xtce:SpaceSystem {NS} name="X"><xtce:TelemetryMetaData>'
        "<xtce:ParameterTypeSet>"
        '<xtce:StringParameterType name="Wide">'
        '<xtce:StringDataEncoding encoding="UTF-16">'
        "<xtce:SizeInBits><xtce:Fixed><xtce:FixedValue>64</xtce:FixedValue>"
        "</xtce:Fixed></xtce:SizeInBits>"
        "</xtce:StringDataEncoding></xtce:StringParameterType>"
        '<xtce:StringParameterType name="Narrow">'
        '<xtce:StringDataEncoding encoding="UTF-8">'
        "<xtce:SizeInBits><xtce:Fixed><xtce:FixedValue>64</xtce:FixedValue>"
        "</xtce:Fixed></xtce:SizeInBits>"
        "</xtce:StringDataEncoding></xtce:StringParameterType>"
        "</xtce:ParameterTypeSet>"
        "</xtce:TelemetryMetaData></xtce:SpaceSystem>",
    )
    with caplog.at_level(logging.WARNING):
        XTCEParser().parse(f)
    charset_warnings = [r for r in caplog.records if "encodes UTF-8 only" in r.message]
    assert len(charset_warnings) == 1  # Wide flagged, Narrow silent
    assert "'Wide'" in charset_warnings[0].getMessage()
    assert "UTF-16" in charset_warnings[0].getMessage()


def test_charset_warning_survives_parse_multiple(tmp_path, caplog):
    # parse_multiple suppresses resolution warnings during its per-file parses
    # (self._warn False), but a bad charset can't become valid after merging —
    # the warning must still surface for split command/telemetry files.
    tlm = _write(
        tmp_path,
        "tlm.xml",
        f'<xtce:SpaceSystem {NS} name="X"><xtce:TelemetryMetaData>'
        "<xtce:ParameterTypeSet>"
        '<xtce:StringParameterType name="Wide">'
        '<xtce:StringDataEncoding encoding="UTF-16">'
        "<xtce:SizeInBits><xtce:Fixed><xtce:FixedValue>64</xtce:FixedValue>"
        "</xtce:Fixed></xtce:SizeInBits>"
        "</xtce:StringDataEncoding></xtce:StringParameterType>"
        "</xtce:ParameterTypeSet>"
        "</xtce:TelemetryMetaData></xtce:SpaceSystem>",
    )
    cmd = _write(
        tmp_path,
        "cmd.xml",
        f'<xtce:SpaceSystem {NS} name="X"><xtce:CommandMetaData><xtce:MetaCommandSet>'
        '<xtce:MetaCommand name="NOOP"/>'
        "</xtce:MetaCommandSet></xtce:CommandMetaData></xtce:SpaceSystem>",
    )
    with caplog.at_level(logging.WARNING):
        XTCEParser().parse_multiple([cmd, tlm])
    assert any("encodes UTF-8 only" in r.message for r in caplog.records)


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
    # Merging a file with itself is idempotent (no override, nothing lost).
    assert XTCEParser().parse_multiple([base, base]).parameter_types["T"].size_in_bits == 8


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


def test_warns_on_illegal_consequence_level(tmp_path, caplog):
    # 'caution' looks plausible but is not in XTCE 1.2's ConsequenceLevelType
    # (normal/vital/critical/forbidden/user1, ISO 14950) — we once shipped it
    # ourselves. Parse keeps the value but must say something.
    f = _write(
        tmp_path,
        "sig.xml",
        f'<xtce:SpaceSystem {NS} name="X"><xtce:CommandMetaData><xtce:MetaCommandSet>'
        '<xtce:MetaCommand name="C">'
        '<xtce:DefaultSignificance consequenceLevel="caution" reasonForWarning="w"/>'
        "</xtce:MetaCommand>"
        "</xtce:MetaCommandSet></xtce:CommandMetaData></xtce:SpaceSystem>",
    )
    with caplog.at_level(logging.WARNING):
        defn = XTCEParser().parse(f)
    assert defn.meta_commands["C"].significance == "caution"  # kept as-is
    assert any("not a legal XTCE value" in r.message for r in caplog.records)


def _rate_container(rate_attrs: str) -> str:
    """A minimal containerized DefaultRateInStream declaration."""
    return (
        f'<xtce:SpaceSystem {NS} name="X"><xtce:TelemetryMetaData>'
        "<xtce:ContainerSet>"
        '<xtce:SequenceContainer name="PKT">'
        f"<xtce:DefaultRateInStream {rate_attrs}/>"
        "<xtce:EntryList/>"
        "</xtce:SequenceContainer>"
        "</xtce:ContainerSet></xtce:TelemetryMetaData></xtce:SpaceSystem>"
    )


def test_rate_in_stream_happy_path(tmp_path):
    f = _write(tmp_path, "rate.xml", _rate_container('basis="perSecond" minimumValue="0.5"'))
    defn = XTCEParser().parse(f)
    assert defn.containers["PKT"].rate_per_second == 0.5


def test_rate_in_stream_unsupported_basis_warns(tmp_path, caplog):
    f = _write(tmp_path, "rate.xml", _rate_container('basis="perContainerUpdate" minimumValue="2"'))
    with caplog.at_level(logging.WARNING):
        defn = XTCEParser().parse(f)
    assert defn.containers["PKT"].rate_per_second is None
    assert any("basis" in r.message and "rate ignored" in r.message for r in caplog.records)


def test_rate_in_stream_no_value_warns(tmp_path, caplog):
    f = _write(tmp_path, "rate.xml", _rate_container('basis="perSecond"'))
    with caplog.at_level(logging.WARNING):
        defn = XTCEParser().parse(f)
    assert defn.containers["PKT"].rate_per_second is None
    assert any("no positive finite rate" in r.message for r in caplog.records)


def test_rate_in_stream_non_numeric_warns(tmp_path, caplog):
    f = _write(tmp_path, "rate.xml", _rate_container('minimumValue="fast"'))
    with caplog.at_level(logging.WARNING):
        defn = XTCEParser().parse(f)
    assert defn.containers["PKT"].rate_per_second is None
    assert any("is not a number" in r.message for r in caplog.records)


def test_rate_in_stream_zero_minimum_falls_back_to_maximum(tmp_path):
    # minimumValue="0" is the standard's 'no guaranteed rate' — the declared
    # maximumValue is the honest best-effort rate, not a rejection.
    f = _write(tmp_path, "rate.xml", _rate_container('minimumValue="0" maximumValue="4"'))
    defn = XTCEParser().parse(f)
    assert defn.containers["PKT"].rate_per_second == 4.0
