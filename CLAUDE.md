# Fairly Reservation Sync

Automated pipeline that pulls upcoming reservations from Fairly for the owner's homes, normalizes them into one record shape, and delivers them to:

1. **Owner's API / database**: the system of record for all upcoming reservations.
2. **RemoteLock CSV (Big Sky only)**: Big Sky has no Fairly-managed lock code, so guest access is provisioned in RemoteLock from a CSV upload (manual today).

Target runtime: **Azure Function, timer trigger (cron)**.

## Status

- [x] Manual proof of concept via Claude + Fairly MCP (Nov 2026): CSV exports in this folder.
- [x] Phase 1: Spec written (some open questions remain, see below)
- [ ] Phase 2: Fairly data access from a headless job (Claude Scheduled Task + Fairly connector)
- [x] Phase 3: Transform module `sync.py` with tests, verified against the manual exports
- [ ] Phase 4: Owner API integration (email summary via `notify.py` written; reservations API not started)
- [x] Phase 5: RemoteLock CSV generation (Big Sky)
- [ ] Phase 6: Scheduling and monitoring

## Code layout

| File | Purpose |
|---|---|
| `config.json` | Homes in scope, owner ID, email settings |
| `sync.py` | Raw Fairly JSON (`raw/`) → CSVs + `summary.json` (`output/`). All business rules live here. |
| `notify.py` | Emails `summary.json` and the RemoteLock rows through the messaging API |
| `run_sync.sh` | Cloud entry point: exports branch worktree → `sync.py` → commit/push `exports` → `notify.py` |
| `tests/test_sync.py` | Unit tests with made-up data: `py -m unittest discover -s tests` |

Local run (Windows): `py -m pip install -r requirements.txt`, put `get_reservation_by_id` JSON files in `raw/`, then `py sync.py --raw raw --out output`. Test the email with `py notify.py --out output --dry-run`, or against the local server with `--api-url https://localhost:7075/api/email/sendemailform --insecure --force`.

**Exports branch:** `exports` is an orphan branch holding only the latest CSVs and summary, one commit per run that changed something. It contains guest PII and must stay in a private repo.

**Email API notes** (`POST https://messaging.zmark.net/api/email/sendemailform`, test server `https://localhost:7075/api/email/sendemailform`):
- `[FromForm] EmailToSend`, sent as multipart/form-data: `ToEmail`, `ToName`, `Subject`, `EmailBody` (HTML) and `Attachments` (the RemoteLock CSVs).
- The application ID is fixed at 2 in the API, which also sets the From address (`GetAdminInfo`). Don't send From fields.
- A `checkUserAgent` filter blocks script User-Agents (`python-urllib`, `python-requests`, `curl/`, `Headless`, ...). `notify.py` sends `FairlyReservationSync/1.0`; keep it that way.
- Auth: none for now (shelved). It uses the `PublicForm` rate limit.
- The old `sendemail` ([FromBody]) action is still used elsewhere. Don't change it.

## Homes in scope

| Home | Fairly home ID | Location / TZ | Lock / access | Outputs |
|---|---|---|---|---|
| Papago Park Condo w/ Pool & Hot Tub | `m3C823iHokWV3CSBFjkFU` | Phoenix, AZ / America/Phoenix | Fairly-managed code | API |
| Big Sky Mountain Retreat \| Ski-In/Ski-Out | `wDGU0odwZhMn_liZWCTHm` | Big Sky, MT / America/Denver | **None in Fairly → RemoteLock** | API + RemoteLock CSV |
| Palms of Destin Condo w/ Lagoon Pool | `ZqH1txJqhcMDXvsIwmkrz` | Destin, FL / America/Chicago | Building staff (front desk) | API (added Nov 11 2026). Caretaker is Amber Greenwood, so "Caretaker Payout" here is her money, not the owner's. |
| Sedona View Retreat | `puCjP_PbkDflhxH-JmXLH` | Sedona, AZ | Not live | Out of scope |

Keep home IDs in configuration, not hard-coded, so homes can be added or removed.

## Data source: Fairly

Accessed today through the **Fairly MCP server** (tools: `search_reservations`, `get_reservation_by_id`, `search_homes`, `whoami`). Results are scoped to homes the authenticated user is on.

- `search_reservations(home_id, date_range_start=today)` returns every reservation whose stay overlaps today or later, newest check-in first, 50 per page (`nextCursor` for more).
- `get_reservation_by_id(id)` returns the reservation plus line items, funds distribution, payouts and conversation messages. Responses are large (75–120 KB each).

### Filtering rules

- Keep only `STATUS = CONFIRMED` (and `MIDSTAY_CANCELLED` if the stay is in progress).
- Drop `CANCELLED`, `PAYMENT_FAILED`, `VALIDATION_FAILED`, `PROPOSED_ALTERATION`, `INITIAL_SNAPSHOT`. Failed VRBO payment attempts show up as duplicate rows for the same dates.
- Include stays in progress (check-in before today, check-out on or after today) and flag them in Notes.
- Include owner (`OWNER`) and owner's-guest (`OWNER_GUEST`) stays. They need access too.

## Record schema

The unique key is the **Fairly reservation ID** (`ID`). Every write must be idempotent on this key.

| Field | Source | Notes |
|---|---|---|
| Fairly Reservation ID | `ID` | Primary key. Not in the CSV today; required for the API. |
| Property | home `TITLE` | |
| Check-in | `CHECK_IN` | Date. Time comes from `CHECK_IN_AT` (home TZ, normally 4 PM). |
| Check-out | `CHECK_OUT` | Date. Time comes from `CHECK_OUT_AT` (normally 10 AM). |
| Nights | derived | `CHECK_OUT - CHECK_IN` |
| Guest First Name | `GUEST_FIRST_NAME` | |
| Last Initial | parsed from the "Reservation created" system message: `...confirmed reservation with <First> <I>.` | **Fragile**: depends on message text. Blank for owner-entered stays (no message). The owner's own stays use the owner's initial. |
| Guest Type | `GUEST_TYPE` | Guest / Owner / Owner's guest |
| Phone | `GUEST_PHONE_NUMBERS[0]` | **Airbnb stopped sharing real guest phones in late Oct 2026** and now only gives a proxy number, so new Airbnb bookings arrive with no phone. Missing phones on Airbnb are expected (no warning); VRBO and direct bookings still have them. |
| Email | n/a | **Not available from Fairly.** Kept as an empty column for compatibility. |
| Channel | `CHANNEL_ID` | AIRBNB / VRBO / FAIRLY_DIRECT / FAIRLY_WEB (owner bookings) |
| Confirmation | `EXTERNAL_ID` | Channel confirmation code. Empty for owner bookings. |
| Adults / Children | `GUEST_NUMBER_ADULTS` / `_CHILDREN` | Null for owner bookings |
| Pets | `GUEST_NUMBER_PETS` | |
| Booked On | `BOOKED_AT` | UTC; convert to the home's TZ |
| Owner Payout | `get_reservation_by_id` → `funds_distribution`: sum `AMOUNT_DUE` where `DELETED_AT` is null, `USER_ID` = owner, `ROLE = OWNER`, reason `REVENUE_SHARE_RENT` | Expected net payout; Fairly recalculates it live until paid out (KB article 84). $0 for owner stays. Do **not** use `PRICES`, which is the gross advertised price. |
| Caretaker Payout | same table, `ROLE = CARETAKER` (`CLEANING_LABOR` + `REVENUE_SHARE_RENT`) | The owner is also the caretaker on Papago and Big Sky, so this money goes to them too. |
| Cancellation Policy | `CANCELLATION_POLICY` | |
| Message Thread Link | `MESSAGE_THREAD_LINK` | Null for owner bookings |
| Status | `STATUS` | Needed for change detection |
| Notes | `NOTES` + derived flags | e.g. "In progress" |

## Outputs

### 1. Owner API / DB

- Upsert by Fairly Reservation ID.
- When a previously synced reservation becomes cancelled or disappears, mark it cancelled. Don't delete it.
- API details: **TBD** (see open questions).

### 2. RemoteLock CSV (Big Sky only)

- One file per run containing Big Sky reservations only.
- Columns (RemoteLock import format): `name, email, department, start date, start time, end date, end time, pin`
  - `name` = `<First> <Last Initial>.` · `email` = empty (not available) · `department` = home title
  - `start date` / `start time` = check-in date, `16:00` · `end date` / `end time` = check-out date, `10:00` (formats `YYYY-MM-DD`, 24-hour `HH:mm`, confirmed OK)
  - `pin` = **last 4 digits of the guest's phone**. If there's no phone, leave it blank and flag it. Also flag two overlapping stays with the same PIN.
  - **Airbnb impact:** new Big Sky Airbnb guests have no phone, so no PIN. Airbnb now provides a **"suggested door code"** (last 4 digits of the real guest phone). The Fairly MCP team offered to expose it. **TODO:** once exposed, use it as the PIN when there's no phone (field name TBD), and add a test.
- File name: `remotelock_big_sky.csv`
- **Import behavior:** RemoteLock ignores duplicate rows, so re-uploading the full list is safe. Not yet known: whether a changed row (new dates) updates the existing guest or adds a second one, and how cancellations get removed. That probably has to be manual or done through the RemoteLock API.
- **Known issue:** RemoteLock has set schedules **1 day off** before. This is likely a time zone problem, where a date-only value is read as UTC midnight and then shifted to Mountain time. Check the time zone on the RemoteLock account and lock, and spot-check the first and last guest after each upload.
- Delivery: manual upload today. Later, the RemoteLock API could replace the CSV.
- Cancellations and date changes after upload must be applied to RemoteLock too. Output a delta (new / changed / cancelled) or a full snapshot, depending on how RemoteLock imports.

## Architecture (proposed)

```
Timer (cron) ─► Azure Function
                  ├─ Fetch: Fairly reservations for configured homes
                  ├─ Filter + normalize ─► Reservation records
                  ├─ Diff against last state (owner DB is the source of truth)
                  ├─ Upsert changes ─► Owner API
                  └─ Big Sky ─► RemoteLock CSV ─► Blob Storage (+ notify)
```

### Runtime options

- **A. Claude Scheduled Task (preferred, per the Fairly MCP team).** A cloud Claude agent runs on a cron schedule, fetches from Fairly through the MCP connector, then runs a **deterministic transform script kept in a git repo**. That script does the filtering, field mapping and the RemoteLock CSV, then calls the owner API. Claude only handles the fetch and the orchestration; business rules live in code with tests.
  - Fairly auth: add Fairly as a **custom connector** on claude.ai (Settings → Connectors → Add custom connector, URL `https://mcp.fairly.com/`), then click Connect and sign in to Fairly once. The connector holds the login. No Fairly password goes in the task, prompt or repo.
  - The routine needs this repo on GitHub (it clones it on each run).
- **B. Azure Function.** Requires headless Fairly access, which isn't available yet.

- Secrets (Fairly credentials, owner API key) go in **Azure Key Vault**, through Function app settings references.
- Generated CSVs go in **Blob Storage** with a short retention period. They contain guest PII (names, phone numbers).
- Logging: Application Insights. Never log phone numbers or full guest records.

## Plan

1. **Resolve open questions** (below). Phase 2 is blocked on #1.
2. **Fairly access spike**: prove a headless job can authenticate and call `search_reservations` / `get_reservation_by_id`.
3. **Transform module**: pure functions (raw reservation → record), with unit tests using the saved JSON samples. Covers status filtering, in-progress stays, the last-initial parser, missing phones, owner stays and payout extraction.
4. **Owner API client**: upsert and cancel; idempotent; retry with backoff.
5. **RemoteLock CSV writer**: Big Sky filter, exact column order, delta logic.
6. **Azure Function**: timer trigger, config, Key Vault, Blob output, alerting on failure; deploy with infrastructure-as-code.
7. **Parallel run**: compare automated output against a manual export for 1–2 weeks before relying on it.

## Open questions

1. **Fairly access outside Claude.** The Fairly MCP team says they run the MCP on a schedule through **Claude Scheduled Tasks** (cloud routines). That is now the leading option in place of an Azure Function; see "Runtime options". To verify: Fairly is connected as a claude.ai connector, the routine can reach the owner API, and secrets can be supplied to it.
2. **Owner API**: base URL, auth method, endpoints and payload schema for upserting reservations. Is there an existing reservations table, or a new one?
3. **RemoteLock details**: the column list is known (see Outputs). Still open: the PIN source, the `department` value, the accepted date/time formats, whether re-importing updates or duplicates existing guests, and whether to use the RemoteLock API instead.
4. **Owner and owner's-guest stays in RemoteLock**: include them?
5. **Schedule**: how often? Hourly catches last-minute bookings; daily is simpler.
6. **Language/runtime** for the Function: C#/.NET, Python or Node?
7. **Owner Payout**: resolved; it uses the expected amount from the planned payout split. Should the API also store the actual paid-out amount once the `payouts` rows exist (after check-in)?
8. **Retention**: how long to keep guest phone numbers in the DB and Blob Storage?

## Conventions

- Dates in CSVs: `YYYY-MM-DD`. Phone numbers: `(xxx) xxx-xxxx` in CSVs, E.164 (`+1...`) in the API.
- CSV encoding: UTF-8 without BOM, comma-delimited, quoted fields.
- Treat guest data as PII. Don't commit real exports. `.gitignore` excludes `*.csv`, `data/`, `raw/` and `output/`. Test fixtures must use made-up guest data.
