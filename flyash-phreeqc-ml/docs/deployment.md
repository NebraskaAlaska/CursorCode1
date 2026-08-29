# Deployment guide

WPI Virtual LAB has two explicit bundles. `docker-compose.local.yml` binds Streamlit to loopback and keeps AI disabled by default. `docker-compose.server.yml` publishes only an authenticated TLS reverse proxy, keeps the app and optional Ollama service internal, and applies resource limits. The legacy `docker-compose.yml` mirrors the local bundle.

See [local installation](local_installation.md) or [hosted deployment](hosted_deployment.md). Five writable roots are durable volumes: `/app/outputs`, `/app/experiments`, `/app/data/processed`, `/app/imports`, and `/var/lib/wpi/resources`. Raw/local records, downloaded databases, models, credentials, and TLS material are excluded from every image target.

PHREEQC is fixed to USGS `3.8.6-17100`. Its archive SHA-256 is `b5c4a6dfea1a6bb6a3436857a50346bb943904a49582714494b4f1b1e54e64e1`; bundled `phreeqc.dat` is `59373961d648dfbf68a40744060c1d64f57ecbec98f4f5fb89f3a1b4213ccd10`. The database is `/opt/phreeqc/database/phreeqc.dat`; official examples are retained under `/opt/phreeqc/share/examples/`. The deterministic source manifest and full USGS rights notice are under `release/`.

PHREEQC output remains simulation under reviewed assumptions, never experimental validation. AI is optional and advisory, and execution/save gates remain explicit.
