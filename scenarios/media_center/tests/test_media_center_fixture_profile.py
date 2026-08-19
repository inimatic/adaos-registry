from __future__ import annotations

import json
from pathlib import Path


FIXTURE = Path(__file__).parent / "fixtures" / "library-profile.v1.json"


def test_representative_library_profile_is_complete_and_bounded() -> None:
    profile = json.loads(FIXTURE.read_text(encoding="utf-8"))

    assert profile["schema"] == "adaos.media_center.test_library_profile.v1"
    assert sum(segment["count"] for segment in profile["segments"]) == 20_000
    assert {segment["id"] for segment in profile["segments"]} == {
        "movies",
        "series",
        "albums",
        "audiobooks",
        "playlists_and_alternatives",
    }
    edge_cases = profile["edge_cases"]
    assert edge_cases["duplicate_sets"] >= 100
    assert len(edge_cases["non_ascii_names"]) >= 3
    assert edge_cases["unavailable_agents"]
    assert edge_cases["slow_roots"] and edge_cases["blocked_roots"]
    assert edge_cases["unsupported_codecs"]
    assert profile["acceptance_views"]["catalog_page_size"] == 30
    assert profile["acceptance_views"]["player_queue_size"] == 10
