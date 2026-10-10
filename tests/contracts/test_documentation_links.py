"""Rendered internal links and external guide URLs must resolve."""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlsplit

import pytest

_ROOT = Path(__file__).parents[2]


def _load_checker():
    path = _ROOT / "validation" / "check_research_documentation_links.py"
    spec = importlib.util.spec_from_file_location("ml4t_documentation_links", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_site(site: Path, href: str) -> None:
    page = site / "user-guide" / "index.html"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(f'<article><a href="{href}">Go</a></article>')
    destination = site / "tutorials" / "index.html"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text('<article><h2 id="working-section">Working</h2></article>')


def test_rendered_page_and_fragment_links(tmp_path: Path) -> None:
    checker = _load_checker()
    _write_site(tmp_path, "../tutorials/#working-section")
    assert checker.check_links(tmp_path) == (1, 0)

    _write_site(tmp_path, "../tutorials/#missing-section")
    with pytest.raises(ValueError, match="missing anchor"):
        checker.check_links(tmp_path)

    _write_site(tmp_path, "../absent/")
    with pytest.raises(ValueError, match="missing target"):
        checker.check_links(tmp_path)


def test_external_url_rejects_http_404(tmp_path: Path) -> None:
    checker = _load_checker()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200 if self.path == "/present" else 404)
            self.end_headers()

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        _write_site(tmp_path, base + "/present")
        assert checker.check_links(tmp_path) == (1, 1)
        _write_site(tmp_path, base + "/absent")
        with pytest.raises(ValueError, match="HTTP Error 404"):
            checker.check_links(tmp_path)
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_homepage_internal_fragment_is_checked(tmp_path: Path) -> None:
    checker = _load_checker()
    homepage = tmp_path / "index.html"
    guide = tmp_path / "user-guide" / "index.html"
    guide.parent.mkdir(parents=True)
    guide.write_text('<article><h2 id="working-section">Working</h2></article>')
    homepage.write_text('<article><a href="user-guide/#working-section">Guide</a></article>')
    assert checker.check_links(tmp_path) == (1, 0)

    homepage.write_text('<article><a href="user-guide/#missing-section">Guide</a></article>')
    with pytest.raises(ValueError, match="missing anchor"):
        checker.check_links(tmp_path)


_BLOB = "https://github.com/example/project/blob/main/docs/guide.ipynb"


def _contents(**changes):
    return json.dumps(
        {
            "type": "file",
            "path": "docs/guide.ipynb",
            "size": 12,
            "sha": "a" * 40,
            "html_url": _BLOB,
            "git_url": "https://api.github.com/repos/example/project/git/blobs/" + "a" * 40,
        }
        | changes
    ).encode()


class FakeResponse(io.BytesIO):
    def __init__(self, body, status=200, headers=None):
        super().__init__(body)
        self.status, self.headers = status, headers or {}


class ScriptedHTTP:
    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)
        self.calls, self.bodies = [], []

    def __call__(self, request, *, timeout):
        self.calls.append(request)
        assert timeout == 15
        outcome = next(self.outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, FakeResponse):
            self.bodies.append(outcome)
            return outcome
        status, body, headers = outcome
        response = FakeResponse(body, status, headers)
        self.bodies.append(response)
        if status >= 400:
            raise HTTPError(request.full_url, status, "synthetic", headers, response)
        return response


def test_blob_503_uses_matching_official_contents_evidence():
    checker = _load_checker()
    http = ScriptedHTTP([(503, b"unavailable", {})] * 3 + [(200, _contents(), {})])
    waits = []
    result = checker.verify_external(_BLOB, transport=http, sleeper=waits.append)
    assert result.status == "verified_present" and result.reason == "github_contents_file_present"
    assert [e["http_status"] for e in result.evidence[:-1]] == [503, 503, 503, 200]
    assert result.evidence[-1] == {
        "channel": "github_contents",
        "repository": "example/project",
        "ref": "main",
        "path": "docs/guide.ipynb",
        "blob_sha": "a" * 40,
        "size": 12,
    }
    parsed = urlsplit(http.calls[-1].full_url)
    assert (
        parsed.netloc == "api.github.com"
        and parsed.path == "/repos/example/project/contents/docs/guide.ipynb"
    )
    assert parse_qs(parsed.query) == {"ref": ["main"]}
    assert waits == [1, 2] and all(body.closed for body in http.bodies)


@pytest.mark.parametrize("status", [404, 410])
def test_web_missing_never_becomes_present_via_api(status):
    checker = _load_checker()
    http = ScriptedHTTP([(status, b"missing", {})])
    result = checker.verify_external(_BLOB, transport=http, sleeper=lambda _: None)
    assert result.status == "verified_missing" and len(http.calls) == 1
    assert all(body.closed for body in http.bodies)


@pytest.mark.parametrize("status", [404, 410])
def test_api_missing_after_web_unavailable_fails_closed(status):
    checker = _load_checker()
    http = ScriptedHTTP([(503, b"unavailable", {})] * 3 + [(status, b"missing", {})])
    result = checker.verify_external(_BLOB, transport=http, sleeper=lambda _: None)
    assert result.status == "verified_missing" and len(http.calls) == 4


@pytest.mark.parametrize("api_retry", [False, True])
def test_rate_limit_retries_are_bounded_and_retry_after_is_honored(api_retry):
    checker = _load_checker()
    outcomes = [(429, b"rate limit", {"Retry-After": "3"}), (200, _contents(), {})]
    if api_retry:
        outcomes = [(503, b"unavailable", {})] * 3 + outcomes
    http, waits = ScriptedHTTP(outcomes), []
    result = checker.verify_external(_BLOB, transport=http, sleeper=waits.append)
    assert result.status == "verified_present" and waits[-1] == 3
    assert len(http.calls) == (5 if api_retry else 2)
    assert all(body.closed for body in http.bodies)


def test_both_channels_unavailable_block_documentation_with_reason_evidence(tmp_path, monkeypatch):
    checker = _load_checker()
    http = ScriptedHTTP([(503, b"unavailable", {})] * 6)
    verify = checker.verify_external
    monkeypatch.setattr(
        checker, "verify_external", lambda url: verify(url, transport=http, sleeper=lambda _: None)
    )
    _write_site(tmp_path, _BLOB)
    results = []
    with pytest.raises(ValueError, match="temporarily_unverifiable"):
        checker.check_links(tmp_path, external_results=results)
    assert results[0].status == "temporarily_unverifiable" and len(results[0].evidence) == 6
    assert len(http.calls) == 6 and all(body.closed for body in http.bodies)


@pytest.mark.parametrize("error", [TimeoutError(), URLError("offline"), OSError("interrupted")])
def test_transport_failures_can_only_pass_with_official_file_evidence(error):
    checker = _load_checker()
    http = ScriptedHTTP([error] * 3 + [(200, _contents(), {})])
    result = checker.verify_external(_BLOB, transport=http, sleeper=lambda _: None)
    assert result.status == "verified_present" and len(result.evidence) == 5
    assert all(e["http_status"] is None for e in result.evidence[:3])


@pytest.mark.parametrize(
    "changes",
    [
        {"type": "dir"},
        {"path": "docs/other.ipynb"},
        {"sha": "bad"},
        {"size": True},
        {"size": -1},
        {"html_url": _BLOB.replace("main", "master")},
        {"html_url": _BLOB.replace("example", "another")},
        {"git_url": "https://api.github.com/repos/another/project/git/blobs/" + "a" * 40},
        {"submodule_git_url": "https://github.com/another/project"},
    ],
)
def test_api_200_for_another_or_invalid_file_is_not_presence_evidence(changes):
    checker = _load_checker()
    http = ScriptedHTTP([(503, b"unavailable", {})] * 3 + [(200, _contents(**changes), {})])
    result = checker.verify_external(_BLOB, transport=http, sleeper=lambda _: None)
    assert (
        result.status == "temporarily_unverifiable"
        and result.reason == "github_contents_identity_mismatch"
    )


@pytest.mark.parametrize("body", [b"<html>login</html>", b"[]", b"null"])
def test_api_nonfile_payload_is_not_silently_accepted(body):
    checker = _load_checker()
    http = ScriptedHTTP([(503, b"unavailable", {})] * 3 + [(200, body, {})])
    assert (
        checker.verify_external(_BLOB, transport=http, sleeper=lambda _: None).status
        == "temporarily_unverifiable"
    )


def test_oversized_api_metadata_fails_closed(monkeypatch):
    checker = _load_checker()
    monkeypatch.setattr(checker, "_API_RESPONSE_LIMIT", 10)
    http = ScriptedHTTP([(503, b"unavailable", {})] * 3 + [(200, _contents(), {})])
    result = checker.verify_external(_BLOB, transport=http, sleeper=lambda _: None)
    assert result.status == "temporarily_unverifiable" and "oversized_api_response" in result.reason
    assert all(body.closed for body in http.bodies)


@pytest.mark.parametrize("ref", ["b" * 40, "release/stable"])
def test_pinned_and_encoded_refs_preserve_exact_path_and_scope(ref):
    checker = _load_checker()
    path = "docs/教程 file.ipynb"
    url = f"https://github.com/example/project/blob/{quote(ref, safe='')}/{quote(path, safe='/')}"
    payload = _contents(
        path=path, html_url=f"https://github.com/example/project/blob/{ref}/{quote(path, safe='/')}"
    )
    http = ScriptedHTTP([(503, b"unavailable", {})] * 3 + [(200, payload, {})])
    result = checker.verify_external(url, transport=http, sleeper=lambda _: None)
    assert result.status == "verified_present"
    assert parse_qs(urlsplit(http.calls[-1].full_url).query) == {"ref": [ref]}
    assert result.evidence[-1]["path"] == path


@pytest.mark.parametrize(
    "url",
    [
        _BLOB.replace("github.com", "github.com.evil.invalid"),
        _BLOB.replace("github.com", "github.com:443"),
        _BLOB.replace("/blob/", "/tree/"),
        _BLOB.replace("/main/", "/feature/topic/"),
        _BLOB.replace("docs/guide.ipynb", "../guide.ipynb"),
        _BLOB.replace("docs/guide.ipynb", "%2e%2e/guide.ipynb"),
        _BLOB.replace("docs/guide.ipynb", "docs/%00bad"),
        _BLOB + "?raw=1",
    ],
)
def test_unsupported_or_ambiguous_blob_links_have_no_guessed_api_fallback(url):
    checker = _load_checker()
    http = ScriptedHTTP([(503, b"unavailable", {})] * 3)
    result = checker.verify_external(url, transport=http, sleeper=lambda _: None)
    assert result.status == "temporarily_unverifiable" and len(http.calls) == 3


def test_api_credentials_stay_on_fixed_origin_and_are_not_in_evidence(monkeypatch):
    checker = _load_checker()
    token = "synthetic-link-token"
    monkeypatch.setenv("GITHUB_TOKEN", token)
    http = ScriptedHTTP([(503, b"unavailable", {})] * 3 + [(200, _contents(), {})])
    monkeypatch.setattr(checker, "urlopen", http)
    handlers = []

    class Opener:
        def open(self, request, *, timeout):
            return http(request, timeout=timeout)

    monkeypatch.setattr(
        checker, "build_opener", lambda handler: handlers.append(handler) or Opener()
    )
    result = checker.verify_external(_BLOB, sleeper=lambda _: None)
    assert result.status == "verified_present"
    assert all(request.get_header("Authorization") is None for request in http.calls[:3])
    assert http.calls[-1].get_header("Authorization") == "Bearer " + token
    assert (
        handlers[0].redirect_request(None, None, 302, "redirect", {}, "https://evil.invalid")
        is None
    )
    assert token not in json.dumps(asdict(result))


def test_internal_anchor_failure_still_blocks_when_external_api_verifies_present(
    tmp_path, monkeypatch
):
    checker = _load_checker()
    http = ScriptedHTTP([(503, b"unavailable", {})] * 3 + [(200, _contents(), {})])
    verify = checker.verify_external
    monkeypatch.setattr(
        checker, "verify_external", lambda url: verify(url, transport=http, sleeper=lambda _: None)
    )
    _write_site(tmp_path, "../tutorials/#missing-section")
    page = tmp_path / "user-guide" / "index.html"
    page.write_text(
        page.read_text().replace("</article>", f'<a href="{_BLOB}">Source</a></article>')
    )
    results = []
    with pytest.raises(ValueError, match="missing anchor"):
        checker.check_links(tmp_path, external_results=results)
    assert results[0].status == "verified_present"


def test_api_read_timeout_closes_response_and_retries_before_verifying():
    checker = _load_checker()

    class Interrupted(FakeResponse):
        def read(self, bound):
            raise TimeoutError("interrupted metadata")

    interrupted = Interrupted(b"partial")
    http = ScriptedHTTP([(503, b"unavailable", {})] * 3 + [interrupted, (200, _contents(), {})])
    waits = []
    result = checker.verify_external(_BLOB, transport=http, sleeper=waits.append)
    assert result.status == "verified_present" and len(http.calls) == 5
    assert interrupted.closed and waits == [1, 2, 1]
