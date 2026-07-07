"""Behavior-sidecar loader/validation tests (xtce_sim.behavior + inspect wiring).

Validation is strict and total: every reference is checked against the
resolved SimDefinition and all problems are reported in one BehaviorError.
"""

from pathlib import Path

import pytest
from click.testing import CliRunner

from xtce_sim import behavior
from xtce_sim.behavior import (
    BehaviorError,
    CopyArgEffect,
    IncrementEffect,
    RampEffect,
    SetEffect,
    load_behavior,
    sidecar_path,
)
from xtce_sim.cli import main
from xtce_sim.definition import SimDefinition

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
IMAGING = EXAMPLES / "imaging_sat.xml"


@pytest.fixture(scope="module")
def simdef() -> SimDefinition:
    return SimDefinition.from_xtce(IMAGING)


def _load(tmp_path, simdef, text: str):
    p = tmp_path / "t.behavior.toml"
    p.write_text(text)
    return load_behavior(p, simdef)


def _errors(tmp_path, simdef, text: str) -> str:
    with pytest.raises(BehaviorError) as exc:
        _load(tmp_path, simdef, text)
    return str(exc.value)


# ---- the shipped sidecar is the canonical happy path -----------------------


def test_shipped_imaging_sidecar_validates(simdef):
    spec = load_behavior(EXAMPLES / "imaging_sat.behavior.toml", simdef)
    assert len(spec.initial) == 5
    assert set(spec.commands) == {
        "SET_MODE", "HEATER_ON", "HEATER_OFF", "SET_HEATER_SETPOINT",
        "IMAGER_ON", "IMAGER_OFF", "SET_EXPOSURE", "TAKE_IMAGE", "SET_ATTITUDE",
    }
    ramp = next(
        e for e in spec.commands["HEATER_ON"] if isinstance(e, RampEffect)
    )
    assert ramp.target == "@THM_HEATER{HeaterId}_SETPOINT" and ramp.tau == 30.0


def test_sidecar_discovery():
    assert sidecar_path([IMAGING]) == EXAMPLES / "imaging_sat.behavior.toml"
    assert sidecar_path([EXAMPLES / "my_vehicle.xml"]) is None  # none exists


# ---- effect parsing ---------------------------------------------------------


def test_effect_kinds_parse(tmp_path, simdef):
    spec = _load(
        tmp_path,
        simdef,
        """
        [IMAGER_ON]
        IMG_STATE = 1
        IMG_CAPTURE_COUNT = { increment = 1 }
        IMG_FOCAL_PLANE_TEMP = { ramp_to = 35.0, tau = 20, emit = "immediate" }
        [SET_EXPOSURE]
        IMG_EXPOSURE_MS = "@arg:ExposureMs"
        """,
    )
    kinds = {type(e) for e in spec.commands["IMAGER_ON"]}
    assert kinds == {SetEffect, IncrementEffect, RampEffect}
    ramp = next(e for e in spec.commands["IMAGER_ON"] if isinstance(e, RampEffect))
    assert ramp.emit == "immediate"
    assert isinstance(spec.commands["SET_EXPOSURE"][0], CopyArgEffect)


def test_enum_label_set_is_valid(tmp_path, simdef):
    spec = _load(tmp_path, simdef, '[SET_MODE]\nHK_SYSTEM_MODE = "IMAGING"\n')
    assert spec.commands["SET_MODE"][0].value == "IMAGING"


# ---- validation errors ------------------------------------------------------


def test_unknown_command_table(tmp_path, simdef):
    assert "unknown command" in _errors(tmp_path, simdef, "[NO_SUCH_CMD]\nIMG_STATE = 1\n")


def test_unknown_field(tmp_path, simdef):
    assert "unknown telemetry field" in _errors(
        tmp_path, simdef, "[IMAGER_ON]\nNOT_A_FIELD = 1\n"
    )


def test_unknown_arg_reference(tmp_path, simdef):
    msg = _errors(tmp_path, simdef, '[IMAGER_ON]\nIMG_STATE = "@arg:Nope"\n')
    assert "no argument 'Nope'" in msg


def test_template_arg_must_exist(tmp_path, simdef):
    msg = _errors(tmp_path, simdef, '[IMAGER_ON]\n"IMG_{Unit}_STATE" = 1\n')
    assert "template argument {Unit}" in msg


def test_template_expansion_catches_missing_field(tmp_path, simdef):
    # HeaterId expands to 1..2; THM_HEATER1_BOGUS / THM_HEATER2_BOGUS don't exist.
    msg = _errors(tmp_path, simdef, '[HEATER_ON]\n"THM_HEATER{HeaterId}_BOGUS" = 1\n')
    assert "THM_HEATER1_BOGUS" in msg and "THM_HEATER2_BOGUS" in msg


def test_bad_enum_label(tmp_path, simdef):
    msg = _errors(tmp_path, simdef, '[SET_MODE]\nHK_SYSTEM_MODE = "WARP_SPEED"\n')
    assert "not a label" in msg and "SAFE" in msg


def test_string_value_for_numeric_field(tmp_path, simdef):
    assert "string value for numeric field" in _errors(
        tmp_path, simdef, '[IMAGER_ON]\nIMG_EXPOSURE_MS = "fast"\n'
    )


def test_ramp_requires_tau_and_numeric_target(tmp_path, simdef):
    assert "requires tau" in _errors(
        tmp_path, simdef, "[IMAGER_ON]\nIMG_FOCAL_PLANE_TEMP = { ramp_to = 35.0 }\n"
    )
    assert "tau must be a positive number" in _errors(
        tmp_path, simdef,
        "[IMAGER_ON]\nIMG_FOCAL_PLANE_TEMP = { ramp_to = 35.0, tau = -1 }\n",
    )
    assert "@FIELD reference" in _errors(
        tmp_path, simdef,
        '[IMAGER_ON]\nIMG_FOCAL_PLANE_TEMP = { ramp_to = "warm", tau = 5 }\n',
    )


def test_unknown_verb_key(tmp_path, simdef):
    msg = _errors(
        tmp_path, simdef,
        "[IMAGER_ON]\nIMG_FOCAL_PLANE_TEMP = { ramp_too = 35.0, tau = 5 }\n",
    )
    assert "unknown key(s) ['ramp_too']" in msg


def test_bad_emit_value(tmp_path, simdef):
    assert "emit must be one of" in _errors(
        tmp_path, simdef, '[IMAGER_ON]\nIMG_STATE = { set = 1, emit = "now" }\n'
    )


def test_initial_unknown_field_and_bad_label(tmp_path, simdef):
    msg = _errors(
        tmp_path, simdef,
        '[_initial]\nNOT_A_FIELD = 1\nHK_SYSTEM_MODE = "WARP_SPEED"\n',
    )
    assert "unknown telemetry field" in msg and "not a label" in msg


def test_all_errors_collected_in_one_raise(tmp_path, simdef):
    msg = _errors(
        tmp_path, simdef,
        "[NO_SUCH_CMD]\nX = 1\n[IMAGER_ON]\nNOT_A_FIELD = 1\nIMG_STATE = { bogus = 1 }\n",
    )
    assert "3 problem(s)" in msg


def test_invalid_toml_is_behavior_error(tmp_path, simdef):
    p = tmp_path / "bad.behavior.toml"
    p.write_text("[NOT CLOSED\n")
    with pytest.raises(BehaviorError, match="not valid TOML"):
        load_behavior(p, simdef)


# ---- review-driven cases ----------------------------------------------------


def test_templated_field_values_are_still_validated(tmp_path, simdef):
    # Value checks must expand templates just like existence checks do —
    # a bad label on a templated field is not allowed to slip through, and
    # EVERY expansion is checked (both heaters here).
    msg = _errors(
        tmp_path, simdef, '[HEATER_ON]\n"THM_HEATER{HeaterId}_STATE" = "BANANA"\n'
    )
    assert "not a label of THM_HEATER1_STATE" in msg
    assert "not a label of THM_HEATER2_STATE" in msg


def test_sidecar_path_keeps_dotted_stems(tmp_path):
    # v1.2.xml must map to v1.2.behavior.toml, not v1.behavior.toml.
    xtce = tmp_path / "v1.2.xml"
    xtce.write_text("<x/>")
    good = tmp_path / "v1.2.behavior.toml"
    good.write_text("")
    assert sidecar_path([xtce]) == good


def test_table_form_set_accepts_arg_copy_with_emit(tmp_path, simdef):
    # '@arg:' means copy in the table form too — that's how a copy gets
    # emit="immediate" — and it must not parse as a literal string set.
    spec = _load(
        tmp_path, simdef,
        '[SET_EXPOSURE]\nIMG_EXPOSURE_MS = { set = "@arg:ExposureMs", emit = "immediate" }\n',
    )
    eff = spec.commands["SET_EXPOSURE"][0]
    assert isinstance(eff, CopyArgEffect) and eff.emit == "immediate"


def test_stray_tau_rejected_outside_ramp(tmp_path, simdef):
    assert "tau is only valid with ramp_to" in _errors(
        tmp_path, simdef, "[IMAGER_ON]\nIMG_STATE = { set = 1, tau = 5 }\n"
    )


def test_boolean_values_rejected(tmp_path, simdef):
    assert "boolean values are ambiguous" in _errors(
        tmp_path, simdef, "[IMAGER_ON]\nIMG_STATE = true\n"
    )


def test_raw_int_for_enum_field_must_be_a_real_value(tmp_path, simdef):
    # 3 is IMAGING — fine as a raw escape; 99 maps to nothing and is a typo.
    spec = _load(tmp_path, simdef, "[SET_MODE]\nHK_SYSTEM_MODE = 3\n")
    assert spec.commands["SET_MODE"][0].value == 3
    assert "not a raw value" in _errors(
        tmp_path, simdef, "[SET_MODE]\nHK_SYSTEM_MODE = 99\n"
    )


def test_bare_at_field_set_value_gets_a_hint(tmp_path, simdef):
    msg = _errors(
        tmp_path, simdef, '[IMAGER_ON]\nIMG_STATE = "@IMG_EXPOSURE_MS"\n'
    )
    assert 'did you mean "@arg:' in msg


def test_initial_rejects_templates_with_hint(tmp_path, simdef):
    assert "templates are not allowed here" in _errors(
        tmp_path, simdef, '[_initial]\n"THM_HEATER{HeaterId}_TEMP" = 20.0\n'
    )


# ---- inspect wiring ---------------------------------------------------------


def test_inspect_narrates_behavior_sidecar():
    result = CliRunner().invoke(main, ["inspect", str(IMAGING)])
    assert result.exit_code == 0, result.output
    assert "Behavior (" in result.output
    assert "THM_HEATER{HeaterId}_TEMP ramps to" in result.output
    assert "IMG_EXPOSURE_MS = @arg:ExposureMs" in result.output


def test_inspect_behavior_validation_failure_is_fatal(tmp_path):
    bad = tmp_path / "bad.behavior.toml"
    bad.write_text("[NO_SUCH_CMD]\nX = 1\n")
    result = CliRunner().invoke(
        main, ["inspect", str(IMAGING), "--behavior", str(bad)]
    )
    assert result.exit_code != 0
    assert "unknown command" in result.output


def test_inspect_without_sidecar_unchanged():
    result = CliRunner().invoke(main, ["inspect", str(EXAMPLES / "my_vehicle.xml")])
    assert result.exit_code == 0, result.output
    assert "Behavior (" not in result.output


def test_describe_lines(simdef):
    spec = load_behavior(EXAMPLES / "imaging_sat.behavior.toml", simdef)
    lines = behavior.describe(spec)
    assert any(line.startswith("HEATER_ON:") for line in lines)
    assert any("tau=30.0s" in line for line in lines)
