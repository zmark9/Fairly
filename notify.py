#!/usr/bin/env python3
"""
notify.py - email the sync summary through the zmark messaging API.

Reads output/summary.json (written by sync.py) and the RemoteLock CSVs, and
POSTs an EmailToSend as multipart/form-data to <api_url> (the API's
`sendemailform` action, which binds [FromForm] and fixes applicationId = 2,
so the sender address comes from the API, not from here).

The API rejects common script User-Agents (python-urllib, curl/, ...), so we
send our own User-Agent.

Usage:
    python notify.py --out output                 # send using config.json settings
    python notify.py --out output --dry-run       # print the request, send nothing
    python notify.py --out output --api-url https://localhost:7075/api/email/sendemailform --insecure --force

--insecure skips TLS certificate checks and is only allowed for localhost
(the local dev server uses a self-signed certificate).
"""

import argparse
import csv
import html
import json
import ssl
import sys
import uuid
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def load_remotelock_tables(out_dir):
    """{filename: [rows]} for every remotelock_*.csv in the output folder."""
    tables = {}
    for path in sorted(Path(out_dir).glob("remotelock_*.csv")):
        with path.open(newline="", encoding="utf-8") as handle:
            tables[path.name] = list(csv.DictReader(handle))
    return tables


def find_attachments(out_dir, cfg):
    """Files to attach, from config email.attachments (file names or glob patterns)."""
    patterns = cfg.get("attachments") or ["remotelock_*.csv"]
    found = []
    for pattern in patterns:
        for path in sorted(Path(out_dir).glob(pattern)):
            if path not in found:
                found.append(path)
    return found


def build_subject(summary):
    parts = [f"{len(summary['added'])} new", f"{len(summary['changed'])} changed",
             f"{len(summary['cancelled'])} cancelled"]
    if summary["warnings"]:
        parts.append(f"{len(summary['warnings'])} warnings")
    return f"Fairly sync {summary['run_date']}: " + ", ".join(parts)


def build_body(summary, remotelock_tables, exports_url, note=""):
    """Simple HTML email: changes, warnings, then the RemoteLock rows as a table."""
    esc = html.escape
    out = [f"<h2>Fairly reservation sync - {esc(summary['run_date'])}</h2>"]
    if note:
        out.append(f'<p style="color:#b00020"><strong>{esc(note)}</strong></p>')
    per_home = ", ".join(f"{esc(home)}: {count}" for home, count in summary["per_home"].items())
    out.append(f"<p>{summary['total']} upcoming reservations ({per_home}).</p>")

    for title, key in [("New", "added"), ("Changed", "changed"),
                       ("Cancelled / removed - update RemoteLock if needed", "cancelled"),
                       ("Warnings", "warnings")]:
        if summary[key]:
            out.append(f"<h3>{esc(title)}</h3><ul>")
            out += [f"<li>{esc(item)}</li>" for item in summary[key]]
            out.append("</ul>")
    if not summary["has_changes"]:
        out.append("<p>No changes since the last run.</p>")

    for name, rows in remotelock_tables.items():
        out.append(f"<h3>{esc(name)}</h3>")
        if not rows:
            out.append("<p>No reservations.</p>")
            continue
        columns = list(rows[0].keys())
        out.append('<table border="1" cellpadding="4" cellspacing="0" style="border-collapse:collapse">')
        out.append("<tr>" + "".join(f"<th>{esc(c)}</th>" for c in columns) + "</tr>")
        for row in rows:
            out.append("<tr>" + "".join(f"<td>{esc(row[c])}</td>" for c in columns) + "</tr>")
        out.append("</table>")

    if exports_url:
        out.append(f'<p>All files: <a href="{esc(exports_url)}">{esc(exports_url)}</a></p>')
    return "\n".join(out)


USER_AGENT = "FairlyReservationSync/1.0"


def email_fields(cfg, subject, body):
    """Form fields matching the API's EmailToSend model (From* is set by the API)."""
    return {
        "ToEmail": cfg["to_email"],
        "ToName": cfg.get("to_name", ""),
        "Subject": subject,
        "EmailBody": body,
    }


def multipart_request(url, fields, attachments):
    """Build a multipart/form-data POST: text fields plus CSV files as 'Attachments'."""
    boundary = uuid.uuid4().hex
    chunks = []
    for name, value in fields.items():
        chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'
                      .encode("utf-8"))
    for path in attachments:
        chunks.append(
            (f'--{boundary}\r\nContent-Disposition: form-data; name="Attachments"; filename="{path.name}"\r\n'
             f"Content-Type: text/csv\r\n\r\n").encode("utf-8") + path.read_bytes() + b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    return urllib.request.Request(url, data=b"".join(chunks), method="POST",
                                  headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                                           "User-Agent": USER_AGENT})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default="output")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--api-url", help="override email.api_url from config.json")
    parser.add_argument("--insecure", action="store_true", help="skip TLS checks (localhost only)")
    parser.add_argument("--dry-run", action="store_true", help="print the request instead of sending")
    parser.add_argument("--force", action="store_true", help="send even if nothing changed")
    parser.add_argument("--note", default="", help="highlighted message at the top (e.g. push failure); forces a send")
    args = parser.parse_args(argv)

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8-sig"))["email"]  # -sig: tolerate a BOM
    summary = json.loads((Path(args.out) / "summary.json").read_text(encoding="utf-8"))

    if not cfg.get("enabled", True):
        print("Email disabled in config.json; skipping.")
        return 0
    if not summary["has_changes"] and not summary["warnings"] and not cfg.get("send_when_unchanged") \
            and not args.force and not args.note:
        print("No changes and no warnings; no email sent.")
        return 0
    if not cfg.get("to_email"):
        print("WARNING: email.to_email is empty in config.json; no email sent.", file=sys.stderr)
        return 0

    api_url = args.api_url or cfg["api_url"]
    host = urllib.parse.urlparse(api_url).hostname
    if args.insecure and host not in ("localhost", "127.0.0.1"):
        sys.exit("--insecure is only allowed for localhost.")
    url = api_url

    fields = email_fields(cfg, build_subject(summary),
                          build_body(summary, load_remotelock_tables(args.out), cfg.get("exports_url"), args.note))
    attachments = find_attachments(args.out, cfg) if cfg.get("attach_files") else []
    request = multipart_request(url, fields, attachments)

    if args.dry_run:
        print(f"POST {url}\nContent-Type: {request.get_header('Content-type')}\n"
              f"User-Agent: {request.get_header('User-agent')}\n")
        print(json.dumps({k: v for k, v in fields.items() if k != "EmailBody"}, indent=2))
        print(f"\nEmailBody ({len(fields['EmailBody'])} chars) not shown.")
        print(f"Attachments: {[p.name for p in attachments] or 'none'}")
        return 0

    context = ssl._create_unverified_context() if args.insecure else None
    try:
        with urllib.request.urlopen(request, timeout=30, context=context) as response:
            print(f"Email API responded {response.status}: {response.read(500).decode('utf-8', 'replace')}")
    except urllib.error.HTTPError as err:
        print(f"Email API error {err.code}: {err.read(500).decode('utf-8', 'replace')}", file=sys.stderr)
        return 1
    except urllib.error.URLError as err:
        print(f"Could not reach email API: {err.reason}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
