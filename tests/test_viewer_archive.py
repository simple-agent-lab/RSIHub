from __future__ import annotations

import copy
import json

from evolve.archive import merge_events, merged_rows


def test_merge_events_matches_file_reduction(tmp_path) -> None:
    """Removing the in-memory reducer would split viewer and CLI ledger semantics."""
    events = [
        {"genid": "1", "parent": "0", "status": "pending"},
        {"genid": "1", "note": "proposal prepared"},
    ]
    path = tmp_path / "archive.jsonl"
    path.write_text("".join(json.dumps(event) + "\n" for event in events))

    assert merge_events(events) == merged_rows(path)


def test_merge_events_keeps_input_immutable() -> None:
    """Removing the event copy would let receipt projection alter cached source data."""
    events = [{"genid": "1", "parent": "0", "_evolve_receipt_certified": True}]
    original = copy.deepcopy(events)

    merge_events(events)

    assert events == original
