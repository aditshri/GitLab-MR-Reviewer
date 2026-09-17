"""Unit tests for AuditService."""

import csv
import json
from io import StringIO
from pathlib import Path

import pytest

from app.audit_service import AuditService


MR_URL = "https://gitlab.example.invalid/team/project/-/merge_requests/7"


def _record(service: AuditService, diff: str = "diff content", **overrides: str) -> dict[str, str]:
    """Create a valid test record without real credentials."""

    record = service.create_review_record(
        mr_url=MR_URL,
        mr_title="Improve parser",
        mr_author="author",
        review_type="Quick",
        review_content="Review content",
        diff=diff,
    )
    record.update(overrides)
    return record


@pytest.fixture
def service(tmp_path: Path) -> AuditService:
    """Use an isolated temporary JSON audit file."""

    return AuditService(tmp_path / "audit_logs" / "reviews.json")


def test_creating_review_record_includes_required_fields(service: AuditService) -> None:
    """A new record contains identity, review, timestamp, and hash data."""

    record = _record(service)

    assert record["review_id"]
    assert record["mr_url"] == MR_URL
    assert record["mr_title"] == "Improve parser"
    assert record["mr_author"] == "author"
    assert record["review_type"] == "Quick"
    assert record["timestamp"]
    assert record["review_content"] == "Review content"
    assert record["diff_hash"] == service.generate_diff_hash("diff content")


def test_persisting_review_creates_json_audit_file(
    service: AuditService,
) -> None:
    """Persisting a record creates the configured JSON file."""

    record = service.persist_review(_record(service))

    assert service._audit_path.exists()
    assert service.get_review(record["review_id"]) == record


def test_review_can_be_retrieved_by_id(service: AuditService) -> None:
    """A persisted review is returned by its unique ID."""

    record = service.log_review(
        MR_URL,
        "Improve parser",
        "author",
        "Comprehensive",
        "Detailed review",
        "diff",
    )

    assert service.get_review(record["review_id"]) == record
    assert service.get_review("missing-review") is None


def test_mr_history_returns_only_matching_reviews_newest_first(
    service: AuditService,
) -> None:
    """MR history is scoped by URL and sorted newest first."""

    older = _record(service, diff="older", timestamp="2026-09-15T10:00:00+00:00")
    newer = _record(service, diff="newer", timestamp="2026-09-15T11:00:00+00:00")
    other = _record(
        service,
        diff="other",
        mr_url="https://gitlab.example.invalid/team/project/-/merge_requests/8",
        timestamp="2026-09-15T12:00:00+00:00",
    )
    service.persist_review(older)
    service.persist_review(newer)
    service.persist_review(other)

    history = service.get_mr_history(MR_URL)

    assert [record["review_id"] for record in history] == [
        newer["review_id"],
        older["review_id"],
    ]


def test_recent_reviews_respects_limit_and_sort_order(service: AuditService) -> None:
    """Recent reviews are global, newest first, and limited."""

    for index in range(3):
        service.persist_review(
            _record(
                service,
                diff=f"diff-{index}",
                timestamp=f"2026-09-15T1{index}:00:00+00:00",
            )
        )

    recent = service.get_recent_reviews(limit=2)

    assert len(recent) == 2
    assert recent[0]["timestamp"] > recent[1]["timestamp"]
    assert service.get_recent_reviews(limit=0) == []


def test_diff_hash_is_deterministic(service: AuditService) -> None:
    """The same diff always produces the same hash."""

    assert service.generate_diff_hash("same diff") == service.generate_diff_hash("same diff")
    assert service.generate_diff_hash("same diff") != service.generate_diff_hash("other diff")


def test_same_mr_and_diff_are_detected_as_duplicate(service: AuditService) -> None:
    """A matching MR URL and diff hash are recognized as already reviewed."""

    service.persist_review(_record(service, diff="unchanged"))

    assert service.has_been_reviewed(MR_URL, "unchanged") is True


def test_different_diff_is_not_detected_as_duplicate(service: AuditService) -> None:
    """A changed diff is not treated as the same review state."""

    service.persist_review(_record(service, diff="old version"))

    assert service.has_been_reviewed(MR_URL, "new version") is False


def test_mr_summary_contains_count_and_history(service: AuditService) -> None:
    """The MR summary includes count, latest review, IDs, and review types."""

    record = _record(service, review_type="Security")
    service.persist_review(record)

    summary = service.get_mr_summary(MR_URL)

    assert summary["mr_url"] == MR_URL
    assert summary["review_count"] == 1
    assert summary["latest_review"] == record
    assert summary["review_ids"] == [record["review_id"]]
    assert summary["review_types"] == ["Security"]


def test_json_export_returns_and_writes_audit_data(
    service: AuditService,
    tmp_path: Path,
) -> None:
    """JSON export contains persisted reviews and can write to a destination."""

    record = service.persist_review(_record(service))
    destination = tmp_path / "exports" / "reviews.json"

    content = service.export_json(destination)

    assert json.loads(content) == {"reviews": [record]}
    assert json.loads(destination.read_text(encoding="utf-8")) == {"reviews": [record]}


def test_csv_export_returns_and_writes_audit_data(
    service: AuditService,
    tmp_path: Path,
) -> None:
    """CSV export contains the audit fields and can write to a destination."""

    record = service.persist_review(_record(service))
    destination = tmp_path / "exports" / "reviews.csv"

    content = service.export_csv(destination)
    rows = list(csv.DictReader(StringIO(content)))

    assert rows == [record]
    assert destination.read_text(encoding="utf-8") == content


def test_missing_audit_data_is_empty_and_exportable(tmp_path: Path) -> None:
    """A missing audit file behaves as an empty audit history."""

    service = AuditService(tmp_path / "missing.json")

    assert service.get_recent_reviews() == []
    assert service.get_mr_history(MR_URL) == []
    assert json.loads(service.export_json()) == {"reviews": []}


def test_malformed_audit_data_is_ignored(tmp_path: Path) -> None:
    """Malformed JSON does not prevent safe empty reads or exports."""

    audit_path = tmp_path / "reviews.json"
    audit_path.write_text("{not valid json", encoding="utf-8")
    service = AuditService(audit_path)

    assert service.get_recent_reviews() == []
    assert service.export_csv().splitlines()[0].startswith("review_id,")


def test_invalid_records_are_ignored(tmp_path: Path) -> None:
    """Well-formed JSON with invalid records is handled safely."""

    audit_path = tmp_path / "reviews.json"
    audit_path.write_text(json.dumps({"reviews": [{"review_id": "incomplete"}]}), encoding="utf-8")

    assert AuditService(audit_path).get_recent_reviews() == []
