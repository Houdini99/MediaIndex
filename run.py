"""Clean entry point for the Docker container.

Calling pattern:
  python run.py                        # local dev / Docker CMD
  flask --app run:app run ...          # Flask CLI also works (app is exposed)
"""
import os
import socket

from app.logging_setup import configure_logging
from app import create_app
from app.config import Config


def get_lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('10.255.255.255', 1))
        ip = s.getsockname()[0]
    except Exception:
        ip = '127.0.0.1'
    finally:
        s.close()
    return ip


# ── Configure logging BEFORE creating the app so the redirected
#    stdout/stderr captures any startup output. ────────────────────────────
configure_logging(os.path.dirname(os.path.abspath(__file__)))

app = create_app()


if __name__ == '__main__':
    print(f"Starting MediaIndex on http://{get_lan_ip()}:{Config.PORT}/")
    app.run(host="0.0.0.0", port=Config.PORT, debug=False)
