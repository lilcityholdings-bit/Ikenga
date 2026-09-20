"""Local dev convenience entrypoint for the dashboard (the "Web" process).

In production (e.g. Railway), point the web service's start command
directly at streamlit instead:
    streamlit run dashboard/app.py --server.port $PORT --server.address 0.0.0.0
This script just wraps that same command for local use.
"""

import os
import subprocess
import sys


def main():
    port = os.environ.get("PORT", "8501")
    cmd = [
        sys.executable, "-m", "streamlit", "run", "dashboard/app.py",
        "--server.port", port,
        "--server.address", "0.0.0.0",
    ]
    subprocess.run(cmd, check=False)


if __name__ == "__main__":
    main()
