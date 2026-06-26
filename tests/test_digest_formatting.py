from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from litdigest.digest import (  # noqa: E402
    Article,
    build_at_a_glance,
    collapse_whitespace,
    escape_markdown_inline,
    trim_clean_sentence,
)


class MarkdownFormattingTests(unittest.TestCase):
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
            [("Top signal", "Important Trial: Treatment improved the primary outcome compared with placebo.")],
        )

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
