import time

import pytest

from ai_usage_board.core.models import (
    SERVICE_ORDER,
    SnapshotError,
    clean_text,
    number,
    order_services,
    validate_snapshot,
)
from ai_usage_board.providers.codex import normalize


BASE = ["claude", "codex", "gemini", "antigravity"]


def test_default_order_is_claude_codex_gemini_antigravity():
    assert SERVICE_ORDER == tuple(BASE)
    assert order_services([]) == BASE


def test_order_puts_the_single_connected_service_first():
    assert order_services(["codex"]) == ["codex", "claude", "gemini", "antigravity"]
    assert order_services(["gemini"]) == ["gemini", "claude", "codex", "antigravity"]
    assert order_services(["claude"]) == BASE
    assert order_services(["antigravity"]) == ["antigravity", "claude", "codex", "gemini"]


def test_order_is_stable_for_multiple_connections():
    # Connected keep base order among themselves, then the rest.
    assert order_services(["gemini", "codex"]) == ["codex", "gemini", "claude", "antigravity"]
    assert order_services(["antigravity", "claude"]) == ["claude", "antigravity", "codex", "gemini"]
    assert order_services(BASE) == BASE


def test_order_ignores_unknown_keys():
    assert order_services(["nope"]) == BASE


def test_number_rejects_bool_and_nan():
    with pytest.raises(SnapshotError):
        number(True, 0, 100)
    with pytest.raises(SnapshotError):
        number(float("nan"), 0, 100)
    with pytest.raises(SnapshotError):
        number(101, 0, 100)
    assert number(42, 0, 100) == 42.0


def test_clean_text_collapses_and_truncates():
    assert clean_text("  a \n b  ") == "a b"
    assert len(clean_text("x" * 500)) == 120
    assert clean_text("ab") == "ab"
    with pytest.raises(SnapshotError):
        clean_text(None)


def test_validate_snapshot_rebuilds_payload():
    payload = {
        "provider": "claude",
        "windows": [
            {"label": "주간", "used": 61.5, "resetText": "11/20 09:00", "evil": "drop me"}
        ],
        "note": "ok",
        "pageUpdatedAt": time.time(),
        "injected": "drop me too",
    }
    result = validate_snapshot(payload)
    assert set(result) == {"provider", "windows", "note", "updatedAt", "source", "error"}
    assert set(result["windows"][0]) == {"label", "used", "resetText"}
    assert result["source"] == "browser"


def test_validate_snapshot_rejects_unknown_provider_and_overflow():
    with pytest.raises(SnapshotError):
        validate_snapshot({"provider": "evil", "windows": []})
    with pytest.raises(SnapshotError):
        validate_snapshot({"provider": "claude", "windows": [{}] * 9})
    with pytest.raises(SnapshotError):
        validate_snapshot({"provider": "claude", "windows": [{"label": "x", "used": 1000}]})


def test_validate_snapshot_ignores_a_lying_page_clock():
    result = validate_snapshot(
        {"provider": "gemini", "windows": [], "pageUpdatedAt": 1}
    )
    assert abs(result["updatedAt"] - time.time()) < 5


def test_normalize_codex_reports_used_not_remaining():
    # Shape the CLI actually sends: limit id -> {primary, secondary}.
    result = normalize(
        {
            "ordinaryUsageAllowed": True,
            "rateLimits": {
                "limitId": "codex",
                "primary": {"usedPercent": 51, "windowDurationMins": 300, "resetsAt": 1794547250},
                "secondary": {"usedPercent": 11, "windowDurationMins": 10080},
            },
            "rateLimitsByLimitId": {
                "codex": {
                    "limitId": "codex",
                    "primary": {"usedPercent": 51, "windowDurationMins": 300, "resetsAt": 1794547250},
                    "secondary": {"usedPercent": 11, "windowDurationMins": 10080},
                }
            },
        }
    )
    labels = {w["label"]: w["used"] for w in result["windows"]}
    assert labels == {"5시간": 51.0, "주간": 11.0}
    assert result["source"] == "cli"


def test_normalize_codex_falls_back_to_the_flat_reply():
    result = normalize(
        {
            "rateLimits": {
                "primary": {"usedPercent": 25, "windowDurationMins": 300},
                "secondary": {"usedPercent": 10, "windowDurationMins": 10080},
            }
        }
    )
    labels = {w["label"]: w["used"] for w in result["windows"]}
    assert labels == {"5시간": 25.0, "주간": 10.0}


def test_normalize_codex_labels_each_limit_when_several():
    result = normalize(
        {
            "rateLimitsByLimitId": {
                "codex": {"limitName": "Codex", "primary": {"usedPercent": 10, "windowDurationMins": 300}},
                "zed": {"limitName": "Zed", "primary": {"usedPercent": 20, "windowDurationMins": 300}},
            }
        }
    )
    assert {w["label"] for w in result["windows"]} == {"Codex 5시간", "Zed 5시간"}


def test_normalize_codex_without_buckets():
    result = normalize({})
    assert result["windows"] == []
    assert "없습니다" in result["note"]


def test_a_working_reading_says_nothing_about_where_it_came_from():
    # A healthy card does not spend a row saying where its figure came from:
    # the card title already says so. That row is kept for errors.
    result = normalize({"rateLimits": {"primary": {"usedPercent": 22, "windowDurationMins": 300}}})
    assert result["windows"]
    assert result["note"] == ""
