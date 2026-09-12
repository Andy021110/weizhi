"""Import and re-verify evidence projects produced by ai-news-article-workflow."""
import json
import os
import re
from pathlib import Path
from urllib.parse import urldefrag

from . import storage


def _norm(text):
    return re.sub(r"\s+", " ", (text or "").replace("’", "'").replace("“", '"').replace("”", '"')).strip().casefold()


def _numbers(text):
    value = (text or "").replace(",", "")
    months = {
        "january": "1", "february": "2", "march": "3", "april": "4",
        "may": "5", "june": "6", "july": "7", "august": "8",
        "september": "9", "october": "10", "november": "11", "december": "12",
    }
    value = re.sub(
        r"\b(" + "|".join(months) + r")\b",
        lambda match: months[match.group(1).lower()],
        value,
        flags=re.IGNORECASE,
    )
    return {x.lstrip("$").rstrip("%") for x in re.findall(r"(?<![\w.])\$?\d+(?:\.\d+)?%?", value)}


def _url(value):
    return urldefrag((value or "").rstrip("/"))[0]


def _get_or_create_source(doc, project):
    uri = doc.get("final_url") or doc.get("url")
    digest = doc.get("sha256")
    for item in storage.list_sources():
        if _url(item.get("canonical_uri")) == _url(uri) and item.get("snapshot_sha256") == digest:
            return item
    result = storage.create_source({
        "source_type": "pdf" if str(doc.get("raw_path", "")).lower().endswith(".pdf") else "article",
        "canonical_uri": uri,
        "title": doc.get("title") or uri,
        "published_at": doc.get("published_at"),
        "snapshot_path": str((project / doc["clean_path"]).resolve()),
        "snapshot_sha256": digest,
        "status": "ready",
    })
    if result.get("error"):
        raise ValueError(result["error"])
    return result["source"]


def import_evidence_project(project_dir):
    project = Path(project_dir).resolve()
    index_path = project / "source/source-index.json"
    claims_path = project / "claims/verified-claims.json"
    if not index_path.exists() or not claims_path.exists():
        raise ValueError("项目缺少 source-index.json 或 verified-claims.json")
    index = json.loads(index_path.read_text(encoding="utf-8"))
    claim_doc = json.loads(claims_path.read_text(encoding="utf-8"))

    documents = {}
    source_ids = {}
    for doc in index.get("documents") or []:
        clean_path = doc.get("clean_path")
        if doc.get("status") != "ready" or not clean_path:
            continue
        snapshot = project / clean_path
        if not snapshot.exists():
            continue
        source = _get_or_create_source(doc, project)
        text = snapshot.read_text(encoding="utf-8")
        for url in (doc.get("url"), doc.get("final_url")):
            if url:
                documents[_url(url)] = text
                source_ids[_url(url)] = source["id"]

    imported = 0
    failed = 0
    conn = storage._conn()
    try:
        for claim in claim_doc.get("claims") or []:
            source_url = _url(claim.get("source_url"))
            snapshot = documents.get(source_url, "")
            evidence = (claim.get("evidence_text") or "").strip()
            errors = []
            if not snapshot:
                errors.append("source snapshot not found")
            if not evidence or _norm(evidence) not in _norm(snapshot):
                errors.append("evidence is not an exact snapshot excerpt")
            missing = _numbers(claim.get("claim_text")) - _numbers(evidence)
            if missing:
                errors.append("claim numbers absent from evidence: %s" % ",".join(sorted(missing)))
            upstream = (claim.get("verification") or {}).get("status")
            status = "supported" if not errors and upstream == "supported" else "failed"
            if status == "failed":
                failed += 1
            else:
                imported += 1
            source_id = source_ids.get(source_url)
            if not source_id:
                continue
            raw_kind = claim.get("claim_type") or claim.get("claim_kind") or "fact"
            claim_type = {"number": "fact", "prediction": "inference"}.get(raw_kind, raw_kind)
            if claim_type not in {"fact", "publisher_claim", "opinion", "inference"}:
                claim_type = "inference"
            locator = claim.get("locator")
            if not isinstance(locator, str):
                locator = json.dumps(locator, ensure_ascii=False) if locator is not None else None
            conn.execute(
                "INSERT OR REPLACE INTO v2_evidence_claims VALUES (?,?,?,?,?,?,?,?,?)",
                (claim.get("id"), source_id, claim.get("claim_text") or "", evidence, locator,
                 claim_type, status,
                 json.dumps({"method": "v2_exact_snapshot_excerpt+number_check", "errors": errors}, ensure_ascii=False),
                 storage._now()),
            )
        conn.commit()
    finally:
        conn.close()
    return {"sources": len(set(source_ids.values())), "supported_claims": imported, "failed_claims": failed}
