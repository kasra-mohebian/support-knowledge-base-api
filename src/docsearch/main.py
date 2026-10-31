"""FastAPI application factory; start with uvicorn docsearch.main:create_app --factory."""

from contextlib import asynccontextmanager
from dataclasses import dataclass
import hmac
import os
from pathlib import Path
import re
import sqlite3
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .store import DuplicateDocument, InvalidQuery, StaleVersion, Store


Category = Literal["general", "accounts", "billing", "delivery"]
SearchMode = Literal["any", "all"]
SQLITE_MAX_INT = 2**63 - 1
DocumentID = Annotated[int, Field(gt=0, le=SQLITE_MAX_INT)]


@dataclass(frozen=True)
class Settings:
    database_path: Path
    api_key: str

    @classmethod
    def from_env(cls):
        return cls(Path(os.getenv("DOCSEARCH_DB", "var/documents.sqlite3")),
                   os.getenv("DOCSEARCH_API_KEY", ""))

    def validate(self):
        if not re.fullmatch(r"[A-Za-z0-9_-]{24,128}", self.api_key):
            raise ValueError("Set DOCSEARCH_API_KEY to 24–128 ASCII letters, digits, '_' or '-'.")


class DocumentInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=16000)
    source: str = Field(min_length=1, max_length=200)
    category: Category = "general"

    @field_validator("title", "content", "source")
    @classmethod
    def clean_text(cls, value):
        value = value.strip()
        if not value or any(ord(c) < 32 and c not in "\n\t\r" for c in value):
            raise ValueError("Use non-empty text without control characters.")
        return value


class DocumentUpdate(DocumentInput):
    expected_version: int = Field(gt=0, le=SQLITE_MAX_INT)


class Document(DocumentInput):
    id: int
    version: int
    created_at: str
    updated_at: str


class DocumentPage(BaseModel):
    items: list[Document]
    total: int
    limit: int
    offset: int


class SearchHit(BaseModel):
    id: int
    title: str
    source: str
    category: Category
    version: int
    excerpt: str
    rank: float


class SearchResults(BaseModel):
    query: str
    mode: SearchMode
    items: list[SearchHit]


class BodyLimit:
    """Bound actual request bytes, including when Content-Length is absent or false."""
    def __init__(self, app, maximum=65536):
        self.app, self.maximum = app, maximum

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in {"POST", "PUT", "PATCH"}:
            await self.app(scope, receive, send)
            return
        messages, size = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            size += len(message.get("body", b""))
            if size > self.maximum:
                await JSONResponse({"detail": "Request body exceeds 64 KiB."}, 413)(scope, receive, send)
                return
            messages.append(message)
            if not message.get("more_body", False):
                break
        iterator = iter(messages)

        async def replay():
            return next(iterator, {"type": "http.disconnect"})

        await self.app(scope, replay, send)


def create_app(settings: Settings | None = None):
    settings = settings or Settings.from_env()
    store = Store(settings.database_path)

    @asynccontextmanager
    async def lifespan(app):
        settings.validate()
        store.initialize()
        yield

    app = FastAPI(title="Support Knowledge Base API", version="0.3.0", lifespan=lifespan,
                  description="Maintain support guides and find source excerpts. No generated answers.")
    app.add_middleware(BodyLimit)
    header = APIKeyHeader(name="X-API-Key", auto_error=False)

    def authorize(key: Annotated[str | None, Depends(header)]):
        if key is None or not hmac.compare_digest(key.encode(), settings.api_key.encode()):
            raise HTTPException(401, "Invalid or missing API key.")

    protected = [Depends(authorize)]

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # FastAPI's default response includes submitted values; keep text out of errors.
        details = [{"loc": e["loc"], "msg": e["msg"], "type": e["type"]} for e in exc.errors()]
        return JSONResponse({"detail": details}, status_code=422)

    @app.exception_handler(sqlite3.OperationalError)
    async def storage_error(request, exc):
        return JSONResponse({"detail": "Storage temporarily unavailable."}, status_code=503)

    @app.get("/health", tags=["operations"])
    def health():
        return {"status": "ok"}

    @app.post("/documents", response_model=Document, status_code=201,
              dependencies=protected, tags=["documents"])
    def add_document(document: DocumentInput, response: Response):
        try:
            result = store.add(**document.model_dump())
        except DuplicateDocument:
            raise HTTPException(409, "An identical document already exists.") from None
        response.headers["Location"] = f"/documents/{result['id']}"
        return result

    @app.get("/documents", response_model=DocumentPage, dependencies=protected, tags=["documents"])
    def list_documents(limit: int = Query(20, ge=1, le=50), offset: int = Query(0, ge=0, le=100000),
                       category: Category | None = None):
        return store.list(limit, offset, category)

    @app.get("/documents/{document_id}", response_model=Document, dependencies=protected, tags=["documents"])
    def get_document(document_id: DocumentID):
        result = store.get(document_id)
        if result is None:
            raise HTTPException(404, "Document not found.")
        return result

    @app.put("/documents/{document_id}", response_model=Document, dependencies=protected, tags=["documents"])
    def update_document(document_id: DocumentID, document: DocumentUpdate):
        try:
            result = store.update(document_id, **document.model_dump())
        except StaleVersion:
            raise HTTPException(409, "Document changed. Fetch the latest version before editing.") from None
        except DuplicateDocument:
            raise HTTPException(409, "An identical document already exists.") from None
        if result is None:
            raise HTTPException(404, "Document not found.")
        return result

    @app.delete("/documents/{document_id}", status_code=204, dependencies=protected, tags=["documents"])
    def delete_document(document_id: DocumentID):
        if not store.delete(document_id):
            raise HTTPException(404, "Document not found.")
        return Response(status_code=204)

    @app.get("/search", response_model=SearchResults, dependencies=protected, tags=["search"])
    def search(q: str = Query(min_length=1, max_length=200), limit: int = Query(10, ge=1, le=50),
               category: Category | None = None, mode: SearchMode = "any"):
        try:
            return {"query": q, "mode": mode, "items": store.search(q, limit, category, mode)}
        except InvalidQuery as exc:
            raise HTTPException(422, str(exc)) from None

    return app
