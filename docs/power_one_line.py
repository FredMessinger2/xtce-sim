"""One-line electrical diagram of the imaging_sat reference EPS.

Generates power-one-line.svg. Requires schemdraw (not a project
dependency — install it anywhere and rerun this script when the diagram
changes).
"""

import schemdraw
from schemdraw import elements as elm
from schemdraw import flow

d = schemdraw.Drawing(unit=2.0)

BUS_X = 14.5
BUS_TOP = 8.0
BUS_BOT = -8.0

# ---- generation chains: array -> SADA slip ring -> MPPT -> bus --------------
for y, wing in ((6.5, "+Y"), (2.5, "-Y")):
    sol = d.add(
        elm.Solar()
        .at((0, y))
        .right()
        .label(f"{wing} wing\n60 W BOL\nVmp 28 V / Imp 2.1 A", loc="top", fontsize=9)
    )
    ln = d.add(
        elm.Line().at(sol.end).right(1.6).label("2.1 A max\n@ 28 V", loc="top", fontsize=8)
    )
    sada = d.add(flow.Box(w=2.4, h=1.2).at(ln.end).anchor("W").label("SADA\nslip ring", fontsize=9))
    ln2 = d.add(elm.Line().at(sada.E).right(1.2))
    mppt = d.add(flow.Box(w=2.4, h=1.2).at(ln2.end).anchor("W").label("MPPT\nbuck", fontsize=9))
    d.add(
        elm.Line()
        .at(mppt.E)
        .right()
        .tox(BUS_X)
        .label("2.4 A max\n@ 24 V", loc="top", fontsize=8)
    )
    d.add(elm.Dot().at((BUS_X, y)))

# ---- the 24 V bus (vertical bar) --------------------------------------------
d.add(elm.Line().at((BUS_X, BUS_TOP)).down().toy(BUS_BOT).linewidth(3.5))
d.add(elm.Label().at((BUS_X, BUS_TOP + 0.4)).label("24 V battery bus", fontsize=11))

# ---- battery via charge/discharge control -----------------------------------
d.add(elm.Dot().at((BUS_X, -7.0)))
lnb = d.add(elm.Line().at((BUS_X, -7.0)).left().tox(9.8))
bcr = d.add(
    flow.Box(w=2.8, h=1.2).at(lnb.end).anchor("E").label("charge /\ndischarge ctl", fontsize=9)
)
lnb2 = d.add(
    elm.Line()
    .at(bcr.W)
    .left(1.9)
    .label("chg 2 A max\ndis 6 A max", loc="top", fontsize=8)
)
d.add(
    elm.Battery()
    .at(lnb2.end)
    .left()
    .label("Li-ion, 6 cells in series\n22.2 V nominal", loc="bottom", fontsize=9)
)

# ---- switched loads: LCL (switch + fuse) -> load box ------------------------
LOADS = [
    (7.0, "CDH (OBC + GPS)\n0.3 A"),
    (4.5, "ADCS\n0.5 A / 2.5 A slew"),
    (2.0, "COMMS\n0.1 A RX / 1.9 A TX"),
    (-0.5, "IMAGER\n0.2 A / 1.0 A"),
    (-3.0, "HEATERS\n0.4-0.8 A duty"),
    (-5.5, "SADA motors x2\n0.04-0.4 A"),
]
for y, label in LOADS:
    d.add(elm.Dot().at((BUS_X, y)))
    sw = d.add(elm.Switch().at((BUS_X, y)).right().label("LCL", loc="top", fontsize=8))
    fu = d.add(elm.Fuse().at(sw.end).right(1.6))
    d.add(flow.Box(w=3.6, h=1.15).at(fu.end).anchor("W").label(label, fontsize=9))

d.save("power-one-line.svg")
print("wrote power-one-line.svg")
