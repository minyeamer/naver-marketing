from __future__ import annotations

from core.action import CafeBannedError, CafeNotFoundError, goto_cafe_url, read_action_log
from core.browser import BrowserController, ProfileNotFoundError
from core.login import NaverLoginError, NaverLoginFailedError, ReCaptchaRequiredError, WarningAccountError

from extensions.adb import AdbRuntimeError, IpAddrNotChangedError, MOBILE_IP_TOKEN
from extensions.adb import ensure_adb_server_ready, rotate_mobile_ip_addr
from extensions.gsheets import ServiceAccount, WorksheetConnection, WorksheetClient, ACCOUNT_PATH
from extensions.slack import SlackClient, SlackConfig

from utils.common import AttrDict, Delay, print_json, wait
from utils.timer import ActionTimer

from pathlib import Path
from typing import get_type_hints, Any, Literal, Sequence, TypedDict, TypeVar
import datetime as dt
import json
import random
import sys
import time
import traceback


class QuietTime(TypedDict):
    start: str
    end: str


LOGS_ROOT = ".logs"

def is_default(value: Any) -> bool:
    return isinstance(value, str) and (value == ":default:")


class MaxRetries(TypedDict, total=False):
    task_loop: int
    task_error: int


class Config(TypedDict, total=False):
    no: int
    last_active_ts: dt.datetime | str | None
    userid: str
    passwd: str
    ip_addr: str
    cafe_id: int | str
    menu_id: int | str
    cafe_name: str
    visit_limit: int | str
    visit_delay: int | str
    total_visit_count: int | str
    error_delay: int | str


Index = TypeVar("Index", bound=int)
StopTask = TypeVar("StopTask", bound=bool)

ErrorFlag = Literal[
    "Chrome 프로필 없음", "adb 실행 오류", "IP주소 변경 실패",
    "네이버 계정 불일치", "네이버 계정 보호조치", "네이버 CAPTCHA 발생", "네이버 로그인 오류",
    "카페 비회원", "카페 활동정지", "반복 횟수 초과", "실행 금지 시간",
    "브라우저 조작 오류", "알 수 없는 오류", "오류 횟수 초과",
]


class ErrorLog(TypedDict):
    type: str
    message: str | None
    exc_info: str
    flag: ErrorFlag


class MaxLoopExceeded(RuntimeError):
    pass


class QuietHoursError(RuntimeError):
    pass


def safe_int(value: int | str | None) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def randint(value: int | str | None) -> int:
    if "~" in str(value):
        try:
            return random.randint(*map(safe_int, str(value).split("~", 1)))
        except ValueError:
            return 0
    return safe_int(value)


def to_seconds(value: int | str | None) -> int:
    if isinstance(value, str) and ":" in value:
        parts = value.split(":")[-3:]
        return sum(safe_int(part) * 60 ** (len(parts) - index - 1) for index, part in enumerate(parts))
    return safe_int(value)


def to_datetime(value: dt.datetime | str | None) -> dt.datetime | None:
    if isinstance(value, dt.datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, str) and value.strip():
        try:
            return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            pass
    return None


def seconds_to_text(seconds: float) -> str:
    minutes = max(1, int(round(seconds / 60)))
    hours, minutes = divmod(minutes, 60)
    return f"{hours}시간 {minutes}분" if hours and minutes else (f"{hours}시간" if hours else f"{minutes}분")


def format_wait_time(seconds: float) -> str:
    minutes = max(1, int(round(seconds / 60)))
    hours, minutes = divmod(minutes, 60)
    if hours and minutes:
        return f"{hours}시간 {minutes}분"
    elif hours:
        return f"{hours}시간"
    else:
        return f"{minutes}분"


def progress(step: int | None, total: int | None):
    return f"{safe_int(step)}/{safe_int(total)}"


class ConfigWrapper(AttrDict):

    def __init__(self, config: Config):
        super().__init__()
        self.no = config["no"]
        self.userid = config["userid"]
        self.passwd = config["passwd"]
        self.ip_addr = config["ip_addr"]
        self.cafe_id = config["cafe_id"]
        self.menu_id = "0"
        self.cafe_name = config["cafe_name"]
        self.visit_count = 0
        self.visit_limit = max(0, randint(config.get("visit_limit")))
        self.visit_delay = max(0, to_seconds(config.get("visit_delay")))
        self.total_visit_count = max(0, safe_int(config.get("total_visit_count")))
        self.last_active_ts = to_datetime(config.get("last_active_ts"))
        self.error_delay = max(0, to_seconds(config.get("error_delay")))
        self.errors: list[ErrorLog] = list()
        self.__timer = ActionTimer()
        if self.last_active_ts:
            self.__timer.set_timer("visit", self.last_active_ts)

    @property
    def log(self) -> dict:
        return dict(
            last_active_ts = ((self.last_active_ts.strftime("%Y-%m-%dT%H:%M:%S")+"+09:00")
                if isinstance(self.last_active_ts, dt.datetime) else None),
            visit_count = self.visit_count,
            visit_limit = self.visit_limit,
            total_visit_count = self.total_visit_count,
            timer = self.timer.get_all_elapsed_times(ndigits=3),
            errors = self.errors,
        )

    @property
    def done(self) -> bool:
        return self.visit_count >= self.visit_limit

    @property
    def timer(self) -> ActionTimer:
        return self.__timer

    def has_next_visit(self) -> bool:
        return ((not self.done)
                and self.timer.gte("visit", self.visit_delay)
                and self.timer.gte("error", self.error_delay))

    def visit(self):
        self.visit_count += 1
        self.last_active_ts = dt.datetime.now()
        self.timer.set_timer("visit", self.last_active_ts)
        self.timer.end_timer("error")

    def stop(self):
        self.visit_count = self.visit_limit

    def public_items(self) -> dict:
        filter_private = (lambda _ConfigWrapper = None, **kwargs: kwargs)
        return filter_private(**self)


class Visitor(BrowserController):

    def __init__(
            self,
            configs: dict | Sequence[Config],
            profiles_path: str | Path,
            adb_path: str | Path | None = None,
            device: str | None = None,
            headless: bool = True,
            clear_data: bool = False,
            action_delay: Delay = (0.3, 0.6),
            goto_delay: Delay = (1, 3),
            reload_delay: Delay = (3, 5),
            upload_delay: Delay = (2, 4),
            quiet_time: QuietTime | None = None,
            mobile: bool = True,
            run_label: str | None = None,
            write_config: WorksheetConnection = dict(),
            slack_config: SlackConfig = dict(),
            **kwargs: Any
        ):
        super().__init__(None, "Default",
            device, headless, clear_data, action_delay, goto_delay, reload_delay, upload_delay)
        self.profiles_path = Path(profiles_path)
        self.adb_path = Path(adb_path) if adb_path else None

        self.set_quite_time(quiet_time)
        self.check_quiet_time()

        if isinstance(configs, dict):
            self.validate_worksheet_connection(configs, empty=False)
            self.configs = self.read_configs_from_gsheets(**configs)
        elif isinstance(configs, Sequence):
            self.configs = [ConfigWrapper(config) for config in configs]
        else:
            raise ValueError("설정이 올바르지 않습니다.")

        self.index: Index = 0
        self.mobile = mobile
        self.run_label = run_label

        self.slack = SlackClient(**slack_config) if slack_config else None

        self.validate_worksheet_connection(write_config, empty=True)
        self.write_config = write_config

    @property
    def config(self) -> ConfigWrapper:
        return self.configs[self.index]

    @property
    def profile(self) -> dict:
        return {"path": self.profiles_path / self.config.userid, "dir": "Default"}

    @property
    def delays2(self) -> dict[str,Delay]:
        return self.delays.get_delays(["action", "goto"])

    def set_quite_time(self, quiet_time: QuietTime = dict()):
        if quiet_time:
            quiet_time = quiet_time.copy()
            now = dt.datetime.now().strftime("%H:%M:%S")
            today = dt.date.today()
            tomorrow = dt.date.today() + dt.timedelta(days=1)
            for key in ["start", "end"]:
                date = "{} ".format(tomorrow if quiet_time[key] < now else today)
                quiet_time[key] = dt.datetime.strptime(date+quiet_time[key], "%Y-%m-%d %H:%M:%S")
            self.quiet_time: dict[str, dt.datetime] = quiet_time
        else:
            self.quiet_time: dict[str, dt.datetime] = None

    def check_quiet_time(self):
        if self.quiet_time:
            if self.quiet_time["start"] <= dt.datetime.now() <= self.quiet_time["end"]:
                raise QuietHoursError("실행 금지 시간대입니다.")

    ########################### Entry Point ###########################

    def start(
            self,
            max_retries: MaxRetries = dict(),
            task_delay: float = 30.,
            action_delay: float = 600.,
            verbose: int | str | Path = 0,
            save_log: bool = True,
            **kwargs: Any
        ):
        self.check_quiet_time()
        self.init_adb_server()
        self.notify_task_loop(loop_step=1, task_flag="시작")

        stop_task: StopTask = None

        step = 0
        for step in range(1, (max_retries.get("task_loop") or 30) + 1):
            if stop_task or all(config.done for config in self.configs):
                self.notify_task_loop(step, "실패" if stop_task else "완료")
                return

            if isinstance(stop_task, bool):
                self.wait_task_loop(step, task_delay, verbose)

            stop_task = self.task_loop(step, max_retries, action_delay, verbose, save_log)

        all_done = all(config.done for config in self.configs)
        self.notify_task_loop(step, task_flag=("완료" if all_done else "실패"))

    def wait_task_loop(self, loop_step: int, task_delay: float = 30., verbose: int | str | Path = 0):
        delays = [delay for key in ["visit", "error"]
                if isinstance(delay := self.min_action_delay(key), float)]
        wait_delay = max(task_delay, (min(delays) if delays else 0.))
        self.print_loop("task_loop_wait", loop_step, verbose, seconds=wait_delay)
        self.notify_task_loop(loop_step, task_flag="대기", wait_delay=wait_delay)
        wait(wait_delay)

    def min_action_delay(self, key: Literal["visit", "error"]) -> float | None:
        delays = [max(0., getattr(config, f"{key}_delay") - elapsed)
            for config in self.configs if not config.done
                if isinstance(elapsed := config.timer.get_elapsed_time(key), float)]
        return min(delays) if delays else None

    ############################# <start> #############################
    ############################ Task Loop ############################

    def task_loop(
            self,
            loop_step: int,
            max_retries: MaxRetries = dict(),
            action_delay: float = 600.,
            verbose: int | str | Path = 0,
            save_log: bool = True,
        ) -> bool:
        max_task_error = max_retries.get("task_error") or 10

        for i, config in enumerate(self.configs):
            self.index = i
            error_flag = None

            if config.done or (not config.has_next_visit()):
                continue

            visit_started_at = time.monotonic() if loop_step > 1 else None
            try:
                self.print_loop("task_loop_start", loop_step, verbose)
                self.check_quiet_time()

                if self.is_mobile_proxy(config.ip_addr):
                    self.run_mobile_proxy()

                self.do_visit(proxy=self.resolve_proxy(config.ip_addr))
            except Exception as error:
                try:
                    error_flag = self.handle_error_task(error, max_task_error)
                except Exception:
                    error_flag = "알 수 없는 오류"
            finally:
                if all(config.done for config in self.configs[i+1:]):
                    visit_started_at = None
                stop_task = self.finalize_task(loop_step, action_delay, visit_started_at, error_flag, verbose, save_log)
                if stop_task:
                    self.print_loop("task_loop_break", loop_step, verbose)
                    return True

        return False

    @BrowserController.with_chrome_profile
    def do_visit(self, **kwargs):
        self.notify_playwright_proxy(**kwargs)
        goto_cafe_url(self.page, self.config.cafe_id, None, self.mobile, self.delays.goto)

        action_log = read_action_log(self.page, total_only=True, **self.delays2)
        if isinstance(visit_count := action_log["total"].get("visit"), int):
            self.config.total_visit_count = visit_count

        self.config.visit()

    def handle_error_task(self, error: Exception, max_task_error: int) -> ErrorFlag:
        self.config.timer.start_timer("error")
        error_flag = self.get_error_flag(error)

        self.config.errors.append(dict(
            type = str(type(error).__name__),
            message = self.get_error_msg(error),
            exc_info = '\n'.join(traceback.format_exception(*sys.exc_info())),
            flag = error_flag,
        ))

        if len(self.config.errors) > max_task_error:
            return "오류 횟수 초과"
        return error_flag

    def finalize_task(
            self,
            loop_step: int,
            action_delay: float,
            visit_started_at: float | None,
            error_flag: ErrorFlag | None,
            verbose: int | str | Path,
            save_log: bool,
        ) -> StopTask:
        try:
            self.print_loop("task_loop_end", loop_step, verbose)
        except Exception:
            pass

        try:
            stop_task = self.handle_error_flag(error_flag)
        except Exception:
            stop_task = True

        if save_log:
            try:
                self.save_log_json()
            except Exception:
                pass

        if self.write_config:
            try:
                self.write_log_table_to_gsheets(**self.write_config)
            except Exception:
                pass

        wait_delay = None
        if (action_delay > 0.) and (loop_step > 1) and isinstance(visit_started_at, float):
            if not (self.config.done or stop_task):
                wait_delay = max(0., action_delay - (time.monotonic() - visit_started_at))

        try:
            action_flag = "실패" if error_flag else ("완료" if self.config.done else "대기")
            self.notify_visit_action(loop_step, action_flag, error_flag, wait_delay)
        except Exception:
            pass

        if isinstance(wait_delay, float) and (wait_delay > 0.):
            wait(wait_delay)

        return stop_task

    ############################ Task Loop ############################
    ############################## <end> ##############################

    ########################### Handle Error ##########################

    def get_error_msg(self, error: Exception) -> str | None:
        try:
            return str(error) or None
        except Exception:
            return None

    def get_error_flag(self, error: Exception) -> ErrorFlag:
        if isinstance(error, ProfileNotFoundError):
            return "Chrome 프로필 없음"
        elif isinstance(error, AdbRuntimeError):
            return "adb 실행 오류"
        elif isinstance(error, IpAddrNotChangedError):
            return "IP주소 변경 실패"
        elif isinstance(error, NaverLoginError):
            if isinstance(error, NaverLoginFailedError):
                return "네이버 계정 불일치"
            elif isinstance(error, WarningAccountError):
                return "네이버 계정 보호조치"
            elif isinstance(error, ReCaptchaRequiredError):
                return "네이버 CAPTCHA 발생"
            else:
                return "네이버 로그인 오류"
        elif isinstance(error, CafeNotFoundError):
            return "카페 비회원"
        elif isinstance(error, CafeBannedError):
            return "카페 활동정지"
        elif isinstance(error, MaxLoopExceeded):
            return "반복 횟수 초과"
        elif isinstance(error, QuietHoursError):
            return "실행 금지 시간"
        elif error.__class__.__module__.startswith("playwright"):
            return "브라우저 조작 오류"
        else:
            return "알 수 없는 오류"

    def handle_error_flag(self, error_flag: ErrorFlag | None) -> bool:
        if not isinstance(error_flag, str):
            return False

        elif error_flag in {"adb 실행 오류", "IP주소 변경 실패", "실행 금지 시간"}:
            return True

        elif error_flag.startswith("네이버") or error_flag == "Chrome 프로필 없음":
            userid = self.config.userid
            for config in self.configs:
                if config.userid == userid:
                    config.stop()

        elif error_flag in {"카페 비회원", "카페 활동정지", "오류 횟수 초과"}:
            self.config.stop()

        return False

    ############################# Task Log ############################

    def print_loop(self, task_step: str, loop_step: int, verbose: int | str | Path = 0, **kwargs):
        common = lambda: dict(
            index = self.index,
            no = self.config.no,
            userid = self.config.userid,
            cafe_id = self.config.cafe_id,
            cafe_name = self.config.cafe_name,
        )

        if task_step == "task_loop_start":
            body = dict(
                task_step = task_step,
                loop_step = loop_step,
                **common(),
                config = self.config.public_items(),
            )

        elif task_step == "task_loop_wait":
            body = dict(
                task_step = task_step,
                loop_step = loop_step,
                seconds = kwargs.get("seconds"),
                timers = {i: config.timer.get_all_elapsed_times() for i, config in enumerate(self.configs)},
                delays = {i: {key: max(0.0, getattr(config, f"{key}_delay") - elapsed)
                    for key, elapsed in config.timer.get_all_elapsed_times().items()
                            if key in {"visit", "error"}}
                        for i, config in enumerate(self.configs)},
            )

        else:
            body = dict(
                task_step = task_step,
                loop_step = loop_step,
                **common(),
                log = self.config.log,
            )

        body["timestamp"] = dt.datetime.now().isoformat(timespec="seconds")
        print_json(body, verbose)

    def save_log_json(self):
        logs_path = Path(LOGS_ROOT) / "visit" / self.config.userid
        logs_path.mkdir(parents=True, exist_ok=True)
        with open(logs_path / (dt.datetime.now().strftime("%Y%m%d%H%M%S")+".json"), 'w', encoding="utf-8") as file:
            json.dump(self.config.log, file, indent=2, ensure_ascii=False, default=str)

    ########################## Read and Write #########################

    def read_configs_from_gsheets(
            self,
            key: str,
            sheet: str,
            account: str | Path | Literal[":default:"] = ":default:",
            head: int = 1,
        ) -> list[ConfigWrapper]:
        client = WorksheetClient(self._get_credentials(account), key, sheet, head)
        str_keys = [i for i, type in enumerate(get_type_hints(Config).values(), start=1) if type == str]
        records = client.get_all_records(numericise_ignore=str_keys)
        return [ConfigWrapper(record) for record in records if isinstance(record["no"], int)]

    def write_log_table_to_gsheets(
            self,
            key: str,
            sheet: str,
            account: str | Path | Literal[":default:"] = ":default:",
            head: int = 1,
        ):
        client = WorksheetClient(self._get_credentials(account), key, sheet, head)
        records = self.make_log_table()
        client.overwrite_worksheet(records)

    def make_log_table(self) -> list[dict]:
        rows = list()
        for config in self.configs:
            rows.append(dict(
                no = config.no,
                userid = config.userid,
                cafe_name = config.cafe_name,
                ip_addr = config.ip_addr,
                last_active_ts = config.last_active_ts,
                visit_count = config.visit_count,
                total_visit_count = config.total_visit_count,
                error_flag = ", ".join([error["flag"] for error in config.errors]) if config.errors else None,
            ))
        return rows

    def validate_worksheet_connection(self, conn: WorksheetConnection, empty: bool = False) -> bool:
        if not isinstance(conn, dict):
            raise TypeError("구글시트 연결 정보가 올바른 타입이 아닙니다.")
        elif empty and (not conn):
            return True
        elif not (conn.get("key") and conn.get("sheet")):
            raise KeyError("구글시트 연결 정보에 'key' 또는 'sheet' 값이 없습니다.")
        return True

    def _get_credentials(self, account: str | Path | Literal[":default:"] = ":default:") -> ServiceAccount:
        return ServiceAccount(ACCOUNT_PATH if is_default(account) else str(account))

    ######################## Slack Notification #######################

    def notify_slack(self, text: str, blocks: list | None = None):
        if self.slack:
            try: self.slack.chat_message(text, blocks=blocks)
            except: pass

    def run_md(self, label: str, sep: str = "  ") -> str:
        return f"[{label}]" + ((sep + self.run_label) if self.run_label else str())

    @property
    def now(self) -> str:
        return dt.datetime.now().strftime("%H:%M")

    @property
    def user_md(self) -> str:
        return f"_*{self.config.userid}*_ ({progress(self.index+1, len(self.configs))})"

    def notify_task_loop(
            self,
            loop_step: int,
            task_flag: Literal["시작", "대기", "완료", "실패"] | None = None,
            wait_delay: float | None = None,
            sep: str = "  ·  ",
        ):
        first_line = [self.run_md(f"프로그램 {task_flag}")]
        if loop_step > 1:
            first_line.append(f"반복 횟수 {loop_step}")
            if wait_delay:
                first_line.append(f"{format_wait_time(wait_delay)} 후 재시작")
        else:
            first_line.append(f"{len(self.configs)}개 계정-카페 활동 대기")
        text = sep.join(first_line + [self.now])

        rows = [["순서", "번호", "아이디", "카페명", "방문 수\n(완료/할당)", "누적 방문"]]

        for i, config in enumerate(self.configs, start=1):
            rows.append([
                str(i), str(config.no), config.userid, config.cafe_name,
                progress(config.visit_count, config.visit_limit),
                str(config.total_visit_count) if config.total_visit_count > 0 else '-',
            ])

        if self.slack:
            self.notify_slack(text, blocks=[self.slack.create_table(rows)])

    def notify_visit_action(
            self,
            loop_step: int,
            action_flag: Literal["시작", "대기", "완료", "실패"] | None = None,
            error_flag: str | None = None,
            wait_delay: float | None = None,
            sep: str = "  ·  ",
        ):
        config = self.config
        first_line = [self.run_md("카페 방문"), self.user_md, config.cafe_name, self.now]

        if action_flag == "대기":
            bullet = ":small_orange_diamond: "
        elif action_flag == "완료":
            bullet = ":small_blue_diamond: "
        elif action_flag == "실패":
            bullet = f":small_red_triangle: {(error_flag + sep) if error_flag else str()}"
        else:
            bullet = ":black_small_square: "

        if not ((action_flag == "시작") and (loop_step == 1)):
            second_line = [f"반복 횟수 {loop_step}"]
            second_line.append(f"방문 수(완료/할당) {progress(config.visit_count, config.visit_limit)}")
            if config.total_visit_count > 0:
                second_line.append(f"누적 방문 {config.total_visit_count}")
        else:
            second_line = list()

        third_line = list()
        if config.done:
            third_line.append("금일 할당량 완료")
        else:
            elapsed = self.config.timer.get_elapsed_time("visit")
            if (elapsed is None) or (elapsed >= config.visit_delay):
                third_line.append("지금 방문 가능")
            else:
                third_line.append(f"{format_wait_time(config.visit_delay - elapsed)} 후 방문 가능")

        if wait_delay:
            third_line.append(f"다음 활동 {format_wait_time(wait_delay)} 후")

        self.notify_slack('\n'.join([
            sep.join(first_line),
            *([bullet + sep.join(second_line)] if second_line else list()),
            f":hourglass_flowing_sand: {sep.join(third_line)}",
        ]))

    def notify_playwright_proxy(self, proxy: str | None = None, sep: str = "  ·  ", **kwargs):
        if proxy:
            self.notify_slack('\n'.join([
                sep.join([self.run_md("프록시 IP 적용"), self.user_md, self.now]),
                f":white_checK_mark: {proxy}",
            ]))

    #################### Mobile Tethering Extension ###################

    def is_mobile_proxy(self, ip_addr: str | None) -> bool:
        return isinstance(ip_addr, str) and ip_addr.strip().lower() == MOBILE_IP_TOKEN

    def resolve_proxy(self, ip_addr: str | None) -> str | None:
        if self.is_mobile_proxy(ip_addr):
            return None
        return ip_addr or None

    def init_adb_server(self):
        if not any(self.is_mobile_proxy(config.ip_addr) for config in self.configs):
            return
        elif not (self.adb_path and self.adb_path.exists()):
            raise FileNotFoundError(f"adb 실행 파일을 찾을 수 없습니다: {self.adb_path}")
        ensure_adb_server_ready(self.adb_path)

    def run_mobile_proxy(self, sep: str = "  ·  "):
        try:
            ensure_adb_server_ready(self.adb_path)
            original_ip, new_ip = rotate_mobile_ip_addr(self.adb_path)
            self.notify_slack('\n'.join([
                sep.join([self.run_md("모바일 IP 변경"), self.user_md, self.now]),
                f":white_checK_mark: {original_ip} → {new_ip}",
            ]))
        except Exception:
            self.notify_slack('\n'.join([
                sep.join([self.run_md("모바일 IP 변경 실패"), self.user_md, self.now]),
                ":x: 비행기 모드 전환 후 60초 내 IP 변경 실패",
            ]))
            raise
