import email.utils
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ticket_app import configuration
from ticket_app.clock import ServerClock
from ticket_app.helpers import _is_terminal_order_failure, _parse_js_object
from ticket_app.runner import TicketRunner


class FakeResponse:
    def __init__(self, date_header):
        self.headers = {"Date": date_header}


class FakeSession:
    def __init__(self, date_headers):
        self.date_headers = list(date_headers)

    def head(self, url, timeout):
        return FakeResponse(self.date_headers.pop(0))


class OptimizationTests(unittest.TestCase):
    def test_server_clock_uses_lowest_rtt_sample(self):
        date_header = email.utils.formatdate(1000, usegmt=True)
        cfg = SimpleNamespace(
            request_timeout_seconds=5,
            time_sync_samples=3,
            time_sync_max_rtt_seconds=1.0,
        )
        clock = ServerClock(FakeSession([date_header, date_header, date_header]), cfg)

        wall_times = [1000.0, 1000.4, 1001.0, 1001.1, 1002.0, 1002.2]
        perf_times = [10.0, 10.4, 11.0, 11.1, 12.0, 12.2]
        with patch("ticket_app.clock.time.time", side_effect=wall_times), patch(
            "ticket_app.clock.time.perf_counter", side_effect=perf_times
        ), patch("ticket_app.clock.time.sleep"):
            clock.sync()

        self.assertAlmostEqual(clock.offset_seconds, -1.05, places=6)

    def test_hot_window_interval_switches_around_start_time(self):
        cfg = SimpleNamespace(
            pre_query_seconds=3.0,
            hot_query_interval_seconds=0.25,
            hot_window_seconds=10.0,
            query_interval_seconds=1.0,
        )
        runner = object.__new__(TicketRunner)
        runner.cfg = cfg
        runner.clock = SimpleNamespace(now=lambda: configuration.datetime(2026, 5, 1, 9, 59, 58))
        target = configuration.datetime(2026, 5, 1, 10, 0, 0)

        self.assertEqual(runner._current_query_interval(target), 0.25)

        runner.clock = SimpleNamespace(now=lambda: configuration.datetime(2026, 5, 1, 10, 0, 11))
        self.assertEqual(runner._current_query_interval(target), 1.0)

    def test_terminal_failure_does_not_treat_negative_wait_as_failure(self):
        self.assertFalse(_is_terminal_order_failure(""))
        self.assertFalse(_is_terminal_order_failure("排队中，预计等待 -100 秒"))
        self.assertTrue(_is_terminal_order_failure("没有足够的票!"))

    def test_parse_js_object_handles_hex_escape(self):
        """JS 对象的 \\xHH 十六进制转义（如 \\xA5 表示 ¥）必须能正确解析。"""
        raw = "{'name':'\\u5F20\\u4E09','price':'\\xA578.0\\u5143','note':null,'arr':[{'id':'1','value':'\\u786C\\u5EA7'}]}"
        parsed = _parse_js_object(raw)
        self.assertEqual(parsed["name"], "张三")
        self.assertEqual(parsed["price"], "¥78.0元")
        self.assertIsNone(parsed["note"])
        self.assertEqual(parsed["arr"][0]["value"], "硬座")

    def test_book_ticket_handles_network_timeout(self):
        """提交订单时网络超时（ReadTimeout）应返回 False 而不是抛出异常。"""
        import requests

        cfg = SimpleNamespace(
            order_wait_attempts=3,
            order_wait_interval_seconds=0.01,
            ticket_type="1",
            perf_log=False,
        )
        runner = object.__new__(TicketRunner)
        runner.cfg = cfg
        runner.client = SimpleNamespace(
            submit_order_request=lambda ticket: (_ for _ in ()).throw(requests.exceptions.ReadTimeout("read timeout"))
        )
        candidate = {"ticket": {"station_train_code": "G1"}, "seat_label": "二等座", "seat_type": "O"}
        prepared = {"O": object()}
        self.assertFalse(runner._book_ticket(candidate, prepared))

    def test_candidate_order_respects_train_and_seat_priority(self):
        cfg = SimpleNamespace(
            preferred_trains=["G2", "G1"],
            only_preferred_trains=True,
        )
        runner = object.__new__(TicketRunner)
        runner.cfg = cfg
        runner.preferred_order = {"G2": 0, "G1": 1}
        runner.seat_sequence = [("二等座", configuration.SEAT_SPECS["二等座"]), ("一等座", configuration.SEAT_SPECS["一等座"])]
        tickets = [
            {
                "station_train_code": "G1",
                "can_buy": True,
                "seats": {"edz": "有", "ydz": "有"},
            },
            {
                "station_train_code": "G2",
                "can_buy": True,
                "seats": {"edz": "--", "ydz": "1"},
            },
            {
                "station_train_code": "G3",
                "can_buy": True,
                "seats": {"edz": "有", "ydz": "有"},
            },
        ]

        candidates = runner._find_candidates(tickets)

        self.assertEqual([item["ticket"]["station_train_code"] for item in candidates], ["G2", "G1", "G1"])
        self.assertEqual([item["seat_label"] for item in candidates], ["一等座", "二等座", "一等座"])

    def test_suggest_stations_offers_similar_names(self):
        runner = object.__new__(TicketRunner)
        runner.stations = SimpleNamespace(
            stations={
                "北京西": "BXP",
                "北京南": "VNP",
                "北京": "BJP",
                "北京朝阳": "IFP",
                "郑州东": "ZAF",
            }
        )
        suggestions = runner._suggest_stations("北京")
        self.assertIn("北京", suggestions)
        self.assertIn("北京西", suggestions)
        self.assertNotIn("郑州东", suggestions)

    def test_suggest_stations_returns_empty_when_no_match(self):
        runner = object.__new__(TicketRunner)
        runner.stations = SimpleNamespace(stations={"北京西": "BXP"})
        self.assertEqual(runner._suggest_stations("不存在的站"), [])

    def test_should_stop_when_started_after_stop_time(self):
        """当天启动时已过 STOP_AT，应立即停止，而不是顺延到第二天。"""
        cfg = SimpleNamespace(stop_at="15:05:00")
        runner = object.__new__(TicketRunner)
        runner.cfg = cfg
        runner.clock = SimpleNamespace(now=lambda: configuration.datetime(2026, 5, 1, 15, 10, 0))
        self.assertTrue(runner._should_stop())

    def test_should_not_stop_before_stop_time(self):
        cfg = SimpleNamespace(stop_at="15:05:00")
        runner = object.__new__(TicketRunner)
        runner.cfg = cfg
        runner.clock = SimpleNamespace(now=lambda: configuration.datetime(2026, 5, 1, 15, 4, 30))
        self.assertFalse(runner._should_stop())

    def test_should_not_stop_when_stop_at_empty(self):
        cfg = SimpleNamespace(stop_at="")
        runner = object.__new__(TicketRunner)
        runner.cfg = cfg
        runner.clock = SimpleNamespace(now=lambda: configuration.datetime(2026, 5, 1, 20, 0, 0))
        self.assertFalse(runner._should_stop())


if __name__ == "__main__":
    unittest.main()
