from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import email_digest  # noqa: E402


class EmailDigestTests(unittest.TestCase):
    def test_summary_extracts_new_inline_pubmed_links(self) -> None:
        markdown = """# Weekly ID + General Medicine Literature Digest (09-08-2026)

## Core Digest

1. **Important trial title.**
    Lancet | 08-08-2026 | Score: 18 | [PubMed](https://pubmed.ncbi.nlm.nih.gov/12345/)

2. **Another trial.**
    JAMA | 07-08-2026 | Score: 12 | [PubMed](https://pubmed.ncbi.nlm.nih.gov/67890/)
"""

        headline, entries = email_digest.extract_summary_entries(markdown)

        self.assertEqual(headline, "Weekly ID + General Medicine Literature Digest (09-08-2026)")
        self.assertEqual(
            entries,
            [
                ("Important trial title.", "https://pubmed.ncbi.nlm.nih.gov/12345/"),
                ("Another trial.", "https://pubmed.ncbi.nlm.nih.gov/67890/"),
            ],
        )

    def test_summary_still_extracts_legacy_pubmed_lines(self) -> None:
        markdown = """# Weekly ID + General Medicine Literature Digest

1. **Legacy title.**
    PubMed: [https://pubmed.ncbi.nlm.nih.gov/12345/](https://pubmed.ncbi.nlm.nih.gov/12345/)
"""

        _headline, entries = email_digest.extract_summary_entries(markdown)

        self.assertEqual(entries, [("Legacy title.", "https://pubmed.ncbi.nlm.nih.gov/12345/")])


if __name__ == "__main__":
    unittest.main()
