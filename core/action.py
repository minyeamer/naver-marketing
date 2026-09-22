from __future__ import annotations

from playwright.sync_api import Page, Locator

from core.agent import Prompt4, Prompt5
from core.agent import ArticleParams, select_articles
from core.agent import ArticleInfo, create_comment, create_replies
from core.agent import NewArticle, create_article
from core.agent import ModifiedArticle, modify_article

from utils.common import print_json, regexp_extract, wait, Delay
from utils.date import to_iso_date, to_iso_date_str, to_iso_datetime, to_iso_datetime_str
from utils.locator import Overlay, locate, locate_all
from utils.locator import is_visible, range_boundaries
from utils.mouse import safe_wheel
from utils.touchscreen import safe_tap

from typing import TypeVar, TypedDict, TYPE_CHECKING
from urllib.parse import urlparse
import datetime as dt
import random
import re
import time

if TYPE_CHECKING:
    from typing import Iterable, Literal
    from pathlib import Path

MenuName = TypeVar("MenuName", bound=str)
CafeId = TypeVar("CafeId", bound=str)
ArticleId = TypeVar("ArticleId", bound=str)
Comment = TypeVar("Comment", bound=str)

class CafeRanges(TypedDict):
    boundary: Locator
    overlay: Overlay

class Contents(TypedDict):
    lines: list[str]
    visible_lines: list[str]
    total_lines: int
    word_count: int
    read_start: int
    read_end: int
    read_done: bool

class Replies(ArticleInfo):
    title: str
    contents: list[str]
    comments: list[str]
    url: str
    copy_link: str | None
    created_at: str
    replies: list[str]

class Wpm(TypedDict):
    kor: int
    eng: int
    img: int
    wait: bool


class TotalCount(TypedDict):
    visit: int
    article: int
    comment: int

class TodayCount(TypedDict):
    article: int
    last_article_ts: dt.datetime
    comment: int
    last_comment_ts: dt.datetime

class ActionLog(TypedDict):
    total: TotalCount
    today: TodayCount | None


class CafeNotFoundError(RuntimeError):
    ...

class CafeNotLoadedError(RuntimeError):
    ...

class CafeBannedError(RuntimeError):
    ...

class ArticlePermissionError(RuntimeError):
    ...


###################################################################
################### Action 0 - :goto_cafe_home: ###################
###################################################################

def goto_cafe_home(
        page: Page,
        mobile: bool = True,
        action_delay: Delay = (0.3, 0.6),
        goto_delay: Delay = (1, 3),
    ):
    """## Action 0"""
    if page.url.startswith(cafe_url(mobile)):
        return
    goto_naver_main(page, mobile, goto_delay)

    try:
        if mobile:
            page.tap('#MM_logo [href="/aside/"]'), wait(goto_delay)
            if page.locator(".layer_alert").count() > 0:
                page.tap(".layer_alert .la_option"), wait(action_delay)
            page.tap('[href="https://m.cafe.naver.com"]'), wait(goto_delay)
        else:
            page.goto(cafe_url(mobile=False)), wait(goto_delay)
            # :has(a[href="https://cafe.naver.com"][target="_blank"])
            # from ncafe.utils.desktop import click_new_page
            # click_new_page(context, page, '[href="https://cafe.naver.com"]')
    except:
        page.goto(cafe_url(mobile)), wait(goto_delay)


def return_to_cafe_home(page: Page, mobile: bool = True, goto_delay: Delay = (1, 3)):
    selector = f'.WebHeader [href="{cafe_url(mobile)}"]'
    try:
        if mobile:
            page.locator(selector).tap(), wait(goto_delay)
        else:
            page.locator(selector).click(), wait(goto_delay)
    except:
        page.goto(cafe_url(mobile)), wait(goto_delay)


def goto_naver_main(
        page: Page,
        mobile: bool = True,
        goto_delay: Delay = (1, 3),
    ):
    if page.url != main_url(mobile):
        page.goto(main_url(mobile)), wait(goto_delay)


def main_url(mobile: bool) -> str:
    return f"https://{'m.' if mobile else 'www.'}naver.com"


def cafe_url(mobile: bool) -> str:
    if mobile:
        return "https://m.cafe.naver.com"
    else:
        return "https://section.cafe.naver.com"


###################################################################
###################### Action 1 - :goto_cafe: #####################
###################################################################

def goto_cafe(page: Page, cafe_name: str, goto_delay: Delay = (1, 3), timeout: float = 10000):
    """## Action 1"""
    try:
        locate(page, 'a:has-text("내 카페")', nth=-1).tap()
    except:
        page.goto("https://m.cafe.naver.com/ca-fe/home/cafes/join")
    wait(goto_delay)

    try:
        page.wait_for_selector(cafe_link := f'.cafe_info:has-text("{cafe_name}")', timeout=timeout)
    except:
        if page.locator(".cafe_info").count() == 0:
            raise CafeNotLoadedError("가입카페 목록이 로딩되지 않았습니다.")
        else:
            raise CafeNotFoundError(f"가입카페 목록에서 '{cafe_name}' 카페를 찾을 수 없습니다.")

    ranges = dict(
        boundary = page.locator("body").first,
        overlay = dict(top=page.locator(".HeaderWrap").first.bounding_box()["height"]))
    safe_tap(page, cafe_link, **ranges), wait(goto_delay)


def goto_cafe_url(
        page: Page,
        cafe_id: int | str,
        menu_id: int | str | None = None,
        mobile: bool = True,
        goto_delay: Delay = (1, 3),
    ):
    path = f"https://{'m.' if mobile else 'www.'}cafe.naver.com/ca-fe/web/cafes"
    if (menu_id is None) or (str(menu_id) == '0'):
        page.goto(f"{path}/{cafe_id}"), wait(goto_delay)
    else:
        page.goto(f"{path}/{cafe_id}/menus/{menu_id}"), wait(goto_delay)


def go_back(page: Page, goto_delay: Delay = (1, 3)):
    page.go_back(), wait(goto_delay)


def get_cafe_ranges(page: Page, header: bool = True, tab: bool = False) -> CafeRanges:
    return dict(
        boundary = page.locator("body").first,
        overlay = _get_cafe_overlay(page, header, tab),
    )


def _get_cafe_overlay(page: Page, header: bool = True, tab: bool = False) -> Overlay:
    try:
        header_height = page.locator(".WebHeader").first.bounding_box()["height"] if header else 0
    except:
        header_height = 52
    try:
        tab_height = page.locator(".ArticleTab").first.bounding_box()["height"] if tab else 0
    except:
        tab_height = 66
    return dict(top = (header_height + tab_height))


###################################################################
###################### Action 2 - :goto_menu: #####################
###################################################################

def goto_menu(
        page: Page,
        menu_name: str = str(),
        action_delay: Delay = (0.3, 0.6),
        goto_delay: Delay = (1, 3),
        has_text: Iterable[str] = list(),
        has_not_text: Iterable[str] = list(),
    ) -> MenuName:
    """## Action 2"""
    open_menu(page, action_delay)
    if menu_name:
        safe_tap(page, f'a:has-text("{menu_name}")', delay=action_delay), wait(goto_delay)
        return menu_name
    else:
        has_text = re.compile('|'.join(has_text)) if has_text else None
        has_not_text = re.compile('|'.join(has_not_text)) if has_not_text else None
        filters = dict(has_text=has_text, has_not_text=has_not_text)
        a = safe_tap(page, "a.link_menu", nth="random", filters=filters, delay=action_delay)
        return a.locator(".menu").text_content()


def open_menu(page: Page, action_delay: Delay = (0.3, 0.6)):
    page.tap(f'header button:has-text("메뉴")'), wait(action_delay)


def _get_menu_boundary(page: Page) -> Locator:
    return page.locator(".list_section").first


def _get_menu_overlay(page: Page) -> Overlay:
    return dict(top = page.locator(".header_top").first.bounding_box()["height"])


###################################################################
################## Action 3 - :explore_articles: ##################
###################################################################

def explore_articles(
        page: Page,
        visited: set[ArticleId] = set(),
        read_articles_commented_only: bool = False,
        prompt: Prompt4 = dict(),
        verbose: int | str | Path = 0,
        **kwargs
    ) -> list[ArticleParams]:
    """## Action 3"""
    articles = list_articles(page, visited, read_articles_commented_only)
    if read_articles_commented_only:
        return articles
    elif articles:
        return select_articles(articles, **prompt, verbose=verbose, **kwargs) # Agent 1
    else:
        return list()


def list_articles(
        page: Page,
        visited: set[ArticleId] = set(),
        read_articles_commented_only: bool = False,
    ) -> list[ArticleParams]:
    articles = list()
    for article in locate_all(page, ".ArticleListItem", **get_cafe_ranges(page, header=True, tab=True)):
        link, comment = article.locator(".mainLink").first, article.locator(".comment_area").first
        if read_articles_commented_only and (comment.locator(".number").first.text_content().strip() == '0'):
            continue

        params = _parse_params(link.get_attribute("href") or str())
        if params["articleid"] not in visited:
            visited.add(params["articleid"])
            params["title"] = link.locator(".tit").first.text_content().strip()
            articles.append(params)

    return articles


def _parse_params(href: str) -> dict[str,str]:
    query = urlparse(href).query
    return dict([kv.split('=') for kv in query.split('&')])


def next_articles(page: Page, action_delay: Delay = (0.3, 0.6)):
    ranges = get_cafe_ranges(page, header=True, tab=True)
    delta = page.viewport_size["height"] - ranges["overlay"]["top"]
    safe_wheel(page, delta=delta, **ranges)
    wait(action_delay)


def reload_articles(page: Page, goto_delay: Delay = (1, 3)):
    page.reload(), wait(goto_delay)


###################################################################
#################### Action 4 - :goto_article: ####################
###################################################################

def goto_article(page: Page, id: str | int | Literal["random"], goto_delay: Delay = (1, 3)) -> bool:
    """## Action 4"""
    articles = locate_all(page, ".mainLink", **get_cafe_ranges(page, header=True, tab=True))
    if isinstance(id, int):
        articles[id].tap(), wait(goto_delay)
    if id == "random":
        random.choice(articles).tap(), wait(goto_delay)
        return True

    for article in articles:
        params = _parse_params(article.get_attribute("href") or str())
        if id == params["articleid"]:
            article.tap(), wait(goto_delay)
            return True
    return False


###################################################################
#################### Action 5 - :read_article: ####################
###################################################################

def read_article(
        page: Page,
        wpm: Wpm = dict(),
        verbose: int | str | Path = 0,
        contents_only: bool = False,
        copy_link: bool = True,
    ) -> ArticleInfo | Contents:
    """## Action 5"""
    lines, visible_lines = list(), list()
    isin_viewport = False
    word_count, read_start, read_end = 0, 0, 0
    _, min_y, _, max_y = range_boundaries(page, **get_cafe_ranges(page, header=True, tab=False))

    content = page.locator("#postContent > .ArticleSectionLoggerContent > .content").first
    elements = content.locator(
        'p:not([style="display: none;"]):not(.se-module-oglink *), '
        'img:not([style="display: none;"]):not(.se-module-oglink *)'
    ).all()

    for i, el in enumerate(elements):
        tag_name = str(el.evaluate("el => el.tagName")).upper()
        if tag_name == "IMG":
            line = f"![{el.get_attribute('alt') or '이미지'}]({el.get_attribute('src')})"
        else:
            line = el.text_content().replace('\u200b', '').strip()
            word_count += len(line)
        lines.append(line)

        if is_visible(el, min_y, max_y):
            if not isin_viewport:
                isin_viewport = True
                read_start = i
            visible_lines.append(line)
        elif isin_viewport:
            isin_viewport = False
            read_end = i

    if page.locator("#postContent > .article_permission").count() > 0:
        message = lines[min(1, len(lines)-1)] if lines else "카페에 가입하면 바로 글을 볼 수 있어요!"
        raise ArticlePermissionError(message)

    total_lines = len(lines)
    if isin_viewport and total_lines:
        read_end = total_lines - 1
    read_done = ((read_end + 1) == total_lines) if (total_lines > 0) and visible_lines else True

    seconds = round(_estimate_reading_seconds(visible_lines, **wpm), 1)
    print_json({"action": "read_article", "reading_time": seconds}, verbose)
    if wpm.get("wait"):
        wait(max(seconds, 0.1))

    if contents_only:
        keys = ["lines", "visible_lines", "total_lines", "word_count", "read_start", "read_end", "read_done"]
        values = [lines, visible_lines, total_lines, word_count, read_start, read_end, read_done]
        return dict(zip(keys, values))
    else:
        return _make_article_info(page, lines, copy_link)


def read_full_article(
        page: Page,
        action_delay: Delay = (0.3, 0.6),
        wpm: Wpm = dict(),
        verbose: int | str | Path = 0,
        contents_only: bool = False,
        timeout: float = 30.,
    ) -> ArticleInfo | Contents:
    start_time, end_time = time.perf_counter(), (lambda: time.perf_counter())
    contents = read_article(page, wpm, verbose, contents_only=True)
    read_start = contents["read_start"]

    while (not contents["read_done"]) and ((end_time() - start_time) < timeout):
        next_lines(page, action_delay)
        contents = read_article(page, wpm, verbose, contents_only=True)
    if read_comments(page):
        ranges = get_cafe_ranges(page, header=True, tab=False)
        safe_wheel(page, target=page.locator(".CommonComment .write").first, **ranges), wait(action_delay)

    if contents_only:
        contents["read_start"] = read_start
        return contents
    else:
        return _make_article_info(page, contents["lines"])


def _make_article_info(
        page: Page,
        lines: list[str],
        action_delay: Delay = (0.3, 0.6),
        copy_link: bool = True,
    ) -> ArticleInfo:
    return {
        "title": page.locator(".post_title .tit").first.text_content().strip(),
        "contents": lines,
        "comments": read_comments(page),
        "url": page.locator('meta[property="og:url"]').first.get_attribute("content"),
        "copy_link": (copy_article_url(page, action_delay) if copy_link else None),
        "created_at": to_iso_datetime_str(page.locator(".post_title .date").first.text_content().strip()),
    }


def copy_article_url(page: Page, action_delay: Delay = (0.3, 0.6)) -> str:
    try:
        page.locator(".title_area .btn_more").first.tap(timeout=5000), wait(action_delay)
        page.locator("a.btn", has_text="URL 복사").first.tap(), wait(action_delay)
        url = page.evaluate("async () => await navigator.clipboard.readText()")
        wait(action_delay)
        return url if isinstance(url, str) and url.startswith("https://") else page.url
    except:
        return page.url


def next_lines(page: Page, action_delay: Delay = (0.3, 0.6)):
    ranges = get_cafe_ranges(page, header=True, tab=False)
    delta = page.viewport_size["height"] - ranges["overlay"]["top"]
    safe_wheel(page, delta=delta, **ranges)
    wait(action_delay)


def prev_lines(page: Page, action_delay: Delay = (0.3, 0.6)):
    ranges = get_cafe_ranges(page, header=True, tab=False)
    delta = page.viewport_size["height"] - ranges["overlay"]["top"]
    safe_wheel(page, delta=(delta * -1), **ranges)
    wait(action_delay)


def _estimate_reading_seconds(
        lines: Iterable[str],
        kor: int = 160,
        eng: int = 238,
        img: int = 20,
        **kwargs
    ) -> float:
    seconds, image_seconds = 0., (img / 60)
    for line in lines:
        if not line:
            continue
        elif line.startswith("![") and line.endswith(')'):
            seconds += image_seconds
        elif (cpm := _calc_weighted_cpm(line, kor, eng)):
            total_chars = _count_hangul_chars(line) + _count_english_chars(line)
            seconds += round((total_chars / cpm) * 60, 5)
    return seconds


def _calc_weighted_cpm(text: str, kor_cpm: int = 160, eng_cpm: int = 238) -> float:
    kor_chars = _count_hangul_chars(text)
    eng_chars = _count_english_chars(text)
    total_chars = kor_chars + eng_chars
    if total_chars == 0:
        return 0
    return (kor_chars * kor_cpm + eng_chars * eng_cpm) / total_chars * 3


def _count_hangul_chars(text) -> int:
    return len(re.sub(r"[^ㄱ-ㅎㅏ-ㅣ가-힣]", '', text))


def _count_english_chars(text) -> int:
    return len(re.sub(r"[^a-zA-Z0-9]", '', text))


###################################################################
#################### Action 6 - :like_article: ####################
###################################################################

def like_article(page: Page, action_delay: Delay = (0.3, 0.6)):
    """## Action 6"""
    like_button = page.locator('.right_area [data-type="like"]').first
    if like_button.get_attribute("aria-pressed") == "false":
        like_button.tap(), wait(action_delay)


###################################################################
#################### Action 7 - :write_comment: ###################
###################################################################

def write_comment(
        page: Page,
        comment: str,
        action_delay: Delay = (0.3, 0.6),
        goto_delay: Delay = (1, 3),
        upload_delay: Delay = (2, 4),
        dry_run: bool = False,
    ):
    """## Action 7"""
    page.locator(".right_area .f_reply").first.tap(), wait(goto_delay)
    comment_area = page.locator(".comment_textarea").first
    if page.locator(".CommentViewStop").count() > 0:
        raise CafeBannedError("현재 활동정지 상태입니다.")

    comment_area.locator(".textarea_write").first.tap(), wait(action_delay)
    comment_area.locator(".text_input_area").first.type(comment, delay=100), wait(action_delay)
    if not dry_run:
        comment_area.locator(".btn_area > button", has_text="등록").tap()
    wait(upload_delay)
    go_back(page, goto_delay)


def read_comments(page: Page) -> list[str]:
    if page.locator(".CommonComment .num").first.text_content() != '0':
        comments = locate_all(page, ".comment_list .comment_content")
        return [comment.text_content() for comment in comments]
    else:
        return list()


###################################################################
########## Action 5+7 - :read_article_and_write_comment: ##########
###################################################################

def read_article_and_write_comment(
        page: Page,
        action_delay: Delay = (0.3, 0.6),
        goto_delay: Delay = (1, 3),
        upload_delay: Delay = (2, 4),
        wpm: Wpm = dict(),
        prompt: Prompt5 = dict(),
        verbose: int | str | Path = 0,
        dry_run: bool = False,
        timeout: float = 30.,
        **kwargs
    ) -> tuple[ArticleInfo, Comment]:
    """## Action 5+7"""
    start_time, end_time = time.perf_counter(), (lambda: time.perf_counter())
    contents = read_article(page, dict(wpm, **{"wait": False}), verbose, contents_only=True)
    article_info = _make_article_info(page, contents["lines"])
    comment = create_comment(article_info, **prompt, verbose=verbose, **kwargs) # Agent 2

    if wpm.get("wait"):
        current_wait = round(_estimate_reading_seconds(contents["visible_lines"]), 1)
        creating_time = round(time.perf_counter() - start_time, 1)
        left_wait = current_wait - creating_time
        if left_wait > 0.:
            wait(left_wait)

        while (not contents["read_done"]) and ((end_time() - start_time) < timeout):
            next_lines(page, action_delay)
            contents = read_article(page, wpm, verbose, contents_only=True)
        if article_info["comments"]:
            ranges = get_cafe_ranges(page, header=True, tab=False)
            safe_wheel(page, target=page.locator(".CommonComment .write").first, **ranges), wait(action_delay)

    if comment:
        write_comment(page, comment, action_delay, goto_delay, upload_delay, dry_run)
    return article_info, comment


###################################################################
#################### Action 8 - :write_article: ###################
###################################################################

def write_article(
        page: Page,
        articles: Iterable[ArticleInfo],
        my_articles: Iterable[str] = list(),
        action_delay: Delay = (0.3, 0.6),
        goto_delay: Delay = (1, 3),
        upload_delay: Delay = (2, 4),
        prompt: Prompt5 = dict(),
        verbose: int | str | Path = 0,
        dry_run: bool = False,
        **kwargs
    ) -> NewArticle:
    """## Action 8"""
    try:
        with page.expect_event("dialog", timeout=3000) as dialog:
            page.locator(".FloatingWriteButton > button").first.tap(), wait(goto_delay)
        raise CafeBannedError(dialog.value)
    except CafeBannedError as error:
        raise error
    except:
        pass

    if kwargs.pop("agent_name", str()) == "modify_article":
        article = modify_article(articles, my_articles, **prompt, verbose=verbose, **kwargs) # Agent 5
    else:
        article = create_article(articles, my_articles, **prompt, verbose=verbose, **kwargs) # Agent 4

    if not article:
        return dict()

    title_area = page.locator(".ArticleWriteFormSubject textarea").first
    title_area.tap(), wait(action_delay)
    title_area.type(article["title"], delay=100), wait(action_delay)

    content_area = page.locator("#one-editor article").first
    content_area.tap(), wait(action_delay)
    if content_area.text_content().strip() != "내용을 입력하세요.":
        page.keyboard.press("Control+A"), wait(action_delay)
        page.keyboard.press("Delete"), wait(action_delay)

    for line_no, content in enumerate(article["contents"]):
        if line_no > 0:
            page.keyboard.press("Enter"), wait(action_delay)
        if content:
            content_area.type(content, delay=100), wait(action_delay)

    if not dry_run:
        safe_tap(page, '.ArticleWriteComplete > [role="button"]', filters=dict(has_text="등록")), wait(upload_delay)

    return article


def update_article(
        page: Page,
        articles: Iterable[ArticleInfo],
        my_articles: Iterable[str] = list(),
        action_delay: Delay = (0.3, 0.6),
        goto_delay: Delay = (1, 3),
        upload_delay: Delay = (2, 4),
        prompt: Prompt5 = dict(),
        verbose: int | str | Path = 0,
        dry_run: bool = False,
        **kwargs
    ) -> ModifiedArticle:
    kwargs["agent_name"] = "modify_article"
    return write_article(
        page, articles, my_articles, action_delay, goto_delay, upload_delay, prompt, verbose, dry_run, **kwargs)


###################################################################
################## Action 9 - :read_my_articles: ##################
###################################################################

def read_my_articles(
        page: Page,
        goto_delay: Delay = (1, 3),
        n_articles: int | None = None,
        read_articles: bool = True,
        wpm: Wpm = dict(),
        verbose: int | str | Path = 0,
    ) -> list[ArticleInfo]:
    """## Action 9"""
    data = list()
    for item in locate_all(page, ".list_area .txt_area")[:n_articles]:
        if read_articles:
            safe_tap(item, **_get_info_ranges(page)), wait(goto_delay)
            try:
                data.append(read_article(page, wpm, verbose, contents_only=False, copy_link=False))
            finally:
                go_back(page, goto_delay)
        else:
            data.append({
                "title": item.locator(".tit").text_content().strip(),
                "contents": list(),
                "comments": list(),
                "created_at": to_iso_date_str(item.locator(".time").text_content().strip()),
            })
    return data


def open_info(page: Page, action_delay: Delay = (0.3, 0.6), goto_delay: Delay = (1, 3)):
    open_menu(page, action_delay)
    page.tap("header .info_link"), wait(goto_delay)


def close_info(page: Page, goto_delay: Delay = (1, 3)):
    page.tap('.HeaderGnbLeft [role="button"]'), wait(goto_delay)


def read_action_log(
        page: Page,
        total_only: bool = False,
        my_articles: Iterable[ArticleInfo] = list(),
        action_delay: Delay = (0.3, 0.6),
        goto_delay: Delay = (1, 3),
        today: dt.date | None = None,
    ) -> ActionLog:
    open_menu(page, goto_delay)
    try:
        keys = [span.text_content().strip() for span in locate_all(page, ".myinfo_detail .detail_title")]
        values = [(_safe_int(span.text_content().replace(',', '').strip()) or 0)
                for span in locate_all(page, ".myinfo_detail .detail_count")]
        alias = {"방문": "visit", "작성글": "article", "댓글": "comment"}
        total_count: TotalCount = {alias[key]: value for key, value in zip(keys, values) if key in alias}
        if total_only:
            return dict(total=total_count, today=None)

        try:
            page.tap("header .info_link"), wait(goto_delay)
            try:
                default_count = dict(article=0, last_article_ts=None, comment=0, last_comment_ts=None)
                today_count = _read_daily_log(page, default_count, goto_delay, today, my_articles)
            except:
                today_count = None
            finally:
                close_info(page, goto_delay)
        except:
            today_count = None
        return dict(total=total_count, today=today_count)
    finally:
        page.touchscreen.tap(0, 0), wait(action_delay)


def _read_daily_log(
        page: Page,
        today_count: TodayCount,
        goto_delay: Delay = (1, 3),
        today: dt.date | None = None,
        my_articles: Iterable[ArticleInfo] = list(),
    ) -> TodayCount:
    today = today if isinstance(today, dt.date) else dt.date.today()
    yesterday = dt.datetime.today() - dt.timedelta(days=1)

    today_count["article"] = len([1 for item in locate_all(page, ".list_area .time")
        if to_iso_date(item.text_content().strip(), default=yesterday).date() == today])

    if today_count["article"] > 0:
        safe_tap(page, ".list_area .txt_area", **_get_info_ranges(page)), wait(goto_delay)
        today_count["last_article_ts"] = to_iso_datetime(page.locator(".post_title .date").first.text_content().strip())
        go_back(page, goto_delay)

    my_ids = {my_id for article in my_articles if (my_id := regexp_extract(r"/articles/(\d+)", article["url"]))}
    page.locator('.tab_menu:has-text("작성댓글")').tap(), wait(goto_delay)

    comments = [ts for item in locate_all(page, ".comment_item")
        if ((ts := to_iso_datetime(item.locator(".date").first.text_content().strip(), default=yesterday)).date() == today)
            and (regexp_extract(r'"content_id":\s*(\d+)', item.get_attribute("data-nlog-params")) not in my_ids)]
    today_count["comment"] = len(comments)
    if today_count["comment"] > 0:
        today_count["last_comment_ts"] = max(comments)

    return today_count


def _get_info_ranges(page: Page) -> CafeRanges:
    return dict(
        boundary = page.locator("body").first,
        overlay = _get_info_overlay(page),
    )


def _get_info_overlay(page: Page) -> Overlay:
    return dict(top = page.locator(".HeaderWrap").first.bounding_box()["height"])


def _safe_int(value: str) -> int:
    try: return int(value)
    except: return


###################################################################
################# Action 10 - :reply_my_articles: #################
###################################################################

def reply_my_articles(
        page: Page,
        cutoff_date: dt.date | None = None,
        max_reply_length: int = 100,
        action_delay: Delay = (0.3, 0.6),
        goto_delay: Delay = (1, 3),
        upload_delay: Delay = (2, 4),
        n_articles: int | None = None,
        prompt: Prompt5 = dict(),
        verbose: int | str | Path = 0,
        dry_run: bool = False,
        **kwargs
    ) -> list[Replies]:
    """## Action 9"""
    if not isinstance(cutoff_date, dt.date):
        return list()
    data = list()

    default = cutoff_date - dt.timedelta(days=1)
    for item in locate_all(page, ".list_area .txt_area")[:n_articles]:
        if cutoff_date <= to_iso_date(item.locator(".time").text_content().strip(), default=default).date():
            safe_tap(item, **_get_info_ranges(page)), wait(goto_delay)
            try:
                args = (max_reply_length, action_delay, goto_delay, upload_delay, prompt, verbose, dry_run)
                data.append(reply_comments(page, *args, **kwargs))
            finally:
                go_back(page, goto_delay)
        else:
            break
    return data


def reply_comments(
        page: Page,
        max_reply_length: int = 100,
        action_delay: Delay = (0.3, 0.6),
        goto_delay: Delay = (1, 3),
        upload_delay: Delay = (2, 4),
        prompt: Prompt5 = dict(),
        verbose: int | str | Path = 0,
        dry_run: bool = False,
        **kwargs
    ) -> Replies:
    article_info = read_article(page, contents_only=False)

    page.locator(".right_area .f_reply").first.tap(), wait(goto_delay)
    try:
        comments, replies = _catch_comments_without_replies(page, max_reply_length), list()
        if comments:
            article_info["comments"] = list(comments.values())
            replies = create_replies(article_info, **prompt, verbose=verbose, **kwargs) # Agent 3

            comment_areas = locate_all(page, ".comment_list li")
            # 답글 대상은 nth-child 기준으로 선택되기 때문에 역순으로 등록한다.
            for i, (nth, comment) in list(enumerate(comments.items()))[::-1]:
                comment_area = comment_areas[nth]
                if (i >= len(replies)) or (not replies[i]):
                    continue
                elif comment_area.locator(".txt").first.text_content() != comment:
                    continue

                comment_area.locator(".btn_write").first.tap()
                comment_area.locator(".textarea_write").first.tap(), wait(action_delay)
                comment_area.locator(".text_input_area").first.type(replies[i], delay=100), wait(action_delay)
                if not dry_run:
                    comment_area.locator(".btn_area > button", has_text="등록").tap()
                wait(upload_delay)

        article_info["replies"] = replies
        return article_info
    finally:
        go_back(page, goto_delay)


def _catch_comments_without_replies(page: Page, max_length: int = 100) -> dict[int, Comment]:
    comments = locate_all(page, ".comment_list li")
    target, targets = 0, dict()
    for i, comment in enumerate(comments, start=1):
        if "reply" in comment.get_attribute("class"):
            target = 0
        else:
            if target and len(comment := comments[target-1].locator(".txt").first.text_content()) <= max_length:
                targets[target-1] = comment
            target = i
    if target and len(comment := comments[target-1].locator(".txt").first.text_content()) <= max_length:
        targets[target-1] = comment
    return targets
