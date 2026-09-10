from __future__ import annotations

from core.browser import BrowserController, ProfileNotFoundError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from core.login import NaverLoginError
from core.login import WarningAccountError, ReCaptchaRequiredError, NaverLoginFailedError

from core.action import CafeNotFoundError, CafeNotLoadedError, CafeBannedError, Wpm
from core.action import ActionLog, TotalCount, TodayCount
from core.action import goto_cafe_home, goto_cafe, goto_menu, goto_cafe_url, return_to_cafe_home
from core.action import goto_article, explore_articles
from core.action import reload_articles, next_articles, go_back, copy_article_url
from core.action import read_article, read_full_article, read_article_and_write_comment
from core.action import write_article, update_article, like_article, reply_my_articles
from core.action import read_my_articles, open_info, close_info, read_action_log

from core.agent import set_api_key, KEY_PATH, PROMPTS_ROOT

from extensions.adb import AdbRuntimeError, IpAddrNotChangedError, MOBILE_IP_TOKEN
from extensions.adb import ensure_adb_server_ready, rotate_mobile_ip_addr
from extensions.gsheets import WorksheetClient, ServiceAccount, ACCOUNT_PATH
from extensions.slack import SlackClient, SlackConfig
# from extensions.vpn import VpnClient, VpnConfig, VpnRuntimeError
# from extensions.vpn import VpnLoginFailedError, VpnInUseError, VpnFailedError
# from extensions.vpn import WindowNotFoundError, ElementNotFoundError

from utils.common import AttrDict, Delay, wait, print_json, regexp_extract
from utils.timer import ActionTimer

from typing import get_type_hints, Literal, TypeVar, TypedDict, TYPE_CHECKING
from collections import deque
import datetime as dt
import json
import time

from pathlib import Path
import os
import random
import sys
import traceback

if TYPE_CHECKING:
    from typing import Any, Sequence
    from core.action import ArticleId, CafeId, Comment, Replies
    from core.agent import ArticleInfo, NewArticle, ModifiedArticle
    from extensions.gsheets import WorksheetConnection

class QuiteTime(TypedDict):
    start: str | int
    end: str | int


SECRETS_ROOT = ".secrets"
STATES_ROOT = os.path.join(SECRETS_ROOT, "states")

LOGS_ROOT = ".logs"

DEFAULTS = {
    "account": ACCOUNT_PATH,
    "openai_key": KEY_PATH,
}

def is_default(value: Any) -> bool:
    return isinstance(value, str) and (value == ":default:")


class MaxLoopExceeded(RuntimeError):
    ...

class PromptNotFoundError(RuntimeError):
    ...

class QuietHoursError(RuntimeError):
    ...


class MaxRetries(TypedDict, total=False):
    task_loop: int
    task_error: int
    action_loop: int
    read_loop: int
    # vpn_connect: int

class ArticleIdInfo(TypedDict):
    clubid: str
    articleid: str
    title: str
    contents: list[str]
    comments: list[str]
    created_at: str


def randint(value: int | str) -> int:
    if '~' in str(value):
        try: return random.randint(*map(safe_int, value.split('~', 1)))
        except: return 0
    return safe_int(value)


def safe_int(value: int | str) -> int:
    try: return int(value)
    except: return 0


def to_seconds(value: int | str) -> int:
    if isinstance(value, str):
        if ':' in value:
            seconds = 0
            for i, part in enumerate(value.split(':')[:3]):
                seconds += safe_int(part) * (60**(2-i))
            return seconds
    return safe_int(value)


def seconds_to_hhmmss(seconds: float) -> str:
    total = int(round(seconds))
    m, s = divmod(total, 60)
    h, m = divmod(m, 60) if m >= 60 else (0, m)
    return (f"{h:02d}:" if h > 0 else str()) + f"{m:02d}:{s:02d}"


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


###################################################################
####################### Task Config - Farmer ######################
###################################################################

class Config(TypedDict):
    no: int
    userid: str
    passwd: str
    ip_addr: str
    dst_cafe_id: str
    dst_menu_id: str
    dst_cafe: str
    dst_menu: str
    src_cafe_id: str
    src_menu_id: str
    src_cafe: str
    src_menu: str
    read_count: str
    comment_count: str
    comment_delay: int
    daily_comment_limit: int
    article_count: str
    article_delay: int
    daily_article_limit: int
    like_count: str
    min_line_limit: int
    comment_length: str
    read_articles_commented_only: bool
    reply_yn: bool
    visit_limit: int
    comment_limit: int
    last_active_ts: dt.datetime
    total_visit_count: int
    total_article_count: int
    total_comment_count: int
    error_delay: int

class ActionCount(TypedDict):
    read: int
    comment: int
    article: int
    like: int

class ActionDelay(TypedDict):
    visit: int
    comment: int
    article: int
    error: int

class ActionLimit(TypedDict):
    visit: int
    comment: int
    daily_comment: int
    daily_article: int
    min_line: int

class WordLength(TypedDict):
    # title: str
    # contents: str
    comment: str

class ActionStatus(TypedDict):
    done: bool
    qualified: bool


class CafeInfo(AttrDict):
    def __init__(self, id: str, name: str, menu_id: str, menu: str):
        self.id = id
        self.name = name
        self.menu_id = menu_id
        self.menu = menu

class CafePair(AttrDict):
    def __init__(self, dst: CafeInfo, src: CafeInfo):
        self.dst = dst if isinstance(dst, CafeInfo) else CafeInfo(**dst)
        self.src = src if isinstance(src, CafeInfo) else CafeInfo(**src)


class ConfigWrapper(AttrDict):

    def __init__(self, config: Config = dict(), **kwargs):
        super().__init__()
        self.no = config["no"]
        self.userid = config["userid"]
        self.passwd = config["passwd"]
        self.ip_addr = config["ip_addr"]
        self.cafe: CafePair = CafePair(
            dst = {
                "id": config["dst_cafe_id"], "name": config["dst_cafe"],
                "menu_id": config["dst_menu_id"], "menu": config["dst_menu"]},
            src = {
                "id": config["src_cafe_id"], "name": config["src_cafe"],
                "menu_id": config["src_menu_id"], "menu": config["src_menu"]},
        )

        counter: ActionCount = {key[:-len("_count")]: randint(config[key]) for key in config.keys() if key.endswith("_count")}
        self.counter, self.__counter = counter.copy(), counter.copy()

        self.delay: ActionDelay = {key[:-len("_delay")]: to_seconds(config[key]) for key in config.keys() if key.endswith("_delay")}
        self.limit: ActionLimit = {key[:-len("_limit")]: safe_int(config[key]) for key in config.keys() if key.endswith("_limit")}
        self.length: WordLength = {key[:-len("_length")]: config[key] for key in config.keys() if key.endswith("_length")}
        self.read_articles_commented_only: bool = config["read_articles_commented_only"]
        self.reply_yn: bool = config["reply_yn"]

        self.__log: TaskLog = TaskLog(config)
        self.__status: ActionStatus = dict(done=None, qualified=None)
        self.__timer: ActionTimer = ActionTimer()

    @property
    def done(self) -> bool:
        if not self.__status["done"]:
            self.__status["done"] = (
                    self.qualified
                and (self.counter["comment"] == 0)
                and (self.counter["like"] == 0)
                and ((self.counter["article"] == 0)))
            return self.__status["done"]
        else:
            return True

    @property
    def qualified(self) -> bool:
        return self.__status["qualified"]

    @property
    def log(self) -> TaskLog:
        return self.__log

    @property
    def timer(self) -> ActionTimer:
        return self.__timer

    def get_initial_count(self, key: Literal["read", "article", "comment", "like"]) -> int:
        return self.__counter.get(key, 0)

    def calc_counter(self, key: Literal["read", "article", "comment", "like"]) -> int:
        return self.__counter.get(key, 0) - self.counter.get(key, 0)

    def reset_counter(self, key: Literal["all", "read", "article", "comment", "like"]):
        if key == "all":
            self.counter[key] = {key: self.__counter.get(key, 0) for key in self.counter.keys()}
        else:
            self.counter[key] = self.__counter.get(key, 0)

    def sub_counter(self, key: Literal["all", "read", "article", "comment", "like"]):
        if key == "all":
            self.counter[key] = {key: (self.counter[key] - 1) for key in self.counter.keys()}
        else:
            self.counter[key] = self.counter[key] - 1

    def zero_counter(self, key: Literal["all", "read", "article", "comment", "like"]):
        if key == "all":
            self.counter[key] = {key: 0 for key in self.counter.keys()}
            self.__status["done"] = True
        else:
            self.counter[key] = 0

    def qualify(self):
        if self.__status["qualified"] is False:
            self.reset_counter("article")
        self.__status["qualified"] = True

    def disqualify(self):
        if self.__status["qualified"] is not False:
            self.zero_counter("article")
        self.__status["qualified"] = False

    def public_items(self) -> dict:
        filter_private = (lambda _ConfigWrapper = None, **kwargs: kwargs)
        return filter_private(**self)


class ActionThreshold(AttrDict):

    def __init__(self, comment: float = 0.3, like: float = 0.4, write: float = 0.5):
        super().__init__()
        self.comment = comment
        self.like = like
        self.write = write


###################################################################
######################## Task Log - Farmer ########################
###################################################################

Index = TypeVar("Index", bound=int)
UserId = TypeVar("UserId", bound=str)
StopTask = TypeVar("StopTask", bound=bool)

class ArticleActivity(TypedDict):
    title: str
    contents: list[str]
    comments: list[str]
    created_at: str
    written_comment: Comment
    like_this: bool


ErrorFlag = Literal[
    "Chrome 프로필 없음", "adb 실행 오류", "IP주소 변경 실패",
    # "VPN 로그인 오류", "VPN 사용중", "VPN 접속 오류", "VPN 확인 불가", "VPN 조작 오류",
    "네이버 계정 불일치", "네이버 계정 보호조치", "네이버 CAPTCHA 발생", "네이버 로그인 오류",
    "카페 비회원", "카페 활동정지", "반복 횟수 초과", "프롬프트 없음", "실행 금지 시간",
    "브라우저 조작 오류", "알 수 없는 오류", "오류 횟수 초과"]

class ErrorLog(TypedDict):
    type: str
    message: str
    exc_info: str
    flag: ErrorFlag | None


class TaskLog(AttrDict):

    def __init__(self, config: Config = dict()):
        self.last_active_ts: dt.datetime | None = config.get("last_active_ts")
        self.time_on_cafe: float | None = config.get("time_on_cafe")
        self.user_info: ActionLog = dict(
            total = dict(
                visit = config.get("total_visit_count"),
                article = config.get("total_article_count"),
                comment = config.get("total_comment_count"),
            ),
            today = dict(),
        )
        self.read_ids: dict[Literal["dst", "src"], set[ArticleId]] = dict(dst=set(), src=set())
        self.read_articles: list[ArticleActivity] = list()
        self.my_articles: deque[ArticleInfo] = deque()
        self.written_articles: list[NewArticle] = list()
        self.written_replies: list[Replies] = list()
        self.total_steps: int = 0
        self.errors: list[ErrorLog] = list()

    def to_json(self, ellipsis_list: bool = False) -> dict:
        def serialize(kv: tuple[str, Any]) -> tuple[str, Any]:
            if kv[0] in "last_active_ts":
                ts = (kv[1].strftime("%Y-%m-%dT%H:%M:%S")+"+09:00") if isinstance(kv[1], dt.datetime) else None
                return kv[0], ts
            elif kv[0] == "read_ids":
                return kv[0], dict(dst=','.join(kv[1]["dst"]), src=','.join(kv[1]["src"]))
            elif kv[0] in ("read_articles", "my_articles", "written_articles"):
                return kv[0], len(kv[1]) if ellipsis_list else list(kv[1])
            else:
                return kv
        return dict(map(serialize, self.items()))


class LogTableRow(TypedDict):
    no: int
    userid: str
    cafe_name: str
    menu_name: str
    ip_addr: str
    last_active_ts: dt.datetime
    time_on_cafe: str
    total_visit_count: int
    total_article_count: int
    total_comment_count: int
    today_article_count: int
    today_comment_count: int
    read_ids: str
    read_articles: int
    new_article_count: int
    new_comment_count: int
    new_reply_count: int
    new_like_count: int
    total_steps: int
    error_flag: ErrorFlag


###################################################################
###################### Task Executor - Farmer #####################
###################################################################

AFTER, BEFORE, IN_LOOP = "after", "before", "in_loop"

class Farmer(BrowserController):

    def __init__(
            self,
            configs: WorksheetConnection | Sequence[Config],
            profiles_path: str | Path,
            openai_key:  str | Path | Literal[":default:"] = ":default:",
            adb_path: str | Path | None = None,
            device: str = str(),
            headless: bool = True,
            clear_data: bool = False,
            action_delay: Delay = (0.3, 0.6),
            goto_delay: Delay = (1, 3),
            reload_delay: Delay = (10, 12),
            upload_delay: Delay = (2, 4),
            quiet_time: QuiteTime = dict(),
            mobile: bool = True,
            comment_threshold: float = 0.3,
            like_threshold: float = 0.4,
            write_threshold: float = 0.4,
            dst_wpm: Wpm = dict(),
            src_wpm: Wpm = dict(),
            # vpn_config: VpnConfig = dict(),
            write_config: WorksheetConnection = dict(),
            slack_config: SlackConfig = dict(),
            **kwargs
        ):
        super().__init__(None, "Default",
            device, headless, clear_data, action_delay, goto_delay, reload_delay, upload_delay)
        self.profiles_path: Path = Path(profiles_path) if profiles_path else Path()
        self.adb_path: Path | None = Path(adb_path) if adb_path else None

        self.set_quite_time(quiet_time)
        self.check_quiet_time()

        if isinstance(configs, dict):
            self.validate_worksheet_connection(configs, empty=False)
            self.configs = self.read_configs_from_gsheets(**configs)
        elif isinstance(configs, Sequence):
            self.configs = [ConfigWrapper(config) for config in configs]
        else:
            raise ValueError("설정이 올바르지 않습니다.")

        set_api_key(DEFAULTS["openai_key"] if is_default(openai_key) else openai_key)

        self.index: Index = 0
        self.mobile = mobile
        self.threshold = ActionThreshold(comment_threshold, like_threshold, write_threshold)
        self.wpm: dict[Literal["dst", "src"], Wpm] = dict(dst=dst_wpm, src=src_wpm)
        self.original_articles: set[tuple[CafeId, ArticleId]] = set()

        self.slack = SlackClient(**slack_config) if slack_config else None
        # self.set_vpn_client(vpn_config)

        self.validate_worksheet_connection(write_config, empty=True)
        self.write_config = write_config

    @property
    def config(self) -> ConfigWrapper:
        return self.configs[self.index]

    @property
    def log(self) -> TaskLog:
        return self.configs[self.index].log

    @property
    def profile(self) -> dict:
        return {"path": (self.profiles_path / self.config.userid), "dir": "Default"}

    @property
    def delays2(self) -> dict[str,Delay]:
        return self.delays.get_delays(["action", "goto"])

    @property
    def delays3(self) -> dict[str,Delay]:
        return self.delays.get_delays(["action", "goto", "upload"])

    def set_quite_time(self, quiet_time: QuiteTime = dict()):
        if quiet_time:
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
            num_my_articles: int = 10,
            max_read_length: int = 500,
            max_reply_length: int = 100,
            reload_start_step: int = 10,
            reply_cutoff_date: dt.date | str | Literal["today", "yesterday"] = "today",
            task_delay: float = 30.,
            action_delay: float = 600.,
            # vpn_delay: float = 5.,
            verbose: int | str | Path = 0,
            dry_run: bool = False,
            save_log: bool = True,
            **kwargs
        ):
        self.check_quiet_time()
        self.init_adb_server()
        self.notify_task_loop(loop_step=1, task_flag="시작")

        # if self.vpn_enabled:
        #     self.vpn.start_process(self.vpn_config.force_restart)
        #     if not self.vpn.try_login(**self.vpn_config.login):
        #         self.vpn.restart_service(**self.vpn_config.login)

        stop_task: StopTask = None
        reply_cutoff_date = self.get_cutoff_date(reply_cutoff_date)

        for step in range(1, (max_retries.get("task_loop") or 30)+1):
            if stop_task or all([config.done for config in self.configs]):
                self.notify_task_loop(step, task_flag=("실패" if stop_task else "완료"))
                break

            if isinstance(stop_task, bool):
                self.wait_task_loop(step, task_delay, verbose)

            stop_task = self.task_loop(
                step, max_retries, num_my_articles, max_read_length, max_reply_length, reload_start_step,
                reply_cutoff_date, action_delay, verbose, dry_run, save_log)

    def get_cutoff_date(self, cutoff_date: dt.date | str | Literal["today", "yesterday"] = "today") -> dt.date:
        if isinstance(cutoff_date, str):
            if cutoff_date == "today":
                return dt.date.today()
            elif cutoff_date == "yesterday":
                return dt.date.today() - dt.timedelta(days=1)
            else:
                return dt.datetime.strptime(cutoff_date, "%Y-%m-%d").date()
        else:
            return cutoff_date if isinstance(cutoff_date, dt.date) else dt.date.today()

    def wait_task_loop(self, loop_step: int, task_delay: float = 30., verbose: int | str | Path = 0):
        delays = [delay for key in ["comment", "article", "error"]
            if isinstance(delay := self.min_action_delay(key), float)]
        min_delay = min(delays) if delays else 0.

        wait_delay = max(task_delay, min_delay)
        self.print_loop("task_loop_wait", loop_step, verbose, seconds=wait_delay)
        self.notify_task_loop(loop_step, task_flag="대기", wait_delay=wait_delay)
        wait(wait_delay)

    def min_action_delay(self, key: Literal["comment", "article", "error"]) -> float | None:
        delays = [max(0., config.delay[key] - elapsed)
            for config in self.configs
                if ((key in config.delay)
                    and (config.counter.get(key, 1) > 0)
                    and isinstance(elapsed := config.timer.get_elapsed_time(key), float))]
        return min(delays) if delays else None

    ############################# <start> #############################
    ############################ Task Loop ############################

    def task_loop(
            self,
            loop_step: int,
            max_retries: MaxRetries = dict(),
            num_my_articles: int = 10,
            max_read_length: int = 500,
            max_reply_length: int = 100,
            reload_start_step: int = 10,
            reply_cutoff_date: dt.date | None = None,
            action_delay: float = 600.,
            # vpn_delay: float = 5.,
            verbose: int | str | Path = 0,
            dry_run: bool = False,
            save_log: bool = True,
        ) -> StopTask:
        stop_task = False
        max_task_error = max_retries.get("task_error") or 10
        # vpn_ip, max_vpn_retries = None, max_retries.get("vpn_connect") or 10

        for i in range(len(self.configs)):
            self.index = i
            error_flag = None

            if self.config.done:
                continue
            elif stop_task:
                self.print_loop("task_loop_break", loop_step, verbose)
                break

            action_started_at = time.monotonic() if loop_step > 1 else None
            try:
                self.print_loop("task_loop_start", loop_step, verbose)
                self.check_quiet_time()

                if self.is_mobile_proxy(self.config.ip_addr):
                    self.run_mobile_proxy()
                proxy = self.resolve_proxy(self.config.ip_addr)

                # if self.vpn_enabled and (target_ip := self.config.ip_addr):
                #     vpn_ip = self.ensure_vpn_connected(target_ip, vpn_ip, max_vpn_retries, vpn_delay)

                self.do_actions(
                    loop_step, max_retries, num_my_articles, max_read_length, max_reply_length,
                    reload_start_step, reply_cutoff_date, verbose, dry_run, proxy=proxy)
                self.config.timer.end_timer("error")
            except Exception as error:
                try:
                    error_flag = self.handle_error_task(error, max_task_error)
                except Exception:
                    error_flag = "알 수 없는 오류"
            finally:
                if all(config.done for config in self.configs[i+1:]):
                    action_started_at = None
                stop_task = self.finalize_task(
                    loop_step, action_delay, action_started_at, error_flag, verbose, save_log)

        return stop_task

    def handle_error_task(self, error: Exception, max_task_error: int = 10) -> ErrorFlag:
        self.config.timer.start_timer("error")
        error_flag = self.get_error_flag(error)

        self.log.errors.append(dict(
            type = str(type(error).__name__),
            message = self.get_error_msg(error),
            exc_info = '\n'.join(traceback.format_exception(*sys.exc_info())),
            flag = error_flag,
        ))

        if len(self.log.errors) > max_task_error:
            return "오류 횟수 초과"
        return error_flag

    def finalize_task(
            self,
            loop_step: int,
            action_delay: float = 600.,
            action_started_at: float | None = None,
            error_flag: str | None = None,
            verbose: int | str | Path = 0,
            save_log: bool = True,
        ) -> StopTask:
        try:
            self.log.last_active_ts = dt.datetime.now()
            self.log.time_on_cafe = (self.log.time_on_cafe or 0.) + (self.config.timer.end_timer("visit", 3) or 0.)
        except Exception:
            pass

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
        if (action_delay > 0.) and (loop_step > 1) and isinstance(action_started_at, float):
            if not (self.config.done or stop_task):
                wait_delay = max(0., action_delay - (time.monotonic() - action_started_at))

        try:
            action_flag = "실패" if error_flag else ("완료" if self.config.done else "대기")
            self.notify_action_loop(loop_step, action_flag, error_flag, wait_delay)
        except Exception:
            pass

        if isinstance(wait_delay, float) and (wait_delay > 0.):
            wait(wait_delay)

        return stop_task

    ###################### Single account actions #####################

    @BrowserController.with_chrome_profile
    def do_actions(
            self,
            loop_step: int,
            max_retries: MaxRetries = dict(),
            num_my_articles: int = 10,
            max_read_length: int = 500,
            max_reply_length: int = 100,
            reload_start_step: int = 10,
            reply_cutoff_date: dt.date | None = None,
            verbose: int | str | Path = 0,
            dry_run: bool = False,
            **kwargs
        ):
        self.notify_playwright_proxy(**kwargs)
        self.navigate_to_menu()
        self.config.timer.start_timer("visit")
        write_timing = self.get_write_timing(num_my_articles)
        self.notify_action_loop(loop_step, action_flag="시작")

        max_action_steps = max_retries.get("action_loop") or 100
        max_read_steps = max_retries.get("read_loop") or 100
        common = (max_read_length, reload_start_step, verbose, dry_run)

        if (write_timing == BEFORE) and self.has_next_article():
            self.read_src_cafe_and_write_dst_cafe(max_read_steps, *common)

        is_article_allowed = (write_timing == IN_LOOP)
        self.action_loop(max_action_steps, is_article_allowed, *common)

        if (write_timing == AFTER) and self.has_next_article():
            self.read_src_cafe_and_write_dst_cafe(max_read_steps, *common)

        if self.config.reply_yn:
            self.reply_my_articles(reply_cutoff_date, max_reply_length, verbose, dry_run)

        qualified = self.check_action_log(total_only=False)
        if (not self.config.qualified) and qualified:
            self.config.qualify()

            if self.config.cafe.src.name:
                self.read_src_cafe_and_write_dst_cafe(max_read_steps, *common)

    ###################### Read and write article #####################

    def read_src_cafe_and_write_dst_cafe(
            self,
            max_steps: int = 100,
            max_read_length: int = 500,
            reload_start_step: int = 10,
            verbose: int | str | Path = 0,
            dry_run: bool = False,
        ) -> NewArticle:
        self.navigate_to_menu(referer="cafe", target="src")
        articles = self.read_loop(max_steps, max_read_length, reload_start_step, verbose)

        self.navigate_to_menu(referer="cafe", target="dst")
        return self.copy_and_write_article(articles, verbose, dry_run)

    def get_write_timing(
            self,
            num_my_articles: int = 10,
        ) -> Literal["after", "before", "in_loop"] | None:
        if self.config.qualified or self.check_action_log(total_only=bool(self.log.user_info["today"])):
            self.config.qualify()

            if self.need_my_articles((n := num_my_articles)):
                self.log.my_articles = deque(self.read_my_articles(n), maxlen=n)

            if self.config.cafe.src.name:
                return AFTER if random.uniform(0, 1) > self.threshold.write else BEFORE
            else:
                return IN_LOOP
        else:
            self.config.disqualify()
            return None

    def copy_and_write_article(
            self,
            articles: list[ArticleIdInfo],
            verbose: int | str | Path = 0,
            dry_run: bool = False,
        ) -> ModifiedArticle:
        filter_ids = (lambda clubid = None, articleid = None, **kwargs: kwargs)
        data = [filter_ids(**article) for article in articles]
        new_article: ModifiedArticle = self.write_article(data, "modify", verbose, dry_run)

        if isinstance(new_article.get("origin"), int):
            origin = articles[new_article["origin"]]
            self.original_articles.add((origin["clubid"], origin["articleid"]))

        return new_article

    ############################# <start> #############################
    ############################ Read Loop ############################

    def read_loop(
            self,
            max_steps: int = 100,
            max_read_length: int = 500,
            reload_start_step: int = 10,
            verbose: int | str | Path = 0,
        ) -> list[ArticleIdInfo]:
        articles, step, unselected_steps = list(), 1, 0
        self.config.reset_counter("read")
        src_read_ids = set()

        for step in range(1, max_steps+1):
            self.check_quiet_time()

            if step > reload_start_step:
                wait(self.delays.reload)
                reload_articles(self.page, self.delays.goto)
            elif step > 2:
                next_articles(self.page, self.delays.action)

            selected = explore_articles(
                self.page, src_read_ids, self.get_prompt("sample_articles", "src"), verbose) # Action 3
            self.log.read_ids["src"].update(src_read_ids)
            for params in selected:
                self.check_quiet_time()
                if (params["clubid"], params["articleid"]) in self.original_articles:
                    continue

                if goto_article(self.page, params["articleid"], self.delays.goto): # Action 4
                    try:
                        article = self.read_full_article(max_read_length, verbose) # Action 5
                        if article:
                            articles.append(dict(article, clubid=params["clubid"], articleid=params["articleid"]))
                            self.log.read_articles.append(article)
                    except:
                        pass
                    finally:
                        go_back(self.page, self.delays.goto)

                if self.config.counter["read"] < 1:
                    return articles

            read_ids = ','.join([param["articleid"] for param in selected])
            self.print_loop("read_loop_end", step, verbose, read_ids=read_ids)

            if (step > reload_start_step) and (not selected):
                wait(max(10, (unselected_steps := unselected_steps + 1)))
            else:
                unselected_steps = 0

            self.log.total_steps += 1

        return articles

    ############################# Action 5 ############################

    def read_full_article(self, max_read_length: int = 500, verbose: int | str | Path = 0) -> ArticleInfo | None:
        contents = read_article(self.page, contents_only=True)
        if contents["word_count"] > max_read_length:
            return None
        elif len(contents["lines"]) < self.config.limit["min_line"]:
            return None
        elif len([content for content in contents["lines"] if content.startswith("![")]) != 0:
            return None
        else:
            article = read_full_article(self.page, self.delays.action, self.wpm["src"], verbose)
            self.notify_read_action(article, target="src")
            self.config.sub_counter("read")
            return article

    ############################ Read Loop ############################
    ############################## <end> ##############################

    ############################# <start> #############################
    ########################## Reaction Loop ##########################

    def action_loop(
            self,
            max_steps: int = 100,
            is_article_allowed: bool = False,
            max_read_length: int = 500,
            reload_start_step: int = 10,
            verbose: int | str | Path = 0,
            dry_run: bool = False,
        ):
        articles, step, unselected_steps = list(), 1, 0
        self.config.reset_counter("read")

        for step in range(1, max_steps+1):
            self.check_quiet_time()

            if not self.has_next_action(is_article_allowed):
                break

            if step > reload_start_step:
                wait(self.delays.reload)
                reload_articles(self.page, self.delays.goto)
            elif step > 2:
                next_articles(self.page, self.delays.action)

            selected = explore_articles(
                self.page, self.log.read_ids["dst"], self.config.read_articles_commented_only,
                self.get_prompt("select_articles", "dst"), verbose) # Action 3
            for params in selected:
                self.check_quiet_time()

                if goto_article(self.page, params["articleid"], self.delays.goto): # Action 4
                    try:
                        activity = self.read_and_react(max_read_length, verbose, dry_run)
                        if activity:
                            keys = ["title", "contents", "comments", "created_at"]
                            articles.append({key: activity[key] for key in keys})
                    finally:
                        go_back(self.page, self.delays.goto)

                if is_article_allowed and self.is_article_allowed():
                    self.write_article(articles, "create", verbose, dry_run)

            read_ids = ','.join([param["articleid"] for param in selected])
            self.print_loop("action_loop_end", step, verbose, read_ids=read_ids)

            if (step > reload_start_step) and (not selected):
                wait(max(10, (unselected_steps := unselected_steps + 1)))
            else:
                unselected_steps = 0

            self.log.total_steps += 1

    def get_prompt(self, file_name: str, target: Literal["dst", "src"] = "dst", **replacements: str) -> dict:
        dst, src = self.config.cafe.dst, self.config.cafe.src
        replacements = dict(replacements, dst_cafe=dst.name, dst_menu=dst.menu, src_cafe=src.name, src_menu=src.menu)

        cafe = src if target == "src" else dst
        cafe_root = os.path.join(PROMPTS_ROOT, cafe.name)
        cafe_menu_root = os.path.join(cafe_root, cafe.menu)

        for root in [cafe_menu_root, cafe_root, PROMPTS_ROOT]:
            markdown_path = os.path.join(root, f"{file_name}.md")
            if os.path.exists(markdown_path):
                return dict(markdown_path=markdown_path, replacements=replacements)
        raise PromptNotFoundError(f"'{file_name}' 프롬프트가 존재하지 않습니다.")

    ########################### Action 0+1+2 ##########################

    def navigate_to_menu(
            self,
            referer: Literal["cafe"] | None = None,
            target: Literal["dst", "src"] = "dst",
        ):
        if referer == "cafe":
            return_to_cafe_home(self.page, self.mobile, self.delays.goto)
        else:
            goto_cafe_home(self.page, self.mobile, **self.delays2) # Action 0
        wait(self.delays.goto)

        cafe = self.config.cafe.src if target == "src" else self.config.cafe.dst
        try:
            goto_cafe(self.page, cafe.name, self.delays.goto), wait(self.delays.goto) # Action 1
            goto_menu(self.page, cafe.menu, **self.delays2), wait(self.delays.goto) # Action 2
        except (Exception if target == "src" else CafeNotLoadedError) as error:
            try:
                goto_cafe_url(self.page, cafe.id, cafe.menu_id, self.mobile, self.delays.goto)
            except:
                raise error

        self.notify_cafe_switch(target)

    ############################# Action 9 ############################

    def check_action_log(self, total_only: bool = False) -> bool:
        qualified = True
        action_log = read_action_log(self.page, total_only, self.log.my_articles, **self.delays2)
        total, today = action_log["total"], action_log["today"]

        if today is not None:
            group_key = (self.config.userid, self.config.cafe.dst.name)
            confirmed_articles = sum(len(config.log.written_articles) for config in self.configs
                if (config.userid, config.cafe.dst.name) == group_key)
            today["article"] = max(today["article"], confirmed_articles)

        self.sync_action_log(total, today)

        for key, count in action_log["total"].items():
            if self.config.limit.get(key):
                qualified &= (self.config.limit[key] <= count)

        if today is not None:
            for key in ["article", "comment"]:
                daily_limit = self.config.limit[f"daily_{key}"]
                if daily_limit and (daily_limit <= today[key]):
                    self.config.zero_counter(key)
                if key not in self.config.timer:
                    self.config.timer.set_timer(key, today[f"last_{key}_ts"])

        return qualified

    def sync_action_log(self, total: TotalCount, today: TodayCount | None = None):
        group_key = (self.config.userid, self.config.cafe.dst.name)
        for config in self.configs:
            if (config.userid, config.cafe.dst.name) == group_key:
                config.log.user_info["total"] = total.copy()
                if today is not None:
                    config.log.user_info["today"] = today.copy()

    def read_my_articles(self, n: int = 10) -> list[ArticleInfo]:
        open_info(self.page, **self.delays2)
        try:
            return read_my_articles(self.page, self.delays.goto, n) # Action 9
        finally:
            close_info(self.page, self.delays.goto)

    ######################### Action Condition ########################

    def has_next_action(self, is_article_allowed: bool = False) -> bool:
        return (self.has_next_like()
            or self.has_next_comment()
            or (is_article_allowed and self.has_next_article()))

    def has_next_article(self) -> bool:
        if self.config.limit["daily_article"] < self.config.log.user_info["today"]["article"]:
            self.config.zero_counter("article")
        return ((self.config.counter["article"] > 0)
            and self.config.timer.gte("article", self.config.delay["article"]))

    def is_article_allowed(self) -> bool:
        return ((self.config.counter["read"] < 1) and self.has_next_article())

    def has_next_comment(self) -> bool:
        if self.config.limit["daily_comment"] < self.config.log.user_info["today"]["comment"]:
            self.config.zero_counter("comment")
        return ((self.config.counter["comment"] > 0)
            and self.config.timer.gte("comment", self.config.delay["comment"]))

    def is_comment_allowed(self) -> bool:
        return (self.has_next_comment() and (random.uniform(0, 1) > self.threshold.comment))

    def has_next_like(self) -> bool:
        return (self.config.counter["like"] > 0)

    def is_like_allowed(self) -> bool:
        return (self.has_next_like() and (random.uniform(0, 1) > self.threshold.like))

    def need_my_articles(self, num_my_articles: int = 10) -> bool:
        return ((num_my_articles > 0)
            and (self.config.counter["article"] > 0)
            and (self.log.user_info["total"]["article"] > 0)
            and (not self.log.my_articles))

    ########################### Action 5+6+7 ##########################

    def read_and_react(
            self,
            max_read_length: int = 500,
            verbose: int | str | Path = 0,
            dry_run: bool = False,
        ) -> ArticleActivity | None:
        word_count = read_article(self.page, contents_only=True)["word_count"]
        if word_count > max_read_length:
            return None
        else:
            comment: Comment = None
            common = dict(page=self.page, wpm=self.wpm["dst"], verbose=verbose)

        if self.is_comment_allowed():
            article, comment = read_article_and_write_comment(
                **common,
                prompt = self.get_prompt("create_comment", "dst",
                    comment_limit = (self.config.length.get("comment") or "20자 이내")),
                dry_run = dry_run,
                **self.delays3,
            ) # Action 5+7 & Agent 2
            if comment:
                self.config.sub_counter("comment")
                self.config.timer.start_timer("comment")
                self.notify_comment_action(article, comment)
        else:
            article = read_full_article(**common, action_delay=self.delays.action) # Action 5
        self.config.sub_counter("read")
        article["written_comment"] = comment

        if self.is_like_allowed():
            if not dry_run:
                like_article(self.page, self.delays.action) # Action 6
            self.config.sub_counter("like")
            article["like_this"] = True
            self.notify_like_action(article)
        else:
            article["like_this"] = False

        if not (article["written_comment"] or article["like_this"]):
            self.notify_read_action(article, target="dst")

        self.log.read_articles.append(article)

        return article

    ############################# Action 8 ############################

    def write_article(
            self,
            articles: list[ArticleInfo],
            action: Literal["create", "modify"] = "create",
            verbose: int | str | Path = 0,
            dry_run: bool = False,
            update: bool = True,
        ) -> NewArticle | ModifiedArticle:
        info, new = dict(title=str(), contents=list(), comments=list(), created_at=str()), dict()

        replacements = dict()
        if action == "create":
            replacements = dict(
                title_limit = (self.config.length.get("title") or "30자 이내"),
                contents_limit = (self.config.length.get("contents") or "300자 이내"),
            )

        try:
            new = (update_article if action == "modify" else write_article)(
                page = self.page,
                articles = articles,
                my_articles = self.log.my_articles,
                prompt = self.get_prompt(f"{action}_article", "dst", **replacements),
                verbose = verbose,
                dry_run = dry_run,
                **self.delays3,
            ) # Action 8
            url = copy_article_url(self.page, self.delays.action) if not dry_run else None
            self.notify_article_action(new, url)
        finally:
            go_back(self.page, self.delays.goto)
        self.config.timer.start_timer("article")

        self.config.reset_counter("read")
        self.config.sub_counter("article")
        self.log.written_articles.append(new)

        if self.log.my_articles.maxlen and update:
            for key in ["title", "contents", "created_at"]:
                if key in new:
                    info[key] = new[key]
            self.log.my_articles.appendleft(info)
        return new

    ########################## Reaction Loop ##########################
    ############################## <end> ##############################

    ############################ Action 10 ############################

    def reply_my_articles(
            self,
            cutoff_date: dt.date | None = None,
            max_reply_length: int = 100,
            verbose: int | str | Path = 0,
            dry_run: bool = False,
        ) -> list[Replies]:
        replies = list()

        open_info(self.page, **self.delays2)
        try:
            replies = reply_my_articles(
                page = self.page,
                cutoff_date = (cutoff_date if isinstance(cutoff_date, dt.date) else dt.date.today()),
                max_reply_length = max_reply_length,
                **self.delays3,
                prompt = self.get_prompt("create_replies", "dst"),
                verbose = verbose,
                dry_run = dry_run,
            )
            self.notify_reply_action(replies)
        finally:
            close_info(self.page, self.delays.goto)

        if replies:
            self.log.written_replies += replies
        return replies

    ############################ Task Loop ############################
    ############################## <end> ##############################

    ########################### Handle Error ##########################

    def get_error_msg(self, error: Exception) -> str:
        try:
            return str(error) or None
        except:
            return None

    def get_error_flag(self, error: Exception) -> ErrorFlag:
        if isinstance(error, ProfileNotFoundError):
            return "Chrome 프로필 없음"
        elif isinstance(error, AdbRuntimeError):
            return "adb 실행 오류"
        elif isinstance(error, IpAddrNotChangedError):
            return "IP주소 변경 실패"
        # elif isinstance(error, VpnRuntimeError):
        #     if isinstance(error, VpnLoginFailedError):
        #         return "VPN 로그인 오류"
        #     elif isinstance(error, VpnInUseError):
        #         return "VPN 사용중"
        #     elif isinstance(error, VpnFailedError):
        #         return "VPN 접속 오류"
        #     else:
        #         return "VPN 오류"
        # elif isinstance(error, WindowNotFoundError):
        #     return "VPN 확인 불가"
        # elif isinstance(error, ElementNotFoundError):
        #     return "VPN 조작 오류"
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
            cafe_name = regexp_extract(r"'([^']+)'", str(error), default="확인불가")
            return f"카페 비회원: {cafe_name}"
        elif isinstance(error, CafeBannedError):
            return "카페 활동정지"
        elif isinstance(error, MaxLoopExceeded):
            return "반복 횟수 초과"
        elif isinstance(error, PromptNotFoundError):
            return "프롬프트 없음"
        elif isinstance(error, QuietHoursError):
            return "실행 금지 시간"
        elif isinstance(error, PlaywrightTimeoutError):
            return "브라우저 조작 오류"
        else:
            return "알 수 없는 오류"

    def handle_error_flag(self, error_flag: ErrorFlag) -> StopTask:
        if not isinstance(error_flag, str):
            return False

        elif error_flag in ("adb 실행 오류", "IP주소 변경 실패"):
            return True

        # elif error_flag == "VPN 사용중":
        #     try:
        #         self.vpn.restart_service(**self.vpn_config.login)
        #         self.config.zero_counter("all")
        #         return False
        #     except:
        #         return True

        elif error_flag.startswith("네이버") or (error_flag == "Chrome 프로필 없음"):
            userid = self.config.userid
            for config in self.configs:
                if config.userid == userid:
                    config.zero_counter("all")
            return False

        elif error_flag.startswith("카페 비회원"):
            userid = self.config.userid
            dst, src = self.config.cafe.dst.name, self.config.cafe.src.name
            cafe_name = error_flag.split(": ")[1]
            cafes = {dst, src} if cafe_name == "확인불가" else ({dst} if dst == cafe_name else {src})

            for config in self.configs:
                if ((config.userid == userid)
                    and ((config.cafe.dst.name in cafes) or (config.cafe.src.name in cafes))):
                    config.zero_counter("all")
            return False

        elif error_flag == "카페 활동정지":
            userid, cafe_name = self.config.userid, self.config.cafe.dst
            for config in self.configs:
                if (config.userid == userid) and (config.cafe.dst == cafe_name):
                    config.zero_counter("all")
            return False

        elif error_flag == "오류 횟수 초과":
            self.config.zero_counter("all")
            return False

        else:
            return error_flag in {"프롬프트 없음", "실행 금지 시간"}

    ############################# Task Log ############################

    def print_loop(self, task_step: str, loop_step: int, verbose: int | str | Path = 0, **kwargs):
        common = lambda: dict(
            index = self.index,
            userid = self.config.userid,
            cafe_name = (self.config.cafe.src if task_step == "read_loop_end" else self.config.cafe.dst).name,
            menu_name = (self.config.cafe.src if task_step == "read_loop_end" else self.config.cafe.dst).menu,
        )

        if task_step in {"action_loop_end", "read_loop_end"}:
            body = dict(
                task_step = task_step,
                loop_step = loop_step,
                **common(),
                read_ids = str(read_ids) if (read_ids := kwargs.get("read_ids")) else None,
                counter = self.config.counter,
                timer = self.config.timer.get_all_elapsed_times(ndigits=3),
            )

        elif task_step == "task_loop_start":
            body = dict(
                task_step = task_step,
                loop_step = loop_step,
                **common(),
                config = self.config.public_items(),
                state = str(state) if (state := kwargs.get("state")) else None,
            )

        elif task_step == "task_loop_wait":
            body = dict(
                task_step = task_step,
                loop_step = loop_step,
                seconds = kwargs.get("seconds"),
                timers = {i: config.timer.get_all_elapsed_times() for i, config in enumerate(self.configs)},
                delays = {i: {key: (config.delay[key] - secs)
                    for key, secs in config.timer.get_all_elapsed_times().items()
                            if (key in config.delay) and isinstance(secs, float)}
                        for i, config in enumerate(self.configs)},
            )

        else:
            body = dict(
                task_step = task_step,
                loop_step = loop_step,
                **common(),
                log = self.log.to_json(ellipsis_list=((not isinstance(verbose, int)) or (verbose < 3))),
            )

        body["timestamp"] = dt.datetime.now().isoformat(timespec="seconds")
        print_json(body, verbose)

    def save_log_json(self):
        logs_path = Path(LOGS_ROOT) / self.config.userid
        logs_path.mkdir(parents=True, exist_ok=True)
        with open(logs_path / (dt.datetime.now().strftime("%Y%m%d%H%M%S")+".json"), 'w', encoding="utf-8") as file:
            json.dump(self.log.to_json(), file, indent=2, ensure_ascii=False, default=str)

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

    def make_log_table(self) -> list[LogTableRow]:
        rows = list()
        for config in self.configs:
            log = config.log
            rows.append(dict(
                no = config.no,
                userid = config.userid,
                cafe_name = config.cafe.dst.name,
                menu_name = config.cafe.dst.menu,
                ip_addr = config.ip_addr,
                last_active_ts = log.last_active_ts,
                time_on_cafe = self.calculate_field(log, "time_on_cafe"),
                total_visit_count = log.user_info["total"].get("visit"),
                total_article_count = log.user_info["total"].get("article"),
                total_comment_count = log.user_info["total"].get("comment"),
                today_article_count = log.user_info["today"].get("article"),
                today_comment_count = log.user_info["today"].get("comment"),
                read_ids = self.calculate_field(log, "read_ids"),
                read_articles = self.calculate_field(log, "read_articles"),
                new_article_count = self.calculate_field(log, "new_article_count"),
                new_comment_count = self.calculate_field(log, "new_comment_count"),
                new_reply_count = self.calculate_field(log, "new_reply_count"),
                new_like_count = self.calculate_field(log, "new_like_count"),
                total_steps = log.total_steps,
                error_flag = self.calculate_field(log, "error_flag"),
            ))
        return rows

    def calculate_field(self, log: TaskLog, field: str) -> Any:
        if field == "time_on_cafe":
            return seconds_to_hhmmss(log.time_on_cafe) if isinstance(log.time_on_cafe, float) else None
        elif field.startswith("read_"):
            if field == "read_ids":
                return ','.join(sorted(log.read_ids["dst"].union(log.read_ids["src"]))) or None
            elif field == "read_articles":
                return len(log.read_articles)
        elif field.startswith("new_"):
            if field == "new_article_count":
                return len(log.written_articles)
            elif field == "new_comment_count":
                return len([1 for activity in log.read_articles if activity.get("written_comment")])
            elif field == "new_reply_count":
                return sum([len(replies["replies"]) for replies in log.written_replies])
            elif field == "new_like_count":
                return len([1 for activity in log.read_articles if activity.get("like_this")])
        elif field == "error_flag":
            return ", ".join([error["flag"] for error in log.errors]) if log.errors else None
        return None

    def validate_worksheet_connection(self, conn: WorksheetConnection, empty: bool = False) -> bool:
        if not isinstance(conn, dict):
            raise TypeError("구글시트 연결 정보가 올바른 타입이 아닙니다.")
        elif empty and (not conn):
            return True
        elif not (conn.get("key") and conn.get("sheet")):
            raise KeyError("구글시트 연결 정보에 'key' 또는 'sheet' 값이 없습니다.")
        return True

    def _get_credentials(self, account: str | Path | Literal[":default:"] = ":default:") -> ServiceAccount:
        return ServiceAccount(DEFAULTS["account"] if is_default(account) else str(account))

    ######################## Slack Notification #######################

    def notify_slack(self, text: str, blocks: list | None = None):
        if self.slack:
            self.slack.chat_message(text, blocks=blocks)
            # try: self.slack.chat_message(text, blocks=blocks)
            # except: pass

    @property
    def now(self) -> str:
        return dt.datetime.now().strftime("%H:%M")

    @property
    def user_md(self) -> str:
        return f"_*{self.config.userid}*_ ({progress(self.index+1, len(self.configs))})"

    @property
    def cafe_md(self) -> str:
        return f"{self.config.cafe.dst.name} / {self.config.cafe.dst.menu}"

    def notify_task_loop(
            self,
            loop_step: int,
            task_flag: Literal["시작", "대기", "완료", "실패"] | None = None,
            wait_delay: float | None = None,
            sep: str = "  ·  ",
        ):
        if loop_step > 1:
            first_line = [f"반복 횟수 {loop_step}"]
            if wait_delay:
                first_line.append(f"{format_wait_time(wait_delay)} 후 재시작")
        else:
            first_line = [f"{len(self.configs)}개 계정-카페 활동 대기"]
        text = f"[프로그램 {task_flag}]  " + sep.join(first_line + [self.now])

        rows = [[
            "순서", "번호", "아이디", "카페명", "게시판",
            "작성글 수\n(완료/할당)", "작성댓글 수\n(완료/할당)", "좋아요 수\n(완료/할당)",
            "|", "금일 작성글 수\n(완료/한도)", "금일 작성댓글 수\n(완료/한도)"
        ]]

        for i, config in enumerate(self.configs, start=1):
            rows.append([
                str(i), str(config.no), config.userid, config.cafe.dst.name, config.cafe.dst.menu,
                *[progress(self.calculate_field(config.log, f"new_{key}_count"), config.get_initial_count(key))
                    for key in ["article", "comment", "like"]],
                "|",
                *[(progress(config.log.user_info["today"].get(key), config.limit.get("daily_"+key))
                        if config.log.total_steps > 0 else '-')
                    for key in ["article", "comment"]],
            ])

        self.notify_slack(text, blocks=[self.slack.create_table(rows)])

    def notify_action_loop(
            self,
            loop_step: int,
            action_flag: Literal["시작", "대기", "완료", "실패"] | None = None,
            error_flag: str | None = None,
            wait_delay: float | None = None,
            sep: str = "  ·  ",
        ):
        config = self.config
        first_line = [self.user_md, self.cafe_md, self.now]

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
            fields = ["read_articles", "time_on_cafe", "new_reply_count"]
            for field, label in zip(fields, ["읽은 글", "체류 시간", "답글"]):
                if (value := self.calculate_field(config.log, field)):
                    second_line.append(f"{label} {value}")
        else:
            second_line = list()

        third_line = list()
        for key, label in [("article", "글"), ("comment", "댓글")]:
            if self.config.counter.get(key, 0) <= 0:
                third_line.append(f"*{label}* | 금일 할당량 완료")
                continue

            delay = self.config.delay.get(key)
            elapsed = self.config.timer.get_elapsed_time(key)
            if (elapsed is None) or (elapsed >= delay):
                third_line.append(f"*{label}* | 지금 작성 가능")
            else:
                third_line.append(f"*{label}* | {format_wait_time(delay - elapsed)} 후 작성 가능")

        if wait_delay:
            third_line.append(f"다음 활동 {format_wait_time(wait_delay)} 후")

        text = '\n'.join([
            (f"[카페 활동 {action_flag}]  " + sep.join(first_line)),
            *([bullet + sep.join(second_line)] if second_line else list()),
            f":hourglass_flowing_sand: {sep.join(third_line)}",
        ])

        rows = [["구분", "방문 수", "작성글 수\n(완료/할당)", "작성댓글 수\n(완료/할당)", "좋아요 수\n(완료/할당)"]]
        rows.append(["작업 예정", "",
            *[progress(self.calculate_field(config.log, f"new_{key}_count"), config.get_initial_count(key))
                for key in ["article", "comment", "like"]]])
        rows.append(["-----------", "", "", "", ""])
        rows.append(["일일 한도", "",
            *[progress(config.log.user_info["today"].get(key), config.limit.get("daily_"+key))
                for key in ["article", "comment"]], ""])
        rows.append(["전체 누적",
            *[config.log.user_info["total"].get(key) for key in ["visit", "article", "comment"]], ""])

        self.notify_slack(text, blocks=[self.slack.create_table(rows)])

    def notify_cafe_switch(self, target: Literal["dst", "src"], sep: str = "  ·  "):
        config = self.config
        cafe = config.cafe.src if target == "src" else config.cafe.dst
        text = sep.join([f"[카페 이동]  {self.user_md}", f"{cafe.name} / {cafe.menu}", self.now])
        self.notify_slack(text)

    def notify_article_action(
            self,
            article: ModifiedArticle | NewArticle,
            url: str | None,
            sep: str = "  ·  ",
        ):
        title = article.get("title") or "(제목 없음)"
        contents = article.get("contents") or list()

        self.notify_slack('\n'.join([
            sep.join([f"[글쓰기]  {self.user_md}", self.cafe_md, self.now]),
            f"<{url}|{title}>" if url else f"*{title}*",
            *[(">"+line) for line in contents if line.strip() and (not line.startswith("!["))],
        ]))

    def notify_comment_action(self, article: ArticleInfo, comment: Comment, sep: str = "  ·  "):
        title = article.get("title") or "(제목 없음)"
        link = article.get("copy_link")

        self.notify_slack('\n'.join([
            sep.join([f"[댓글]  {self.user_md}", self.cafe_md, self.now]),
            "글 >  {}".format(f"<{link}|{title}>" if link else f"*{title}*"),
            f">{comment}",
        ]))

    def notify_like_action(self, article: ArticleInfo, sep: str = "  ·  "):
        title = article.get("title") or "(제목 없음)"
        link = article.get("copy_link")

        self.notify_slack('\n'.join([
            sep.join([f"[좋아요]  {self.user_md}", self.cafe_md, self.now]),
            "글 >  {}".format(f"<{link}|{title}>" if link else f"*{title}*"),
        ]))

    def notify_read_action(self, article: ArticleInfo, target: Literal["src", "dst"]="dst", sep: str = "  ·  "):
        title = article.get("title") or "(제목 없음)"
        link = article.get("copy_link")
        cafe = self.config.cafe.src if target == "src" else self.config.cafe.dst

        self.notify_slack('\n'.join([
            sep.join([f"[글 읽기]  {self.user_md}", f"{cafe.name} / {cafe.menu}", self.now]),
            "글 >  {}".format(f"<{link}|{title}>" if link else f"*{title}*"),
        ]))

    def notify_reply_action(self, replies: list[Replies], sep: str = "  ·  "):
        lines, links, count = list(), set(), 0
        for article_info in replies:
            comments, replies = article_info["comments"], article_info["replies"]
            if len(comments) != len(replies): continue

            link = article_info["copy_link"]
            links.add(link)

            for comment, reply in zip(comments, replies):
                if reply:
                    lines.append("댓글 >  {}".format(f"<{link}|{comment}>" if link else f"*{comment}*"))
                    lines.append(f">{reply}")
                    count += 1

        self.notify_slack('\n'.join([
            sep.join([f"[답글]  {self.user_md}", self.cafe_md, self.now]),
            f"_총 {len(links)}개 글에서 {count}개 댓글에 답글 작성_",
            *lines,
        ]))

    def notify_playwright_proxy(self, proxy: str | None = None, sep: str = "  ·  ", **kwargs):
        if proxy:
            self.notify_slack('\n'.join([
                sep.join([f"[프록시 IP 적용]  {self.user_md}", self.now]),
                f":white_checK_mark: {proxy}",
            ]))

    #################### Mobile Tethering Extension ###################

    def is_mobile_proxy(self, ip_addr: str | None) -> bool:
        return isinstance(ip_addr, str) and (ip_addr.strip().lower() == MOBILE_IP_TOKEN)

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
                sep.join([f"[모바일 IP 변경]  {self.user_md}", self.now]),
                f":white_checK_mark: {original_ip} → {new_ip}",
            ]))
        except Exception:
            self.notify_slack('\n'.join([
                sep.join([f"[모바일 IP 변경 실패]  {self.user_md}", self.now]),
                ":x: 비행기 모드 전환 후 60초 내 IP 변경 실패",
            ]))
            raise

    ########################## VPN Extension ##########################

    # @property
    # def vpn(self) -> VpnClient:
    #     if self.__vpn is not None:
    #         return self.__vpn
    #     else:
    #         raise RuntimeError("VPN 클라이언트가 초기화되지 않았습니다.")

    # def set_vpn_client(self, vpn_config: VpnConfig = dict()):
    #     if vpn_config:
    #         config = vpn_config if isinstance(vpn_config, VpnConfig) else VpnConfig(**vpn_config)
    #         self.__vpn = VpnClient(**config)
    #         self.vpn_config = config
    #         self.vpn_enabled = True
    #     else:
    #         self.__vpn = None
    #         self.vpn_config = None
    #         self.vpn_enabled = False

    # def ensure_vpn_connected(
    #         self,
    #         target_ip: str,
    #         source_ip: str | None = None,
    #         max_vpn_retries: int = 10,
    #         vpn_delay: float = 5.,
    #     ) -> str:
    #     if source_ip:
    #         try:
    #             if target_ip == source_ip:
    #                 self.vpn.wait_for_connection(source_ip, **self.vpn_config.wait_options)
    #                 return
    #             else:
    #                 self.vpn.disconnect(**self.vpn_config.wait_options)
    #         except:
    #             self.safe_terminate_vpn()
    #         wait(vpn_delay)

    #     for step in range(1, max_vpn_retries+1):
    #         try:
    #             self.vpn.start_process(force_restart=False)
    #             self.vpn.try_login(**self.vpn_config.login)
    #             if (connected_ip := self.vpn.search_and_connect(target_ip, **self.vpn_config.connect)):
    #                 return connected_ip
    #         except VpnInUseError as error:
    #             raise error
    #         except:
    #             self.safe_terminate_vpn()
    #         wait(vpn_delay * step)
    #     raise VpnFailedError("VPN이 연결되지 않았습니다.")

    # def safe_terminate_vpn(self):
    #     try:
    #         self.vpn.terminate_process()
    #     except:
    #         pass
