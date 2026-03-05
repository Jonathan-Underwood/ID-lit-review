#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import smtplib
from email.message import EmailMessage
from pathlib import Path
import urllib.error
import urllib.request


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Email a generated literature digest.")
    parser.add_argument(
        "--provider",
        choices=["smtp", "brevo"],
        default="smtp",
        help="Email transport provider (default: smtp).",
    )
    recipients = parser.add_mutually_exclusive_group(required=True)
    recipients.add_argument("--to", help="Recipient email address.")
    recipients.add_argument(
        "--to-file",
        help="Path to text file with one recipient email per line. Lines starting with # are ignored.",
    )
    parser.add_argument("--subject", required=True, help="Email subject line.")
    parser.add_argument("--markdown", required=True, help="Path to digest markdown file.")
    parser.add_argument("--pdf", help="Optional path to digest PDF attachment.")
    parser.add_argument(
        "--body-mode",
        choices=["summary", "full"],
        default="summary",
        help="Email body mode: concise summary (default) or full markdown body.",
    )
    parser.add_argument(
        "--smtp-timeout-seconds",
        type=float,
        default=20.0,
        help="SMTP connect timeout in seconds (default: 20).",
    )
    parser.add_argument(
        "--smtp-security",
        choices=["auto", "starttls", "ssl", "none"],
        default="auto",
        help="SMTP security mode (default: auto; ssl for 465, starttls otherwise).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate inputs and print email summary without sending.",
    )
    return parser.parse_args()


def build_summary_body(markdown_text: str) -> str:
    headline = "Weekly ID + General Medicine Literature Digest"
    for line in markdown_text.splitlines():
        if line.startswith("# "):
            headline = line[2:].strip()
            break

    entries: list[tuple[str, str]] = []
    seen_urls: set[str] = set()
    current_title = ""
    title_pat = re.compile(r"^\s*\d+\.\s+\*\*(.+?)\*\*\s*$")
    pubmed_pat = re.compile(r"https://pubmed\.ncbi\.nlm\.nih\.gov/\d+/?")

    for raw in markdown_text.splitlines():
        line = raw.strip()
        title_match = title_pat.match(line)
        if title_match:
            current_title = title_match.group(1).strip()
            continue
        if "PubMed:" not in line:
            continue
        url_match = pubmed_pat.search(line)
        if not url_match:
            continue
        url = url_match.group(0)
        if url in seen_urls:
            continue
        seen_urls.add(url)
        title = current_title or "Untitled paper"
        entries.append((title, url))

    lines = [headline, "", "Key papers (title + PubMed):"]
    if entries:
        lines.extend([f"- {title} — {url}" for title, url in entries])
    else:
        lines.append("- None found")
    return "\n".join(lines)


def load_recipients(args: argparse.Namespace) -> list[str]:
    if args.to:
        return [args.to.strip()]
    to_file = Path(args.to_file)
    if not to_file.exists():
        raise SystemExit(f"Recipient file not found: {to_file}")
    recipients: list[str] = []
    for raw in to_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        recipients.append(line)
    if not recipients:
        raise SystemExit(f"No recipients found in: {to_file}")
    return recipients


def send_via_smtp(args: argparse.Namespace, recipient: str, body: str, pdf_path: Path | None) -> None:
    msg = EmailMessage()
    msg["Subject"] = args.subject
    msg["From"] = os.getenv("SMTP_FROM", os.getenv("SMTP_USER", "unset-from"))
    msg["To"] = recipient
    msg.set_content(body)

    if pdf_path is not None:
        msg.add_attachment(
            pdf_path.read_bytes(),
            maintype="application",
            subtype="pdf",
            filename=pdf_path.name,
        )

    smtp_host = os.getenv("SMTP_HOST")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER")
    smtp_password = os.getenv("SMTP_PASSWORD")
    smtp_from = os.getenv("SMTP_FROM", smtp_user)

    if not all([smtp_host, smtp_user, smtp_password, smtp_from]):
        raise SystemExit("Missing SMTP env vars. Set SMTP_HOST, SMTP_USER, SMTP_PASSWORD, SMTP_FROM.")

    msg.replace_header("From", smtp_from)

    security = args.smtp_security
    if security == "auto":
        security = "ssl" if smtp_port == 465 else "starttls"

    print(f"Connecting to SMTP server {smtp_host}:{smtp_port} (security={security}) ...")
    if security == "ssl":
        with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=args.smtp_timeout_seconds) as server:
            print("SMTP SSL connected, logging in ...")
            server.login(smtp_user, smtp_password)
            print("SMTP login successful, sending message ...")
            server.send_message(msg)
    else:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=args.smtp_timeout_seconds) as server:
            if security == "starttls":
                server.starttls()
                print("SMTP STARTTLS established, logging in ...")
            else:
                print("SMTP plaintext session, logging in ...")
            server.login(smtp_user, smtp_password)
            print("SMTP login successful, sending message ...")
            server.send_message(msg)


def send_via_brevo(args: argparse.Namespace, recipient: str, body: str, pdf_path: Path | None) -> None:
    api_key = os.getenv("BREVO_API_KEY")
    sender_email = os.getenv("BREVO_SENDER_EMAIL")
    sender_name = os.getenv("BREVO_SENDER_NAME", "")
    if not all([api_key, sender_email]):
        raise SystemExit(
            "Missing Brevo env vars. Set BREVO_API_KEY and BREVO_SENDER_EMAIL"
            " (optional: BREVO_SENDER_NAME)."
        )

    payload: dict[str, object] = {
        "sender": {"email": sender_email, "name": sender_name},
        "to": [{"email": recipient}],
        "subject": args.subject,
        "textContent": body,
    }
    if pdf_path is not None:
        payload["attachment"] = [
            {
                "name": pdf_path.name,
                "content": base64.b64encode(pdf_path.read_bytes()).decode("ascii"),
            }
        ]

    request = urllib.request.Request(
        url="https://api.brevo.com/v3/smtp/email",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "accept": "application/json",
            "content-type": "application/json",
            "api-key": api_key,
        },
        method="POST",
    )
    print("Sending via Brevo API ...")
    try:
        with urllib.request.urlopen(request, timeout=args.smtp_timeout_seconds) as response:
            _ = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"Brevo API error: HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"Brevo API request failed: {exc}") from exc


def main() -> int:
    args = parse_args()
    recipients = load_recipients(args)
    md_path = Path(args.markdown)
    if not md_path.exists():
        raise SystemExit(f"Markdown file not found: {md_path}")
    markdown_text = md_path.read_text(encoding="utf-8")
    body = markdown_text if args.body_mode == "full" else build_summary_body(markdown_text)

    pdf_path: Path | None = None
    if args.pdf:
        pdf_path = Path(args.pdf)
        if not pdf_path.exists():
            raise SystemExit(f"PDF file not found: {pdf_path}")

    if args.dry_run:
        print("Dry run: email payload validated.")
        print(f"Provider: {args.provider}")
        if args.to:
            print(f"To: {args.to}")
        else:
            print(f"To file: {args.to_file}")
            print(f"Recipient count: {len(recipients)}")
        print(f"Subject: {args.subject}")
        print(f"Body mode: {args.body_mode}")
        print(f"Markdown: {md_path}")
        print(f"PDF: {args.pdf if args.pdf else 'none'}")
        return 0

    for recipient in recipients:
        if args.provider == "brevo":
            send_via_brevo(args=args, recipient=recipient, body=body, pdf_path=pdf_path)
        else:
            send_via_smtp(args=args, recipient=recipient, body=body, pdf_path=pdf_path)
        print(f"Digest sent to {recipient}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
