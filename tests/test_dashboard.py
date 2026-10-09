from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from trad.dashboard import DashboardHTTPServer, DashboardService
from trad.futures_paper import FuturesPaperEngine, FuturesPersistenceError, FuturesReconciliationError


NOW = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)


def market_payload(domain: str = "both") -> dict[str, object]:
    timestamp = NOW - timedelta(minutes=1)
    return {
        "domain": domain,
        "symbol": "BTC/USDT",
        "timeframe_seconds": 60,
        "timestamp": timestamp.isoformat(),
        "close_time": NOW.isoformat(),
        "received_at": NOW.isoformat(),
        "open": "100.00",
        "high": "101.00",
        "low": "99.00",
        "close": "100.00",
        "volume": "1",
        "checked_at": NOW.isoformat(),
    }


class DashboardHTTPTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = DashboardService(futures_database_path=":memory:", clock=lambda: NOW)
        self.server = DashboardHTTPServer(("127.0.0.1", 0), self.service)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, method: str, path: str, payload: dict[str, object] | None = None) -> tuple[int, dict[str, object] | str]:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"} if data is not None else {},
        )
        try:
            with urlopen(request, timeout=3) as response:
                body = response.read().decode("utf-8")
                return response.status, json.loads(body) if response.headers.get_content_type() == "application/json" else body
        except HTTPError as error:
            body = error.read().decode("utf-8")
            return error.code, json.loads(body)

    def test_dashboard_startup_static_assets_and_state_response(self) -> None:
        status, page = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("Paper-trading control room", page)
        self.assertIn("/static/app.js", page)
        status, script = self.request("GET", "/static/app.js")
        self.assertEqual(status, 200)
        self.assertIn("/api/state", script)
        self.assertIn("Unavailable", script)
        self.assertIn("No fills yet.", script)
        self.assertIn("showToast(error.message", script)
        status, styles = self.request("GET", "/static/styles.css")
        self.assertEqual(status, 200)
        self.assertIn("--cyan", styles)
        status, response = self.request("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertTrue(response["ok"])
        data = response["data"]
        self.assertEqual(data["mode"], "paper")
        self.assertTrue(data["simulation_only"])
        spot_usdt = next(item for item in data["spot"]["wallet"] if item["asset"] == "USDT")
        self.assertEqual(spot_usdt["total"], "1000.00")
        self.assertEqual(data["futures"]["wallet"][0]["total"], "1000.00")
        self.assertFalse(data["futures"]["persistence"]["durable"])

    def test_invalid_requests_are_json_errors_without_tracebacks(self) -> None:
        status, response = self.request("POST", "/api/spot/orders", {})
        self.assertEqual(status, 400)
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "invalid_request")
        self.assertNotIn("Traceback", json.dumps(response))
        status, response = self.request("GET", "/api/not-a-route")
        self.assertEqual(status, 404)
        self.assertEqual(response["error"]["code"], "not_found")

    def test_persistence_failure_is_a_safe_service_unavailable_response(self) -> None:
        def fail(_payload: dict[str, object]) -> object:
            raise FuturesPersistenceError("database path /private/secret.sqlite is unavailable")

        self.service.submit_futures_order = fail  # type: ignore[method-assign]
        status, response = self.request("POST", "/api/futures/orders", {
            "action": "open", "side": "long", "quantity": "1", "price": "100", "client_order_id": "persistence-failure"
        })
        self.assertEqual(status, 503)
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "persistence_unavailable")
        self.assertNotIn("secret.sqlite", json.dumps(response))
        self.assertNotIn("Traceback", json.dumps(response))

    def test_reconciliation_failure_is_a_safe_service_unavailable_response(self) -> None:
        def fail() -> object:
            raise FuturesReconciliationError("inconsistent durable state")

        self.service.futures.reconcile = fail  # type: ignore[method-assign]
        status, response = self.request("GET", "/api/state")
        self.assertEqual(status, 503)
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "persistence_unavailable")
        self.assertNotIn("Traceback", json.dumps(response))

    def test_market_data_gate_and_spot_futures_operations_are_integrated(self) -> None:
        status, response = self.request("POST", "/api/spot/orders", {
            "side": "buy", "quantity": "0.01", "price": "100", "client_order_id": "spot-1"
        })
        self.assertEqual(status, 422)
        self.assertEqual(response["error"]["code"], "order_rejected")
        self.assertIn("market-data", response["error"]["message"])

        status, response = self.request("POST", "/api/market-data", market_payload())
        self.assertEqual(status, 200)
        self.assertEqual(response["data"]["results"]["spot"]["status"], "safe")
        self.assertEqual(response["data"]["results"]["futures"]["status"], "safe")

        spot_order_payload = {
            "side": "buy", "quantity": "0.01", "price": "100", "client_order_id": "spot-after-data"
        }
        status, first = self.request("POST", "/api/spot/orders", spot_order_payload)
        self.assertEqual(status, 200)
        status, repeated = self.request("POST", "/api/spot/orders", spot_order_payload)
        self.assertEqual(status, 200)
        self.assertEqual(first["data"]["order_id"], repeated["data"]["order_id"])
        spot_id = first["data"]["order_id"]
        status, fill = self.request("POST", f"/api/spot/orders/{spot_id}/fill", {
            "quantity": "0.01", "price": "100", "fill_id": "spot-fill-1"
        })
        self.assertEqual(status, 200)
        self.assertEqual(fill["data"]["fill_id"], "spot-fill-1")

        futures_order_payload = {
            "action": "open", "side": "long", "quantity": "1", "price": "100",
            "leverage": "1", "client_order_id": "futures-1"
        }
        status, futures_order = self.request("POST", "/api/futures/orders", futures_order_payload)
        self.assertEqual(status, 200)
        futures_id = futures_order["data"]["order_id"]
        status, futures_fill = self.request("POST", f"/api/futures/orders/{futures_id}/fill", {
            "quantity": "1", "price": "100", "fill_id": "futures-fill-1"
        })
        self.assertEqual(status, 200)
        self.assertEqual(futures_fill["data"]["fee"], "0.10")
        status, mark = self.request("POST", "/api/futures/mark", {"price": "105"})
        self.assertEqual(status, 200)
        self.assertEqual(mark["data"]["position"]["mark_price"], "105.00")
        status, funding = self.request("POST", "/api/futures/funding", {"rate": "0.01", "payment_id": "fund-1"})
        self.assertEqual(status, 200)
        self.assertEqual(funding["data"]["amount"], "-1.05")

        status, state_response = self.request("GET", "/api/state")
        self.assertEqual(status, 200)
        state = state_response["data"]
        spot_usdt = next(item for item in state["spot"]["wallet"] if item["asset"] == "USDT")
        self.assertEqual(spot_usdt["total"], "999.00")
        self.assertEqual(state["futures"]["wallet"][0]["total"], "998.85")
        self.assertEqual(state["futures"]["positions"][0]["unrealized_pnl"], "5.00")
        self.assertEqual(state["futures"]["positions"][0]["notional"], "105.00")
        self.assertEqual(state["futures"]["funding"][0]["payment_id"], "fund-1")
        self.assertTrue(state["spot"]["reconciliation"]["is_consistent"])
        self.assertTrue(state["futures"]["reconciliation"]["is_consistent"])
        self.assertEqual(state["futures"]["valuation"]["account_equity"], "1003.85")

    def test_rejected_futures_action_when_safety_is_reset(self) -> None:
        status, response = self.request("POST", "/api/market-data", market_payload())
        self.assertEqual(status, 200)
        status, response = self.request("POST", "/api/market-data/reset", {"confirm": True})
        self.assertEqual(status, 200)
        self.assertEqual(response["data"]["health"]["futures"]["status"], "no_data")
        status, response = self.request("POST", "/api/futures/orders", {
            "action": "open", "side": "short", "quantity": "1", "price": "100", "client_order_id": "futures-reset"
        })
        self.assertEqual(status, 422)
        self.assertEqual(response["error"]["code"], "order_rejected")


class DashboardPersistenceTests(unittest.TestCase):
    def test_futures_recovery_is_visible_without_claiming_spot_durability(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "dashboard.sqlite3"
            service = DashboardService(futures_database_path=path, clock=lambda: NOW)
            service.record_market_data(market_payload())
            order = service.submit_futures_order({
                "action": "open", "side": "long", "quantity": "1", "price": "100",
                "client_order_id": "durable-order",
            })
            service.fill_futures_order(order.order_id, {"quantity": "1", "price": "100", "fill_id": "durable-fill"})
            service.close()
            recovered = DashboardService(futures_database_path=path, clock=lambda: NOW)
            try:
                state = recovered.snapshot()
                self.assertTrue(state["futures"]["persistence"]["durable"])
                self.assertEqual(state["futures"]["positions"][0]["quantity"], "1.00000000")
                self.assertEqual(state["futures"]["market_data"]["status"], "no_data")
                self.assertFalse(state["spot"]["persistence"]["durable"])
            finally:
                recovered.close()

    def test_custom_failure_is_not_returned_as_a_stack_trace(self) -> None:
        failed = {"done": False}

        def failure_hook(operation: str) -> None:
            if operation == "accept_order" and not failed["done"]:
                failed["done"] = True
                raise RuntimeError("injected transaction failure")

        futures = FuturesPaperEngine(clock=lambda: NOW, failure_hook=failure_hook)
        service = DashboardService(futures=futures, clock=lambda: NOW)
        try:
            service.record_market_data(market_payload())
            with self.assertRaises(RuntimeError):
                service.submit_futures_order({
                    "action": "open", "side": "long", "quantity": "1", "price": "100",
                    "client_order_id": "failure-order",
                })
            self.assertEqual(service.futures.orders(), ())
        finally:
            service.close()


if __name__ == "__main__":
    unittest.main()
