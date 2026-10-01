#!/usr/bin/env python3
"""
NAMB Freshdesk helper for the ticket routine. Uses Freshdesk's REST API directly
(no MCP server), so nothing here counts against the MCP action allowance and
nothing breaks when the MCP server renames its tools.

  python3 kb.py ticket 1084039            -> requester, status, subject, description
  python3 kb.py search "printer"          -> top matching published KB articles
  python3 kb.py article 4000225005        -> full article text + attachment text
  python3 kb.py reply 1084039 reply.html  -> send reply.html as the public reply

Safety checks built into `reply` (the script refuses, whatever the caller says):
  - requester email must be in ALLOWED_REQUESTERS (env var, comma-separated;
    default jbruce@namb.net; an entry starting with "@" allows a whole domain)
  - ticket status must be Open (2)
  - the ticket must not already have a public agent reply (no double replies)
  - the body must be real HTML, not escaped or markdown

Reads the API key from the FRESHDESK_API_KEY environment variable and never prints it.
"""
import base64
import html
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

DOMAIN = os.environ.get("FRESHDESK_DOMAIN", "namb.freshdesk.com")
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
MAX_RESULTS = 8
MAX_ATTACHMENT_CHARS = 15000


def _auth_header():
    key = os.environ.get("FRESHDESK_API_KEY", "").strip()
    if not key:
        sys.exit("ERROR: FRESHDESK_API_KEY is not set in this environment.")
    return "Basic " + base64.b64encode(f"{key}:X".encode()).decode()


def _get(url, with_auth=True, raw=False, method="GET", payload=None):
    """GET/POST a URL; retries once on 429. Auth is only sent to the Freshdesk domain."""
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    for attempt in range(2):
        req = urllib.request.Request(url, data=body, method=method)
        if with_auth:
            req.add_header("Authorization", _auth_header())
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = resp.read(MAX_ATTACHMENT_BYTES + 1) if raw else resp.read()
                return data if raw else json.loads(data.decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt == 0:
                time.sleep(min(int(e.headers.get("Retry-After", "30")), 60))
                continue
            err = e.read().decode("utf-8", "replace")[:300]
            sys.exit(f"ERROR: HTTP {e.code} from {urllib.parse.urlparse(url).netloc}: {err}")
        except urllib.error.URLError as e:
            sys.exit(f"ERROR: could not reach {urllib.parse.urlparse(url).netloc}: {e.reason}")


class _TextExtractor(HTMLParser):
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "ol", "ul", "table"}

    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag == "li":
            self.parts.append("\n- ")
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


def html_to_text(s):
    if not s:
        return ""
    p = _TextExtractor()
    p.feed(s)
    text = html.unescape("".join(p.parts))
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def _attachment_text(att):
    name = att.get("name", "attachment")
    size = att.get("size") or 0
    url = att.get("attachment_url")
    lower = name.lower()
    if size > MAX_ATTACHMENT_BYTES:
        return f"(skipped: {name} is larger than 10 MB)"
    if not url:
        return f"(no download URL for {name})"
    if not lower.endswith((".pdf", ".docx", ".txt", ".md", ".csv")):
        return f"(skipped: {name} is not a text document)"
    # Attachment URLs are pre-signed; never send the Freshdesk key to them.
    data = _get(url, with_auth=False, raw=True)
    if len(data) > MAX_ATTACHMENT_BYTES:
        return f"(skipped: {name} is larger than 10 MB)"
    try:
        if lower.endswith(".pdf"):
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(data))
            text = "\n".join((page.extract_text() or "") for page in reader.pages)
        elif lower.endswith(".docx"):
            import docx
            d = docx.Document(io.BytesIO(data))
            text = "\n".join(par.text for par in d.paragraphs)
            for table in d.tables:
                for row in table.rows:
                    text += "\n" + " | ".join(cell.text for cell in row.cells)
        else:
            text = data.decode("utf-8", "replace")
    except ImportError as e:
        return f"(could not read {name}: missing library {e.name}; add it to the setup script)"
    except Exception as e:  # noqa: BLE001
        return f"(could not read {name}: {e})"
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    if not text:
        return f"(no extractable text in {name}; it may be a scanned image)"
    if len(text) > MAX_ATTACHMENT_CHARS:
        text = text[:MAX_ATTACHMENT_CHARS] + "\n...(truncated)"
    return text


def cmd_search(term):
    q = urllib.parse.quote(term)
    results = _get(f"https://{DOMAIN}/api/v2/search/solutions?term={q}")
    if isinstance(results, dict):
        results = results.get("results", [])
    published = [a for a in results if a.get("status") == 2][:MAX_RESULTS]
    if not published:
        print(f'No published articles match "{term}".')
        return
    print(f'{len(published)} published article(s) for "{term}":\n')
    for a in published:
        snippet = html_to_text(a.get("description_text") or a.get("description") or "")
        snippet = re.sub(r"\s+", " ", snippet)[:220]
        print(f"- ID {a.get('id')} | {a.get('title')} | folder {a.get('folder_id')} | updated {str(a.get('updated_at',''))[:10]}")
        if snippet:
            print(f"  {snippet}")


def cmd_article(article_id):
    if not str(article_id).isdigit():
        sys.exit("ERROR: article id must be a number.")
    a = _get(f"https://{DOMAIN}/api/v2/solutions/articles/{article_id}")
    print(f"# {a.get('title')}")
    print(f"ID: {a.get('id')} | folder {a.get('folder_id')} | status {a.get('status')} | updated {str(a.get('updated_at',''))[:10]}\n")
    print("## Article")
    print(html_to_text(a.get("description") or a.get("description_text") or "") or "(empty)")
    for att in a.get("attachments") or []:
        print(f"\n## Attachment: {att.get('name')}")
        print(_attachment_text(att))


def _ticket(ticket_id):
    if not str(ticket_id).isdigit():
        sys.exit("ERROR: ticket id must be a number.")
    return _get(f"https://{DOMAIN}/api/v2/tickets/{ticket_id}?include=requester")


def _requester_email(t):
    return ((t.get("requester") or {}).get("email") or "").strip().lower()


def _allowed(email):
    raw = os.environ.get("ALLOWED_REQUESTERS", "jbruce@namb.net,kguillen@namb.net")
    for entry in (e.strip().lower() for e in raw.split(",") if e.strip()):
        if entry.startswith("@") and email.endswith(entry):
            return True
        if email == entry:
            return True
    return False


def cmd_ticket(ticket_id):
    t = _ticket(ticket_id)
    email = _requester_email(t)
    print(f"requester_email: {email or '(none)'}")
    print(f"requester_name: {(t.get('requester') or {}).get('name') or '(none)'}")
    print(f"requester_allowed: {'yes' if email and _allowed(email) else 'no'}")
    print(f"status: {t.get('status')}")
    print(f"subject: {t.get('subject')}")
    print("description:")
    print(html_to_text(t.get("description") or t.get("description_text") or "") or "(empty)")


def cmd_reply(ticket_id, body_file):
    t = _ticket(ticket_id)
    email = _requester_email(t)
    if not email or not _allowed(email):
        sys.exit(f"REFUSED: requester {email or '(unknown)'} is not in ALLOWED_REQUESTERS. No reply sent.")
    if t.get("status") != 2:
        sys.exit(f"REFUSED: ticket status is {t.get('status')}, not Open (2). No reply sent.")
    convs = _get(f"https://{DOMAIN}/api/v2/tickets/{ticket_id}/conversations")
    if any((not c.get("incoming")) and (not c.get("private")) for c in (convs or [])):
        sys.exit("REFUSED: this ticket already has a public agent reply. No reply sent.")
    try:
        with open(body_file, encoding="utf-8") as f:
            body = f.read().strip()
    except OSError as e:
        sys.exit(f"ERROR: could not read {body_file}: {e}")
    if not body or "<p>" not in body.lower():
        sys.exit("REFUSED: reply body must be HTML with <p> tags. No reply sent.")
    if "&lt;p&gt;" in body or body.startswith("```"):
        sys.exit("REFUSED: reply body is escaped or in a code block. No reply sent.")
    r = _get(f"https://{DOMAIN}/api/v2/tickets/{ticket_id}/reply", method="POST", payload={"body": body})
    print(f"SENT: reply {r.get('id')} on ticket {ticket_id} to {email}")


def main():
    cmds = ("ticket", "search", "article", "reply", "requester")
    if len(sys.argv) < 3 or sys.argv[1] not in cmds:
        sys.exit('Usage: python3 kb.py ticket <id> | search "<keywords>" | article <id> | reply <ticket_id> <file.html>')
    cmd = sys.argv[1]
    if cmd == "search":
        cmd_search(" ".join(sys.argv[2:]))
    elif cmd == "article":
        cmd_article(sys.argv[2])
    elif cmd in ("ticket", "requester"):
        cmd_ticket(sys.argv[2])
    else:
        if len(sys.argv) < 4:
            sys.exit("Usage: python3 kb.py reply <ticket_id> <file.html>")
        cmd_reply(sys.argv[2], sys.argv[3])


if __name__ == "__main__":
    main()
