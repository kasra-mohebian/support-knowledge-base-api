"""SQLite persistence and literal full-text search. No external services."""

from contextlib import contextmanager
import hashlib
from pathlib import Path
import re
import sqlite3


class DuplicateDocument(Exception):
    pass


class InvalidQuery(ValueError):
    pass


class StaleVersion(Exception):
    pass


def fingerprint(title: str, content: str, source: str) -> str:
    # Length prefixes avoid ambiguous concatenations without altering document text.
    parts = [title, content, source]
    return hashlib.sha256("".join(f"{len(p)}:{p}" for p in parts).encode()).hexdigest()


class Store:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connect(self):
        con = sqlite3.connect(self.path, timeout=5)
        con.row_factory = sqlite3.Row
        try:
            with con:
                yield con
        finally:
            con.close()

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as con:
            con.execute("PRAGMA journal_mode=WAL")
            if con.execute("PRAGMA user_version").fetchone()[0] > 1:
                raise RuntimeError("Database schema is newer than this application.")
            con.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS documents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    source TEXT NOT NULL,
                    category TEXT NOT NULL DEFAULT 'general',
                    version INTEGER NOT NULL DEFAULT 1,
                    fingerprint TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS document_index
                    USING fts5(title, content, content='documents', content_rowid='id');
                CREATE TRIGGER IF NOT EXISTS documents_insert AFTER INSERT ON documents BEGIN
                    INSERT INTO document_index(rowid, title, content)
                        VALUES (new.id, new.title, new.content);
                END;
                CREATE TRIGGER IF NOT EXISTS documents_delete AFTER DELETE ON documents BEGIN
                    INSERT INTO document_index(document_index, rowid, title, content)
                        VALUES ('delete', old.id, old.title, old.content);
                END;
                CREATE TRIGGER IF NOT EXISTS documents_update AFTER UPDATE ON documents BEGIN
                    INSERT INTO document_index(document_index, rowid, title, content)
                        VALUES ('delete', old.id, old.title, old.content);
                    INSERT INTO document_index(rowid, title, content)
                        VALUES (new.id, new.title, new.content);
                END;
            """)
            # Upgrade databases created by the initial local prototype without discarding data.
            columns = {r['name'] for r in con.execute("PRAGMA table_info(documents)")}
            for name, definition in [
                ("category", "TEXT NOT NULL DEFAULT 'general'"),
                ("version", "INTEGER NOT NULL DEFAULT 1"),
                ("updated_at", "TEXT NOT NULL DEFAULT ''"),
            ]:
                if name not in columns:
                    con.execute(f"ALTER TABLE documents ADD COLUMN {name} {definition}")
            con.execute("UPDATE documents SET updated_at=created_at WHERE updated_at=''")
            con.execute("PRAGMA user_version=1")

    def add(self, title: str, content: str, source: str, category: str = "general"):
        try:
            with self.connect() as con:
                row = con.execute(
                    "INSERT INTO documents(title,content,source,category,fingerprint) VALUES (?,?,?,?,?) RETURNING *",
                    (title, content, source, category, fingerprint(title, content, source)),
                ).fetchone()
                return self.public(row)
        except sqlite3.IntegrityError as exc:
            if "documents.fingerprint" in str(exc):
                raise DuplicateDocument from exc
            raise

    @staticmethod
    def public(row):
        return {k: row[k] for k in ("id", "title", "content", "source", "category",
                                   "version", "created_at", "updated_at")}

    def update(self, document_id: int, expected_version: int, title: str, content: str,
               source: str, category: str = "general"):
        try:
            with self.connect() as con:
                row = con.execute("""
                    UPDATE documents SET title=?,content=?,source=?,category=?,fingerprint=?,
                        version=version+1, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
                    WHERE id=? AND version=? RETURNING *
                """, (title, content, source, category, fingerprint(title, content, source),
                      document_id, expected_version)).fetchone()
                if row is None:
                    if con.execute("SELECT 1 FROM documents WHERE id=?", (document_id,)).fetchone():
                        raise StaleVersion
                    return None
                return self.public(row)
        except sqlite3.IntegrityError as exc:
            if "documents.fingerprint" in str(exc):
                raise DuplicateDocument from exc
            raise

    def get(self, document_id: int):
        with self.connect() as con:
            row = con.execute("SELECT * FROM documents WHERE id=?", (document_id,)).fetchone()
            return self.public(row) if row else None

    def list(self, limit: int, offset: int, category: str | None = None):
        with self.connect() as con:
            con.execute("BEGIN")  # Rows and count describe the same read snapshot.
            rows = con.execute(
                "SELECT * FROM documents WHERE (? IS NULL OR category=?) ORDER BY id LIMIT ? OFFSET ?",
                (category, category, limit, offset)
            ).fetchall()
            total = con.execute("SELECT count(*) FROM documents WHERE (? IS NULL OR category=?)",
                                (category, category)).fetchone()[0]
            return {"items": [self.public(r) for r in rows], "total": total,
                    "limit": limit, "offset": offset}

    def delete(self, document_id: int):
        with self.connect() as con:
            return con.execute("DELETE FROM documents WHERE id=?", (document_id,)).rowcount == 1

    def search(self, query: str, limit: int, category: str | None = None):
        # Do not expose FTS operators or interpret SQL supplied by the caller.
        # unicode61 handles document tokenization; these literal words are ANDed.
        words = re.findall(r"[^\W_]+", query, flags=re.UNICODE)
        if not words:
            raise InvalidQuery("Query must contain a letter or number.")
        expression = " AND ".join('"' + w + '"' for w in words)
        with self.connect() as con:
            rows = con.execute("""
                SELECT d.id, d.title, d.source, d.category, d.version,
                       snippet(document_index, 1, '', '', ' … ', 32) AS excerpt,
                       bm25(document_index, 5.0, 1.0) AS rank
                FROM document_index JOIN documents d ON d.id=document_index.rowid
                WHERE document_index MATCH ? AND (? IS NULL OR d.category=?)
                ORDER BY rank, d.id LIMIT ?
            """, (expression, category, category, limit)).fetchall()
            return [dict(r) for r in rows]
