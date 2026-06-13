"""Provider-specific restricted searches."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone
from typing import Any


class RestrictedSearchError(RuntimeError):
    pass


def search_arxiv(query: str, cutoff: date, cfg: dict[str, Any]) -> list[dict[str, Any]]:
    n = _int(cfg, "max_results", 10)
    params = urllib.parse.urlencode({
        "search_query": "all:" + query,
        "start": 0,
        "max_results": _int(cfg, "fetch_results", n),
        "sortBy": "relevance",
    })
    xml = _request_text(str(cfg["base_url"]) + "?" + params, timeout=_int(cfg, "timeout", 60))
    root = ET.fromstring(xml)
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    results = []
    for entry in root.findall("atom:entry", ns):
        published = _date(_node_text(entry, "atom:published", ns))
        if published > cutoff:
            continue
        url = _node_text(entry, "atom:id", ns)
        authors = [_node_text(a, "atom:name", ns) for a in entry.findall("atom:author", ns)]
        results.append(_item(
            "arxiv",
            _clean(_node_text(entry, "atom:title", ns)),
            url,
            published,
            ", ".join(a for a in authors if a),
            [_clean(_node_text(entry, "atom:summary", ns))],
        ))
        if len(results) >= n:
            break
    return results


def search_openalex(query: str, cutoff: date, cfg: dict[str, Any]) -> list[dict[str, Any]]:
    params = {
        "search": query,
        "filter": f"to_publication_date:{cutoff.isoformat()}",
        "per-page": str(_int(cfg, "max_results", 10)),
        "sort": "publication_date:desc",
    }
    if cfg.get("mailto"):
        params["mailto"] = str(cfg["mailto"])
    url = str(cfg["base_url"]) + "?" + urllib.parse.urlencode(params)
    data = json.loads(_request_text(url, timeout=_int(cfg, "timeout", 60)))
    results = []
    for work in data.get("results", []):
        if not isinstance(work, dict) or not work.get("publication_date"):
            continue
        published = _date(str(work["publication_date"]))
        if published > cutoff:
            continue
        authors = [
            (a.get("author") or {}).get("display_name", "")
            for a in work.get("authorships", [])
            if isinstance(a, dict)
        ]
        abstract = _openalex_abstract(work.get("abstract_inverted_index"))
        results.append(_item(
            "openalex",
            str(work.get("title") or ""),
            _openalex_url(work),
            published,
            ", ".join(a for a in authors if a),
            [abstract] if abstract else [],
        ))
    return results


def search_exa(query: str, cutoff: date, cfg: dict[str, Any]) -> list[dict[str, Any]]:
    key = str(cfg.get("api_key") or "").strip()
    if not key:
        raise RestrictedSearchError("exa is enabled but providers.exa.api_key is empty")
    body = json.dumps({
        "query": query,
        "type": "auto",
        "numResults": _int(cfg, "max_results", 10),
        "endPublishedDate": cutoff.isoformat() + "T23:59:59Z",
        "contents": {"highlights": True},
    }).encode("utf-8")
    data = json.loads(_request_text(
        str(cfg["base_url"]).rstrip("/") + "/search",
        data=body,
        timeout=_int(cfg, "timeout", 60),
        headers={"x-api-key": key, "Content-Type": "application/json"},
    ))
    results = []
    for row in data.get("results", []):
        if not isinstance(row, dict) or not row.get("publishedDate"):
            continue
        published = _date(str(row["publishedDate"]))
        if published > cutoff:
            continue
        results.append(_item(
            "exa",
            str(row.get("title") or ""),
            str(row.get("url") or ""),
            published,
            str(row.get("author") or ""),
            _strings(row.get("highlights")),
        ))
    return results


PROVIDERS = {
    "arxiv": search_arxiv,
    "openalex": search_openalex,
    "exa": search_exa,
}


def _request_text(
    url: str,
    *,
    timeout: int,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> str:
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, data=data, headers=headers or {}),
            timeout=timeout,
        ) as resp:
            return resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        raise RestrictedSearchError(f"HTTP {exc.code} from {url}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RestrictedSearchError(f"request failed for {url}: {exc.reason}") from exc


def _item(
    source: str,
    title: str,
    url: str,
    published: date,
    author: str,
    highlights: list[str],
) -> dict[str, Any]:
    return {
        "source": source,
        "title": title,
        "url": url,
        "published_date": published.isoformat(),
        "author": author,
        "highlights": highlights,
        "arxiv_id": _arxiv_id(url),
    }


def _int(cfg: dict[str, Any], key: str, default: int) -> int:
    value = int(cfg.get(key, default))
    if value <= 0:
        raise RestrictedSearchError(f"{key} must be positive")
    return value


def _date(value: str) -> date:
    value = value.strip()
    if len(value) == 10:
        value += "T00:00:00+00:00"
    elif value.endswith("Z"):
        value = value[:-1] + "+00:00"
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).date()


def _node_text(node: ET.Element, path: str, ns: dict[str, str]) -> str:
    found = node.find(path, ns)
    return found.text.strip() if found is not None and found.text else ""


def _clean(value: str) -> str:
    return " ".join(value.split())


def _arxiv_id(url: str) -> str | None:
    match = re.search(
        r"arxiv\.org/(?:abs|pdf)/([0-9]{4}\.[0-9]{4,5}|[a-z\-]+/[0-9]{7})(v[0-9]+)?",
        url,
        re.I,
    )
    return (match.group(1) + (match.group(2) or "")).removesuffix(".pdf") if match else None


def _openalex_url(work: dict[str, Any]) -> str:
    location = work.get("primary_location") or {}
    if isinstance(location, dict):
        return str(
            location.get("pdf_url")
            or location.get("landing_page_url")
            or work.get("doi")
            or work.get("id")
            or ""
        )
    return str(work.get("doi") or work.get("id") or "")


def _openalex_abstract(index: Any) -> str:
    if not isinstance(index, dict):
        return ""
    words = [
        (pos, word)
        for word, positions in index.items()
        if isinstance(positions, list)
        for pos in positions
        if isinstance(pos, int)
    ]
    return " ".join(word for _, word in sorted(words))[:2000]


def _strings(value: Any) -> list[str]:
    return [str(x) for x in value] if isinstance(value, list) else []
