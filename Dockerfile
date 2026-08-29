# Proof that the Paymos Linux package works end-to-end: build the wheel natively on
# Linux, install ONLY that wheel into a clean python:3.11-slim (no Rust, no source),
# prove the native FROST core loads + runs on Linux, then serve the clickable demo
# (pointed at prod). If `docker build` gets past the self-test RUN, the Linux package
# is proven: the native `paymos._core` compiled + imported + executed on Linux.
#
#   docker build -f sdk/python/Dockerfile -t paymos-demo .        # from the repo root
#   docker run --rm -p 8000:8000 paymos-demo                      # open http://localhost:8000
#
# The runtime image never sees Rust or the source — only `pip install <wheel>`.

# ---------- build stage: compile the abi3 wheel natively on Linux ----------
FROM python:3.11 AS build
# maturin (the PyO3 build backend) + the Rust toolchain. python:3.11 (bookworm) already
# carries curl + a C toolchain, so rustup installs cleanly.
RUN pip install --no-cache-dir maturin
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal
ENV PATH="/root/.cargo/bin:${PATH}"

# Mirror the repo layout maturin expects: pyproject ([tool.maturin] manifest-path =
# ../rust/paymos_core/Cargo.toml) → the crate → its wallet-mpc path dep (../../../mpc/wallet-mpc).
WORKDIR /io
COPY mpc/wallet-mpc            /io/mpc/wallet-mpc
COPY sdk/rust                 /io/sdk/rust
COPY sdk/python/pyproject.toml /io/sdk/python/pyproject.toml
COPY sdk/python/README.md      /io/sdk/python/README.md
COPY sdk/python/paymos         /io/sdk/python/paymos

WORKDIR /io/sdk/python
# Build against the stable ABI (Cargo.toml enables abi3-py311) → a cp311-abi3 Linux wheel
# that this and any Python 3.11+ can install. Output to /io/dist.
RUN maturin build --release --out /io/dist && ls -la /io/dist

# ---------- runtime stage: clean slim, install ONLY the wheel ----------
FROM python:3.11-slim AS runtime
WORKDIR /app

# Install the SDK purely from the wheel — no Rust, no source tree. fastapi/uvicorn are the
# demo's own extras (not SDK deps).
COPY --from=build /io/dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl fastapi uvicorn && rm -f /tmp/*.whl

# THE PROOF: import the pure package AND execute the native FROST core on Linux, from the
# installed wheel. dkg_part1 exercises the compiled Rust `_core`; a non-ok result fails the build.
RUN python -c "import json; from paymos import Wallet, _core; \
r = json.loads(_core.mpc_call(json.dumps({'op':'dkg_part1','id':1,'max':2,'min':2}))); \
assert r.get('ok'), r; \
print('LINUX PROOF: paymos installed from wheel; native _core ran on Linux ->', sorted(r)[:4])"

# The clickable demo (backend + page). The SDK is already installed from the wheel above.
COPY sdk/python/examples /app/examples
EXPOSE 8000
ENV PAYMOS_BASE_URL=https://wallet.paymos.io
# host 0.0.0.0 so the mapped port is reachable from the host.
CMD ["uvicorn", "examples.app:app", "--host", "0.0.0.0", "--port", "8000"]
