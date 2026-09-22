# Install

Setup now happens **inside the app**. You do not touch environment variables.

---

## Step 1 — get the code into GitHub

Open **github.com** on your phone.

1. Tap `+` (top right) → **New repository**
2. Name: `money-bots` · choose **Private** · tap **Create repository**
3. Tap **uploading an existing file** → upload the zip contents

If you only have the .txt: tap **creating a new file**, type the path shown
after `FILE:` (typing `core/db.py` makes the `core` folder automatically),
paste that block, commit, repeat.

---

## Step 2 — deploy it

**Render** (reads `render.yaml`, sets up the disk for you):

1. **dashboard.render.com** → **New** → **Blueprint**
2. Pick your `money-bots` repo → **Apply**
3. Wait for the build, then open the URL it gives you

**Railway** works too: New Project → Deploy from GitHub repo → pick it.
Then add a Volume mounted at `/data`.

Nothing to configure. No keys yet.

---

## Step 3 — the app sets itself up

Open your URL. A **Setup** screen appears with three boxes.

**Box 1 — Password.** Type one, tap save. This locks the dashboard.

**Box 2 — AI key.** Open **console.groq.com**, sign up, API Keys → Create,
copy it, paste it in, tap save. Free.

**Box 3 — Your site.** Make a GitHub token:

- github.com → your avatar → **Settings**
- scroll to the bottom → **Developer settings**
- **Personal access tokens** → **Fine-grained tokens** → **Generate new token**
- Repository access: **All repositories**
- Permissions → Repository permissions:
  - **Contents** → Read and write
  - **Administration** → Read and write
- Generate, copy it

Paste it in the box, pick a name for your site, tap **Set up my site**.

The app creates the repo, turns on GitHub Pages, and tells you your URL.
You do not visit any settings pages.

> If you'd rather not grant Administration, make a public repo called
> `mysite` by hand and set Settings → Pages → main + /docs. Then just type
> its name in the box.

---

## Step 4 — first article

1. Tap **Done, take me to the dashboard**
2. Scroll to **Fleet**, set Bot-1's niche — be specific.
   "budget espresso machines under $300" beats "coffee"
3. **Save niche** → **Start**
4. Scroll up, **Run one cycle now**
5. Scroll to **Review queue**, read what it wrote, publish or reject

---

## Autopilot

Review a handful yourself first so you know what its output looks like.
Then: **Autopilot** → toggle ON → Save.

It publishes drafts scoring 72+, rejects below 45, sends the rest to you.
Start the daily cap at 3. Check "What autopilot decided recently" for the
first week — anything you disagree with, tap **Take this down**. That
teaches it far more than agreeing does.

---

## What you still have to do yourself

- **Log revenue by hand.** Commissions arrive at Amazon or wherever, not
  here. Until you enter them, the bots are optimising for writing quality,
  not money.
- **Apply to affiliate programs** once you have real pages live. Most reject
  thin new sites, and a `github.io` URL hurts. A custom domain helps a lot.
- **Add analytics.** Settings and backup → paste a Plausible or GA snippet.
  Without it the system is optimising blind.
- **Back up.** Settings and backup → Build backup file. If the disk goes,
  everything goes.
- **Renew the GitHub token** every 90 days. Publishing will tell you when
  it expires.

---

## Check your copy is intact

    python tests/run_all.py

45 checks, no internet needed.

## If something breaks

**Worker shows Down** — open your host's deploy logs, read the last error.

**"GitHub refused the token"** — it expired (90 days) or lost its
permissions. Make a new one, paste it in Setup.

**Articles are generic / "search unavailable"** — DuckDuckGo blocks
datacenter IPs, which is what your host is. The bots still write, but from
the model's own knowledge. Narrow the niche to compensate.

**Site 404s** — normal until the first publish. After that, check the repo's
Actions tab for the Pages build.
