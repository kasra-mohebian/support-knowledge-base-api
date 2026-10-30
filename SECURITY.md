# Security

This service is designed for local use. Do not expose the development server directly to the internet.

- All document and search endpoints require `X-API-Key`. The same key has full read/write access; there are no separate users or roles.
- `/health`, `/docs`, `/redoc` and `/openapi.json` do not require authentication. They do not expose stored documents.
- Document bodies are limited to 64 KiB at the application boundary. A public deployment also needs upstream request timeouts, connection limits, TLS and rate limits.
- SQL values are bound parameters; search terms are treated as literal words rather than user-controlled FTS expressions.
- The application does not fetch URLs, execute document content, call a model or make outbound API requests.
- Validation errors omit submitted input values. The documented run command disables access logs, which would otherwise include search query strings. The application does not log documents or keys.
- SQLite files contain plaintext, including FTS index data. Use a private directory and suitable operating-system permissions. Deleting a document removes its live API/index entry; it is not guaranteed forensic erasure from database pages, WAL files or backups.
- A source label is untrusted text. Render excerpts and labels as text in clients.
- Database files, environment files and local virtual environments are ignored by Git. Ignore rules do not remove files already committed.

For a security issue, avoid posting keys, documents or personal data in a public issue. Use GitHub private vulnerability reporting if it is enabled on the published repository.
