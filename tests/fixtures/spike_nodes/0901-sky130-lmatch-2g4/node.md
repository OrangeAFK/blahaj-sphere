---
id: 901
slug: sky130-lmatch-2g4
title: "Extract a sky130 NMOS gate load and hand-design a 2.4 GHz L-match to 50 ohm"
parents: []
kind: challenge
domain: matching
subskills: [matching.l_network, smith_chart.navigation]
tier: 2
size_est_min: 40
sim_runtime_s: 25
image: rfic:1
pdk: sky130
run: ["ngspice -b match.sp"]
proof_artifacts: [match.sp, load.txt]
vocab: ["L-match", "S11", "reflection coefficient", "Smith chart", "loaded Q", "series-to-parallel transform", "gate capacitance", "Cgg", "constant-resistance circle", "constant-conductance circle", "return loss", "impedance matching", "shunt capacitor", "series inductor"]
status: prelab
provisional: false
created: 2026-11-10T09:57:29-06:00
references: []
---

# Extract a sky130 NMOS gate load and hand-design a 2.4 GHz L-match to 50 ohm

## Goal
A wide sky130 NMOS is the input of a 2.4 GHz block. Its gate is a poor match to a 50 ohm source.
First **measure** what impedance the device presents, then **design by hand** an ideal two-element
L-network (one series element, one shunt element) that matches it to 50 ohm.

## Given conditions
- Frequency: 2.4 GHz. Source: 50 ohm. Reference impedance for all reflection coefficients: 50 ohm.
- Device: `sky130_fd_pr__nfet_01v8`, total W = 200 um (40 fingers), L = 0.15 um, typical corner (`tt`).
- Gate bias 0.7 V (supplied through the matching network from the source DC level), drain at 1.8 V
  (AC ground), source and bulk grounded.
- A 10 ohm resistor is in series with the gate (poly / layout resistance).
- Network topology is fixed: **series element on the device side, shunt element on the 50 ohm port side**.
  Components are ideal (lossless) values you choose: inductor `Lm` (nH) and capacitor `Cm` (pF).

## Files
- `match.sp` — the netlist. It contains two copies of the device:
  copy A is the bare load, probed by a 1 A AC source so that `V(ga)` equals the load impedance;
  copy B sits behind the matching network. Edit the parameters `Lm` and `Cm` only.
- `load.txt` — fill in the series-equivalent load at 2.4 GHz, R + jX, that you read from the simulation.
- `out.txt` (produced) — columns: frequency, Re/Im of the load impedance, Re/Im of the impedance
  the 50 ohm source sees looking into the network.

## How to run
`ngspice -b match.sp` (cwd = your work dir; about 25 s, the PDK models take most of that).
Then compute S11 from the `zin` columns of `out.txt` yourself (plot it, or find where it is minimal).

## Specs enforced by the checker
| Test | Spec |
|---|---|
| Simulation runs | exit code 0 |
| Extracted load resistance in `load.txt` vs simulation | within 10 % |
| Extracted load reactance in `load.txt` vs simulation | within 10 % |
| S11 at 2.4 GHz | <= -15 dB |
| Frequency of the S11 minimum (1-4 GHz sweep) | between 2.3 and 2.5 GHz |

## Done means
`load.txt` matches the simulated load, the network values in `match.sp` meet every spec above, and
you can show the path from the load point to the centre of the Smith chart, element by element.
