"""Team-scoped RSS/Atom ingestion for qualitative market research."""
from __future__ import annotations

import hashlib
import html
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from xml.etree import ElementTree

import yaml
import requests

from .research import utc_now


ATOM = "{http://www.w3.org/2005/Atom}"
CONTENT = "{http://purl.org/rss/1.0/modules/content/}"


@dataclass(frozen=True)
class ResearchTeam:
    key: str
    canonical_name: str
    aliases: tuple[str, ...]
    rss_feeds: tuple[dict[str, Any], ...]
    subreddits: tuple[str, ...]


def load_research_teams(path: Any) -> list[ResearchTeam]:
    doc = yaml.safe_load(path.read_text()) if hasattr(path, "read_text") else yaml.safe_load(str(path))
    teams = (doc or {}).get("teams") or {}
    out: list[ResearchTeam] = []
    for key, row in teams.items():
        if not isinstance(row, dict):
            continue
        canonical = str(row.get("canonical_name") or key).strip()
        aliases = [canonical]
        aliases.extend(str(alias).strip() for alias in (row.get("aliases") or []) if str(alias).strip())
        feeds = []
        for item in row.get("rss_feeds") or row.get("feeds") or []:
            if isinstance(item, str):
                feeds.append({"name": item, "url": item})
            elif isinstance(item, dict) and item.get("url"):
                feeds.append(dict(item))
        subreddits = tuple(str(name).strip() for name in (row.get("subreddits") or []) if str(name).strip())
        out.append(ResearchTeam(
            key=str(key),
            canonical_name=canonical,
            aliases=tuple(dict.fromkeys(aliases)),
            rss_feeds=tuple(feeds),
            subreddits=subreddits,
        ))
    return out


def _strip_html(raw: Any) -> str:
    text = html.unescape(str(raw or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _child_text(node: ElementTree.Element, *names: str) -> str:
    for name in names:
        child = node.find(name)
        if child is not None and child.text:
            return _strip_html(child.text)
    return ""


def _link(node: ElementTree.Element) -> str:
    link = _child_text(node, "link")
    if link:
        return link
    atom_link = node.find(f"{ATOM}link")
    if atom_link is not None:
        return str(atom_link.attrib.get("href") or "").strip()
    return ""


def parse_feed_datetime(raw: Any) -> str | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        parsed = None
    if parsed is None:
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _published_at(node: ElementTree.Element) -> str | None:
    raw = _child_text(node, "pubDate", f"{ATOM}published", f"{ATOM}updated", "updated")
    return parse_feed_datetime(raw)


def parse_feed(payload: bytes | str, *, source: str, team: str) -> list[dict[str, Any]]:
    raw = payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else str(payload)
    root = ElementTree.fromstring(raw)
    rss_items = root.findall("./channel/item")
    atom_items = root.findall(f".//{ATOM}entry")
    nodes = rss_items or atom_items
    out = []
    for node in nodes:
        title = _child_text(node, "title", f"{ATOM}title")
        body = _child_text(
            node,
            "description",
            f"{CONTENT}encoded",
            f"{ATOM}summary",
            f"{ATOM}content",
        )
        url = _link(node)
        if not title and not body:
            continue
        content_hash = news_content_hash(team, source, title, body, url)
        out.append({
            "id": content_hash[:24],
            "team": team,
            "source": source,
            "title": title,
            "body": body,
            "url": url,
            "published_at": _published_at(node),
            "fetched_at": utc_now(),
            "content_hash": content_hash,
        })
    return out


def news_content_hash(team: str, source: str, title: str, body: str, url: str) -> str:
    raw = "\n".join([
        str(team or "").strip().lower(),
        str(source or "").strip().lower(),
        str(url or "").strip().lower(),
        str(title or "").strip().lower(),
        str(body or "").strip().lower(),
    ])
    return hashlib.sha256(raw.encode()).hexdigest()


def item_matches_team(item: dict[str, Any], team: ResearchTeam) -> bool:
    text = f"{item.get('title') or ''} {item.get('body') or ''}".lower()
    return any(str(alias).lower() in text for alias in team.aliases if alias)


def _is_recent(item: dict[str, Any], *, window_hours: float, now: datetime | None = None) -> bool:
    published = item.get("published_at")
    if not published:
        return True
    try:
        parsed = datetime.fromisoformat(str(published).replace("Z", "+00:00"))
    except ValueError:
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return parsed.astimezone(timezone.utc) >= now - timedelta(hours=float(window_hours))


def fetch_url(url: str, *, user_agent: str, timeout: float = 20.0) -> tuple[int, bytes]:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": user_agent,
            "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml;q=0.9,*/*;q=0.5",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            status = int(getattr(response, "status", 200) or 200)
            return status, response.read()
    except urllib.error.URLError:
        response = requests.get(
            url,
            headers={
                "User-Agent": user_agent,
                "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml;q=0.9,*/*;q=0.5",
            },
            timeout=timeout,
        )
        if response.status_code >= 400:
            raise urllib.error.HTTPError(url, response.status_code, response.reason, response.headers, None)
        return int(response.status_code), response.content


def feed_sources(team: ResearchTeam) -> list[dict[str, Any]]:
    sources = []
    for feed in team.rss_feeds:
        source = dict(feed)
        source.setdefault("name", source.get("url"))
        source.setdefault("kind", "rss")
        sources.append(source)
    for subreddit in team.subreddits:
        sources.append({
            "name": f"reddit_{subreddit}",
            "url": f"https://www.reddit.com/r/{subreddit}/new.rss",
            "kind": "reddit_rss",
            "require_alias": subreddit.lower() == "baseball",
        })
    return sources


def ingest_team_feeds(
    *,
    teams: list[ResearchTeam],
    store: Any,
    user_agent: str,
    window_hours: float = 48,
    request_delay_seconds: float = 0.25,
    fetcher: Any = fetch_url,
    now: datetime | None = None,
) -> dict[str, Any]:
    store.init_schema()
    team_counts = {team.key: {"parsed": 0, "inserted": 0, "ignored_old": 0, "discarded_alias": 0} for team in teams}
    source_status = []
    for team in teams:
        for source in feed_sources(team):
            status_row = {
                "team": team.key,
                "canonical_name": team.canonical_name,
                "source": source.get("name"),
                "url": source.get("url"),
                "kind": source.get("kind", "rss"),
                "status": "unknown",
                "http_status": None,
                "parsed": 0,
                "inserted": 0,
                "ignored_old": 0,
                "discarded_alias": 0,
            }
            try:
                http_status, body = fetcher(str(source["url"]), user_agent=user_agent)
                status_row["http_status"] = http_status
            except urllib.error.HTTPError as exc:
                status_row["http_status"] = exc.code
                status_row["status"] = f"http_{exc.code}"
                status_row["error"] = str(exc)
                source_status.append(status_row)
                continue
            except Exception as exc:
                status_row["status"] = f"error:{exc.__class__.__name__}"
                status_row["error"] = str(exc)
                source_status.append(status_row)
                continue

            if status_row["http_status"] in {403, 429}:
                status_row["status"] = f"http_{status_row['http_status']}"
                source_status.append(status_row)
                continue
            try:
                items = parse_feed(body, source=str(source.get("name") or source.get("url")), team=team.key)
            except (ElementTree.ParseError, UnicodeDecodeError) as exc:
                status_row["status"] = f"parse_error:{exc.__class__.__name__}"
                status_row["error"] = str(exc)
                source_status.append(status_row)
                continue

            parsed = 0
            filtered = []
            ignored_old = 0
            discarded_alias = 0
            for item in items:
                if bool(source.get("require_alias")) and not item_matches_team(item, team):
                    discarded_alias += 1
                    continue
                if not _is_recent(item, window_hours=window_hours, now=now):
                    ignored_old += 1
                    continue
                parsed += 1
                filtered.append(item)
            inserted = store.record_news_items(filtered)
            status_row.update({
                "status": "ok",
                "parsed": parsed,
                "inserted": inserted,
                "ignored_old": ignored_old,
                "discarded_alias": discarded_alias,
            })
            team_counts[team.key]["parsed"] += parsed
            team_counts[team.key]["inserted"] += inserted
            team_counts[team.key]["ignored_old"] += ignored_old
            team_counts[team.key]["discarded_alias"] += discarded_alias
            source_status.append(status_row)
            if request_delay_seconds > 0:
                time.sleep(request_delay_seconds)

    return {
        "generated_at": utc_now(),
        "window_hours": float(window_hours),
        "team_counts": team_counts,
        "source_status": source_status,
        "total_parsed": sum(row["parsed"] for row in team_counts.values()),
        "total_inserted": sum(row["inserted"] for row in team_counts.values()),
    }
