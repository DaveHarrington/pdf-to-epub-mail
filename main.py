#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "fastapi==0.115.0",
#   "uvicorn[standard]==0.30.6",
#   "python-multipart==0.0.9",
#   "python-dotenv==1.0.1",
# ]
# ///

import os
import tempfile
import subprocess
import logging
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, UploadFile, File, Header, HTTPException
from fastapi.responses import JSONResponse

import smtplib
from email.message import EmailMessage

from dotenv import load_dotenv
load_dotenv()

logger = logging.getLogger("pdf2epub")

BEARER_TOKEN = os.environ.get("BEARER_TOKEN", "change-me")
KINDLE_EMAIL = os.environ.get("KINDLE_EMAIL", "change-me")

SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
SMTP_FROM = os.environ.get("SMTP_FROM", SMTP_USER or "noreply@example.com")
SMTP_TLS = os.environ.get("SMTP_TLS", "true").lower() in ("1", "true", "yes")

EBOOK_CONVERT_BIN = os.environ.get("EBOOK_CONVERT_BIN", "ebook-convert")
MAX_PDF_BYTES = int(os.environ.get("MAX_PDF_BYTES", str(25 * 1024 * 1024)))  # 25MB


app = FastAPI()


def require_bearer(authorization: Optional[str]) -> None:
    logger.info("auth: checking Authorization header")

    if not authorization:
        logger.warning("auth: missing Authorization header")
        raise HTTPException(status_code=401, detail="Missing bearer token")

    if not authorization.startswith("Bearer "):
        logger.warning("auth: Authorization header not Bearer scheme")
        raise HTTPException(status_code=401, detail="Missing bearer token")

    token = authorization.split(" ", 1)[1].strip()
    if not token:
        logger.warning("auth: Bearer token empty")
        raise HTTPException(status_code=401, detail="Missing bearer token")

    if token != BEARER_TOKEN:
        logger.warning("auth: invalid bearer token")
        raise HTTPException(status_code=403, detail="Invalid bearer token")

    logger.info("auth: ok")


def pdf_to_epub(pdf_path: Path, epub_path: Path) -> None:
    logger.info("convert: starting pdf->epub via calibre")
    logger.info("convert: input=%s output=%s bin=%s", pdf_path, epub_path, EBOOK_CONVERT_BIN)

    cmd = [EBOOK_CONVERT_BIN, str(pdf_path), str(epub_path)]
    logger.info("convert: running: %s", " ".join(cmd))

    try:
        p = subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out = (p.stdout or b"").decode(errors="replace").strip()
        err = (p.stderr or b"").decode(errors="replace").strip()
        if out:
            logger.info("convert: calibre stdout (tail): %s", out[-1000:])
        if err:
            logger.info("convert: calibre stderr (tail): %s", err[-1000:])
    except FileNotFoundError as e:
        logger.exception("convert: ebook-convert not found (is calibre installed?)")
        raise RuntimeError("Conversion failed: ebook-convert not found (install calibre)") from e
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or b"").decode(errors="replace")
        logger.error("convert: calibre failed (rc=%s) stderr (tail): %s", e.returncode, stderr[-2000:])
        raise RuntimeError(f"Conversion failed: {stderr[-2000:]}") from e

    if not epub_path.exists() or epub_path.stat().st_size == 0:
        logger.error("convert: output epub missing/empty at %s", epub_path)
        raise RuntimeError("Conversion failed: output EPUB was not created")

    logger.info("convert: success (bytes=%d)", epub_path.stat().st_size)


def send_email_with_attachment(
    subject: str,
    body: str,
    to_addr: str,
    attachment_path: Path,
) -> None:
    logger.info("email: preparing message to=%s from=%s", to_addr, SMTP_FROM)

    if not SMTP_HOST:
        logger.error("email: SMTP_HOST is not set")
        raise RuntimeError("SMTP_HOST is not set")

    if not attachment_path.exists():
        logger.error("email: attachment missing at %s", attachment_path)
        raise RuntimeError("Attachment not found")

    data = attachment_path.read_bytes()
    logger.info("email: attachment=%s bytes=%d", attachment_path.name, len(data))

    msg = EmailMessage()
    msg["From"] = SMTP_FROM
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body)
    msg.add_attachment(
        data,
        maintype="application",
        subtype="epub+zip",
        filename=attachment_path.name,
    )

    logger.info("email: connecting host=%s port=%d tls=%s user_set=%s",
                SMTP_HOST, SMTP_PORT, SMTP_TLS, bool(SMTP_USER))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as s:
            if SMTP_TLS:
                logger.info("email: starting TLS")
                s.starttls()

            if SMTP_USER:
                logger.info("email: logging in as %s", SMTP_USER)
                s.login(SMTP_USER, SMTP_PASS)

            logger.info("email: sending")
            s.send_message(msg)

    except smtplib.SMTPAuthenticationError:
        logger.exception("email: authentication failed")
        raise RuntimeError("Email failed: SMTP authentication failed (check SMTP_USER/SMTP_PASS)") from None
    except Exception as e:
        logger.exception("email: send failed")
        raise RuntimeError(f"Email failed: {e}") from e

    logger.info("email: sent ok")


@app.post("/convert")
async def convert_pdf_to_epub_endpoint(
    authorization: Optional[str] = Header(default=None),
    file: UploadFile = File(...),
):
    logger.info("request: /convert received filename=%s content_type=%s", file.filename, file.content_type)

    require_bearer(authorization)

    if file.content_type not in ("application/pdf", "application/octet-stream"):
        logger.warning("request: invalid content_type=%s", file.content_type)
        raise HTTPException(status_code=400, detail=f"Expected PDF, got {file.content_type}")

    logger.info("request: reading upload into memory")
    raw = await file.read()
    logger.info("request: read bytes=%d (max=%d)", len(raw), MAX_PDF_BYTES)

    if len(raw) > MAX_PDF_BYTES:
        logger.warning("request: too large bytes=%d", len(raw))
        raise HTTPException(status_code=413, detail="PDF too large")

    logger.info("request: starting temp workspace")
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        pdf_path = td_path / "input.pdf"
        epub_path = td_path / "output.epub"

        logger.info("request: writing input pdf to %s", pdf_path)
        pdf_path.write_bytes(raw)

        try:
            pdf_to_epub(pdf_path, epub_path)
        except Exception as e:
            logger.exception("request: conversion failed")
            raise HTTPException(status_code=500, detail=str(e))

        try:
            logger.info("request: emailing epub to KINDLE_EMAIL=%s", KINDLE_EMAIL)
            send_email_with_attachment(
                subject="Your converted EPUB",
                body="Attached is the EPUB converted from your PDF.",
                to_addr=KINDLE_EMAIL,
                attachment_path=epub_path,
            )
        except Exception as e:
            logger.exception("request: email failed")
            raise HTTPException(status_code=502, detail=str(e))

    logger.info("request: /convert done ok")
    return JSONResponse({"ok": True, "sent_to": KINDLE_EMAIL})

@app.post("/test-email")
async def test_email(authorization: Optional[str] = Header(default=None)):
    logger.info("request: /test-email received")
    require_bearer(authorization)

    try:
        logger.info("test-email: sending to KINDLE_EMAIL=%s", KINDLE_EMAIL)
        send_email_with_attachment(
            subject="SMTP test (no attachment)",
            body="If you received this, SMTP is working.",
            to_addr=KINDLE_EMAIL,
            attachment_path=Path("/dev/null"),  # not used
        )
    except Exception:
        # send_email_with_attachment expects an attachment; so do a plain send here instead
        try:
            if not SMTP_HOST:
                raise RuntimeError("SMTP_HOST is not set")

            msg = EmailMessage()
            msg["From"] = SMTP_FROM
            msg["To"] = "ozzy.dave@gmail.com"
            msg["Subject"] = "SMTP test (no attachment)"
            msg.set_content("If you received this, SMTP is working.")

            logger.info("test-email: connecting host=%s port=%d tls=%s user_set=%s",
                        SMTP_HOST, SMTP_PORT, SMTP_TLS, bool(SMTP_USER))

            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as s:
                if SMTP_TLS:
                    logger.info("test-email: starting TLS")
                    s.starttls()
                if SMTP_USER:
                    logger.info("test-email: logging in as %s", SMTP_USER)
                    s.login(SMTP_USER, SMTP_PASS)

                logger.info("test-email: sending")
                s.send_message(msg)

            logger.info("test-email: sent ok")
        except Exception as e:
            logger.exception("test-email: failed")
            raise HTTPException(status_code=502, detail=str(e))

    return JSONResponse({"ok": True, "sent_to": KINDLE_EMAIL})

@app.get("/")
def root():
    return "ok"

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))

