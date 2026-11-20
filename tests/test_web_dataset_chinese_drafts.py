import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CANDIDATES = ROOT / "exports/data-review/dataset-candidates-20261013.json"
DRAFTS = ROOT / "deploy/dataset-chinese-drafts.json"


def _source_hash(metadata):
    text = json.dumps(
        metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(text.encode()).hexdigest()


def test_chinese_drafts_cover_the_current_candidate_snapshot():
    source = json.loads(CANDIDATES.read_text(encoding="utf-8"))
    drafts = json.loads(DRAFTS.read_text(encoding="utf-8"))

    assert drafts["schema"] == "researchkit.web-dataset-chinese-drafts/v1"
    candidates = {item["candidate_id"]: item for item in source["candidates"]}
    assert len(candidates) == 66
    assert set(drafts["drafts"]) == set(candidates)

    for candidate_id, candidate in candidates.items():
        original = candidate["metadata"]
        draft = drafts["drafts"][candidate_id]
        localized = draft["metadata"]
        assert draft["wiki_ref"] == original["wiki_ref"]
        assert draft["source_sha256"] == _source_hash(original)
        assert set(localized) <= {
            "description", "format", "usage", "license", "student_conditions"
        }
        assert isinstance(localized["description"], str)
        assert localized["description"].strip()
        assert any("\u4e00" <= char <= "\u9fff" for char in localized["description"])


def test_absent_usage_and_license_are_not_invented():
    source = json.loads(CANDIDATES.read_text(encoding="utf-8"))
    drafts = json.loads(DRAFTS.read_text(encoding="utf-8"))["drafts"]

    for candidate in source["candidates"]:
        original = candidate["metadata"]
        localized = drafts[candidate["candidate_id"]]["metadata"]
        for field in ("format", "usage", "license", "student_conditions"):
            if field not in original:
                assert field not in localized
        if "description" not in original:
            assert "具体说明尚未收录" in localized["description"]
