FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# The agora/ submodule must be initialized on the host before building:
#   git submodule update --init --recursive
# The build will fail fast if it's missing — see the RUN check below.

COPY pyproject.toml README.md LICENSE ./
COPY sim/ sim/
COPY agora/ agora/

RUN test -d agora/cms_client || ( \
        echo "ERROR: agora/cms_client/ missing — did you run 'git submodule update --init'?" >&2; \
        exit 1 \
    )

RUN pip install --no-cache-dir .

# Defaults the control plane to 0.0.0.0 so other containers in the same
# docker network can inject faults. Override with --control-host if binding
# to loopback is needed.
ENV AGORA_SIM_CMS_URL="" \
    AGORA_SIM_COUNT=3 \
    AGORA_SIM_BOARD=pi_5 \
    AGORA_SIM_SERIAL_PREFIX=sim \
    AGORA_SIM_CONTROL_HOST=0.0.0.0 \
    AGORA_SIM_CONTROL_PORT=9090

EXPOSE 9090

# Entrypoint reads env vars so compose can configure via `environment:`.
# Fails fast if AGORA_SIM_CMS_URL is empty.
ENTRYPOINT ["/bin/sh", "-c", "\
    test -n \"$AGORA_SIM_CMS_URL\" || { echo 'AGORA_SIM_CMS_URL required' >&2; exit 1; }; \
    exec python -m sim \
        --cms-url \"$AGORA_SIM_CMS_URL\" \
        --count \"$AGORA_SIM_COUNT\" \
        --board \"$AGORA_SIM_BOARD\" \
        --serial-prefix \"$AGORA_SIM_SERIAL_PREFIX\" \
        --control-host \"$AGORA_SIM_CONTROL_HOST\" \
        --control-port \"$AGORA_SIM_CONTROL_PORT\" \
"]
