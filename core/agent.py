from __future__ import annotations

from openai.types.chat.chat_completion import ChatCompletion
import openai

from utils.common import print_json
from utils.date import cur_time_str

from typing import TypeVar, TypedDict, TYPE_CHECKING
from pathlib import Path
import datetime as dt
import json
import os
import time

if TYPE_CHECKING:
    from typing import Iterable, Literal

ChatModel = TypeVar("ChatModel", bound=str)

class Message(TypedDict):
    role: str
    content: str

class Prompt4(TypedDict, total=False):
    model: ChatModel | None
    messages: list[Message] | None
    markdown_path: str | Path | None
    temperature: float | None
    replacements: dict[str,str]

class Prompt5(TypedDict, total=False):
    model: ChatModel | None
    messages: list[Message] | None
    markdown_path: str | Path | None
    reasoning_effort: Literal["minimal","low","medium","high"] | None
    replacements: dict[str,str]


class ArticleParams(TypedDict):
    clubid: str
    articleid: str
    boardtype: str
    menuid: str
    title: str

class ArticleInfo(TypedDict):
    title: str
    contents: list[str]
    comments: list[str]
    url: str
    copy_link: str | None
    created_at: str

class NewComment(TypedDict, total=False):
    comment: str
    reject_reason: int | None
    violation_reason: int | None
    emotion: int
    created_at: str

class NewArticle(TypedDict, total=False):
    title: str
    contents: list[str]
    type: int
    emotion: int
    created_at: str
    violation_reason: int | None

class ModifiedArticle(TypedDict, total=False):
    title: str
    contents: list[str]
    origin: int
    created_at: str
    violation_reason: int | None


KEY_PATH = ".secrets/api.key"

MODELS = {"gpt-5.4-nano", "gpt-5.4-mini"}
PROMPTS_ROOT = ".prompts/"


def read_file(path: str | Path) -> str:
    if isinstance(path, Path) or os.path.isfile(path):
        with open(path, 'r', encoding="utf-8") as file:
            return file.read()
    elif isinstance(path, str):
        return path
    else:
        return str()


def read_markdown(
        markdown_path: str | Path,
        user_message: str = str(),
        model: ChatModel | None = None,
        **replacements: str,
    ) -> tuple[ChatModel, list[Message]]:
    """
    ### MARKDOWN FORMAT
    ```
    '${model}\\n<--->\\n${system-content}\\n<--->\\n${user-content}\\n${assistant-content}'
    ```
    """
    markdown = read_file(markdown_path)
    for key, value in replacements.items():
        markdown = markdown.replace("{{ $"+key+" }}", value)

    sections = markdown.split("\n<--->\n")
    model = model if model else sections[0]
    messages = [{"role": "system", "content": sections[1]}]

    for section in sections[2:]:
        try:
            user, assistant = section.split('\n')
        except ValueError:
            continue
        messages += [{"role": "user", "content": user}, {"role": "assistant", "content": assistant}]

    if user_message:
        messages.append({"role": "user", "content": user_message})
    return model, messages


def parse_article(article_info: ArticleInfo, **kwargs) -> dict:
    return {
        "title": article_info["title"],
        "contents": (article_info["contents"] or list()),
        "comments": (article_info["comments"] or list()),
        "created_at": article_info["created_at"],
        **kwargs
    }


def min_json(data: dict | list) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(',', ':'), default=str)


###################################################################
############################### Chat ##############################
###################################################################

def set_api_key(api_key: str | Path | None = None):
    openai.api_key = read_file(api_key if api_key else Path(KEY_PATH)).strip()


def chat(
        model: ChatModel,
        messages: list[Message],
        agent_name: str | None = None,
        verbose: int | str | Path = 0,
        **kwargs
    ) -> str:
    start_time = time.perf_counter()
    response = openai.chat.completions.create(model=model, messages=messages, **kwargs)
    content = _get_content(response)
    print_json({
        **({"agent_name": agent_name} if agent_name else {}),
        "model": model,
        "content": content,
        "inference_time": round(time.perf_counter() - start_time, 1),
        "tokens": json.dumps(_get_tokens_suage(response)).replace('\"', '\'')
    }, verbose)
    return content


def chat_json(
        model: str,
        messages: list[Message],
        agent_name: str | None = None,
        verbose: int | str | Path = 0,
        **kwargs
    ) -> dict | list:
    start_time = time.perf_counter()
    response = openai.chat.completions.create(model=model, messages=messages, **kwargs)
    message = {
        **({"agent_name": agent_name} if agent_name else {}),
        "model": model,
        "content": _get_content(response),
        "inference_time": round(time.perf_counter() - start_time, 1),
        "tokens": json.dumps(_get_tokens_suage(response)).replace('\"', '\'')
    }
    try:
        message["content"] = json.loads(message["content"])
        print_json(message, verbose)
        return message["content"]
    except json.JSONDecodeError as error:
        print_json(message, verbose)
        print_json({"agent_name": agent_name, "error": str(type(error)), "message": str(error)}, verbose)
        raise error


def _get_content(response: ChatCompletion) -> str:
    return response.choices[0].message.content


def _get_tokens_suage(response: ChatCompletion) -> dict[str,int]:
    return {
        "input": response.usage.prompt_tokens,
        "output": response.usage.completion_tokens
    }


###################################################################
#################### Agent 1: :select_articles: ###################
###################################################################

def select_articles(
        articles: Iterable[ArticleParams],
        model: ChatModel | None = None,
        messages: list[Message] | None = None,
        markdown_path: str | Path | None = None,
        temperature: float | None = 0.1,
        replacements: dict[str,str] = dict(),
        verbose: int | str | Path = 0,
        **kwargs
    ) -> list[ArticleParams]:
    """
    ### MARKDOWN FORMAT
    ```
    '${model}\\n<--->\\n${system-content}\\n<--->\\n${user-content}\\n${assistant-content}'
    ```
    ### INPUT FORMAT
    ```
    [
        {"articleid": "131", "title": "제목1"},
        {"articleid": "242", "title": "제목2"},
        {"articleid": "353", "title": "제목3"}
    ]
    ```
    ### OUTPUT FORMAT
    ```
    ["131","242"]
    ```
    """
    name = "select_articles"
    if messages is None:
        data = [{"articleid": article["articleid"], "title": article["title"]}
                for article in articles if not is_question(article["title"])]
        model, messages = read_markdown(markdown_path, min_json(data), model, **replacements)
        print_json({"agent_name": name, "user_message": data}, verbose)
    else:
        print_json({"agent_name": name, "user_message": messages[-1]["content"]}, verbose)

    if isinstance(temperature, (float,int)):
        kwargs["temperature"] = temperature

    try:
        article_ids = set(chat_json(model or "gpt-5.4-nano", messages, name, verbose, **kwargs))
        return [article for article in articles if article["articleid"] in article_ids]
    except Exception as e:
        print_json({"agent_name": name, "error": str(type(e)), "message": str(e)}, verbose)
    return str()


def is_question(title: str) -> bool:
    return title.endswith('?') or ("추천" in title)


###################################################################
#################### Agent 2: :create_comment: ####################
###################################################################

def create_comment(
        article_info: ArticleInfo = dict(),
        model: ChatModel | None = None,
        messages: list[Message] | None = None,
        markdown_path: str | Path | None = None,
        reasoning_effort: Literal["minimal","low","medium","high"] | None = "high",
        replacements: dict[str,str] = dict(),
        verbose: int | str | Path = 0,
        **kwargs
    ) -> str:
    """
    ### MARKDOWN FORMAT
    ```
    '${model}\\n<--->\\n${system-content}\\n<--->\\n${user-content}\\n${assistant-content}'
    ```
    ### INPUT FORMAT
    ```
    {
        "title": "제목",
        "contents": ["문장1", "![alt](url)", "문장2"],
        "comments": ["댓글1", "댓글2"],
        "created_at": "2026-01-02T12:04:05+09:00",
        "current_time": "2026-01-02T12:04:05+09:00"
    }
    ```
    ### OUTPUT FORMAT
    ```
    {"comment":null,"reject_reason":null,"violation_reason":null}
    ```
    """
    name = "create_comment"
    if messages is None:
        data = parse_article(article_info, current_time=cur_time_str())
        model, messages = read_markdown(markdown_path, min_json(data), model, **replacements)
        print_json({"agent_name": name, "user_message": data}, verbose)
    else:
        print_json({"agent_name": name, "user_message": messages[-1]["content"]}, verbose)

    if isinstance(reasoning_effort, str):
        kwargs["reasoning_effort"] = reasoning_effort

    try:
        comment = chat_json(model or "gpt-5.4-mini", messages, name, verbose, **kwargs)
        if comment.get("comment") and not (comment.get("reject_reason") or comment.get("violation_reason")):
            return comment["comment"]
    except Exception as e:
        print_json({"agent_name": name, "error": str(type(e)), "message": str(e)}, verbose)
    return str()


###################################################################
#################### Agent 3: :create_replies: ####################
###################################################################

def create_replies(
        article_info: ArticleInfo = dict(),
        model: ChatModel | None = None,
        messages: list[Message] | None = None,
        markdown_path: str | Path | None = None,
        reasoning_effort: Literal["minimal","low","medium","high"] | None = "high",
        replacements: dict[str,str] = dict(),
        verbose: int | str | Path = 0,
        **kwargs
    ) -> list[str]:
    """
    ### MARKDOWN FORMAT
    ```
    '${model}\\n<--->\\n${system-content}\\n<--->\\n${user-content}\\n${assistant-content}'
    ```
    ### INPUT FORMAT
    ```
    {
        "title": "제목",
        "contents": ["문장1", "![alt](url)", "문장2"],
        "comments": ["댓글1", "댓글2"],
        "created_at": "2026-01-02T12:04:05+09:00",
        "current_time": "2026-01-02T12:04:05+09:00"
    }
    ```
    ### OUTPUT FORMAT
    ```
    [{"comment":null,"reject_reason":null,"violation_reason":null},{"comment":null,"reject_reason":null,"violation_reason":null}]
    ```
    """
    name = "create_replies"
    if messages is None:
        data = parse_article(article_info, current_time=cur_time_str())
        model, messages = read_markdown(markdown_path, min_json(data), model, **replacements)
        print_json({"agent_name": name, "user_message": data}, verbose)
    else:
        print_json({"agent_name": name, "user_message": messages[-1]["content"]}, verbose)

    if isinstance(reasoning_effort, str):
        kwargs["reasoning_effort"] = reasoning_effort

    replies = list()
    try:
        for comment in chat_json(model or "gpt-5.4-mini", messages, name, verbose, **kwargs):
            if (isinstance(comment, dict) and comment.get("comment")
                and not (comment.get("reject_reason") or comment.get("violation_reason"))):
                replies.append(comment["comment"])
            else:
                replies.append(str())
    except Exception as e:
        print_json({"agent_name": name, "error": str(type(e)), "message": str(e)}, verbose)
    return replies


###################################################################
#################### Agent 4: :create_article: ####################
###################################################################

def create_article(
        articles: Iterable[ArticleInfo] = list(),
        my_articles: Iterable[str] = list(),
        model: ChatModel | None = None,
        messages: list[Message] | None = None,
        markdown_path: str | Path | None = None,
        reasoning_effort: Literal["minimal","low","medium","high"] | None = "high",
        replacements: dict[str,str] = dict(),
        verbose: int | str | Path = 0,
        **kwargs
    ) -> NewArticle:
    """
    ### MARKDOWN FORMAT
    ```
    '${model}\\n<--->\\n${system-content}\\n<--->\\n${user-content}\\n${assistant-content}'
    ```
    ### INPUT FORMAT
    ```
    {
        "articles": [{
            "title": "제목",
            "contents": ["문장1", "![alt](url)", "문장2"],
            "comments": ["댓글1", "댓글2"],
            "created_at": "2026-01-02T12:04:05+09:00"
        }],
        "my_articles": [{
            "title": "제목",
            "contents": ["문장1", "![alt](url)", "문장2"],
            "comments": ["댓글1", "댓글2"],
            "created_at": "2026-01-02T12:04:05+09:00"
        }],
        "current_time": "2026-01-02T12:04:05+09:00"
    }
    ```
    ### OUTPUT FORMAT
    ```
    {"title":"제목","contents":["문장1","문장2"],"type":null,"emotion":null,"violation_reason":null}
    ```
    """
    name = kwargs.pop("agent_name", "create_article")
    if messages is None:
        data = {
            "articles": list(map(parse_article, articles)),
            "my_articles": list(map(parse_article, my_articles)),
            "current_time": cur_time_str()
        }
        model, messages = read_markdown(markdown_path, min_json(data), model, **replacements)
        print_json({"agent_name": name, "user_message": data}, verbose)
    else:
        print_json({"agent_name": name, "user_message": messages[-1]["content"]}, verbose)

    if isinstance(reasoning_effort, str):
        kwargs["reasoning_effort"] = reasoning_effort

    try:
        article = chat_json(model or "gpt-5.4-mini", messages, name, verbose, **kwargs)
        if article["title"] and article["contents"] and (not article.get("violation_reason")):
            article["created_at"] = dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S") + "+09:00"
            return article
    except Exception as e:
        print_json({"agent_name": name, "error": str(type(e)), "message": str(e)}, verbose)
    return dict()


###################################################################
#################### Agent 5: :modify_article: ####################
###################################################################

def modify_article(
        articles: Iterable[ArticleInfo] = list(),
        my_articles: Iterable[str] = list(),
        model: ChatModel | None = None,
        messages: list[Message] | None = None,
        markdown_path: str | Path | None = None,
        reasoning_effort: Literal["minimal","low","medium","high"] | None = "high",
        replacements: dict[str,str] = dict(),
        verbose: int | str | Path = 0,
        **kwargs
    ) -> ModifiedArticle:
    """
    ### MARKDOWN FORMAT
    ```
    '${model}\\n<--->\\n${system-content}\\n<--->\\n${user-content}\\n${assistant-content}'
    ```
    ### INPUT FORMAT
    ```
    {
        "articles": [{
            "title": "제목",
            "contents": ["문장1", "![alt](url)", "문장2"],
            "comments": ["댓글1", "댓글2"],
            "created_at": "2026-01-02T12:04:05+09:00"
        }],
        "my_articles": [{
            "title": "제목",
            "contents": ["문장1", "![alt](url)", "문장2"],
            "comments": ["댓글1", "댓글2"],
            "created_at": "2026-01-02T12:04:05+09:00"
        }],
        "current_time": "2026-01-02T12:04:05+09:00"
    }
    ```
    ### OUTPUT FORMAT
    ```
    {"title":"제목","contents":["문장1","문장2"],"origin":0,"violation_reason":null}
    ```
    """
    kwargs["agent_name"] = "modify_article"
    return create_article(
        articles, my_articles, model, messages, markdown_path, reasoning_effort, replacements, verbose, **kwargs)
