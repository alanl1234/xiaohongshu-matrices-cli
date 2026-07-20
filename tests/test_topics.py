"""Tests for topic resolution and hash_tag payload building."""

from unittest.mock import MagicMock

from xhs_cli.command_normalizers import resolve_topic_payload


def _client_returning(payload):
    """Build a fake client whose search_topics returns the given structure."""
    client = MagicMock()
    client.search_topics.return_value = payload
    return client


def test_exact_match_returns_payload():
    client = _client_returning(
        {"topic_info_dtos": [{"id": "65a1b2", "name": "考研", "type": "topic"}]}
    )
    payload, miss = resolve_topic_payload(client, "考研")
    assert payload == {"id": "65a1b2", "name": "考研", "type": "topic"}
    assert miss is None


def test_fuzzy_retry_on_noise():
    # First call (exact) empty, second call (cleaned) hits.
    client = MagicMock()
    client.search_topics.side_effect = [
        {},  # exact "考研!" -> empty
        {},  # (only one call expected, but side_effect needs enough entries)
    ]
    # Rebuild with side_effect that returns empty for raw then a hit for cleaned.
    client = MagicMock()
    client.search_topics.side_effect = [
        {"topic_info_dtos": []},  # exact "考研!" yields nothing
        {"topic_info_dtos": [{"id": "65a1b2", "name": "考研", "type": "topic"}]},  # cleaned "考研" hits
    ]
    payload, miss = resolve_topic_payload(client, "考研!")
    assert payload is not None and payload["id"] == "65a1b2"
    assert miss is None
    # search_topics must have been called twice (exact + cleaned retry)
    assert client.search_topics.call_count == 2


def test_explicit_id_override_when_search_fails():
    client = _client_returning({"topic_info_dtos": []})  # nothing found
    payload, miss = resolve_topic_payload(client, "冷门话题", explicit_id="99zz")
    assert payload == {"id": "99zz", "name": "冷门话题", "type": "topic"}
    assert miss is None


def test_unresolved_when_no_match_and_no_explicit_id():
    client = _client_returning({"topic_info_dtos": []})
    payload, miss = resolve_topic_payload(client, "冷门话题")
    assert payload is None
    assert miss == "冷门话题"


def test_search_exception_does_not_propagate():
    client = MagicMock()
    client.search_topics.side_effect = RuntimeError("network down")
    payload, miss = resolve_topic_payload(client, "考研", explicit_id="65a1b2")
    # Falls back to explicit id instead of raising.
    assert payload == {"id": "65a1b2", "name": "考研", "type": "topic"}
    assert miss is None
