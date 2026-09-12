"""Fetch a queued source, persist an immutable snapshot, and extract exact claims."""
import hashlib
import ipaddress
import json
import re
import socket
import subprocess
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

import trafilatura

from . import storage


MAX_BYTES = 15 * 1024 * 1024
MIN_TEXT_CHARS = 180
EXTRACTOR_VERSION = "sentence-ranker-v4"
HERE = Path(__file__).resolve().parent.parent
SNAPSHOT_ROOT = HERE / "data" / "v2_sources"
EGRESS_PROXY_NET = ipaddress.ip_network("198.18.0.0/15")


def _validate_remote_url(url):
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("只支持公开的 http/https 地址")
    try:
        literal = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        literal = None
    if literal and (literal.is_private or literal.is_loopback or literal.is_link_local or literal.is_reserved or literal.is_unspecified):
        raise ValueError("不能抓取本机或内网地址")
    for item in socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)):
        address = ipaddress.ip_address(item[4][0])
        if not literal and address in EGRESS_PROXY_NET:
            continue
        if address.is_private or address.is_loopback or address.is_link_local or address.is_multicast or address.is_reserved or address.is_unspecified:
            raise ValueError("不能抓取本机或内网地址")


def _download(url):
    _validate_remote_url(url)
    request = urllib.request.Request(url, headers={"User-Agent": "WeiZhiSourceProcessor/2.0"})
    with urllib.request.urlopen(request, timeout=20) as response:
        size = response.headers.get("Content-Length")
        if size and int(size) > MAX_BYTES:
            raise ValueError("来源文件超过 15MB")
        body = response.read(MAX_BYTES + 1)
        if len(body) > MAX_BYTES:
            raise ValueError("来源文件超过 15MB")
        return {
            "body": body,
            "content_type": (response.headers.get("Content-Type") or "").lower(),
            "final_url": response.geturl(),
        }


def _extract_pdf(raw_path):
    output = raw_path.with_suffix(".txt")
    result = subprocess.run(
        ["pdftotext", "-layout", str(raw_path), str(output)],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise ValueError("PDF 正文提取失败")
    return output.read_text(encoding="utf-8", errors="replace")


def _extract_text(body, content_type, source_type, raw_path):
    is_pdf = source_type == "pdf" or "application/pdf" in content_type or body[:4] == b"%PDF"
    if is_pdf:
        return _extract_pdf(raw_path)
    text = trafilatura.extract(body, include_links=False, include_images=False, favor_precision=True)
    if not text:
        text = body.decode("utf-8", errors="replace")
        text = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>", " ", text, flags=re.I)
        text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"[ \t]+", " ", text or "").strip()


def _candidate_claims(text):
    pieces = re.split(r"(?<=[。！？!?])\s*|(?<=[.!?])\s+|\n+", text)
    seen = set()
    ranked = []
    signals = ("because", "therefore", "compared", "result", "method", "allows", "requires",
               "trained", "strategy", "configuration", "throughput", "latency", "pitfall", "incorrect",
               "因为", "因此", "相比", "结果", "方法", "支持", "需要", "通过", "提升", "降低", "训练", "策略")
    for position, raw in enumerate(pieces):
        sentence = " ".join(raw.split()).strip("•-*# ")
        if not 30 <= len(sentence) <= 500:
            continue
        key = sentence.casefold()
        code_markers = ("from_pretrained(", "batch_decode(", "tokenizer(", "model_inputs", "input_ids",
                        "import ", "print(", "device_map=", "quantization_config=", "save_pretrained(")
        symbol_ratio = sum(char in "=()[]{}" for char in sentence) / float(len(sentence))
        word_count = len(re.findall(r"[A-Za-z\u4e00-\u9fff]+", sentence))
        has_cjk = bool(re.search(r"[\u4e00-\u9fff]", sentence))
        terminal = sentence.endswith(("。", "！", "？", ".", "!", "?"))
        balanced = sentence.count("(") == sentence.count(")") and sentence.count("[") == sentence.count("]")
        fragment = (bool(re.match(r"^[a-z]", sentence)) and not re.match(r"^(generate|tokenizer|softmax)\(\)", key))
        fragment = fragment or any(x in key for x in (".transformers ", "; - the ", "; - other ", "e.g."))
        navigation = key.startswith(("this guide ", "this section ", "refer to ", "see this ", "learn more "))
        if (sentence.startswith(('"', "'")) or " | " in sentence or any(x in key for x in code_markers)
                or ("=" in sentence and symbol_ratio > 0.025) or symbol_ratio > 0.08 or word_count < 7
                or (not has_cjk and not terminal) or not balanced or fragment or navigation):
            continue
        if key in seen:
            continue
        seen.add(key)
        score = min(len(sentence), 180) / 180.0
        score += 1.2 if re.search(r"\d", sentence) else 0
        score += sum(0.35 for signal in signals if signal in key)
        ranked.append((score, -position, sentence))
    ranked.sort(reverse=True)
    selected = sorted(ranked[:9], key=lambda item: -item[1])
    return [item[2] for item in selected]


def _record_run(run_id, source_id, status, stage, detail, finished=False):
    conn = storage._conn()
    try:
        if status == "running":
            conn.execute(
                "INSERT INTO v2_source_processing_runs VALUES (?,?,?,?,?,?,?)",
                (run_id, source_id, status, stage, json.dumps(detail, ensure_ascii=False), storage._now(), None),
            )
        else:
            conn.execute(
                "UPDATE v2_source_processing_runs SET status=?,stage=?,detail_json=?,finished_at=? WHERE id=?",
                (status, stage, json.dumps(detail, ensure_ascii=False), storage._now() if finished else None, run_id),
            )
        conn.commit()
    finally:
        conn.close()


def process_source(source_id, fetcher=None, force=False):
    source = storage.get_source(source_id)
    if not source:
        return {"error": "来源不存在"}
    if source["source_type"] in ("repository", "note"):
        return {"error": "当前处理器先支持网页、论文和 PDF"}
    if source["status"] == "ready" and source.get("snapshot_path") and not force:
        count = next((x["supported_claim_count"] for x in storage.list_sources() if x["id"] == source_id), 0)
        return {"success": True, "source": source, "supported_claims": count, "reused": True}

    run_id = storage._id("run")
    _record_run(run_id, source_id, "running", "fetch", {"url": source["canonical_uri"]})
    try:
        payload = (fetcher or _download)(source["canonical_uri"])
        body = payload["body"]
        if isinstance(body, str):
            body = body.encode("utf-8")
        digest = hashlib.sha256(body).hexdigest()
        directory = SNAPSHOT_ROOT / source_id / digest[:16]
        directory.mkdir(parents=True, exist_ok=True)
        suffix = ".pdf" if source["source_type"] == "pdf" or body[:4] == b"%PDF" else ".html"
        raw_path = directory / ("raw" + suffix)
        raw_path.write_bytes(body)
        text = _extract_text(body, payload.get("content_type", ""), source["source_type"], raw_path)
        if len(text) < MIN_TEXT_CHARS:
            raise ValueError("正文不足 180 字，无法形成可靠材料快照")
        clean_path = directory / "clean.txt"
        clean_path.write_text(text, encoding="utf-8")
        claims = _candidate_claims(text)

        conn = storage._conn()
        try:
            conn.execute(
                "UPDATE v2_sources SET canonical_uri=?,snapshot_path=?,snapshot_sha256=?,status='ready' WHERE id=?",
                (payload.get("final_url") or source["canonical_uri"], str(clean_path), digest, source_id),
            )
            if force:
                conn.execute("UPDATE v2_evidence_claims SET verification_status='failed' WHERE source_id=?", (source_id,))
            for position, evidence in enumerate(claims):
                claim_id = "claim_" + hashlib.sha256((source_id + "\n" + evidence).encode()).hexdigest()[:20]
                conn.execute(
                    "INSERT OR REPLACE INTO v2_evidence_claims VALUES (?,?,?,?,?,?,?,?,?)",
                    (claim_id, source_id, evidence, evidence, "sentence:%d" % (position + 1),
                     "publisher_claim", "supported",
                     json.dumps({"method": "exact_snapshot_sentence", "extractor_version": EXTRACTOR_VERSION,
                                 "snapshot_sha256": digest}, ensure_ascii=False),
                     storage._now()),
                )
            conn.commit()
        finally:
            conn.close()
        detail = {"snapshot_sha256": digest, "extractor_version": EXTRACTOR_VERSION,
                  "text_chars": len(text), "supported_claims": len(claims)}
        _record_run(run_id, source_id, "completed", "verify", detail, finished=True)
        return {"success": True, "source": storage.get_source(source_id), **detail}
    except Exception as exc:
        message = str(exc)[:500] or exc.__class__.__name__
        conn = storage._conn()
        try:
            conn.execute("UPDATE v2_sources SET status='failed' WHERE id=?", (source_id,))
            conn.commit()
        finally:
            conn.close()
        _record_run(run_id, source_id, "failed", "failed", {"error": message}, finished=True)
        return {"error": message, "source": storage.get_source(source_id)}
