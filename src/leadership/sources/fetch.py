"""Polite HTTP fetching with a disk cache.

- Honors robots.txt for every host (fetched once per host per run, only on a cache miss).
- At most one request per second per host.
- Descriptive User-Agent. SEC requests use SEC_USER_AGENT instead (SEC policy).
- Never fetches linkedin.com (ToS), regardless of robots.txt.
- Every response, including errors, is cached under data/<domain>/raw/, so re-running
  discovery or extraction never re-fetches unless `refresh=True`.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import time
import urllib.robotparser
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
from selectolax.lexbor import LexborHTMLParser as HTMLParser

from leadership.config import REPO_ROOT
from leadership.models import utc_now_iso

log = logging.getLogger(__name__)

DEFAULT_USER_AGENT = (
    "LeadershipResearchBot/0.1 (take-home research project; public leadership pages only; "
    "honors robots.txt)"
)
BANNED_HOSTS = ("linkedin.com",)  # ToS forbids scraping. See DECISIONS.md D1.
MIN_INTERVAL_S = 1.0
TIMEOUT_S = 20.0
MAX_BYTES = 20_000_000  # SEC filings with inline XBRL can be several MB
MAX_REDIRECTS = 5
_DROP_TAGS = ("script", "style", "noscript", "svg", "template", "iframe", "head")


class FetchBlocked(Exception):
    """The fetch was not attempted: robots.txt, a banned host, or similar."""


@dataclass
class FetchResult:
    url: str
    final_url: str
    status: int
    content_type: str
    fetched_at: str
    body: bytes
    from_cache: bool
    blocked: str = ""  # set when a redirect hop was refused by policy (cached too)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def text_body(self) -> str:
        """Decode the body: the header charset if given, else UTF-8, else Windows-1252.

        The fallback matters for evidence checks: SEC filings often declare ASCII but contain
        cp1252 bytes (0x92 for a curly apostrophe). Decoding those as UTF-8 would turn
        "company's" into "company�s", and no quoted snippet would match the text.
        """
        charset = ""
        if "charset=" in self.content_type:
            charset = self.content_type.split("charset=")[-1].split(";")[0].strip(" \"'")
        if charset:
            try:
                return self.body.decode(charset)
            except (LookupError, UnicodeDecodeError):
                pass
        try:
            return self.body.decode("utf-8")
        except UnicodeDecodeError:
            return self.body.decode("cp1252", errors="replace")


def is_banned(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return any(host == b or host.endswith("." + b) for b in BANNED_HOSTS)


def url_key(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:20]


def repo_relative(path: Path) -> str:
    """Repo-relative POSIX path (portable across OSes); absolute only outside the repo."""
    resolved = path.resolve()
    if resolved.is_relative_to(REPO_ROOT):
        return resolved.relative_to(REPO_ROOT).as_posix()
    return resolved.as_posix()


def html_to_text(html: str) -> tuple[str, str]:
    """Visible text (one block per line) and the page title. Scripts/styles are dropped."""
    tree = HTMLParser(html)
    title_node = tree.css_first("title")
    title = title_node.text(strip=True) if title_node else ""
    tree.strip_tags(list(_DROP_TAGS))
    root = tree.body or tree.root
    raw = root.text(separator="\n") if root else ""
    lines = (" ".join(line.split()) for line in raw.splitlines())
    return "\n".join(line for line in lines if line), title


# Elements that start a new visual block. Inline elements (a, b, strong, span, ...) don't.
_BLOCK_TAGS = frozenset(
    {
        "address",
        "article",
        "aside",
        "blockquote",
        "body",
        "br",
        "caption",
        "dd",
        "details",
        "dialog",
        "div",
        "dl",
        "dt",
        "fieldset",
        "figcaption",
        "figure",
        "footer",
        "form",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "summary",
        "table",
        "tbody",
        "td",
        "tfoot",
        "th",
        "thead",
        "tr",
        "ul",
    }
)


def html_blocks(html: str) -> list[str]:
    """Text of each innermost block element, inline children joined with spaces.

    Used only to re-anchor evidence (validate.py): a name and a title count as one statement
    only if they sit in the same block, so a team grid can't pair a name with a neighbour's
    title. doc.text (what the LLM sees and evidence is checked against) is not changed.
    """
    tree = HTMLParser(html)
    tree.strip_tags(list(_DROP_TAGS))
    root = tree.body or tree.root
    blocks: list[str] = []
    buf: list[str] = []

    def flush() -> None:
        text = " ".join(" ".join(buf).split())
        if text:
            blocks.append(text)
        buf.clear()

    def walk(node) -> None:
        for child in node.iter(include_text=True):
            if child.tag == "-text":
                buf.append(child.text_content or "")
            elif child.tag in _BLOCK_TAGS:
                flush()
                walk(child)
                flush()
            else:
                walk(child)

    if root is not None:
        walk(root)
        flush()
    return blocks


class Fetcher:
    def __init__(
        self,
        cache_dir: Path,
        refresh: bool = False,
        transport: httpx.BaseTransport | None = None,  # tests inject a MockTransport
        min_interval_s: float = MIN_INTERVAL_S,
    ) -> None:
        self.cache_dir = cache_dir
        self.refresh = refresh
        # Redirects are followed by hand in get(), so every hop is policy-checked first.
        self._client = httpx.Client(timeout=TIMEOUT_S, follow_redirects=False, transport=transport)
        self.min_interval_s = min_interval_s
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._last_request: dict[str, float] = {}
        self.network_requests = 0

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Fetcher:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ politeness

    def _wait_turn(self, host: str) -> None:
        last = self._last_request.get(host)
        if last is not None:
            delay = self.min_interval_s - (time.monotonic() - last)
            if delay > 0:
                time.sleep(delay)
        self._last_request[host] = time.monotonic()

    def _robots_txt(self, base: str, user_agent: str) -> FetchResult:
        """robots.txt itself, through the same cache (so cached runs stay offline)."""
        robots_url = f"{base}/robots.txt"
        cached = self._load("_robots", robots_url)
        if cached is not None:
            return cached
        self._wait_turn(urlsplit(base).netloc)
        self.network_requests += 1
        fetched_at = utc_now_iso()
        try:
            resp = self._client.get(
                robots_url, headers={"User-Agent": user_agent}, follow_redirects=True
            )  # RFC 9309 allows following robots.txt redirects
            if any(is_banned(str(r.url)) for r in [*resp.history, resp]):
                raise httpx.HTTPError(f"robots.txt redirected to a banned host ({resp.url})")
            result = FetchResult(
                robots_url, str(resp.url), resp.status_code,
                resp.headers.get("content-type", ""), fetched_at, resp.content, False,
            )  # fmt: skip
        except httpx.HTTPError as exc:
            log.warning("robots.txt unreachable for %s: %s", base, exc)
            result = FetchResult(robots_url, robots_url, 0, "", fetched_at, b"", False)
        self._store("_robots", result)
        return result

    def _robots_for(self, url: str, user_agent: str) -> urllib.robotparser.RobotFileParser | None:
        parts = urlsplit(url)
        base = f"{parts.scheme}://{parts.netloc}"
        if base not in self._robots:
            res = self._robots_txt(base, user_agent)
            parser = urllib.robotparser.RobotFileParser()
            if res.status == 0:
                log.warning("treating %s as disallowed: robots.txt unreachable", base)
                self._robots[base] = None
                return None
            if res.status in (401, 403) or res.status >= 500:
                # 5xx: RFC 9309 says assume complete disallow. 401/403: CPython's rule (an
                # auth wall means stay out), stricter than the RFC.
                parser.parse(["User-agent: *", "Disallow: /"])
            elif res.status >= 400:
                parser.parse([])  # no robots.txt: everything allowed
            else:
                parser.parse(res.text_body().splitlines())
            self._robots[base] = parser
        return self._robots[base]

    def sitemaps_for(self, base: str) -> list[str]:
        parser = self._robots_for(base + "/", DEFAULT_USER_AGENT)
        return list(parser.site_maps() or []) if parser is not None else []

    def robots_verdict(self, url: str, user_agent: str = DEFAULT_USER_AGENT) -> tuple[int, bool]:
        """(robots.txt HTTP status, may we fetch url). Status 0 means unreachable.

        Lets callers tell an explicit Disallow (2xx robots.txt that forbids the path) apart
        from bot protection (robots.txt itself answered 401/403/5xx or was unreachable).
        """
        allowed = self.allowed(url, user_agent)
        parts = urlsplit(url)
        res = self._load("_robots", f"{parts.scheme}://{parts.netloc}/robots.txt")
        return (res.status if res is not None else 0), allowed

    def allowed(self, url: str, user_agent: str) -> bool:
        parser = self._robots_for(url, user_agent)
        return parser is not None and parser.can_fetch(user_agent, url)

    # ------------------------------------------------------------------ cache

    def _paths(self, namespace: str, url: str) -> tuple[Path, Path]:
        stem = self.cache_dir / namespace / url_key(url)
        return stem.with_suffix(".meta.json"), stem.with_suffix(".body.gz")

    def _load(self, namespace: str, url: str) -> FetchResult | None:
        meta_path, body_path = self._paths(namespace, url)
        if self.refresh or not meta_path.exists():
            return None
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        body = gzip.decompress(body_path.read_bytes()) if body_path.exists() else b""
        return FetchResult(
            url=meta["url"],
            final_url=meta["final_url"],
            status=meta["status"],
            content_type=meta["content_type"],
            fetched_at=meta["fetched_at"],
            body=body,
            from_cache=True,
            blocked=meta.get("blocked", ""),
        )

    def _store(self, namespace: str, result: FetchResult) -> None:
        meta_path, body_path = self._paths(namespace, result.url)
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta = {
            "url": result.url,
            "final_url": result.final_url,
            "status": result.status,
            "content_type": result.content_type,
            "fetched_at": result.fetched_at,
        }
        if result.blocked:
            meta["blocked"] = result.blocked
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        if result.ok and result.body:
            # mtime=0 keeps the gzip bytes identical across re-fetches of the same content.
            body_path.write_bytes(gzip.compress(result.body, mtime=0))

    def text_path(self, namespace: str, url: str) -> Path:
        return self.cache_dir / namespace / f"{url_key(url)}.txt"

    def write_text(self, namespace: str, url: str, text: str) -> str:
        """Cache the extracted text that extraction and the evidence check will use."""
        path = self.text_path(namespace, url)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        return repo_relative(path)

    # ------------------------------------------------------------------ fetch

    def _policy_block(self, url: str, user_agent: str, check_robots: bool) -> str:
        """Reason this URL must not be requested, or '' if it may be."""
        if is_banned(url):
            return f"{url}: host is banned by project policy (ToS)"
        if check_robots and not self.allowed(url, user_agent):
            return f"{url}: disallowed by robots.txt"
        return ""

    def get(
        self,
        url: str,
        namespace: str,
        user_agent: str = DEFAULT_USER_AGENT,
        check_robots: bool = True,
    ) -> FetchResult:
        """GET with cache. Raises FetchBlocked if policy forbids the request.

        Redirects are followed by hand: every hop is checked (banned host, robots.txt) and
        rate-limited *before* it is requested. A refused hop is cached as blocked, so warm
        runs stay offline. Network errors are logged and returned as status 0 (and cached),
        not raised, so one bad page doesn't stop a collection run.
        """
        if is_banned(url):
            raise FetchBlocked(f"{url}: host is banned by project policy (ToS)")
        cached = self._load(namespace, url)
        if cached is not None:
            if cached.blocked:
                raise FetchBlocked(cached.blocked)
            return cached

        fetched_at = utc_now_iso()
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            reason = self._policy_block(current, user_agent, check_robots)
            if reason:
                if current == url:
                    raise FetchBlocked(reason)  # nothing requested; nothing to cache
                blocked = FetchResult(url, current, 0, "", fetched_at, b"", False, reason)
                self._store(namespace, blocked)
                raise FetchBlocked(reason)
            self._wait_turn(urlsplit(current).netloc)
            self.network_requests += 1
            try:
                resp = self._client.get(current, headers={"User-Agent": user_agent})
            except httpx.HTTPError as exc:
                log.warning("fetch failed for %s: %s", current, exc)
                result = FetchResult(url, current, 0, "", fetched_at, b"", from_cache=False)
                self._store(namespace, result)
                return result
            if not resp.is_redirect:
                break
            current = urljoin(current, resp.headers.get("location", ""))
        else:
            result = FetchResult(url, current, 0, "", fetched_at, b"", False)
            log.warning("too many redirects for %s", url)
            self._store(namespace, result)
            return result

        final_url = current
        body = resp.content if len(resp.content) <= MAX_BYTES else b""
        if not body and resp.content:
            log.warning("skipping %s: body larger than %d bytes", url, MAX_BYTES)
        result = FetchResult(
            url=url,
            final_url=final_url,
            status=resp.status_code if body or not resp.content else 413,
            content_type=resp.headers.get("content-type", ""),
            fetched_at=fetched_at,
            body=body,
            from_cache=False,
        )
        self._store(namespace, result)
        return result
