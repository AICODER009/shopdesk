"""Local dev server: `python -m shopdesk.devserver [--port 8000]`.

Same app as `uvicorn shopdesk.main:app`, but started on a selector event loop, which
psycopg's async pool needs on Windows (uvicorn defaults to Proactor there). On Linux this
is plain uvicorn.
"""

import argparse

import uvicorn

from shopdesk.runtime import run


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    server = uvicorn.Server(uvicorn.Config("shopdesk.main:app", host=args.host, port=args.port))
    run(server.serve())


if __name__ == "__main__":
    main()
