#!/usr/bin/env bash
# Set every command-settable telemetry field on the ImagingSat sim to a
# distinctive value, one send at a time, so you can watch each change land
# on the monitor or the web console.
#
# Run from the repository root, with the sim already serving:
#
#   uv run xtce-sim run examples/imaging_sat/imaging_sat.xml --port 5000 --interval 1
#   ./examples/imaging_sat/set_all_fields.sh
#
# Overridable knobs (environment variables):
#   PORT=5000   sim TCP port
#   PAUSE=1     seconds between sends (0 for full speed)
#   DEF=...     definition file (default: the imaging_sat XTCE)
#
# A failed send aborts the script immediately (no sim, wrong directory).
#
# What this reaches: every field the behavior sidecar wires to a command.
# Fields driven by [_signals] (panel temps, battery) move on their own, and
# fields with no behavior yet (rates, currents, momentum, ATS/RTS...) stay
# put until the dynamics arc lands.

set -u
DEF=${DEF:-examples/imaging_sat/imaging_sat.xml}
PORT=${PORT:-5000}
PAUSE=${PAUSE:-1}

send() {
  uv run xtce-sim send --def "$DEF" --port "$PORT" "$@" || exit 1
  sleep "$PAUSE"
}

echo "== system: HK_SYSTEM_MODE =="
send SET_MODE Mode=IMAGING

echo "== thermal: heater states cycle OFF->ON, setpoints (temps ramp toward setpoints) =="
send HEATER_OFF HeaterId=1
send HEATER_ON HeaterId=1
send HEATER_ON HeaterId=2
send SET_HEATER_SETPOINT HeaterId=1 Setpoint=32
send SET_HEATER_SETPOINT HeaterId=2 Setpoint=27

echo "== imager: state cycles OFF->IDLE->CAPTURING, exposure, gain, count, events =="
send IMAGER_OFF
send IMAGER_ON
send SET_EXPOSURE ExposureMs=250 GainLevel=2
send TAKE_IMAGE ImageCount=7

echo "== adcs: quaternion (90 deg about Z), euler angles, wheel speeds, MTQ =="
send ADCS_SLEW_TO_QUATERNION Q1=0 Q2=0 Q3=0.7071 Q4=0.7071
send ADCS_SLEW_TO_ANGLES Roll=10.5 Pitch=-15.25 Yaw=45.0
send ADCS_WHEEL_SET_SPEED WheelId=1 Speed=1500
send ADCS_WHEEL_SET_SPEED WheelId=2 Speed=-2200
send ADCS_WHEEL_SET_SPEED WheelId=3 Speed=3300
send ADCS_WHEEL_SET_SPEED WheelId=4 Speed=-4100
send ADCS_MTQ_ENABLE State=OFF

echo "== adcs: mode — TRACK_TARGET flips it, then NADIR by direct command =="
send ADCS_TRACK_TARGET Latitude=34.05 Longitude=-118.24
send ADCS_SET_MODE Mode=NADIR

echo "done — every command-settable field has been set."
