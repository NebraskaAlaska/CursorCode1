# Local installation

Install Docker Desktop or Docker Engine with Compose v2 and allocate at least 4 GB RAM. From `flyash-phreeqc-ml`:

```bash
./scripts/launch-local.sh --build
./scripts/check-local-install.sh
```

On macOS use `scripts/launch-local.command`; on Windows use `scripts/launch-local.ps1 -Build` and `scripts/check-local-install.ps1`. Open `http://127.0.0.1:8501`. Set `WPI_PORT` to change only the loopback port; use the authenticated server bundle for a shared host.

The build downloads the fixed HTTPS PHREEQC archive and rejects archive, database, or notice mismatch. `check-local-install` validates Compose, health, the database digest, and official example `ex1`. The example proves runtime operability only—not WPI experimental validation.

The local and compatibility Compose app services set `init: true`. Keep that setting: the init
process reaps terminated PHREEQC descendants adopted by container PID 1. If you run the test image
directly, use `docker run --rm --init ...`; a raw container without `--init` does not satisfy the
release process-tree contract.

Stop while retaining named volumes with `scripts/stop-local.sh`. Back up all five durable roots with `scripts/backup-local.sh /private/path`; restore explicitly with `scripts/restore-local.sh --yes ARCHIVE`. PowerShell equivalents use `backup-local.ps1 -Destination PATH` and `restore-local.ps1 -Archive FILE -Yes`. Archives can contain research records and are unencrypted.

AI defaults to disabled. Configure only a reviewed provider path. Never place a cloud credential in a build argument, repository file, screenshot, log, or release manifest.

Developers using Python directly must use Python 3.12:

```bash
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -c constraints-py312.txt -r requirements-dev.txt
python scripts/validate_dependency_lock.py --installed
python -m pytest -q -p no:cacheprovider
```
