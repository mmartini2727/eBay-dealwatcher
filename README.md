# DealWatch

DealWatch is a generic marketplace deal-monitoring engine. It polls a
marketplace for active listings matching a profile, normalizes them into a
comparable shape, scores them against a self-built price baseline, and alerts
when something is worth acting on.

First target: eBay / Lenovo ThinkPad T14. The architecture is profile-driven
and provider-driven so the second target costs a YAML file, not a rewrite.

Read `docs/design.md` before changing anything structural. It is authoritative
and the decisions in it were made deliberately.

## Two things to know up front

**There is no sold-listings API.** eBay's `findCompletedItems` is deprecated
and Marketplace Insights is Limited Release and effectively unobtainable.
DealWatch builds its price baseline from history it collects itself. See
design.md §2 before proposing anything that queries sold comps.

**Nothing here is internet-exposed.** eBay's Marketplace Account Deletion
compliance endpoint lives in a separate Cloudflare Worker repo
(`ebay-deletion-endpoint`) because it is a permanent uptime obligation that
shares nothing with this service. DealWatch binds to LAN/loopback only. Do not
fold that endpoint back in — reasoning in design.md §3.1.

## V0.1 status

- Dockerized FastAPI application, LAN-bound
- Health endpoint
- Environment-based secret/config handling
- Profile schema + validation (`dealwatch/normalize/schema.py`)
- Unit tests
- Placeholders for the eBay provider, normalize engine, collector, scoring,
  SQLite, notifier, and MCP server

## Configuration

```bash
cp .env.example .env
```

| Variable | Purpose |
| --- | --- |
| `EBAY_CLIENT_ID` | Portal calls this the App ID |
| `EBAY_CLIENT_SECRET` | Portal calls this the Cert ID |
| `DISCORD_WEBHOOK_DEALS` | Alert destination (V0.9) |
| `LOG_LEVEL` | Defaults to INFO |

Browse only needs an **application access token** via the client credentials
grant — no user token, no consent flow, no RuName. Scope is
`https://api.ebay.com/oauth/api_scope` and nothing more.

Do not commit `.env`. On the LXC, keep it on the filesystem and reference it
with `env_file:` rather than pasting values into the Portainer stack editor.

## Run

```bash
docker compose up --build -d
curl http://127.0.0.1:8087/health
```

```json
 {
    "status": "ok",
    "budget": {
        "period": "2026-08-29",
        "used": 6,
        "ceiling": 4750,
        "remaining": 4744
    }
 } 
```
> **Note:** `period` is the LA date the counter belongs to; `ceiling` is `daily_call_limit - daily_reserve_calls`.

The application listens on port 8000 inside the container and is published as port 8087 on the Docker host.

From the Docker LXC itself, the health endpoint can be reached at:
http://127.0.0.1:8087/health

From another device or service on the LAN, such as Uptime Kuma, use the Docker LXC's LAN IP:
http://192.168.99.204:8087/health

The published port binds all interfaces so LAN monitoring can reach it. The LXC is not port-forwarded and DealWatch has no authentication — this is a LAN-only service by design (see design.md §3.1).

## Development without Docker

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
pytest
uvicorn dealwatch.main:app --reload
```

No test may require live eBay credentials to pass.

## Profiles

`profiles/*.yaml` defines what to hunt: query strings, Browse-side filters,
require/reject rules, attribute extraction, bucket keys, seed baselines, and
alert thresholds. Adding a new target should be a YAML file and nothing else —
there is exactly one normalization engine and it is generic.

Trace a title through the pipeline:

```bash
python -m dealwatch.normalize.explain \
  --profile profiles/thinkpad-t14.yaml \
  --title "Lenovo ThinkPad T14 Gen 2 Ryzen 5 PRO 5650U 16GB/512GB"
```

Profiles are validated at load. A bad regex or a `bucket_key` naming a field
no stage produces is a startup error, not a silent no-match at 2am.

## Milestones

The collector deliberately ships **before** scoring. The survival-derived
baseline needs weeks of history and that clock only starts when rows begin
landing. Every day the collector is not running is a day of comps that cannot
be recovered.

| Version | Deliverable | Status |
| --- | --- | --- |
| V0.1 | Docker + FastAPI skeleton | done |
| V0.2 | eBay OAuth (client credentials, token cache) | done |
| V0.3 | Browse API search + persisted rate-limit budget | done |
| V0.4 | Normalized `Listing` model | done |
| V0.5 | SQLite + listing history (WAL) | done |
| V0.6 | **Dumb collector loop — poll and persist, no scoring** | done |
| V0.7 | ThinkPad T14 profile + normalize engine | done |
| V0.8 | Scoring engine (a-e: baselines, scoring ladder, bucket_key, sweep/pagination data integrity, `poll.sort`) | done |
| V0.9 | Discord alerts | done |
| V0.9a | Soldered-RAM buyability labeling (pulled out of V0.9 - design.md §5.7) | next |
| V1.0 | MCP server (streamable HTTP, LAN only) | |

Ship V0.6 even though the normalizer is a stub. Raw titles and prices are
useful history, and persisting `raw_json` means the engine can be re-run over
everything already collected once it exists.

## Flow Chart 
![dealwatch_pipeline_overview.svg](docs/dealwatch_pipeline_overview.svg)

## Database snapshots

`data/dealwatch.db` is the irreplaceable asset. Code is rewritable; months of
comps are not. A live LXC/Proxmox backup does **not** guarantee a consistent
SQLite file — WAL mode means the `.db`, `-wal`, and `-shm` files are only
coherent together, and a filesystem-level copy can catch them mid-write.

Snapshots are taken with `VACUUM INTO`, which produces a single fully
checkpointed file with no sidecars, from a read transaction — the collector
keeps running throughout. **Do not stop the container to take a snapshot.**

Offsite retention is the PBS prune policy for this LXC: last 3, 7 daily,
4 weekly, 12 monthly. That is the real answer to "how far back can I
recover comps" — roughly a year.

### Taking one

On the **LXC host** (the container image has no `sqlite3` CLI):

```bash
cd /path/to/dealwatch
./scripts/snapshot.sh              # routine
./scripts/snapshot.sh pre-v0.9     # labelled, before a deploy or migration
```

Output goes to `/root/dealwatch-data/dealwatch-<UTC timestamp>[-tag].db`,
deliberately **outside the repo and outside the `data/` bind mount**.
Snapshots are carried offsite by this LXC's PBS backup; there is no
separate copy step. `DEALWATCH_SNAPSHOT_KEEP` controls local retention
(default 7); `DEALWATCH_SNAPSHOT_DIR` overrides the destination.

The script fails loudly on a failed `PRAGMA integrity_check` or a zero-row
snapshot, leaving a `.partial` file for inspection. A snapshot's observation
count being slightly *below* the live database is expected — the collector
wrote during the vacuum.

### When

- **Daily at 01:30 local**, via cron on the LXC host (not in the container):
  `30 1 * * * cd /path/to/dealwatch && ./scripts/snapshot.sh >> /var/log/dealwatch-snapshot.log 2>&1`
- **Before every deploy**, tagged with the milestone.
- **Before any migration or backfill** that rewrites existing rows.

01:30 is chosen to complete ahead of this LXC's 02:00 PBS backup. **If the
PBS schedule changes, change this too** — a snapshot taken after the backup
window is offsite a full day late, and the staleness is invisible until you
need it. The vacuum writes to a `.partial` name and renames atomically, so
an overlap cannot produce a corrupt snapshot — but it can produce a PBS
backup containing no snapshot at all for that night.

### Restoring

A PBS restore of this LXC contains both the live `data/dealwatch.db`
(captured mid-write at 02:00) and the vacuum snapshots in
`/root/dealwatch-data/`. **Use a snapshot.** The live copy is likely
recoverable via WAL replay, but that depends on the backup mode being atomic
across `.db`/`-wal`/`-shm`; the snapshot depends on nothing. Do not simply
start the container on a restored LXC and assume the database is sound.

```bash
docker compose down
cp /root/dealwatch-data/dealwatch-<stamp>.db data/dealwatch.db
rm -f data/dealwatch.db-wal data/dealwatch.db-shm
chown 10001:10001 data/dealwatch.db
docker compose up -d
```

The `chown` is not optional — the container runs as uid 10001 and a
root-owned database file fails to open on write. Delete the stale `-wal`
and `-shm`: they belong to the database you just replaced.

## Operational notes

- Add `/health` to Uptime Kuma. Separately add an **external** monitor against
  the Worker's challenge endpoint — its silent death has consequences that
  otherwise go unnoticed for days.
- Container runs as a non-root user; `data/` is chowned to it.
- `profiles/` mounts read-only, `data/` read-write.
- **Updating `scripts/*.py` on the LXC: use the trailing-`/.`/trailing-`/`
  form, not the bare one.** `scripts/` (the maintenance/report scripts —
  `baseline_report.py`, `recompute_baselines.py`, `score_active.py`, etc.)
  isn't baked into the image or mounted by `compose.yaml`; it has to be
  copied into the running container by hand before any of them can be run
  there. `docker cp scripts dealwatch:/app/scripts` only overwrites cleanly
  the *first* time. Once `/app/scripts/` already exists inside the
  container, Docker's `cp` copies the *directory* `scripts` into it rather
  than merging its contents — a second invocation silently nests it as
  `/app/scripts/scripts/*.py`, and the container keeps running whatever was
  already at `/app/scripts/*.py`. This is the same class of failure as the
  profile-restart bug above: the copy command reports success either way.

  ```bash
  docker cp scripts/. dealwatch:/app/scripts/
  ```

  Verify the copy actually landed before trusting a script's output —
  don't rely on the exit code:

  ```bash
  md5sum scripts/*.py | awk '{print $1}' | sort > /tmp/local.md5
  docker exec dealwatch sh -c 'md5sum /app/scripts/*.py' | awk '{print $1}' | sort > /tmp/remote.md5
  diff /tmp/local.md5 /tmp/remote.md5 && echo "scripts match"
  ```
