"""Serialize a `Report` to JSON (string or file)."""

from __future__ import annotations

import json

from storage_validator.models import Report


def report_to_json(report: Report, indent: int = 2) -> str:
    """Serialize a Report to a JSON string, including overall_status."""
    data = report.to_dict()
    data["overall_status"] = report.overall_status()
    return json.dumps(data, indent=indent)


def write_report(report: Report, path: str, indent: int = 2) -> None:
    """Serialize a Report to JSON and write it to `path`."""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(report_to_json(report, indent=indent))
        fh.write("\n")
