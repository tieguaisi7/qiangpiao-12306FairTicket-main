import argparse
import logging
import sys
from pathlib import Path
from typing import List, Optional

from ticket_app.configuration import AppConfig, AppError, DEFAULT_CONFIG_FILE, load_config
from ticket_app.runner import TicketRunner

VERSION = "1.1.0"


def configure_logging(level: str, log_file: Optional[Path] = None) -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, level, logging.INFO))
    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    if log_file is not None:
        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.FileHandler(str(log_file), encoding="utf-8")
            file_handler.setFormatter(
                logging.Formatter(
                    fmt="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S",
                )
            )
            root.addHandler(file_handler)
        except OSError as exc:
            logging.warning("无法写入日志文件 %s: %s", log_file, exc)


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="12306 命令行抢票助手")
    parser.add_argument("--version", action="version", version=f"12306FairTicket v{VERSION}")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_FILE), help="配置脚本路径，默认使用同目录 config.py")
    parser.add_argument("--validate-config", action="store_true", help="只校验配置，不登录、不访问 12306")
    parser.add_argument("--check-stations", action="store_true", help="校验配置中的车站名，不登录、不下单")
    parser.add_argument("--list-passengers", action="store_true", help="登录后列出当前账号的常用乘车人，便于填写 PASSENGER_NAMES")
    parser.add_argument("--query-once", action="store_true", help="登录后执行单次余票查询，便于验证配置与观察票源")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    config_path = Path(args.config).resolve()
    logging_ready = False
    try:
        cfg = load_config(config_path)
        configure_logging(cfg.log_level, cfg.log_file)
        logging_ready = True
        logging.info("已加载配置: %s", cfg.config_path)
        if args.validate_config:
            logging.info("配置校验通过")
            return 0
        runner = TicketRunner(cfg)
        if args.check_stations:
            return runner.check_stations()
        if args.list_passengers:
            return runner.list_passengers()
        if args.query_once:
            return runner.query_once()
        return runner.run()
    except AppError as exc:
        if not logging_ready:
            configure_logging("INFO")
        logging.error("%s", exc)
        return 2
    except KeyboardInterrupt:
        if not logging_ready:
            configure_logging("INFO")
        logging.warning("用户中断任务")
        return 130
    except Exception as exc:
        if not logging_ready:
            configure_logging("INFO")
        logging.exception("程序异常退出: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
