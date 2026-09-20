"""The dashboard you look at and click. This is only ONE of two required
processes — see README.md. Running this alone means Auto Mode can flip on
with nothing actually executing it; the health panel below exists to catch
exactly that.
"""

import hmac
import json
import time

import streamlit as st

from agents.bot_template import ensure_bots_seeded
from config import settings
from core import controller, database as db, publisher

st.set_page_config(page_title="Money Bots", page_icon="🤖", layout="wide")

db.init_db()
ensure_bots_seeded()

MAX_LOGIN_ATTEMPTS_BEFORE_COOLDOWN = 3
COOLDOWN_SECONDS = 30


def _check_password():
    if not settings.DASHBOARD_PASSWORD:
        st.error(
            "DASHBOARD_PASSWORD is not set. Refusing to load — this dashboard "
            "can start bots and approve spending, and may sit on a public URL."
        )
        st.stop()

    if st.session_state.get("authed"):
        return

    attempts = st.session_state.get("login_attempts", 0)
    locked_until = st.session_state.get("login_locked_until", 0)
    remaining = locked_until - time.monotonic()
    if remaining > 0:
        st.error(f"Too many wrong attempts. Try again in {int(remaining) + 1}s.")
        st.stop()

    password = st.text_input("Dashboard password", type="password")
    if password:
        # Constant-time compare — a plain `==` short-circuits on the first
        # mismatched byte, which leaks how many leading characters were
        # right to anyone able to time repeated attempts against a
        # password gate that's meant to sit on a public URL.
        if hmac.compare_digest(password, settings.DASHBOARD_PASSWORD):
            st.session_state["authed"] = True
            st.session_state.pop("login_attempts", None)
            st.session_state.pop("login_locked_until", None)
            st.rerun()
        else:
            attempts += 1
            st.session_state["login_attempts"] = attempts
            if attempts >= MAX_LOGIN_ATTEMPTS_BEFORE_COOLDOWN:
                st.session_state["login_locked_until"] = time.monotonic() + COOLDOWN_SECONDS
                st.session_state["login_attempts"] = 0
            st.error("Wrong password.")
    st.stop()


_check_password()

title_col, logout_col = st.columns([6, 1])
with title_col:
    st.title("🤖 Money Bots")
with logout_col:
    if st.button("Log out"):
        st.session_state.pop("authed", None)
        st.rerun()

# --- Health panel ---
health = controller.get_health()
col1, col2 = st.columns(2)
with col1:
    if health["alive"]:
        st.success("Worker: 🟢 Alive")
    else:
        st.error("Worker: 🔴 Not running — start the worker service (`python worker.py`).")
with col2:
    site = publisher.site_url()
    if site:
        st.markdown(f"**Your Live Site:** [{site}]({site})")
    else:
        st.warning("Your Live Site: not configured (set GITHUB_USERNAME / GITHUB_REPO).")

st.divider()

# --- Auto mode + bot controls ---
auto_mode = db.get_setting("auto_mode", "false") == "true"
new_auto_mode = st.toggle("Auto Mode", value=auto_mode)
if new_auto_mode != auto_mode:
    db.set_setting("auto_mode", "true" if new_auto_mode else "false")
    st.rerun()

bots = db.get_bots()

start_col, stop_col = st.columns(2)
with start_col:
    if st.button("▶️ Start All Bots"):
        for bot in bots:
            db.set_bot_status(bot["id"], "started")
        st.rerun()
with stop_col:
    if st.button("⏸️ Stop All Bots"):
        for bot in bots:
            db.set_bot_status(bot["id"], "stopped")
        st.rerun()

st.subheader("Bots")
for bot in bots:
    name_col, niche_col, status_col, action_col = st.columns([3, 3, 2, 2])
    name_col.write(f"**{bot['name']}**")
    niche_col.write(bot["niche"])
    status_col.write("🟢 started" if bot["status"] == "started" else "⚪ stopped")
    toggle_label = "Stop" if bot["status"] == "started" else "Start"
    if action_col.button(toggle_label, key=f"toggle-{bot['id']}"):
        db.set_bot_status(bot["id"], "stopped" if bot["status"] == "started" else "started")
        st.rerun()

st.divider()

# --- Recent activity ---
st.subheader("Recent Activity")
activity = db.get_recent_activity(limit=30)
if not activity:
    st.caption("No activity yet.")
else:
    for entry in activity:
        st.text(f"[{entry['ts']}] bot={entry['bot_id']} {entry['kind']}: {entry['detail']}")

st.divider()

# --- Affiliate programs ---
st.subheader("Affiliate Programs")
st.caption(
    "Discovery is automated and grounded in real search results. Signup stays "
    "a human click — CAPTCHAs, verification, and tax forms need a person, and "
    "most networks forbid bot-created accounts anyway. Nothing gets monetized "
    "until you paste in the real tracking link a program gives you after it "
    "approves you — that's the step that actually turns a program into "
    "revenue, and only a human can do it."
)

pending_programs = db.list_affiliate_programs(status="pending")
st.markdown("**Pending signup**")
if not pending_programs:
    st.caption("None discovered yet.")
for program in pending_programs:
    with st.expander(program["name"]):
        st.markdown(f"[Signup link]({program['signup_url']})")
        try:
            checklist = json.loads(program["checklist"] or "[]")
        except json.JSONDecodeError:
            checklist = []
        for item in checklist:
            st.checkbox(item, key=f"cl-{program['id']}-{item}")
        if st.button("Mark as submitted", key=f"submit-{program['id']}"):
            db.update_affiliate_status(program["id"], "submitted")
            st.rerun()

submitted_programs = db.list_affiliate_programs(status="submitted")
st.markdown("**Submitted — waiting on approval**")
if not submitted_programs:
    st.caption("None waiting.")
for program in submitted_programs:
    with st.expander(program["name"]):
        st.markdown(f"[Signup link]({program['signup_url']})")
        st.caption("Once the program approves you, paste the tracking link it gives you below.")
        link = st.text_input("Your affiliate tracking link", key=f"link-{program['id']}")
        if st.button("Activate", key=f"activate-{program['id']}", disabled=not link):
            db.set_affiliate_link(program["id"], link.strip())
            st.rerun()
        if st.button("Rejected / not approved", key=f"reject-aff-{program['id']}"):
            db.update_affiliate_status(program["id"], "rejected")
            st.rerun()

active_programs = db.list_affiliate_programs(status="active")
st.markdown("**Active — live in generated articles**")
if not active_programs:
    st.caption("None active yet — nothing is monetized until one is.")
for program in active_programs:
    bot = db.get_bot(program["bot_id"]) if program["bot_id"] else None
    bot_label = bot["name"] if bot else "unassigned"
    st.text(f"✅ {program['name']} ({bot_label}) → {program['affiliate_url']}")

st.divider()

# --- Code proposals ---
st.subheader("Code Proposals")
st.caption(
    "Approving records the approval only — there is no engine that rewrites "
    "the bot's own files yet."
)
pending_proposals = db.list_code_proposals(status="pending")
if not pending_proposals:
    st.caption("No pending proposals.")
for proposal in pending_proposals:
    with st.expander(proposal["description"]):
        st.code(proposal["diff"] or "")
        approve_col, reject_col = st.columns(2)
        if approve_col.button("Approve", key=f"approve-{proposal['id']}"):
            db.update_code_proposal_status(proposal["id"], "approved")
            st.rerun()
        if reject_col.button("Reject", key=f"reject-{proposal['id']}"):
            db.update_code_proposal_status(proposal["id"], "rejected")
            st.rerun()

st.divider()
st.caption("Spending approval gate is symbolic — there is no payment integration yet.")
