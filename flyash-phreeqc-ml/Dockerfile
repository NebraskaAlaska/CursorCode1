# Materials Research Assistant — server-side Streamlit app with PHREEQC built in.
#
# This image builds the USGS PHREEQC batch CLI from source so leaching/geochemistry
# simulations run ON THE SERVER. Colleagues need only a browser — no local PHREEQC, no key.
#
# SECRETS ARE NEVER BAKED IN: ANTHROPIC_API_KEY is injected at RUNTIME (platform secret /
# `-e` / `--env-file`), and the Dockerfile sets no key. See docs/deployment.md.
#
# Single-stage on purpose (reliability over image size): the PHREEQC binary runs in the exact
# environment it was built in, so there is no risk of a stripped runtime missing a shared lib.
# docs/deployment.md describes an optional multi-stage slim build once you can test it.
FROM python:3.12-slim

# PHREEQC source version (the project's verified version). If USGS bumps it, find the current
# Linux source tarball at https://www.usgs.gov/software/phreeqc-version-3 and override with
# `--build-arg PHREEQC_VERSION=...` (or a full `--build-arg PHREEQC_URL=...`).
ARG PHREEQC_VERSION=3.8.6-17100
ARG PHREEQC_URL=https://water.usgs.gov/water-resources/software/PHREEQC/phreeqc-${PHREEQC_VERSION}.tar.gz

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# --- build PHREEQC from source (USGS batch CLI: `phreeqc <in> <out> <db>`) -------------------
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        build-essential gfortran curl ca-certificates libgomp1; \
    cd /tmp; \
    echo "Downloading PHREEQC source: ${PHREEQC_URL}"; \
    # -f: fail (non-zero) + show the HTTP status on a 4xx/5xx instead of saving an error page;
    # -S: show errors; -L: follow redirects; --retry: ride out transient hiccups.
    curl -fSL --retry 3 --retry-delay 2 -o phreeqc.tar.gz "${PHREEQC_URL}"; \
    # Guard: a soft "200 OK" error page (or truncated file) is not a gzip tarball — fail clearly.
    if ! tar tzf phreeqc.tar.gz >/dev/null 2>&1; then \
        echo "ERROR: ${PHREEQC_URL} did not return a valid PHREEQC source tarball."; \
        echo "       Find the current Linux source at https://www.usgs.gov/software/phreeqc-version-3"; \
        echo "       and rebuild with --build-arg PHREEQC_URL=<url> (or --build-arg PHREEQC_VERSION=<ver>)."; \
        exit 1; \
    fi; \
    tar xzf phreeqc.tar.gz; \
    cd phreeqc-*/; \
    ./configure --prefix=/usr/local; \
    make -j"$(nproc)"; \
    make install; \
    # collect the default (redistributable) databases at a stable path
    mkdir -p /opt/phreeqc/database; \
    cp "$(find / -name phreeqc.dat -type f 2>/dev/null | head -n1)" /opt/phreeqc/database/phreeqc.dat; \
    for d in wateq4f.dat pitzer.dat llnl.dat minteq.v4.dat sit.dat Amm.dat; do \
        f="$(find / -name "$d" -type f 2>/dev/null | head -n1)"; \
        if [ -n "$f" ]; then cp "$f" /opt/phreeqc/database/ || true; fi; \
    done; \
    cd /; rm -rf /tmp/phreeqc* /var/lib/apt/lists/*

# --- PHREEQC runtime configuration (paths are NOT secrets) ----------------------------------
# CEMDATA18 is NOT redistributable, so the image ships the open phreeqc.dat. For cementitious /
# fly-ash accuracy, mount CEMDATA18 and override PHREEQC_DATABASE (see docs/deployment.md).
ENV PHREEQC_EXE=/usr/local/bin/phreeqc \
    PHREEQC_DATABASE=/opt/phreeqc/database/phreeqc.dat \
    PHREEQC_TIMEOUT_S=120 \
    APP_IN_DOCKER=1
# APP_IN_DOCKER lets the app phrase availability honestly: inside the container PHREEQC should be
# ready (so a failure is a deployment issue to fix), vs. a local Streamlit run with no PHREEQC.

# --- self-test: fail the BUILD loudly if PHREEQC can't actually run --------------------------
RUN set -eux; \
    printf 'SOLUTION 1\n    pH 7.0\n    temp 25.0\nEND\n' > /tmp/selftest.pqi; \
    "$PHREEQC_EXE" /tmp/selftest.pqi /tmp/selftest.pqo "$PHREEQC_DATABASE"; \
    test -s /tmp/selftest.pqo; \
    rm -f /tmp/selftest.pqi /tmp/selftest.pqo

# --- Python dependencies (cached layer) ------------------------------------------------------
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# --- application source (honours .dockerignore: no secrets, no raw research data) -----------
COPY . /app
RUN chmod +x /app/docker-entrypoint.sh

# --- run as a non-root user; it may only write to gitignored run dirs under /app ------------
RUN useradd -m -u 10001 appuser && chown -R appuser:appuser /app
USER appuser

# --- Streamlit serving -----------------------------------------------------------------------
# PORT is honoured by the entrypoint (Render/Railway/Cloud Run inject it); defaults to 8501.
ENV PORT=8501
EXPOSE 8501
HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=3 \
    CMD python -c "import os,urllib.request as u; u.urlopen('http://127.0.0.1:%s/_stcore/health' % os.environ.get('PORT','8501'))" || exit 1
ENTRYPOINT ["/app/docker-entrypoint.sh"]
