import argparse
import os
import sys

from .server import serve
from .store import Store


def main(argv=None):
    ap = argparse.ArgumentParser(prog="__PKG__", description="__NAME__: an auth service")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8085)
    ap.add_argument("--db", default="auth.db")
    args = ap.parse_args(argv)
    secret = os.environ.get("AUTH_SECRET")
    if not secret or len(secret) < 16:
        print("set AUTH_SECRET to a random string of at least 16 characters", file=sys.stderr)
        return 2
    httpd = serve(Store(args.db), secret, args.host, args.port)
    print(f"listening on http://{args.host}:{args.port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
