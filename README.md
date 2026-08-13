# sahibinden-cli

Query sahibinden.com vehicle listings from a local cache. Built to be called by
a coding agent, so every command speaks `--json` and the exit codes mean
something.

```
sahibinden-cli price --brand Volkswagen --model Passat --json
```

## Install

```powershell
pip install -e .
```

Puts `sahibinden-cli` on PATH — works from any directory, any terminal.

### One-time session setup

sahibinden serves a sign-in wall to any session without cookies. This is not bot
detection: a plain incognito window on the same connection gets the same wall.
So the browser profile has to be signed in once.

```powershell
python tools\handshake.py
```

A Chrome window opens. Sign in **with a burner account**, not your own — bans
here are IP-wide and self-releasing, so the account is not what absorbs the
damage, but there is no reason to attach your identity to this. The script
detects the completed login on its own and verifies the wall is cleared.

Re-run it if `status` starts reporting `login_wall`; sessions expire eventually.

## Everyday use

```powershell
# What is a 2019+ Passat under 900k worth?
sahibinden-cli search --brand Volkswagen --model Passat --min-year 2019 --max-price 900000

# Market summary from everything stored — no network, no budget cost
sahibinden-cli price --brand Volkswagen --model Passat

# Damage records for the cheapest candidates (1 request each)
sahibinden-cli details --limit 5

# Everything known about one car
sahibinden-cli listing 1331795270

# Budget, cooldown, database contents
sahibinden-cli status
```

`search` fetches; `price`, `listing` and `history` read the local database and
cost nothing.

## Commands

| Command | Network | Purpose |
|---|---|---|
| `search` | yes | resolve brand/model/city → URL, sweep, store |
| `details` | yes | damage records; one request per listing |
| `nightly` | yes | refresh every tracked search, then spend detail budget |
| `price` | no | price percentiles, damage split, turnover |
| `listing` | no | full record: row, price history, damage, description |
| `history` | no | price trail for one listing |
| `reparse` | no | re-run parsers over saved HTML |
| `status` | no | budget, cooldown, database stats |
| `sql` | no | read-only SQL against the local database |
| `brands` / `cities` | no | what the resolver accepts |
| `config` | no | show or persist settings |
| `capabilities` | no | full JSON description of every command |

## Why damage needs three sources

The 13-panel diagram, the `agir_hasar_kayitli` flag and the seller's description
routinely disagree. A real example from this dataset:

```
1331795270   718,500 TL   2016 Passat   panels: 13/13 original
             agir hasar kayitli: evet
             description: "SİGORTA ŞİRKETİNDEN ALINDIĞI İÇİN AĞIR HASAR İŞLENMİŞTİR"
```

An insurance write-off, repaired well enough that every panel reads original —
and the cheapest car in its group. Ranking on the diagram alone puts exactly the
wrong car first. All three are captured and shown together; `?` means not yet
checked, never "clean".

## Looking inside the database

`data/sahibinden.db` is plain SQLite; any GUI will open it. From the CLI:

```powershell
sahibinden-cli sql --tables
sahibinden-cli sql --schema
sahibinden-cli sql "SELECT brand, model, COUNT(*) n, MIN(last_price_try) FROM listings GROUP BY brand, model"
```

The connection is opened read-only at the URI level, so writes are refused by
sqlite itself rather than by pattern-matching the query text.

| Table | Holds |
|---|---|
| `listings` | one row per car: price, year, km, brand, first/last seen, delisted |
| `price_observations` | price history — written only when a price changes |
| `listing_details` | damage diagram, agir hasar flag, description, spec |
| `sweep_runs` | every crawl: url, pages fetched, whether it was complete |
| `sweep_appearances` | which listings appeared in which sweep (drives delisting) |

## Rate limiting

Bans on this site are rate-triggered, IP-wide, and lift on their own after about
an hour. Since the ban costs you your own browsing too, the tool governs itself:

- **daily budget** (default 100) across all commands
- **12–25s jittered spacing**, enforced from the last request on record — so it
  holds across separate commands and separate processes, not just within a run
- **never retries.** A challenge ends the run.
- **cooldown, not shutdown.** A trip pauses 90 min, then 4 h, then 12 h, and
  clears itself. Each trip permanently widens spacing ×1.75 for a week, so the
  crawler settles below the ceiling instead of rediscovering it.

```powershell
sahibinden-cli config --set daily_budget=150     # persisted
sahibinden-cli search --daily-budget 20 ...      # this run only
$env:SAHIBINDEN_DAILY_BUDGET=20                  # this shell
```

Precedence: defaults → `data/config.json` → `SAHIBINDEN_*` → CLI flags.

## Scheduled refresh

A Windows task `sahibinden-cli-nightly` runs `nightly` at 03:15 daily. Complete
sweeps are what let the store detect delisting — the closest observable proxy
for a sale — and a ban at 3am costs nothing.

```powershell
Start-ScheduledTask sahibinden-cli-nightly       # run now
Get-ScheduledTask sahibinden-cli-nightly         # check
```

## For agents

```
sahibinden-cli capabilities        # every command and argument, as JSON
```

Exit codes:

| Code | Meaning |
|---|---|
| 0 | success |
| 1 | error — bad arguments, unknown brand, listing not in database |
| 2 | **refused** — budget spent or cooling down. Wait; do not retry |

A `2` is not a failure. It means the tool declined on purpose and retrying makes
things worse.

Filters that sahibinden cannot express in a URL come back as `post_filters` on
the result and are applied to the parsed rows, so a caller never silently gets a
wider market than it asked for.

## Notes

- Year/km filter ids (`a5`, `a4`) are harvested from sahibinden's own search
  form, never guessed. A wrong id still returns HTTP 200 and a filtered-looking
  page — guessing produces confidently wrong numbers rather than an error.
- Every fetched page is stored raw before parsing, so `reparse` backfills
  existing data after a parser fix instead of spending requests.
- `browser-profile/`, `data/` and `fingerprint/` are gitignored. The first holds
  live session cookies; do not commit it.
