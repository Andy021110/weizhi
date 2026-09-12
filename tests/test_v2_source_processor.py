import json
from pathlib import Path

from weizhi_v2 import source_processor, storage


def _source(status="pending"):
    return storage.create_source({
        "source_type": "article", "canonical_uri": "https://example.com/research",
        "title": "Research article", "status": status,
    })["source"]


def _html():
    sentences = [
        "The evaluation method compares a fixed baseline with the new system under the same task conditions and records every failure category.",
        "The report used 120 representative tasks because aggregate accuracy alone can hide serious errors in smaller user segments.",
        "The authors require every factual answer to link back to an exact source excerpt so reviewers can inspect the original context.",
        "The experiment also records latency and cost, therefore a quality improvement cannot silently exceed the product budget.",
    ]
    return ("<html><body><article><h1>Evaluation</h1><p>" + "</p><p>".join(sentences) + "</p></article></body></html>").encode()


def test_processor_persists_snapshot_and_exact_supported_claims(tmp_db, tmp_path, monkeypatch):
    monkeypatch.setattr(source_processor, "SNAPSHOT_ROOT", tmp_path / "snapshots")
    source = _source()
    result = source_processor.process_source(source["id"], fetcher=lambda url: {
        "body": _html(), "content_type": "text/html; charset=utf-8", "final_url": url,
    })
    assert result["success"] is True
    assert result["text_chars"] >= source_processor.MIN_TEXT_CHARS
    assert result["supported_claims"] >= 2
    saved = storage.get_source(source["id"])
    assert saved["status"] == "ready"
    clean = Path(saved["snapshot_path"])
    assert clean.exists()
    snapshot = clean.read_text(encoding="utf-8")
    conn = storage._conn()
    try:
        claims = conn.execute("SELECT * FROM v2_evidence_claims WHERE source_id=?", (source["id"],)).fetchall()
        run = conn.execute("SELECT * FROM v2_source_processing_runs WHERE source_id=?", (source["id"],)).fetchone()
    finally:
        conn.close()
    assert all(row["verification_status"] == "supported" and row["evidence_text"] in snapshot for row in claims)
    assert run["status"] == "completed"
    assert json.loads(run["detail_json"])["supported_claims"] == len(claims)


def test_processor_marks_short_or_broken_source_failed(tmp_db, tmp_path, monkeypatch):
    monkeypatch.setattr(source_processor, "SNAPSHOT_ROOT", tmp_path / "snapshots")
    source = _source()
    result = source_processor.process_source(source["id"], fetcher=lambda url: {
        "body": b"<p>too short</p>", "content_type": "text/html", "final_url": url,
    })
    assert "正文不足" in result["error"]
    assert storage.get_source(source["id"])["status"] == "failed"


def test_processor_reuses_ready_snapshot(tmp_db, tmp_path, monkeypatch):
    monkeypatch.setattr(source_processor, "SNAPSHOT_ROOT", tmp_path / "snapshots")
    source = _source()
    fetch = lambda url: {"body": _html(), "content_type": "text/html", "final_url": url}
    first = source_processor.process_source(source["id"], fetcher=fetch)
    second = source_processor.process_source(source["id"], fetcher=lambda url: (_ for _ in ()).throw(RuntimeError("must not fetch")))
    assert first["success"] and second["success"]
    assert second["reused"] is True
    assert second["supported_claims"] == first["supported_claims"]


def test_processor_routes_pdf_through_pdf_text_extractor(tmp_db, tmp_path, monkeypatch):
    monkeypatch.setattr(source_processor, "SNAPSHOT_ROOT", tmp_path / "snapshots")
    extracted = " ".join([
        "The paper compares retrieval and generation failures under a fixed evaluation protocol.",
        "The benchmark contains 80 questions with an exact supporting passage for every answer.",
        "The authors report latency separately because answer accuracy does not capture user waiting cost.",
    ])
    monkeypatch.setattr(source_processor, "_extract_pdf", lambda raw_path: extracted)
    source = storage.create_source({
        "source_type": "pdf", "canonical_uri": "https://example.com/paper.pdf",
        "title": "Evaluation paper", "status": "pending",
    })["source"]
    result = source_processor.process_source(source["id"], fetcher=lambda url: {
        "body": b"%PDF-test-fixture", "content_type": "application/pdf", "final_url": url,
    })
    assert result["success"] is True
    assert result["supported_claims"] >= 2
    assert Path(result["source"]["snapshot_path"]).read_text(encoding="utf-8") == extracted


def test_remote_fetch_rejects_loopback_address():
    try:
        source_processor._validate_remote_url("http://127.0.0.1/private")
    except ValueError as exc:
        assert "本机或内网" in str(exc)
    else:
        raise AssertionError("loopback URL should be rejected")


def test_public_hostname_can_use_managed_egress_proxy(monkeypatch):
    monkeypatch.setattr(source_processor.socket, "getaddrinfo", lambda *args: [
        (source_processor.socket.AF_INET, source_processor.socket.SOCK_STREAM, 6, "", ("198.18.0.43", 443))
    ])
    source_processor._validate_remote_url("https://public.example/article")


def test_claim_ranker_excludes_code_and_parameter_table_rows():
    text = "\n".join([
        'model = AutoModelForCausalLM.from_pretrained("demo/model", device_map="auto")',
        "temperature | float | High values increase randomness and low values reduce it. |",
        "The default decoding strategy is greedy search, which selects the next most likely token and may repeat predictable phrases in open-ended generation.",
        "Some chat models expect a structured prompt format, and an incorrect format can produce a suboptimal answer even when the model weights are unchanged.",
    ])
    claims = source_processor._candidate_claims(text)
    assert len(claims) == 2
    assert all("from_pretrained" not in item and " | " not in item for item in claims)


def test_claim_ranker_excludes_extraction_fragments():
    text = "\n".join([
        "is running.transformers chat Qwen/Qwen2.5-0.5B-Instruct",
        "parameters supports custom stopping criteria; - other custom methods can be loaded through the",
        "High values (>0.8) are useful for creative tasks, while low values (e.g.",
        "The default decoding strategy uses greedy search, which selects the next most likely token and can be unsuitable for open-ended creative tasks.",
        "Some chat models require a structured prompt format, and an incorrect format can produce a suboptimal answer even when the weights are unchanged.",
    ])
    claims = source_processor._candidate_claims(text)
    assert len(claims) == 2
    assert all(item[0].isupper() for item in claims)


def test_claim_ranker_excludes_navigation_sentences():
    text = "\n".join([
        "This guide will show you the basics of text generation and common pitfalls to avoid.",
        "Refer to the generation strategies guide to learn more about decoding.",
        "The default decoding strategy uses greedy search and selects the most likely next token.",
        "An incorrect prompt format can produce a suboptimal answer for a chat model.",
    ])
    claims = source_processor._candidate_claims(text)
    assert len(claims) == 2
    assert all(not item.startswith(("This guide", "Refer to")) for item in claims)
