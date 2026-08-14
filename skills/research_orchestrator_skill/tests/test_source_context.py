from __future__ import annotations

from research.orchestrator import ResearchOrchestrator


def test_source_context_discloses_balanced_coverage_and_provenance(monkeypatch) -> None:
    refs = {
        "notebook": "artifact://skill/tlp/part0/notebook#cell=1",
        "review": "artifact://skill/tlp/part0/review#lines=1-20",
    }

    def extract_text(_skill_id, _group_id, artifact_id, *, max_characters, query=""):
        key = "notebook" if artifact_id == "notebook" else "review"
        return {
            "artifact_ref": refs[key].split("#", 1)[0],
            "content": f"--- fragment [{refs[key]}] ---\nsource",
            "coverage": {
                "strategy": "notebook_semantic_digest_v1" if key == "notebook" else "utf8_line_chunks",
                "selected_characters": 6,
                "truncated": False,
            },
            "provenance": [{"ref": refs[key]}],
        }

    monkeypatch.setattr("research.orchestrator.artifact_context.extract_text", extract_text)
    bundle = {
        "skill_ref": "skill:tlp",
        "sources": [
            {"source_id": "notebook", "group_id": "part0", "name": "experiment.ipynb", "artifact_ref": refs["notebook"].split("#", 1)[0], "digest": "sha256:" + "1" * 64},
            {"source_id": "review", "group_id": "part0", "name": "review.md", "artifact_ref": refs["review"].split("#", 1)[0], "digest": "sha256:" + "2" * 64},
        ],
    }

    context = ResearchOrchestrator(repository=object())._source_context(bundle)

    assert context["coverage"]["sources_total"] == 2
    assert context["coverage"]["sources_represented"] == 2
    assert context["coverage"]["selected_characters"] == 12
    assert context["coverage"]["unreadable_sources"] == []
    assert context["coverage"]["items"][0]["provenance_refs"] == [refs["notebook"]]
