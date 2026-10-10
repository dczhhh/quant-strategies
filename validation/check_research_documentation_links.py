"""Fork link verification with official GitHub API evidence; imported checker stays frozen."""

from __future__ import annotations

import argparse
import json
import os
import posixpath
import re
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

_SITE_ORIGIN = "https://www.ml4trading.io"
_SITE_PREFIX = "/docs/backtest/"
_USER_AGENT = "ml4t-backtest-documentation-link-check/1.0"
_GUIDE_SECTIONS = {"user-guide", "book-guide"}
_API_RESPONSE_LIMIT = 2 * 1024 * 1024


@dataclass(frozen=True)
class ExternalCheck:
    url: str
    status: Literal["verified_present", "verified_missing", "temporarily_unverifiable"]
    reason: str
    evidence: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class Probe:
    status: int | None
    body: bytes
    retryable: bool
    reason: str
    evidence: tuple[dict[str, Any], ...]


class NoAPIRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _native_open(request: Request, timeout: float):
    # Credentials go only to the fixed API origin and never follow redirects.
    if urlsplit(request.full_url).netloc == "api.github.com":
        return build_opener(NoAPIRedirect()).open(request, timeout=timeout)
    return urlopen(request, timeout=timeout)


def _probe(url: str, *, api: bool, transport: Callable, sleeper: Callable[[float], None]) -> Probe:
    observations = []
    for attempt in range(3):
        headers = {"User-Agent": _USER_AGENT}
        if api:
            headers |= {
                "Accept": "application/vnd.github.object+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
            if token := os.environ.get("GITHUB_TOKEN"):
                headers["Authorization"] = "Bearer " + token
        status, body, retry_after = None, b"", 0.0
        try:
            with transport(Request(url, headers=headers), timeout=15) as response:
                status = response.status
                if api and status == 200:
                    body = response.read(_API_RESPONSE_LIMIT + 1)
                    if len(body) > _API_RESPONSE_LIMIT:
                        observations.append(
                            {
                                "url": url,
                                "attempt": attempt + 1,
                                "http_status": status,
                                "reason": "oversized_api_response",
                            }
                        )
                        return Probe(
                            status, b"", False, "oversized_api_response", tuple(observations)
                        )
                reason = f"http_{status}"
        except HTTPError as error:
            with error:
                status = error.code
                reason = f"HTTP Error {status}"
                with suppress(ValueError, TypeError):
                    retry_after = float(error.headers.get("Retry-After", "0"))
        except (URLError, TimeoutError, OSError) as error:
            status, body = None, b""
            reason = type(error).__name__
        retryable = status is None or status == 429 or (status is not None and 500 <= status <= 599)
        observations.append(
            {"url": url, "attempt": attempt + 1, "http_status": status, "reason": reason}
        )
        if status is not None and 200 <= status < 400:
            return Probe(status, body, False, reason, tuple(observations))
        if not retryable or attempt == 2:
            return Probe(status, body, retryable, reason, tuple(observations))
        delay = max(float(2**attempt), retry_after) if 0 < retry_after <= 10 else float(2**attempt)
        sleeper(delay)
    raise AssertionError("bounded probe loop must return")


def _github_file(url: str) -> tuple[str, str, str, str] | None:
    """Only canonical, unambiguous public blob links have an API fallback."""
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc != "github.com" or parsed.query:
        return None
    parts = parsed.path.split("/")
    if len(parts) < 6 or parts[0] or parts[3] != "blob":
        return None
    owner, repository, raw_ref = parts[1], parts[2], parts[4]
    if (
        not re.fullmatch(r"[A-Za-z0-9-]{1,39}", owner)
        or not re.fullmatch(r"[A-Za-z0-9_.-]+", repository)
        or repository in {".", ".."}
    ):
        return None
    try:
        ref = unquote(raw_ref, errors="strict")
        path = unquote("/".join(parts[5:]), errors="strict")
    except UnicodeError:
        return None
    # Slash-containing refs must be encoded in one URL segment. Arbitrary unencoded
    # branch prefixes cannot be distinguished from a path without guessing.
    if (
        ref not in {"main", "master"}
        and not re.fullmatch(r"[0-9a-f]{40}", ref)
        and "%2f" not in raw_ref.lower()
    ):
        return None
    if (
        not re.fullmatch(r"[A-Za-z0-9_./-]{1,255}", ref)
        or any(p in {"", ".", ".."} for p in ref.split("/"))
        or not path
        or "\\" in path
        or "%" in path
        or any(ord(c) < 32 or ord(c) == 127 for c in path)
        or PurePosixPath(path).as_posix() != path
        or any(p in {".", "..", ""} for p in path.split("/"))
    ):
        return None
    return owner, repository, ref, path


def verify_external(
    url: str, *, transport: Callable = _native_open, sleeper: Callable[[float], None] = time.sleep
) -> ExternalCheck:
    web = _probe(url, api=False, transport=transport, sleeper=sleeper)
    if web.status is not None and 200 <= web.status < 400:
        return ExternalCheck(url, "verified_present", "web_reachable", web.evidence)
    if web.status in {404, 410}:
        return ExternalCheck(url, "verified_missing", web.reason, web.evidence)
    target = _github_file(url) if web.retryable else None
    if target is None:
        return ExternalCheck(url, "temporarily_unverifiable", web.reason, web.evidence)
    owner, repository, ref, path = target
    api_url = (
        f"https://api.github.com/repos/{owner}/{repository}/contents/{quote(path, safe='/')}?"
        + urlencode({"ref": ref})
    )
    api = _probe(api_url, api=True, transport=transport, sleeper=sleeper)
    observations = web.evidence + api.evidence
    if api.status in {404, 410}:
        return ExternalCheck(
            url, "verified_missing", "github_contents_missing: " + api.reason, observations
        )
    if api.status != 200 or api.reason == "oversized_api_response":
        return ExternalCheck(
            url,
            "temporarily_unverifiable",
            "github_contents_unverifiable: " + api.reason,
            observations,
        )
    try:
        content = json.loads(api.body)
        sha = content.get("sha")
        html_url = content.get("html_url")
        git_url = content.get("git_url")
        html = urlsplit(html_url) if isinstance(html_url, str) else None
        html_path = unquote(html.path).split("/", 4) if html else []
        if (
            content.get("type") != "file"
            or content.get("submodule_git_url") is not None
            or content.get("path") != path
            or not isinstance(sha, str)
            or re.fullmatch(r"[0-9a-f]{40}", sha) is None
            or type(content.get("size")) is not int
            or content["size"] < 0
            or html is None
            or html.scheme != "https"
            or html.netloc != "github.com"
            or html.query
            or html.fragment
            or len(html_path) != 5
            or html_path[1].casefold() != owner.casefold()
            or html_path[2].casefold() != repository.casefold()
            or html_path[3] != "blob"
            or html_path[4] != ref + "/" + path
            or not isinstance(git_url, str)
            or git_url.casefold()
            != f"https://api.github.com/repos/{owner}/{repository}/git/blobs/{sha}".casefold()
        ):
            raise ValueError("contents identity mismatch")
    except (ValueError, TypeError, AttributeError):
        return ExternalCheck(
            url, "temporarily_unverifiable", "github_contents_identity_mismatch", observations
        )
    evidence = {
        "channel": "github_contents",
        "repository": f"{owner}/{repository}",
        "ref": ref,
        "path": path,
        "blob_sha": sha,
        "size": content["size"],
    }
    return ExternalCheck(
        url, "verified_present", "github_contents_file_present", (*observations, evidence)
    )


class ContentParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_article = False
        self.links: list[str] = []
        self.ids: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "article":
            self.in_article = True
        if identifier := values.get("id"):
            self.ids.add(identifier)
        if self.in_article:
            attribute = "src" if tag == "img" else "href" if tag == "a" else None
            if attribute and (link := values.get(attribute)):
                self.links.append(link)

    def handle_endtag(self, tag: str) -> None:
        if tag == "article":
            self.in_article = False


def _page_url(path: Path, site: Path) -> str:
    relative = path.relative_to(site).as_posix()
    if relative == "index.html":
        return _SITE_ORIGIN + _SITE_PREFIX
    if relative.endswith("/index.html"):
        return _SITE_ORIGIN + _SITE_PREFIX + relative[: -len("index.html")]
    return _SITE_ORIGIN + _SITE_PREFIX + relative


def _target_path(site: Path, path: str) -> Path:
    relative = unquote(path.removeprefix(_SITE_PREFIX))
    normalized = posixpath.normpath(relative).lstrip("/")
    if path.endswith("/") or normalized == ".":
        normalized = posixpath.join(normalized, "index.html")
    target = (site / normalized).resolve()
    if not target.is_relative_to(site.resolve()):
        raise ValueError("link escapes the documentation site")
    return target


def check_links(
    site: Path, *, external_results: list[ExternalCheck] | None = None
) -> tuple[int, int]:
    site = site.resolve()
    pages: dict[Path, ContentParser] = {}
    for page in sorted(site.rglob("*.html")):
        parser = ContentParser()
        parser.feed(page.read_text(encoding="utf-8"))
        pages[page.resolve()] = parser

    failures: list[str] = []
    external: set[str] = set()
    checked = 0
    for page, content in pages.items():
        section = page.relative_to(site).parts[0]
        for link in content.links:
            checked += 1
            destination = urlsplit(urljoin(_page_url(page, site), link))
            if destination.scheme not in {"http", "https"}:
                failures.append(f"{page.relative_to(site)}: unsupported link {link}")
                continue
            if destination.netloc not in {"www.ml4trading.io", "ml4trading.io"}:
                if section in _GUIDE_SECTIONS:
                    external.add(destination._replace(fragment="").geturl())
                continue
            if not destination.path.startswith(_SITE_PREFIX):
                if section in _GUIDE_SECTIONS:
                    external.add(destination._replace(fragment="").geturl())
                continue
            try:
                target = _target_path(site, destination.path)
            except ValueError as error:
                failures.append(f"{page.relative_to(site)}: {link}: {error}")
                continue
            if not target.is_file():
                failures.append(f"{page.relative_to(site)}: {link}: missing target")
                continue
            if destination.fragment and target.suffix == ".html":
                anchor = unquote(destination.fragment)
                if anchor not in pages[target].ids:
                    failures.append(f"{page.relative_to(site)}: {link}: missing anchor")

    with ThreadPoolExecutor(max_workers=8) as executor:
        for result in executor.map(verify_external, sorted(external)):
            if external_results is not None:
                external_results.append(result)
            if result.status != "verified_present":
                failures.append(f"{result.url}: {result.status}: {result.reason}")
    if failures:
        raise ValueError("Documentation link failures:\n" + "\n".join(failures))
    return checked, len(external)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--site", type=Path, default=Path("site"))
    args = parser.parse_args()
    results: list[ExternalCheck] = []
    try:
        checked, external = check_links(args.site, external_results=results)
    finally:
        for result in results:
            print(json.dumps(asdict(result), sort_keys=True))
    print(f"Checked {checked} rendered content links and {external} external destinations")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
