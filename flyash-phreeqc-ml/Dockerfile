# syntax=docker/dockerfile:1.7

# WPI Virtual LAB release candidate.
#
# The PHREEQC source URL, archive digest, phreeqc.dat digest, and full USGS
# User Rights Notice are fixed. Changing any one is a scientific-resource
# update that must be proposed, reviewed, and tested separately.

FROM debian:bookworm-slim@sha256:88200866dfff7ea7f5cbcb6ec7c8a701889efe6fe859fe64d6990e4b07ea4171 AS phreeqc-builder

ENV DEBIAN_FRONTEND=noninteractive \
    PHREEQC_VERSION=3.8.6-17100 \
    PHREEQC_SOURCE_URL=https://water.usgs.gov/water-resources/software/PHREEQC/phreeqc-3.8.6-17100.tar.gz \
    PHREEQC_SOURCE_SHA256=b5c4a6dfea1a6bb6a3436857a50346bb943904a49582714494b4f1b1e54e64e1 \
    PHREEQC_DATABASE_SHA256=59373961d648dfbf68a40744060c1d64f57ecbec98f4f5fb89f3a1b4213ccd10 \
    PHREEQC_NOTICE_SHA256=f5c97c1c0fea8c27096d3e7b298a627264a71683dc2debb374d07baaea168b34

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       build-essential ca-certificates curl gfortran \
    && rm -rf /var/lib/apt/lists/*

COPY release/PHREEQC_USGS_RIGHTS_NOTICE.txt /tmp/expected-PHREEQC-NOTICE.txt

RUN set -eu; \
    mkdir -p \
      /build \
      /out/bin \
      /out/database \
      /out/share/doc/phreeqc \
      /out/share/examples \
      /out/share/manifest; \
    cd /build; \
    curl --fail --show-error --silent --proto '=https' --tlsv1.2 \
      --retry 3 --retry-delay 2 --output phreeqc.tar.gz "$PHREEQC_SOURCE_URL"; \
    echo "$PHREEQC_SOURCE_SHA256  phreeqc.tar.gz" | sha256sum -c -; \
    tar -tzf phreeqc.tar.gz | awk \
      'BEGIN { bad=0 } /^\// || /(^|\/)\.\.($|\/)/ { bad=1 } END { exit bad }'; \
    if tar -tvzf phreeqc.tar.gz | awk \
      'substr($1,1,1)=="l" || substr($1,1,1)=="h" { bad=1 } END { exit bad }'; then :; \
    else echo 'PHREEQC archive contains a link entry; refusing extraction' >&2; exit 1; fi; \
    tar -xzf phreeqc.tar.gz; \
    cd "phreeqc-$PHREEQC_VERSION"; \
    echo "$PHREEQC_DATABASE_SHA256  database/phreeqc.dat" | sha256sum -c -; \
    echo "$PHREEQC_NOTICE_SHA256  doc/NOTICE" | sha256sum -c -; \
    cmp -s doc/NOTICE /tmp/expected-PHREEQC-NOTICE.txt; \
    ./configure --prefix=/opt/phreeqc; \
    make -j"$(getconf _NPROCESSORS_ONLN)"; \
    make check; \
    install -m 0755 src/phreeqc /out/bin/phreeqc; \
    find database -maxdepth 1 -type f -name '*.dat' -exec install -m 0644 {} /out/database/ \;; \
    find examples -maxdepth 1 -type f -exec install -m 0644 {} /out/share/examples/ \;; \
    install -m 0644 doc/NOTICE /out/share/doc/phreeqc/USGS_USER_RIGHTS_NOTICE.txt; \
    executable_sha=$(sha256sum /out/bin/phreeqc | awk '{print $1}'); \
    architecture=$(dpkg --print-architecture); \
    printf '%s\n' \
      '{' \
      '  "manifest_schema": "wpi.virtual-lab.scientific-resource",' \
      '  "manifest_version": 1,' \
      '  "resource_id": "usgs.phreeqc.runtime",' \
      "  \"installation_id\": \"usgs.phreeqc.runtime.3.8.6-17100.${executable_sha%${executable_sha#????????????????}}\"," \
      '  "resource_kind": "phreeqc_runtime",' \
      '  "display_name": "USGS PHREEQC 3.8.6-17100 Linux container runtime",' \
      '  "provider": "U.S. Geological Survey",' \
      '  "official_source_url": "https://water.usgs.gov/water-resources/software/PHREEQC/phreeqc-3.8.6-17100.tar.gz",' \
      '  "discovered_version": "3.8.6-17100",' \
      '  "installed_version": "3.8.6-17100",' \
      '  "active_version": "3.8.6-17100",' \
      '  "archive_filename": "phreeqc-3.8.6-17100.tar.gz",' \
      '  "source_sha256": "b5c4a6dfea1a6bb6a3436857a50346bb943904a49582714494b4f1b1e54e64e1",' \
      "  \"executable_sha256\": \"$executable_sha\"," \
      "  \"content_sha256\": \"$executable_sha\"," \
      '  "install_path": "/opt/phreeqc/bin/phreeqc",' \
      "  \"architectures\": [\"$architecture\"]," \
      '  "operating_system_targets": ["linux"],' \
      '  "build_toolchain": ["Debian bookworm GNU toolchain"],' \
      '  "citation": {"title": "USGS PHREEQC 3", "authors": ["U.S. Geological Survey"], "url": "https://water.usgs.gov/water-resources/software/PHREEQC/", "source_location": "doc/NOTICE", "extraction_confidence": 1.0},' \
      '  "rights_notice": "Exact upstream notice: /opt/phreeqc/share/doc/phreeqc/USGS_USER_RIGHTS_NOTICE.txt",' \
      '  "redistribution_state": "permitted",' \
      '  "test_status": "passed",' \
      '  "standalone_test_status": "passed",' \
      '  "rollback_state": "candidate",' \
      '  "warnings": ["Simulation runtime only; output is not experimental validation."]' \
      '}' > /out/share/manifest/runtime-manifest.json; \
    /out/bin/phreeqc \
      /out/share/examples/ex1 \
      /tmp/ex1.pqo \
      /out/database/phreeqc.dat >/tmp/ex1.screen 2>&1; \
    test -s /tmp/ex1.pqo

FROM python:3.12.10-slim-bookworm@sha256:fd95fa221297a88e1cf49c55ec1828edd7c5a428187e67b5d1805692d11588db AS python-deps

ARG TARGETARCH

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=300 \
    PIP_RETRIES=10 \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH

RUN python -m venv /opt/venv
COPY requirements.txt constraints-py312.txt /tmp/dependencies/
RUN --mount=type=cache,id=wpi-pip-runtime-${TARGETARCH},target=/root/.cache/pip \
    set -eu; \
    attempt=1; \
    while [ "$attempt" -le 3 ]; do \
      if python -m pip install \
        --constraint /tmp/dependencies/constraints-py312.txt \
        --requirement /tmp/dependencies/requirements.txt; then \
        break; \
      fi; \
      if [ "$attempt" -eq 3 ]; then \
        echo "Verified runtime dependency installation failed after $attempt attempts" >&2; \
        exit 1; \
      fi; \
      python -m pip cache purge >/dev/null 2>&1 || :; \
      attempt=$((attempt + 1)); \
    done

FROM python:3.12.10-slim-bookworm@sha256:fd95fa221297a88e1cf49c55ec1828edd7c5a428187e67b5d1805692d11588db AS runtime

ARG APP_VERSION=0.1.0-phase4-rc
ARG VCS_REF=uncommitted

LABEL org.opencontainers.image.title="WPI Virtual LAB" \
      org.opencontainers.image.version="$APP_VERSION" \
      org.opencontainers.image.revision="$VCS_REF" \
      org.opencontainers.image.vendor="WPI Virtual LAB research prototype" \
      org.opencontainers.image.licenses="USGS-PHREEQC-User-Rights-Notice" \
      org.opencontainers.image.description="Private-beta materials research app with verified PHREEQC runtime"

ENV APP_VERSION=$APP_VERSION \
    APP_VCS_REF=$VCS_REF \
    APP_IN_DOCKER=1 \
    DEBIAN_FRONTEND=noninteractive \
    HOME=/home/appuser \
    MPLCONFIGDIR=/tmp/matplotlib \
    PATH=/opt/venv/bin:/opt/phreeqc/bin:$PATH \
    PHREEQC_DATABASE=/opt/phreeqc/database/phreeqc.dat \
    PHREEQC_DATABASE_ID=usgs.phreeqc.database.phreeqc \
    PHREEQC_DATABASE_MANIFEST_ID=usgs.phreeqc.database.phreeqc \
    PHREEQC_DATABASE_SHA256=59373961d648dfbf68a40744060c1d64f57ecbec98f4f5fb89f3a1b4213ccd10 \
    PHREEQC_DATABASE_VERSION=3.8.6-17100 \
    PHREEQC_EXE=/opt/phreeqc/bin/phreeqc \
    PHREEQC_OFFICIAL_EXAMPLE=/opt/phreeqc/share/examples/ex1 \
    PHREEQC_RUNTIME_ID=usgs.phreeqc.runtime \
    PHREEQC_RUNTIME_MANIFEST_ID=usgs.phreeqc.runtime-manifest-v1 \
    PHREEQC_RUNTIME_MANIFEST=/opt/phreeqc/share/manifest/runtime-manifest.json \
    PHREEQC_SOURCE_ID=usgs-phreeqc-3.8.6-17100-source \
    PHREEQC_SOURCE_MANIFEST=/app/release/PHREEQC_SOURCE_MANIFEST.json \
    PHREEQC_SOURCE_MANIFEST_SHA256=a779c7ab1192221cf3f8bc0339976f62ba5aeaf67945674efe6e85e8b2a5b22b \
    PHREEQC_TIMEOUT_S=120 \
    PHREEQC_VERSION=3.8.6-17100 \
    PORT=8501 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false \
    VLAB_AI_PROVIDER=disabled \
    VLAB_DEPLOYMENT_MODE=local \
    VLAB_IMPORT_ROOT=/app/imports \
    WPI_RESOURCE_ROOT=/var/lib/wpi/resources

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 appuser \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin appuser

COPY --from=python-deps /opt/venv /opt/venv
COPY --from=phreeqc-builder /out /opt/phreeqc

WORKDIR /app
COPY --chown=root:root app.py app_ui.py README.md pyproject.toml ./
COPY --chown=root:root .streamlit ./.streamlit
COPY --chown=root:root flyash_phreeqc_ml ./flyash_phreeqc_ml
COPY --chown=root:root ui ./ui
COPY --chown=root:root scripts ./scripts
COPY --chown=root:root release ./release
COPY --chown=root:root resources ./resources
COPY --chown=root:root release/templates/SYNTHETIC_DEMO_icp_template.csv \
    /opt/wpi/templates/SYNTHETIC_DEMO_icp_template.csv
COPY --chown=root:root docker-entrypoint.sh /usr/local/bin/wpi-entrypoint

RUN chmod 0555 /usr/local/bin/wpi-entrypoint \
    && find /app -type d -exec chmod 0555 {} + \
    && find /app -type f -exec chmod 0444 {} + \
    && mkdir -p \
       /app/data/processed \
       /app/imports \
       /app/imports/experimental_icp \
       /app/experiments \
       /app/outputs \
       /var/lib/wpi/resources \
    && cp /opt/wpi/templates/SYNTHETIC_DEMO_icp_template.csv \
       /app/imports/experimental_icp/experimental_release_template.csv \
    && chown -R 10001:10001 \
       /app/data/processed \
       /app/imports \
       /app/imports/experimental_icp \
       /app/experiments \
       /app/outputs \
       /var/lib/wpi/resources \
    && chmod 0750 \
       /app/data/processed \
       /app/imports/experimental_icp \
       /app/experiments \
       /app/outputs \
       /var/lib/wpi/resources

USER 10001:10001
EXPOSE 8501

HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=3 \
    CMD python -c "import os,urllib.request as u; u.urlopen('http://127.0.0.1:%s/_stcore/health' % os.environ.get('PORT','8501'), timeout=3).read()" || exit 1

ENTRYPOINT ["/usr/local/bin/wpi-entrypoint"]

FROM runtime AS test

ARG TARGETARCH

USER root
COPY requirements-dev.txt /tmp/dependencies/requirements-dev.txt
COPY constraints-py312.txt requirements.txt /tmp/dependencies/
RUN --mount=type=cache,id=wpi-pip-dev-${TARGETARCH},target=/root/.cache/pip \
    set -eu; \
    attempt=1; \
    while [ "$attempt" -le 3 ]; do \
      if python -m pip install \
        --constraint /tmp/dependencies/constraints-py312.txt \
        --requirement /tmp/dependencies/requirements-dev.txt; then \
        break; \
      fi; \
      if [ "$attempt" -eq 3 ]; then \
        echo "Verified development dependency installation failed after $attempt attempts" >&2; \
        exit 1; \
      fi; \
      python -m pip cache purge >/dev/null 2>&1 || :; \
      attempt=$((attempt + 1)); \
    done
COPY --chown=root:root conftest.py ./conftest.py
COPY --chown=root:root tests ./tests
COPY --chown=root:root docs ./docs
COPY --chown=root:root experiments/README.md ./experiments/README.md
COPY --chown=root:root Dockerfile .dockerignore .gitignore docker-entrypoint.sh ./
COPY --chown=root:root docker-compose.yml docker-compose.local.yml docker-compose.server.yml ./
COPY --chown=root:root requirements.txt requirements-dev.txt constraints-py312.txt ./
COPY --chown=root:root deploy ./deploy
USER 10001:10001

FROM runtime AS release
