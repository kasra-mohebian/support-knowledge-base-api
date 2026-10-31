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
    hits = client.get("/search", params={"q": "parcel tracking", "mode": "all"}).json()["items"]
    assert {h["id"] for h in hits} == {a["id"], b["id"]}
    assert hits[0]["id"] == a["id"]
    assert client.get("/search?q=parcel+nonexistent&mode=all").json()["items"] == []


def test_queries_are_literal_not_sql_or_fts_commands(client):
    client.post("/documents", json=DOC)
    for q in ['"', "parcel OR nonexistent", "parcel' UNION SELECT 1 --", "title:parcel"]:
        r = client.get("/search", params={"q": q, "mode": "all"})
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
    assert store.search("tracking reference", 10, mode="all") == []


@pytest.mark.parametrize("value", [2**63, 10**100])
@pytest.mark.parametrize("method", ["GET", "PUT", "DELETE"])
def test_out_of_range_document_ids_are_validation_errors(client, value, method):
    kwargs = {"json": DOC | {"expected_version": 1}} if method == "PUT" else {}
    assert client.request(method, f"/documents/{value}", **kwargs).status_code == 422


@pytest.mark.parametrize("value", [2**63, 10**100])
def test_out_of_range_versions_are_validation_errors(client, value):
    doc = client.post("/documents", json=DOC).json()
    r = client.put(f"/documents/{doc['id']}", json=DOC | {"expected_version": value})
    assert r.status_code == 422
    assert client.get(f"/documents/{doc['id']}").json()["version"] == 1


def test_maximum_sqlite_id_does_not_crash(client):
    for method in ["GET", "DELETE", "PUT"]:
        kwargs = {"json": DOC | {"expected_version": 1}} if method == "PUT" else {}
        assert client.request(method, f"/documents/{2**63-1}", **kwargs).status_code == 404


def test_any_search_finds_partial_matches_but_all_stays_precise(client):
    client.post("/documents", json=DOC)
    any_result = client.get("/search?q=parcel+nonexistent").json()
    assert any_result["mode"] == "any" and len(any_result["items"]) == 1
    assert client.get("/search?q=parcel+nonexistent&mode=all").json()["items"] == []


def test_support_question_regressions_on_fictional_guides(client):
    import json
    examples = Path(__file__).parents[1] / "examples/documents.json"
    for doc in json.loads(examples.read_text()):
        client.post("/documents", json=doc)
    for query, category, title in [
        ("forgot password", "accounts", "Resetting a demo account password"),
        ("How do I track my parcel?", "delivery", "Tracking a parcel"),
        ("change my delivery address", "delivery", "Changing a delivery address"),
    ]:
        hits = client.get("/search", params={"q": query, "category": category}).json()["items"]
        assert hits and hits[0]["title"] == title
    assert client.get("/search?q=unknownword").json()["items"] == []


@pytest.mark.parametrize("query", ["how do I", "!!!", "   "])
def test_queries_without_content_words_are_rejected(client, query):
    assert client.get("/search", params={"q": query}).status_code == 422


def test_invalid_search_mode_is_rejected(client):
    assert client.get("/search?q=parcel&mode=unexpected").status_code == 422


@pytest.mark.parametrize("field", ["title", "content", "source"])
@pytest.mark.parametrize("control", ["\x7f", "\x80", "\x85", "\x9f"])
def test_unicode_control_characters_are_rejected(client, field, control):
    response = client.post("/documents", json=DOC | {field: "a" + control + "b"})
    assert response.status_code == 422
    assert client.get("/documents").json()["total"] == 0


@pytest.mark.parametrize("field", ["title", "content", "source"])
def test_invisible_formatting_alone_is_not_document_text(client, field):
    assert client.post("/documents", json=DOC | {field: " \u200b\u200c "}).status_code == 422


def test_persian_half_space_and_multiline_content_are_preserved(client):
    doc = {"title": "راهنمای حساب‌های کاربری", "content": "می‌توانید رمز را بازنشانی کنید.\nخط دوم\tنمونه\r\nپایان",
           "source": "راهنمای ساختگی"}
    response = client.post("/documents", json=doc)
    assert response.status_code == 201
    assert response.json()["title"] == doc["title"]
    assert response.json()["content"] == doc["content"]


def test_different_category_alone_does_not_create_a_distinct_document(client):
    assert client.post("/documents", json=DOC | {"category": "delivery"}).status_code == 201
    assert client.post("/documents", json=DOC | {"category": "accounts"}).status_code == 409
    page = client.get("/documents").json()
    assert page["total"] == 1 and page["items"][0]["category"] == "delivery"
