import socket
import time
import urllib.request


def find_free_port() -> int:
    """Return an available TCP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_http_ok(url: str, deadline: float) -> bool:
    """Poll an HTTP URL until it responds or deadline passes.

    Designed for SSE endpoints as well; any successful response indicates readiness.
    """
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                # For SSE endpoints, just getting a response indicates readiness
                if resp.status in (200, 204):
                    return True
                return True
        except Exception:
            time.sleep(0.2)
    return False
