import json
import os
import datetime as dt
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from litdigest.digest import (  # noqa: E402
    Article,
    EFETCH_URL,
    ESEARCH_URL,
    LLMEnrichmentError,
    apply_llm_enrichment,
    efetch,
    esearch,
    gemini_enrich_batch,
    load_cache,
    ncbi_get,
    parse_args,
    post_json,
    reset_http_telemetry,
    save_cache,
    snapshot_http_telemetry,
)


class FakeResponse:
    def __init__(self, payload: bytes):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self) -> bytes:
        return self.payload


def make_article(pmid: str, score: int) -> Article:
    return Article(
        pmid=pmid,
        title=f"Article {pmid}",
        journal="Test Journal",
        pub_date="01-01-2026",
        abstract="A useful abstract.",
        article_types=["Journal Article"],
        doi=None,
        linked_comment_pmids=[],
        journal_group="unknown",
        score=score,
        score_reasons=[],
        category="Clinical/Translational (0-12 months likely)",
        translation_horizon="0-12 months",
        rule_score=score,
        llm_score=0,
        llm_enrichment=None,
    )


class PubMedReliabilityTests(unittest.TestCase):
    def test_esearch_uses_creation_date_query_not_publication_date_parameters(self) -> None:
        captured_params: dict[str, str] = {}

        def fake_ncbi_get(url: str, params: dict[str, str]) -> bytes:
            self.assertEqual(url, ESEARCH_URL)
            captured_params.update(params)
            return b'{"esearchresult":{"idlist":["123"]}}'

        with mock.patch("litdigest.digest.ncbi_get", side_effect=fake_ncbi_get):
            pmids = esearch(
                term='("Lancet"[Journal])',
                start_date=dt.date(2026, 8, 30),
                end_date=dt.date(2026, 9, 5),
                retmax=750,
            )

        self.assertEqual(pmids, ["123"])
        self.assertIn("2026/08/30:2026/09/05[crdt]", captured_params["term"])
        self.assertNotIn("datetype", captured_params)
        self.assertNotIn("mindate", captured_params)
        self.assertNotIn("maxdate", captured_params)

    def test_ncbi_get_retries_read_timeout(self) -> None:
        response = FakeResponse(b"ok")
        env = {
            "NCBI_HTTP_RETRY_ATTEMPTS": "2",
            "NCBI_HTTP_BACKOFF_SECONDS": "0",
            "NCBI_HTTP_RETRY_JITTER_SECONDS": "0",
        }
        with (
            mock.patch.dict(os.environ, env),
            mock.patch(
                "litdigest.digest.urllib.request.urlopen",
                side_effect=[TimeoutError("read timed out"), response],
            ) as urlopen,
            mock.patch("litdigest.digest.time.sleep") as sleep,
        ):
            payload = ncbi_get(ESEARCH_URL, {"db": "pubmed"}, timeout=1)

        self.assertEqual(payload, b"ok")
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(0.0)

    def test_efetch_splits_large_id_list_into_batches(self) -> None:
        requested_batches: list[list[str]] = []

        def fake_ncbi_get(url: str, params: dict[str, str]) -> bytes:
            self.assertEqual(url, EFETCH_URL)
            ids = params["id"].split(",")
            requested_batches.append(ids)
            articles = "".join(
                f"<PubmedArticle><MedlineCitation><PMID>{pmid}</PMID></MedlineCitation></PubmedArticle>"
                for pmid in ids
            )
            return f"<PubmedArticleSet>{articles}</PubmedArticleSet>".encode()

        with (
            mock.patch.dict(os.environ, {"NCBI_EFETCH_BATCH_SIZE": "2"}),
            mock.patch("litdigest.digest.ncbi_get", side_effect=fake_ncbi_get),
        ):
            root = efetch(["1", "2", "3", "4", "5"])

        self.assertEqual(requested_batches, [["1", "2"], ["3", "4"], ["5"]])
        self.assertEqual(
            [node.text for node in root.findall(".//PMID")],
            ["1", "2", "3", "4", "5"],
        )

    def test_default_pubmed_cap_is_750(self) -> None:
        self.assertEqual(parse_args([]).max_results, 750)


class LLMReliabilityTests(unittest.TestCase):
    def test_loading_old_cache_removes_read_recommendation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "cache.json"
            path.write_text(
                json.dumps(
                    {
                        "1": {
                            "model": "old-model",
                            "enrichment": {
                                "headline_result": "Useful result.",
                                "read_recommendation": "read_now",
                            },
                        }
                    }
                ),
                encoding="utf-8",
            )
            cache = load_cache(path)

        self.assertNotIn("read_recommendation", cache["1"]["enrichment"])

    def test_gemini_3_request_uses_supported_thinking_and_default_sampling(self) -> None:
        article = make_article("1", 10)
        response = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "text": json.dumps(
                                    {
                                        "items": [
                                            {
                                                "pmid": "1",
                                                "one_line_summary": "Useful finding.",
                                                "read_recommendation": "read_if_time",
                                                "clinical_relevance_12m": 3,
                                                "translation_horizon": "0-12 months",
                                                "confidence": 0.8,
                                            }
                                        ]
                                    }
                                )
                            }
                        ]
                    }
                }
            ]
        }
        with mock.patch("litdigest.digest.post_json", return_value=response) as post:
            result = gemini_enrich_batch(
                [article], "gemini-3.5-flash-lite", "test-key", profile="lite"
            )

        generation_config = post.call_args.kwargs["payload"]["generationConfig"]
        self.assertEqual(generation_config["thinkingConfig"]["thinkingLevel"], "MINIMAL")
        self.assertNotIn("temperature", generation_config)
        item_schema = generation_config["responseSchema"]["properties"]["items"]["items"]
        self.assertNotIn("read_recommendation", item_schema["properties"])
        self.assertNotIn("confidence", item_schema["properties"])
        self.assertNotIn("read_recommendation", result["1"])
        self.assertNotIn("confidence", result["1"])
        self.assertEqual(result["1"]["one_line_summary"], "Useful finding.")

    def test_full_gemini_schema_requests_structured_primary_result(self) -> None:
        article = make_article("1", 10)
        article.abstract = ("A" * 5100) + " FULL_ABSTRACT_END"
        item = {
            "pmid": "1",
            "why_it_matters_points": ["A", "B"],
            "study_design": "Double-blind, placebo-controlled randomized trial.",
            "at_a_glance_summary": "Treatment improved recovery.",
            "outcome_label": "Primary outcome",
            "primary_outcome": "Clinical recovery at day 28",
            "effect_estimate": "Risk ratio 1.24",
            "confidence_interval": "95% CI 1.08 to 1.42",
            "effect_units": "",
            "trial_n": "n=1015",
            "major_limitation": "Single-centre trial.",
            "clinical_implications": ["The treatment may improve recovery.", "Confirm applicability."],
            "clinical_impact_12m": 4,
            "method_quality": 4,
            "novelty": 3,
            "translation_horizon": "0-12 months",
        }
        response = {
            "candidates": [{"content": {"parts": [{"text": json.dumps({"items": [item]})}]}}]
        }
        with mock.patch("litdigest.digest.post_json", return_value=response) as post:
            result = gemini_enrich_batch([article], "gemini-3.5-flash", "test-key", profile="full")

        item_schema = post.call_args.kwargs["payload"]["generationConfig"]["responseSchema"][
            "properties"
        ]["items"]["items"]
        self.assertEqual(item_schema["properties"]["why_it_matters_points"]["minItems"], 2)
        self.assertEqual(item_schema["properties"]["why_it_matters_points"]["maxItems"], 2)
        for field in (
            "study_design",
            "at_a_glance_summary",
            "outcome_label",
            "primary_outcome",
            "effect_estimate",
            "confidence_interval",
            "effect_units",
        ):
            self.assertIn(field, item_schema["required"])
            self.assertEqual(result["1"][field], item[field])
        self.assertNotIn("action", item_schema["properties"])
        self.assertNotIn("confidence", item_schema["properties"])
        for scoring_field in ("clinical_impact_12m", "method_quality", "novelty"):
            self.assertIn(scoring_field, item_schema["required"])
        prompt_text = post.call_args.kwargs["payload"]["contents"][0]["parts"][0]["text"]
        self.assertIn("FULL_ABSTRACT_END", prompt_text)

    def test_full_and_lite_profiles_use_separate_models(self) -> None:
        articles = [make_article("1", 10), make_article("2", 9)]
        results = [
            {
                "1": {
                    "why_it_matters_points": ["A", "B"],
                    "study_design": "Randomized controlled trial.",
                    "headline_result": "Useful result.",
                    "trial_n": "n=100",
                    "major_limitation": "Small sample.",
                    "clinical_takeaway": ["Consider it.", "Verify it."],
                    "read_recommendation": "read_now",
                    "clinical_impact_12m": 4,
                    "method_quality": 4,
                    "novelty": 3,
                    "action": "discuss",
                    "translation_horizon": "0-12 months",
                    "confidence": 0.8,
                }
            },
            {"2": {"read_recommendation": "read_if_time", "translation_horizon": ">12 months"}},
        ]
        with (
            tempfile.TemporaryDirectory() as tmp_dir,
            mock.patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}),
            mock.patch("litdigest.digest.gemini_enrich_batch", side_effect=results) as enrich,
        ):
            _articles, enriched_count, stats = apply_llm_enrichment(
                articles=articles,
                enabled=True,
                llm_top_n=1,
                llm_core_top_n=1,
                llm_lite_top_n=1,
                llm_cache_path=Path(tmp_dir) / "cache.json",
                gemini_model="gemini-3.5-flash",
                gemini_lite_model="gemini-3.5-flash-lite",
                llm_batch_size=1,
                llm_lite_batch_size=1,
                llm_batch_delay_seconds=0,
                llm_max_requests=2,
            )

        requested_models = [call.kwargs["gemini_model"] for call in enrich.call_args_list[:2]]
        self.assertEqual(requested_models, ["gemini-3.5-flash", "gemini-3.5-flash-lite"])
        self.assertEqual(enriched_count, 2)
        self.assertEqual(
            stats["models"],
            {"full": "gemini-3.5-flash", "lite": "gemini-3.5-flash-lite"},
        )

    def test_lite_model_continues_after_full_model_quota_exhaustion(self) -> None:
        articles = [make_article("1", 10), make_article("2", 9)]
        lite_core = {"1": {"one_line_summary": "First paper fallback summary."}}
        lite_second = {"2": {"one_line_summary": "Second paper summary."}}
        with (
            tempfile.TemporaryDirectory() as tmp_dir,
            mock.patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}),
            mock.patch(
                "litdigest.digest.gemini_enrich_batch",
                side_effect=[
                    LLMEnrichmentError("http_429: exceeded your current quota"),
                    lite_core,
                    lite_second,
                ],
            ) as enrich,
        ):
            enriched_articles, enriched_count, stats = apply_llm_enrichment(
                articles=articles,
                enabled=True,
                llm_top_n=1,
                llm_core_top_n=1,
                llm_lite_top_n=1,
                llm_cache_path=Path(tmp_dir) / "cache.json",
                gemini_model="gemini-3.5-flash",
                gemini_lite_model="gemini-3.5-flash-lite",
                llm_batch_size=1,
                llm_lite_batch_size=1,
                llm_batch_delay_seconds=0,
                llm_max_requests=4,
            )

        requested_models = [call.kwargs["gemini_model"] for call in enrich.call_args_list]
        requested_profiles = [call.kwargs["profile"] for call in enrich.call_args_list]
        self.assertEqual(
            requested_models,
            ["gemini-3.5-flash", "gemini-3.5-flash-lite", "gemini-3.5-flash-lite"],
        )
        self.assertEqual(requested_profiles, ["full", "full", "lite"])
        self.assertEqual(enriched_count, 2)
        self.assertEqual(stats["enriched_count"], 2)
        self.assertEqual(stats["success_rate"], 1.0)
        self.assertEqual(stats["quota_exhausted_models"], ["gemini-3.5-flash"])
        self.assertEqual(stats["phase_stats"]["full_fallback"]["target_count"], 1)
        self.assertEqual(stats["phase_stats"]["full_fallback"]["items_enriched"], 1)
        self.assertEqual(stats["phase_stats"]["lite"]["target_count"], 1)
        self.assertEqual(stats["salvage_stats"]["requests_attempted"], 0)
        self.assertTrue(all(article.llm_enrichment for article in enriched_articles))

    def test_workflow_manual_runs_default_to_no_email_and_has_quality_gate(self) -> None:
        workflow = (PROJECT_ROOT / ".github" / "workflows" / "weekly-digest.yml").read_text(
            encoding="utf-8"
        )
        wrapper = (PROJECT_ROOT / "scripts" / "run_weekly_digest.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn("send_email:", workflow)
        self.assertIn("default: false", workflow)
        self.assertIn("github.event_name == 'schedule'", workflow)
        self.assertIn('LLM_MIN_EMAIL_SUCCESS_RATE: "0.5"', workflow)
        self.assertIn('LLM_MIN_EMAIL_CORE_ENRICHED: "10"', workflow)
        self.assertIn('LLM_MIN_EMAIL_SUCCESS_RATE="${LLM_MIN_EMAIL_SUCCESS_RATE:-0.5}"', wrapper)
        self.assertIn('LLM_MIN_EMAIL_CORE_ENRICHED="${LLM_MIN_EMAIL_CORE_ENRICHED:-10}"', wrapper)
        self.assertIn("Email quality gate failed", wrapper)

    def test_post_json_retries_raw_read_timeout(self) -> None:
        response = FakeResponse(json.dumps({"ok": True}).encode())
        env = {
            "LLM_HTTP_RETRY_ATTEMPTS": "2",
            "LLM_HTTP_BACKOFF_SECONDS": "0",
            "LLM_HTTP_RETRY_JITTER_SECONDS": "0",
        }
        reset_http_telemetry()
        with (
            mock.patch.dict(os.environ, env),
            mock.patch(
                "litdigest.digest.urllib.request.urlopen",
                side_effect=[TimeoutError("read timed out"), response],
            ),
            mock.patch("litdigest.digest.time.sleep"),
        ):
            result = post_json("https://example.test", {}, {}, timeout=1)

        self.assertEqual(result, {"ok": True})
        telemetry = snapshot_http_telemetry()
        self.assertEqual(telemetry["total_attempts"], 2)
        self.assertEqual(telemetry["retries_performed"], 1)
        self.assertEqual(telemetry["successful_responses"], 1)

    def test_partial_enrichment_is_checkpointed_and_returned_after_timeout(self) -> None:
        articles = [make_article("1", 10), make_article("2", 9)]
        first_result = {
            "1": {
                "headline_result": "A useful result.",
                "read_recommendation": "read_now",
                "translation_horizon": "0-12 months",
            }
        }
        with tempfile.TemporaryDirectory() as tmp_dir:
            cache_path = Path(tmp_dir) / "llm_cache.json"
            with (
                mock.patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}),
                mock.patch(
                    "litdigest.digest.gemini_enrich_batch",
                    side_effect=[first_result, TimeoutError("read timed out")],
                ),
                mock.patch("litdigest.digest.save_cache", wraps=save_cache) as checkpoint,
            ):
                enriched_articles, enriched_count, stats = apply_llm_enrichment(
                    articles=articles,
                    enabled=True,
                    llm_top_n=2,
                    llm_core_top_n=2,
                    llm_lite_top_n=0,
                    llm_cache_path=cache_path,
                    gemini_model="test-model",
                    llm_batch_size=1,
                    llm_lite_batch_size=1,
                    llm_batch_delay_seconds=0,
                    llm_max_requests=2,
                )

            cache = json.loads(cache_path.read_text(encoding="utf-8"))

        self.assertEqual(enriched_count, 1)
        self.assertEqual(stats["enriched_count"], 1)
        self.assertEqual(stats["failed_count"], 1)
        self.assertEqual(cache["1"]["enrichment"]["headline_result"], "A useful result.")
        self.assertGreaterEqual(checkpoint.call_count, 2)
        by_pmid = {article.pmid: article for article in enriched_articles}
        self.assertIsNotNone(by_pmid["1"].llm_enrichment)
        self.assertTrue(any(reason.startswith("llm_error:") for reason in by_pmid["2"].score_reasons))


if __name__ == "__main__":
    unittest.main()
