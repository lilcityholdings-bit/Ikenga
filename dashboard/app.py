"""Control panel. Everything you actually need, nothing you do not."""
import os
import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import autopilot, db, genome, site  # noqa: E402
from core.auth import require_login  # noqa: E402
from core.controller import Controller  # noqa: E402
from core.worker import read_beat, run_once  # noqa: E402

st.set_page_config(page_title="Revenue Bots", page_icon="●", layout="wide")
require_login()

db.init()
ctl = Controller()
ctl.seed()


@st.cache_data(ttl=15, show_spinner=False)
def snapshot():
    return db.kpis()


def refresh():
    st.cache_data.clear()


st.title("Revenue Bots")

# ---------------------------------------------------------------- setup

def needs_setup():
    """Setup is unfinished while there is no password or no usable AI key."""
    from core.llm import is_llm_available
    has_password = bool(db.get_secret("DASHBOARD_PASSWORD")
                        or os.getenv("DASHBOARD_PASSWORD"))
    return not has_password or not is_llm_available()


if needs_setup() or st.session_state.get("show_setup"):
    st.header("Setup")
    st.caption("Three things. You can change any of them later.")

    with st.container(border=True):
        st.markdown("**1. Pick a password for this dashboard**")
        current = db.get_secret("DASHBOARD_PASSWORD")
        st.caption("Set" if current else "Not set — anyone with the link can control your bots")
        pw = st.text_input("Password", type="password", key="setup_pw")
        if st.button("Save password"):
            if len(pw) < 8:
                st.error("Use at least 8 characters")
            else:
                db.set_secret("DASHBOARD_PASSWORD", pw)
                st.success("Saved. You will be asked for it next time.")
                st.rerun()

    with st.container(border=True):
        st.markdown("**2. Paste a free AI key**")
        st.caption("console.groq.com -> API Keys -> Create. Free, takes a minute.")
        key = st.text_input("Groq key", type="password", key="setup_groq")
        if st.button("Save AI key"):
            if key.strip():
                db.set_secret("GROQ_API_KEY", key.strip())
                st.success("Saved")
                st.rerun()
            else:
                st.warning("Paste the key first")

    with st.container(border=True):
        st.markdown("**3. Connect where your articles get published**")
        gh_ok = False
        try:
            from core.publisher import check_connection, create_site_repo
            chk = check_connection()
            gh_ok = chk["ok"]
            st.caption(chk["detail"])
        except Exception as e:
            st.caption(str(e))

        if not gh_ok:
            st.caption("github.com -> your avatar -> Settings -> Developer settings "
                       "-> Personal access tokens -> Fine-grained tokens -> Generate. "
                       "Give it Contents: Read and write, and Administration: "
                       "Read and write if you want the repo made for you.")
            tok = st.text_input("GitHub token", type="password", key="setup_tok")
            site = st.text_input("Name for your site", value="mysite", key="setup_site")
            if st.button("Set up my site"):
                if not tok.strip():
                    st.warning("Paste the token first")
                else:
                    db.set_secret("GITHUB_TOKEN", tok.strip())
                    with st.spinner("Creating the repo and turning on Pages..."):
                        res = create_site_repo(site.strip() or "mysite")
                    if res["ok"]:
                        st.success(f"Done — {res['detail']}")
                        st.write(f"Your site: {res['url']}")
                        st.caption("It will 404 until the first article is published.")
                        st.rerun()
                    else:
                        st.error(res["detail"])

    if st.button("Done, take me to the dashboard"):
        st.session_state["show_setup"] = False
        st.rerun()
    st.divider()


if not (db.get_secret("DASHBOARD_PASSWORD") or os.getenv("DASHBOARD_PASSWORD")):
    # Skipping setup leaves a public URL that can start bots and publish to
    # the owner's site. Say so on every page load, not just once.
    st.error("No password set — anyone with this link can control your bots "
             "and publish to your site. Open setup and set one.")

# ---------------------------------------------------------------- header

k = snapshot()
c = st.columns(5)
c[0].metric("Bots", k["live_bots"])
c[1].metric("Articles", k["articles"])
c[2].metric("Published", k["published"])
c[3].metric("Waiting on you", k["needs_review"])
c[4].metric("Revenue", f"${k['revenue']:.2f}")

# ---------------------------------------------------------------- health

with st.expander("System health", expanded=k["live_bots"] == 0):
    hb = read_beat()
    h = st.columns(3)
    h[0].metric("Worker", "Alive" if hb["alive"] else "Down")
    try:
        from core.llm import is_llm_available, provider_status
        h[1].metric("Model", "Ready" if is_llm_available() else "No key")
        st.caption(" · ".join(f"{k2}: {v}" for k2, v in provider_status().items()))
    except Exception:
        pass
    try:
        from core.publisher import check_connection
        gh = check_connection()
        h[2].metric("Publishing", "Ready" if gh["ok"] else "Not set up")
        st.caption(gh["detail"])
    except Exception as e:
        st.caption(f"publisher: {e}")

    if hb["age"] is not None:
        st.caption(f"Last worker beat {int(hb['age'])}s ago — {hb['status']}")
    if not hb["alive"]:
        st.warning("The worker is not running. Bots will not produce anything.")

    if st.button("Run one cycle now"):
        with st.spinner("Running..."):
            r = run_once()
        refresh()
        st.success(f"{r['ran']} bots ran · autopilot published "
                   f"{r['auto'].get('published', 0)}, rejected "
                   f"{r['auto'].get('rejected', 0)}")
        st.rerun()

st.divider()

# ---------------------------------------------------------------- autopilot

st.header("Autopilot")
cfg = autopilot.get_config()
a = st.columns(3)
with a[0]:
    on = st.toggle("Autopilot ON", value=bool(cfg["autopilot_enabled"]))
with a[1]:
    may_pub = st.toggle("May publish", value=bool(cfg["autopilot_publish"]))
with a[2]:
    cap = st.number_input("Max auto-publishes/day", 1, 50,
                          int(cfg["autopilot_daily_cap"]))

if st.button("Save autopilot settings"):
    autopilot.set_config(autopilot_enabled=bool(on), autopilot_publish=bool(may_pub),
                         autopilot_daily_cap=int(cap))
    st.success("Saved")
    st.rerun()

if on:
    st.caption(f"Published automatically in the last 24h: {db.auto_published_since(24)} "
               f"of {int(cap)}. Strong drafts go live, weak ones are rejected, "
               f"borderline ones land in your queue. Your verdicts count about "
               f"three times more than the critic's.")
else:
    st.caption("Off — every draft waits for you below.")

with st.expander("What autopilot decided recently"):
    rows = db.get_auto_decisions(15)
    if not rows:
        st.write("Nothing yet.")
    for r in rows:
        live = r["status"] == "auto_published"
        label = ("**PUBLISHED** " if live
                 else "approved, not published — " if r["status"] == "auto_approved"
                 else "rejected — ")
        st.write(label + r["title"][:70])
        if r["status"] == "auto_approved":
            st.caption("The critic approved this but it could not be delivered. "
                       "Finish setup to publish, or publish it by hand.")
        st.caption(f"{r['bot_name']} · {r.get('notes', '')}")
        if live:
            if r.get("live_url"):
                st.write(r["live_url"])
            if st.button("Take this down", key=f"td{r['id']}"):
                msg = autopilot.take_down(r["id"])
                refresh()
                st.warning(f"Taken down — {msg}")
                st.rerun()

st.divider()

# ---------------------------------------------------------------- queue

st.header("Review queue")
queue = db.get_queue("needs_review")
if not queue:
    st.info("Nothing waiting. Start some bots, or run a cycle.")
for item in queue:
    st.subheader(f"{item['title']}")
    st.caption(f"{item['bot_name']} · {item['path']}")
    if item.get("notes"):
        st.caption(item["notes"])
    title, body = autopilot._read_draft(item["path"], item.get("page", ""))
    if body:
        with st.expander("Read it"):
            st.write(body[:4000])
    q = st.columns(4)
    with q[0]:
        if st.button("Publish", key=f"p{item['id']}"):
            try:
                from core.publisher import publish_site_folder
                url = publish_site_folder(item["path"])
                db.set_queue_status(item["id"], "published", live_url=url)
                db.update_bot(item["bot_id"], live_url=url)
                refresh()
                st.success(f"Live at {url}")
                st.rerun()
            except Exception as e:
                st.error(f"Publish failed: {e}")
    with q[1]:
        if st.button("Reject", key=f"r{item['id']}"):
            site.remove_page(item["bot_id"], item.get("page", ""))
            db.set_queue_status(item["id"], "rejected")
            refresh()
            st.rerun()
    with q[2]:
        if st.button("Later", key=f"l{item['id']}"):
            db.set_queue_status(item["id"], "deferred")
            refresh()
            st.rerun()
    st.divider()

# ---------------------------------------------------------------- revenue

st.header("Log revenue")
pub = db.get_published()
if not pub:
    st.caption("Publish a page first — then commissions can be traced to it.")
else:
    r1, r2 = st.columns(2)
    with r1:
        item_id = st.selectbox(
            "Which page earned this?",
            options=[p["id"] for p in pub],
            format_func=lambda i: next(
                f"{p['title'][:45]} — {p['bot_name']}" for p in pub if p["id"] == i),
        )
        amount = st.number_input("Amount ($)", min_value=0.0, step=0.01)
    with r2:
        source = st.text_input("Source", placeholder="Amazon Associates")
        note = st.text_input("Note", placeholder="which product")
    if st.button("Save"):
        if amount > 0:
            db.add_earning(item_id, amount, source, note)
            refresh()
            st.success(f"Logged ${amount:.2f}")
            st.rerun()
        else:
            st.warning("Enter an amount above 0")

for e in db.get_earnings(8):
    st.caption(f"${e['amount']:.2f} · {e['bot_name']} · {e.get('source', '')} "
               f"· {e.get('note', '')}")

st.divider()

# ---------------------------------------------------------------- fleet

st.header("Fleet")
f = st.columns(3)
if f[0].button("Start all", use_container_width=True):
    st.success(f"Started {ctl.start_all()}")
    st.rerun()
if f[1].button("Pause all", use_container_width=True):
    st.warning(f"Paused {ctl.pause_all()}")
    st.rerun()
if f[2].button("Stop all", use_container_width=True):
    st.error(f"Stopped {ctl.stop_all()}")
    st.rerun()

order = st.text_area("Order for every bot", height=70,
                     placeholder="Only cover budget options under $200 this week")
if st.button("Send to all bots"):
    if order.strip():
        st.success(f"Sent to {ctl.order_all(order.strip())} bots")
    else:
        st.warning("Type an order first")

st.subheader("Standings")
st.caption(f"Next ranking: {ctl.next_deadline()}")
for row in ctl.scoreboard():
    b, o = row["bot"], row["outcomes"]
    g = genome.load(b.get("genome") or "")
    with st.container(border=True):
        top = st.columns([3, 1, 1])
        top[0].markdown(f"**{b['name']}** · gen {b['generation']} · {b['status']}")
        top[1].metric("Score", f"{row['score']:.1f}")
        top[2].metric("Earned", f"${row['earnings']:.2f}")
        st.caption(f"Style: {genome.summary(g)} · Niche: {b['niche'] or 'not set'} "
                   f"· Pages: {site.page_count(b['id'])}")
        st.caption(f"You: {o['published']} published / {o['rejected']} rejected  ·  "
                   f"Autopilot: {o['auto_published']} / {o['auto_rejected']}  ·  "
                   f"Waiting: {o['pending']}")
        if b["live_url"]:
            st.caption(b["live_url"])

        bc = st.columns(3)
        if bc[0].button("Start", key=f"s{b['id']}"):
            ctl.start(b["id"])
            st.rerun()
        if bc[1].button("Pause", key=f"pa{b['id']}"):
            ctl.pause(b["id"])
            st.rerun()
        if bc[2].button("Stop", key=f"st{b['id']}"):
            ctl.stop(b["id"])
            st.rerun()

        niche = st.text_input("Niche", value=b["niche"], key=f"n{b['id']}",
                              placeholder="budget espresso machines under $300")
        if st.button("Save niche", key=f"sn{b['id']}"):
            ctl.set_niche(b["id"], niche)
            st.success("Saved")
            st.rerun()

with st.expander("Run ranking now"):
    st.write("Culls genuine underperformers and breeds from bots whose work was "
             "accepted. New bots get 48 hours grace.")
    if st.button("Run ranking"):
        st.json(ctl.run_selection())
        refresh()

with st.expander("Settings and backup"):
    if st.button("Open setup"):
        st.session_state["show_setup"] = True
        st.rerun()
    st.caption("Back up everything: bots, verdicts, earnings and every article.")
    if st.button("Build backup file"):
        path = db.export_backup()
        st.success(f"Written to {path}")
        st.caption("Download it from your host's shell, or keep it on the disk.")
    snippet = db.get_setting("analytics_snippet", "")
    new_snippet = st.text_area(
        "Analytics snippet (optional)", value=snippet, height=80,
        placeholder="<script defer data-domain=... src=...></script>")
    if st.button("Save analytics"):
        db.set_setting("analytics_snippet", new_snippet.strip())
        st.success("Saved — it goes into every page from the next rebuild")
        st.rerun()

with st.expander("Recent activity"):
    for a2 in db.recent_actions(25):
        st.caption(f"{a2['created_at'][11:19]} · {a2.get('bot_name') or 'system'} · "
                   f"{a2['action']} · {str(a2.get('detail', ''))[:90]}")
