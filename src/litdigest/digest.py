from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import textwrap
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ESEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
EFETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"


class LLMEnrichmentError(Exception):
    """Raised when an LLM enrichment call fails in a user-actionable way."""


@dataclass
class Article:
    pmid: str
    title: str
    journal: str
    pub_date: str
    abstract: str
    article_types: list[str]
    doi: str | None
    linked_comment_pmids: list[str]
    journal_group: str
    score: int
    score_reasons: list[str]
    category: str
    translation_horizon: str
    rule_score: int
    llm_score: int
    llm_enrichment: dict[str, Any] | None


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def build_journal_term(
    journal_config: dict[str, Any],
) -> tuple[str, dict[str, str], dict[str, int], dict[str, str]]:
    tier_weights = journal_config["tier_weights"]
    journal_to_tier: dict[str, str] = {}
    journal_to_weight: dict[str, int] = {}
    journal_to_group: dict[str, str] = {}
    terms: list[str] = []
    for category, entries in journal_config.items():
        if category == "tier_weights":
            continue
        for item in entries:
            name = item["name"]
            tier = item["tier"]
            journal_to_tier[name.lower()] = tier
            journal_to_weight[name.lower()] = tier_weights.get(tier, 0)
            journal_to_group[name.lower()] = category
            terms.append(f'"{name}"[Journal]')
    return "(" + " OR ".join(terms) + ")", journal_to_tier, journal_to_weight, journal_to_group


def build_topic_term(topic_config: dict[str, Any]) -> str:
    keywords = (
        topic_config["near_term_clinical_translation_keywords"]
        + topic_config.get("infectious_disease_priority_keywords", [])
        + topic_config["important_basic_science_keywords"]
    )
    keyword_terms = [f'"{kw}"[Title/Abstract]' for kw in keywords]
    return "(" + " OR ".join(keyword_terms) + ")"


def ncbi_get(url: str, params: dict[str, str]) -> bytes:
    # Use POST to avoid 414 Request-URI Too Long for large PubMed queries.
    encoded = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(
        url=url,
        data=encoded,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=45) as resp:
        return resp.read()


def post_json(url: str, payload: dict[str, Any], headers: dict[str, str], timeout: int = 60) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    attempts = 3
    backoff_seconds = 3.0
    for i in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8")
            return json.loads(body)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            snippet = normalize(raw)[:260]
            retryable = exc.code in {429, 500, 502, 503, 504}
            if retryable and i < attempts - 1 and "exceeded your current quota" not in snippet:
                time.sleep(backoff_seconds * (2**i))
                continue
            raise LLMEnrichmentError(f"http_{exc.code}: {snippet}") from exc
        except urllib.error.URLError as exc:
            if i < attempts - 1:
                time.sleep(backoff_seconds * (2**i))
                continue
            raise LLMEnrichmentError(f"url_error: {exc.reason}") from exc
    raise LLMEnrichmentError("post_json_failed_after_retries")


def load_cache(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def save_cache(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)


def extract_json_blob(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        snippet = normalize(text)[:180]
        raise LLMEnrichmentError(f"no_json_in_response: {snippet}")
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise LLMEnrichmentError("invalid_json_payload") from exc


def parse_gemini_text_response(response: dict[str, Any]) -> str:
    text_parts: list[str] = []
    for cand in response.get("candidates", []):
        finish_reason = str(cand.get("finishReason", "")).upper()
        if finish_reason == "MAX_TOKENS":
            raise LLMEnrichmentError("truncated_response:max_tokens")
        content = cand.get("content", {})
        for part in content.get("parts", []):
            piece = part.get("text")
            if piece:
                text_parts.append(piece)
    if not text_parts:
        prompt_feedback = response.get("promptFeedback")
        if prompt_feedback:
            raise LLMEnrichmentError(f"empty_response_parts:{normalize(json.dumps(prompt_feedback))[:120]}")
        raise LLMEnrichmentError("empty_response_parts")
    return "\n".join(text_parts)


def should_retry_smaller_batch(exc: Exception) -> bool:
    msg = normalize(str(exc))
    retry_signals = [
        "no_json_in_response",
        "invalid_json_payload",
        "truncated_response:max_tokens",
        "batch_response_missing_items_array",
        "batch_response_no_valid_items",
        "missing_pmid_in_batch_response",
    ]
    return any(sig in msg for sig in retry_signals)


def gemini_enrich_batch(
    batch: list[Article],
    gemini_model: str,
    gemini_api_key: str,
    profile: str = "full",
) -> dict[str, dict[str, Any]]:
    item_lines: list[str] = []
    for art in batch:
        item_lines.append(
            textwrap.dedent(
                f"""
                PMID: {art.pmid}
                Title: {art.title[:400]}
                Journal: {art.journal}
                Publication types: {", ".join(art.article_types[:4])}
                Abstract: {art.abstract[:5000]}
                """
            ).strip()
        )
    joined_items = "\n\n---\n\n".join(item_lines)

    if profile == "lite":
        prompt = textwrap.dedent(
            f"""
            You are assisting with an infectious diseases and general medicine weekly journal scan.
            Return ONLY valid JSON in this format:
            {{
              "items": [
                {{
                  "pmid": "string",
                  "one_line_summary": "1 concise sentence focused on clinical relevance",
                  "read_recommendation": "read_now|read_if_time|awareness_only",
                  "clinical_relevance_12m": 0-5 integer,
                  "translation_horizon": "0-12 months|>12 months",
                  "confidence": 0.0-1.0 number
                }}
              ]
            }}

            Rules:
            - Include one output object per PMID listed below.
            - Keep one_line_summary to <= 30 words.
            - Set read_recommendation using: read_now (practice-changing/high-signal), read_if_time (useful detail), awareness_only (low immediate actionability).
            - Do not include any text outside JSON.

            Papers:
            {joined_items}
            """
        ).strip()
    else:
        prompt = textwrap.dedent(
            f"""
            You are assisting with an infectious diseases and general medicine weekly journal scan.
            Return ONLY valid JSON in this format:
            {{
              "items": [
                {{
                  "pmid": "string",
                  "why_it_matters_points": ["exactly 3 concise bullets on context and impact (not actions)"],
                  "headline_result": "1 sentence with key numeric outcome and comparator if available",
                  "trial_n": "sample size text, e.g. n=842 or not reported",
                  "clinical_takeaway": ["exactly 2 or 3 concise action-oriented bullets, including key limitation/caveat"],
                  "read_recommendation": "read_now|read_if_time|awareness_only",
                  "clinical_impact_12m": 0-5 integer,
                  "method_quality": 0-5 integer,
                  "novelty": 0-5 integer,
                  "action": "none|watch|discuss|implement_candidate",
                  "translation_horizon": "0-12 months|>12 months",
                  "confidence": 0.0-1.0 number
                }}
              ]
            }}

            Rules:
            - Include one output object per PMID listed below.
            - Keep outputs clinically oriented and concise, but more informative.
            - headline_result should carry the key numeric effect size/result.
            - why_it_matters_points must have exactly 3 bullets and must focus on context/importance only (disease burden, who should care, decision impact).
            - why_it_matters_points must NOT include management instructions and must NOT repeat numeric effect-size details already in headline_result.
            - If trial n is not stated, set trial_n to "not reported".
            - clinical_takeaway must have 2 or 3 short strings, be action-oriented (what to do or watch), and include at least one key limitation/caveat.
            - Do not duplicate ideas between why_it_matters_points and clinical_takeaway.
            - Set read_recommendation using: read_now (practice-changing/high-signal), read_if_time (useful detail), awareness_only (low immediate actionability).
            - Do not include any text outside JSON.

            Papers:
            {joined_items}
            """
        ).strip()

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/{gemini_model}:generateContent"
        f"?key={urllib.parse.quote(gemini_api_key)}"
    )
    if profile == "lite":
        max_output_tokens = max(1200, 500 * len(batch))
        item_schema = {
            "type": "OBJECT",
            "properties": {
                "pmid": {"type": "STRING"},
                "one_line_summary": {"type": "STRING"},
                "read_recommendation": {"type": "STRING"},
                "clinical_relevance_12m": {"type": "INTEGER"},
                "translation_horizon": {"type": "STRING"},
                "confidence": {"type": "NUMBER"},
            },
            "required": [
                "pmid",
                "one_line_summary",
                "read_recommendation",
                "clinical_relevance_12m",
                "translation_horizon",
                "confidence",
            ],
        }
    else:
        max_output_tokens = max(5000, 2000 * len(batch))
        item_schema = {
            "type": "OBJECT",
            "properties": {
                "pmid": {"type": "STRING"},
                "why_it_matters_points": {
                    "type": "ARRAY",
                    "items": {"type": "STRING"}
                },
                "headline_result": {"type": "STRING"},
                "trial_n": {"type": "STRING"},
                "clinical_takeaway": {
                    "type": "ARRAY",
                    "items": {"type": "STRING"}
                },
                "read_recommendation": {"type": "STRING"},
                "clinical_impact_12m": {"type": "INTEGER"},
                "method_quality": {"type": "INTEGER"},
                "novelty": {"type": "INTEGER"},
                "action": {"type": "STRING"},
                "translation_horizon": {"type": "STRING"},
                "confidence": {"type": "NUMBER"}
            },
            "required": [
                "pmid",
                "why_it_matters_points",
                "headline_result",
                "trial_n",
                "clinical_takeaway",
                "read_recommendation",
                "clinical_impact_12m",
                "method_quality",
                "novelty",
                "action",
                "translation_horizon",
                "confidence"
            ],
        }
    base_payload = {
        "contents": [
            {
                "parts": [
                    {"text": prompt},
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": max_output_tokens,
            "responseMimeType": "application/json",
            "responseSchema": {
                "type": "OBJECT",
                "properties": {
                    "items": {
                        "type": "ARRAY",
                        "items": item_schema
                    }
                },
                "required": ["items"]
            },
        },
    }
    try:
        response = post_json(
            url=url,
            payload=base_payload,
            headers={"Content-Type": "application/json"},
        )
    except LLMEnrichmentError as exc:
        # Some project/model combinations can reject responseSchema; retry without schema once.
        if "http_400" not in normalize(str(exc)):
            raise
        fallback_payload = {
            "contents": base_payload["contents"],
            "generationConfig": {
                "temperature": 0.1,
                "maxOutputTokens": max_output_tokens,
                "responseMimeType": "application/json",
            },
        }
        response = post_json(
            url=url,
            payload=fallback_payload,
            headers={"Content-Type": "application/json"},
        )

    parsed = extract_json_blob(parse_gemini_text_response(response))
    items = parsed.get("items")
    if not isinstance(items, list):
        raise LLMEnrichmentError("batch_response_missing_items_array")

    out: dict[str, dict[str, Any]] = {}
    for row in items:
        if not isinstance(row, dict):
            continue
        pmid = str(row.get("pmid", "")).strip()
        if not pmid:
            continue
        out[pmid] = sanitize_enrichment_row(row=row, profile=profile)
    if not out:
        raise LLMEnrichmentError("batch_response_no_valid_items")
    return out


def llm_priority_points(enrichment: dict[str, Any]) -> int:
    if "clinical_relevance_12m" in enrichment:
        relevance = int(enrichment.get("clinical_relevance_12m", 0))
        return max(0, min(2, relevance // 2))

    impact = int(enrichment.get("clinical_impact_12m", 0))
    quality = int(enrichment.get("method_quality", 0))
    novelty = int(enrichment.get("novelty", 0))
    horizon = str(enrichment.get("translation_horizon", "")).strip()

    # Skew toward novelty + clinical impact, with small method-quality influence.
    # Translation horizon is only a light tie-breaker.
    points = (novelty * 0.55) + (impact * 0.35) + (quality * 0.10)
    if horizon == "0-12 months":
        points += 0.25
    return max(0, min(3, int(round(points))))


def short_error(exc: Exception) -> str:
    msg = normalize(str(exc))
    return msg[:140] if msg else type(exc).__name__


def is_quota_error(exc: Exception) -> bool:
    msg = normalize(str(exc))
    return "http_429" in msg or "exceeded your current quota" in msg


def esearch(term: str, start_date: dt.date, end_date: dt.date, retmax: int) -> list[str]:
    params = {
        "db": "pubmed",
        "term": term,
        "retmax": str(retmax),
        "retmode": "json",
        "sort": "pub date",
        "datetype": "pdat",
        "mindate": start_date.isoformat(),
        "maxdate": end_date.isoformat(),
    }
    api_key = os.getenv("NCBI_API_KEY")
    if api_key:
        params["api_key"] = api_key
    payload = ncbi_get(ESEARCH_URL, params)
    parsed = json.loads(payload.decode("utf-8"))
    return parsed.get("esearchresult", {}).get("idlist", [])


def efetch(pmids: list[str]) -> ET.Element:
    params = {
        "db": "pubmed",
        "id": ",".join(pmids),
        "retmode": "xml",
    }
    api_key = os.getenv("NCBI_API_KEY")
    if api_key:
        params["api_key"] = api_key
    payload = ncbi_get(EFETCH_URL, params)
    return ET.fromstring(payload)


def text_or_empty(node: ET.Element | None) -> str:
    return (node.text or "").strip() if node is not None else ""


def text_with_children(node: ET.Element | None) -> str:
    if node is None:
        return ""
    return "".join(node.itertext()).strip()


def first(iterable: list[str]) -> str:
    return iterable[0] if iterable else ""


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def sanitize_list_field(value: Any, max_items: int, forbidden_tokens: set[str]) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        txt = str(item).strip()
        if not txt:
            continue
        txt = re.sub(r"^\s*[-*•]+\s*", "", txt).strip()
        norm = normalize(txt).strip(" :;,.")
        if not norm or norm in forbidden_tokens or norm in seen:
            continue
        seen.add(norm)
        out.append(txt)
        if len(out) >= max_items:
            break
    return out


def sanitize_enrichment_row(row: dict[str, Any], profile: str) -> dict[str, Any]:
    cleaned = dict(row)
    forbidden_tokens = {
        "pmid",
        "why_it_matters_points",
        "headline_result",
        "trial_n",
        "clinical_takeaway",
        "read_recommendation",
        "read_now",
        "read_if_time",
        "awareness_only",
        "clinical_impact_12m",
        "method_quality",
        "novelty",
        "action",
        "translation_horizon",
        "confidence",
    }
    if profile == "full":
        cleaned["why_it_matters_points"] = sanitize_list_field(
            value=cleaned.get("why_it_matters_points"),
            max_items=3,
            forbidden_tokens=forbidden_tokens,
        )
        cleaned["clinical_takeaway"] = sanitize_list_field(
            value=cleaned.get("clinical_takeaway"),
            max_items=3,
            forbidden_tokens=forbidden_tokens,
        )
    return cleaned


def format_read_recommendation(value: str) -> str:
    token = normalize(value)
    mapping = {
        "read_now": "Read now",
        "read_if_time": "Read if time",
        "read if time": "Read if time",
        "awareness_only": "Awareness only",
        "awareness only": "Awareness only",
    }
    return mapping.get(token, value.strip())


def collapse_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def escape_markdown_inline(text: str) -> str:
    # Prevent title text from accidentally triggering markdown formatting.
    escaped = text.replace("\\", "\\\\")
    for ch in ["*", "_", "`", "[", "]", "<", ">"]:
        escaped = escaped.replace(ch, f"\\{ch}")
    return escaped


def collect_abstract(article: ET.Element) -> str:
    chunks: list[str] = []
    for node in article.findall(".//AbstractText"):
        label = (node.attrib.get("Label") or "").strip()
        body = text_with_children(node)
        if body:
            chunks.append(f"{label}: {body}" if label else body)
    return re.sub(r"\s+", " ", " ".join(chunks)).strip()


def collect_linked_comment_pmids(article: ET.Element) -> list[str]:
    pmids: list[str] = []
    for cc in article.findall(".//CommentsCorrections"):
        ref_type = (cc.attrib.get("RefType") or "").strip()
        if ref_type not in {"CommentIn", "CommentOn"}:
            continue
        cpmid = text_or_empty(cc.find(".//PMID"))
        if cpmid:
            pmids.append(cpmid)
    seen: set[str] = set()
    out: list[str] = []
    for p in pmids:
        if p in seen:
            continue
        seen.add(p)
        out.append(p)
    return out


def pick_category(article_text: str, topic_config: dict[str, Any]) -> str:
    near = topic_config["near_term_clinical_translation_keywords"]
    basic = topic_config["important_basic_science_keywords"]
    near_hits = sum(1 for kw in near if kw in article_text)
    basic_hits = sum(1 for kw in basic if kw in article_text)
    if near_hits >= basic_hits:
        return "Clinical/Translational (0-12 months likely)"
    return "Basic science to watch (>12 months likely)"


def score_article(
    journal: str,
    journal_group: str,
    article_types: list[str],
    title: str,
    abstract: str,
    topic_config: dict[str, Any],
    journal_weights: dict[str, int],
) -> tuple[int, list[str], str]:
    score = 0
    reasons: list[str] = []
    j_weight = journal_weights.get(journal.lower(), 0)
    score += j_weight
    if j_weight > 0:
        reasons.append(f"journal_weight={j_weight}")

    group_bonus = {
        "infectious_diseases_microbiology_ipc": 3,
        "general_medicine_acute_care": 1,
        "basic_translational_infection_relevant": 0,
    }.get(journal_group, 0)
    if group_bonus > 0:
        score += group_bonus
        reasons.append(f"group_bias={group_bonus}")

    article_type_weights = topic_config["article_type_weights"]
    lower_types = [a.lower() for a in article_types]
    review_like_pattern = r"\b(review|systematic review|meta-analysis|narrative review|scoping review)\b"
    review_in_article_types = any(re.search(review_like_pattern, atype) for atype in lower_types)
    review_in_text = re.search(review_like_pattern, normalize(f"{title} {abstract}")) is not None
    is_review_like = review_in_article_types or review_in_text
    matched_type_weights: list[tuple[str, int]] = []
    for key, weight in article_type_weights.items():
        if any(key in atype for atype in lower_types):
            matched_type_weights.append((key, int(weight)))
    if matched_type_weights:
        best_key, best_weight = max(matched_type_weights, key=lambda item: item[1])
        score += best_weight
        reasons.append(f"article_type:{best_key}=+{best_weight}")
    else:
        # Strict fallback: infer trial design only with explicit high-quality trial signals.
        trial_text = normalize(f"{title} {abstract}")
        inferred_type_weights: list[tuple[str, int]] = []
        observational_pattern = (
            r"\b(case series|retrospective|cohort|propensity|matched|case-control|"
            r"observational|registry|database study|cross-sectional|before-and-after)\b"
        )
        if (re.search(observational_pattern, trial_text) is None) and (not is_review_like):
            phase_iii_signal = re.search(r"\bphase\s*(iii|3)\b", trial_text) is not None
            phase_ii_signal = re.search(r"\bphase\s*(ii|2)\b", trial_text) is not None
            randomization_signal = re.search(
                r"\b(randomized|randomised|randomly assigned|random assignment|"
                r"assigned in a \d+:\d+ ratio|rct)\b",
                trial_text,
            ) is not None
            trial_design_signal = re.search(
                r"\b(controlled|double-blind|single-blind|placebo-controlled|open-label|"
                r"noninferiority|superiority)\b",
                trial_text,
            ) is not None

            if phase_iii_signal:
                inferred_type_weights.append(
                    ("clinical trial, phase iii", int(article_type_weights["clinical trial, phase iii"]))
                )
            if phase_ii_signal:
                inferred_type_weights.append(
                    ("clinical trial, phase ii", int(article_type_weights["clinical trial, phase ii"]))
                )
            if randomization_signal and trial_design_signal:
                inferred_type_weights.append(
                    ("randomized controlled trial", int(article_type_weights["randomized controlled trial"]))
                )
        if inferred_type_weights:
            best_key, best_weight = max(inferred_type_weights, key=lambda item: item[1])
            score += best_weight
            reasons.append(f"article_type_heuristic:{best_key}=+{best_weight}")

    haystack = normalize(f"{title} {abstract}")
    near_hits = [
        kw for kw in topic_config["near_term_clinical_translation_keywords"] if kw in haystack
    ]
    id_hits = [
        kw for kw in topic_config.get("infectious_disease_priority_keywords", []) if kw in haystack
    ]
    basic_hits = [kw for kw in topic_config["important_basic_science_keywords"] if kw in haystack]
    if near_hits:
        add = min(4, len(near_hits))
        score += add
        reasons.append(f"near_term_keywords=+{add}")
    if id_hits:
        add = min(6, len(id_hits))
        score += add
        reasons.append(f"id_keywords=+{add}")
    if basic_hits:
        add = min(2, len(basic_hits))
        score += add
        reasons.append(f"basic_science_keywords=+{add}")

    oncology_hits = [
        kw for kw in topic_config.get("oncology_downweight_keywords", []) if kw in haystack
    ]
    if oncology_hits:
        base_penalty = int(topic_config.get("oncology_downweight", 2))
        # Keep infection-focused oncology content from being over-penalized.
        penalty = base_penalty if not id_hits else max(1, base_penalty - 1)
        score -= penalty
        reasons.append(f"oncology_downweight=-{penalty}")

    if is_review_like:
        penalty = int(topic_config.get("review_downweight", 3))
        score -= penalty
        reasons.append(f"review_downweight=-{penalty}")

    category = pick_category(haystack, topic_config)
    return score, reasons, category


def parse_articles(
    root: ET.Element,
    topic_config: dict[str, Any],
    journal_weights: dict[str, int],
    journal_groups: dict[str, str],
) -> list[Article]:
    rows: list[Article] = []
    for article in root.findall(".//PubmedArticle"):
        pmid = text_or_empty(article.find(".//PMID"))
        title = collapse_whitespace(text_with_children(article.find(".//ArticleTitle")))
        journal = text_or_empty(article.find(".//Journal/ISOAbbreviation"))
        if not journal:
            journal = text_or_empty(article.find(".//Journal/Title"))
        journal_group = journal_groups.get(journal.lower(), "unknown")

        year = text_or_empty(article.find(".//PubDate/Year"))
        medline = text_or_empty(article.find(".//PubDate/MedlineDate"))
        pub_date = year or medline or "Unknown"

        abstract = collect_abstract(article)

        article_types = [text_or_empty(t) for t in article.findall(".//PublicationType")]
        article_types = [t for t in article_types if t]
        linked_comment_pmids = collect_linked_comment_pmids(article)

        doi = None
        for aid in article.findall(".//ArticleId"):
            if aid.attrib.get("IdType") == "doi" and (aid.text or "").strip():
                doi = aid.text.strip()
                break

        score, reasons, category = score_article(
            journal=journal,
            journal_group=journal_group,
            article_types=article_types,
            title=title,
            abstract=abstract,
            topic_config=topic_config,
            journal_weights=journal_weights,
        )

        translation_horizon = (
            "0-12 months" if "Clinical/Translational" in category else ">12 months"
        )
        rows.append(
            Article(
                pmid=pmid,
                title=title,
                journal=journal,
                pub_date=pub_date,
                abstract=abstract,
                article_types=article_types,
                doi=doi,
                linked_comment_pmids=linked_comment_pmids,
                journal_group=journal_group,
                score=score,
                score_reasons=reasons,
                category=category,
                translation_horizon=translation_horizon,
                rule_score=score,
                llm_score=0,
                llm_enrichment=None,
            )
        )
    rows.sort(key=lambda x: x.score, reverse=True)
    return rows


def apply_llm_enrichment(
    articles: list[Article],
    enabled: bool,
    llm_top_n: int,
    llm_core_top_n: int,
    llm_lite_top_n: int,
    llm_cache_path: Path,
    gemini_model: str,
    llm_batch_size: int,
    llm_lite_batch_size: int,
    llm_batch_delay_seconds: float,
    llm_max_requests: int,
) -> tuple[list[Article], int, dict[str, Any]]:
    if not enabled:
        return articles, 0, {
            "target_count": 0,
            "enriched_count": 0,
            "failed_count": 0,
            "requests_used": 0,
            "max_requests_reached": False,
            "quota_exhausted": False,
            "success_rate": 1.0,
            "error_counts": {},
        }

    gemini_api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not gemini_api_key:
        raise RuntimeError("`--llm-enrich` was set but GEMINI_API_KEY is missing.")

    cache = load_cache(llm_cache_path)
    enriched_pmids: set[str] = set()
    quota_exhausted = False
    max_requests_reached = False
    request_count = 0

    effective_core_n = llm_core_top_n if llm_core_top_n > 0 else llm_top_n
    core_target = select_core_digest(articles=articles, core_size=effective_core_n)
    core_target_pmids = {art.pmid for art in core_target}
    lite_target: list[Article] = []
    if llm_lite_top_n > 0:
        lite_candidates = [art for art in articles if art.pmid not in core_target_pmids]
        lite_target = lite_candidates[:llm_lite_top_n]

    phase_defs: list[tuple[str, list[Article], int]] = [
        ("full", core_target, max(1, llm_batch_size)),
        ("lite", lite_target, max(1, llm_lite_batch_size)),
    ]
    target: list[Article] = core_target + lite_target
    enrichment_by_pmid: dict[str, dict[str, Any]] = {}

    def run_phase(profile: str, phase_target: list[Article], batch_size: int) -> None:
        nonlocal request_count, quota_exhausted, max_requests_reached
        if not phase_target:
            return
        unresolved: list[Article] = []
        for art in phase_target:
            cached = cache.get(art.pmid)
            if isinstance(cached, dict) and cached.get("model") == gemini_model:
                cached_enrichment = cached.get("enrichment")
                cached_profile = str(cached.get("profile", "full")).strip() or "full"
                if isinstance(cached_enrichment, dict) and cached_profile == profile:
                    enrichment_by_pmid[art.pmid] = cached_enrichment
                    continue
            unresolved.append(art)

        queue: list[list[Article]] = [
            unresolved[i : i + batch_size] for i in range(0, len(unresolved), batch_size)
        ]
        while queue and not quota_exhausted:
            if llm_max_requests > 0 and request_count >= llm_max_requests:
                max_requests_reached = True
                break
            batch = queue.pop(0)
            if not batch:
                continue
            if request_count > 0 and llm_batch_delay_seconds > 0:
                time.sleep(llm_batch_delay_seconds)
            request_count += 1
            try:
                batch_enrichment = gemini_enrich_batch(
                    batch=batch,
                    gemini_model=gemini_model,
                    gemini_api_key=gemini_api_key,
                    profile=profile,
                )
                missing_pmids = [art for art in batch if art.pmid not in batch_enrichment]
                if missing_pmids:
                    raise LLMEnrichmentError("missing_pmid_in_batch_response")
            except (LLMEnrichmentError, ValueError, KeyError, json.JSONDecodeError) as exc:
                if is_quota_error(exc):
                    err = short_error(exc)
                    for art in batch:
                        art.score_reasons.append(f"llm_error:{err}")
                    quota_exhausted = True
                    continue
                if len(batch) > 1 and should_retry_smaller_batch(exc):
                    mid = len(batch) // 2
                    queue.insert(0, batch[mid:])
                    queue.insert(0, batch[:mid])
                    continue
                err = short_error(exc)
                for art in batch:
                    art.score_reasons.append(f"llm_error:{err}")
                continue

            for art in batch:
                enrichment = batch_enrichment[art.pmid]
                enrichment_by_pmid[art.pmid] = enrichment
                cache[art.pmid] = {
                    "model": gemini_model,
                    "profile": profile,
                    "enrichment": enrichment,
                    "updated_at": dt.datetime.utcnow().isoformat(timespec="seconds") + "Z",
                }
                enriched_pmids.add(art.pmid)

    for profile, phase_target, phase_batch_size in phase_defs:
        if quota_exhausted or max_requests_reached:
            break
        run_phase(profile=profile, phase_target=phase_target, batch_size=phase_batch_size)

    for art in target:
        enrichment = enrichment_by_pmid.get(art.pmid)
        if enrichment is None:
            if not any("llm_error:" in r for r in art.score_reasons):
                if quota_exhausted:
                    art.score_reasons.append("llm_skipped:quota_exhausted")
                elif max_requests_reached:
                    art.score_reasons.append("llm_skipped:max_requests_reached")
            continue
        llm_pts = llm_priority_points(enrichment)
        art.llm_score = llm_pts
        art.llm_enrichment = enrichment
        art.score = art.rule_score + llm_pts
        art.score_reasons.append(f"llm_priority=+{llm_pts}")
        horizon = str(enrichment.get("translation_horizon", "")).strip()
        if horizon in {"0-12 months", ">12 months"}:
            art.translation_horizon = horizon

    articles.sort(key=lambda x: x.score, reverse=True)

    # Ensure final core papers have full enrichment payload where possible.
    final_core = select_core_digest(articles=articles, core_size=min(15, len(articles)))
    for art in final_core:
        if has_full_enrichment_payload(art.llm_enrichment):
            continue
        cached = cache.get(art.pmid)
        if (
            isinstance(cached, dict)
            and cached.get("model") == gemini_model
            and str(cached.get("profile", "full")).strip() == "full"
            and isinstance(cached.get("enrichment"), dict)
        ):
            enrichment = cached["enrichment"]
            enrichment_by_pmid[art.pmid] = enrichment
            art.llm_enrichment = enrichment
            llm_pts = llm_priority_points(enrichment)
            art.llm_score = llm_pts
            art.score = art.rule_score + llm_pts
            if not any(r.startswith("llm_priority=+") for r in art.score_reasons):
                art.score_reasons.append(f"llm_priority=+{llm_pts}")
            enriched_pmids.add(art.pmid)
            continue

        if quota_exhausted or max_requests_reached:
            break
        if llm_max_requests > 0 and request_count >= llm_max_requests:
            max_requests_reached = True
            break
        if request_count > 0 and llm_batch_delay_seconds > 0:
            time.sleep(llm_batch_delay_seconds)
        request_count += 1
        try:
            full_enrichment = gemini_enrich_batch(
                batch=[art],
                gemini_model=gemini_model,
                gemini_api_key=gemini_api_key,
                profile="full",
            )
            enrichment = full_enrichment.get(art.pmid)
            if not isinstance(enrichment, dict):
                raise LLMEnrichmentError("missing_pmid_in_batch_response")
            enrichment_by_pmid[art.pmid] = enrichment
            art.llm_enrichment = enrichment
            llm_pts = llm_priority_points(enrichment)
            art.llm_score = llm_pts
            art.score = art.rule_score + llm_pts
            if not any(r.startswith("llm_priority=+") for r in art.score_reasons):
                art.score_reasons.append(f"llm_priority=+{llm_pts}")
            cache[art.pmid] = {
                "model": gemini_model,
                "profile": "full",
                "enrichment": enrichment,
                "updated_at": dt.datetime.utcnow().isoformat(timespec="seconds") + "Z",
            }
            enriched_pmids.add(art.pmid)
        except (LLMEnrichmentError, ValueError, KeyError, json.JSONDecodeError) as exc:
            err = short_error(exc)
            art.score_reasons.append(f"llm_error:{err}")
            if is_quota_error(exc):
                quota_exhausted = True
                break

    articles.sort(key=lambda x: x.score, reverse=True)
    save_cache(llm_cache_path, cache)
    targeted = len(target)
    success_rate = (len(enrichment_by_pmid) / targeted) if targeted else 1.0
    error_counts: dict[str, int] = {}
    for art in target:
        for reason in art.score_reasons:
            if reason.startswith("llm_error:") or reason.startswith("llm_skipped:"):
                error_counts[reason] = error_counts.get(reason, 0) + 1

    stats = {
        "target_count": targeted,
        "enriched_count": len(enrichment_by_pmid),
        "failed_count": max(0, targeted - len(enrichment_by_pmid)),
        "requests_used": request_count,
        "max_requests_reached": max_requests_reached,
        "quota_exhausted": quota_exhausted,
        "success_rate": round(success_rate, 3),
        "error_counts": error_counts,
    }
    return articles, len(enriched_pmids), stats


def format_llm_diagnostic(llm_stats: dict[str, Any]) -> str:
    if llm_stats.get("target_count", 0) <= 0 or llm_stats.get("enriched_count", 0) > 0:
        return ""

    errors = llm_stats.get("error_counts", {}) or {}
    top_errors = sorted(errors.items(), key=lambda x: x[1], reverse=True)[:3]
    lines = [
        "LLM diagnostic:",
        "  No items were enriched for this run.",
    ]
    if top_errors:
        lines.append("  Top LLM failure reasons:")
        for key, count in top_errors:
            lines.append(f"    - {key}: {count}")

    if any("truncated_response:max_tokens" in k for k in errors):
        lines.extend(
            [
                "  Likely cause: output was truncated by max tokens.",
                "  Next actions:",
                "    - Use smaller batches: --llm-batch-size 1 or 2",
                "    - Keep delay for RPM: --llm-batch-delay-seconds 15",
                "    - If still failing, increase maxOutputTokens in src/litdigest/digest.py",
            ]
        )
    elif llm_stats.get("quota_exhausted"):
        lines.extend(
            [
                "  Likely cause: quota/rate limit exhausted.",
                "  Next actions:",
                "    - Reduce requests: lower --llm-top-n",
                "    - Cap requests: --llm-max-requests 18",
                "    - Keep delay for RPM: --llm-batch-delay-seconds 15",
            ]
        )
    elif llm_stats.get("max_requests_reached"):
        lines.extend(
            [
                "  Likely cause: run reached --llm-max-requests before completion.",
                "  Next action: raise --llm-max-requests or lower --llm-top-n.",
            ]
        )

    return "\n".join(lines)


def estimate_llm_requests(
    articles: list[Article],
    llm_top_n: int,
    llm_core_top_n: int,
    llm_lite_top_n: int,
    llm_cache_path: Path,
    gemini_model: str,
    llm_batch_size: int,
    llm_lite_batch_size: int,
    llm_output_tokens_per_paper_estimate: int,
) -> dict[str, Any]:
    cache = load_cache(llm_cache_path)
    core_n = llm_core_top_n if llm_core_top_n > 0 else llm_top_n
    core_target = select_core_digest(articles=articles, core_size=core_n)
    core_target_pmids = {art.pmid for art in core_target}
    lite_target: list[Article] = []
    if llm_lite_top_n > 0:
        lite_candidates = [art for art in articles if art.pmid not in core_target_pmids]
        lite_target = lite_candidates[:llm_lite_top_n]
    target = core_target + lite_target

    core_batch_size = max(1, llm_batch_size)
    lite_batch_size = max(1, llm_lite_batch_size)

    def is_cached_for_profile(art: Article, profile: str) -> bool:
        cached = cache.get(art.pmid)
        if not (isinstance(cached, dict) and cached.get("model") == gemini_model):
            return False
        if not isinstance(cached.get("enrichment"), dict):
            return False
        cached_profile = str(cached.get("profile", "full")).strip() or "full"
        return cached_profile == profile

    core_cached = sum(1 for art in core_target if is_cached_for_profile(art, "full"))
    lite_cached = sum(1 for art in lite_target if is_cached_for_profile(art, "lite"))
    core_unresolved_articles = [art for art in core_target if not is_cached_for_profile(art, "full")]
    lite_unresolved_articles = [art for art in lite_target if not is_cached_for_profile(art, "lite")]
    core_unresolved = len(core_unresolved_articles)
    lite_unresolved = len(lite_unresolved_articles)
    unresolved_articles = core_unresolved_articles + lite_unresolved_articles

    core_requests = (core_unresolved + core_batch_size - 1) // core_batch_size
    lite_requests = (lite_unresolved + lite_batch_size - 1) // lite_batch_size
    estimated_requests = core_requests + lite_requests

    # Rough token estimator: ~4 characters per token.
    # Includes prompt scaffolding overhead and per-paper metadata/abstract slices used by gemini_enrich_batch.
    def approx_tokens(chars: int) -> int:
        return max(1, (chars + 3) // 4)

    prompt_overhead_chars = 1800
    per_paper_fixed_chars = 300  # PMID/title labels/journal/type wrappers and separators
    input_tokens_unresolved = 0
    for art in unresolved_articles:
        title_chars = min(400, len(art.title or ""))
        abstract_chars = min(1600, len(art.abstract or ""))
        input_tokens_unresolved += approx_tokens(per_paper_fixed_chars + title_chars + abstract_chars)
    input_tokens_unresolved += approx_tokens(prompt_overhead_chars)

    est_output_full_per_batch = max(5000, int(llm_output_tokens_per_paper_estimate * core_batch_size * 1.25))
    est_output_lite_per_batch = max(1200, int((llm_output_tokens_per_paper_estimate * 0.4) * lite_batch_size * 1.25))
    est_output_total = (est_output_full_per_batch * core_requests) + (est_output_lite_per_batch * lite_requests)

    return {
        "target_count": len(target),
        "cached_count": core_cached + lite_cached,
        "unresolved_count": core_unresolved + lite_unresolved,
        "core_target_count": len(core_target),
        "core_unresolved_count": core_unresolved,
        "lite_target_count": len(lite_target),
        "lite_unresolved_count": lite_unresolved,
        "estimated_requests": estimated_requests,
        "estimated_requests_core": core_requests,
        "estimated_requests_lite": lite_requests,
        "batch_size": core_batch_size,
        "batch_size_core": core_batch_size,
        "batch_size_lite": lite_batch_size,
        "estimated_input_tokens_unresolved": input_tokens_unresolved,
        "estimated_output_tokens_total": est_output_total,
        "estimated_output_tokens_per_batch": est_output_full_per_batch,
        "estimated_output_tokens_per_batch_core": est_output_full_per_batch,
        "estimated_output_tokens_per_batch_lite": est_output_lite_per_batch,
        "output_tokens_per_paper_estimate": llm_output_tokens_per_paper_estimate,
    }


def pubmed_link(pmid: str) -> str:
    return f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"


def doi_link(doi: str | None) -> str:
    return f"https://doi.org/{doi}" if doi else ""


def summarize_reading_time(total_items: int) -> str:
    if total_items <= 15:
        return "~15 minutes"
    if total_items <= 40:
        return "~30-60 minutes"
    return "~60+ minutes"


def has_full_enrichment_payload(enrichment: dict[str, Any] | None) -> bool:
    if not isinstance(enrichment, dict):
        return False
    why = enrichment.get("why_it_matters_points")
    takeaways = enrichment.get("clinical_takeaway")
    headline = str(enrichment.get("headline_result", "")).strip()
    if isinstance(why, list) and any(str(x).strip() for x in why):
        return True
    if isinstance(takeaways, list) and any(str(x).strip() for x in takeaways):
        return True
    return bool(headline)


def select_core_digest(articles: list[Article], core_size: int = 15) -> list[Article]:
    if core_size <= 0:
        return []
    return articles[:core_size]


def parse_pubmed_records(root: ET.Element) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for article in root.findall(".//PubmedArticle"):
        pmid = text_or_empty(article.find(".//PMID"))
        if not pmid:
            continue
        title = collapse_whitespace(text_with_children(article.find(".//ArticleTitle")))
        journal = text_or_empty(article.find(".//Journal/ISOAbbreviation")) or text_or_empty(
            article.find(".//Journal/Title")
        )
        abstract = collect_abstract(article)
        article_types = [text_or_empty(t) for t in article.findall(".//PublicationType")]
        article_types = [t for t in article_types if t]
        out[pmid] = {
            "pmid": pmid,
            "title": title,
            "journal": journal,
            "abstract": abstract,
            "article_types": article_types,
            "pubmed_url": pubmed_link(pmid),
        }
    return out


def write_podcast_source(
    articles: list[Article],
    output_dir: Path,
    as_of: dt.date,
    core_size: int = 15,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{as_of.isoformat()}_core_podcast_source.md"
    core = select_core_digest(articles=articles, core_size=core_size)

    linked_pmids: list[str] = []
    for art in core:
        linked_pmids.extend(art.linked_comment_pmids)
    linked_pmids = list(dict.fromkeys(linked_pmids))
    editorial_index: dict[str, dict[str, Any]] = {}
    if linked_pmids:
        try:
            linked_root = efetch(linked_pmids)
            editorial_index = parse_pubmed_records(linked_root)
        except Exception:
            editorial_index = {}

    with path.open("w", encoding="utf-8") as handle:
        handle.write(f"# Core Digest Podcast Source ({as_of.isoformat()})\n\n")
        handle.write(
            "This document collates the top core papers for AI podcast/script generation, "
            "including full PubMed abstracts and linked commentary/editorial abstracts where available.\n\n"
        )
        for i, art in enumerate(core, start=1):
            title_display = escape_markdown_inline(art.title)
            handle.write(f"## {i}. {title_display}\n\n")
            handle.write(f"- Journal: {art.journal}\n")
            handle.write(f"- Date: {art.pub_date}\n")
            handle.write(f"- Score: {art.score}\n")
            handle.write(f"- PubMed: {pubmed_link(art.pmid)}\n")
            if art.doi:
                handle.write(f"- DOI: {doi_link(art.doi)}\n")
            handle.write("\n### Abstract\n\n")
            handle.write((art.abstract or "No abstract available.") + "\n\n")

            if art.llm_enrichment:
                handle.write("### LLM Summary\n\n")
                trial_n = str(art.llm_enrichment.get("trial_n", "")).strip()
                if trial_n:
                    handle.write(f"- **Trial n:** {escape_markdown_inline(trial_n)}\n")

                why_points = art.llm_enrichment.get("why_it_matters_points")
                if isinstance(why_points, list) and why_points:
                    handle.write("\n- **Why it matters:**\n")
                    for point in why_points:
                        txt = str(point).strip()
                        if txt:
                            handle.write(f"  - {escape_markdown_inline(txt)}\n")
                else:
                    one_line = str(art.llm_enrichment.get("one_line_summary", "")).strip()
                    if one_line:
                        handle.write(f"\n- **Why it matters:** {escape_markdown_inline(one_line)}\n")

                headline_result = str(art.llm_enrichment.get("headline_result", "")).strip()
                if headline_result:
                    handle.write(f"- **Headline result:** {escape_markdown_inline(headline_result)}\n")

                takeaways = art.llm_enrichment.get("clinical_takeaway")
                if isinstance(takeaways, list) and takeaways:
                    handle.write("\n- **Clinical takeaway:**\n")
                    for takeaway in takeaways:
                        txt = str(takeaway).strip()
                        if txt:
                            handle.write(f"  - {escape_markdown_inline(txt)}\n")
                read_rec = format_read_recommendation(str(art.llm_enrichment.get("read_recommendation", "")))
                if read_rec:
                    handle.write(f"\n- **Read priority:** {escape_markdown_inline(read_rec)}\n")
                handle.write("\n")

            linked_records = [editorial_index[p] for p in art.linked_comment_pmids if p in editorial_index]
            if linked_records:
                handle.write("### Linked Editorial/Commentary (PubMed)\n\n")
                for rec in linked_records:
                    types = ", ".join(rec.get("article_types", [])[:4])
                    linked_title = escape_markdown_inline(str(rec.get("title", "Untitled")))
                    handle.write(f"- **{linked_title}**\n")
                    handle.write(f"  - Journal: {rec.get('journal', 'Unknown')}\n")
                    if types:
                        handle.write(f"  - Type: {types}\n")
                    handle.write(f"  - PubMed: {rec.get('pubmed_url')}\n")
                    if rec.get("abstract"):
                        handle.write(f"  - Abstract: {rec['abstract']}\n")
                handle.write("\n")

    return path


def write_outputs(
    articles: list[Article],
    output_dir: Path,
    as_of: dt.date,
    days: int,
    llm_stats: dict[str, Any] | None = None,
) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    md_path = output_dir / f"{as_of.isoformat()}_digest.md"
    json_path = output_dir / f"{as_of.isoformat()}_digest.json"

    top = articles[:40]
    core = select_core_digest(articles=articles, core_size=15)
    core_pmids = {art.pmid for art in core}
    extended = [art for art in top if art.pmid not in core_pmids]
    with md_path.open("w", encoding="utf-8") as handle:
        handle.write(f"# Weekly ID + General Medicine Literature Digest ({as_of.isoformat()})\n\n")
        handle.write(f"- Window: last {days} days\n")
        handle.write(f"- Core digest items: {len(core)}\n")
        handle.write(f"- Extended digest items: {len(extended)}\n")
        handle.write(f"- Estimated reading time: {summarize_reading_time(len(top))}\n\n")
        if llm_stats:
            target = int(llm_stats.get("target_count", 0))
            enriched = int(llm_stats.get("enriched_count", 0))
            pct = (enriched / target * 100.0) if target else 0.0
            handle.write(
                f"- LLM summary success: {pct:.1f}% ({enriched}/{target})\n\n"
            )

        handle.write("## Core Digest (10-15 mins)\n\n")
        for i, art in enumerate(core, start=1):
            title_display = escape_markdown_inline(art.title)
            handle.write(f"{i}. **{title_display}**\n")
            handle.write("\n")
            handle.write(
                f"    Journal: {art.journal} | Date: {art.pub_date} | Score: {art.score} (rule {art.rule_score}"
                f"{', llm +' + str(art.llm_score) if art.llm_score else ''}) | Translation horizon: {art.translation_horizon}\n"
            )
            handle.write("\n")
            p_link = pubmed_link(art.pmid)
            handle.write(f"    PubMed: [{p_link}]({p_link})\n")
            handle.write("\n")
            if art.doi:
                d_link = doi_link(art.doi)
                handle.write(f"    DOI: [{d_link}]({d_link})\n")
            if art.article_types:
                handle.write(f"    Type: {', '.join(art.article_types[:3])}\n")
            handle.write("\n")    
            read_rec = ""
            if art.llm_enrichment:
                trial_n = str(art.llm_enrichment.get("trial_n", "")).strip()
                if trial_n:
                    handle.write(f"    **Trial n:** {escape_markdown_inline(trial_n)}\n")

                why_points = art.llm_enrichment.get("why_it_matters_points")
                if isinstance(why_points, list) and why_points:
                    handle.write("\n    **Why it matters:**\n")
                    for point in why_points:
                        txt = str(point).strip()
                        if txt:
                            handle.write(f"    - {escape_markdown_inline(txt)}\n")
                elif art.llm_enrichment.get("why_this_matters"):
                    # Backward compatibility with older cache entries.
                    fallback = escape_markdown_inline(str(art.llm_enrichment["why_this_matters"]))
                    handle.write(f"\n    **Why it matters:** {fallback}\n")
                else:
                    lite_summary = str(art.llm_enrichment.get("one_line_summary", "")).strip()
                    if lite_summary:
                        handle.write(f"\n    **Why it matters:** {escape_markdown_inline(lite_summary)}\n")
                handle.write("\n")        
                headline_result = str(art.llm_enrichment.get("headline_result", "")).strip()
                if headline_result:
                    handle.write(f"    **Headline result:** {escape_markdown_inline(headline_result)}\n")
                read_rec = format_read_recommendation(str(art.llm_enrichment.get("read_recommendation", "")))
            takeaways = art.llm_enrichment.get("clinical_takeaway") if art.llm_enrichment else None
            if isinstance(takeaways, list) and takeaways:
                handle.write("\n    **Clinical takeaway:**\n")
                for takeaway in takeaways:
                    txt = str(takeaway).strip()
                    if txt:
                        handle.write(f"    - {escape_markdown_inline(txt)}\n")
            if read_rec:
                handle.write(f"\n    **Read priority:** {escape_markdown_inline(read_rec)}\n")
            handle.write("\n---\n\n")

        handle.write("## Extended Digest (up to 60 minutes)\n\n")
        for i, art in enumerate(extended, start=1):
            title_display = escape_markdown_inline(art.title)
            handle.write(f"{i}. **{title_display}**\n")
            handle.write(
                f"    Journal: {art.journal} | Date: {art.pub_date} | Score: {art.score} (rule {art.rule_score}"
                f"{', llm +' + str(art.llm_score) if art.llm_score else ''})\n"
            )
            handle.write(f"    Journal group: {art.journal_group}\n")
            p_link = pubmed_link(art.pmid)
            handle.write(f"    PubMed: [{p_link}]({p_link})\n")
            if art.doi:
                d_link = doi_link(art.doi)
                handle.write(f"    DOI: [{d_link}]({d_link})\n")
            if art.llm_enrichment:
                lite_summary = str(art.llm_enrichment.get("one_line_summary", "")).strip()
                if lite_summary:
                    handle.write(f"    LLM note: {escape_markdown_inline(lite_summary)}\n")
                read_rec = format_read_recommendation(str(art.llm_enrichment.get("read_recommendation", "")))
                if read_rec:
                    handle.write(f"    Read priority: {escape_markdown_inline(read_rec)}\n")
            handle.write("\n---\n\n")

    payload = []
    for art in top:
        payload.append(
            {
                "pmid": art.pmid,
                "title": art.title,
                "journal": art.journal,
                "journal_group": art.journal_group,
                "pub_date": art.pub_date,
                "score": art.score,
                "rule_score": art.rule_score,
                "llm_score": art.llm_score,
                "score_reasons": art.score_reasons,
                "translation_horizon": art.translation_horizon,
                "category": art.category,
                "article_types": art.article_types,
                "llm_enrichment": art.llm_enrichment,
                "pubmed_url": pubmed_link(art.pmid),
                "doi_url": doi_link(art.doi) if art.doi else None,
            }
        )

    with json_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)

    return md_path, json_path


def run(
    days: int,
    retmax: int,
    output_dir: Path,
    config_dir: Path,
    llm_enrich: bool,
    llm_top_n: int,
    llm_core_top_n: int,
    llm_lite_top_n: int,
    llm_cache_path: Path,
    gemini_model: str,
    llm_batch_size: int,
    llm_lite_batch_size: int,
    llm_batch_delay_seconds: float,
    llm_min_success_rate: float,
    llm_max_requests: int,
    estimate_llm_requests_only: bool,
    llm_output_tokens_per_paper_estimate: int,
    podcast_source: bool,
    podcast_max_items: int,
) -> tuple[Path, Path, int, int, dict[str, Any], Path | None]:
    journal_config = load_json(config_dir / "journals.json")
    topic_config = load_json(config_dir / "topics.json")

    journal_term, _journal_to_tier, journal_to_weight, journal_to_group = build_journal_term(journal_config)
    topic_term = build_topic_term(topic_config)
    full_query = f"({journal_term}) AND ({topic_term})"

    end_date = dt.date.today()
    start_date = end_date - dt.timedelta(days=days)
    pmids = esearch(term=full_query, start_date=start_date, end_date=end_date, retmax=retmax)
    if not pmids:
        raise RuntimeError("No PMIDs returned for the configured query.")

    root = efetch(pmids)
    articles = parse_articles(
        root=root,
        topic_config=topic_config,
        journal_weights=journal_to_weight,
        journal_groups=journal_to_group,
    )
    if estimate_llm_requests_only:
        req_stats = estimate_llm_requests(
            articles=articles,
            llm_top_n=llm_top_n,
            llm_core_top_n=llm_core_top_n,
            llm_lite_top_n=llm_lite_top_n,
            llm_cache_path=llm_cache_path,
            gemini_model=gemini_model,
            llm_batch_size=llm_batch_size,
            llm_lite_batch_size=llm_lite_batch_size,
            llm_output_tokens_per_paper_estimate=llm_output_tokens_per_paper_estimate,
        )
        raise RuntimeError(
            "LLM_REQUEST_ESTIMATE:"
            + json.dumps(req_stats)
        )
    articles, enriched_count, llm_stats = apply_llm_enrichment(
        articles=articles,
        enabled=llm_enrich,
        llm_top_n=llm_top_n,
        llm_core_top_n=llm_core_top_n,
        llm_lite_top_n=llm_lite_top_n,
        llm_cache_path=llm_cache_path,
        gemini_model=gemini_model,
        llm_batch_size=llm_batch_size,
        llm_lite_batch_size=llm_lite_batch_size,
        llm_batch_delay_seconds=llm_batch_delay_seconds,
        llm_max_requests=llm_max_requests,
    )

    # Add display-oriented LLM breakdown so "missing" enriched papers are traceable.
    top = articles[:40]
    core = select_core_digest(articles=articles, core_size=15)
    core_pmids = {art.pmid for art in core}
    extended = [art for art in top if art.pmid not in core_pmids]
    llm_stats["core_enriched_count"] = sum(1 for art in core if art.llm_enrichment)
    llm_stats["extended_enriched_count"] = sum(1 for art in extended if art.llm_enrichment)
    llm_stats["extended_enriched_pmids"] = [art.pmid for art in extended if art.llm_enrichment]
    llm_stats["quota_429_count"] = sum(
        count
        for key, count in (llm_stats.get("error_counts", {}) or {}).items()
        if key.startswith("llm_error:http_429")
    )

    if llm_enrich and llm_min_success_rate > 0 and llm_stats["success_rate"] < llm_min_success_rate:
        raise RuntimeError(
            f"LLM success rate {llm_stats['success_rate']:.2f} below minimum {llm_min_success_rate:.2f}"
        )
    md_path, json_path = write_outputs(
        articles,
        output_dir=output_dir,
        as_of=end_date,
        days=days,
        llm_stats=llm_stats if llm_enrich else None,
    )
    podcast_path = None
    if podcast_source:
        podcast_path = write_podcast_source(
            articles=articles,
            output_dir=output_dir,
            as_of=end_date,
            core_size=max(1, podcast_max_items),
        )
    return md_path, json_path, len(articles), enriched_count, llm_stats, podcast_path, len(pmids)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate weekly ID + general medicine literature digest from PubMed."
    )
    parser.add_argument("--days", type=int, default=7, help="Lookback window in days (default: 7).")
    parser.add_argument(
        "--max-results",
        type=int,
        default=400,
        help="Maximum PubMed records to retrieve before scoring (default: 400).",
    )
    parser.add_argument(
        "--output-dir",
        default="outputs",
        help="Directory for generated markdown/json outputs (default: outputs).",
    )
    parser.add_argument(
        "--config-dir",
        default="config",
        help="Directory containing journals.json and topics.json (default: config).",
    )
    parser.add_argument(
        "--llm-enrich",
        action="store_true",
        help="Enable Gemini enrichment summaries and scoring.",
    )
    parser.add_argument(
        "--llm-top-n",
        type=int,
        default=24,
        help="Legacy full-enrichment target count. Used when --llm-core-top-n is 0 (default: 24).",
    )
    parser.add_argument(
        "--llm-core-top-n",
        type=int,
        default=15,
        help="Number of top papers for full enrichment (default: 15). Set 0 to use --llm-top-n.",
    )
    parser.add_argument(
        "--llm-lite-top-n",
        type=int,
        default=20,
        help="Number of additional papers after core tier for lightweight enrichment (default: 20).",
    )
    parser.add_argument(
        "--llm-cache",
        default="outputs/llm_cache.json",
        help="Cache file path for LLM enrichment results (default: outputs/llm_cache.json).",
    )
    parser.add_argument(
        "--gemini-model",
        default="gemini-2.5-flash",
        help="Gemini model name for enrichment (default: gemini-2.5-flash).",
    )
    parser.add_argument(
        "--llm-batch-size",
        type=int,
        default=2,
        help="Core-tier papers per Gemini request when --llm-enrich is set (default: 2).",
    )
    parser.add_argument(
        "--llm-lite-batch-size",
        type=int,
        default=4,
        help="Lite-tier papers per Gemini request when --llm-enrich is set (default: 4).",
    )
    parser.add_argument(
        "--llm-batch-delay-seconds",
        type=float,
        default=15.0,
        help="Delay between Gemini batch requests in seconds to stay within RPM limits (default: 15).",
    )
    parser.add_argument(
        "--llm-min-success-rate",
        type=float,
        default=0.0,
        help="Optional minimum LLM enrichment success rate (0-1). If lower, run fails (default: 0).",
    )
    parser.add_argument(
        "--safe-mode",
        action="store_true",
        help="Use conservative LLM settings for stability (batch-size 2, delay 15s, top-n 20 if unset).",
    )
    parser.add_argument(
        "--llm-max-requests",
        type=int,
        default=18,
        help="Hard cap on Gemini requests per run (default: 18; set 0 for no cap).",
    )
    parser.add_argument(
        "--estimate-llm-requests",
        action="store_true",
        help="Estimate Gemini request count from current cache and target set, then exit before any LLM calls.",
    )
    parser.add_argument(
        "--llm-output-tokens-per-paper-estimate",
        type=int,
        default=180,
        help="Estimated output tokens per paper for preflight estimation (default: 180).",
    )
    parser.add_argument(
        "--podcast-source",
        action="store_true",
        help="Generate a separate long-form source document from the core digest for AI podcast scripting.",
    )
    parser.add_argument(
        "--podcast-max-items",
        type=int,
        default=15,
        help="Number of core papers to include in podcast source document when --podcast-source is set (default: 15).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    output_dir = Path(args.output_dir).resolve()
    config_dir = Path(args.config_dir).resolve()
    llm_cache_path = Path(args.llm_cache).resolve()
    if args.safe_mode:
        args.llm_batch_size = 2
        args.llm_lite_batch_size = 2
        args.llm_batch_delay_seconds = 15.0
        if args.llm_top_n == 0:
            args.llm_top_n = 20
        if args.llm_core_top_n == 0:
            args.llm_core_top_n = 12
        args.llm_lite_top_n = min(args.llm_lite_top_n, 10)

    try:
        md_path, json_path, count, enriched_count, llm_stats, podcast_path, retrieved_count = run(
            days=args.days,
            retmax=args.max_results,
            output_dir=output_dir,
            config_dir=config_dir,
            llm_enrich=args.llm_enrich,
            llm_top_n=args.llm_top_n,
            llm_core_top_n=args.llm_core_top_n,
            llm_lite_top_n=args.llm_lite_top_n,
            llm_cache_path=llm_cache_path,
            gemini_model=args.gemini_model,
            llm_batch_size=args.llm_batch_size,
            llm_lite_batch_size=args.llm_lite_batch_size,
            llm_batch_delay_seconds=args.llm_batch_delay_seconds,
            llm_min_success_rate=args.llm_min_success_rate,
            llm_max_requests=args.llm_max_requests,
            estimate_llm_requests_only=args.estimate_llm_requests,
            llm_output_tokens_per_paper_estimate=args.llm_output_tokens_per_paper_estimate,
            podcast_source=args.podcast_source,
            podcast_max_items=args.podcast_max_items,
        )
        print(textwrap.dedent(f"""
        PubMed records retrieved: {retrieved_count} (cap: {args.max_results}, cap reached: {retrieved_count >= args.max_results})
        Generated digest with {count} scored items.
        LLM enriched items: {enriched_count}
        LLM target count: {llm_stats["target_count"]}
        LLM enriched in core shown section: {llm_stats.get("core_enriched_count", 0)}
        LLM enriched in extended section: {llm_stats.get("extended_enriched_count", 0)}
        LLM failed count: {llm_stats["failed_count"]}
        LLM success rate: {llm_stats["success_rate"]}
        LLM requests used: {llm_stats["requests_used"]}
        LLM max requests reached: {llm_stats["max_requests_reached"]}
        LLM quota exhausted: {llm_stats["quota_exhausted"]}
        Markdown: {md_path}
        JSON: {json_path}
        Podcast source: {podcast_path if podcast_path else "not generated"}
        """).strip())
        if args.llm_enrich:
            diagnostic = format_llm_diagnostic(llm_stats)
            if diagnostic:
                print(diagnostic)
        return 0
    except Exception as exc:  # pragma: no cover
        marker = "LLM_REQUEST_ESTIMATE:"
        if isinstance(exc, RuntimeError) and str(exc).startswith(marker):
            raw = str(exc)[len(marker):]
            stats = json.loads(raw)
            print(textwrap.dedent(f"""
            LLM request estimate:
            Target papers: {stats["target_count"]}
            Cached papers: {stats["cached_count"]}
            Unresolved papers: {stats["unresolved_count"]}
            Core target / unresolved: {stats.get("core_target_count", "n/a")} / {stats.get("core_unresolved_count", "n/a")}
            Lite target / unresolved: {stats.get("lite_target_count", "n/a")} / {stats.get("lite_unresolved_count", "n/a")}
            Batch size (core/lite): {stats.get("batch_size_core", stats["batch_size"])} / {stats.get("batch_size_lite", "n/a")}
            Estimated new Gemini requests: {stats["estimated_requests"]}
            Estimated core/lite requests: {stats.get("estimated_requests_core", "n/a")} / {stats.get("estimated_requests_lite", "n/a")}
            Estimated unresolved input tokens: {stats["estimated_input_tokens_unresolved"]}
            Estimated output tokens per batch (core/lite): {stats.get("estimated_output_tokens_per_batch_core", stats["estimated_output_tokens_per_batch"])} / {stats.get("estimated_output_tokens_per_batch_lite", "n/a")}
            Estimated total output tokens: {stats["estimated_output_tokens_total"]}
            Output tokens/paper assumption: {stats["output_tokens_per_paper_estimate"]}
            """).strip())
            return 0
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
