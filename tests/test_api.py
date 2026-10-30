from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from docsearch.main import create_app, Settings
from docsearch.store import DuplicateDocument, Store


KEY = "test-only-key-not-a-real-secret-123"
HEADERS = {"X-API-Key": KEY}
DOC = {"title": "Parcel tracking", "content": "Track a parcel using the tracking reference.",
       "source": "fictional-support-guide"}


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(Settings(tmp_path / "db.sqlite3", KEY))) as c:
        c.headers.update(HEADERS)
        yield c


def test_document_lifecycle_and_index_cleanup(client):
    created = client.post("/documents", json=DOC)
    assert created.status_code == 201
    document = created.json()
    assert created.headers["location"] == f"/documents/{document['id']}"
    assert client.get(created.headers["location"]).json() == document
    hits = client.get("/search", params={"q": "parcel"}).json()["items"]
    assert [h["id"] for h in hits] == [document["id"]]
    assert hits[0]["source"] == DOC["source"]
    assert client.delete(created.headers["location"]).status_code == 204
    assert client.get(created.headers["location"]).status_code == 404
    assert client.delete(created.headers["location"]).status_code == 404
    assert client.get("/search", params={"q": "parcel"}).json()["items"] == []


def test_authentication_covers_every_data_endpoint(client):
    client.headers.clear()
    for method, path, kwargs in [
        ("GET", "/documents", {}), ("GET", "/documents/1", {}),
        ("DELETE", "/documents/1", {}), ("GET", "/search?q=parcel", {}),
        ("POST", "/documents", {"json": DOC}),
        ("PUT", "/documents/1", {"json": DOC | {"expected_version": 1}}),
    ]:
        assert client.request(method, path, **kwargs).status_code == 401
        assert client.request(method, path, headers={"X-API-Key": "wrong"}, **kwargs).status_code == 401
    assert client.get("/health").json() == {"status": "ok"}


@pytest.mark.parametrize("change", [
    {"title": " "}, {"content": ""}, {"content": "x" * 16001},
    {"source": "\x00secret"}, {"title": 123}, {"surprise": True},
])
def test_validation(client, change):
    assert client.post("/documents", json=DOC | change).status_code == 422


def test_validation_does_not_echo_submitted_text(client):
    marker = "private-canary-test-input"
    response = client.post("/documents", json=DOC | {"extra": marker})
    assert response.status_code == 422
    assert marker not in response.text


def test_duplicate_and_trimmed_duplicate(client):
    assert client.post("/documents", json=DOC).status_code == 201
    assert client.post("/documents", json=DOC).status_code == 409
    assert client.post("/documents", json=DOC | {"title": " Parcel tracking "}).status_code == 409
    assert client.get("/documents").json()["total"] == 1


def test_same_body_different_source_is_distinct(client):
    assert client.post("/documents", json=DOC).status_code == 201
    assert client.post("/documents", json=DOC | {"source": "second-guide"}).status_code == 201


def test_pagination_and_stable_order(client):
    for i in range(4):
        assert client.post("/documents", json=DOC | {"title": f"Guide {i}"}).status_code == 201
    page = client.get("/documents", params={"offset": 1, "limit": 2}).json()
    assert [x["title"] for x in page["items"]] == ["Guide 1", "Guide 2"]
    assert page["total"] == 4


@pytest.mark.parametrize("path", ["/documents?limit=51", "/documents?offset=-1",
                                  "/documents/0", "/search?q=x&limit=0", "/search?q=!!!"])
def test_bounded_queries(client, path):
    assert client.get(path).status_code == 422


def test_and_search_title_weight_and_no_match(client):
    a = client.post("/documents", json=DOC).json()
    b = client.post("/documents", json={"title": "Reference", "content": "parcel tracking",
                                      "source": "fictional"}).json()
    hits = client.get("/search", params={"q": "parcel tracking"}).json()["items"]
    assert {h["id"] for h in hits} == {a["id"], b["id"]}
    assert hits[0]["id"] == a["id"]
    assert client.get("/search?q=parcel+nonexistent").json()["items"] == []


def test_queries_are_literal_not_sql_or_fts_commands(client):
    client.post("/documents", json=DOC)
    for q in ['"', "parcel OR nonexistent", "parcel' UNION SELECT 1 --", "title:parcel"]:
        r = client.get("/search", params={"q": q})
        assert r.status_code in (200, 422)
        if r.status_code == 200:
            assert r.json()["items"] == []
    assert client.get("/documents").json()["total"] == 1


def test_unicode_and_plain_text_excerpt(client):
    d = {"title": "Guide", "content": "Café Öffnungszeiten <script>example</script>", "source": "demo"}
    client.post("/documents", json=d)
    r = client.get("/search", params={"q": "cafe"})
    assert len(r.json()["items"]) == 1
    assert r.headers["content-type"].startswith("application/json")
    assert "<script>" in r.json()["items"][0]["excerpt"]  # consumer must render as text


def test_body_limit_counts_actual_bytes(client):
    r = client.post("/documents", content=b"x" * 65537,
                    headers={"Content-Type": "application/json", "Content-Length": "1"})
    assert r.status_code == 413


def test_persists_across_app_restarts(tmp_path):
    settings = Settings(tmp_path / "db.sqlite3", KEY)
    with TestClient(create_app(settings)) as c:
        c.post("/documents", json=DOC, headers=HEADERS)
    with TestClient(create_app(settings)) as c:
        assert len(c.get("/search?q=parcel", headers=HEADERS).json()["items"]) == 1


def test_key_required_before_creating_database(tmp_path):
    path = tmp_path / "not-created.db"
    with pytest.raises(ValueError, match="DOCSEARCH_API_KEY"):
        with TestClient(create_app(Settings(path, ""))):
            pass
    assert not path.exists()


def test_concurrent_duplicate_writes_are_atomic(tmp_path):
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()

    def write(_):
        try:
            store.add(**DOC)
            return "created"
        except DuplicateDocument:
            return "duplicate"

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(write, range(8)))
    assert results.count("created") == 1
    assert results.count("duplicate") == 7
    assert len(store.search("parcel", 10)) == 1


def test_openapi_documents_key_and_response_models(client):
    schema = client.get("/openapi.json").json()
    assert schema["components"]["securitySchemes"]["APIKeyHeader"]["name"] == "X-API-Key"
    assert "security" in schema["paths"]["/search"]["get"]


def test_edit_updates_index_and_rejects_stale_version(client):
    created = client.post("/documents", json=DOC).json()
    path = f"/documents/{created['id']}"
    edit = {"title": "Password reset", "content": "Reset your password using an expiring link.",
            "source": "fictional-account-guide", "category": "accounts", "expected_version": 1}
    result = client.put(path, json=edit)
    assert result.status_code == 200
    assert result.json()["version"] == 2
    assert result.json()["created_at"] == created["created_at"]
    assert result.json()["updated_at"] >= created["updated_at"]
    assert client.get("/search?q=parcel").json()["items"] == []
    assert client.get("/search?q=password").json()["items"][0]["version"] == 2
    assert client.put(path, json=edit).status_code == 409
    assert client.get(path).json() == result.json()


def test_edit_duplicate_rolls_back_document_and_index(client):
    a = client.post("/documents", json=DOC).json()
    other = DOC | {"title": "Another guide", "content": "Special refund instructions"}
    b = client.post("/documents", json=other).json()
    path = f"/documents/{b['id']}"
    assert client.put(path, json=DOC | {"expected_version": 1}).status_code == 409
    assert client.get(path).json() == b
    assert client.get("/search?q=refund").json()["items"][0]["id"] == b["id"]
    assert client.get("/search?q=parcel").json()["items"][0]["id"] == a["id"]


def test_edit_requires_version_and_existing_id(client):
    assert client.put("/documents/999", json=DOC | {"expected_version": 1}).status_code == 404
    assert client.put("/documents/999", json=DOC).status_code == 422
    assert client.put("/documents/999", json=DOC | {"expected_version": True}).status_code == 422


def test_category_filter_applies_to_search_list_and_count(client):
    client.post("/documents", json=DOC | {"category": "delivery"})
    client.post("/documents", json=DOC | {"source": "second-guide", "category": "accounts"})
    for path in ["/documents?category=delivery", "/search?q=parcel&category=delivery"]:
        page = client.get(path).json()
        assert len(page["items"]) == 1
        assert page["items"][0]["category"] == "delivery"
    assert client.get("/documents?category=accounts").json()["total"] == 1
    assert client.get("/documents?category=billing").json()["total"] == 0
    assert client.get("/search?q=parcel&category=billing").json()["items"] == []
    assert client.get("/documents?category=unexpected").status_code == 422
    assert client.post("/documents", json=DOC | {"category": "unexpected"}).status_code == 422


def test_two_writers_cannot_silently_overwrite_one_version(tmp_path):
    from docsearch.store import StaleVersion
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    doc = store.add(**DOC)

    def edit(i):
        try:
            return store.update(doc["id"], 1, **(DOC | {"content": f"revision {i}"}))["version"]
        except StaleVersion:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(edit, range(2)))
    assert results.count(2) == 1
    assert results.count("conflict") == 1
    assert store.get(doc["id"])["version"] == 2


def test_legacy_schema_upgrade_preserves_content_and_search(tmp_path):
    import sqlite3
    from docsearch.store import fingerprint
    path = tmp_path / "db.sqlite3"
    with sqlite3.connect(path) as con:
        con.execute("""CREATE TABLE documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, content TEXT NOT NULL,
            source TEXT NOT NULL, fingerprint TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL DEFAULT '2026-01-01T00:00:00Z')""")
        con.execute("INSERT INTO documents(title,content,source,fingerprint) VALUES (?,?,?,?)",
                    (*DOC.values(), fingerprint(**DOC)))
        con.execute("CREATE VIRTUAL TABLE document_index USING fts5(title,content,content='documents',content_rowid='id')")
        con.execute("INSERT INTO document_index(document_index) VALUES ('rebuild')")
    store = Store(path)
    store.initialize()
    store.initialize()  # restart is idempotent
    doc = store.get(1)
    assert doc["category"] == "general" and doc["version"] == 1
    assert doc["updated_at"] == doc["created_at"]
    assert store.search("parcel", 10)[0]["id"] == 1
    store.update(1, 1, **(DOC | {"content": "replacement"}))
    assert store.search("parcel", 10)  # title still matches
    assert store.search("tracking reference", 10) == []
