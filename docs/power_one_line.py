"""One-line electrical diagram of the imaging_sat reference EPS, with the
logical (XTCE telemetry/command) layer overlaid in color on the physical
circuit.

Color legend (also drawn on the diagram):
  black  — physical element, no telemetry in the ICD
  blue   — XTCE parameter, telemetered AND driven by the sim today
  orange — XTCE parameter, declared in the ICD but not yet driven

Generates power-one-line.svg. Requires schemdraw (not a project
dependency — install it anywhere and rerun this script when the diagram
changes).
"""

import schemdraw
from schemdraw import elements as elm
from schemdraw import flow

DRIVEN = "#1f77b4"  # blue: telemetered and driven today
UNDRIVEN = "#e8801a"  # orange: declared in the XTCE, not yet driven

d = schemdraw.Drawing(unit=2.0)

BUS_X = 14.5
BUS_TOP = 8.5
BUS_BOT = -8.0

# ---- generation chains: array -> SADA slip ring -> MPPT -> bus --------------
for y, wing in ((6.5, "+Y"), (2.5, "-Y")):
    sol = d.add(
        elm.Solar()
        .at((0, y))
        .right()
        .label(f"{wing} wing\n60 W BOL\nVmp 28 V / Imp 2.1 A", loc="top", fontsize=9)
    )
    ln = d.add(elm.Line().at(sol.end).right(1.6))
    sada = d.add(flow.Box(w=2.4, h=1.2).at(ln.end).anchor("W").label("SADA\nslip ring", fontsize=9))
    ln2 = d.add(elm.Line().at(sada.E).right(1.2))
    mppt = d.add(flow.Box(w=2.4, h=1.2).at(ln2.end).anchor("W").label("MPPT\nbuck", fontsize=9))
    lnout = d.add(elm.Line().at(mppt.E).right().tox(BUS_X))
    d.add(elm.Dot().at((BUS_X, y)))
    if wing == "+Y":
        # The ICD carries ONE solar sense pair; drawn once, on the +Y chain.
        d.add(
            elm.CurrentLabelInline(direction="in", ofst=0.9)
            .at(lnout)
            .color(UNDRIVEN)
            .label("PWR_SOLAR_CURRENT", fontsize=8, color=UNDRIVEN)
        )
        vstub = d.add(elm.Line().at(ln2.center).up(0.9).color(DRIVEN).linestyle("--"))
        d.add(
            elm.MeterV()
            .at(vstub.end)
            .up(1.4)
            .color(DRIVEN)
            .label("PWR_SOLAR_VOLTAGE", loc="top", fontsize=8, color=DRIVEN)
        )

# ---- the 24 V bus (vertical bar) --------------------------------------------
d.add(elm.Line().at((BUS_X, BUS_TOP)).down().toy(BUS_BOT).linewidth(3.5))
d.add(elm.Label().at((BUS_X + 2.4, BUS_TOP + 0.4)).label("24 V battery bus", fontsize=11))

# bus voltage sense (tapped to the left, clear of the CDH branch labels)
vb = d.add(elm.Line().at((BUS_X, 8.2)).left(1.0).color(DRIVEN).linestyle("--"))
d.add(
    elm.MeterV()
    .at(vb.end)
    .left(1.4)
    .color(DRIVEN)
    .label("PWR_BATTERY_VOLTAGE", loc="top", fontsize=8, color=DRIVEN)
)

# ---- battery via charge/discharge control -----------------------------------
d.add(elm.Dot().at((BUS_X, -7.0)))
lnb = d.add(elm.Line().at((BUS_X, -7.0)).left().tox(9.8))
bcr = d.add(
    flow.Box(w=2.8, h=1.2).at(lnb.end).anchor("E").label("charge /\ndischarge ctl", fontsize=9)
)
d.add(
    elm.CurrentLabelInline(direction="in", ofst=0.9)
    .at(lnb)
    .color(UNDRIVEN)
    .label("PWR_BATTERY_CURRENT (+/-)", fontsize=8, color=UNDRIVEN)
)
lnb2 = d.add(
    elm.Line()
    .at(bcr.W)
    .left(1.9)
    .label("chg 2 A max\ndis 6 A max", loc="bottom", fontsize=8)
)
bat = d.add(
    elm.Battery()
    .at(lnb2.end)
    .left()
    .label("Li-ion, 6 cells in series\n22.2 V nominal", loc="bottom", fontsize=9)
)
d.add(
    elm.Label()
    .at((bat.center[0], -8.5))
    .label("PWR_BATTERY_TEMP", fontsize=8, color=DRIVEN)
)

# ---- switched loads: LCL (switch + fuse) -> load box ------------------------
# Each LCL is commanded by SET_POWER and read back as a PWR_*_STATE enum.
LOADS = [
    (7.0, "CDH (OBC + GPS)\n0.3 A", "PWR_CDH_STATE", DRIVEN),
    (4.5, "ADCS\n0.5 A / 2.5 A slew", "PWR_ADCS_STATE", DRIVEN),
    (2.0, "COMMS\n0.1 A RX / 1.9 A TX", "PWR_COMMS_STATE", DRIVEN),
    (-0.5, "IMAGER\n0.2 A / 1.0 A", "PWR_IMAGER_STATE", DRIVEN),
    (-3.0, "HEATERS\n0.4-0.8 A duty", "PWR_HEATER_STATE", DRIVEN),
    (-5.5, "SADA motors x2\n0.04-0.4 A", None, None),
]
for y, label, param, color in LOADS:
    d.add(elm.Dot().at((BUS_X, y)))
    sw = d.add(elm.Switch().at((BUS_X, y)).right().label("LCL", loc="top", fontsize=8))
    if param:
        d.add(
            elm.Label()
            .at((BUS_X + 1.05, y + 0.75))
            .label(param, fontsize=8, color=color)
        )
    fu = d.add(elm.Fuse().at(sw.end).right(1.6))
    d.add(flow.Box(w=3.6, h=1.15).at(fu.end).anchor("W").label(label, fontsize=9))

# command-path note on the switch column
d.add(
    elm.Label()
    .at((BUS_X + 1.6, BUS_BOT - 0.4))
    .label(
        "switches commanded by SET_POWER\n(heater channel: HEATER_ON/OFF/AUTO)",
        fontsize=8,
        color=DRIVEN,
    )
)

# ---- legend -----------------------------------------------------------------
lx, ly = 0.0, -3.2
d.add(flow.Box(w=6.4, h=2.6).at((lx + 3.2, ly - 1.3)).anchor("center").linewidth(1))
d.add(elm.Label().at((lx + 0.3, ly - 0.5)).label("black: physical element, no telemetry", fontsize=8, halign="left"))
d.add(elm.Label().at((lx + 0.3, ly - 1.2)).label("blue: XTCE parameter, driven by the sim today", fontsize=8, color=DRIVEN, halign="left"))
d.add(elm.Label().at((lx + 0.3, ly - 1.9)).label("orange: declared in the XTCE, not yet driven", fontsize=8, color=UNDRIVEN, halign="left"))

d.save("power-one-line.svg")
print("wrote power-one-line.svg")
