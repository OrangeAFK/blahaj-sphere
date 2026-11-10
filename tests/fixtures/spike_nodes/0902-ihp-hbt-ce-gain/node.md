---
id: 902
slug: ihp-hbt-ce-gain
title: "Bias an SG13G2 npn13G2 common-emitter stage and predict its small-signal gain"
parents: []
kind: challenge
domain: amplifiers
subskills: [devices.bjt_bias, amplifiers.small_signal_gain]
tier: 2
size_est_min: 40
sim_runtime_s: 1
image: rfic:1
pdk: ihp-sg13g2
run: ["ngspice -b ce_amp.sp"]
proof_artifacts: [ce_amp.sp, out.txt]
vocab: ["Ic", "Vbe", "gm", "beta", "bias point", "voltage divider", "emitter degeneration", "bypass capacitor", "small-signal gain", "Vce headroom", "HBT", "thermal voltage"]
status: prelab
provisional: false
created: "2026-11-10T10:03:03-06:00"
references: []
---

# Bias an SG13G2 npn13G2 common-emitter stage and predict its small-signal gain

## Goal
Set the DC collector current of a SiGe HBT common-emitter amplifier with a resistive base divider,
then check your hand predictions of Ic, gm and mid-band voltage gain against an ngspice DC operating
point and AC sweep.

## Given conditions
- Device: `npn13G2` from IHP SG13G2, emitter count `Nx=8`, typical HBT corner. Substrate tied to ground.
- Supply VCC = 1.5 V (the HBT's collector-emitter breakdown is low, so keep Vce modest).
- Collector resistor Rc = 450 ohm, emitter resistor Re = 250 ohm, bypassed by a large capacitor.
- Input and output are AC-coupled (1 uF); the AC load is RL = 1 kohm; the source is an ideal voltage source.
- Target bias: **Ic = 1 mA**.
- Mid-band gain is read at **10 MHz**.

## Specs enforced by the checker
| Test | Spec | Unit |
|---|---|---|
| Collector current Ic | between 0.85 and 1.15 | mA |
| Vce headroom | >= 0.4 | V |
| Mid-band \|Av\| at 10 MHz | >= 7.5 | V/V |

## What you edit
Only `workspace/ce_amp.sp`: the two divider resistors `R1v` (top) and `R2v` (bottom). Everything else is fixed.
The starter simulates fine but fails the specs. Do the prelab first, pick your first R1/R2 from hand
calculation, then simulate and refine.

## How to run
From the work directory: `ngspice -b ce_amp.sp`. This writes `out.txt` (DC operating point and mid-band
gain) and `ac.txt` (gain vs frequency, for plotting). The checker re-runs the same command.

## Done means
Checker passes all three tests, and you can explain where your hand predictions of Ic, gm and gain agree
or disagree with the simulation, and why.
