"""Load only the included fictional documents through the API."""
import json
import os
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def main():
    key = os.environ.get("DOCSEARCH_API_KEY")
    if not key:
        raise SystemExit("Set DOCSEARCH_API_KEY to the server's key first.")
    documents = json.loads(Path(__file__).with_name("documents.json").read_text())
    for doc in documents:
        request = Request("http://127.0.0.1:8000/documents", data=json.dumps(doc).encode(),
                          headers={"Content-Type": "application/json", "X-API-Key": key})
        try:
            with urlopen(request, timeout=10) as response:
                print("created:", json.load(response)["title"])
        except HTTPError as exc:
            if exc.code == 409:
                print("already exists:", doc["title"])
            else:
                raise SystemExit(f"Request failed with HTTP {exc.code}.") from None


if __name__ == "__main__":
    main()
