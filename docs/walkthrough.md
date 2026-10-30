# A support guide from creation to correction

This example uses invented guides. Start the server using the README and use the same `DOCSEARCH_API_KEY` in the client terminal.

## 1. Create a guide

```bash
curl -s http://127.0.0.1:8000/documents \
  -H "X-API-Key: $DOCSEARCH_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"title":"Delivery timing","content":"Standard delivery takes three days.","source":"fictional-support-policy","category":"delivery"}'
```

The response contains `id`, `version: 1`, and creation/update timestamps. Use the returned ID in subsequent requests; the examples below assume it is `1`.

## 2. Find the relevant guide

```bash
curl -s --get http://127.0.0.1:8000/search \
  -H "X-API-Key: $DOCSEARCH_API_KEY" \
  --data-urlencode 'q=standard delivery' \
  --data-urlencode 'category=delivery'
```

A support agent can open the result's document ID to read the complete guide. The excerpt is a stored passage, not a generated promise about delivery.

## 3. Correct the guide

Imagine the fictional policy changes from three days to five. Send the version you read alongside the replacement content:

```bash
curl -s -X PUT http://127.0.0.1:8000/documents/1 \
  -H "X-API-Key: $DOCSEARCH_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"title":"Delivery timing","content":"Standard delivery takes five days.","source":"fictional-support-policy","category":"delivery","expected_version":1}'
```

The response has `version: 2`. Searching for `five` finds the updated text; searching for `three` no longer finds this guide.

If a second editor sends an edit based on version 1, it gets `409 Conflict`. They should read `/documents/1` again and decide how to combine the changes. The API does not automatically merge edits or silently retry against the new version.

## 4. Remove an obsolete guide

```bash
curl -i -X DELETE http://127.0.0.1:8000/documents/1 \
  -H "X-API-Key: $DOCSEARCH_API_KEY"
```

A successful deletion returns `204` with no response body. The guide disappears from live reads and search. This is not guaranteed erasure from disk pages or backups; see `SECURITY.md`.

## Decisions worth understanding

- **SQLite rather than a separate database server:** the local setup is easy to reproduce. It trades away multi-host operation and high write concurrency.
- **FTS5 rather than a language model:** lexical search is sufficient for a small guide library and works offline. It does not understand synonyms or generate answers.
- **Version checks rather than last-write-wins:** an editor must notice another editor's changes. The check and update are one conditional SQL statement.
- **Database uniqueness rather than an initial existence check:** two requests can arrive together; the database is the final authority on duplicates.
- **Triggers rather than separate API-side index writes:** content and search updates share one transaction, including rollbacks.
- **An API key rather than a user system:** sufficient for the documented local demo; it offers neither per-person accountability nor separate editor/reader permissions.

Before using this as a resume project, run the walkthrough yourself, inspect the SQL and tests, and be able to explain these tradeoffs. The repository was developed with AI assistance; understanding and maintaining it are separate from having working code.
