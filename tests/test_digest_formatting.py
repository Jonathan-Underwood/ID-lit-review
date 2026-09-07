import datetime as dt
from pathlib import Path
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from litdigest.digest import (  # noqa: E402
    Article,
    NATHNAC_OUTBREAKS_URL,
    OutbreakItem,
    _compact_at_a_glance_text,
    build_at_a_glance,
    cached_enrichment_is_usable,
    collapse_whitespace,
    compact_trial_n,
    escape_markdown_inline,
    is_rct_article,
    load_json,
    parse_nathnac_outbreaks_rss,
    primary_result_box_markdown,
    score_article,
    trim_clean_sentence,
    write_outputs,
)


class MarkdownFormattingTests(unittest.TestCase):
    def test_systematic_review_of_rcts_is_not_labelled_rct(self) -> None:
        self.assertFalse(
            is_rct_article(
                ["Journal Article", "Systematic Review", "Meta-Analysis"],
                "Indirect vaccine effectiveness under randomized conditions: a systematic review and meta-analysis",
                "We included twelve randomized controlled trials.",
            )
        )

    def test_pooled_trial_analysis_is_not_labelled_primary_rct(self) -> None:
        self.assertFalse(
            is_rct_article(
                ["Journal Article"],
                "Electronic nudges: a pooled analysis of two nationwide randomized trials",
                "We performed a participant-level pooled analysis of two randomized clinical trials.",
            )
        )

    def test_pubmed_rct_type_does_not_override_secondary_analysis(self) -> None:
        self.assertFalse(
            is_rct_article(
                ["Journal Article", "Randomized Controlled Trial"],
                "Predictors of treatment failure in children in the ODYSSEY trial",
                "ODYSSEY demonstrated superior efficacy. We assessed predictors at treatment initiation.",
            )
        )

    def test_secondary_rct_analysis_does_not_receive_rct_score_bonus(self) -> None:
        topic_config = load_json(PROJECT_ROOT / "config" / "topics.json")
        _score, reasons, _category = score_article(
            journal="Clin Infect Dis",
            journal_group="infectious_diseases_microbiology_ipc",
            article_types=["Journal Article", "Randomized Controlled Trial"],
            title="Predictors of treatment failure in children in the ODYSSEY trial",
            abstract=(
                "ODYSSEY demonstrated superior efficacy. We assessed predictors at treatment initiation."
            ),
            topic_config=topic_config,
            journal_weights={"clin infect dis": 3},
        )

        self.assertIn("article_type:rct_suppressed_secondary_analysis", reasons)
        self.assertFalse(any(reason.startswith("article_type:randomized controlled trial=+") for reason in reasons))

    def test_primary_rct_survives_later_post_hoc_endpoint_wording(self) -> None:
        self.assertTrue(
            is_rct_article(
                ["Journal Article", "Randomized Controlled Trial"],
                "Extracorporeal CO2 elimination: a randomized controlled trial",
                "Adults were randomized to treatment or control. Results are reported for the primary endpoint. "
                "A later device-free-days endpoint was assessed in a post hoc exploratory analysis.",
            )
        )

    def test_primary_phase_one_rct_with_intervening_design_descriptors(self) -> None:
        self.assertTrue(
            is_rct_article(
                ["Journal Article"],
                "Safety and pharmacokinetics: a randomized, double-blind, placebo-controlled, "
                "first-in-human phase 1 clinical trial",
                "This study assessed safety and pharmacokinetics in healthy adults.",
            )
        )

    def test_trial_protocol_is_not_labelled_rct(self) -> None:
        self.assertFalse(
            is_rct_article(
                ["Journal Article", "Clinical Trial Protocol"],
                "Protocol for a randomized controlled trial of a new intervention",
                "Participants will be randomly assigned.",
            )
        )

    def test_old_cache_is_invalidated_only_for_nonprimary_trial_papers(self) -> None:
        article = Article(
            pmid="secondary",
            title="A pooled analysis of two randomized trials",
            journal="Lancet",
            pub_date="01-09-2026",
            abstract="We combined participant-level trial data.",
            article_types=["Journal Article"],
            doi=None,
            linked_comment_pmids=[],
            journal_group="general_medicine_acute_care",
            score=1,
            score_reasons=[],
            category="Clinical/Translational (0-12 months likely)",
            translation_horizon="0-12 months",
            rule_score=1,
            llm_score=0,
            llm_enrichment=None,
        )
        old_cache = {
            "model": "gemini-3.5-flash",
            "profile": "full",
            "enrichment": {"headline_result": "Old text"},
        }
        updated_cache = {
            **old_cache,
            "rct_guardrail_version": 1,
            "enrichment_version": 5,
        }

        self.assertFalse(
            cached_enrichment_is_usable(
                old_cache,
                art=article,
                expected_model="gemini-3.5-flash",
                expected_profile="full",
            )
        )
        self.assertTrue(
            cached_enrichment_is_usable(
                updated_cache,
                art=article,
                expected_model="gemini-3.5-flash",
                expected_profile="full",
            )
        )

    def test_primary_result_box_has_structured_outcome_effect_and_ci(self) -> None:
        box = primary_result_box_markdown(
            {
                "primary_outcome": "Vancomycin-free hours alive through day 7",
                "effect_estimate": "Adjusted difference 4.0 (109.7 vs 105.7)",
                "confidence_interval": "95% CI -9.5 to 18.2",
                "effect_units": "hours",
            }
        )

        self.assertIn("> **PRIMARY RESULT**", box)
        self.assertIn("**Primary outcome:** Vancomycin-free hours alive through day 7", box)
        self.assertIn(
            "**Effect estimate:** Adjusted difference 4.0 (109.7 vs 105.7) "
            "(95% CI -9.5 to 18.2) hours",
            box,
        )

    def test_primary_result_box_fills_an_unreported_element(self) -> None:
        box = primary_result_box_markdown(
            {
                "primary_outcome": "All-cause mortality at day 28",
                "effect_estimate": "Risk ratio 0.92",
                "effect_units": "",
            }
        )

        self.assertIn("**Effect estimate:** Risk ratio 0.92 (CI not reported in abstract)", box)

    def test_non_rct_result_box_uses_headline_without_effect_or_ci(self) -> None:
        box = primary_result_box_markdown(
            {
                "at_a_glance_summary": "Vaccination was associated with fewer hospital admissions across the included observational studies.",
                "outcome_label": "Main outcome",
                "primary_outcome": "Hospital admission",
                "effect_estimate": "Risk ratio 0.78",
                "confidence_interval": "95% CI 0.70 to 0.87",
            },
            structured_result=False,
        )

        self.assertIn("> **HEADLINE RESULT**", box)
        self.assertIn("Vaccination was associated with fewer hospital admissions", box)
        self.assertNotIn("Primary outcome", box)
        self.assertNotIn("Effect estimate", box)
        self.assertNotIn("95% CI", box)

    def test_confirmed_outbreak_source_typo_is_corrected(self) -> None:
        rss = """<?xml version="1.0" encoding="UTF-8" ?>
        <rss version="2.0"><channel><item>
          <title>Ebola disease in DRC</title>
          <description>There were 6,100 confirmed cases and 2,950 deatha.</description>
          <pubDate>Sun, 06 Sep 2026 00:00:00 +0000</pubDate>
        </item></channel></rss>
        """

        outbreaks = parse_nathnac_outbreaks_rss(rss)

        self.assertEqual(
            outbreaks[0].description,
            "There were 6,100 confirmed cases and 2,950 deaths.",
        )

    def test_nathnac_rss_items_use_outbreaks_page_when_link_missing(self) -> None:
        rss = """<?xml version="1.0" encoding="UTF-8" ?>
        <rss version="2.0">
          <channel>
            <item>
              <title>Crimean-Congo haemorrhagic fever in Spain</title>
              <description>As of 10 July 2026, local authorities reported &lt;b&gt;one death&lt;/b&gt;. Please see our Topics in Brief article for further details on Crimean-Congo haemorrhagic fever.</description>
              <pubDate>Mon, 13 Jul 2026 00:00:00 +0000</pubDate>
            </item>
            <item>
              <title>Older outbreak</title>
              <description>An older update outside the digest window.</description>
              <pubDate>Wed, 01 Jul 2026 00:00:00 +0000</pubDate>
            </item>
          </channel>
        </rss>
        """

        outbreaks = parse_nathnac_outbreaks_rss(
            rss,
            start_date=dt.date(2026, 7, 6),
            end_date=dt.date(2026, 7, 13),
        )

        self.assertEqual(len(outbreaks), 1)
        self.assertEqual(outbreaks[0].title, "Crimean-Congo haemorrhagic fever in Spain")
        self.assertEqual(outbreaks[0].description, "As of 10 July 2026, local authorities reported one death.")
        self.assertEqual(outbreaks[0].pub_date, "Mon, 13 Jul 2026 00:00:00 +0000")
        self.assertEqual(outbreaks[0].link, NATHNAC_OUTBREAKS_URL)

    def test_outbreak_watch_titles_are_bold_not_per_item_links(self) -> None:
        article = Article(
            pmid="1",
            title="Important Trial.",
            journal="Lancet",
            pub_date="01-01-2026",
            abstract="Abstract",
            article_types=["Journal Article", "Randomized Controlled Trial"],
            doi="10.1000/example",
            linked_comment_pmids=[],
            journal_group="general_medicine_acute_care",
            score=10,
            score_reasons=[],
            category="Clinical/Translational (0-12 months likely)",
            translation_horizon="0-12 months",
            rule_score=10,
            llm_score=0,
            llm_enrichment={
                "at_a_glance_summary": "Treatment improved recovery compared with placebo.",
                "why_it_matters_points": [
                    "The outcome affects an important clinical decision.",
                    "The intervention could change near-term practice.",
                ],
                "study_design": "Multicentre, double-blind, placebo-controlled randomized trial.",
                "outcome_label": "Primary outcome",
                "primary_outcome": "Clinical recovery at day 28",
                "effect_estimate": "Risk ratio 1.24",
                "confidence_interval": "95% CI 1.08 to 1.42",
                "effect_units": "",
                "read_recommendation": "read_now",
                "trial_n": "n=1015",
                "clinical_implications": [
                    "The treatment may improve recovery in similar patients.",
                    "Applicability outside the trial population remains uncertain.",
                ],
            },
        )
        outbreak = OutbreakItem(
            title="Legionnaires' disease in USA",
            description="Between 2 and 9 July 2026, 46 confirmed cases were reported.",
            pub_date="Fri, 10 Jul 2026 00:00:00 +0000",
            link=NATHNAC_OUTBREAKS_URL,
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            md_path, _json_path = write_outputs(
                articles=[article],
                output_dir=Path(tmp_dir),
                as_of=dt.date(2026, 7, 13),
                days=7,
                outbreaks=[outbreak],
            )
            text = md_path.read_text(encoding="utf-8")

        self.assertIn(
            f"Recent updates from [NaTHNaC TravelHealthPro outbreaks]({NATHNAC_OUTBREAKS_URL}) "
            "in the last 7 days:",
            text,
        )
        self.assertIn("## Outbreak Watch\n\n", text)
        self.assertIn("- Window: last 7 days | Scored papers: 1 | Core: 1 | Extended: 0", text)
        self.assertIn("## Core Digest\n\n", text)
        self.assertIn("## Extended Digest\n\n", text)
        self.assertNotIn("Core Digest (10-15 mins)", text)
        self.assertNotIn("Extended Digest (up to 60 minutes)", text)
        self.assertIn(
            "Lancet | RCT | 01-01-2026 | Score: 10 (rule 10) | Horizon: 0-12 months | "
            "n=1015 | [PubMed](https://pubmed.ncbi.nlm.nih.gov/1/)",
            text,
        )
        self.assertNotIn("**Trial n:**", text)
        self.assertNotIn("Read priority", text)
        self.assertNotIn("Group:", text)
        self.assertNotIn("general_medicine_acute_care", text)
        self.assertNotIn("PubMed: [https://pubmed.ncbi.nlm.nih.gov/1/]", text)
        self.assertNotIn("DOI: [https://doi.org/10.1000/example]", text)
        self.assertNotIn("[DOI](https://doi.org/10.1000/example)", text)
        self.assertIn("    > **PRIMARY RESULT**", text)
        self.assertIn("**Primary outcome:** Clinical recovery at day 28", text)
        self.assertIn("**Effect estimate:** Risk ratio 1.24 (95% CI 1.08 to 1.42)", text)
        why_index = text.index("**Why it matters:**")
        design_index = text.index("**Study design:**")
        result_index = text.index("**PRIMARY RESULT**")
        self.assertLess(why_index, design_index)
        self.assertLess(design_index, result_index)
        self.assertEqual(text.count("The outcome affects an important clinical decision."), 1)
        self.assertEqual(text.count("The intervention could change near-term practice."), 1)
        self.assertNotIn("**Headline result:**", text)
        self.assertIn("**Clinical implications:**", text)
        self.assertIn("- **Legionnaires' disease in USA:** Between 2 and 9 July 2026", text)
        self.assertNotIn(f"[Legionnaires' disease in USA]({NATHNAC_OUTBREAKS_URL})", text)

    def test_methods_qa_appendix_records_run_specific_provenance(self) -> None:
        article = Article(
            pmid="1",
            title="Important Trial.",
            journal="Lancet",
            pub_date="01-09-2026",
            abstract="Abstract",
            article_types=["Randomized Controlled Trial"],
            doi="10.1000/example",
            linked_comment_pmids=[],
            journal_group="general_medicine_acute_care",
            score=10,
            score_reasons=[],
            category="Clinical/Translational (0-12 months likely)",
            translation_horizon="0-12 months",
            rule_score=8,
            llm_score=2,
            llm_enrichment=None,
        )
        methods_qa = {
            "journal_groups": {"General medicine and acute care": ["Lancet", "BMJ"]},
            "search_query": '("Lancet"[Journal]) AND ("sepsis"[Title/Abstract])',
            "topic_groups": {
                "Clinical translation terms": ["randomized"],
                "Infectious-disease priority terms": ["sepsis", "bloodstream infection"],
            },
            "date_field": "PubMed record creation date (CRDT; previous complete UTC days)",
            "start_date": "2026-08-30",
            "end_date": "2026-09-06",
            "cutoff_utc": "2026-09-06T09:13:04+00:00",
            "retmax": 750,
            "retrieved_count": 626,
            "scored_count": 479,
            "models": {"full": "gemini-3.5-flash", "lite": "gemini-3.5-flash-lite"},
        }

        with tempfile.TemporaryDirectory() as tmp_dir:
            md_path, _json_path = write_outputs(
                articles=[article],
                output_dir=Path(tmp_dir),
                as_of=dt.date(2026, 9, 6),
                days=7,
                methods_qa=methods_qa,
            )
            text = md_path.read_text(encoding="utf-8")

        self.assertIn("## Methods and QA Appendix", text)
        self.assertIn("Database: PubMed via NCBI E-utilities", text)
        self.assertIn("Registries: none", text)
        self.assertIn("applying \\[Journal\\] to every journal", text)
        self.assertIn("**Infectious-disease priority terms:**", text)
        self.assertIn('"sepsis", "bloodstream infection"', text)
        self.assertIn("search executed at 2026-09-06T09:13:04+00:00", text)
        self.assertIn("626 PubMed records retrieved -> 479 eligible records scored -> 1 displayed", text)
        self.assertIn("478 eligible records were below the display cut-off", text)
        self.assertIn("has not been externally validated", text)
        self.assertIn("gemini-3.5-flash-lite", text)

    def test_compact_trial_n_shortens_verbose_values(self) -> None:
        self.assertEqual(
            compact_trial_n(
                "n=956 infants were recruited per country (Uganda and Nepal), "
                "with pre-booster analyses conducted in 876 participants."
            ),
            "n=956 infants were recruited per country",
        )

    def test_compact_trial_n_hides_unreported_sample_size(self) -> None:
        self.assertEqual(compact_trial_n("not reported"), "")

    def test_angle_brackets_are_not_escaped(self) -> None:
        text = 'p<0.0001 and RNA <50 copies/mL in ">=88%"'

        self.assertEqual(
            escape_markdown_inline(text),
            "p<0.0001 and RNA <50 copies/mL in >=88%",
        )

    def test_awkward_comparison_quotes_are_cleaned(self) -> None:
        text = 'aged ">=50 years, compared with ">="5 days, and response in ">=88%"'

        self.assertEqual(
            collapse_whitespace(text),
            "aged >=50 years, compared with >=5 days, and response in >=88%",
        )

    def test_unicode_comparison_symbols_are_ascii_normalized(self) -> None:
        self.assertEqual(collapse_whitespace("aged ≥50 years and BMI ≤30"), "aged >=50 years and BMI <=30")

    def test_markdown_control_characters_are_still_escaped(self) -> None:
        text = "A [trial] with *signal* and _subgroup_"

        self.assertEqual(
            escape_markdown_inline(text),
            r"A \[trial\] with \*signal\* and \_subgroup\_",
        )

    def test_at_a_glance_uses_ranked_headlines(self) -> None:
        article = Article(
            pmid="1",
            title="Important Trial.",
            journal="Lancet",
            pub_date="01-01-2026",
            abstract="Abstract",
            article_types=["Journal Article"],
            doi=None,
            linked_comment_pmids=[],
            journal_group="general_medicine_acute_care",
            score=10,
            score_reasons=[],
            category="Clinical/Translational (0-12 months likely)",
            translation_horizon="0-12 months",
            rule_score=10,
            llm_score=0,
            llm_enrichment={
                "headline_result": "Treatment improved the primary outcome compared with placebo.",
                "read_recommendation": "read_now",
            },
        )

        self.assertEqual(
            build_at_a_glance([article], max_items=1),
            [("General medicine highlight (core #1)", "Treatment improved the primary outcome compared with placebo.")],
        )

    def test_at_a_glance_compacts_long_headlines(self) -> None:
        article = Article(
            pmid="1",
            title="Screening Trial.",
            journal="Lancet",
            pub_date="01-01-2026",
            abstract="Abstract",
            article_types=["Journal Article"],
            doi=None,
            linked_comment_pmids=[],
            journal_group="general_medicine_acute_care",
            score=10,
            score_reasons=[],
            category="Clinical/Translational (0-12 months likely)",
            translation_horizon="0-12 months",
            rule_score=10,
            llm_score=0,
            llm_enrichment={
                "headline_result": (
                    "Phone-based screening was non-inferior to home-based screening for overall "
                    "tuberculosis detection (rate difference 0.73 [95% CI 0.05-1.41]) among "
                    "survivors and contacts, but home-based screening detected a higher recurrence "
                    "rate in survivors."
                ),
                "read_recommendation": "read_now",
            },
        )

        self.assertEqual(
            build_at_a_glance([article], max_items=1),
            [
                (
                    "General medicine highlight (core #1)",
                    "Phone-based screening was non-inferior to home-based screening for overall tuberculosis detection among survivors and contacts.",
                )
            ],
        )

    def test_at_a_glance_does_not_end_midway_through_outcome_list(self) -> None:
        text = (
            "Treatment with nirmatrelvir-ritonavir for either 15 or 25 days yielded no "
            "statistically significant improvement in patient-reported outcomes at 90 days "
            "compared to placebo across cognitive, autonomic, and exercise phenotypes "
            "(e.g., cognitive phenotype difference 3.2% [95% CI -10.4 to 16.8])."
        )

        self.assertEqual(
            _compact_at_a_glance_text(text),
            "Treatment with nirmatrelvir-ritonavir for either 15 or 25 days yielded no "
            "statistically significant improvement in patient-reported outcomes at 90 days "
            "compared to placebo.",
        )

    def test_at_a_glance_removes_statistical_parentheses_before_compacting(self) -> None:
        text = (
            "Couples HIV testing uptake by 12 months postpartum was significantly higher in "
            "the home visit group (56.2%; aRR 4.22) and HIV self-test group "
            "(50.0%; aRR 3.69) compared with standard care (13.6%)."
        )

        self.assertEqual(
            _compact_at_a_glance_text(text),
            "Couples HIV testing uptake by 12 months postpartum was significantly higher in "
            "the home visit group and HIV self-test group compared with standard care.",
        )

    def test_successful_noninferiority_is_not_negative_signal(self) -> None:
        top_article = Article(
            pmid="1",
            title="Top Trial.",
            journal="Lancet",
            pub_date="01-01-2026",
            abstract="Abstract",
            article_types=["Journal Article"],
            doi=None,
            linked_comment_pmids=[],
            journal_group="general_medicine_acute_care",
            score=10,
            score_reasons=[],
            category="Clinical/Translational (0-12 months likely)",
            translation_horizon="0-12 months",
            rule_score=10,
            llm_score=0,
            llm_enrichment={
                "headline_result": "Treatment improved the primary outcome compared with placebo.",
                "read_recommendation": "read_now",
            },
        )
        noninferiority_article = Article(
            pmid="2",
            title="Noninferiority Trial.",
            journal="Lancet",
            pub_date="01-01-2026",
            abstract="Abstract",
            article_types=["Journal Article"],
            doi=None,
            linked_comment_pmids=[],
            journal_group="unknown",
            score=9,
            score_reasons=[],
            category="Important Basic/Mechanistic Science (>12 months)",
            translation_horizon=">12 months",
            rule_score=9,
            llm_score=0,
            llm_enrichment={
                "headline_result": "The regimen was non-inferior to standard care for viral suppression.",
                "read_recommendation": "read_if_time",
            },
        )

        labels = [label for label, _text in build_at_a_glance([top_article, noninferiority_article])]

        self.assertNotIn("Negative or neutral signal (core #2)", labels)

    def test_trimming_does_not_stop_on_decimal_points(self) -> None:
        text = "Result was 13.9% vs 11.1%; HR 1.25; P = 0.08 in the main analysis."

        self.assertEqual(
            trim_clean_sentence(text, 50),
            "Result was 13.9% vs 11.1%; HR 1.25; P = 0.08 in...",
        )

    def test_trimming_avoids_dangling_statistical_fragments(self) -> None:
        text = "The intervention did not reduce the outcome (13.9% vs. 11.1%; HR 1.25)."

        self.assertEqual(
            trim_clean_sentence(text, 57),
            "The intervention did not reduce the outcome (13.9%...",
        )


if __name__ == "__main__":
    unittest.main()
