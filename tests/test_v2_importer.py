import hashlib
import json
from pathlib import Path

from weizhi_v2 import storage
from weizhi_v2.importer import _numbers, import_evidence_project


def make_project(root: Path, changed_number=False):
    (root / "source").mkdir()
    (root / "claims").mkdir()
    text = "The benchmark used 42 tasks and compared several harnesses."
    clean = root / "source/clean.txt"
    clean.write_text(text, encoding="utf-8")
    index = {"documents": [{
        "document_id": "d1", "status": "ready", "url": "https://example.com/a",
        "final_url": "https://example.com/a", "title": "Harness benchmark",
        "clean_path": "source/clean.txt", "raw_path": "source/raw.html",
        "sha256": hashlib.sha256(text.encode()).hexdigest(),
    }]}
    claim = {
        "id": "c1", "source_url": "https://example.com/a", "claim_text": "The benchmark used %s tasks." % (420 if changed_number else 42),
        "evidence_text": "The benchmark used 42 tasks", "claim_kind": "number",
        "locator": {"section": "Results"},
        "verification": {"status": "supported"},
    }
    (root / "source/source-index.json").write_text(json.dumps(index), encoding="utf-8")
    (root / "claims/verified-claims.json").write_text(json.dumps({"claims": [claim]}), encoding="utf-8")


def test_importer_reverifies_snapshot(tmp_db, tmp_path):
    assert _numbers("2026 年 10 月 1 日") <= _numbers("October 1, 2026")
    make_project(tmp_path)
    result = import_evidence_project(tmp_path)
    assert result == {"sources": 1, "supported_claims": 1, "failed_claims": 0}
    conn = storage._conn()
    try:
        row = conn.execute("SELECT claim_type, locator FROM v2_evidence_claims WHERE id='c1'").fetchone()
    finally:
        conn.close()
    assert row["claim_type"] == "fact"
    assert json.loads(row["locator"]) == {"section": "Results"}


def test_importer_rejects_changed_number_even_if_upstream_says_supported(tmp_db, tmp_path):
    make_project(tmp_path, changed_number=True)
    result = import_evidence_project(tmp_path)
    assert result["supported_claims"] == 0
    assert result["failed_claims"] == 1
    conn = storage._conn()
    try:
        status = conn.execute("SELECT verification_status FROM v2_evidence_claims WHERE id='c1'").fetchone()[0]
    finally:
        conn.close()
    assert status == "failed"
