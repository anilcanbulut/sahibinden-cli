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

# Damage records — aim them, one request each
sahibinden-cli details --brand Volkswagen --model Passat --min-year 2018 --max-km 200000

# Everything known about one car
sahibinden-cli listing 1331795270

# Budget, cooldown, database contents
sahibinden-cli status
```

`search` fetches; `price`, `listing` and `history` read the local database and
cost nothing.

### Filters

Everything sahibinden's own sidebar offers, in the order the website draws it.
To see the values each one accepts:

```powershell
sahibinden-cli filters
```

| Sidebar | Flags |
|---|---|
| Adres | `--city` (il), `--district` (ilçe) |
| Fiyat | `--min-price` `--max-price` `--currency TL\|USD\|EUR\|GBP` |
| Yıl | `--min-year` `--max-year` |
| Yakıt / Motor Tipi | `--fuel` |
| Vites | `--transmission` |
| Araç Durumu | `--condition` |
| KM | `--min-km` `--max-km` |
| Kasa Tipi | `--body` |
| Motor Gücü | `--power` |
| Motor Hacmi | `--engine` |
| Çekiş | `--drive` |
| Renk | `--color` |
| Garanti | `--warranty` |
| Ağır Hasar Kayıtlı | `--heavy-damage` |
| Plaka / Uyruk | `--plate` |
| Kimden | `--seller` |
| Takaslı | `--tradeable` |
| İlan Tarihi | `--listed-within` |
| Fotoğraf, Video | `--has-video` `--has-clip` |
| Boya, Değişen Parça | `--unpainted` `--unchanged` (`--undamaged` = both) |
| Kelime ile filtrele | `--keyword` `--keyword-in-description` |

Values accept English or Turkish, partial or full: `--fuel diesel` and
`--fuel Dizel` both give `a17=22`; `--heavy-damage no` gives `a116445=1263354`.
An unrecognised value is an error listing the valid ones, never a dropped
filter — a filter silently ignored would return a confident answer about a
market you did not ask for.

**Checkboxes repeat, radios do not.** Where sahibinden draws a checkbox list,
the flag can be given more than once and the parameter repeats exactly as the
site's own links do:

```powershell
sahibinden-cli search --brand Volkswagen --model Passat `
    --fuel diesel --fuel hybrid --color white --color black
```

Where it draws radio buttons (`--seller`, `--listed-within`, `--tradeable`) a
second value is an error rather than two parameters and a guess about which one
the site will honour. `filters` marks each group `[repeatable]` or
`[one value]`.

`--currency` picks the unit `--min-price` / `--max-price` are read in, so
`--currency usd --max-price 30000` is thirty thousand dollars. Listings the
seller priced in a foreign currency keep that unit: the amount is stored, the
lira column is left empty rather than filled with a number forty times too
small, and both `search` and `price` say how many rows sit outside their lira
figures.

`--district` needs `--city`, since sahibinden addresses a district as part of
its province (`/ankara-cankaya`). Like `--model` it is slugified and passed
through — `total_results` is what confirms it matched.

### Where the filter ids come from

Nothing about the filters is written down in this repo. `harvest` reads them
out of saved sahibinden pages — the attribute ids, every value, whether the
group is a checkbox or a radio, the boolean switches and the currency ids —
and merges them into `data/taxonomy.json`:

```powershell
sahibinden-cli harvest              # samples/ and data/raw/, no network
sahibinden-cli harvest --dry-run    # what it would learn, without writing
```

The committed pages under `samples/` are enough to bootstrap a fresh clone, so
the filters work before the first fetch. Run it again after a sweep to pick up
values that page happened to expose.

Guessing is not the fallback, because a wrong id is not an error: sahibinden
answers HTTP 200 with a filtered-looking page for an attribute id that belongs
to something else, so an invented id returns confidently wrong numbers.
Faceted search also hides the filter you already have applied, so values that
were active on a captured page are recovered from the applied-filter chips
(each chip's link removes exactly one parameter, which identifies it).

Anything a category has no harvested id for is applied to the parsed rows
instead and reported as `post_filters`, so a caller never silently gets a wider
market than it asked for.

### Narrowing is yours to do

A sweep fetches **5 pages by default** (~100 listings), whatever the search
returns. It does not widen on its own, and it does not narrow on your behalf —
use the same filters you would use on the website:

```powershell
sahibinden-cli search --brand Fiat --model Egea `
    --min-year 2019 --max-km 150000 --max-price 700000 --city Ankara --undamaged
```

Well-filtered searches are also cheaper and better: one filtered Passat query
(`--min-year 2015 --max-price 900000`) cut 3,228 listings to 14 and completed in
a single request, where the unfiltered version was 162 pages.

When a sweep does not reach the end, the output says so — those rows are the
cheapest ~100 by whatever `--sort` you used, not a sample of the market, and
reading a median off them would be wrong.

Raise the cap per run with `--pages N`, or permanently:

```powershell
sahibinden-cli config --set default_pages=10
```

## Commands

| Command | Network | Purpose |
|---|---|---|
| `search` | yes | resolve brand/model/city → URL, sweep, store |
| `details` | yes | damage records; one request per listing |
| `nightly` | yes | refresh tracked searches (oldest first, capped), then spend detail budget |
| `price` | no | price percentiles, damage split, turnover |
| `listing` | no | full record: row, price history, damage, description |
| `history` | no | price trail for one listing |
| `reparse` | no | re-run parsers over saved HTML |
| `status` | no | budget, cooldown, database stats (`-v` lists what is recorded) |
| `sql` | no | read-only SQL against the local database |
| `harvest` | no | rebuild the filter vocabulary from saved HTML |
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

## Input matching

Brand and city arguments are forgiving. Matching is tried in order of
confidence: alias, exact, unique prefix, then fuzzy.

| You type | Resolves to | Why |
|---|---|---|
| `VOLKSWAGEN`, `vOlKsWaGeN` | Volkswagen | case-insensitive |
| `volks`, `merc`, `ist` | Volkswagen, Mercedes-Benz, İstanbul | unique prefix |
| `vw`, `mb` | Volkswagen, Mercedes-Benz | alias |
| `wolkswagen`, `renaut`, `toyata` | Volkswagen, Renault, Toyota | typo tolerance |
| `sanliurfa`, `Škoda` | Şanlıurfa, Skoda | Turkish characters folded |
| `b` | *error* — ambiguous: BMW, BYD | a prefix must be unique |

An ambiguous prefix is an error listing the candidates, never a silent pick —
guessing would return a confident market summary for the wrong car.

`--model` is not validated (sahibinden loads model lists per brand over XHR), so
it is slugified and passed through. `total_results` tells you whether it matched;
`search` reports `NO RESULTS — check spelling` when it did not.

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

## Aim the detail budget

`details` queues cheapest-first *within the scope you give it*. Unscoped that
backfires: one broad `--max-price 900000` sweep drags in the bottom of the
market, and a full day's budget went on 1980s cars with broken gearboxes.

```powershell
sahibinden-cli details --brand Volkswagen --model Passat --min-year 2018 --max-km 200000
sahibinden-cli details --listing-id 1331795270
```

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
