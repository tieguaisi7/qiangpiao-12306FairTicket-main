import logging
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import requests

from .client import RailwayClient
from .clock import ServerClock
from .configuration import AppConfig, AppError, PreparedPassengerSet, SEAT_SPECS, _elapsed_ms, _perf_log, _resolve_schedule_time
from .helpers import _build_passenger_strings, _is_terminal_order_failure, _resolve_submit_seat_code, _stock_available
from .stations import StationStore


class TicketRunner:
    def __init__(self, cfg: AppConfig) -> None:
        self.cfg = cfg
        self.client = RailwayClient(cfg)
        self.clock = ServerClock(self.client.session, cfg)
        self.stations = StationStore(self.client.session, cfg)
        self.preferred_order = {train: index for index, train in enumerate(cfg.preferred_trains)}
        self.seat_sequence = [(seat_label, SEAT_SPECS[seat_label]) for seat_label in cfg.seat_types]

    def run(self) -> int:
        self.clock.sync()
        self.stations.load()
        from_code = self.stations.code(self.cfg.from_station)
        to_code = self.stations.code(self.cfg.to_station)
        self.client.ensure_login()
        selected_passengers = self._select_passengers() if self.cfg.auto_submit else []
        prepared_passengers = self._prepare_passengers_by_seat_code(selected_passengers) if self.cfg.auto_submit else {}
        target_start = self._resolve_target_start()
        self._wait_for_query_start(target_start)
        logging.info(
            "任务启动: %s -> %s, 日期 %s, 座席 %s",
            self.cfg.from_station,
            self.cfg.to_station,
            self.cfg.train_date,
            ",".join(self.cfg.seat_types),
        )
        first_query_perf = time.perf_counter()
        for attempt in range(1, self.cfg.max_retries + 1):
            if self._should_stop():
                logging.info("已到 STOP_AT，任务结束")
                return 1
            logging.info("第 %s/%s 次查询余票...", attempt, self.cfg.max_retries)
            query_start = time.perf_counter()
            if attempt == 1:
                _perf_log(self.cfg, "等待结束到首个查票请求准备耗时 %.1fms", _elapsed_ms(first_query_perf))
            tickets = self.client.query_tickets(from_code, to_code)
            find_start = time.perf_counter()
            candidates = self._find_candidates(tickets)
            _perf_log(
                self.cfg,
                "本轮查票总耗时 %.1fms，筛选 %.1fms，候选 %s 个",
                _elapsed_ms(query_start),
                _elapsed_ms(find_start),
                len(candidates),
            )
            if not candidates:
                time.sleep(self._current_query_interval(target_start))
                continue
            for candidate in candidates:
                ticket = candidate["ticket"]
                candidate["found_perf"] = time.perf_counter()
                logging.info(
                    "发现票源: %s %s-%s %s %s 余票:%s",
                    ticket["station_train_code"],
                    ticket["start_time"],
                    ticket["arrive_time"],
                    candidate["seat_label"],
                    ticket["duration"],
                    candidate["stock"],
                )
                if not self.cfg.auto_submit:
                    continue
                try:
                    if self._book_ticket(candidate, prepared_passengers):
                        return 0
                except Exception as exc:
                    logging.warning("提交订单过程出现异常，跳过当前候选票: %s", exc)
                    continue
            time.sleep(self._current_query_interval(target_start))
        logging.info("已达到最大重试次数，任务结束")
        return 1

    def check_stations(self) -> int:
        """检查配置中的车站是否有效，并打印对应的 12306 站点编码。"""
        self.stations.load()
        for station_name in (self.cfg.from_station, self.cfg.to_station):
            code = self.stations.stations.get(station_name)
            if code:
                logging.info("车站有效: %s -> %s", station_name, code)
            else:
                logging.error("车站无效: %s（请使用 12306 标准站名）", station_name)
                suggestions = self._suggest_stations(station_name)
                if suggestions:
                    logging.info("相近站名: %s", "、".join(suggestions))
        return 0

    def _suggest_stations(self, name: str, limit: int = 8) -> List[str]:
        """基于模糊匹配，为无效站名提供相近的候选站名。"""
        candidates: List[Tuple[int, str]] = []
        for station in self.stations.stations:
            if name in station or station in name:
                score = abs(len(station) - len(name))
                candidates.append((score, station))
        candidates.sort(key=lambda item: (item[0], item[1]))
        return [item[1] for item in candidates[:limit]]

    def list_passengers(self) -> int:
        """登录后列出当前账号的常用乘车人，方便填写 PASSENGER_NAMES。"""
        self.client.ensure_login()
        passengers = self.client.get_passengers()
        if not passengers:
            logging.info("当前账号没有常用乘车人")
            return 0
        logging.info("当前账号常用乘车人 %s 人:", len(passengers))
        for index, passenger in enumerate(passengers, start=1):
            name = passenger.get("passenger_name", "")
            id_type = passenger.get("passenger_id_type_name", "")
            id_no = passenger.get("passenger_id_no", "")
            masked = id_no[:4] + "********" + id_no[-4:] if len(id_no) >= 8 else ""
            logging.info("  %s. %s（%s %s）", index, name, id_type, masked)
        return 0

    def query_once(self) -> int:
        """登录后执行单次余票查询，便于快速验证配置与观察票源。"""
        self.clock.sync()
        self.stations.load()
        from_code = self.stations.code(self.cfg.from_station)
        to_code = self.stations.code(self.cfg.to_station)
        self.client.ensure_login()
        logging.info(
            "单次查询: %s -> %s, 日期 %s, 座席 %s",
            self.cfg.from_station,
            self.cfg.to_station,
            self.cfg.train_date,
            ",".join(self.cfg.seat_types),
        )
        tickets = self.client.query_tickets(from_code, to_code)
        if not tickets:
            logging.info("没有查询到任何车次")
            return 0
        logging.info("共查询到 %s 个车次:", len(tickets))
        for ticket in tickets:
            seat_text = " ".join(
                f"{label}={ticket['seats'].get(spec.stock_key, '--')}"
                for label, spec in self.seat_sequence
            )
            logging.info(
                "  %s %s-%s %s 历时%s | %s",
                ticket["station_train_code"],
                ticket["start_time"],
                ticket["arrive_time"],
                ticket["duration"],
                ticket["date"],
                seat_text,
            )
        return 0

    def _select_passengers(self) -> List[Dict[str, Any]]:
        passengers = self.client.get_passengers()
        by_name = {item.get("passenger_name"): item for item in passengers}
        missing = [name for name in self.cfg.passenger_names if name not in by_name]
        if missing:
            available = "、".join(sorted(name for name in by_name if name))
            raise AppError(f"配置中的乘车人不存在: {missing}。当前账号常用乘车人: {available}")
        selected = [by_name[name] for name in self.cfg.passenger_names]
        logging.info("已选择乘车人: %s", "、".join(self.cfg.passenger_names))
        return selected

    def _prepare_passengers_by_seat_code(self, passengers: List[Dict[str, Any]]) -> Dict[str, PreparedPassengerSet]:
        submit_codes = {spec.submit_code for spec in SEAT_SPECS.values() if spec.submit_code}
        submit_codes.update({"O", "1"})
        prepared: Dict[str, PreparedPassengerSet] = {}
        for submit_code in submit_codes:
            passenger_copies = []
            for passenger in passengers:
                passenger_copy = passenger.copy()
                passenger_copy["seat_type"] = submit_code
                passenger_copies.append(passenger_copy)
            passenger_ticket_str, old_passenger_str = _build_passenger_strings(
                passenger_copies, self.cfg.ticket_type
            )
            prepared[submit_code] = PreparedPassengerSet(passenger_copies, passenger_ticket_str, old_passenger_str)
        _perf_log(self.cfg, "已预生成 %s 种座席乘车人提交字符串", len(prepared))
        return prepared

    def _resolve_target_start(self) -> Optional[datetime]:
        return _resolve_schedule_time(self.cfg.start_at, self.clock.now())

    def _wait_for_query_start(self, target_start: Optional[datetime]) -> None:
        if not target_start:
            return
        now = self.clock.now()
        if len(self.cfg.start_at) == 8 and target_start < now:
            logging.info("START_AT 已早于当前服务器时间，立即开始")
            return
        query_start = target_start - timedelta(seconds=self.cfg.pre_query_seconds)
        if query_start <= now:
            logging.info("已进入热身查询窗口，立即开始；目标开售时间 %s", target_start.strftime("%Y-%m-%d %H:%M:%S"))
            return
        logging.info(
            "等待热身查询窗口: %s；目标开售时间: %s",
            query_start.strftime("%Y-%m-%d %H:%M:%S"),
            target_start.strftime("%Y-%m-%d %H:%M:%S"),
        )
        self.clock.sleep_until(query_start)

    def _should_stop(self) -> bool:
        now = self.clock.now()
        stop_at = _resolve_schedule_time(self.cfg.stop_at, now)
        if not stop_at:
            return False
        return now >= stop_at

    def _current_query_interval(self, target_start: Optional[datetime]) -> float:
        if not target_start:
            return self.cfg.query_interval_seconds
        now = self.clock.now()
        hot_start = target_start - timedelta(seconds=self.cfg.pre_query_seconds)
        hot_end = target_start + timedelta(seconds=self.cfg.hot_window_seconds)
        if hot_start <= now <= hot_end:
            return self.cfg.hot_query_interval_seconds
        return self.cfg.query_interval_seconds

    def _find_candidates(self, tickets: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        def sort_key(ticket: Dict[str, Any]) -> Tuple[int, int, str]:
            train = ticket["station_train_code"].upper()
            if not self.preferred_order:
                return (0, 0, train)
            if train in self.preferred_order:
                return (0, self.preferred_order[train], train)
            return (1, len(self.preferred_order), train)

        candidates: List[Dict[str, Any]] = []
        for ticket in sorted(tickets, key=sort_key):
            train_code = ticket["station_train_code"].upper()
            if self.cfg.preferred_trains and self.cfg.only_preferred_trains and train_code not in self.preferred_order:
                continue
            if not ticket["can_buy"]:
                continue
            for seat_label, spec in self.seat_sequence:
                stock = ticket["seats"].get(spec.stock_key, "--")
                if not _stock_available(stock):
                    continue
                candidates.append(
                    {
                        "ticket": ticket,
                        "seat_label": seat_label,
                        "seat_type": _resolve_submit_seat_code(seat_label, ticket),
                        "stock": stock,
                    }
                )
        return candidates

    def _book_ticket(self, candidate: Dict[str, Any], prepared_passengers: Dict[str, PreparedPassengerSet]) -> bool:
        ticket = candidate["ticket"]
        seat_type = candidate["seat_type"]
        passengers = prepared_passengers[seat_type]
        submit_start = time.perf_counter()
        found_perf = candidate.get("found_perf")
        if isinstance(found_perf, float):
            _perf_log(self.cfg, "发现候选到提交请求准备耗时 %.1fms", (submit_start - found_perf) * 1000)
        logging.info("开始提交订单: %s %s", ticket["station_train_code"], candidate["seat_label"])
        try:
            ok, message = self.client.submit_order_request(ticket)
        except requests.exceptions.RequestException as exc:
            logging.warning("提交订单请求网络异常，跳过当前候选票: %s", exc)
            return False
        _perf_log(self.cfg, "submitOrderRequest 耗时 %.1fms", _elapsed_ms(submit_start))
        if not ok:
            logging.warning("提交订单请求失败: %s", message)
            return False
        try:
            step_start = time.perf_counter()
            token, ticket_info = self.client.init_dc()
            _perf_log(self.cfg, "initDc 耗时 %.1fms", _elapsed_ms(step_start))
        except (AppError, requests.exceptions.RequestException) as exc:
            logging.warning("初始化确认订单页失败: %s", exc)
            return False
        step_start = time.perf_counter()
        try:
            ok, message = self.client.check_order_info(passengers, token)
        except requests.exceptions.RequestException as exc:
            logging.warning("订单信息校验网络异常: %s", exc)
            return False
        _perf_log(self.cfg, "checkOrderInfo 耗时 %.1fms", _elapsed_ms(step_start))
        if not ok:
            logging.warning("订单信息校验失败: %s", message)
            return False
        step_start = time.perf_counter()
        try:
            ok, queue_data = self.client.get_queue_count(ticket, ticket_info, seat_type, token)
        except requests.exceptions.RequestException as exc:
            logging.warning("获取排队信息网络异常: %s", exc)
            return False
        _perf_log(self.cfg, "getQueueCount 耗时 %.1fms", _elapsed_ms(step_start))
        if not ok:
            logging.warning("获取排队信息失败: %s", queue_data)
            return False
        logging.info("排队信息: %s", queue_data)
        left_ticket = queue_data.get("ticket") or ticket_info.get("leftTicketStr") or ticket.get("left_ticket", "")
        step_start = time.perf_counter()
        try:
            ok, message = self.client.confirm_single_for_queue(passengers, ticket_info, left_ticket, token)
        except requests.exceptions.RequestException as exc:
            logging.warning("确认排队网络异常: %s", exc)
            return False
        _perf_log(self.cfg, "confirmSingleForQueue 耗时 %.1fms", _elapsed_ms(step_start))
        if not ok:
            logging.warning("确认排队失败: %s", message)
            return False
        logging.info("已提交排队，等待出票结果...")
        for _ in range(self.cfg.order_wait_attempts):
            try:
                ok, result = self.client.query_order_wait_time(token)
            except requests.exceptions.RequestException as exc:
                logging.warning("查询出票结果网络异常，稍后重试: %s", exc)
                time.sleep(self.cfg.order_wait_interval_seconds)
                continue
            order_id = result.get("orderId")
            wait_time = result.get("waitTime")
            message = result.get("msg")
            if order_id:
                logging.info("抢票成功，订单号: %s。请尽快到 12306 完成支付。", order_id)
                return True
            if _is_terminal_order_failure(message):
                logging.warning("出票失败，放弃当前候选票: %s", message or f"waitTime={wait_time}")
                return False
            if message:
                logging.info("出票状态: %s", message)
            elif wait_time is not None:
                logging.info("排队中，预计等待 %s 秒", wait_time)
            time.sleep(self.cfg.order_wait_interval_seconds)
        logging.warning("出票等待超时，请手动到 12306 订单中心确认")
        return False
