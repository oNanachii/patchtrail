"""Invite identities on disk; short-lived BYOK credentials only in memory."""
import argparse
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time

from .providers import DeepSeekProvider, ProviderError
from .service import ServiceError
from .storage import Store


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class MemberRegistry:
    def __init__(self, data_dir, *, clock=time.time, ttl=3600):
        self.directory = Path(data_dir) / "members"
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)
        self.database = self.directory / "identities.sqlite3"
        self.clock, self.ttl = clock, ttl
        self.lock = threading.RLock()
        self.sessions = {}
        self.timers = {}
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS members (id TEXT PRIMARY KEY, invite_hash TEXT UNIQUE NOT NULL, revoked INTEGER NOT NULL DEFAULT 0)")
        self.database.chmod(0o600)

    def connect(self):
        return sqlite3.connect(self.database, timeout=5)

    def create(self, output):
        code, member_id = secrets.token_urlsafe(32), secrets.token_hex(16)
        # O_EXCL prevents accidental overwrite; no invitation goes to stdout.
        fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            with self.connect() as db:
                if db.execute("SELECT count(*) FROM members").fetchone()[0] >= 32:
                    raise ValueError("Invitation capacity reached")
                db.execute("INSERT INTO members(id,invite_hash) VALUES (?,?)", (member_id, digest(code)))
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                fd = None
                json.dump({"member_id": member_id, "invite_code": code}, file)
                file.write("\n")
        finally:
            if fd is not None:
                os.close(fd)
        return member_id

    def revoke(self, member_id):
        if not re.fullmatch(r"[a-f0-9]{32}", member_id):
            raise ValueError("Invalid member identity")
        with self.lock:
            with self.connect() as db:
                db.execute("UPDATE members SET revoked=1 WHERE id=?", (member_id,))
            for token_hash in list(self.sessions):
                if self.sessions[token_hash]["member_id"] == member_id:
                    self.logout(token_hash)
        # Retain every record, but release durable active-run capacity. Otherwise
        # a revoked identity could leave the global queue permanently blocked.
        database = self.directory / member_id / "runs.sqlite3"
        if database.is_file():
            Store(database).revoke_active()

    def member_ids(self):
        with self.connect() as db:
            return [row[0] for row in db.execute("SELECT id FROM members")]

    def _prune(self):
        now = self.clock()
        for token_hash in list(self.sessions):
            if self.sessions[token_hash]["expires_at"] <= now:
                self.logout(token_hash)

    def login(self, invite, api_key, model):
        if not re.fullmatch(r"[A-Za-z0-9_\-]{16,256}", api_key):
            raise ServiceError("invalid_key", "Enter a DeepSeek API key without whitespace", 422)
        with self.lock, self.connect() as db:
            self._prune()
            row = db.execute("SELECT id FROM members WHERE invite_hash=? AND revoked=0", (digest(invite),)).fetchone()
            if not row:
                raise ServiceError("invalid_invitation", "Invitation is invalid or revoked", 401)
            if len(self.sessions) >= 64:
                raise ServiceError("session_capacity", "Session capacity reached", 429)
            token = secrets.token_urlsafe(32)
            session = {"member_id": row[0], "expires_at": int(self.clock() + self.ttl), "api_key": api_key}
            token_hash = digest(token)
            self.sessions[token_hash] = session
            timer = threading.Timer(self.ttl, self.logout, args=(token_hash,))
            timer.daemon = True
            self.timers[token_hash] = timer
            timer.start()
            return {"session_token": token, "role": "member", "member_id": row[0],
                    "expires_at": session["expires_at"], "model": model}

    def session(self, token_hash):
        with self.lock, self.connect() as db:
            self._prune()
            session = self.sessions.get(token_hash)
            if session:
                row = db.execute("SELECT revoked FROM members WHERE id=?", (session["member_id"],)).fetchone()
                if not row or row[0]:
                    self.logout(token_hash)
                    session = None
            if not session:
                raise ServiceError("session_expired", "Session expired, signed out or invitation revoked; sign in again", 401)
            return dict(session)

    def logout(self, token_hash):
        with self.lock:
            self.sessions.pop(token_hash, None)
            timer = self.timers.pop(token_hash, None)
            if timer:
                timer.cancel()

    def close(self):
        with self.lock:
            for token_hash in list(self.sessions):
                self.logout(token_hash)

    def provider(self, token_hash, settings, transport=None):
        self.session(token_hash)
        def credential():
            try:
                return self.session(token_hash)["api_key"]
            except ServiceError:
                raise ProviderError("Member session ended before dispatch; no new model request sent") from None
        return DeepSeekProvider(replace(settings, api_key="", base_url="https://api.deepseek.com"),
                                transport, credential_supplier=credential)


def main():
    parser = argparse.ArgumentParser(description="Local invitation administration; credentials are never printed")
    parser.add_argument("action", choices=["create", "revoke"])
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--member-id")
    args = parser.parse_args()
    registry = MemberRegistry(args.data_dir)
    if args.action == "create":
        if not args.output:
            parser.error("create requires --output pointing to a new private local file")
        registry.create(args.output)
        print("Invitation written to the requested private file.")
    else:
        if not args.member_id or not re.fullmatch(r"[a-f0-9]{32}", args.member_id):
            parser.error("revoke requires a valid --member-id")
        registry.revoke(args.member_id)
        print("Invitation revoked; subsequent model requests are disabled.")


if __name__ == "__main__":
    main()
