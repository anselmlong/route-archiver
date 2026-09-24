"""Read-only HTTP smoke checks; no Telegram credentials or third-party packages."""
import argparse
import json
import sys
from urllib.error import URLError
from urllib.request import urlopen


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base_url", help="API origin, for example http://127.0.0.1:8160")
    args = parser.parse_args()
    for path in ("/healthz", "/readyz", "/"):
        try:
            with urlopen(args.base_url.rstrip("/") + path, timeout=5) as response:
                body = response.read(1024 * 1024)
                if response.status != 200:
                    raise ValueError("non-200 response")
                if path == "/":
                    if b"USC Routes" not in body:
                        raise ValueError("route UI missing")
                elif json.loads(body) != {"ok": True}:
                    raise ValueError("unexpected health response")
            print(f"PASS {path}")
        except (URLError, OSError, ValueError):
            print(f"FAIL {path}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
