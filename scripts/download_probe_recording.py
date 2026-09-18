"""Copy one selected recording to private staging; never print its URL/text."""
import argparse
import os
from pathlib import Path
import sqlite3
import urllib.parse
import urllib.request


def checked(url):
    parts = urllib.parse.urlsplit(url)
    if (parts.scheme != "https" or parts.hostname != "texnikachuz.moizvonki.ru"
            or parts.port not in (None, 443) or parts.username or parts.password):
        raise ValueError("Recording host is not permitted")
    return url


class Redirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return super().redirect_request(req, fp, code, msg, headers, checked(newurl))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--call-id", type=int, required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    with sqlite3.connect(Path(args.db).as_uri() + "?mode=ro", uri=True) as conn:
        row = conn.execute("SELECT recording FROM calls WHERE id=? AND answered=1", (args.call_id,)).fetchone()
    if not row or not row[0]:
        raise ValueError("Selected call has no recording")
    target = Path(args.output)
    os.umask(0o077)
    created = False
    try:
        with target.open("xb") as output:
            created = True
            with urllib.request.build_opener(Redirect).open(checked(row[0]), timeout=30) as response:
                length = 0
                while chunk := response.read(65536):
                    length += len(chunk)
                    if length > 10 * 1024 * 1024:
                        raise ValueError("Recording exceeds probe size limit")
                    output.write(chunk)
        print("Private test recording downloaded:", length, "bytes")
    except BaseException:
        if created:
            target.unlink(missing_ok=True)
        raise


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        raise SystemExit("Recording download failed: " + type(exc).__name__) from None
