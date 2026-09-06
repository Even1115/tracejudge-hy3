# LiveCodeBench official-checker execution image (tracejudge-lcb-checker).
#
# Build context MUST be the pinned official checker checkout
# (artifacts/external/livecodebench-checker @ 28fef95ea8c9f7a547c8329f2cd3d32b92c1fa24),
# whose four pinned files are hash-verified by run_smoke_check before build:
#
#   docker build --platform linux/amd64 \
#     -f docker/livecodebench/Dockerfile \
#     -t tracejudge-lcb-checker \
#     artifacts/external/livecodebench-checker
#
# After building, pin the resulting image digest
# (docker image inspect --format='{{.Id}}' tracejudge-lcb-checker)
# wherever LCBDockerRunner(image=...) is configured.

# This exact local/registry image is linux/amd64 (Python 3.11.10).
# The previous python:3.11-slim digest selected arm64, despite --platform.
FROM ganler/evalplus@sha256:26b118098bef281fe8dfe999bf05f1d5b45374b4e6c00161ec0f30592aef4740

# numpy pin comes from the pinned checker commit's own uv.lock (numpy 2.2.5);
# the official stdio branch of testing_util.py needs nothing else third-party.
RUN pip install --no-cache-dir numpy==2.2.5

# Pinned official checker sources, baked at build time so runtime containers
# never mount the checkout.  The container-side entrypoint is staged by the
# host runner via the read-only /control bind (same pattern as EvalPlus).
COPY lcb_runner/ /opt/lcb/lcb_runner/
ENV PYTHONPATH=/opt/lcb
