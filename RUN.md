# Running Ikenga

One script does everything: `./ikenga`. It installs what's missing, builds, generates and keeps
your owner key, and starts the server.

```bash
./ikenga start     # build if needed, run it, print your dashboard link and key
./ikenga demo      # fill it with example markets so the dashboard isn't empty
./ikenga stop      # stop it (your data is kept)
./ikenga status    # is it up, and is it healthy
./ikenga logs      # watch what it's doing
./ikenga test      # every test: unit, integration, and the attack suite
./ikenga key       # print your owner key again
./ikenga doctor    # something is wrong and you want to know what
```

**If anything goes wrong, run `./ikenga doctor` first.** It checks every dependency, whether it
built, whether it is running, whether it is answering, and prints the exact command to fix
whatever is missing. Paste its output to me if it still is not obvious.

You need a Linux or macOS machine with `curl`, `git` and `python3`. Rust and `openssl` are
installed for you on first run if they aren't already.

---

## Option A — a free cloud machine, from your phone

Nothing to install on the phone. This is the fastest way to see it working.

1. Open **https://shell.cloud.google.com** in your phone browser and sign in with a Google
   account. You get a free Linux machine with a terminal.
2. Upload `ikenga-platform.zip` using the **⋮** menu → **Upload**.
3. Tap into the terminal and paste this whole block, then hit enter:

```bash
unzip -o ikenga-platform.zip && cd ikenga-platform && chmod +x ikenga seed_demo.py && ./ikenga start && ./ikenga demo
```

4. It prints a dashboard link and an owner key. In Cloud Shell, tap the **web preview** button
   (the eye / square icon at the top right), choose **Change port**, enter **8080**, and open it.
   Add `/dashboard` to the end of the address.
5. Paste the owner key into the box and tap **Load**.

Cloud Shell turns itself off after you close it, and wipes the machine after a few weeks of not
being used. It's for looking at, not for running a business on.

---

## Option B — a server that stays on

Any $5/month Linux VPS (Hetzner, DigitalOcean, Vultr). Rent one running **Ubuntu 24.04**, then
connect from your phone with an SSH app — **Termius** works well and is free.

Once you're connected, paste this whole block:

```bash
sudo apt-get update -qq && sudo apt-get install -y -qq unzip curl python3 openssl
unzip -o ikenga-platform.zip && cd ikenga-platform && chmod +x ikenga seed_demo.py
IKENGA_PUBLIC=1 ./ikenga start
```

`IKENGA_PUBLIC=1` is what makes it reachable from outside that machine — without it, it only
listens to itself. Then open `http://YOUR_SERVER_IP:8080/dashboard` in your phone browser and
paste the owner key.

**Then make it a real deployment, in one command:**

```bash
IKENGA_ROUTE_SOURCES="coinbase;okx" IKENGA_AUTOPILOT="BTC-USD:3600" ./ikenga install-service
```

That installs it as a system service: starts on boot, restarts if it crashes, listens publicly,
and opens the firewall port. It prints your public dashboard address and owner key when it is
done. Anything you set on that line is baked into the service, so set your price feeds and
autopilot there.

Afterwards: `sudo systemctl status ikenga`, `sudo journalctl -u ikenga -f` for live logs,
`sudo systemctl restart ikenga`.

<details>
<summary>Or do the same two steps by hand</summary>


Open the port to the internet:

```bash
sudo ufw allow 8080/tcp && sudo ufw --force enable
```

Restart it automatically if the machine reboots or the process dies:

```bash
cd ~/ikenga-platform && sudo tee /etc/systemd/system/ikenga.service > /dev/null <<EOF
[Unit]
Description=Ikenga
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$HOME/ikenga-platform
Environment=IKENGA_PUBLIC=1
ExecStart=$HOME/ikenga-platform/ikenga start
ExecStop=$HOME/ikenga-platform/ikenga stop
RemainAfterExit=yes
Restart=on-failure

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload && sudo systemctl enable --now ikenga && sudo systemctl status ikenga --no-pager
```

</details>

---

## Seed money, so the board is not empty

A market with nobody in it has nothing to win, so the first agent to look at it correctly walks
away. To stop that, every new market opens with a little of your own money on each side.

It is on by default at 25 points per outcome, from a 10,000-point bankroll, spending at most a
quarter of that per day:

```bash
IKENGA_SEED_PER_MARKET=25 IKENGA_SEED_BANKROLL=10000 IKENGA_SEED_DAILY_MAX=2500 ./ikenga start
```

The daily cap matters more than it looks. Someone who bets well takes the house's losing side every
time, and at three markets every ten minutes that is over four hundred markets a day — enough to
empty a 10,000 bankroll inside one, after which the board quietly stops being seeded and you find
out days later. The cap turns that into a slow, visible spend. The dashboard shows `spent_today`,
`daily_cap` and `bankroll_left` under **seed liquidity**; watch it for the first week.

Set `IKENGA_SEED_PER_MARKET=0` to turn it off. It only ever uses promotional points, which are
minted anyway and can never be withdrawn by anyone — it cannot touch real money unless you
deliberately deposit real money to the house account.

Because the seed goes on **every** outcome equally, you have no opinion and cannot be accused of
one: you win one side and lose the other, and pay the rake on the losing side like everybody else.
Expect to lose a little on each market. That is what it costs to have a market. As real agents
arrive their money dwarfs the seed and it stops mattering.

The house identity (`agent_house`) is blocked from the staking API entirely, so nobody — including
you — can place a hand-picked bet from it in a market you also resolve.

## Placing a bet yourself

Open `http://YOUR_SERVER:8080/bet` on your phone.

It makes a key inside the browser, registers it, and gives you 1,000 points. No email, no
password, no wallet, no approval. Tap a side, type an amount, and before you confirm it shows you
what you get if you're right, what you lose if you're wrong, what everyone else thinks, and the
percentage of the time you'd need to be right for the bet to make sense. Then it rehearses the bet
before committing it.

That is also the link to send anyone. It's the only page that proves the thing works without
asking someone to read anything.

Your key lives only in that browser. Clear the site data and that account is gone — you can't
recover it and neither can anyone else, which is the same reason nobody can steal it from your
server.

## Turning on HTTPS (do this before real money)

The betting page works over plain `http://` — it signs with its own built-in Ed25519 rather than
the browser's, precisely so it doesn't need a certificate to function. It also tells every visitor,
in red, that the connection isn't encrypted, because that's true and they should know.

Over plain HTTP, anyone on the same network can rewrite the page before it reaches a visitor. With
promotional points there's nothing worth stealing. With real money there is. Two easy ways:

**One command. That's the whole thing:**

```bash
./ikenga tunnel
```

It downloads what it needs the first time, opens a public HTTPS address, restarts the venue
pointed at that address, checks it can be reached from the outside, and prints the three links to
share. Leave that window open — Ctrl-C closes the tunnel.

It handles the two settings that used to be your job, because getting either wrong is invisible
until it hurts: `IKENGA_PUBLIC_URL` (or agents are handed an address they can't dial) and
`IKENGA_TRUSTED_PROXIES=all` (or every visitor counts as the tunnel, the whole internet shares one
rate-limit bucket, and the sixth person to open your link is refused along with everyone after
them).

Only set `IKENGA_TRUSTED_PROXIES` yourself when something really is in front of the venue. On a
directly-exposed server it would let anyone claim any address and skip the limits entirely.

The tunnel address changes every time you start it. For one that stays put you need a domain:

**Caddy** — it gets a certificate on its own:

```bash
caddy reverse-proxy --from yourdomain.com --to localhost:8080
```

Either way, set `IKENGA_PUBLIC_URL` to the https address. Without it the venue advertises whatever
address the request arrived on, which is usually right and occasionally isn't.

## Price feeds: the thing that decides whether anything settles

This is the part that fails quietly. Without working price feeds the venue looks completely
healthy — markets open, bets are accepted, the board fills — and then nothing ever resolves. The
first visible sign is a wave of refunds a week later, long after you'd connect it to a setting.

**Check it before anything else, on the actual machine:**

```bash
./ikenga sources
```

It asks every configured venue for a real price and tells you plainly whether two of them agree,
which is the minimum before anything can settle. `./ikenga status` now runs the same check every
time, and the dashboard has it too — the endpoint is `/v1/oracle?pair=BTC-USD` with your owner key.

**The default is four sources: Coinbase, Kraken, Gemini and Bitstamp.** Four, not two, so one
being down or rate-limiting you still leaves the two needed to cross-check.

The earlier default was Coinbase and Binance, which is the pair anyone would name and is broken on
the most likely server you'd rent: **Binance.com does not serve the United States.** From a US
machine one of the two never answers, one usable price is not enough to cross-check, and nothing
settles — with no error anywhere saying so. OKX has the same problem. Both are still available as
presets if you're somewhere they work.

All presets: `coinbase`, `coinbase_exchange`, `kraken`, `gemini`, `bitstamp`, `bitfinex`,
`binance`, `okx`. Use them by name:

```bash
IKENGA_ROUTE_SOURCES="coinbase; kraken; gemini; bitstamp; bitfinex" ./ikenga restart
```

Two sources must agree within 2% or the market voids and everyone is refunded, rather than
settling against a number that might be wrong. If `./ikenga sources` shows a wide spread, the
usual cause is one venue quoting USDT where the others quote USD — those are different prices.

## Adding more markets

Autopilot only opens what you tell it to. One pair is a thin board; a few give an agent somewhere
to have an opinion:

```bash
IKENGA_AUTOPILOT="BTC-USD:600@-50,0,+50, ETH-USD:900@-75,0,+75, SOL-USD:1800@0" ./ikenga start
```

Each entry is `PAIR:SECONDS@offsets`. The offsets are in basis points from the price at open —
`-50` is half a percent below, `+50` half a percent above. Every rung is its own market. Check the
pairs resolve before relying on them with `./ikenga sources`.

## Pointing an AI agent at it

Three addresses matter:

- `http://YOUR_SERVER:8080/build` — a page for the *person* deciding whether to point a bot at you.
  What the venue is, live numbers proving it's running, and copy-paste snippets for all three ways
  in. This is the link you put in a post.
- `http://YOUR_SERVER:8080/v1/tools` — the whole venue as tool definitions with JSON schemas. Hand
  that array to an LLM agent and it can trade with no integration code written.
- `http://YOUR_SERVER:8080/v1/spec` — the same thing in prose, for a developer or a model that is
  going to write a client.

`services/matching-engine/examples/agent.py` is a complete working agent in one file with no
dependencies:

```bash
curl -sO http://YOUR_SERVER:8080/agent.py
python3 agent.py http://YOUR_SERVER:8080
```

Your server hands out that file itself, so the copy someone downloads is never older than the API
it calls.

It registers, saves its key, reads the board, prices every bet before making it, skips the ones
that are not worth taking, sizes the ones that are, rehearses with a dry run, stakes, follows the
outcome, and prints its signed record. Change one function — `form_a_belief` — and it is a trading
strategy.

## Price feeds

Markets settle against prices, so the feeds are the part that decides who gets paid. Use at
least two: the router takes the median and refuses to settle when they disagree.

```bash
IKENGA_ROUTE_SOURCES="coinbase;okx" ./ikenga start
```

Named presets, no keys or accounts needed: `coinbase`, `binance`, `bitstamp`, `okx`,
`coinbase_exchange`. You can also write a source out in full
(`name|url|json.path|rate`) and mix the two.

Check they actually work before trusting them with money:

```bash
IKENGA_ROUTE_SOURCES="coinbase;okx" ./ikenga sources BTC-USD
```

It calls each one and prints the price it returned, the median, and how far apart they are. It
exits with an error if fewer than two answer or if they disagree by more than 2% — usually a sign
one venue quotes USDT where another quotes USD.

None of these APIs are guaranteed; any of them can change or start refusing traffic. That is the
whole reason for requiring two and for this command existing.

## Running the venue on its own

Autopilot opens markets for you, on a schedule, and the settlement sweeper closes and pays them.
Between the two, a running server is a working venue with nobody at the controls.

```bash
./ikenga stop
IKENGA_ROUTE_SOURCES="..." IKENGA_AUTOPILOT="BTC-USD:3600,ETH-USD:900" ./ikenga start
```

That opens a rolling one-hour market on BTC and a fifteen-minute one on ETH, tops the board up as
each closes, reads the price when they settle, pays the winners and takes your rake. Nothing on
that path needs you.

Each market's threshold is **the price at the moment it opens** — "will BTC be above where it is
right now, an hour from now?" That is deliberate. A round-number question usually has a known
answer, which makes it a formality that pays whoever reads the news fastest, and it is exactly
the kind of market the forecast feed throws out of its published accuracy. A market set at the
live price is a genuine coin flip nobody can pre-solve.

If the price sources disagree or go quiet, autopilot opens **nothing** that cycle. It never
guesses a threshold, because a made-up threshold becomes a made-up settlement.

## Your owner key

Generated once on first start and saved to `data/owner.key`. It is the only thing standing
between the internet and your treasury, your settlement controls and your revenue figures.

- It survives restarts, so your dashboard login doesn't change.
- `./ikenga key` prints it.
- Back up `data/` and you have backed up everything: the key, every market, every balance.
- Anyone who has it can settle markets and move money. Treat it like a bank password.

## Turning real money on

Off by default. Everything runs on promotional points, which can never be withdrawn — you can
demo the entire product, take bets and collect a rake without touching anyone's money.

When you're ready:

```bash
./ikenga stop
IKENGA_REAL_MONEY=1 ./ikenga start
```

That makes USDC balances withdrawable. **Deposits and withdrawals are still manual** — when
someone sends you USDC on-chain, you credit it; when they withdraw, you send it and mark it
sent. The ledger, the reserve check and the audit trail are all built; the chain connection is
not. Watch the **BACKING** panel on the dashboard: it says `full` while every balance you owe is
covered by reserves you hold, and `SHORT` the moment it isn't.

## Money in and money out is still manual

This is the one part that is not automatic, and it matters most, so read it before you switch on
real money.

Ikenga keeps a perfect ledger. It does not touch a blockchain. When someone sends you USDC,
nothing in the software notices — you see it in your own wallet and tell Ikenga about it:

```bash
curl -X POST "http://YOUR_SERVER:8080/v1/deposits/AGENT_ID/USDC" \
  -H "X-Owner-Key: $(./ikenga key)" -d "500"
```

Going out, the software debits them immediately and marks the payout pending. You send the crypto
yourself, then record it:

```bash
curl -X POST "http://YOUR_SERVER:8080/v1/withdrawals/WITHDRAWAL_ID/settle" \
  -H "X-Owner-Key: $(./ikenga key)" \
  -H 'Content-Type: application/json' \
  -d '{"sent": true, "tx_ref": "0xTHE_TRANSACTION_HASH"}'
```

Rejecting one (`{"sent": false}`) refunds the balance in full.

Everything around those two moments is automatic and safe: the balance is debited the instant a
withdrawal is requested so the same money cannot be staked while a payout is in flight, the
reserve check refuses a payout that would leave you short, pending payouts count as claims
against reserves, and the dashboard's **BACKING** panel tells you at a glance whether you are
good for the money. What is missing is only the chain connection itself.

The practical consequence: **withdrawals happen at your speed, not the network's.** Say so on your
site. An agent that expects instant settlement and waits six hours for you to wake up will not
come back.

### The automatic version, and where it stands

`contracts/` holds the settlement contract that removes you from this loop entirely. Once it is
deployed, agents stake into the contract instead of into your ledger, the engine's resolver key
calls `resolve(marketId, winner)`, winners pull their own payouts, and you call `withdrawFees()`
whenever you want your rake. You never sign a payout again, and there is no moment where you
could pay the wrong person because you never touch the money.

The design points worth knowing before you deploy it:

- **Two keys, not one.** The owner key is yours and lives offline. The resolver key lives on the
  server. Neither of them can move escrow — there is no function in the contract that lets an
  owner take staked money. That is why a stolen server key is a problem you fix by redeploying
  rather than a problem where the money is gone.
- **Your fee address and fee rate are fixed at deployment** and can never be changed by anyone,
  including you. Set them carefully.
- **Anyone can rescue an abandoned market.** Seven days past its resolve deadline, any stranger
  can call `expire()` and everyone takes their stake back. Your server dying does not trap
  anyone's money.
- **It has never been compiled or tested.** There is no Solidity compiler in the environment it
  was written in. Read `contracts/README.md` — the first section says exactly what has to happen
  before it holds a real dollar.

## If something goes wrong

Start here:

```bash
./ikenga doctor
```

| What you saw | What it means |
|---|---|
| `Permission denied` | You skipped the chmod. `chmod +x ikenga seed_demo.py` |
| `Illegal option -o pipefail` | Old copy of the script. This version handles `sh ikenga` fine. |
| `Address already in use` | Already running. `./ikenga status`, or `./ikenga restart`. |
| `Could not install Rust` | Network blocked the download. `curl https://sh.rustup.rs -sSf \| sh -s -- -y` then `source $HOME/.cargo/env` |
| Stuck on "Installing Rust" | It genuinely takes a few minutes the first time. |
| Build fails | Full compiler output is in `data/build.log`. |
| Says running, dashboard won't open | The address, not the server. It only listens to itself unless you set `IKENGA_PUBLIC=1`. In Cloud Shell use web preview on port 8080, not `localhost`. |
| Key rejected | `./ikenga key` and copy it again — no spaces, no line break. |
| Signups rate-limited during `demo` | Deliberate. `IKENGA_REGISTRATIONS_PER_HOUR=100 ./ikenga start`, local use only. |
| Different port | `PORT=9000 ./ikenga start` |

**`localhost` will not work from your phone.** The link the script prints is correct *for the
machine it is running on*. From a phone you need either Cloud Shell's web preview button, or a
real server with `IKENGA_PUBLIC=1` and its public IP address.

## Settings

| Variable | Default | What it does |
|---|---|---|
| `PORT` | `8080` | Port to listen on |
| `IKENGA_PUBLIC` | off | Listen on all interfaces so the outside world can reach it |
| `IKENGA_REAL_MONEY` | off | Make USDC balances withdrawable |
| `IKENGA_AUTOPILOT` | off | Markets to run automatically, e.g. `BTC-USD:3600,ETH-USD:900` (symbol:seconds-open). Needs `IKENGA_ROUTE_SOURCES` for live prices |
| `IKENGA_ROUTE_SOURCES` | none | Price feeds. Without at least two that agree, autopilot opens nothing rather than guessing |
| `IKENGA_MARKET_HISTORY` | `1000` | Settled markets kept in memory. Older ones are trimmed; they stay in the log. Live markets are never trimmed |
| `IKENGA_FEED_DELAY_MS` | `900000` | How stale the free forecast feed is. Floor of 60000 |
| `IKENGA_ENABLE_ORDERBOOK` | off | Turn on the order book. Leave off — it's the only part that needs a counterparty |
| `IKENGA_REGISTRATIONS_PER_HOUR` | `5` | Signups per IP per hour |
