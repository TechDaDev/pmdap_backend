"""Railway server monitor tests.

Railway and Redis are always mocked - nothing in this suite makes a real
upstream call or requires a live Redis. Coverage:

  * collector: OK / RATE_LIMITED (backoff) / CONFIG_ERROR / early-return while
    in a backoff window / single-chain guard / disabled
  * buffer: replace + read (sorted, deduped, capped) and collector status
  * data endpoint: authz, DISABLED, OK shape, no token leakage, Redis failure
"""

import json
import time

import pytest
from django.contrib.auth.models import Permission
from django.urls import reverse

from accounts.models import User
from opsconsole import buffer
from opsconsole import collector
from opsconsole.railway_client import RailwayMetricsError
from tests.factories import UserFactory

pytestmark = pytest.mark.django_db


class FakeRedis:
    """In-memory stand-in for the redis methods the buffer uses."""

    def __init__(self):
        self.data = {}  # key -> value (str or list)
        self.expiries = {}

    def _get(self, key):
        return self.data.get(key)

    def get(self, key):
        return self._get(key)

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.data:
            return False
        self.data[key] = value
        if ex:
            self.expiries[key] = time.time() + ex
        return True

    def delete(self, key):
        self.data.pop(key, None)
        self.expiries.pop(key, None)

    def rpush(self, key, *values):
        if key not in self.data or not isinstance(self.data[key], list):
            self.data[key] = []
        self.data[key].extend(values)

    def lrange(self, key, start, end):
        items = self.data.get(key) or []
        return items[start : end + 1 if end >= 0 else None]

    def expire(self, key, ttl):
        self.expiries[key] = time.time() + ttl
        return True

    def hset(self, key, mapping=None):
        if key not in self.data or not isinstance(self.data[key], dict):
            self.data[key] = {}
        for k, v in (mapping or {}).items():
            self.data[key][k] = v

    def hgetall(self, key):
        value = self.data.get(key)
        return value if isinstance(value, dict) else {}


@pytest.fixture
def fake_redis(monkeypatch):
    redis = FakeRedis()
    monkeypatch.setattr(buffer, "redis_client", lambda: redis)
    return redis


def make_agent(*, staff=True, perm=False):
    user = UserFactory(email="agent-monitor@example.com", role=User.Role.IDENTITY_VERIFICATION_AGENT)
    user.is_staff = staff
    user.save(update_fields=("is_staff",))
    if perm:
        p = Permission.objects.get(codename="view_server_monitor", content_type__app_label="opsconsole")
        user.user_permissions.add(p)
    return user


def login(client, user):
    client.force_login(user)
    return client


class FakeClient:
    enabled = True

    def __init__(self, services=None, payload=None, error=None):
        self.services = services or [{"id": "srv-1", "name": "pmdap_backend"}]
        self.payload = payload
        self.error = error
        self.discover_calls = 0
        self.fetch_calls = 0

    def discover_services(self):
        self.discover_calls += 1
        return self.services

    def fetch_all_metrics(self, services, measurements, start, end, sample):
        self.fetch_calls += 1
        if self.error:
            raise self.error
        if self.payload is not None:
            return self.payload
        now = int(time.time())
        return {
            s["name"]: {
                "CPU_USAGE": [(now - 10, 0.5), (now, 0.7)],
                "MEMORY_USAGE_GB": [(now - 10, 0.2), (now, 0.25)],
                "DISK_USAGE_GB": [(now - 10, 1.0), (now, 1.0)],
                "NETWORK_RX_GB": [(now - 10, 0.001), (now, 0.002)],
                "NETWORK_TX_GB": [(now - 10, 0.004), (now, 0.005)],
            }
            for s in services
        }


@pytest.fixture
def run_collector(monkeypatch):
    """Patch the client factory + task scheduling; returns a helper."""

    def _run(fake_client=None, enabled="true", sample=None):
        monkeypatch.setenv("RAILWAY_METRICS_ENABLED", enabled)
        if sample:
            monkeypatch.setenv("RAILWAY_METRICS_SAMPLE_SECONDS", sample)
        client = fake_client or FakeClient()
        monkeypatch.setattr(collector, "RailwayMetricsClient", lambda: client)
        scheduled = []
        monkeypatch.setattr(
            collector.collect_railway_metrics, "apply_async",
            lambda countdown=None, **kw: scheduled.append(countdown),
        )
        collector.collect_railway_metrics()
        return client, scheduled

    return _run


class TestCollector:
    def test_ok_stores_series_and_status(self, fake_redis, run_collector):
        client, scheduled = run_collector()
        status = buffer.get_collector_status()
        assert status["status"] == "OK"
        assert status["services"] == "pmdap_backend"
        assert client.discover_calls == 1
        series = buffer.read_series("pmdap_backend", "CPU_USAGE")
        assert len(series) == 2
        assert series[0][1] == 0.5 and series[1][1] == 0.7
        assert scheduled and scheduled[0] == 5  # reschedules at sample interval

    def test_disabled_returns_immediately(self, fake_redis, run_collector, monkeypatch):
        called = []
        monkeypatch.setattr(collector.collect_railway_metrics, "apply_async",
                            lambda countdown=None, **kw: called.append(countdown))
        collector.collect_railway_metrics()  # enabled env unset
        assert called == []

    def test_rate_limited_sets_backoff(self, fake_redis, run_collector):
        client, scheduled = run_collector(
            FakeClient(error=RailwayMetricsError("RATE_LIMITED"))
        )
        status = buffer.get_collector_status()
        assert status["status"] == "RATE_LIMITED"
        assert float(status["next_allowed_at"]) > time.time()
        # Backoff doubles the interval.
        assert scheduled[0] == 10

    def test_early_return_within_backoff_window(self, fake_redis, run_collector):
        now = time.time()
        buffer.set_collector_status(
            status="RATE_LIMITED", next_allowed_at=now + 60, sample_seconds=5
        )
        client, scheduled = run_collector(FakeClient())
        # Must not hit upstream while cooling down.
        assert client.fetch_calls == 0
        assert client.discover_calls == 0
        assert scheduled[0] == 60

    def test_config_error_status(self, fake_redis, run_collector):
        client, _ = run_collector(
            FakeClient(error=RailwayMetricsError("CONFIG_ERROR", "bad token"))
        )
        status = buffer.get_collector_status()
        assert status["status"] == "CONFIG_ERROR"

    def test_redis_failure_does_not_crash_task(self, run_collector, monkeypatch):
        def boom(*args, **kwargs):
            raise RuntimeError("redis down")

        monkeypatch.setattr(buffer, "redis_client", boom)
        client, scheduled = run_collector()
        # Task still reschedules; no exception escaped.
        assert scheduled

    def test_single_chain_guard(self, fake_redis):
        assert buffer.acquire_chain(60) is True
        assert buffer.acquire_chain(60) is False


class TestBuffer:
    def test_replace_series_sorted_deduped(self, fake_redis):
        count = buffer.replace_series(
            "svc", "CPU_USAGE",
            [(2, 2.0), (1, 1.0), (2, 9.0), (3, 3.0)],
            1800,
        )
        assert count == 3
        series = buffer.read_series("svc", "CPU_USAGE")
        assert [p[0] for p in series] == [1, 2, 3]
        # Duplicate timestamps keep the LAST value written (latest sample wins).
        assert [p[1] for p in series] == [1.0, 9.0, 3.0]

    def test_read_series_caps_points(self, fake_redis):
        points = [(i, float(i)) for i in range(2000)]
        buffer.replace_series("svc", "MEMORY_USAGE_GB", points, 1800)
        series = buffer.read_series("svc", "MEMORY_USAGE_GB", max_points=100)
        assert len(series) == 100


class TestRailwayClientErrorMapping:
    """Error-mapping unit tests for the urllib-based GraphQL client."""

    def _make_client(self):
        from opsconsole.railway_client import RailwayMetricsClient

        return RailwayMetricsClient(
            token="t", project_id="p", environment_id="e"
        )

    def _patch_urlopen(self, monkeypatch, exc=None, status=200, body="{}"):
        import urllib.error

        captured = {}

        class FakeResponse:
            def __init__(self, status):
                self.status = status

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return body.encode()

        def fake_urlopen(request, timeout=20):
            captured["headers"] = dict(request.headers)
            if exc:
                raise exc
            return FakeResponse(status)

        monkeypatch.setattr(
            "opsconsole.railway_client.urllib.request.urlopen", fake_urlopen
        )
        return captured

    def test_missing_token_config_error(self, monkeypatch):
        from opsconsole.railway_client import RailwayMetricsClient, RailwayMetricsError

        client = RailwayMetricsClient(token="")
        with pytest.raises(RailwayMetricsError) as exc:
            client._post("q", {})
        assert exc.value.code == "CONFIG_ERROR"

    def test_401_config_error(self, monkeypatch):
        import urllib.error

        from opsconsole.railway_client import RailwayMetricsError

        self._patch_urlopen(
            monkeypatch, exc=urllib.error.HTTPError("u", 401, "x", None, None)
        )
        with pytest.raises(RailwayMetricsError) as exc:
            self._make_client()._post("q", {})
        assert exc.value.code == "CONFIG_ERROR"

    def test_429_rate_limited(self, monkeypatch):
        import urllib.error

        from opsconsole.railway_client import RailwayMetricsError

        self._patch_urlopen(
            monkeypatch, exc=urllib.error.HTTPError("u", 429, "x", None, None)
        )
        with pytest.raises(RailwayMetricsError) as exc:
            self._make_client()._post("q", {})
        assert exc.value.code == "RATE_LIMITED"

    def test_500_upstream_error(self, monkeypatch):
        from opsconsole.railway_client import RailwayMetricsError

        self._patch_urlopen(monkeypatch, status=503, body="oops")
        with pytest.raises(RailwayMetricsError) as exc:
            self._make_client()._post("q", {})
        assert exc.value.code == "UPSTREAM_ERROR"

    def test_graphql_auth_error_config_error(self, monkeypatch):
        from opsconsole.railway_client import RailwayMetricsError

        self._patch_urlopen(
            monkeypatch,
            body='{"errors":[{"message":"unauthorized"}]}',
        )
        with pytest.raises(RailwayMetricsError) as exc:
            self._make_client()._post("q", {})
        assert exc.value.code == "CONFIG_ERROR"

    def test_network_error_upstream(self, monkeypatch):
        import urllib.error

        from opsconsole.railway_client import RailwayMetricsError

        self._patch_urlopen(monkeypatch, exc=urllib.error.URLError("boom"))
        with pytest.raises(RailwayMetricsError) as exc:
            self._make_client()._post("q", {})
        assert exc.value.code == "UPSTREAM_ERROR"

    def test_sends_browser_user_agent(self, monkeypatch):
        captured = self._patch_urlopen(monkeypatch)
        self._make_client()._post("q", {})
        headers = {k.lower(): v for k, v in captured["headers"].items()}
        ua = headers.get("user-agent", "")
        # Cloudflare blocks the urllib default UA (403 error 1010).
        assert "Python-urllib" not in ua
        assert "Mozilla/5.0" in ua

    def test_project_token_type_header(self, monkeypatch):
        from opsconsole.railway_client import RailwayMetricsClient

        captured = self._patch_urlopen(monkeypatch)
        client = RailwayMetricsClient(token="pat", token_type="project")
        client._post("q", {})
        headers = {k.lower(): v for k, v in captured["headers"].items()}
        assert headers.get("project-access-token") == "pat"
        assert "authorization" not in headers


class TestDataEndpoint:
    def test_anonymous_redirects(self, client):
        response = client.get(reverse("admin:ops_server_monitor_data"))
        assert response.status_code == 302

    def test_staff_without_perm_forbidden(self, client):
        user = make_agent(staff=True, perm=False)
        login(client, user)
        assert client.get(reverse("admin:ops_server_monitor_data")).status_code == 403

    def test_staff_with_perm_allowed(self, client, monkeypatch):
        user = make_agent(staff=True, perm=True)
        login(client, user)
        monkeypatch.setattr("opsconsole.monitor_views.RailwayMetricsClient", lambda: FakeClient())
        monkeypatch.setattr(buffer, "get_collector_status",
                            lambda: {"status": "OK", "services": "pmdap_backend",
                                     "updated_at": str(int(time.time())),
                                     "last_ok_at": str(int(time.time()))})
        monkeypatch.setattr(buffer, "read_series",
                            lambda svc, metric: [[int(time.time()), 1.0]])
        response = client.get(reverse("admin:ops_server_monitor_data"))
        assert response.status_code == 200
        assert "no-store" in response["Cache-Control"]
        payload = response.json()
        assert payload["status"] == "OK"
        assert "pmdap_backend" in payload["services"]
        assert payload["services"]["pmdap_backend"]["CPU_USAGE"]

    def test_disabled_status(self, client, monkeypatch):
        user = UserFactory(email="root-mon@example.com")
        user.is_staff = True
        user.is_superuser = True
        user.save(update_fields=("is_staff", "is_superuser"))
        login(client, user)
        fake = FakeClient()
        fake.enabled = False
        monkeypatch.setattr("opsconsole.monitor_views.RailwayMetricsClient", lambda: fake)
        response = client.get(reverse("admin:ops_server_monitor_data"))
        assert response.status_code == 200
        assert response.json()["status"] == "DISABLED"

    def test_monitor_page_disabled_renders_notice(self, client, monkeypatch):
        user = UserFactory(email="root-mon4@example.com")
        user.is_staff = True
        user.is_superuser = True
        user.save(update_fields=("is_staff", "is_superuser"))
        login(client, user)
        fake = FakeClient()
        fake.enabled = False
        monkeypatch.setattr("opsconsole.monitor_views.RailwayMetricsClient", lambda: fake)
        response = client.get(reverse("admin:ops_server_monitor"))
        assert response.status_code == 200
        assert b"Not configured" in response.content

    def test_monitor_page_enabled_renders_dashboard(self, client, monkeypatch):
        user = UserFactory(email="root-mon5@example.com")
        user.is_staff = True
        user.is_superuser = True
        user.save(update_fields=("is_staff", "is_superuser"))
        login(client, user)
        monkeypatch.setattr("opsconsole.monitor_views.RailwayMetricsClient", lambda: FakeClient())
        response = client.get(reverse("admin:ops_server_monitor"))
        assert response.status_code == 200
        assert b"Railway server monitor" in response.content
        assert b"monitor.js" in response.content

    def test_token_never_in_response(self, client, monkeypatch):
        user = UserFactory(email="root-mon2@example.com")
        user.is_staff = True
        user.is_superuser = True
        user.save(update_fields=("is_staff", "is_superuser"))
        login(client, user)
        monkeypatch.setenv("RAILWAY_METRICS_TOKEN", "SUPER-SECRET-TOKEN-123")
        fake = FakeClient()
        monkeypatch.setattr("opsconsole.monitor_views.RailwayMetricsClient", lambda: fake)
        monkeypatch.setattr(buffer, "get_collector_status",
                            lambda: {"status": "OK", "services": "pmdap_backend"})
        monkeypatch.setattr(buffer, "read_series", lambda svc, metric: [])
        response = client.get(reverse("admin:ops_server_monitor_data"))
        assert b"SUPER-SECRET-TOKEN-123" not in response.content

    def test_redis_failure_returns_503(self, client, monkeypatch):
        user = UserFactory(email="root-mon3@example.com")
        user.is_staff = True
        user.is_superuser = True
        user.save(update_fields=("is_staff", "is_superuser"))
        login(client, user)
        monkeypatch.setattr("opsconsole.monitor_views.RailwayMetricsClient", lambda: FakeClient())

        def boom(*args, **kwargs):
            raise RuntimeError("redis down")

        monkeypatch.setattr(buffer, "get_collector_status", boom)
        response = client.get(reverse("admin:ops_server_monitor_data"))
        assert response.status_code == 503
        assert response.json()["status"] == "REDIS_UNAVAILABLE"
