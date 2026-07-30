# Rung StorePriceCache Ingest — Operations Guide

This directory holds turnkey scheduler manifests for running
`scripts/ingest_store_prices.py` automatically. Three deployment
models are supported; pick the one that matches your infrastructure.

| Model | When to use | Files |
|---|---|---|
| **cron** | Single Linux host, traditional sysadmin setup | `cron/rung-ingest.cron` |
| **systemd timer** | Modern Linux host with systemd (Ubuntu ≥16, RHEL ≥7) | `systemd/rung-ingest.{service,timer}` |
| **Kubernetes CronJob** | Container-native cluster, multi-node | `k8s/rung-ingest-cronjob.yaml` + `k8s/rung-ingest-{secret,configmap}.yaml.example` |

The CLI script (`scripts/ingest_store_prices.py`) is the same
regardless of scheduler — every manifest just invokes it with the
right env vars and JSON config.

---

## Cadence

The default schedule is **twice daily at 03:15 and 15:15** plus a
**weekday 06:15 catch-up**. Cadence trade-offs:

| Cadence | Use when | API-quota burn | Stale-cache risk |
|---|---|---|---|
| every 6h | Daily flash deals matter, generous Kroger quota | ~4× baseline | near-zero |
| **twice daily** (default) | Most users — catches weekly ad rotation + same-day deltas | 1× | <12h |
| daily | Light usage; user accepts same-day drift | 0.5× | up to 24h |
| weekly | Bare minimum; for low-traffic deployments | ~0.15× | up to 7d |

Recommended cron expression: `15 3,15 * * *`
Recommended systemd OnCalendar: `*-*-* 03:15:00` + `*-*-* 15:15:00`
Recommended K8s CronJob schedule: `"15 3,15 * * *"`

All times are kept in UTC for the K8s manifest. Cron + systemd
inherit the host's local timezone; verify `date` on the host matches
your intent.

---

## Env-var contract

These variables are read by `scripts/ingest_store_prices.py` and
must be set by the scheduler. Documented here so an ops engineer can
deploy without reading code.

### Required

| Variable | Source | Example |
|---|---|---|
| `KROGER_CLIENT_ID` | developer.kroger.com | `rung-prod-abc123` |
| `KROGER_CLIENT_SECRET` | developer.kroger.com | (32-char secret) |
| `DATABASE_URL` | SQLAlchemy URL pointing at the production SQLite/Postgres | `sqlite:////var/lib/rung/finance.db` |

### Optional

| Variable | Default | Purpose |
|---|---|---|
| `INGEST_CONFIG` | `scripts/ingest_store_prices.config.example.json` | Path to the JSON config describing which stores/terms to fetch |
| `INGEST_LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `PYTHONUNBUFFERED` | unset | Set to `1` so journald / k8s logs see output line-by-line |

### How each scheduler reads the contract

* **cron**: `/etc/rung/ingest.env` is a shell-form file (`KEY=VALUE` per line). The cron line does `set -a; . /etc/rung/ingest.env; set +a;` before invoking the script.
* **systemd**: `EnvironmentFile=/etc/rung/ingest.env` in `rung-ingest.service`. Same file format.
* **Kubernetes**: Each variable is set via `env.valueFrom.secretKeyRef` on the CronJob container; the Secret is named `rung-ingest-secret` (see `k8s/rung-ingest-secret.yaml.example`).

---

## cron install

```bash
sudo mkdir -p /etc/rung /var/log/rung /opt/rung
sudo cp scripts/ingest_store_prices.py /opt/rung/scripts/
sudo cp scripts/ingest_store_prices.config.example.json /opt/rung/scripts/
sudo cp deploy/cron/rung-ingest.cron /etc/cron.d/rung-ingest
sudo chown root:root /etc/cron.d/rung-ingest
sudo chmod 0644 /etc/cron.d/rung-ingest

# Env file: /etc/rung/ingest.env
sudo tee /etc/rung/ingest.env > /dev/null <<'EOF'
KROGER_CLIENT_ID="..."
KROGER_CLIENT_SECRET="..."
DATABASE_URL="sqlite:////var/lib/rung/finance.db"
PYTHONUNBUFFERED=1
EOF
sudo chmod 0600 /etc/rung/ingest.env
```

Verify:
```bash
sudo systemctl status cron       # cron daemon running
cat /var/log/rung/ingest.log    # next run at 03:15 / 15:15
```

Manual run:
```bash
sudo -u root bash -c 'set -a; . /etc/rung/ingest.env; set +a; \
    /usr/bin/python3 /opt/rung/scripts/ingest_store_prices.py --dry-run'
```

---

## systemd install

```bash
sudo mkdir -p /etc/rung /opt/rung
sudo cp scripts/ingest_store_prices.py /opt/rung/scripts/
sudo cp scripts/ingest_store_prices.config.example.json /opt/rung/scripts/
sudo cp deploy/systemd/rung-ingest.service /etc/systemd/system/
sudo cp deploy/systemd/rung-ingest.timer /etc/systemd/system/

sudo useradd --system --home /var/lib/rung --shell /usr/sbin/nologin rung || true

# Env file: /etc/rung/ingest.env
sudo tee /etc/rung/ingest.env > /dev/null <<'EOF'
KROGER_CLIENT_ID="..."
KROGER_CLIENT_SECRET="..."
DATABASE_URL="sqlite:////var/lib/rung/finance.db"
EOF
sudo chown root:rung /etc/rung/ingest.env
sudo chmod 0640 /etc/rung/ingest.env

sudo systemctl daemon-reload
sudo systemctl enable --now rung-ingest.timer
```

Verify:
```bash
systemctl list-timers rung-ingest.timer
journalctl -u rung-ingest.service --since '-2d'
systemctl status rung-ingest.timer
```

Manual run:
```bash
sudo systemctl start rung-ingest.service
journalctl -u rung-ingest.service -e
```

---

## Sync-hazard between the two K8s CronJobs

`rung-ingest-cronjob.yaml` and `rung-ingest-catchup-cronjob.yaml`
are ~95% identical (same Secret, ConfigMap, PVC, securityContext,
container, image, resource limits). Only `metadata.name`,
`metadata.labels.role`, and `spec.schedule` differ. **If you change
anything inside `jobTemplate.spec.template.spec` in one file, mirror
the change in the other** or the two CronJobs will silently diverge
in production. Both files carry a SYNC HAZARD annotation in their
header as a tripwire. A future cleanup could replace the pair with a
single Kustomize overlay or a Helm chart that renders both.

## Kubernetes install

The CronJob expects an image `rung/ingest:latest`. Build it with the
repo's `Dockerfile.ingest`:

```bash
docker build -f Dockerfile.ingest -t rung/ingest:latest .
docker tag rung/ingest:latest <your-registry>/rung/ingest:latest
docker push <your-registry>/rung/ingest:latest
# Then update `image:` in BOTH rung-ingest-cronjob.yaml and
# rung-ingest-catchup-cronjob.yaml before applying.
```

```bash
# 1. Create the Secret (replace example values with real ones).
cp deploy/k8s/rung-ingest-secret.yaml.example deploy/k8s/rung-ingest-secret.yaml
$EDITOR deploy/k8s/rung-ingest-secret.yaml
kubectl apply -f deploy/k8s/rung-ingest-secret.yaml

# 2. Create the ConfigMap holding the JSON config.
kubectl apply -f deploy/k8s/rung-ingest-configmap.yaml.example

# 3. Create the PVC for SQLite persistence.
kubectl apply -f deploy/k8s/rung-ingest-pvc.yaml.example
# (Uncomment the storageClassName line for your cluster first.)

# 4. Create both CronJobs (twice-daily + weekday catch-up).
kubectl apply -f deploy/k8s/rung-ingest-cronjob.yaml
kubectl apply -f deploy/k8s/rung-ingest-catchup-cronjob.yaml
```

Verify:
```bash
kubectl get cronjob -l app=rung,component=store-cache-ingest
kubectl get jobs -l app=rung,component=store-cache-ingest
kubectl logs -l app=rung,component=store-cache-ingest --tail=200
```

Manual run:
```bash
kubectl create job --from=cronjob/rung-ingest rung-ingest-manual-$(date +%s)
kubectl logs -l job-name=rung-ingest-manual-$(date +%s) -f
```

---

## Verifying the cache is fresh

After any manual or scheduled run, confirm rows updated:

```bash
# Cron / systemd host
sqlite3 /var/lib/rung/finance.db \
    "SELECT store_name, COUNT(*), MAX(last_updated) FROM store_price_cache GROUP BY store_name;"

# K8s
kubectl exec -it deploy/rung -- \
    sqlite3 /var/lib/rung/finance.db \
    "SELECT store_name, COUNT(*), MAX(last_updated) FROM store_price_cache GROUP BY store_name;"
```

A healthy cache has `MAX(last_updated)` within the last cadence
interval (≤12h on the default schedule) and a stable `COUNT(*)` per
store+keyword (it grows only when new search terms are added to the
config — name-brand and store-brand rows coexist per keyword).

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `KROGER_CLIENT_ID and KROGER_CLIENT_SECRET must be set.` | Env file not loaded by the scheduler. | Verify `/etc/rung/ingest.env` exists and is readable by the cron/systemd user, or that the K8s Secret has both keys. |
| `status 401` in the journal | Token revoked or wrong credentials. | Re-fetch a fresh client secret from developer.kroger.com and rotate the env file / K8s Secret. |
| `status 429` (rate-limited) | Too many requests in a short window. | Lower `limit` in the JSON config or reduce the cadence. |
| `0 results` for every term | Wrong `location_id` in the JSON config. | Look up the right id via `GET /v1/locations?filter.zipCode=...` from Kroger; update the config. |
| DB lock errors on SQLite | Two concurrent ingest runs sharing the same `finance.db`. | `concurrencyPolicy: Forbid` on K8s; cron entries spaced ≥1h apart; systemd `Persistent=true` will only run one job at a time. |