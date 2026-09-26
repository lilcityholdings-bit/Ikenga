"""
Run this to create a new agent API key:
    python3 scripts/create_api_key.py my_agent_name

It prints the raw key ONCE -- give it to the agent operator and do not
store the raw value anywhere; only the hash lives in the (in-memory,
for now) key store.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.security import register_api_key

if __name__ == "__main__":
    agent_id = sys.argv[1] if len(sys.argv) > 1 else "demo_agent"
    raw_key = register_api_key(agent_id)
    print(f"Agent ID: {agent_id}")
    print(f"API Key (save this now, it will not be shown again): {raw_key}")
