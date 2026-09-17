"""JSON-backed audit trail for completed merge request reviews."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from typing import Any, TypedDict


class ReviewRecord(TypedDict):
    """Persisted information about one completed merge request review."""

    review_id: str
    mr_url: str
    mr_title: str
    mr_author: str
    review_type: str
    timestamp: str
    review_content: str
    diff_hash: str


class MRReviewSummary(TypedDict):
    """Summary of audit history for one merge request."""

    mr_url: str
    review_count: int
    latest_review: ReviewRecord | None
    review_ids: list[str]
    review_types: list[str]


class AuditService:
    """Persist and query completed review records in a JSON audit log."""

    _FIELDS = (
        "review_id",
        "mr_url",
        "mr_title",
        "mr_author",
        "review_type",
        "timestamp",
        "review_content",
        "diff_hash",
    )

    def __init__(self, audit_path: str | Path | None = None) -> None:
        """Initialize the audit store under the project audit_logs directory."""

        self._audit_path = (
            Path(audit_path)
            if audit_path
            else Path(__file__).resolve().parent.parent
            / "audit_logs"
            / "reviews.json"
        )

    @staticmethod
    def generate_diff_hash(diff: str) -> str:
        """Return a deterministic SHA-256 hash for the exact diff text."""

        return hashlib.sha256(diff.encode("utf-8")).hexdigest()

    def create_review_record(
        self,
        mr_url: str,
        mr_title: str,
        mr_author: str,
        review_type: str,
        review_content: str,
        diff: str,
    ) -> ReviewRecord:
        """Create an unsaved review record with an ID, timestamp, and diff hash."""

        return {
            "review_id": str(uuid.uuid4()),
            "mr_url": mr_url,
            "mr_title": mr_title,
            "mr_author": mr_author,
            "review_type": review_type,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "review_content": review_content,
            "diff_hash": self.generate_diff_hash(diff),
        }

    def persist_review(self, record: ReviewRecord) -> ReviewRecord:
        """Persist one review record and return it unchanged."""

        if not self._is_valid_record(record):
            raise ValueError("Invalid review record.")

        records = self._load_records()
        records.append(record)
        self._write_records(records)
        return record

    def log_review(
        self,
        mr_url: str,
        mr_title: str,
        mr_author: str,
        review_type: str,
        review_content: str,
        diff: str,
    ) -> ReviewRecord:
        """Create and persist a completed merge request review record."""

        record = self.create_review_record(
            mr_url=mr_url,
            mr_title=mr_title,
            mr_author=mr_author,
            review_type=review_type,
            review_content=review_content,
            diff=diff,
        )
        return self.persist_review(record)

    def get_review(self, review_id: str) -> ReviewRecord | None:
        """Return a review by ID, or None when it is not present."""

        return next(
            (record for record in self._load_records() if record["review_id"] == review_id),
            None,
        )

    def get_mr_history(self, mr_url: str) -> list[ReviewRecord]:
        """Return reviews for one merge request, newest first."""

        records = [record for record in self._load_records() if record["mr_url"] == mr_url]
        return self._sort_newest_first(records)

    def get_recent_reviews(self, limit: int = 10) -> list[ReviewRecord]:
        """Return up to limit reviews across all merge requests, newest first."""

        if limit < 0:
            raise ValueError("limit must not be negative.")
        return self._sort_newest_first(self._load_records())[:limit]

    def has_been_reviewed(self, mr_url: str, diff: str) -> bool:
        """Return whether the same merge request and diff were reviewed before."""

        diff_hash = self.generate_diff_hash(diff)
        return any(
            record["mr_url"] == mr_url and record["diff_hash"] == diff_hash
            for record in self._load_records()
        )

    def get_mr_summary(self, mr_url: str) -> MRReviewSummary:
        """Return review count and relevant history for one merge request."""

        history = self.get_mr_history(mr_url)
        return {
            "mr_url": mr_url,
            "review_count": len(history),
            "latest_review": history[0] if history else None,
            "review_ids": [record["review_id"] for record in history],
            "review_types": list(dict.fromkeys(record["review_type"] for record in history)),
        }

    def export_json(self, destination: str | Path | None = None) -> str:
        """Return all audit data as JSON and optionally write it to a file."""

        content = json.dumps({"reviews": self._load_records()}, indent=2) + "\n"
        if destination is not None:
            self._write_export(destination, content)
        return content

    def export_csv(self, destination: str | Path | None = None) -> str:
        """Return all audit data as CSV and optionally write it to a file."""

        output = StringIO()
        writer = csv.DictWriter(output, fieldnames=self._FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(self._load_records())
        content = output.getvalue()
        if destination is not None:
            self._write_export(destination, content)
        return content

    def _load_records(self) -> list[ReviewRecord]:
        """Load valid records, treating missing or malformed data as empty."""

        try:
            payload = json.loads(self._audit_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []

        raw_records = payload.get("reviews") if isinstance(payload, dict) else payload
        if not isinstance(raw_records, list):
            return []
        return [record for record in raw_records if self._is_valid_record(record)]

    def _write_records(self, records: list[ReviewRecord]) -> None:
        """Write records atomically to the configured JSON audit file."""

        self._audit_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self._audit_path.with_suffix(".tmp")
        content = json.dumps({"reviews": records}, indent=2) + "\n"
        temporary_path.write_text(content, encoding="utf-8")
        os.replace(temporary_path, self._audit_path)

    @staticmethod
    def _write_export(destination: str | Path, content: str) -> None:
        """Write exported audit content to the requested destination."""

        destination_path = Path(destination)
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        destination_path.write_text(content, encoding="utf-8", newline="")

    @classmethod
    def _is_valid_record(cls, record: Any) -> bool:
        """Check that a loaded record has all expected string fields."""

        return isinstance(record, dict) and all(
            isinstance(record.get(field), str) and bool(record[field].strip())
            for field in cls._FIELDS
        )

    @staticmethod
    def _sort_newest_first(records: list[ReviewRecord]) -> list[ReviewRecord]:
        """Sort records by timestamp without failing on malformed timestamps."""

        return sorted(records, key=lambda record: record["timestamp"], reverse=True)
