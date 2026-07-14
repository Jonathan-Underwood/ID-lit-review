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
    build_at_a_glance,
    collapse_whitespace,
    compact_trial_n,
    escape_markdown_inline,
    parse_nathnac_outbreaks_rss,
    trim_clean_sentence,
    write_outputs,
)


class MarkdownFormattingTests(unittest.TestCase):
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
            article_types=["Journal Article"],
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
                "headline_result": "Treatment improved the primary outcome compared with placebo.",
                "read_recommendation": "read_now",
                "trial_n": "n=1015",
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
            "Lancet | 01-01-2026 | Score: 10 (rule 10) | Horizon: 0-12 months | "
            "n=1015 | [PubMed](https://pubmed.ncbi.nlm.nih.gov/1/)",
            text,
        )
        self.assertNotIn("**Trial n:**", text)
        self.assertNotIn("PubMed: [https://pubmed.ncbi.nlm.nih.gov/1/]", text)
        self.assertNotIn("DOI: [https://doi.org/10.1000/example]", text)
        self.assertNotIn("[DOI](https://doi.org/10.1000/example)", text)
        self.assertIn("- **Legionnaires' disease in USA:** Between 2 and 9 July 2026", text)
        self.assertNotIn(f"[Legionnaires' disease in USA]({NATHNAC_OUTBREAKS_URL})", text)

    def test_compact_trial_n_shortens_verbose_values(self) -> None:
        self.assertEqual(
            compact_trial_n(
                "n=956 infants were recruited per country (Uganda and Nepal), "
                "with pre-booster analyses conducted in 876 participants."
            ),
            "n=956 infants were recruited per country",
        )

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
