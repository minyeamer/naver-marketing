from __future__ import annotations

from core.browser import BrowserController, build_launch_kwargs, STEALTH_SCRIPT
from playwright.sync_api import BrowserContext, Playwright, Page, sync_playwright

from utils.common import AttrDict

from extensions.gsheets import WorksheetClient, ServiceAccount
from extensions.adb import ensure_adb_server_ready, rotate_mobile_ip_addr
from extensions.adb import MOBILE_IP_TOKEN

from pathlib import Path
import threading
import time

from typing import get_type_hints, Sequence, TypeVar, TypedDict, TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Any, Literal
    from extensions.gsheets import WorksheetConnection

Index = TypeVar("Index", bound=int)


MAIN_URL = "https://m.naver.com"


def is_default(value: Any) -> bool:
    return isinstance(value, str) and (value == ":default:")


class Account(TypedDict):
    userid: str
    passwd: str
    ip_addr: str
    enabled: bool


class AccountWrapper(AttrDict):

    def __init__(self, account: Account = dict(), **kwargs):
        super().__init__()
        self.userid = account["userid"]
        self.passwd = account.get("passwd")
        self.ip_addr = account.get("ip_addr")
        self.enabled = account.get("enabled") or False


class ProfileManager(BrowserController):
    """Google Sheets 또는 직접 전달한 계정 목록을 순회하며
    모바일 Chrome 프로필에 네이버 수동 로그인을 지원한다.

    계정 하나만 담긴 list를 전달하면 단일 계정 수동 로그인으로도 사용할 수 있다.
    """

    def __init__(
            self,
            accounts: WorksheetConnection | Sequence[dict],
            profiles_path: str | Path,
            device: str | None = None,
            headless: bool = False,
            clear_data: bool = False,
            adb_path: str | Path | None = None,
        ):
        super().__init__(None, "Default", device, headless, clear_data)
        self.profiles_path = Path(profiles_path) if profiles_path else Path()
        self.adb_path = Path(adb_path) if adb_path else None

        if isinstance(accounts, dict):
            self.validate_worksheet_connection(accounts)
            self.accounts = self.read_accounts_from_gsheets(**accounts)
        elif isinstance(accounts, Sequence):
            self.accounts = [AccountWrapper(account) for account in accounts]
        else:
            raise TypeError("계정 정보가 올바른 타입이 아닙니다.")

        self.index: Index = 0

    @property
    def account(self) -> AccountWrapper:
        return self.accounts[self.index]

    @property
    def profile(self) -> dict:
        return {"path": (self.profiles_path / self.account.userid), "dir": "Default"}

    ########################### Entry Point ###########################

    def start(
            self,
            interrupt: bool = True,
            skip_if_logged_in: bool = True,
            wait_interval: float = 0.25,
            **kwargs
        ):
        if interrupt:
            input("시작하려면 아무 키나 눌러주세요.")

        self.init_adb_server()

        total = len(self.accounts)
        for i in range(total):
            self.index = i

            if not self.account.enabled:
                continue

            if self.is_mobile_proxy(self.account.ip_addr):
                ensure_adb_server_ready(self.adb_path)
                original_ip, new_ip = rotate_mobile_ip_addr(self.adb_path)
                print(f"[{i+1}/{total}] {self.account.userid} IP 변경: {original_ip} → {new_ip}")

            status = self.init_profile(self.profile["path"])
            print(f"[{i+1}/{total}] {self.account.userid} {status}")

            self.warmup_profile(skip_if_logged_in, interrupt, wait_interval)
            print(f"[{i+1}/{total}] {self.account.userid} 완료")

    ########################## Manage Profile #########################

    def init_profile(self, profile_path: Path) -> str:
        status = "(재사용)" if profile_path.exists() else "(새 프로필)"
        profile_path.mkdir(parents=True, exist_ok=True)
        return status

    def warmup_profile(
            self,
            skip_if_logged_in: bool = True,
            interrupt: bool = True,
            wait_interval: float = 0.25,
        ):
        if self.clear_data:
            self.clear_browsing_data()

        with sync_playwright() as playwright:
            proxy = self.resolve_proxy(self.account.ip_addr)
            context = self.launch_persistent_context(playwright, proxy=proxy)
            if skip_if_logged_in and self.is_logged_in(context):
                context.close()
                return

            page = context.pages[0] if context.pages else context.new_page()
            page.goto(MAIN_URL, wait_until="domcontentloaded", timeout=45_000)
            self.wait_for_close(context, interrupt, wait_interval)

    def is_logged_in(self, context: BrowserContext) -> bool:
        try:
            cookies = context.cookies("https://www.naver.com")
            return any(cookie["name"] == "NID_SES" for cookie in cookies)
        except Exception:
            return False

    def wait_for_close(
            self,
            context: BrowserContext,
            interrupt: bool = True,
            wait_interval: float = 0.25,
        ):
        done = threading.Event()
        context.on("close", lambda *_: done.set())

        if interrupt:
            def input_watcher():
                msg = "로그인 완료 후 아무 키나 누르면 브라우저를 닫습니다."
                if self.account.passwd:
                    msg += f"\n비밀번호: {self.account.passwd}"
                try:
                    import msvcrt
                    print(msg)
                    msvcrt.getwch()
                except ImportError:
                    try:
                        input(msg + "\n")
                    except EOFError:
                        return
                except EOFError:
                    return
                done.set()

            threading.Thread(target=input_watcher, daemon=True).start()

        while not done.is_set():
            time.sleep(wait_interval)

        try:
            context.close()
        except Exception:
            pass

    #################### Mobile Tethering Extension ###################

    def is_mobile_proxy(self, ip_addr: str | None) -> bool:
        return isinstance(ip_addr, str) and (ip_addr.strip().lower() == MOBILE_IP_TOKEN)

    def resolve_proxy(self, ip_addr: str | None) -> str | None:
        if self.is_mobile_proxy(ip_addr):
            return None
        return ip_addr or None

    def init_adb_server(self):
        if not any(account.enabled and self.is_mobile_proxy(account.ip_addr) for account in self.accounts):
            return
        elif not (self.adb_path and self.adb_path.exists()):
            raise FileNotFoundError(f"adb 실행 파일을 찾을 수 없습니다: {self.adb_path}")
        ensure_adb_server_ready(self.adb_path)

    ########################## Read Accounts ##########################

    def validate_worksheet_connection(self, conn: dict) -> None:
        if not (conn.get("key") and conn.get("sheet")):
            raise KeyError("구글시트 연결 정보에 'key' 또는 'sheet' 값이 없습니다.")

    def read_accounts_from_gsheets(
            self,
            key: str,
            sheet: str,
            account: str | Path = ":default:",
            head: int = 1,
        ) -> list[AccountWrapper]:
        client = WorksheetClient(self._get_credentials(account), key, sheet, head)
        str_keys = [i for i, type in enumerate(get_type_hints(Account).values(), start=1) if type == str]
        records = client.get_all_records(numericise_ignore=str_keys)
        return [AccountWrapper(record) for record in records if record["userid"]]

    def _get_credentials(self, account: str | Path | Literal[":default:"] = ":default:") -> ServiceAccount:
        return ServiceAccount(account["account"] if is_default(account) else str(account))


class ProfileDebugger:
    """디버깅 전용: 지정한 경로의 Chrome 프로필을 열고 네이버 모바일 메인 페이지까지 이동한다."""

    def __init__(
            self,
            profile_path: str | Path,
            profile_dir: str = "Default",
            proxy: str | None = None,
            device: str | None = None,
            headless: bool = False,
        ):
        self.profile_path = Path(profile_path)
        self.profile_dir = profile_dir
        self.proxy = proxy
        self.device = device
        self.headless = headless
        self.playwright: Playwright = None
        self.context: BrowserContext = None
        self.page: Page = None

    def open(self):
        self.playwright = sync_playwright().start()

        kwargs = build_launch_kwargs(
            headless = self.headless,
            profile_dir = self.profile_dir,
            device_opts = (self.playwright.devices[self.device] if self.device else None),
            proxy = self.proxy,
        )
        self.context = self.playwright.chromium.launch_persistent_context(str(self.profile_path), **kwargs)
        self.context.add_init_script(STEALTH_SCRIPT)
        self.context.grant_permissions(["clipboard-read", "clipboard-write"])

        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.page.goto(MAIN_URL, wait_until="domcontentloaded", timeout=45_000)

    def close(self):
        try:
            self.context.close()
        except Exception:
            pass
        try:
            self.playwright.stop()
        except Exception:
            pass
        self.playwright = self.context = self.page = None
