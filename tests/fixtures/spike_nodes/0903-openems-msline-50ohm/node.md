---
id: 903
slug: openems-msline-50ohm
title: "Size a 50-ohm microstrip line and extract its S-parameters with openEMS"
parents: []
kind: challenge
domain: transmission_lines
subskills: [transmission_lines.microstrip, sparams.two_port]
tier: 2
size_est_min: 45
sim_runtime_s: 25
image: rfic:1
pdk: none
run: ["python3 run_sim.py", "python3 analyze.py"]
proof_artifacts: [run_sim.py, analyze.py, msline.s2p, out.txt]
vocab: ["microstrip", "characteristic impedance", "Z0", "effective permittivity", "quasi-TEM", "S11", "S21", "return loss", "reference plane", "electrical length", "group delay", "ABCD matrix", "Smith chart", "FDTD", "guided wavelength"]
status: prelab
provisional: false
created: 2026-11-10T10:07:06-06:00
references: []
---

# Size a 50-ohm microstrip line and extract its S-parameters with openEMS

## Goal
Your first full-wave EM simulation. Choose the strip width of a microstrip line on a given
substrate so that it is a 50 ohm line, simulate it with openEMS (an FDTD solver), and then
extract the line's characteristic impedance and effective permittivity from the simulated
two-port S-parameters.

## Given
- Substrate: lossless, relative permittivity 4.4, thickness 1.6 mm. Ground plane underneath.
- Strip: zero-thickness perfect conductor on top of the substrate (thickness neglected).
- Line length between the two reference planes: 24 mm (fixed in the script, do not change it).
- Both ports are microstrip ports; S-parameters are referenced to 50 ohm.
- Analysis band: 1 to 5 GHz.

## Files
- `run_sim.py`: builds the structure, runs openEMS, and writes `msline.s2p`.
  The strip width `STRIP_W` is the only design variable.
- `analyze.py`: reads `msline.s2p` and writes `out.txt` (Z0, effective permittivity, delay).
  It contains `TODO` markers: the starter extraction is naive and must be fixed.

## What the checker enforces
The checker re-runs both scripts, then extracts the quantities from `msline.s2p` with its own
independent code.

| Check | Spec |
|---|---|
| Line impedance Z0 (mean over 1-2.5 GHz) | 47.5 to 52.5 ohm |
| Worst return loss, S11 over 1-5 GHz | <= -25 dB |
| Effective permittivity (mean over 1-5 GHz) | 3.15 to 3.55 |
| Worst-case S21 over 1-5 GHz | >= -0.5 dB |
| `analyze.py` Z0 vs the checker's extraction | within 1.5 ohm |
| `analyze.py` effective permittivity vs the checker's | within 3 % |

## How to run
```
python3 run_sim.py      # ~30 s: FDTD simulation, writes msline.s2p
python3 analyze.py      # < 1 s: writes out.txt
```
(The checker runs both for you.) Inspect `msline.s2p` with scikit-rf or your own plots while you work.

## Done means
All checks above pass: the line is a good 50 ohm line over the band **and** your
`analyze.py` recovers the same Z0 and effective permittivity as the independent extraction.
Answer the prelab before you simulate.
