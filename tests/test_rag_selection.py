"""Explicit per-section routing for future RAG slicing."""
from __future__ import annotations

import unittest

from tests._path import *  # noqa: F401,F403
from ckp.md_render import extract_rag_payload


def md(body: str, *, status: str = "draft") -> str:
    return f'''---
knowledge_id: "k-test"
revision: "1"
title: "Routing test"
status: "{status}"
project_id: "p-test"
stage: "方案与设计"
---
{body}'''


class RagSelectionTests(unittest.TestCase):
    def test_routes_body_and_exclude_by_marker_only(self):
        result = extract_rag_payload(md('''## Arbitrary heading A <!-- kd-section id=business_rule rag=body -->
This is searchable knowledge.
### Child heading <!-- kd-section id=business_rule_detail rag=body -->
Still part of body.
### Child exclusion <!-- kd-section id=child_exclusion rag=exclude -->
Child text stays out regardless of the parent's route.

## Appendix <!-- kd-section id=appendix rag=exclude -->
Do not publish.'''))

        self.assertTrue(result["routing_valid"])
        body_sections = [x for x in result["sections"] if x["route"] == "body"]
        self.assertEqual(["business_rule", "business_rule_detail"],
                         [x["id"] for x in body_sections])
        self.assertEqual(["Arbitrary heading A", "Arbitrary heading A / Child heading"],
                         [" / ".join(x["heading_path"]) for x in body_sections])
        self.assertIn("This is searchable knowledge.", result["retrieval_md"])
        self.assertIn("Still part of body.", result["retrieval_md"])
        self.assertNotIn("source position", result["retrieval_md"])
        self.assertNotIn("Private processing details", result["retrieval_md"])
        self.assertNotIn("Child text stays out", result["retrieval_md"])
        self.assertNotIn("Do not publish", result["retrieval_md"])
        self.assertEqual("kd-rag-selection/v3", result["schema"])
        self.assertEqual("draft", result["metadata"]["status"])
        self.assertEqual({"knowledge_id": "k-test", "revision": "1",
                          "content_hash": None}, result["document_ref"])
        self.assertEqual(result["document_ref"], result["sections"][0]["document_ref"])

    def test_section_manifest_links_sources_and_protected_assets(self):
        sources = [{"source_ref_id": "file-1@version-1", "file_id": "file-1",
                    "version_id": "version-1", "sha256": "a" * 64}]
        assets = [{"asset_id": "diagram-1", "body_ref": "asset_id=diagram-1"}]
        result = extract_rag_payload(md('''## Architecture <!-- kd-section id=architecture rag=body -->
See asset_id=diagram-1 for the full structure.'''), assets, sources)

        section = result["sections"][0]
        self.assertEqual(sources, section["source_refs"])
        self.assertEqual(["diagram-1"], section["protected_asset_refs"])

    def test_legacy_routes_are_rejected_and_not_auto_mapped(self):
        for route in ("metadata", "audit"):
            with self.subTest(route=route):
                result = extract_rag_payload(md(
                    f"## Legacy heading <!-- kd-section id=legacy rag={route} -->\n"
                    "Content needing review."))
                self.assertFalse(result["routing_valid"])
                self.assertEqual("", result["retrieval_md"])

    def test_missing_or_malformed_marker_fails_closed(self):
        for heading in ("## No marker", "## Invalid <!-- kd-section id=NoUpper rag=body -->",
                        "## Invalid route <!-- kd-section id=bad_route rag=guess -->",
                        "### Child without marker"):
            with self.subTest(heading=heading):
                result = extract_rag_payload(md(f"{heading}\nMust not be indexed."))
                self.assertFalse(result["routing_valid"])
                self.assertEqual("", result["retrieval_md"])
                self.assertTrue(result["errors"])

    def test_duplicate_section_ids_fail_closed(self):
        result = extract_rag_payload(md('''## First <!-- kd-section id=duplicate rag=body -->
One.
## Second <!-- kd-section id=duplicate rag=body -->
Two.'''))
        self.assertFalse(result["routing_valid"])
        self.assertEqual("", result["retrieval_md"])
        self.assertTrue(any("重复" in error for error in result["errors"]))


if __name__ == "__main__":
    unittest.main()
