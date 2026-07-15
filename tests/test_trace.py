"""Parse-trace tests: the `inspect` verb and -v/-vv parser introspection.

The trace narrates what the parser sees and what it infers. Decisions and
inferences (lines marked ``~``) log at INFO; the per-element firehose logs at
DEBUG. Normal runs stay silent because nothing attaches a handler or lowers
the ``xtce_sim`` logger level until ``enable_trace`` is called.
"""

import logging
from pathlib import Path

import pytest
from click.testing import CliRunner

from xtce_sim.cli import main
from xtce_sim.generate import build_sim_definition
from xtce_sim.parser import XTCEParser

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
DATA = Path(__file__).resolve().parent / "data"
FIXTURE = Path(__file__).resolve().parent / "data" / "full_features.xml"
NS = 'xmlns:xtce="http://www.omg.org/spec/XTCE/20250214"'


@pytest.fixture(autouse=True)
def _reset_trace_logger():
    # enable_trace attaches a handler to the "xtce_sim" logger; undo it so a
    # traced test can't leave a stale handler pointing at a closed CliRunner
    # stream for later tests.
    yield
    trace_logger = logging.getLogger("xtce_sim")
    trace_logger.handlers.clear()
    trace_logger.setLevel(logging.NOTSET)


# ---- instrumentation (caplog, no CLI) ---------------------------------------


def test_trace_reports_inferences_on_fixture(caplog):
    with caplog.at_level(logging.INFO, logger="xtce_sim"):
        defn = XTCEParser().parse(FIXTURE)
        build_sim_definition(defn)
    messages = [r.getMessage() for r in caplog.records]
    # enum size inferred from max enumeration value (no IntegerDataEncoding)
    assert any("inferred 16 bits from max enumeration value 300" in m for m in messages)
    # binary size taken from the legacy sizeInBits attribute
    assert any("legacy sizeInBits attribute" in m for m in messages)
    # synthetic opcode assignment surfaces as an inference
    assert any("no opcode in the XTCE — synthetic" in m for m in messages)
    # inheritance resolution summary
    assert any("resolved inheritance" in m for m in messages)
    # abstract commands / abstract containers accounted for
    assert any("abstract command(s) excluded" in m for m in messages)
    assert any("treated as abstract bases" in m for m in messages)


def test_trace_reports_boolean_default(tmp_path, caplog):
    f = tmp_path / "b.xml"
    f.write_text(
        f'<xtce:SpaceSystem {NS} name="S"><xtce:TelemetryMetaData>'
        "<xtce:ParameterTypeSet>"
        '<xtce:BooleanParameterType name="Bare"/>'
        "</xtce:ParameterTypeSet></xtce:TelemetryMetaData></xtce:SpaceSystem>"
    )
    with caplog.at_level(logging.INFO, logger="xtce_sim"):
        XTCEParser().parse(f)
    assert any(
        "Boolean 'Bare': no encoding declared — defaulted to 1 bit" in r.getMessage()
        for r in caplog.records
    )


def test_firehose_traces_every_element(caplog):
    with caplog.at_level(logging.DEBUG, logger="xtce_sim"):
        XTCEParser().parse(FIXTURE)
    messages = [r.getMessage() for r in caplog.records]
    assert any("IntegerParameterType 'TempType'" in m for m in messages)
    assert any("MetaCommand 'DO_THING'" in m for m in messages)
    assert any("SequenceContainer 'HEALTH'" in m for m in messages)
    assert any("Parameter 'Temp'" in m for m in messages)


def test_decisions_tier_omits_firehose(caplog):
    with caplog.at_level(logging.INFO, logger="xtce_sim"):
        XTCEParser().parse(FIXTURE)
    assert not any(
        "IntegerParameterType" in r.getMessage() for r in caplog.records
    )  # per-element lines are DEBUG only


# ---- unconsumed-element detection -------------------------------------------


def test_reports_ignored_elements_grouped(tmp_path, caplog):
    # Two SplineCalibrators (unsupported) -> one grouped INFO line with count,
    # nested-element total, and an example location.
    f = tmp_path / "spline.xml"
    f.write_text(
        f'<xtce:SpaceSystem {NS} name="S"><xtce:TelemetryMetaData>'
        "<xtce:ParameterTypeSet>"
        '<xtce:FloatParameterType name="V1"><xtce:FloatDataEncoding sizeInBits="32"/>'
        "<xtce:SplineCalibrator><xtce:SplinePoint raw='0' calibrated='0'/>"
        "</xtce:SplineCalibrator></xtce:FloatParameterType>"
        '<xtce:FloatParameterType name="V2"><xtce:FloatDataEncoding sizeInBits="32"/>'
        "<xtce:SplineCalibrator><xtce:SplinePoint raw='1' calibrated='2'/>"
        "</xtce:SplineCalibrator></xtce:FloatParameterType>"
        "</xtce:ParameterTypeSet></xtce:TelemetryMetaData></xtce:SpaceSystem>"
    )
    with caplog.at_level(logging.INFO, logger="xtce_sim"):
        XTCEParser().parse(f)
    # Match the report shape, not bare "ignored" — the tmp_path embeds this
    # test's own name, which contains the word "ignored".
    lines = [r.getMessage() for r in caplog.records if "~ ignored" in r.getMessage()]
    assert len(lines) == 1  # grouped, not one line per occurrence
    assert "2 <SplineCalibrator>" in lines[0]
    assert "+2 nested" in lines[0]  # the SplinePoints inside
    assert "FloatParameterType 'V1'" in lines[0]  # first occurrence in doc order


def test_doc_elements_report_at_debug_only(tmp_path, caplog):
    # A Header is real unconsumed content, but it's documentation — it must
    # not cry wolf at the decisions tier.
    f = tmp_path / "hdr.xml"
    f.write_text(
        f'<xtce:SpaceSystem {NS} name="S">'
        '<xtce:Header version="1.0"><xtce:AuthorSet><xtce:Author>x</xtce:Author>'
        "</xtce:AuthorSet></xtce:Header>"
        "<xtce:TelemetryMetaData><xtce:ParameterTypeSet>"
        '<xtce:IntegerParameterType name="T" signed="false">'
        '<xtce:IntegerDataEncoding sizeInBits="8"/></xtce:IntegerParameterType>'
        "</xtce:ParameterTypeSet></xtce:TelemetryMetaData></xtce:SpaceSystem>"
    )
    with caplog.at_level(logging.INFO, logger="xtce_sim"):
        XTCEParser().parse(f)
    assert not any("~ ignored" in r.getMessage() for r in caplog.records)
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="xtce_sim"):
        XTCEParser().parse(f)
    assert any(
        "<Header>" in r.getMessage() and r.levelno == logging.DEBUG
        for r in caplog.records
    )


def test_fully_consumed_file_reports_nothing(tmp_path, caplog):
    f = tmp_path / "clean.xml"
    f.write_text(
        f'<xtce:SpaceSystem {NS} name="S"><xtce:TelemetryMetaData>'
        "<xtce:ParameterTypeSet>"
        '<xtce:IntegerParameterType name="T" signed="false">'
        '<xtce:IntegerDataEncoding sizeInBits="8"/></xtce:IntegerParameterType>'
        "</xtce:ParameterTypeSet></xtce:TelemetryMetaData></xtce:SpaceSystem>"
    )
    with caplog.at_level(logging.DEBUG, logger="xtce_sim"):
        XTCEParser().parse(f)
    assert not any("~ ignored" in r.getMessage() for r in caplog.records)


def test_inspect_surfaces_unread_elements(tmp_path):
    # The parser must confess to elements it doesn't read. The bundled
    # examples no longer contain any (SplineCalibrator closed the first
    # referent, DefaultSignificance the second — each graduated to
    # supported), so a synthetic file carries the gap now: command
    # verification (VerifierSet) is genuinely unsupported.
    ns = 'xmlns:xtce="http://www.omg.org/spec/XTCE/20250214"'
    f = tmp_path / "unread.xml"
    f.write_text(
        f'<xtce:SpaceSystem {ns} name="X"><xtce:CommandMetaData>'
        "<xtce:MetaCommandSet>"
        '<xtce:MetaCommand name="GO">'
        "<xtce:VerifierSet>"
        '<xtce:CompleteVerifier name="done"/>'
        "</xtce:VerifierSet>"
        "</xtce:MetaCommand>"
        "</xtce:MetaCommandSet></xtce:CommandMetaData></xtce:SpaceSystem>"
    )
    result = CliRunner().invoke(main, ["inspect", str(f)])
    assert result.exit_code == 0, result.output
    assert "VerifierSet" in result.output and "not read by this parser" in result.output


def test_inspect_bundled_vehicles_have_no_unread_elements():
    # Milestone worth pinning: every element in the shipped example AND in
    # the fixture vehicle is actually read. If either gains an unsupported
    # element, this fails and the gap gets a decision instead of silence.
    for xml in (
        EXAMPLES / "imaging_sat/imaging_sat.xml",
        DATA / "my_vehicle/my_vehicle.xml",
    ):
        result = CliRunner().invoke(main, ["inspect", str(xml)])
        assert result.exit_code == 0, result.output
        assert "not read by this parser" not in result.output, result.output


def test_inspect_narrates_significance():
    result = CliRunner().invoke(main, ["inspect", str(EXAMPLES / "imaging_sat/imaging_sat.xml")])
    assert result.exit_code == 0, result.output
    assert "significance: 11 command(s) declare non-normal criticality" in result.output
    assert "2 vital, 9 critical" in result.output


# ---- CLI --------------------------------------------------------------------


def test_inspect_narrates_and_exits_zero():
    result = CliRunner().invoke(main, ["inspect", str(DATA / "my_vehicle/my_vehicle.xml")])
    assert result.exit_code == 0, result.output
    assert "parsing" in result.output
    assert "~ RAW_CMD: no opcode in the XTCE — synthetic" in result.output
    assert "OK: MyVehicle — 61 command(s), 18 packet(s)" in result.output


def test_inspect_dump_prints_full_inventory():
    result = CliRunner().invoke(
        main, ["inspect", "--dump", str(EXAMPLES / "imaging_sat/imaging_sat.xml")]
    )
    assert result.exit_code == 0, result.output
    # The same report generate writes to cmd_tlm.txt, on stdout:
    assert "COMMANDS" in result.output and "TELEMETRY" in result.output
    assert "0x10  SET_POWER" in result.output  # real opcode, full command list
    assert "APID 0x10  HOUSEKEEPING" in result.output
    # ...and the trace + summary are still there.
    assert "parsing" in result.output and "OK: ImagingSat" in result.output


def test_inspect_without_dump_stays_summary_only():
    result = CliRunner().invoke(main, ["inspect", str(EXAMPLES / "imaging_sat/imaging_sat.xml")])
    assert result.exit_code == 0, result.output
    assert "COMMANDS" not in result.output  # no inventory unless asked


def test_inspect_full_includes_firehose():
    result = CliRunner().invoke(main, ["inspect", "--full", str(FIXTURE)])
    assert result.exit_code == 0, result.output
    assert "IntegerParameterType 'TempType'" in result.output
    assert "MetaCommand 'DO_THING'" in result.output


def test_inspect_bad_file_is_clean_error(tmp_path):
    bad = tmp_path / "broken.xml"
    bad.write_text("<xtce:SpaceSystem")  # not well-formed
    result = CliRunner().invoke(main, ["inspect", str(bad)])
    assert result.exit_code != 0
    # The ParseError must surface as click's clean "Error: ..." line — an
    # uncaught exception would leave result.exception a non-SystemExit and
    # print no Error line (CliRunner swallows tracebacks, so asserting on
    # "Traceback" alone would be vacuous).
    assert "Error:" in result.output
    assert isinstance(result.exception, SystemExit)


def test_generate_verbose_traces_and_quiet_without(tmp_path):
    out = str(tmp_path / "gen")
    quiet = CliRunner().invoke(
        main, ["generate", str(DATA / "my_vehicle/my_vehicle.xml"), "--out", out]
    )
    assert quiet.exit_code == 0 and "parsing" not in quiet.output
    traced = CliRunner().invoke(
        main, ["generate", str(DATA / "my_vehicle/my_vehicle.xml"), "--out", out, "-v"]
    )
    assert traced.exit_code == 0 and "parsing" in traced.output
    assert "synthetic" in traced.output  # RAW_CMD inference reaches the console
