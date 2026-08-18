#!/usr/bin/env python3
# ============================================================
#  Gnostic Echo Protocol v10.0 (Drop-in upgrade for v9.6)
#  - Durable file-queue agent for RHF Memory Bridge pings
#  - Atomic claiming, persistent dedupe, retry/backoff, quarantine
#  - Full JSON body by default (data fidelity contract)
# ============================================================

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import logging.handlers
import os
import re
import shutil
import smtplib
import socket
import sqlite3
import ssl
import sys
import time
import traceback
from dataclasses import dataclass
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

APP_NAME = "Gnostic Echo Protocol"
APP_VERSION = "10.0"

DEFAULT_QUEUE = "./cognitive_relay"
DEFAULT_POLL_INTERVAL = 10

# -----------------------------
# Helpers
# -----------------------------

def now_ts() -> int:
    return int(time.time())

def safe_mkdir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)

def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()

def sha256_str(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", errors="replace")).hexdigest()

def clamp_int(x: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, x))

def is_transient_smtp_error(e: Exception) -> bool:
    # Heuristic: treat connection/timeouts/server-disconnects/4xx as transient.
    transient_types = (
        smtplib.SMTPServerDisconnected,
        smtplib.SMTPConnectError,
        smtplib.SMTPHeloError,
        smtplib.SMTPRecipientsRefused,
        smtplib.SMTPDataError,
        smtplib.SMTPResponseException,
        TimeoutError,
        socket.timeout,
        ConnectionError,
        OSError,
    )
    if isinstance(e, transient_types):
        if isinstance(e, smtplib.SMTPResponseException):
            # 4xx = transient, 5xx = permanent-ish
            try:
                return 400 <= int(e.smtp_code) < 500
            except Exception:
                return True
        return True
    return False

def parse_urgency(urgency: Any) -> Tuple[Optional[int], Optional[int]]:
    """
    Accepts formats like:
      "40/12" -> (40, 12)
      40      -> (40, None)
    """
    if urgency is None:
        return (None, None)
    if isinstance(urgency, (int, float)):
        return (int(urgency), None)
    if isinstance(urgency, str):
        m = re.match(r"^\s*(\d+)\s*(?:/\s*(\d+)\s*)?$", urgency)
        if m:
            a = int(m.group(1))
            b = int(m.group(2)) if m.group(2) else None
            return (a, b)
    return (None, None)

def json_dumps_pretty(data: Any) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False)

def atomic_rename(src: Path, dst: Path) -> None:
    # On Windows, Path.rename is atomic when within same volume.
    dst.parent.mkdir(parents=True, exist_ok=True)
    src.rename(dst)

# -----------------------------
# Config
# -----------------------------

@dataclass
class EchoConfig:
    enabled: bool
    queue_path: Path
    poll_interval: int

    sender_email: str
    receiver_email: Union[str, List[str]]
    app_password: str
    smtp_server: str
    smtp_port: int
    use_ssl: bool
    starttls: bool
    smtp_timeout_sec: int

    # Optional safety controls
    dry_run: bool
    max_emails_per_hour: int
    min_seconds_between_emails: int
    max_retries_before_quarantine: int
    retry_backoff_base_sec: int
    retry_backoff_max_sec: int

    # Formatting
    body_mode: str  # "full_json" | "fragments_pretty"
    max_body_chars: int
    attach_json_when_truncated: bool

    # Heartbeat/logging
    log_dir: Path
    heartbeat_file: Path

def load_config(config_path: Path) -> Optional[EchoConfig]:
    try:
        if not config_path.exists():
            print(f"❌ FATAL ERROR: config.json not found at: {config_path.resolve()}")
            return None

        with open(config_path, "r", encoding="utf-8") as f:
            cfg_root = json.load(f)

        if "echo_protocol_config" not in cfg_root:
            print("❌ FATAL ERROR: 'echo_protocol_config' section missing in config.json.")
            return None

        c = cfg_root["echo_protocol_config"]

        enabled = bool(c.get("enabled", False))

        # Queue path: keep your legacy default ./cognitive_relay, but allow absolute paths.
        queue_raw = c.get("queue_path", DEFAULT_QUEUE)
        queue_path = Path(queue_raw)

        poll_interval = int(c.get("poll_interval", DEFAULT_POLL_INTERVAL))

        sender_email = str(c.get("sender_email", "")).strip()
        receiver_email = c.get("receiver_email", "")
        app_password = str(c.get("app_password", "")).strip()
        smtp_server = str(c.get("smtp_server", "smtp.gmail.com")).strip()
        smtp_port = int(c.get("smtp_port", 587))

        # SMTP options (new, optional)
        use_ssl = bool(c.get("use_ssl", False))
        starttls = bool(c.get("starttls", True if not use_ssl else False))
        smtp_timeout_sec = int(c.get("smtp_timeout_sec", 20))

        # Safety throttles (new, optional)
        dry_run = bool(c.get("dry_run", False))
        max_emails_per_hour = int(c.get("max_emails_per_hour", 60))
        min_seconds_between_emails = int(c.get("min_seconds_between_emails", 2))
        max_retries_before_quarantine = int(c.get("max_retries_before_quarantine", 25))
        retry_backoff_base_sec = int(c.get("retry_backoff_base_sec", 15))
        retry_backoff_max_sec = int(c.get("retry_backoff_max_sec", 15 * 60))

        # Formatting (new, optional)
        body_mode = str(c.get("body_mode", "full_json")).strip().lower()
        if body_mode not in ("full_json", "fragments_pretty"):
            body_mode = "full_json"
        max_body_chars = int(c.get("max_body_chars", 150_000))
        attach_json_when_truncated = bool(c.get("attach_json_when_truncated", True))

        # Logging/heartbeat (new, optional)
        log_dir = Path(c.get("log_dir", "./echo_logs"))
        heartbeat_file = Path(c.get("heartbeat_file", "./echo_heartbeat.json"))

        return EchoConfig(
            enabled=enabled,
            queue_path=queue_path,
            poll_interval=max(1, poll_interval),

            sender_email=sender_email,
            receiver_email=receiver_email,
            app_password=app_password,
            smtp_server=smtp_server,
            smtp_port=smtp_port,
            use_ssl=use_ssl,
            starttls=starttls,
            smtp_timeout_sec=max(5, smtp_timeout_sec),

            dry_run=dry_run,
            max_emails_per_hour=max(1, max_emails_per_hour),
            min_seconds_between_emails=max(0, min_seconds_between_emails),
            max_retries_before_quarantine=max(0, max_retries_before_quarantine),
            retry_backoff_base_sec=max(1, retry_backoff_base_sec),
            retry_backoff_max_sec=max(5, retry_backoff_max_sec),

            body_mode=body_mode,
            max_body_chars=max(10000, max_body_chars),
            attach_json_when_truncated=attach_json_when_truncated,

            log_dir=log_dir,
            heartbeat_file=heartbeat_file,
        )

    except json.JSONDecodeError as e:
        print(f"❌ FATAL ERROR: Could not parse config.json: {e}")
        return None
    except Exception as e:
        print(f"❌ FATAL ERROR: Unexpected error loading config: {e}")
        return None

# -----------------------------
# Logging
# -----------------------------

def setup_logging(log_dir: Path) -> None:
    safe_mkdir(log_dir)
    log_file = log_dir / "echo_protocol.log"

    logger = logging.getLogger()
    logger.setLevel(logging.INFO)

    fmt = logging.Formatter(
        fmt="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)

    # Rotating file
    fh = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=2_000_000, backupCount=10, encoding="utf-8"
    )
    fh.setLevel(logging.INFO)
    fh.setFormatter(fmt)

    logger.handlers.clear()
    logger.addHandler(ch)
    logger.addHandler(fh)

# -----------------------------
# Persistent Dedupe (SQLite)
# -----------------------------

class DedupeDB:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        safe_mkdir(db_path.parent)
        self.conn = sqlite3.connect(str(db_path))
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS processed (
                ping_hash TEXT PRIMARY KEY,
                first_seen INTEGER NOT NULL,
                last_seen INTEGER NOT NULL,
                filename TEXT,
                subject TEXT,
                agent_name TEXT,
                urgency TEXT
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS throttle (
                key TEXT PRIMARY KEY,
                value TEXT
            )
            """
        )
        self.conn.commit()

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass

    def has(self, ping_hash: str) -> bool:
        cur = self.conn.execute("SELECT 1 FROM processed WHERE ping_hash=?", (ping_hash,))
        return cur.fetchone() is not None

    def mark(self, ping_hash: str, meta: Dict[str, Any]) -> None:
        ts = now_ts()
        self.conn.execute(
            """
            INSERT INTO processed(ping_hash, first_seen, last_seen, filename, subject, agent_name, urgency)
            VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(ping_hash) DO UPDATE SET last_seen=excluded.last_seen
            """,
            (
                ping_hash,
                ts,
                ts,
                str(meta.get("filename", "")),
                str(meta.get("subject", "")),
                str(meta.get("agent_name", "")),
                str(meta.get("urgency", "")),
            ),
        )
        self.conn.commit()

    def get_throttle(self, key: str) -> Optional[str]:
        cur = self.conn.execute("SELECT value FROM throttle WHERE key=?", (key,))
        row = cur.fetchone()
        return row[0] if row else None

    def set_throttle(self, key: str, value: str) -> None:
        self.conn.execute(
            """
            INSERT INTO throttle(key, value) VALUES(?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (key, value),
        )
        self.conn.commit()

# -----------------------------
# Retry Metadata (per file)
# -----------------------------

class RetryMeta:
    def __init__(self, meta_dir: Path):
        self.meta_dir = meta_dir
        safe_mkdir(meta_dir)

    def _path_for(self, original_name: str) -> Path:
        safe_name = re.sub(r"[^a-zA-Z0-9_.-]+", "_", original_name)
        return self.meta_dir / f"{safe_name}.meta.json"

    def read(self, original_name: str) -> Dict[str, Any]:
        p = self._path_for(original_name)
        if not p.exists():
            return {"attempts": 0, "first_seen": now_ts(), "last_error_ts": 0, "last_error": ""}
        try:
            with open(p, "r", encoding="utf-8") as f:
                d = json.load(f)
            if "attempts" not in d:
                d["attempts"] = 0
            if "first_seen" not in d:
                d["first_seen"] = now_ts()
            if "last_error_ts" not in d:
                d["last_error_ts"] = 0
            if "last_error" not in d:
                d["last_error"] = ""
            return d
        except Exception:
            return {"attempts": 0, "first_seen": now_ts(), "last_error_ts": 0, "last_error": "meta_corrupt"}

    def write(self, original_name: str, data: Dict[str, Any]) -> None:
        p = self._path_for(original_name)
        tmp = p.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        tmp.replace(p)

    def clear(self, original_name: str) -> None:
        p = self._path_for(original_name)
        try:
            if p.exists():
                p.unlink()
        except Exception:
            pass

# -----------------------------
# Email Building + Sending
# -----------------------------

def normalize_recipients(receiver_email: Union[str, List[str]]) -> List[str]:
    if isinstance(receiver_email, str) and receiver_email.strip():
        return [receiver_email.strip()]
    if isinstance(receiver_email, list):
        cleaned = [str(x).strip() for x in receiver_email if str(x).strip()]
        return cleaned if cleaned else []
    return []

def build_email(
    cfg: EchoConfig,
    ping: Dict[str, Any],
    ping_filename: str,
) -> Tuple[MIMEMultipart, Optional[bytes], str]:
    urgency = ping.get("urgency", "N/A")
    subject_text = ping.get("subject", "Ping Update")
    agent_name = ping.get("agent_name", "Echo Agent")
    source = ping.get("source", "unknown")

    # Body modes:
    if cfg.body_mode == "fragments_pretty":
        body_lines: List[str] = []
        body_lines.append(f"DREAM INSIGHT ({str(agent_name).lower()}/{source})")
        body_lines.append("")
        if isinstance(ping.get("body_fragments"), list) and ping["body_fragments"]:
            frags = ping["body_fragments"]
            body_lines.append(f"Seed: {ping.get('seed_text','')}")
            body_lines.append("")
            body_lines.append("Chain:")
            for i, frag in enumerate(frags, start=1):
                body_lines.append(f"  {i}. {frag}")
        else:
            body_lines.append("No fragments provided. Full ping JSON follows:")
            body_lines.append("")
            body_lines.append(json_dumps_pretty(ping))
        body_content = "\n".join(body_lines)
    else:
        # Default: full JSON dump (highest fidelity contract)
        body_content = json_dumps_pretty(ping)

    # Truncation policy
    attachment_bytes: Optional[bytes] = None
    trunc_notice = ""
    if len(body_content) > cfg.max_body_chars:
        trunc_notice = (
            f"\n\n---\n[Echo] Body truncated at {cfg.max_body_chars} chars.\n"
            f"Ping file: {ping_filename}\n"
        )
        truncated = body_content[: cfg.max_body_chars] + trunc_notice
        if cfg.attach_json_when_truncated:
            attachment_bytes = json_dumps_pretty(ping).encode("utf-8", errors="replace")
        body_content = truncated

    recipients = normalize_recipients(cfg.receiver_email)
    receiver_string_for_header = ", ".join(recipients) if recipients else "(no recipients configured)"

    msg = MIMEMultipart()
    msg["From"] = f"{agent_name} via Echo Protocol <{cfg.sender_email}>"
    msg["To"] = receiver_string_for_header
    msg["Subject"] = f"[Urgency {urgency}] {subject_text}"

    # Plaintext body
    msg.attach(MIMEText(body_content, "plain", "utf-8"))

    # Optional JSON attachment (for long pings)
    if attachment_bytes is not None:
        part = MIMEApplication(attachment_bytes, _subtype="json")
        part.add_header("Content-Disposition", "attachment", filename=f"{ping_filename}.json")
        msg.attach(part)

    return msg, attachment_bytes, receiver_string_for_header

def smtp_send(cfg: EchoConfig, recipients: List[str], msg: MIMEMultipart) -> None:
    if not recipients:
        raise RuntimeError("No valid recipients configured (receiver_email empty).")
    if not cfg.sender_email or not cfg.app_password:
        raise RuntimeError("sender_email/app_password not configured.")

    # TLS context
    context = ssl.create_default_context()

    if cfg.use_ssl:
        with smtplib.SMTP_SSL(cfg.smtp_server, cfg.smtp_port, timeout=cfg.smtp_timeout_sec, context=context) as server:
            server.ehlo()
            server.login(cfg.sender_email, cfg.app_password)
            server.sendmail(cfg.sender_email, recipients, msg.as_string())
    else:
        with smtplib.SMTP(cfg.smtp_server, cfg.smtp_port, timeout=cfg.smtp_timeout_sec) as server:
            server.ehlo()
            if cfg.starttls:
                server.starttls(context=context)
                server.ehlo()
            server.login(cfg.sender_email, cfg.app_password)
            server.sendmail(cfg.sender_email, recipients, msg.as_string())

# -----------------------------
# File Queue Operations
# -----------------------------

def is_ping_file(p: Path) -> bool:
    # Strictly process only *.json at queue root (matches your current behavior)
    return p.is_file() and p.suffix.lower() == ".json" and p.parent.is_dir()

def claim_file(p: Path) -> Optional[Path]:
    """
    Atomically claims a file by renaming it to *.processing.
    If rename fails, another process probably has it, or it is being written.
    """
    try:
        claimed = p.with_suffix(p.suffix + ".processing")
        atomic_rename(p, claimed)
        return claimed
    except Exception:
        return None

def unclaim_file(claimed: Path) -> Path:
    """
    Returns claimed *.processing back to original *.json name.
    """
    if not claimed.name.endswith(".processing"):
        return claimed
    original = claimed.with_name(claimed.name.replace(".processing", ""))
    try:
        atomic_rename(claimed, original)
        return original
    except Exception:
        return claimed

def move_to_dir(src: Path, dst_dir: Path, suffix_note: Optional[str] = None) -> Path:
    safe_mkdir(dst_dir)
    # A claimed file is named "<ping>.json.processing". That suffix is a claim
    # marker for crash recovery, not part of the ping's identity — strip it on
    # the way into the archive. Without this the archive fills with
    # "*.json.processing" files that look stuck forever and cannot be opened by
    # anything expecting .json (NotebookLM, editors, the loader).
    name = src.name
    if name.endswith(".processing"):
        name = name[: -len(".processing")]
    stem, suffix = Path(name).stem, Path(name).suffix
    base = name
    if suffix_note:
        stamp = now_ts()
        base = f"{stem}__{suffix_note}__{stamp}{suffix}"
    dst = dst_dir / base
    try:
        atomic_rename(src, dst)
        return dst
    except Exception:
        # Cross-device fallback
        shutil.move(str(src), str(dst))
        return dst

def read_ping_json(p: Path) -> Dict[str, Any]:
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)

def compute_ping_hash(ping: Dict[str, Any], filename: str) -> str:
    """
    Hash stable ping identity. If you resend the same ping content, it won't spam.
    Includes subject + urgency + fragments + seed + source + agent_name.
    """
    core = {
        "agent_name": ping.get("agent_name"),
        "urgency": ping.get("urgency"),
        "subject": ping.get("subject"),
        "source": ping.get("source"),
        "seed_text": ping.get("seed_text"),
        "body_fragments": ping.get("body_fragments"),
        "completion_message": ping.get("completion_message"),
        "save_confirmation": ping.get("save_confirmation"),
        "filename_hint": filename,
    }
    blob = json.dumps(core, ensure_ascii=False, sort_keys=True).encode("utf-8", errors="replace")
    return sha256_bytes(blob)

# -----------------------------
# Throttle Controls
# -----------------------------

def can_send_now(cfg: EchoConfig, db: DedupeDB) -> Tuple[bool, str]:
    """
    Two throttles:
    1) min seconds between emails
    2) max emails per rolling hour
    """
    ts = now_ts()

    # Min seconds between sends
    last_send_raw = db.get_throttle("last_send_ts")
    last_send = int(last_send_raw) if last_send_raw and last_send_raw.isdigit() else 0
    if cfg.min_seconds_between_emails > 0 and (ts - last_send) < cfg.min_seconds_between_emails:
        return False, f"Throttle(min_seconds_between_emails={cfg.min_seconds_between_emails})"

    # Rolling hour count
    hour_bucket = ts // 3600
    key_bucket = f"hour_bucket_{hour_bucket}"
    count_raw = db.get_throttle(key_bucket)
    count = int(count_raw) if count_raw and count_raw.isdigit() else 0
    if count >= cfg.max_emails_per_hour:
        return False, f"Throttle(max_emails_per_hour={cfg.max_emails_per_hour})"

    return True, "OK"

def mark_sent(cfg: EchoConfig, db: DedupeDB) -> None:
    ts = now_ts()
    db.set_throttle("last_send_ts", str(ts))
    hour_bucket = ts // 3600
    key_bucket = f"hour_bucket_{hour_bucket}"
    count_raw = db.get_throttle(key_bucket)
    count = int(count_raw) if count_raw and count_raw.isdigit() else 0
    db.set_throttle(key_bucket, str(count + 1))

# -----------------------------
# Heartbeat
# -----------------------------

def write_heartbeat(cfg: EchoConfig, status: Dict[str, Any]) -> None:
    try:
        hb = dict(status)
        hb["app"] = APP_NAME
        hb["version"] = APP_VERSION
        hb["timestamp"] = now_ts()
        tmp = cfg.heartbeat_file.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(hb, f, indent=2, ensure_ascii=False)
        tmp.replace(cfg.heartbeat_file)
    except Exception:
        # Heartbeat must never kill the agent.
        pass

# -----------------------------
# Main Processing Loop
# -----------------------------

def process_one_file(
    cfg: EchoConfig,
    db: DedupeDB,
    retry_meta: RetryMeta,
    queue_dir: Path,
    processed_dir: Path,
    quarantine_dir: Path,
    claimed_path: Path,
) -> bool:
    """
    Returns True if file was successfully processed (sent or dry-run) and moved to processed.
    Returns False if kept for retry or moved to quarantine.
    """
    original_name = claimed_path.name.replace(".processing", "")
    meta = retry_meta.read(original_name)

    # Backoff check
    attempts = int(meta.get("attempts", 0))
    last_error_ts = int(meta.get("last_error_ts", 0))
    if attempts > 0 and last_error_ts > 0:
        backoff = clamp_int(cfg.retry_backoff_base_sec * (2 ** min(attempts, 10)), cfg.retry_backoff_base_sec, cfg.retry_backoff_max_sec)
        if (now_ts() - last_error_ts) < backoff:
            # Put it back and skip for now
            unclaim_file(claimed_path)
            logging.info(f"⏳ Backoff active for {original_name} (attempts={attempts}, backoff={backoff}s).")
            return False

    try:
        ping = read_ping_json(claimed_path)
    except json.JSONDecodeError as e:
        logging.error(f"☣️ Corrupt JSON in {original_name}: {e}")
        retry_meta.clear(original_name)
        move_to_dir(claimed_path, quarantine_dir, suffix_note="CORRUPT_JSON")
        return False
    except Exception as e:
        logging.error(f"☣️ Failed reading {original_name}: {e}")
        retry_meta.clear(original_name)
        move_to_dir(claimed_path, quarantine_dir, suffix_note="READ_ERROR")
        return False

    # Minimal schema sanity: don't quarantine for missing optional fields—fill defaults.
    if not isinstance(ping, dict):
        logging.error(f"☣️ Ping data not a JSON object in {original_name}.")
        retry_meta.clear(original_name)
        move_to_dir(claimed_path, quarantine_dir, suffix_note="INVALID_SCHEMA")
        return False

    ping.setdefault("subject", "Ping Update")
    ping.setdefault("urgency", "N/A")
    ping.setdefault("agent_name", "Echo Agent")
    ping.setdefault("source", "unknown")

    ping_hash = compute_ping_hash(ping, filename=original_name)

    # Dedupe gate
    if db.has(ping_hash):
        logging.info(f"🔁 Duplicate ping suppressed: {original_name} (hash={ping_hash[:10]})")
        retry_meta.clear(original_name)
        # Move to processed to clear queue
        move_to_dir(claimed_path, processed_dir, suffix_note="DUPLICATE")
        return True

    # Throttle gate
    ok_send, reason = can_send_now(cfg, db)
    if not ok_send:
        logging.info(f"🧯 Send throttled ({reason}). Keeping {original_name} in queue.")
        # Unclaim and keep for later
        unclaim_file(claimed_path)
        return False

    recipients = normalize_recipients(cfg.receiver_email)
    msg, _, to_header = build_email(cfg, ping, ping_filename=original_name)

    # Send (or dry-run)
    try:
        if cfg.dry_run:
            logging.info(f"🧪 DRY-RUN: Would send ping {original_name} to {to_header}")
            logging.info(f"🧪 Subject: {msg['Subject']}")
        else:
            smtp_send(cfg, recipients, msg)
            logging.info(f"✅ Ping sent: {original_name} -> {to_header}")

        # Mark as sent
        mark_sent(cfg, db)
        db.mark(ping_hash, {
            "filename": original_name,
            "subject": ping.get("subject", ""),
            "agent_name": ping.get("agent_name", ""),
            "urgency": ping.get("urgency", ""),
        })

        retry_meta.clear(original_name)
        move_to_dir(claimed_path, processed_dir)
        return True

    except smtplib.SMTPAuthenticationError:
        # Auth errors are not transient; quarantine to avoid infinite loop.
        logging.error("❌ SMTP Authentication Error: Check sender_email/app_password in config.json.")
        retry_meta.clear(original_name)
        move_to_dir(claimed_path, quarantine_dir, suffix_note="AUTH_ERROR")
        return False

    except Exception as e:
        transient = is_transient_smtp_error(e)
        err_txt = f"{type(e).__name__}: {e}"
        logging.error(f"❌ Send failed for {original_name} (transient={transient}): {err_txt}")

        # Update retry meta
        meta["attempts"] = int(meta.get("attempts", 0)) + 1
        meta["last_error_ts"] = now_ts()
        meta["last_error"] = err_txt
        retry_meta.write(original_name, meta)

        if not transient:
            logging.error(f"☣️ Non-transient failure. Quarantining {original_name}.")
            retry_meta.clear(original_name)
            move_to_dir(claimed_path, quarantine_dir, suffix_note="NONTRANSIENT_SEND_FAIL")
            return False

        # Transient: put back for retry unless exceeded max retries
        if cfg.max_retries_before_quarantine > 0 and meta["attempts"] >= cfg.max_retries_before_quarantine:
            logging.error(f"☣️ Max retries reached ({meta['attempts']}). Quarantining {original_name}.")
            retry_meta.clear(original_name)
            move_to_dir(claimed_path, quarantine_dir, suffix_note="MAX_RETRIES")
            return False

        # Return to queue for retry later
        unclaim_file(claimed_path)
        return False

def main() -> int:
    ap = argparse.ArgumentParser(description=f"{APP_NAME} v{APP_VERSION}")
    ap.add_argument("--config", type=str, default="config.json", help="Path to config.json")
    ap.add_argument("--once", action="store_true", help="Process available pings once then exit.")
    ap.add_argument("--dry-run", action="store_true", help="Override config to dry-run mode.")
    args = ap.parse_args()

    cfg = load_config(Path(args.config))
    if not cfg:
        return 2

    if args.dry_run:
        cfg.dry_run = True

    setup_logging(cfg.log_dir)

    logging.info("=" * 60)
    logging.info(f"--- 🔔 {APP_NAME} v{APP_VERSION} Initialized ---")
    logging.info("=" * 60)

    if not cfg.enabled:
        logging.info("Echo Protocol is disabled in config.json. Exiting.")
        return 0

    queue_dir = cfg.queue_path
    processed_dir = queue_dir / "processed_pings"
    quarantine_dir = queue_dir / "quarantine"
    meta_dir = queue_dir / ".echo_meta"
    state_dir = queue_dir / ".echo_state"

    # Ensure dirs
    try:
        safe_mkdir(queue_dir)
        safe_mkdir(processed_dir)
        safe_mkdir(quarantine_dir)
        safe_mkdir(meta_dir)
        safe_mkdir(state_dir)

        logging.info(f"👀 Watching queue directory: {queue_dir.resolve()}")
        logging.info(f"🗄️ Processed pings ->: {processed_dir.resolve()}")
        logging.info(f"☣️ Quarantine ->: {quarantine_dir.resolve()}")

    except Exception as e:
        logging.error(f"❌ FATAL ERROR: Could not create necessary directories: {e}")
        return 3

    db = DedupeDB(state_dir / "echo_dedupe.sqlite")
    retry_meta = RetryMeta(meta_dir)

    logging.info("--- Agent Active ---")

    last_heartbeat = 0

    try:
        while True:
            processed_any = False

            # Recover any stranded *.processing files (e.g., crash mid-send)
            for stranded in queue_dir.glob("*.json.processing"):
                # If there's no backoff active, return to queue so it can retry
                original_name = stranded.name.replace(".processing", "")
                meta = retry_meta.read(original_name)
                attempts = int(meta.get("attempts", 0))
                last_error_ts = int(meta.get("last_error_ts", 0))
                if attempts == 0 and last_error_ts == 0:
                    logging.info(f"🧯 Recovering stranded claim: {stranded.name} -> queue")
                    unclaim_file(stranded)

            # Process queue files
            for f in sorted(queue_dir.glob("*.json")):
                if not is_ping_file(f):
                    continue

                claimed = claim_file(f)
                if not claimed:
                    # Could be in use or being written; skip quietly.
                    continue

                logging.info(f"📩 Found ping request: {claimed.name.replace('.processing','')}")
                processed_any = True
                process_one_file(
                    cfg=cfg,
                    db=db,
                    retry_meta=retry_meta,
                    queue_dir=queue_dir,
                    processed_dir=processed_dir,
                    quarantine_dir=quarantine_dir,
                    claimed_path=claimed,
                )

            # Heartbeat (every ~10s)
            if now_ts() - last_heartbeat >= 10:
                try:
                    queue_count = len(list(queue_dir.glob("*.json")))
                except Exception:
                    queue_count = -1
                write_heartbeat(cfg, {
                    "enabled": cfg.enabled,
                    "queue_path": str(queue_dir.resolve()),
                    "queue_pending": queue_count,
                    "poll_interval": cfg.poll_interval,
                    "dry_run": cfg.dry_run,
                })
                last_heartbeat = now_ts()

            if args.once:
                break

            if not processed_any:
                time.sleep(cfg.poll_interval)
            else:
                # If we processed something, do a short sleep to prevent tight loops
                time.sleep(0.25)

    except KeyboardInterrupt:
        logging.info("🛑 Echo Protocol stopped by user (KeyboardInterrupt).")
    except Exception as e:
        logging.error(f"❌ CRITICAL ERROR: {e}")
        logging.error(traceback.format_exc())
    finally:
        try:
            db.close()
        except Exception:
            pass

    logging.info("💤 Echo Protocol shutdown complete.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
