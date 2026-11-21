# Backup + restore runbook (US-703)

How HesabYar's two databases are backed up, how a backup is proven
good, and how to restore one. Spec §15.4 + §17 phase 7; phased
design §1.3 phase 7.

**The one rule:** a backup is not considered valid until it has
been restored and checked. The monthly restore drill exists to
prove it; a backup whose drill has never passed is an untested
hope, not a backup.

## 1. What is backed up

| Target | What | How |
|---|---|---|
| `hesabyar` | The HesabYar PostgreSQL database (tenants, API keys, inbox/outbox, registries, settlement, cheques, installments, jobs) | `pg_dump -Fc` |
| `tryton` | The Tryton PostgreSQL database (the ledger: chart, journals, invoices, payments, stock) | `pg_dump -Fc` |

Both are PostgreSQL-native consistent backups (spec §15.4). Each
backup is Fernet-encrypted and uploaded to the configured object
store with a manifest (`<key>.manifest.json`) recording the
checksums, the retention expiry, and the encryption key id.

**Not backed up by this story:**

- **Tryton file attachments.** Tryton stores uploaded files (invoice
  attachments, scans) on the trytond server's filesystem, NOT in
  the database — `pg_dump` does not see them. They need a separate
  backup (a filesystem snapshot or an object-store copy of
  trytond's data directory). Until that exists, a restore recovers
  the books but not the attached files. The manual stopgap — a
  daily copy of trytond's data directory, paired with the same
  day's database backup — is the operator's procedure in
  `docs/operator-manual.md` §6.3 (US-705).
- **Configuration and secrets.** `.env` files, the Fernet keys, the
  S3 credentials and the Tryton DSN live outside both databases.
  Keep them in the installation's secret store (and keep an
  offline copy of the backup encryption key — see §6).

## 2. The S3 destination decision — PENDING (UNVERIFIED)

> **Status: UNVERIFIED, awaiting a deployment decision.**
> Phased design §1.3 phase 7: "S3 سازگارِ میزبانی‌شده در ایران یا
> S3 با egress غیرایرانی — ⚠️ UNVERIFIED تا تصمیم استقرار."

The code deliberately does NOT pick a provider and does NOT
hard-code an endpoint, region, or bucket. Everything is
configuration (`HESABYAR_BACKUP__ENDPOINT_URL`,
`HESABYAR_BACKUP__BUCKET`, `HESABYAR_BACKUP__REGION`,
`HESABYAR_BACKUP__ACCESS_KEY_ID`,
`HESABYAR_BACKUP__SECRET_ACCESS_KEY`). Any S3-compatible endpoint
works — the client is path-style SigV4 over plain HTTPS, with no
provider-specific behaviour.

**Before production, the operator must decide:**

1. Iran-hosted S3-compatible storage, or S3 with non-Iranian egress
   (the same Iran-hosting question as the rest of the deployment —
   nearly all banking services require an Iranian IP, so the
   decision should be made together with the hosting decision in
   US-706).
2. The bucket + a least-privilege access key (the backup key needs
   only `s3:PutObject`, `s3:GetObject`, `s3:DeleteObject`,
   `s3:ListBucket` on the backup prefix).
3. Whether the bucket has versioning enabled (recommended: a
   second line of defence against an accidental or malicious
   delete — pruning deletes objects, versioning keeps them
   recoverable).

Until the decision is made, backups run against the local-directory
backend (`HESABYAR_BACKUP__LOCAL_DIRECTORY`) in dev/CI only — that
backend is NOT a production destination (it lives on the same disk
as the databases it backs up).

## 3. Configuration

All settings live under the `HESABYAR_BACKUP__` prefix
(`BackupSettings` in `src/hesabyar/infrastructure/config.py`).

| Setting | Default | Meaning |
|---|---|---|
| `ENDPOINT_URL` | — (none) | S3-compatible endpoint, e.g. `https://s3.example.com`. No default — see §2. |
| `BUCKET` | — | Bucket name (required with `ENDPOINT_URL`). |
| `REGION` | — | S3 signing region (required with `ENDPOINT_URL`; SigV4 needs one, none is guessed). |
| `ACCESS_KEY_ID` / `SECRET_ACCESS_KEY` | — | S3 credentials (required with `ENDPOINT_URL`). Never logged. |
| `LOCAL_DIRECTORY` | — | Local-directory backend (dev/CI). Mutually exclusive with `ENDPOINT_URL`. |
| `KEY_PREFIX` | `hesabyar-backups` | Object-key prefix — one per installation (spec §15.1). |
| `ENCRYPTION_KEY` | — (none) | Fernet key for backups at rest. No default; backups fail closed without it. Mint with `scripts/backup.py gen-key`. |
| `HESABYAR_DATABASE_URL` | the app DB URL | DSN `pg_dump` uses for the HesabYar database. |
| `TRYTON_DATABASE_URL` | — (none) | DSN `pg_dump`/`pg_restore` use for the Tryton database. **No default** — HesabYar otherwise never knows this DSN (it talks REST). Used ONLY by the backup/restore operations layer. |
| `HESABYAR_ADMIN_DATABASE_URL` | `HESABYAR_DATABASE_URL` on the `postgres` DB | Maintenance-DB DSN (a role with CREATEDB) for drill scratch databases. |
| `TRYTON_ADMIN_DATABASE_URL` | `TRYTON_DATABASE_URL` on the `postgres` DB | Same, for the Tryton server. |
| `RETENTION_DAYS` | `30` | Daily backups kept at least this long. |
| `MONTHLY_KEEP_COUNT` | `12` | Monthly restore points kept past the daily window. |
| `BACKUP_CRON` | `30 22 * * *` | Daily backup, 02:00 Tehran (UTC cron). |
| `RESTORE_DRILL_CRON` | `30 23 * * *` | Drill fire time, 03:00 Tehran daily (the monthly gate runs it on the 1st). |
| `PG_DUMP_PATH` / `PG_RESTORE_PATH` | `pg_dump` / `pg_restore` | Binary paths (override when not on PATH). |
| `COMMAND_TIMEOUT_SECONDS` | `1800` | Timeout for one pg_dump / pg_restore run. |
| `MAX_DUMP_BYTES` | `512 MiB` | Fail-closed size guard (Fernet encrypts whole buffers). |
| `MAX_EXTERNAL_REFS_CHECKED` | `200` | Cap on cross-database reference checks per drill. |

### The §19.2 operational exception

Spec §19.2 forbids SQL access to Tryton's internal database from
HesabYar. **Backup/restore is the documented operational
exception:** `pg_dump`/`pg_restore` against the Tryton database is
whole-database backup, the same way the operator runs `pg_dump` by
hand — it is NOT application SQL access to Tryton tables. The code
lives in the operations layer
(`src/hesabyar/infrastructure/operations/`, never in application
services), no backup code reads or writes Tryton TABLES, and the
drill's Tryton checks (trial balance, external references) go
through the REST adapter against a scratch trytond. If a future
change needs to READ Tryton data for a check, it goes through the
adapter or it does not happen.

## 4. Scheduled jobs

On the existing Procrastinate app (US-560) — no second scheduler.
See `docs/scheduler.md`.

| Job | Queue | Schedule (Tehran) | What |
|---|---|---|---|
| `backup.databases` | `backup` | Daily 02:00, **Friday included** | Dump both DBs, encrypt, upload, prune. |
| `backup.restore_drill` | `backup` | Jalali 1st, 03:00 | Restore the newest pair into scratch DBs + reconcile. |

Notes:

- Backups run on Friday: data loss does not take the weekend off.
- The drill's Procrastinate cron fires daily at 03:00; the
  registry's Jalali-monthly gate runs it only on the 1st (other
  days are recorded as `skipped` runs, like Friday skips for daily
  jobs).
- A failed run is recorded in `scheduled_job_runs` and files a
  review item (`inbox_dead_letter`, deduped per job per day) —
  a failed backup is never silent.
- Manual trigger via `POST /api/v1/jobs/{job_id}/trigger` returns
  503 for these jobs: backups only run in the worker process
  (they need pg_dump + the object store), not in the REST process.
  Run them on demand with the CLI (§7) instead.
- If backups are not configured yet, the daily job fails every day
  (recorded + review item). That is the intended signal: configure
  backups before going live, or disable the job explicitly
  (`POST /api/v1/jobs/backup.databases/disable`) with a recorded
  decision — never let a failing backup job become background
  noise you ignore.

## 5. Retention + rotation

- **Daily window:** every backup younger than `RETENTION_DAYS`
  (default 30) is kept.
- **Monthly keeps:** of the older backups, the NEWEST backup of
  each Jalali month is kept for the `MONTHLY_KEEP_COUNT` (default
  12) most recent months — a monthly restore point survives the
  daily window.
- **Pruning** runs after each successful backup (per target) and
  deletes the expired object + its manifest. A pruning failure is
  logged and reported but never fails the backup (old backups
  piling up costs money; it does not lose data).
- **Unparseable manifests are never pruned.** If a manifest is
  corrupt, pruning logs a warning and leaves both the manifest and
  its object alone for the operator to inspect.
- **Rotation** (the operator's part): retention is automatic, but
  the ENCRYPTION KEY is not — see §6.

Object layout (per target):

```text
<KEY_PREFIX>/<target>/YYYY-MM-DD/HHMMSS-<target>.dump.enc
<KEY_PREFIX>/<target>/YYYY-MM-DD/HHMMSS-<target>.dump.enc.manifest.json
```

## 6. Encryption + key rotation

- Backups are encrypted at rest with Fernet (AES-128-CBC +
  HMAC-SHA256, the same pattern as the credential vault). The key
  comes from `HESABYAR_BACKUP__ENCRYPTION_KEY` — a separate key
  from the credential vault's, with its own rotation story.
- The manifest records the key id (first 8 hex of the key's
  SHA-256), so a restore knows which key encrypted a backup.
- **Keep an offline copy of the encryption key.** A backup whose
  key is lost is unrecoverable — Fernet has no recovery path.
  Store the key in the installation's secret store AND offline
  (the same place the database passwords live).

### Key rotation (manual procedure — no tooling)

Rotation tooling does not exist (the same accepted gap as the
credential vault — threat model A6). To rotate:

1. Mint a new key: `uv run python scripts/backup.py gen-key`.
2. Put the new key in `HESABYAR_BACKUP__ENCRYPTION_KEY` and restart
   the worker. New backups use the new key; the manifest records
   the new key id.
3. **Keep the old key** until every backup it encrypted has aged
   out of retention (30 days + 12 monthly keeps — i.e. up to ~13
   months), OR re-encrypt the old backups by hand:
   download → decrypt with the old key → encrypt with the new key
   → re-upload under the same object key with an updated manifest
   (same `created_at`, new `sha256`/`size_bytes`/`encryption_key_id`).
   There is no script for this yet; do it one backup at a time and
   verify each with `scripts/backup.py drill` afterwards.
4. Only delete the old key from the secret store once no manifest
   references its key id anymore
   (`scripts/backup.py list --all` shows the key id per backup).

## 7. Operator CLI

`scripts/backup.py` — the same services the scheduler jobs run:

```bash
uv run python scripts/backup.py backup            # both databases
uv run python scripts/backup.py backup --target hesabyar
uv run python scripts/backup.py drill             # newest pair, scratch DBs
uv run python scripts/backup.py drill --keep-scratch
uv run python scripts/backup.py drill --target hesabyar   # HesabYar only
uv run python scripts/backup.py list              # newest manifest per target
uv run python scripts/backup.py list --all
uv run python scripts/backup.py prune --dry-run
uv run python scripts/backup.py prune
uv run python scripts/backup.py gen-key
```

Exit code 0 = ok, 1 = the operation failed. The CLI never prints
secrets (only database names and object keys).

## 8. The restore drill (what it proves)

Monthly (Jalali 1st) + on demand. Steps:

1. Download the newest manifest per target; verify the stored
   object's SHA-256 against the manifest; decrypt; verify the
   plaintext SHA-256 and size.
2. Create scratch databases (`hesabyar_drill_<ts>_<rand>`,
   `tryton_drill_<ts>_<rand>`) and `pg_restore` into them. The
   HesabYar scratch restore uses `--no-owner --no-privileges`
   (the drill reads it as the admin superuser). The Tryton
   scratch restore KEEPS owners + ACLs: trytond reads the
   scratch database as the `tryton` role (a plain LOGIN role,
   not a superuser), so a `--no-owner` restore would leave
   every object owned by the admin and trytond could not read
   a single table. This works because the drill restores onto
   the SAME server, where the `tryton` role exists.
3. Run the reconciliation checks on the SCRATCH databases:

   | Check | What it proves |
   |---|---|
   | `hesabyar.migrations` | Single Alembic head, and the restored DB is AT that head. |
   | `hesabyar.orphaned_documents` | No row references a missing row (generic FK sweep over every HesabYar table). |
   | `hesabyar.settlement_tranches` | Every payment's tranches tie out (Σ net == payment net, Σ gross == payment gross, progress in bounds). |
   | `cross_database.external_references` | Every external-id-map row pointing at Tryton resolves in the RESTORED Tryton DB (REST adapter). |
   | `tryton.trial_balance` | Every company's trial balance balances (posted moves only, REST adapter). |

4. Drop the scratch databases (unless `--keep-scratch`).

The report distinguishes `passed` (no check failed) from
`fully_verified` (no check failed AND none skipped). A drill that
skipped the trial-balance check — no scratch engine configured —
passed what it ran but did NOT fully verify the backup, and the
report says so. Treat a skipped trial-balance check as an
open item, not a pass.

**The trial-balance check needs a trytond that can serve the
scratch database.** The same trytond that serves production does
this (the adapter's `database` setting selects the scratch DB by
name; the restored DB already contains the REST application key).
If the worker cannot reach trytond, the Tryton checks are skipped —
configure the worker's Tryton settings before relying on the
drill.

## 9. Restoring (the real thing)

> **Stop the writes first.** Restore into a STOPPED installation:
> stop the API, worker and MCP processes, and stop trytond. A
> restore into a live system races with new writes and produces a
> database that is neither the backup nor the live state.

### 9.1 Full restore (both databases)

```bash
# 1. Find the pair to restore (same date, both targets).
uv run python scripts/backup.py list --all

# 2. Download + decrypt both dumps (to a local directory).
#    There is no single restore command yet — use the object store
#    directly (aws-cli / mc / the provider console for S3; plain
#    files for the local backend), then decrypt with Python:
uv run python - <<'EOF'
import json
from pathlib import Path
from hesabyar.infrastructure.config import AppSettings
from hesabyar.infrastructure.operations.backup_cipher import FernetBackupCipher

settings = AppSettings()
cipher = FernetBackupCipher(settings.backup.encryption_key)
for target in ("hesabyar", "tryton"):
    manifest = json.loads(Path(f"{target}.dump.enc.manifest.json").read_bytes())
    plain = cipher.decrypt(Path(f"{target}.dump.enc").read_bytes())
    import hashlib
    assert hashlib.sha256(plain).hexdigest() == manifest["plaintext_sha256"], target
    Path(f"{target}.dump").write_bytes(plain)
    print(f"{target}: decrypted + verified ({len(plain)} bytes)")
EOF

# 3. Drop + recreate both databases, then restore.
#    (PostgreSQL 13+: DROP ... WITH (FORCE) disconnects stragglers.)
psql -U postgres -d postgres -c "DROP DATABASE IF EXISTS hesabyar WITH (FORCE);"
psql -U postgres -d postgres -c "CREATE DATABASE hesabyar OWNER hesabyar ENCODING 'UTF8';"
psql -U postgres -d postgres -c "DROP DATABASE IF EXISTS tryton_db WITH (FORCE);"
psql -U postgres -d postgres -c "CREATE DATABASE tryton_db OWNER tryton ENCODING 'UTF8';"
pg_restore -d hesabyar hesabyar.dump
pg_restore -d tryton_db tryton.dump

# 4. If the backup predates the current code, migrate AFTER restoring:
uv run alembic upgrade head

# 5. Verify BEFORE restarting anything: run the drill against the
#    restored databases (it restores the same pair into scratch DBs
#    and reconciles — if the drill passes, the restore is good).
uv run python scripts/backup.py drill

# 6. Restart trytond, then the API / worker / MCP. Smoke-test:
#    one report, one webhook, one MCP call (see US-706's smoke suite).
```

Notes:

- The dumps include owners/ACLs (`pg_dump` default). Restore as
  a superuser, or create the `hesabyar` / `tryton` roles first —
  `pg_restore` fails on a missing owner unless `--no-owner` is
  passed. A production restore should keep owners (same as the
  drill's Tryton scratch restore); the drill's HesabYar scratch
  restore uses `--no-owner` because the drill reads it as the
  admin superuser.
- `pg_restore` exit code 1 with "already exists" errors means the
  target database was not empty — drop + recreate it and retry.

### 9.2 Restoring ONE database without the other

Sometimes only one side is lost (or only one side needs rolling
back). Restore that side per §9.1, then **reconcile** — the two
databases now disagree about what happened after the restored
side's backup time:

1. Run the drill: it restores the pair into scratch DBs and runs
   the cross-database checks. The `cross_database.external_references`
   check is the reconciliation probe — HesabYar rows pointing at
   Tryton records that no longer exist (or vice versa) show up as
   orphans.
2. For each orphan: decide by hand whether the HesabYar side or
   the Tryton side is authoritative (the ledger is authoritative
   for posted money; the store/inbox is authoritative for unposted
   intake). Fix through the normal application flows (re-ingest,
   re-map, or a correcting journal) — never by hand-editing the
   restored database.
3. Re-run the drill until it passes fully.

### 9.3 Point-in-time recovery

`pg_dump` backups are snapshots, not WAL archives: recovery is to
the last backup (up to ~24h of data loss on the daily schedule).
If the store needs tighter RPO, add WAL archiving (continuous
archiving + `pg_basebackup`) on top of these dumps — that is a
separate story, not this one.

## 10. Failure modes

| Symptom | Cause | Response |
|---|---|---|
| Daily job fails: "No backup destination is configured" | No `ENDPOINT_URL` / `LOCAL_DIRECTORY` | Configure a destination (§3), or disable the job with a recorded decision (§4). |
| Daily job fails: "No backup encryption key" | No `ENCRYPTION_KEY` | Mint + configure a key (§6). Never back up unencrypted. |
| Daily job fails: "pg_dump was not found" | Worker image lacks postgresql-client | Install `postgresql-client` on the worker image (at least as new as the server). |
| Backup fails: "server version mismatch" | pg_dump older than the PostgreSQL server | Install a matching postgresql-client (pg_dump refuses a newer server). |
| Backup fails: S3 403 | Key lacks permission / wrong bucket | Check the key's policy (§2: only the four operations on the backup prefix). |
| Backup fails: "over the N-byte limit" | Dump grew past `MAX_DUMP_BYTES` | Raise the limit only if the worker has the memory (Fernet holds the whole dump); otherwise this is the signal to implement streaming encryption. |
| Drill fails: checksum mismatch | Stored object changed after upload (bit rot, overwrite, wrong object) | The backup is untrusted — do NOT restore it. Restore the previous backup and investigate the store (bucket versioning, §2, is the recovery path). |
| Drill fails: cannot decrypt | Wrong key configured (rotated key, wrong installation's key) | Find the key whose id matches the manifest (§6); keep old keys until their backups age out. |
| Drill fails: `hesabyar.migrations` | Backup predates the current code | Expected after an upgrade: migrate after restoring (§9.1 step 4). If the backup is current, the dump is corrupt. |
| Drill fails: orphaned documents / tranche tie-out | Partial or corrupt restore | Do not trust the backup; try the previous one. If several fail, the dump pipeline itself is suspect — check pg_dump's stderr in the job log. |
| Drill fails: trial balance does not balance | The restored ledger is inconsistent | Stop. Do not restore this backup into production. Investigate whether the SOURCE ledger was already unbalanced (run the trial balance against production via the API) — the drill may have found a real accounting problem, not a backup problem. |
| Drill skips the Tryton checks | Worker cannot reach trytond, or no scratch engine configured | Configure the worker's Tryton settings (§8). A drill with skipped checks is not a full verification. |
| Scratch databases left behind (`*_drill_*`) | Drill crashed mid-cleanup, or `--keep-scratch` | Safe to drop by hand (`DROP DATABASE <name> WITH (FORCE)`) — they are throwaway by construction. Check nothing else references them first (nothing should). |
| Job fails every day, nobody notices | Review items ignored | The failure review item is the alert until US-704's monitoring lands. Check the review queue daily; US-704 adds backup-failure alerts. |

## 11. Who does what

| Task | Who | Notes |
|---|---|---|
| Choose the S3 destination (§2) | Store owner + operator | UNVERIFIED until decided; blocks production backups. |
| Configure backup settings + keys | Operator | Keys in the secret store; offline copy of the encryption key (§6). |
| Watch the daily job + review items | Operator | A failed backup is a same-day fix, not a backlog item. |
| Run the monthly drill / investigate failures | Operator | On the 1st, or on demand after any restore. |
| Perform a production restore | Operator (+ developer on call for §9.2 reconciliation) | Stop the writes first (§9). Never restore into a live system. |
| Rotate the encryption key | Operator | Manual procedure (§6); keep the old key. |
| Fix the backup code / add streaming encryption / WAL archiving | Developer | Future stories; file them, don't improvise. |
| Decide the retention policy | Store owner | Defaults (30 days + 12 monthly) are a starting point, not a policy. |

## 12. Worker image requirements

The worker that runs the backup jobs needs:

- `pg_dump` + `pg_restore` (postgresql-client, version **>=**
  the PostgreSQL server version — pg_dump refuses a newer server).
- Network access to both PostgreSQL servers and to the S3 endpoint.
- For the drill: a CREATEDB role on both servers (the admin DSNs),
  and REST access to trytond (for the trial-balance check).
