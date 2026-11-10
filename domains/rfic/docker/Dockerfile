# rfic:1 — BlahajSphere RFIC toolchain.
# DECISIONS (M0, in-container check): hpretl/iic-osic-tools:2026.10 already ships ngspice, xschem,
# klayout, openEMS, qucs-s, jupyter-lab, scikit-rf, numpy, matplotlib and the sky130A, gf180mcuD,
# ihp-sg13g2, ihp-sg13cmos5l PDKs, so nothing is installed here. The sanity RUN below makes the
# build fail loudly if a future base tag drops one of them.
ARG BASE_TAG=2026.10
FROM hpretl/iic-osic-tools:${BASE_TAG}

LABEL org.blahajsphere.image="rfic:1" \
      org.blahajsphere.mount="/work/nodes"

# Silence the "[INFO] Final PATH variable" banner that the image's login-shell setup prints,
# so `bash -lc <cmd>` output is clean for MCP tools and job logs.
ENV IIC_OSIC_TOOLS_QUIET=1

RUN bash -lc 'set -e; \
      for t in ngspice xschem klayout openEMS qucs-s jupyter-lab; do command -v "$t" >/dev/null || { echo "missing tool: $t"; exit 1; }; done; \
      for p in sky130A ihp-sg13g2; do test -d "$PDK_ROOT/$p" || { echo "missing PDK: $p"; exit 1; }; done; \
      python3 -c "import skrf, numpy, matplotlib, CSXCAD, openEMS"'

# Nodes are bind-mounted here (SPEC §4.2); per-node commands use workdir /work/nodes/<dir>/workspace.
WORKDIR /work/nodes
