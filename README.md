# sahibinden-cli

Search second-hand car listings on [sahibinden.com](https://www.sahibinden.com)
from the terminal. You pass filters, it loads the matching search on sahibinden
right now, and prints the listings (year, km, price, location) along with the
price range and median.

```powershell
sahibinden-cli search --brand Volkswagen --model Passat --condition used --max-price 1500000
```

## Setup (once)

You need Python 3.10+ and Google Chrome.

**1. Install**

```powershell
pip install -e .
```

This puts `sahibinden-cli` on your PATH. Brands, provinces and filter values
ship with the code, so nothing else needs loading.

**2. Sign in**

sahibinden shows a sign-in page instead of listings to visitors who aren't
signed in. The CLI uses its own Chrome profile, and you sign that profile in
once:

```powershell
sahibinden-cli login
```

A Chrome window opens. Sign in by hand, using a spare account rather than your
personal one. The command finishes on its own once you're in. Sessions expire
after a while. When yours does, `sahibinden-cli status` says `EXPIRED` and you
run `login` again. To use the same sign-in on another PC, see
[Using another PC](#using-another-pc).

## Searching

```powershell
sahibinden-cli search --brand vw --model tiguan --city istanbul --min-year 2018 --fuel diesel --transmission automatic
```

What happens:

1. Your filters are turned into a sahibinden search URL.
2. Chrome opens that URL (off-screen, so you won't see a window) and reads up
   to 5 pages, which is about 100 listings, cheapest first.
3. The listings are printed and saved to `data/sahibinden.db`.

A search takes about a minute because the CLI waits between pages. Going faster
gets your IP blocked by sahibinden (see [Limits](#limits)).

Output from that search, run on 27 Sep 2026:

```
  url            : https://www.sahibinden.com/arazi-suv-pickup-volkswagen-tiguan/istanbul?a277_min=2018&a287=400&a279=33625&sorting=price_asc
  category       : Arazi, SUV & Pickup
  total on site  : 7
  pages fetched  : 1 of 1
  Yakıt / Motor Tipi: Dizel
  Vites          : Otomatik
  matched        : 8
  price range    : 1,975,000 TL - 3,800,000 TL
  median         : 2,485,000 TL

  id          year  km               price  location
  --------------------------------------------------------------
  1339324873  2018  167.000      1,975,000  İstanbul Bakırköy
  1178420169  2018  265.500      2,200,000  İstanbul Başakşehir
  1339915368  2018  168.000      2,200,000  İstanbul Bağcılar
  ...
```

**Checking it against the website:** the `url` line is the exact page the CLI
read. Open it in your browser. The `total on site` count and the listings
should match what you see there. The CLI can list one or two more than `total
on site`, because sahibinden inserts paid "promoted" listings into the results
without counting them. The CLI keeps those, as the website shows them too.

### Cars, SUVs and minivans

sahibinden keeps SUVs and pickups (Tiguan, Duster, T10X, Qashqai, Ranger) in a
different category from ordinary cars, with different filter ids. You don't
need to know which is which:

- **With `--model`**, the CLI looks the model up in the brand's own model list
  and searches the right category. The first time you search a brand, reading
  that list costs 1–2 extra requests. After that it's remembered.
- **With only `--brand`**, it searches the brand's cars and tells you if the
  brand also has SUVs. Add `--category suv` to search those instead.

## Filters

| Filter | Flag | Example |
|---|---|---|
| Brand, model | `--brand` `--model` | `--brand vw --model passat` |
| Car or SUV | `--category` | `--category suv` (only needed without `--model`) |
| City, district | `--city` `--district` | `--city istanbul --district kadikoy` |
| Year | `--min-year` `--max-year` | `--min-year 2018` |
| Kilometres | `--min-km` `--max-km` | `--max-km 150000` |
| Price (TL) | `--min-price` `--max-price` | `--max-price 1500000` |
| Second hand only | `--condition` | `--condition used` |
| Fuel | `--fuel` | `--fuel diesel` |
| Gearbox | `--transmission` | `--transmission automatic` |
| Body type | `--body` | `--body sedan` |
| Colour | `--color` | `--color white` |
| Seller | `--seller` | `--seller owner` (sahibinden), `--seller dealer` (galeriden) |
| Heavy-damage record | `--heavy-damage` | `--heavy-damage no` |
| No painted or replaced parts | `--undamaged` | or one of them: `--unpainted`, `--unchanged` |
| Posted recently | `--listed-within` | `--listed-within "son 24 saat"` |
| Word in the title | `--keyword` | `--keyword "cam tavan"` |

There are more filters: engine size, horsepower, drive, plate, warranty and
trade-in. `sahibinden-cli filters` lists every filter and the values it
accepts (`filters --category suv` for SUVs).

A full example:

```powershell
sahibinden-cli search --brand Volkswagen --model Passat --city istanbul `
    --condition used --min-year 2018 --max-km 150000 --max-price 1500000 `
    --fuel diesel --transmission automatic --seller owner
```

How the values work:

- **English or Turkish.** `--fuel diesel` and `--fuel dizel` are the same
  filter. A shortened value also works as long as it matches only one option.
- **Repeat a flag to tick several boxes**, as on the website:
  `--fuel diesel --fuel hybrid --color white --color black`. The exceptions are
  `--seller`, `--listed-within` and `--tradeable`, which take one value each.
- **Wrong values fail loudly.** A brand, model, city or filter value the CLI
  doesn't recognise stops the search with the valid values or a "did you mean",
  before anything is fetched. A filter is never skipped without telling you.
- **`--district` isn't checked in advance.** If it's misspelled, the search
  returns `total on site : 0` and `NO RESULTS — check spelling`.
- `--district` needs `--city`.
- `--currency usd` makes `--min-price` and `--max-price` mean dollars.

### Other search options

| Option | What it does |
|---|---|
| `--sort date_desc` | newest listings first. The default is `price_asc` (cheapest first); `price_desc` and `date_asc` also work. |
| `--pages 10` | read more pages. Each page is 20 listings and costs 1 request. The default is 5 ([change it](#changing-the-limits)). |
| `--limit 50` | print more rows. The default is 20. |
| `--url "<link>"` | skip the filter flags. Set your filters on the website, copy the address bar and paste it here. Any other filter flags are ignored. |
| `--json` | print full data for every listing, including its title and link |

### When a search doesn't get everything

If more listings match than fit in 5 pages, the output says so:

```
  NOTE           : partial sweep — these are the first ~100 of 5636 by price_asc, not the whole market.
```

The price range and median then describe only those 100 cheapest listings, not
the whole market. Narrow the filters (year, km, price, city) or raise `--pages`.

## Limits

sahibinden blocks your IP when it sees too many requests. The block shows a
page saying "Olağan dışı erişim tespit ettik" and lasts about an hour. It
applies to your normal browsing too. So the CLI limits itself:

- **40 requests a day**, across every command. One request is one page of 20
  listings, so a default search costs up to 5. The count resets at midnight.
- **12–25 seconds between requests**, picked at random each time.
- **At most 15 listing pages per `details` run**, so reading damage records
  can't use up the whole day.
- **It backs off when sahibinden pushes back.** It stops and refuses to fetch
  for a while: 90 minutes the first time, 4 hours if it happens again within a
  week, 12 hours after that. Each block in the past week also multiplies the
  wait between requests by 1.75, so after one block it's 21–44 seconds.
  You'll see `refused:` and exit code 2. Wait it out instead of retrying, or
  [end it early](#ending-a-cooldown-early).

Even within those limits, many searches back to back can trigger a block. On
27 Sep 2026 one came after 19 requests in about 25 minutes. Spread your
searches out, and prefer one well-filtered search over several broad ones.

`sahibinden-cli status` shows whether you're signed in, how many requests are
left today, the limits in effect, and any cooldown.

### Changing the limits

| Setting | Default | What it limits | For one run |
|---|---|---|---|
| `daily_budget` | 40 | requests per day, all commands together | `--daily-budget 60` |
| `min_delay` | 12 | shortest wait between requests, in seconds | `--min-delay 20` |
| `max_delay` | 25 | longest wait between requests, in seconds | `--max-delay 40` |
| `default_pages` | 5 | pages a search reads | `--pages 10` on `search` |
| `detail_budget` | 15 | listing pages one `details` run opens | none. `details --limit 5` can lower it, not raise it. |

**To change a limit for good**, save it with `config --set`, once per setting:

```powershell
sahibinden-cli config --set daily_budget=60 --set min_delay=20 --set max_delay=40
```

This writes `data/config.json`. It stays on this PC (it isn't committed), so
set it again on another PC. `sahibinden-cli config` prints the current
settings. Check them after a change: a misspelled name fails with an error,
but a value that isn't a number is ignored and the default is used.

**To change a limit for one run**, use the flag in the last column.
`--daily-budget`, `--min-delay` and `--max-delay` work with every command:

```powershell
sahibinden-cli search --brand vw --model passat --daily-budget 60 --min-delay 20 --max-delay 40
```

**To change a limit for one terminal window**, set an environment variable
named `SAHIBINDEN_` plus the setting in capitals:

```powershell
$env:SAHIBINDEN_DETAIL_BUDGET = 25
```

When a limit is set in more than one place, a flag beats an environment
variable, which beats `config --set`.

Things to know:

- **Raising the budget on a day you've used it doesn't reset the count.**
  After 40 requests, `--daily-budget 60` gives you 20 more.
- **`--pages` has ceilings of its own.** A search never reads more pages than
  today's budget has left, or more than 50 (1,000 listings), which is as far
  as sahibinden goes for one search.
- **Looser limits mean more blocks.** The defaults aren't generous: the block
  on 27 Sep 2026 came well inside them. Raise the budget or shorten the waits
  in small steps.
- **The cooldown lengths and the 1.75× slow-down can't be changed** with a
  setting. They're fixed in `src/sahibinden/fetch/guard.py`. You can still end
  a cooldown whenever you like (below).
- `config` also lists settings that aren't limits (`quiet_start`, `quiet_end`,
  `max_offset`, `accept_languages`, `db_path`, `profile_dir`). Leave those as
  they are.

### Ending a cooldown early

```powershell
sahibinden-cli reset-cooldown
```

This ends the cooldown now, so you can search again straight away. It costs no
requests. It prints how long was left and what started the cooldown.

The block still counts for a week. Waits between requests stay 1.75× longer,
and the next block gets the longer cooldown. To forget the past week's blocks
as well, so waits go back to 12–25 seconds and the next cooldown is 90 minutes
again:

```powershell
sahibinden-cli reset-cooldown --forget-blocks
```

Ending a cooldown doesn't lift sahibinden's block. If sahibinden is still
blocking you, the next search is refused again and starts a longer cooldown.
To check first, open sahibinden.com in your normal browser. If it shows the
"Olağan dışı erişim tespit ettik" page, the block is still on, so keep waiting.

## Using another PC

You don't have to sign in on every PC. This repo is private, so the sign-in
is committed with it as `session.json`. On a new PC (Python 3.10+ and Google
Chrome installed):

```powershell
git clone git@github.com:anilcanbulut/sahibinden-cli.git
cd sahibinden-cli
pip install -e .
sahibinden-cli import-session session.json
sahibinden-cli search --brand vw --model passat --pages 1
```

If that search prints listings, the sign-in carried over. If it asks you to
sign in or is refused, run `sahibinden-cli login` on that PC instead. On a PC
that already has the repo, `git pull` and then `import-session`.

To refresh the file, on the PC that is signed in (costs no requests):

```powershell
sahibinden-cli export-session --out session.json
git commit session.json -m "Refresh the exported session"; git push
```

Things to know:

- **`session.json` is a working login** for the sahibinden account. Keep the
  repo private. Git keeps every committed copy in its history, so if the repo
  is ever shared, sign that account out on sahibinden.
- **It goes stale after about a day.** sahibinden's `st` sign-in cookie expires
  24 hours after you sign in, and searching doesn't renew it. If the file is
  older than that, export and push a fresh one before using it elsewhere.
- **A different network may not work.** sahibinden ties part of the session to
  the network it was created on. If the other PC's first search comes back
  asking you to sign in, or blocked, run `sahibinden-cli login` there instead.

The browser profile folder (`browser-profile/`) can't be copied instead:
Chrome encrypts it with keys tied to the PC.

## Other commands

Every search saves what it finds, so these commands work offline and cost
nothing:

| Command | What it shows |
|---|---|
| `price --brand Volkswagen --model Passat` | min, quartiles, median and max price across everything saved for that car. Takes `--city`, `--min-year`, `--max-km` and more. |
| `listing <id>` | everything saved about one car, including price changes since it was first seen |
| `status` | sign-in state, requests left today, and what's in the database |
| `reset-cooldown` | ends a cooldown now ([more](#ending-a-cooldown-early)) |
| `filters`, `brands`, `cities` | the values the filter flags accept |

These commands use requests:

| Command | What it does |
|---|---|
| `details --brand Volkswagen --model Passat` | opens each saved car's listing page to read its damage record (painted or replaced panels, ağır hasar). 1 request per car, at most 15 per run. |
| `login` | signs in again when `status` says `EXPIRED` |

Run `sahibinden-cli <command> --help` to see all the options for a command.

## Tests

```powershell
python tools/test_filters.py; python tools/test_session.py; python tools/test_store.py; python tools/test_panels.py
```

These run offline and spend no requests.
