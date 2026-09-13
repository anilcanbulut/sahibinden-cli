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

### Signing in

sahibinden serves a sign-in wall to any session without cookies. This is not bot
detection: a plain incognito window on the same connection gets the same wall.
So the browser profile has to be signed in once.

```powershell
sahibinden-cli login
```

A Chrome window opens. Sign in **with a burner account**, not your own — bans
here are IP-wide and self-releasing, so the account is not what absorbs the
damage, but there is no reason to attach your identity to this. Nothing is typed
for you and stdin is never read: the login is multi-step, and any navigation of
ours mid-flow cancels it, so the command just watches until a real page loads.
It then spends one request confirming the wall is actually cleared, so you are
not left guessing.

Sessions expire, and when one does *every* fetch comes back walled. `status`
says so without spending anything:

```
  session    : EXPIRED — last request was walled at 2026-08-23 19:25
               the browser session has expired — run `sahibinden-cli login`
```

### Signing in on one machine, fetching from another

`login` needs a screen to type into, which a server does not have. Copying
`browser-profile/` across does not help either: Chrome encrypts its cookie
store with keys the OS holds — App-Bound Encryption on Windows, the Keychain on
macOS, the login keyring on Linux — so the copy decrypts to nothing, and across
operating systems it will not even load.

What travels is the cookie set, read out of a browser that has already
decrypted it:

```powershell
# on the machine you are signed in on — opens no page, costs no requests
sahibinden-cli export-session --out session.json

# or, against a Chrome you already have open:
#   chrome.exe --remote-debugging-port=9222 --user-data-dir=%USERPROFILE%\sb-chrome
sahibinden-cli export-session --attach --out session.json
```

```bash
scp session.json user@host:~/sahibinden-cli/data/
ssh user@host 'sahibinden-cli import-session data/session.json'
```

Only cookies whose domain contains `sahibinden` are written, and the same
filter runs again on import — a session file is data from another machine, and
seeding whatever it happens to contain would push unrelated sites' cookies into
this browser. Expired cookies are dropped when the context is seeded, not when
the file is written, so a file that sat overnight degrades instead of failing.

The user agent travels with the cookies, because a session minted by Chrome on
Windows and replayed by Chromium on Linux otherwise announces a different
browser than the one that logged in.

`import-session` spends nothing and therefore proves nothing. Cookies are not
bound to an IP on most sites, but "most" is not "this one", and the next
`search` is what settles it: listings, or the wall.

On a headless host `launch()` picks headless automatically when there is no
`DISPLAY`, since headful Chrome there does not degrade — it fails to start.
Chrome still has to be installed: `libnss3` and `libnspr4` are hard
requirements and a minimal server image has neither.

`--attach` is the other half of the same problem. It drives a Chrome already
running at `127.0.0.1:9222` instead of launching one, so with a reverse tunnel
(`ssh -R 9222:127.0.0.1:9222 user@host`) the requests leave from *that*
machine's address — which matters when the site is Turkish and the server is
not.

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
| `panels` | no | the 13-part diagram: panel names, groups, coverage |
| `history` | no | price trail for one listing |
| `reparse` | no | re-run parsers over saved HTML |
| `status` | no | session, budget, cooldown, database stats (`-v` lists what is recorded) |
| `login` | 1 | sign in by hand in a real Chrome window, then verify the session |
| `export-session` | no | write this browser's sahibinden cookies to a portable file |
| `import-session` | no | install a session exported from another machine |
| `sql` | no | read-only SQL against the local database |
| `harvest` | no | rebuild the filter vocabulary from saved HTML |
| `brands` / `cities` | no | what the resolver accepts |
| `config` | no | show or persist settings |
| `capabilities` | no | full JSON description of every command |

## Filtering on individual panels

sahibinden's own paint filters are all-or-nothing: **Boyasız** means nothing
painted anywhere, **Değişensiz** nothing replaced anywhere. That throws away the
distinction that decides what a car is worth — a repainted rear bumper is a
car-park scrape, a changed front fender is an accident.

The diagram carries it per panel, so the CLI filters on it directly:

```powershell
# hood untouched, everything else as it comes
sahibinden-cli price --brand Volkswagen --model Passat --original hood

# the shape most people actually want: body original, bumpers may be painted
sahibinden-cli price --brand Volkswagen --model Passat `
    --original structural --not-changed bumpers

# spelled out, panel by panel
sahibinden-cli price --brand VW --panel roof=original `
                     --panel front_left_door=original,local_painted

# a tolerance instead of a rule
sahibinden-cli price --brand VW --max-painted 2 --max-changed 0
```

| Flag | Means |
|---|---|
| `--original PANELS` | untouched — no paint of any kind |
| `--not-painted PANELS` | local touch-up allowed, no full repaint |
| `--not-changed PANELS` | paint allowed, panel not replaced |
| `--panel PANEL=STATUS[,STATUS]` | the explicit form |
| `--max-painted N` / `--max-changed N` | counts across the whole car |

`PANELS` is one panel or a group: `hood` `kaput` `roof` `trunk` `front_left_door`
… or `all` `structural` `bumpers` `doors` `fenders` `left` `right` `front` `rear`
(Turkish names work too: `kapilar`, `camurluklar`, `sol`, `arka`). Statuses are
`original` `local_painted` `painted` `changed` — or `orijinal`, `lokal`,
`boyalı`, `değişen`. Two rules over the same panel keep the stricter one;
contradictory ones are an error. `sahibinden-cli panels` lists everything,
with how many stored cars each status appears on.

### Unchecked is not clean

Panel status lives on the **detail page**, one request per car — so it is only
known for listings whose details have been fetched. A car with no diagram never
passes a panel filter, and is never counted as failing one either:

```
  panel test   : 9 passed, 10 failed, 126 unchecked
                 the unchecked ones match everything else asked for; their
                 diagram is simply not fetched yet:
                   sahibinden-cli details --brand Volkswagen --model passat
```

Those 126 are the cars worth spending detail requests on. Folding them into
"failed" would hide them; folding them into "passed" would recommend cars nobody
has looked at.

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

This is also what a panel filter runs on. A `price --original hood` over a scope
whose details were never fetched reports every car as unchecked and matches
nothing — the filter is not wrong, the diagrams are simply not there yet.

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
