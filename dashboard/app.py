"""The dashboard you look at and click. This is only ONE of two required
processes — see README.md. Running this alone means Auto Mode can flip on
with nothing actually executing it; the health panel below exists to catch
exactly that.
"""

import json

import streamlit as st

from agents.bot_template import ensure_bots_seeded
from config import settings
from core import controller, database as db, publisher

st.set_page_config(page_title="Money Bots", page_icon="🤖", layout="wide")

db.init_db()
ensure_bots_seeded()


def _check_password():
    if not settings.DASHBOARD_PASSWORD:
        st.error(
            "DASHBOARD_PASSWORD is not set. Refusing to load — this dashboard "
            "can start bots and approve spending, and may sit on a public URL."
        )
        st.stop()

    if st.session_state.get("authed"):
        return

    password = st.text_input("Dashboard password", type="password")
    if password:
        if password == settings.DASHBOARD_PASSWORD:
            st.session_state["authed"] = True
            st.rerun()
        else:
            st.error("Wrong password.")
    st.stop()


_check_password()

st.title("🤖 Money Bots")

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

# --- Affiliate discovery ---
st.subheader("Affiliate Programs (pending human signup)")
st.caption(
    "Discovery is automated and grounded in real search results. Signup stays "
    "a human click — CAPTCHAs, verification, and tax forms need a person, and "
    "most networks forbid bot-created accounts anyway."
)
pending_programs = db.list_affiliate_programs(status="pending")
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
