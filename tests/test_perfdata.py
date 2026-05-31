"""Tests for the Nagios perf_data parser + the three perfdata tools (issue #284).

Before #284 the server exposed no way to read the ``perf_data`` column, so the
agent could only report OK/CRITICAL, never "disk C: is at 77 %, closest to its
90 % warn threshold". These tests pin:

* the Nagios-spec parser against the real ``ecrmut-ad-01`` fixture (quoted
  labels with spaces/':'/'*', ranges incl. negative min:max, empty perf_data,
  value-only, empty trailing fields, B/%/ms/s UOM, inverted "Days life");
* range-correct ``breached`` semantics (NOT a naive ``value > warn``);
* the three respx-mocked tools (``thruk_get_perfdata``,
  ``thruk_perfdata_snapshot``, ``thruk_perfdata_near_threshold``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import ok
from thruk_mcp.perfdata import (
    breaches_range,
    parse_perfdata,
    parse_range,
    proximity_percent,
)

FIXTURE = Path(__file__).parent / "fixtures" / "perfdata_ecrmut_ad_01.json"


@pytest.fixture(scope="module")
def dump() -> dict[str, Any]:
    with FIXTURE.open(encoding="utf-8") as fh:
        return json.load(fh)


def _by_label(perf: str) -> dict[str, dict[str, Any]]:
    return {m["label"]: m for m in parse_perfdata(perf)}


# ---------------------------------------------------------------------------
# parse_range — Nagios threshold-range spec
# ---------------------------------------------------------------------------


class TestParseRange:
    def test_bare_scalar_is_zero_to_n(self) -> None:
        assert parse_range("10") == (0.0, 10.0, False)

    def test_open_ended_high(self) -> None:
        low, high, inside = parse_range("10:")
        assert (low, inside) == (10.0, False)
        assert high == float("inf")

    def test_tilde_low(self) -> None:
        low, high, inside = parse_range("~:10")
        assert low == float("-inf")
        assert (high, inside) == (10.0, False)

    def test_explicit_range(self) -> None:
        assert parse_range("10:20") == (10.0, 20.0, False)

    def test_negative_min_max(self) -> None:
        assert parse_range("-2000:2000") == (-2000.0, 2000.0, False)

    def test_inverted_at_prefix(self) -> None:
        assert parse_range("@10:20") == (10.0, 20.0, True)

    def test_empty_is_none(self) -> None:
        assert parse_range("") is None
        assert parse_range(None) is None


class TestBreachesRange:
    def test_normal_over_high(self) -> None:
        assert breaches_range(15, "10") is True
        assert breaches_range(5, "10") is False

    def test_normal_under_low(self) -> None:
        assert breaches_range(-1, "0:10") is True

    def test_negative_range_inside_not_breached(self) -> None:
        # 'offset' = -0.1459 within -2000:2000 -> not breached.
        assert breaches_range(-0.1459, "-2000:2000") is False
        assert breaches_range(-2500, "-2000:2000") is True

    def test_inverted_alert_inside(self) -> None:
        assert breaches_range(15, "@10:20") is True
        assert breaches_range(25, "@10:20") is False

    def test_none_value_or_empty_spec_never_breaches(self) -> None:
        assert breaches_range(None, "10") is False
        assert breaches_range(99, "") is False
        assert breaches_range(99, None) is False


# ---------------------------------------------------------------------------
# parse_perfdata — acceptance criteria from the fixture
# ---------------------------------------------------------------------------


class TestParsePerfdataFixture:
    def test_quoted_label_with_spaces_colon_star(self, dump: dict[str, Any]) -> None:
        svc = next(
            s for s in dump["services"] if s["description"] == "SERVICE_IIS-CERTIFICATES_STATUS"
        )
        metrics = parse_perfdata(svc["perf_data"])
        assert len(metrics) == 1
        m = metrics[0]
        # The label keeps spaces, ':' and '*' verbatim (no whitespace split).
        assert m["label"] == "Days life : *.ecritel.net"
        assert m["value"] == 263
        assert m["warn"] == "366"
        assert m["crit"] == "389"
        assert m["min"] == 0
        assert m["max"] == 396

    def test_inverted_direction_breached_is_range_correct(self, dump: dict[str, Any]) -> None:
        # 'Days life'=263 warn 366 crit 389: lower-is-worse, but breached MUST
        # follow the literal Nagios range (0:366 -> alert if >366), so 263 is
        # NOT breached. We expose raw warn/crit; we do not guess direction.
        svc = next(
            s for s in dump["services"] if s["description"] == "SERVICE_IIS-CERTIFICATES_STATUS"
        )
        assert parse_perfdata(svc["perf_data"])[0]["breached"] is False

    def test_negative_range_metric(self, dump: dict[str, Any]) -> None:
        svc = next(s for s in dump["services"] if s["description"] == "SERVICE_NTPD_CLIENT-STATUS")
        m = _by_label(svc["perf_data"])
        assert m["offset"]["value"] == -0.1459
        assert m["offset"]["uom"] == "ms"
        assert m["offset"]["warn"] == "-2000:2000"
        assert m["offset"]["crit"] == "-10000:10000"
        assert m["offset"]["breached"] is False
        # 'stratum'=3;;;0 -> empty warn/crit, min present.
        assert m["stratum"]["warn"] is None
        assert m["stratum"]["crit"] is None
        assert m["stratum"]["min"] == 0

    def test_empty_perfdata_returns_empty_list(self, dump: dict[str, Any]) -> None:
        svc = next(
            s for s in dump["services"] if s["description"] == "SERVICE_PUPPET-AGENT-CERT_STATUS"
        )
        assert parse_perfdata(svc["perf_data"]) == []
        assert parse_perfdata("") == []
        assert parse_perfdata(None) == []

    def test_value_only_and_empty_trailing_fields(self, dump: dict[str, Any]) -> None:
        svc = next(s for s in dump["services"] if s["description"] == "SERVICE_AD_STATUS")
        m = _by_label(svc["perf_data"])
        # 'DNS'=4 -> value only, no uom/warn/crit/min/max, no crash.
        assert m["DNS"]["value"] == 4
        assert m["DNS"]["uom"] is None
        assert m["DNS"]["warn"] is None and m["DNS"]["max"] is None
        # 'DNS rss'=80142336B;;;0 -> B uom, empty warn/crit, min present.
        assert m["DNS rss"]["value"] == 80142336
        assert m["DNS rss"]["uom"] == "B"
        assert m["DNS rss"]["min"] == 0

    def test_uom_variety(self, dump: dict[str, Any]) -> None:
        host_m = _by_label(dump["host"]["perf_data"])
        assert host_m["rta"]["uom"] == "ms"
        assert host_m["pl"]["uom"] == "%"
        ssl = next(
            s for s in dump["services"] if s["description"] == "SERVICE_SSL_CERTIFICATE-LDAPS"
        )
        m = _by_label(ssl["perf_data"])
        assert m["time"]["uom"] == "s"
        assert m["time"]["min"] == 0.0
        assert m["time"]["max"] == 10.0

    def test_empty_warn_present_crit(self, dump: dict[str, Any]) -> None:
        # 'DNS'=0;;1 -> value 0, empty warn, crit '1'.
        svc = next(
            s for s in dump["services"] if s["description"] == "SERVICE_ACTIVE-DIRECTORY_HEALTH"
        )
        m = _by_label(svc["perf_data"])
        assert m["DNS"]["value"] == 0
        assert m["DNS"]["warn"] is None
        assert m["DNS"]["crit"] == "1"
        assert m["DNS"]["breached"] is False

    def test_whole_fixture_parses_without_error(self, dump: dict[str, Any]) -> None:
        for svc in dump["services"]:
            assert isinstance(parse_perfdata(svc["perf_data"]), list)
        assert isinstance(parse_perfdata(dump["host"]["perf_data"]), list)


class TestProximityPercent:
    def test_monotonic_up_close(self) -> None:
        # value 88 vs warn 90 (0:90) -> (90-88)/90*100 ~= 2.22 %.
        assert proximity_percent(88, "90") == pytest.approx(2.222, abs=0.01)

    def test_breached_is_zero(self) -> None:
        assert proximity_percent(95, "90") == 0.0

    def test_none_when_no_range(self) -> None:
        assert proximity_percent(50, None) is None
        assert proximity_percent(None, "90") is None


# ---------------------------------------------------------------------------
# Tools — respx-mocked
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_perfdata_host(mocked_server) -> None:
    mcp, router = mocked_server
    router.get("https://thruk.test/r/hosts/ecrmut-ad-01").mock(
        return_value=ok([{"perf_data": "rta=0.963ms;3000;5000;0 pl=0%;80;100;0"}])
    )
    result = await mcp.call_tool("thruk_get_perfdata", {"host": "ecrmut-ad-01"})
    payload = json.loads(result[0].text)
    assert payload["host"] == "ecrmut-ad-01"
    assert payload["service"] is None
    labels = {m["label"] for m in payload["metrics"]}
    assert labels == {"rta", "pl"}


@pytest.mark.asyncio
async def test_get_perfdata_service(mocked_server) -> None:
    mcp, router = mocked_server
    router.get("https://thruk.test/r/services/ecrmut-ad-01/SYSTEM_DRIVE-C_USAGE").mock(
        return_value=ok([{"perf_data": "'C: used %'=77.4%;90;95;0;100"}])
    )
    result = await mcp.call_tool(
        "thruk_get_perfdata",
        {"host": "ecrmut-ad-01", "service": "SYSTEM_DRIVE-C_USAGE"},
    )
    payload = json.loads(result[0].text)
    assert payload["service"] == "SYSTEM_DRIVE-C_USAGE"
    assert len(payload["metrics"]) == 1
    m = payload["metrics"][0]
    assert m["label"] == "C: used %"
    assert m["value"] == 77.4
    assert m["breached"] is False


@pytest.mark.asyncio
async def test_get_perfdata_empty_returns_empty_metrics(mocked_server) -> None:
    mcp, router = mocked_server
    router.get("https://thruk.test/r/services/h1/SERVICE_PUPPET-AGENT-CERT_STATUS").mock(
        return_value=ok([{"perf_data": ""}])
    )
    result = await mcp.call_tool(
        "thruk_get_perfdata",
        {"host": "h1", "service": "SERVICE_PUPPET-AGENT-CERT_STATUS"},
    )
    payload = json.loads(result[0].text)
    assert payload["metrics"] == []


@pytest.mark.asyncio
async def test_get_perfdata_not_found(mocked_server) -> None:
    mcp, router = mocked_server
    router.get("https://thruk.test/r/hosts/nope").mock(return_value=ok([]))
    result = await mcp.call_tool("thruk_get_perfdata", {"host": "nope"})
    payload = json.loads(result[0].text)
    assert "error" in payload


@pytest.mark.asyncio
async def test_perfdata_snapshot(mocked_server) -> None:
    mcp, router = mocked_server
    route = router.get("https://thruk.test/r/services").mock(
        return_value=ok(
            [
                {
                    "host_name": "h1",
                    "description": "SYSTEM_DRIVE-C_USAGE",
                    "perf_data": "'C: used %'=77.4%;90;95;0;100",
                },
                {"host_name": "h2", "description": "EMPTY", "perf_data": ""},
            ]
        )
    )
    result = await mcp.call_tool(
        "thruk_perfdata_snapshot",
        {"filter": {"type": "leaf", "field": "hostgroup", "op": "eq", "value": "PROD"}},
    )
    payload = json.loads(result[0].text)
    assert route.call_count == 1
    assert payload["total"] == 2
    assert payload["results"][0]["host"] == "h1"
    assert payload["results"][0]["metrics"][0]["label"] == "C: used %"
    assert payload["results"][1]["metrics"] == []


@pytest.mark.asyncio
async def test_perfdata_snapshot_invalid_filter(mocked_server) -> None:
    mcp, _ = mocked_server
    result = await mcp.call_tool(
        "thruk_perfdata_snapshot",
        {"filter": {"type": "leaf", "field": "bogus", "op": "eq", "value": "x"}},
    )
    payload = json.loads(result[0].text)
    assert "error" in payload


@pytest.mark.asyncio
async def test_perfdata_near_threshold(mocked_server) -> None:
    mcp, router = mocked_server
    router.get("https://thruk.test/r/services").mock(
        return_value=ok(
            [
                # near: 88 vs warn 90 -> ~2.22 %
                {"host_name": "h1", "description": "DISK", "perf_data": "'used %'=88%;90;95;0;100"},
                # far: 65.8 vs warn 98 -> ~32.9 %
                {"host_name": "h1", "description": "MEM", "perf_data": "'phys %'=65.8%;98;;0;100"},
                # breached: 99.5 > warn 98 -> headroom 0
                {"host_name": "h2", "description": "PAGE", "perf_data": "'pg %'=99.5%;98;99;0;100"},
            ]
        )
    )
    result = await mcp.call_tool("thruk_perfdata_near_threshold", {"within_percent": 10})
    payload = json.loads(result[0].text)
    assert payload["within_percent"] == 10
    assert payload["total"] == 2
    # Sorted by headroom ascending: breached (0) first, then the 2.22 % one.
    assert payload["results"][0]["service"] == "PAGE"
    assert payload["results"][0]["headroom_percent"] == 0.0
    assert payload["results"][0]["breached"] is True
    assert payload["results"][1]["service"] == "DISK"
    assert payload["results"][1]["headroom_percent"] == pytest.approx(2.22, abs=0.01)
    assert payload["results"][1]["breached"] is False
