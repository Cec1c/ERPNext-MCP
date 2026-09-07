"""Durable, target/identity-scoped plans and complete artifacts. No execution retries."""

from __future__ import annotations

import base64
import json
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .config import Settings
from .requests import canonical, digest, pointer


class StateStore:
    def __init__(self, settings: Settings):
        self.settings = settings
        directory = Path(settings.state_dir)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = directory / "state.sqlite3"
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS artifacts (
                    id TEXT PRIMARY KEY, identity TEXT NOT NULL, mime TEXT NOT NULL,
                    body BLOB NOT NULL, size INTEGER NOT NULL, hash TEXT NOT NULL,
                    ready INTEGER NOT NULL, created REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS plans (
                    id TEXT PRIMARY KEY, identity TEXT NOT NULL, hash TEXT NOT NULL,
                    body BLOB NOT NULL, status TEXT NOT NULL, expires REAL NOT NULL,
                    result BLOB, confirmation TEXT, created REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    plan_id TEXT NOT NULL, at REAL NOT NULL, event TEXT NOT NULL,
                    details BLOB NOT NULL
                );
            """)
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def _capacity(self, db, size: int) -> None:
        current = db.execute("SELECT coalesce(sum(length(body)),0) FROM artifacts").fetchone()[0]
        current += db.execute(
            "SELECT coalesce(sum(length(body)+coalesce(length(result),0)),0) FROM plans"
        ).fetchone()[0]
        if current + size > self.settings.max_state_bytes:
            raise ValueError("Local state storage limit reached; archive state before continuing")

    def begin_upload(self, size: int, sha256: str, mime: str) -> dict:
        if not 0 <= size <= self.settings.max_artifact_bytes:
            raise ValueError("Artifact size exceeds configured limit")
        if not re.fullmatch("[0-9a-f]{64}", sha256):
            raise ValueError("Provide the source SHA-256 as 64 lowercase hexadecimal characters")
        identifier = uuid.uuid4().hex
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._capacity(db, size)
            db.execute(
                "INSERT INTO artifacts VALUES (?,?,?,?,?,?,?,?)",
                (identifier, self.settings.identity, mime, b"", size, sha256, 0, time.time()),
            )
        return {
            "artifact_id": identifier,
            "expected_bytes": size,
            "sha256": sha256,
            "next_offset": 0,
        }

    def _artifact(self, db, identifier: str):
        row = db.execute(
            "SELECT * FROM artifacts WHERE id=? AND identity=?",
            (identifier, self.settings.identity),
        ).fetchone()
        if row is None:
            raise ValueError("Artifact not found for this target/identity")
        return row

    def append(self, identifier: str, offset: int, encoded: str) -> dict:
        chunk = base64.b64decode(encoded, validate=True)
        if len(chunk) > 262144:
            raise ValueError("Upload chunks must not exceed 256 KiB")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._artifact(db, identifier)
            if row["ready"] or len(row["body"]) != offset:
                raise ValueError("Upload is finalized or offset does not match received bytes")
            body = bytes(row["body"]) + chunk
            if len(body) > row["size"]:
                raise ValueError("Upload exceeds declared source length")
            self._capacity(db, len(chunk))
            db.execute("UPDATE artifacts SET body=? WHERE id=?", (body, identifier))
        return {"artifact_id": identifier, "next_offset": len(body), "complete": False}

    def finalize(self, identifier: str) -> dict:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._artifact(db, identifier)
            if len(row["body"]) != row["size"] or digest(row["body"]) != row["hash"]:
                raise ValueError("Input is incomplete or source SHA-256 does not match")
            db.execute("UPDATE artifacts SET ready=1 WHERE id=?", (identifier,))
        return {
            "artifact_id": identifier,
            "size_bytes": row["size"],
            "sha256": row["hash"],
            "complete": True,
        }

    def put(self, body: bytes, mime: str = "application/json") -> dict:
        if len(body) > self.settings.max_artifact_bytes:
            raise ValueError(
                "Complete artifact exceeds configured limit; no partial artifact saved"
            )
        identifier = uuid.uuid4().hex
        hashed = digest(body)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT id FROM artifacts WHERE identity=? AND hash=? AND mime=? AND ready=1",
                (self.settings.identity, hashed, mime),
            ).fetchone()
            if existing:
                return {
                    "artifact_id": existing["id"],
                    "size_bytes": len(body),
                    "sha256": hashed,
                    "mime_type": mime,
                }
            self._capacity(db, len(body))
            db.execute(
                "INSERT INTO artifacts VALUES (?,?,?,?,?,?,?,?)",
                (identifier, self.settings.identity, mime, body, len(body), hashed, 1, time.time()),
            )
        return {
            "artifact_id": identifier,
            "size_bytes": len(body),
            "sha256": hashed,
            "mime_type": mime,
        }

    def get(self, identifier: str) -> bytes:
        with self.connect() as db:
            row = self._artifact(db, identifier)
        if not row["ready"] or digest(row["body"]) != row["hash"]:
            raise ValueError("Artifact has not passed completeness verification")
        return bytes(row["body"])

    def read(
        self,
        identifier: str,
        *,
        offset: int = 0,
        limit: int = 4096,
        json_pointer: str | None = None,
    ) -> dict:
        body = self.get(identifier)
        if offset < 0 or not 1 <= limit <= 4096:
            raise ValueError("offset must be non-negative and limit between 1 and 4096")
        if json_pointer is not None:
            selected = pointer(json.loads(body), json_pointer)
            body = canonical(selected)
        chunk = body[offset : offset + limit]
        end = offset + len(chunk)
        return {
            "artifact_id": identifier,
            "offset": offset,
            "returned_bytes": len(chunk),
            "total_bytes": len(body),
            "sha256": digest(body),
            "json_pointer": json_pointer,
            "encoding": "base64",
            "data": base64.b64encode(chunk).decode(),
            "has_more": end < len(body),
            "next_offset": end if end < len(body) else None,
            "complete": end >= len(body) and offset == 0,
        }

    def deliver(self, result: object) -> dict:
        body = canonical(result)
        if len(body) <= self.settings.inline_result_bytes:
            return result if isinstance(result, dict) else {"data": result}
        saved = self.put(body)
        envelope = {
            "inline_complete": False,
            "result_id": saved["artifact_id"],
            **saved,
            "message": "Complete result saved. Read it in chunks with erpnext_result_read.",
        }
        if isinstance(result, dict):
            for key in (
                "ok",
                "status",
                "plan_id",
                "confirmation_required",
                "request_sha256",
                "target",
                "label",
                "operation_count",
                "expires_at",
                "agent_instruction",
            ):
                if key in result:
                    envelope[key] = result[key]
        return envelope

    def prepare(self, payload: dict) -> dict:
        identifier, body = uuid.uuid4().hex, canonical(payload)
        if len(body) > self.settings.max_artifact_bytes:
            raise ValueError("Complete plan exceeds artifact limit; split into explicit batches")
        hashed, now = digest(body), time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._capacity(db, len(body))
            db.execute(
                "INSERT INTO plans VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    identifier,
                    self.settings.identity,
                    hashed,
                    body,
                    "prepared",
                    now + self.settings.plan_ttl,
                    None,
                    None,
                    now,
                ),
            )
        return self.plan(identifier)

    def plan(self, identifier: str) -> dict:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM plans WHERE id=? AND identity=?",
                (identifier, self.settings.identity),
            ).fetchone()
        if row is None:
            raise ValueError("Plan not found for this target/identity")
        if digest(row["body"]) != row["hash"]:
            raise ValueError("Plan integrity check failed")
        return {
            "plan_id": row["id"],
            "request_sha256": row["hash"],
            "status": row["status"],
            "expires_at": row["expires"],
            "payload": json.loads(row["body"]),
            "result": json.loads(row["result"]) if row["result"] else None,
        }

    def list_plans(self, *, status: str | None = None, limit: int = 20, offset: int = 0) -> dict:
        if not 1 <= limit <= 100 or offset < 0:
            raise ValueError("limit must be 1..100 and offset non-negative")
        where, args = "identity=?", [self.settings.identity]
        if status:
            where += " AND status=?"
            args.append(status)
        with self.connect() as db:
            total = db.execute(f"SELECT count(*) FROM plans WHERE {where}", args).fetchone()[0]
            rows = db.execute(
                f"SELECT id,hash,status,expires,created FROM plans WHERE {where} ORDER BY created DESC,id DESC LIMIT ? OFFSET ?",
                [*args, limit, offset],
            ).fetchall()
        return {
            "plans": [
                {
                    "plan_id": row["id"],
                    "request_sha256": row["hash"],
                    "status": row["status"],
                    "expires_at": row["expires"],
                    "created_at": row["created"],
                }
                for row in rows
            ],
            "total_count": total,
            "has_more": offset + len(rows) < total,
            "next_offset": offset + len(rows) if offset + len(rows) < total else None,
        }

    def claim(self, identifier: str, hashed: str, confirmation: str | None) -> bool:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            result = db.execute(
                """UPDATE plans SET status='running', confirmation=?
                WHERE id=? AND identity=? AND hash=? AND status='prepared' AND expires>?""",
                (confirmation, identifier, self.settings.identity, hashed, time.time()),
            )
            if result.rowcount:
                db.execute(
                    "INSERT INTO events VALUES (?,?,?,?)",
                    (
                        identifier,
                        time.time(),
                        "claimed",
                        canonical(
                            {
                                "confirmation_source": "agent_attestation"
                                if confirmation
                                else None,
                                "user_confirmation": confirmation,
                            }
                        ),
                    ),
                )
            return result.rowcount == 1

    def record(self, identifier: str, status: str, result: dict) -> None:
        body = canonical(result)
        with self.connect() as db:
            db.execute(
                "UPDATE plans SET status=?, result=? WHERE id=? AND identity=?",
                (status, body, identifier, self.settings.identity),
            )
            db.execute(
                "INSERT INTO events VALUES (?,?,?,?)",
                (
                    identifier,
                    time.time(),
                    status,
                    canonical({"completed_count": result.get("completed_count", 0)}),
                ),
            )
