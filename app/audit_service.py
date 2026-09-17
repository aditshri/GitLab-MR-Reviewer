"""Persistent audit trail for generated merge-request reviews."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypedDict


class ReviewRecord(TypedDict, total=False):
    """Persisted information about one review run."""

    review_id: str
    mr_url: str
    mr_title: str
    author: str
    mr_author: str
    review_type: str
    timestamp: str
    review_dict: dict[str, Any]
    review_content: str
    diff_hash: str


class AuditService:
    """Store and query review records as JSON files under an audit directory."""

    _LEGACY_FIELDS = (
        "review_id",
        "mr_url",
        "mr_title",
        "mr_author",
        "review_type",
        "timestamp",
        "review_content",
        "diff_hash",
    )

    def __init__(self, audit_dir: str | Path = "audit_logs") -> None:
        """Create an audit directory, or use a JSON file for legacy callers."""

        supplied_path = Path(audit_dir)
        self._legacy_file = supplied_path if supplied_path.suffix.lower() == ".json" else None
        self._audit_dir = supplied_path.parent if self._legacy_file else supplied_path
        self._audit_dir.mkdir(parents=True, exist_ok=True)
        self._audit_path = self._legacy_file or self._audit_dir / "reviews.json"

    @staticmethod
    def generate_diff_hash(diff: str) -> str:
        """Return a deterministic SHA-256 hash for diff text."""

        return hashlib.sha256(diff.encode("utf-8")).hexdigest()

    def log_review(
        self,
        mr_url: str,
        mr_title: str,
        author: str,
        review_type: str,
        review_dict: dict[str, Any] | str,
        diff: str | None = None,
    ) -> str | ReviewRecord:
        """Persist a structured review and return its ID.

        The optional ``diff`` argument preserves the earlier API, which returned
        the complete record rather than only its ID.
        """

        review_id = str(uuid.uuid4())
        timestamp = datetime.now(timezone.utc).isoformat()
        if isinstance(review_dict, str):
            record: ReviewRecord = {
                "review_id": review_id,
                "mr_url": mr_url,
                "mr_title": mr_title,
                "mr_author": author,
                "review_type": review_type,
                "timestamp": timestamp,
                "review_content": review_dict,
                "diff_hash": self.generate_diff_hash(diff or ""),
            }
            self._append_legacy_record(record)
            return record

        diff_value = review_dict.get("diff", "")
        if not isinstance(diff_value, str):
            diff_value = json.dumps(diff_value, sort_keys=True, default=str)
        record = {
            "review_id": review_id,
            "mr_url": mr_url,
            "mr_title": mr_title,
            "author": author,
            "mr_author": author,
            "review_type": review_type,
            "timestamp": timestamp,
            "review_dict": review_dict,
            "diff_hash": self.generate_diff_hash(diff_value),
        }
        self._write_record_file(record)
        return review_id

    def get_trail(self, mr_url: str) -> list[dict[str, Any]]:
        """Return all review records for an MR, newest first."""

        return self._sort_newest_first(
            [record for record in self._load_records() if record.get("mr_url") == mr_url]
        )

    def get_review(self, review_id: str) -> dict[str, Any] | None:
        """Return one review record by ID, or ``None`` when absent."""

        return next(
            (record for record in self._load_records() if record.get("review_id") == review_id),
            None,
        )

    def get_recent(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return the most recent reviews across all merge requests."""

        if limit < 0:
            raise ValueError("limit must not be negative.")
        return self._sort_newest_first(self._load_records())[:limit]

    def get_summary(self, mr_url: str) -> dict[str, Any]:
        """Return issue severity counts and whether the MR was reviewed repeatedly."""

        trail = self.get_trail(mr_url)
        counts = {severity: 0 for severity in ("critical", "high", "medium", "low")}
        for record in trail:
            review_dict = record.get("review_dict")
            issues = review_dict.get("issues", []) if isinstance(review_dict, dict) else []
            if not isinstance(issues, list):
                continue
            for issue in issues:
                if isinstance(issue, dict):
                    severity = str(issue.get("severity", "")).lower()
                    if severity in counts:
                        counts[severity] += 1
        return {
            "mr_url": mr_url,
            "review_count": len(trail),
            "counts_by_severity": counts,
            "re_reviewed": len(trail) > 1,
        }

    def export(self, mr_url: str, format: str = "json") -> str:
        """Export an MR trail to a JSON or CSV file and return its path."""

        export_format = format.lower()
        if export_format not in {"json", "csv"}:
            raise ValueError("format must be 'json' or 'csv'.")
        trail = self.get_trail(mr_url)
        filename = f"{hashlib.sha256(mr_url.encode('utf-8')).hexdigest()[:16]}.{export_format}"
        destination = self._audit_dir / "exports" / filename
        destination.parent.mkdir(parents=True, exist_ok=True)
        if export_format == "json":
            destination.write_text(json.dumps(trail, indent=2) + "\n", encoding="utf-8")
        else:
            fields = sorted({key for record in trail for key in record})
            with destination.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                writer.writerows({key: json.dumps(value) if isinstance(value, (dict, list)) else value for key, value in record.items()} for record in trail)
        return str(destination)

    def compute_delta(self, review_id_a: str, review_id_b: str) -> dict[str, list[dict[str, Any]]]:
        """Return issues newly introduced and resolved between two reviews."""

        first = self._require_review(review_id_a)
        second = self._require_review(review_id_b)
        first_issues = self._issues(first)
        second_issues = self._issues(second)
        first_keys = {self._issue_key(issue): issue for issue in first_issues}
        second_keys = {self._issue_key(issue): issue for issue in second_issues}
        return {
            "new_issues": [issue for key, issue in second_keys.items() if key not in first_keys],
            "resolved_issues": [issue for key, issue in first_keys.items() if key not in second_keys],
        }

    # Compatibility helpers retained for the existing route and test surface.
    def create_review_record(self, mr_url: str, mr_title: str, mr_author: str, review_type: str, review_content: str, diff: str) -> ReviewRecord:
        return {
            "review_id": str(uuid.uuid4()), "mr_url": mr_url, "mr_title": mr_title,
            "mr_author": mr_author, "review_type": review_type,
            "timestamp": datetime.now(timezone.utc).isoformat(), "review_content": review_content,
            "diff_hash": self.generate_diff_hash(diff),
        }

    def persist_review(self, record: ReviewRecord) -> ReviewRecord:
        """Persist an already-created legacy record."""

        self._append_legacy_record(record)
        return record

    def get_mr_history(self, mr_url: str) -> list[dict[str, Any]]:
        return self.get_trail(mr_url)

    def get_recent_reviews(self, limit: int = 10) -> list[dict[str, Any]]:
        return self.get_recent(limit)

    def has_been_reviewed(self, mr_url: str, diff: str) -> bool:
        diff_hash = self.generate_diff_hash(diff)
        return any(record.get("mr_url") == mr_url and record.get("diff_hash") == diff_hash for record in self._load_records())

    def get_mr_summary(self, mr_url: str) -> dict[str, Any]:
        trail = self.get_trail(mr_url)
        return {
            "mr_url": mr_url,
            "review_count": len(trail),
            "latest_review": trail[0] if trail else None,
            "review_ids": [record["review_id"] for record in trail],
            "review_types": list(dict.fromkeys(record.get("review_type", "") for record in trail)),
        }

    def export_json(self, destination: str | Path | None = None) -> str:
        content = json.dumps({"reviews": self._load_records()}, indent=2) + "\n"
        if destination is not None:
            Path(destination).parent.mkdir(parents=True, exist_ok=True)
            Path(destination).write_text(content, encoding="utf-8", newline="")
        return content

    def export_csv(self, destination: str | Path | None = None) -> str:
        records = self._load_records()
        fields = list(self._LEGACY_FIELDS) if self._legacy_file else sorted({key for record in records for key in record})
        from io import StringIO
        output = StringIO()
        writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(records)
        content = output.getvalue()
        if destination is not None:
            Path(destination).parent.mkdir(parents=True, exist_ok=True)
            Path(destination).write_text(content, encoding="utf-8", newline="")
        return content

    def _write_record_file(self, record: ReviewRecord) -> None:
        review_id = record.get("review_id")
        if not review_id:
            raise ValueError("A review record requires a review_id.")
        destination = self._audit_dir / f"{review_id}.json"
        temporary = destination.with_suffix(".tmp")
        temporary.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, destination)

    def _append_legacy_record(self, record: ReviewRecord) -> None:
        records = self._load_records()
        records.append(dict(record))
        self._audit_path.parent.mkdir(parents=True, exist_ok=True)
        self._audit_path.write_text(json.dumps({"reviews": records}, indent=2) + "\n", encoding="utf-8")

    def _load_records(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        if self._legacy_file and self._audit_path.exists():
            try:
                payload = json.loads(self._audit_path.read_text(encoding="utf-8"))
                raw = payload.get("reviews", []) if isinstance(payload, dict) else []
                records.extend(
                    item
                    for item in raw
                    if isinstance(item, dict) and self._is_valid_legacy_record(item)
                )
            except (OSError, json.JSONDecodeError):
                return []
        else:
            for path in self._audit_dir.glob("*.json"):
                try:
                    item = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if isinstance(item, dict) and item.get("review_id"):
                    records.append(item)
        return records

    @staticmethod
    def _sort_newest_first(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(records, key=lambda record: str(record.get("timestamp", "")), reverse=True)

    @staticmethod
    def _issues(record: dict[str, Any]) -> list[dict[str, Any]]:
        review_dict = record.get("review_dict")
        issues = review_dict.get("issues", []) if isinstance(review_dict, dict) else []
        return [issue for issue in issues if isinstance(issue, dict)] if isinstance(issues, list) else []

    @staticmethod
    def _issue_key(issue: dict[str, Any]) -> tuple[str, ...]:
        return tuple(str(issue.get(field, "")).strip().lower() for field in ("category", "file", "line", "description"))

    def _require_review(self, review_id: str) -> dict[str, Any]:
        review = self.get_review(review_id)
        if review is None:
            raise KeyError(f"Review '{review_id}' was not found.")
        return review

    @classmethod
    def _is_valid_legacy_record(cls, record: dict[str, Any]) -> bool:
        """Keep the original audit-file reader strict for legacy callers."""

        return all(
            isinstance(record.get(field), str) and bool(record[field].strip())
            for field in cls._LEGACY_FIELDS
        )
