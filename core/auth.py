"""Password gate for the dashboard. Without this, a public URL = open controls."""
import os
import hmac
import streamlit as st


def require_login():
    password = os.getenv("DASHBOARD_PASSWORD", "").strip()
    if not password:
        try:
            from core.db import get_secret
            password = get_secret("DASHBOARD_PASSWORD")
        except Exception:
            password = ""
    # First run with nothing set: let them in to create one, rather than
    # locking them out of an app they just deployed.
    if not password:
        st.session_state["first_run"] = True
        return

    if st.session_state.get("authed"):
        return

    st.title("Revenue Bots")
    entered = st.text_input("Password", type="password")
    if st.button("Sign in"):
        if hmac.compare_digest(entered, password):
            st.session_state["authed"] = True
            st.rerun()
        else:
            st.error("Wrong password")
    st.stop()
