#!/usr/bin/env python3
"""Crawl HLS/FLV links from configured match pages into an M3U playlist.

Only collects stream URLs actually exposed by the configured pages/browser requests.
No match-status filter is applied: scheduled, live and finished match pages are scanned.
"""
import argparse
import asyncio
import html
import json
import os
import re
import sys
import unicodedata
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urljoin, urlparse

import requests

UA = ("Mozilla/5.0 (Linux; Android 11) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36")
VN = timezone(timedelta(hours=7))

STREAM_EXT = r'(?:m3u8|flv|mpd)'
STREAM_RE = re.compile(
    rf'https?://[^\s"\'<>\\]+?\.{STREAM_EXT}(?:[?\#][^\s"\'<>\\]*)?', re.I
)
REL_STREAM_RE = re.compile(
    rf'["\']([^"\'\s<>]+?\.{STREAM_EXT}(?:[?\#][^"\'\s<>]*)?)["\']', re.I
)
IFRAME_RE = re.compile(r'<iframe[^>]+src=["\']([^"\']+)["\']', re.I)
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
TIME_RE = re.compile(r"luc-(\d{2})(\d{2})-ngay-(\d{2})-(\d{2})-(\d{4})", re.I)
BLV_RE = re.compile(r"\bBLV\b\s*[:\-]?\s*([^\n|,;]{2,60})", re.I)
PAGE_META_JS = r"""() => {
  const q = s => document.querySelector(s) || {};
  const a = [...document.querySelectorAll('a[href]')];
  const cards = {};
  for (const el of a) {
    let p = el, candidates = [];
    for (let i=0; i<7 && p; i++, p=p.parentElement) {
      const t = (p.innerText || '').replace(/\s+/g, ' ').trim();
      if (t.length >= 12 && t.length <= 1800) candidates.push(t);
    }
    // Prefer the smallest ancestor containing matchup + date/time, rather than
    // the anchor's short label (often only "Xem ngay" or "Trực tiếp").
    const ranked = candidates.map(t => ({t, score:
      (/\b(vs|v)\b/i.test(t) ? 8 : 0) +
      (/(?:\d{1,2}:\d{2}\s*[-–/]\s*\d{1,2}[/-]\d{1,2}|\d{1,2}[/-]\d{1,2}[/-]20\d{2})/.test(t) ? 6 : 0) +
      (/(\d{1,2}:\d{2})/.test(t) ? 2 : 0) - Math.max(0, t.length-350)/350
    }));
    ranked.sort((x,y) => y.score-x.score || x.t.length-y.t.length);
    if (ranked.length) cards[el.href] = ranked[0].t;
  }
  return {
    img: q('meta[property="og:image"]').content || '',
    h1: (q('h1').innerText || '').trim(),
    title: (q('meta[property="og:title"]').content || document.title || '').trim(),
    description: q('meta[name="description"]').content || '',
    body: (document.body && document.body.innerText || '').slice(0, 30000),
    scripts: [...document.scripts].map(s => s.textContent || '').join('\n').slice(0, 150000),
    media: [...document.querySelectorAll('iframe[src], video[src], video source[src], source[src]')]
      .map(el => el.src || el.getAttribute('src') || '').filter(Boolean),
    teamImages: [...document.querySelectorAll('img, [style*="background-image"]')].map(el => {
      const parent = el.closest('a, li, article, [class*="team"], [class*="club"], [class*="match"]') || el.parentElement;
      const style = el.getAttribute('style') || '';
      const bg = style.match(/url\(["']?([^"')]+)["']?\)/i);
      return {
        src: el.currentSrc || el.src || el.getAttribute('data-src') || el.getAttribute('data-lazy-src') || (bg && bg[1]) || '',
        alt: el.getAttribute('alt') || '', title: el.getAttribute('title') || '',
        cls: typeof el.className === 'string' ? el.className : '',
        parentText: (parent && parent.innerText || '').slice(0, 240)
      };
    }).filter(x => x.src),
    cards
  };
}"""


def fetch(url, referer=None):
    headers = {"User-Agent": UA}
    if referer:
        headers["Referer"] = referer
    r = requests.get(url, headers=headers, timeout=20)
    r.raise_for_status()
    return r.text


def discover_source_logo(home_url):
    """Find a real station logo URL from the source homepage for M3U tvg-logo."""
    try:
        raw = fetch(home_url)
    except Exception as exc:
        print(f"    [i] Không đọc được logo tự động từ {home_url}: {exc}", file=sys.stderr)
        return ""

    image_candidates = []
    for tag in re.findall(r"<img\b[^>]*>", raw, re.I):
        attrs = {}
        for key, double_value, single_value in re.findall(
                r'([\w:-]+)\s*=\s*(?:"([^"]*)"|\'([^\']*)\')', tag, re.I):
            attrs[key.lower()] = html.unescape((double_value or single_value).strip())
        src = (attrs.get("src") or attrs.get("data-src") or attrs.get("data-lazy-src")
               or attrs.get("data-original") or "").strip()
        if not src or src.startswith("data:"):
            continue
        absolute = urljoin(home_url, src)
        hint = " ".join(attrs.get(k, "") for k in ("alt", "title", "class", "id", "src")).lower()
        score = (100 if "logo" in hint else 0) + (35 if "brand" in hint else 0)
        if any(word in hint for word in ("avatar", "banner", "slider")):
            score -= 50
        image_candidates.append((score, absolute))

    if image_candidates:
        image_candidates.sort(key=lambda item: item[0], reverse=True)
        if image_candidates[0][0] > 0:
            return image_candidates[0][1]

    # Metadata and site icon are fallbacks when no logo-named image exists.
    for pattern in (
        r"<meta[^>]+(?:property|name)=\"(?:og:logo|twitter:image)\"[^>]+content=\"([^\"]+)",
        r"<meta[^>]+property=\"og:image\"[^>]+content=\"([^\"]+)",
        r"<link[^>]+rel=\"[^\"]*(?:icon|shortcut icon)[^\"]*\"[^>]+href=\"([^\"]+)",
    ):
        match = re.search(pattern, raw, re.I)
        if match:
            candidate = urljoin(home_url, html.unescape(match.group(1).strip()))
            if candidate.startswith(("http://", "https://")):
                return candidate
    return ""


def normalize(text):
    text = html.unescape(text)
    return text.replace("\\/", "/").replace("\\u0026", "&").replace("\\u003A", ":")


def extract_stream_candidates(page_url, *contents):
    """Find stream URLs exposed in HTML/inline player configuration, even if not requested yet."""
    found = set()
    for content in contents:
        raw = normalize(str(content or ""))
        found.update(STREAM_RE.findall(raw))
        for relative in REL_STREAM_RE.findall(raw):
            found.add(urljoin(page_url, relative))
    return {u for u in found if _is_stream_url(u)}


def _is_stream_url(url):
    return bool(re.search(r"\.(?:m3u8|flv|mpd)(?:$|[?#])", url, re.I))


def stream_format(url, content_type=""):
    path = urlparse(url).path.lower()
    ctype = (content_type or "").lower()
    if path.endswith(".flv") or "flv" in ctype:
        return "FLV"
    if path.endswith(".mpd") or "dash+xml" in ctype:
        return "DASH"
    if path.endswith(".m3u8") or "mpegurl" in ctype:
        return "HLS"
    return ""


def extract_static(page_url, depth=1, seen=None):
    """Extract direct HLS/FLV references and follow a limited number of iframes."""
    seen = seen if seen is not None else set()
    if page_url in seen:
        return []
    seen.add(page_url)
    try:
        raw = normalize(fetch(page_url))
    except Exception as e:
        print(f"[!] Lỗi tải {page_url}: {e}", file=sys.stderr)
        return []
    title_m = TITLE_RE.search(raw)
    title = re.sub(r"\s+", " ", title_m.group(1)).strip() if title_m else urlparse(page_url).netloc
    found = set(STREAM_RE.findall(raw))
    for rel in REL_STREAM_RE.findall(raw):
        found.add(urljoin(page_url, rel))
    items = [{"url": u, "match": title, "referer": page_url} for u in sorted(found)]
    if depth > 0:
        for src in IFRAME_RE.findall(raw):
            items += extract_static(urljoin(page_url, src), depth - 1, seen)
    return items


def _slug(link):
    return urlparse(link).path.rstrip("/").split("/")[-1]


def slug_to_name(link):
    slug = re.sub(r"-[a-z0-9]{12,}$", "", _slug(link))
    slug = re.sub(r"-luc-\d{4}-ngay-.*$", "", slug, flags=re.I)
    return slug.replace("-", " ").title()


def slug_to_time(link):
    m = TIME_RE.search(link)
    return f"{m.group(3)}/{m.group(4)} {m.group(1)}:{m.group(2)}" if m else ""


def slug_to_start(link):
    m = TIME_RE.search(link)
    return (f"{m.group(5)}-{m.group(4)}-{m.group(3)}T"
            f"{m.group(1)}:{m.group(2)}:00+07:00") if m else ""


def timestamp_to_vn(value):
    try:
        n = int(float(value))
        if n > 10**12:
            n /= 1000
        return datetime.fromtimestamp(n, tz=timezone.utc).astimezone(VN)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def extract_time(link, text):
    """Extract match kick-off from source JSON/page text, then fall back to URL slug."""
    text = text or ""
    timestamp_patterns = (
        r"""["'](?:matchTime|startTime|start_time|kickoff)["']\s*:\s*["']?(\d{10,13})""",
    )
    for pattern in timestamp_patterns:
        m = re.search(pattern, text, re.I)
        if m:
            dt = timestamp_to_vn(m.group(1))
            if dt:
                return dt.strftime("%d/%m %H:%M"), dt.isoformat()

    # ISO date/time fields embedded in JSON or data attributes.
    m = re.search(r"(20\d{2}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)", text)
    if m:
        raw = m.group(1).replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=VN)
            dt = dt.astimezone(VN)
            return dt.strftime("%d/%m %H:%M"), dt.isoformat()
        except ValueError:
            pass

    # Common Vietnamese match-card date formats.
    patterns = (
        # Ném Biên TV match pages/cards commonly render: 11:05-14/11/2026
        (r"\b(\d{1,2}):(\d{2})\s*[-–—]\s*(\d{1,2})/(\d{1,2})/(20\d{2})\b", "time-date-full"),
        (r"\b(\d{1,2})[/.](\d{1,2})[/.](20\d{2})\s+(\d{1,2}):(\d{2})\b", "full"),
        (r"\b(\d{1,2})/(\d{1,2})\s+(\d{1,2}):(\d{2})\b", "day-first"),
        (r"\b(\d{1,2}):(\d{2})\s*[-–— ]\s*(\d{1,2})/(\d{1,2})\b", "time-first"),
    )
    for pattern, kind in patterns:
        m = re.search(pattern, text)
        if not m:
            continue
        if kind == "time-date-full":
            h, mi, d, mo, year = m.groups()
            try:
                dt = datetime(int(year), int(mo), int(d), int(h), int(mi), tzinfo=VN)
            except ValueError:
                continue
            return dt.strftime("%d/%m %H:%M"), dt.isoformat()
        if kind == "full":
            d, mo, year, h, mi = m.groups()
            try:
                dt = datetime(int(year), int(mo), int(d), int(h), int(mi), tzinfo=VN)
            except ValueError:
                continue
            return dt.strftime("%d/%m %H:%M"), dt.isoformat()
        if kind == "day-first":
            d, mo, h, mi = m.groups()
        else:
            h, mi, d, mo = m.groups()
        try:
            datetime(datetime.now(VN).year, int(mo), int(d), int(h), int(mi), tzinfo=VN)
        except ValueError:
            continue
        return f"{int(d):02d}/{int(mo):02d} {int(h):02d}:{mi}", ""

    return slug_to_time(link), slug_to_start(link)


def extract_card_matchup(text):
    """Supplementary parser for match-card text; supports league + teams + time layouts.

    Example: 'SV.League Women - Hisamitsu Springs W VS PFU Blue Cats W -11:05-14/11/2026'.
    It deliberately returns only the matchup and does not replace already valid metadata.
    """
    if not text:
        return ""
    value = html.unescape(text).replace("\xa0", " ")
    value = re.sub(r"\s+", " ", value).strip()
    # Remove trailing kickoff/date and common live/status annotations before parsing teams.
    value = re.sub(
        r"\s*[-–—]?\s*\d{1,2}:\d{2}\s*[-–—/]\s*\d{1,2}[/.]\d{1,2}(?:[/.]20\d{2})?.*$",
        "", value, flags=re.I
    )
    value = re.sub(r"\s+(?:LIVE|HT|FT|TRỰC TIẾP|SẮP DIỄN RA|ĐÃ KẾT THÚC)\b.*$", "", value, flags=re.I)
    # Pick the smallest segment that actually contains the two sides, discarding league prefix.
    segments = re.split(r"\s*[|;\n]+\s*|\s+[-–—]\s+", value)
    candidates = [seg.strip(" :-–—") for seg in segments if re.search(r"\b(?:vs\.?|v\.?|versus)\b", seg, re.I)]
    if not candidates:
        candidates = [value]
    for candidate in candidates:
        m = re.search(r"(.{2,100}?)\s+\b(?:vs\.?|v\.?|versus)\b\s+(.{2,100})", candidate, re.I)
        if not m:
            continue
        left, right = (re.sub(r"\s+", " ", x).strip(" -:|") for x in m.groups())
        # Avoid accepting labels/status fragments as team names.
        if len(left) >= 2 and len(right) >= 2:
            return f"{left} vs {right}"[:140]
    return ""


def extract_card_time(text):
    """Supplementary card-time parser for ' -11:05-14/11/2026' and variants."""
    if not text:
        return "", ""
    patterns = (
        r"(?:^|\s)[-–—]?\s*(\d{1,2}):(\d{2})\s*[-–—/]\s*(\d{1,2})/(\d{1,2})/(20\d{2})\b",
        r"\b(\d{1,2})/(\d{1,2})/(20\d{2})\s+(\d{1,2}):(\d{2})\b",
    )
    value = html.unescape(text).replace("\\u0026", "&")
    for index, pattern in enumerate(patterns):
        m = re.search(pattern, value)
        if not m:
            continue
        try:
            if index == 0:
                hour, minute, day, month, year = map(int, m.groups())
            else:
                day, month, year, hour, minute = map(int, m.groups())
            dt = datetime(year, month, day, hour, minute, tzinfo=VN)
        except ValueError:
            continue
        return dt.strftime("%d/%m %H:%M"), dt.isoformat()
    return "", ""


def extract_card_score(text):
    """Supplementary score parser for scoreboard labels not covered by extract_score."""
    if not text:
        return ""
    value = html.unescape(text).replace("\xa0", " ")
    patterns = (
        r"(?:tỉ\s*số|ty\s*so|score|kết\s*quả|result)\s*[:：]?\s*(\d{1,2})\s*[-:：]\s*(\d{1,2})",
        r"\b(?:HT|FT|LIVE|SET\s*\d+|HIỆP\s*\d+)\s*[:：-]?\s*(\d{1,2})\s*[-:：]\s*(\d{1,2})\b",
    )
    for pattern in patterns:
        m = re.search(pattern, value, re.I)
        if m:
            return f"{m.group(1)}-{m.group(2)}"
    return ""


def merge_match_metadata(existing, supplemental):
    """Fill absent/weak fields from card parsing while preserving valid extracted values."""
    result = dict(existing or {})
    for key, value in (supplemental or {}).items():
        if value and not result.get(key):
            result[key] = value
    # A page title can be a league/site label; use the card matchup only when no matchup exists.
    if supplemental and supplemental.get("match") and not is_match_title(result.get("match", "")):
        result["match"] = supplemental["match"]
    return result


def _team_key(value):
    value = unicodedata.normalize("NFKD", html.unescape(str(value or "")))
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def extract_team_names(match):
    """Return home/away team names from a normalized 'A vs B' match title."""
    value = clean_match_title(match or "")
    parts = re.split(r"\s+(?:vs\.?|v\.?|versus)\s+", value, maxsplit=1, flags=re.I)
    return (parts[0].strip(), parts[1].strip()) if len(parts) == 2 else ("", "")


def extract_team_logos(images, match):
    """Match actual page image URLs to home/away team names; never use broadcaster logos."""
    home, away = extract_team_names(match)
    if not home or not away:
        return "", ""

    def score_image(image, team):
        fields = [image.get(k, "") for k in ("alt", "title", "cls", "parentText", "src")]
        haystack = _team_key(" ".join(map(str, fields)))
        needle = _team_key(team)
        if not haystack or not needle:
            return 0
        if needle in haystack:
            return 100
        tokens = [t for t in needle.split() if len(t) >= 3]
        if not tokens:
            return 0
        hits = sum(1 for token in tokens if token in haystack.split())
        return int(70 * hits / len(tokens)) if hits else 0

    selected = []
    for team in (home, away):
        ranked = sorted(
            ((score_image(image, team), image.get("src", "")) for image in (images or [])),
            key=lambda pair: pair[0], reverse=True
        )
        best_score, best_url = ranked[0] if ranked else (0, "")
        if best_score < 50 or not str(best_url).startswith(("http://", "https://")):
            selected.append("")
        else:
            selected.append(str(best_url))
    return tuple(selected)

def extract_commentator(text):
    m = BLV_RE.search(text or "")
    if m:
        value = re.split(r"\b(?:HT|FT|LIVE|TRỰC TIẾP|SẮP DIỄN RA)\b", m.group(1), flags=re.I)[0]
        return re.sub(r"\s+", " ", value).strip(" :-|")
    return ""


def extract_score(text):
    """Extract current score first, then explicit score or HT/FT fallback."""
    if not text:
        return ""
    patterns = [
        r"\b(\d{1,2})\s*:\s*(\d{1,2})(?=\s*(?:Hiệp|Xem ngay|LIVE|Trực tiếp))",
        r"(?:tỉ số|ty so|score|kết quả)\s*[:\-]?\s*(\d{1,2}\s*[-:]\s*\d{1,2})",
        r"\b(?:HT|FT)\s*(\d{1,2})\s*[-:]\s*(\d{1,2})\b",
    ]
    for p in patterns:
        m = re.search(p, text, re.I)
        if m:
            if m.lastindex == 1:
                return re.sub(r"\s+", "", m.group(1))
            return f"{m.group(1)}-{m.group(2)}"
    return ""


def extract_status(text, include_generic_live=True):
    """Classify a match from visible card/page text; live markers take precedence."""
    value = (text or "").lower()
    live_pattern = r"\b(live|đang diễn ra|hiệp 1|hiệp 2|halftime|hiệp phụ|penalty)\b"
    if include_generic_live:
        live_pattern = r"\b(live|đang diễn ra|trực tiếp|hiệp 1|hiệp 2|halftime|hiệp phụ|penalty)\b"
    if re.search(live_pattern, value):
        return "live"
    if re.search(r"\b(ft|full.?time|đã kết thúc|kết thúc|finished|full time)\b", value):
        return "finished"
    if re.search(r"\b(sắp diễn ra|chưa bắt đầu|upcoming|scheduled|kick.?off|khởi tranh)\b", value):
        return "scheduled"
    return "unknown"


def parse_match_datetime(item, now=None):
    """Return a timezone-aware kick-off datetime from the crawler's normalized fields."""
    now = now or datetime.now(VN)
    raw = item.get("start") or ""
    if raw:
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=VN)
            return dt.astimezone(VN)
        except ValueError:
            pass
    label = item.get("when") or ""
    m = re.search(r"\b(\d{1,2})/(\d{1,2})(?:/(20\d{2}))?\s+(\d{1,2}):(\d{2})\b", label)
    if not m:
        return None
    day, month, year, hour, minute = m.groups()
    year = int(year) if year else now.year
    try:
        dt = datetime(year, int(month), int(day), int(hour), int(minute), tzinfo=VN)
    except ValueError:
        return None
    if not m.group(3) and dt < now - timedelta(days=180):
        try:
            dt = dt.replace(year=year + 1)
        except ValueError:
            return None
    return dt


def match_in_playlist_window(item, now=None):
    """Keep live matches plus scheduled matches kicking off today or tomorrow (VN time)."""
    now = now or datetime.now(VN)
    status = item.get("status", "unknown")
    if status == "live":
        return True
    if status == "finished":
        return False
    kickoff = parse_match_datetime(item, now)
    if kickoff is None:
        # Explicitly scheduled cards without a parseable time cannot be safely date-filtered.
        return False
    today = now.date()
    tomorrow = today + timedelta(days=1)
    return now <= kickoff <= datetime.combine(tomorrow, datetime.max.time(), tzinfo=VN)


def match_id(link):
    qs = parse_qs(urlparse(link).query)
    for key in ("houseId", "scheduleId", "id"):
        if qs.get(key) and qs[key][0]:
            return qs[key][0]
    for seg in urlparse(link).path.split("/"):
        if re.fullmatch(r"\d{4,}", seg):
            return seg
    m = re.search(r"-([a-z0-9]{12,})$", _slug(link))
    return m.group(1) if m else ""


def _is_stream_response(response):
    if _is_stream_url(response.url):
        return True
    try:
        content_type = (response.headers.get("content-type") or "").lower()
        return any(x in content_type for x in (
            "mpegurl", "x-flv", "video/flv", "application/flv", "dash+xml"
        ))
    except Exception:
        return False


async def _record_async_response(response, urls, url_refs=None):
    """Record manifest/video responses and retain the player-frame Referer when known."""
    accepted = _is_stream_url(response.url)
    if not accepted:
        try:
            headers = await response.all_headers()
            content_type = (headers.get("content-type") or "").lower()
            accepted = any(x in content_type for x in (
                "mpegurl", "x-flv", "video/flv", "application/flv", "dash+xml"
            ))
        except Exception:
            accepted = False
    if accepted:
        urls.add(response.url)
        if url_refs is not None:
            try:
                frame_url = response.request.frame.url
                if frame_url and frame_url != "about:blank":
                    url_refs.setdefault(response.url, frame_url)
            except Exception:
                pass


async def _scan_match(browser, link, sem, card_text="", wait_ms=6000):
    """Scan one match page for every HLS/FLV request and extract visible match metadata."""
    async with sem:
        urls = set()
        url_refs = {}
        ctx = await browser.new_context(user_agent=UA)
        pg = await ctx.new_page()
        def record_request(req):
            if _is_stream_url(req.url):
                urls.add(req.url)
                try:
                    frame_url = req.frame.url
                    if frame_url and frame_url != "about:blank":
                        url_refs.setdefault(req.url, frame_url)
                except Exception:
                    pass
        pg.on("request", record_request)
        pg.on("response", lambda res: asyncio.create_task(_record_async_response(res, urls, url_refs)))
        try:
            await pg.goto(link, wait_until="domcontentloaded", timeout=30000)
            # Give player scripts time to request primary and backup qualities/servers.
            waited = 0
            while waited < wait_ms:
                await pg.wait_for_timeout(500)
                waited += 500
        except Exception as e:
            print(f"[!] Lỗi tải trận {link}: {e}", file=sys.stderr)
        try:
            meta = await pg.evaluate(PAGE_META_JS)
        except Exception:
            meta = {}
        await ctx.close()

    # Some sources publish player URLs in inline JS/HTML but do not request them
    # until a user presses Play. Read those candidates as well as browser traffic.
    static_candidates = extract_stream_candidates(
        link, meta.get("scripts", ""), meta.get("body", ""),
        meta.get("description", ""), meta.get("title", "")
    )
    for candidate in static_candidates:
        urls.add(candidate)
        url_refs.setdefault(candidate, link)
    for media_url in meta.get("media", []) or []:
        if _is_stream_url(media_url):
            urls.add(media_url)
            url_refs.setdefault(media_url, link)

    h1, title = meta.get("h1", ""), meta.get("title", "")
    page_title = h1 if is_match_title(h1) else title
    if not is_match_title(page_title) and is_match_title(card_text):
        page_title = card_text
    match = clean_match_title(page_title, link)
    card_match = extract_card_matchup(card_text + "\n" + meta.get("h1", "") + "\n" + meta.get("title", ""))
    if card_match and not is_match_title(match):
        match = card_match
    home_logo, away_logo = extract_team_logos(meta.get("teamImages", []), match)
    combined = "\n".join([card_text, meta.get("body", ""), meta.get("description", ""), meta.get("scripts", "")])
    # Keep the existing page/JSON/slug extractors as primary; card parsing is additive fallback.
    when, start = extract_time(link, combined)
    card_text_all = card_text + "\n" + meta.get("body", "") + "\n" + meta.get("h1", "") + "\n" + meta.get("title", "")
    card_when, card_start = extract_card_time(card_text_all)
    # Existing time extractor remains authoritative; use card parser only for empty fields.
    when = when or card_when
    start = start or card_start
    score = extract_score(card_text + "\n" + meta.get("body", ""))
    card_score = extract_card_score(card_text + "\n" + meta.get("body", ""))
    if not score:
        score = card_score
    commentator = extract_commentator(card_text + "\n" + meta.get("body", ""))
    # Prefer the match card; avoid treating generic site-wide "trực tiếp" labels as live status.
    status = extract_status(card_text)
    if status == "unknown":
        status = extract_status(meta.get("body", ""), include_generic_live=False)
    print(f"    {match}: {len(urls)} link HLS/FLV | trạng thái={status} | giờ={when or 'không rõ'}")
    return [{
        "url": u, "referer": url_refs.get(u, link), "match": match, "when": when, "start": start,
        "mid": match_id(link), "logo": home_logo, "home_logo": home_logo,
        "away_logo": away_logo, "score": score,
        "commentator": commentator, "status": status,
        "format": stream_format(u) or "HLS"
    } for u in sorted(urls)]


def extract_js(page_url):
    """Fallback: use a browser to collect HLS/FLV requests from a single page."""
    from playwright.sync_api import sync_playwright
    urls = set()
    title = urlparse(page_url).netloc
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(user_agent=UA)
        page.on("request", lambda req: urls.add(req.url) if _is_stream_url(req.url) else None)
        page.on("response", lambda res: urls.add(res.url) if _is_stream_response(res) else None)
        try:
            page.goto(page_url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(7000)
            title = page.title() or title
        except Exception as exc:
            print(f"[!] Lỗi {page_url}: {exc}", file=sys.stderr)
        browser.close()
    return [{"url": u, "match": title, "referer": page_url} for u in sorted(urls)]


def clean_match_title(value, link=""):
    """Remove SEO/site suffixes while retaining the two team names."""
    value = html.unescape(re.sub(r"<[^>]+>", " ", value or ""))
    value = re.sub(r"\s+", " ", value).strip(" |–—-:")
    value = re.split(r"\s*[|–—]\s*", value, maxsplit=1)[0]
    value = re.sub(r"\s+(?:trực tiếp|link xem|xem trực tuyến|bóng đá hôm nay).*?$", "", value, flags=re.I)
    if not is_match_title(value) and link:
        value = slug_to_name(link)
    return value.strip(" |–—-:")[:140].strip()


def is_match_title(value):
    return bool(value and len(value) < 180 and re.search(r"\bvs\b|\bv\b|\s-\s", value, re.I))


def prioritize_discovered_links(links, card_map, now=None, lookback_hours=3):
    """Drop cards whose kickoff is older than the rolling lookback window and scan live/near-future matches first.

    Cards without a parseable date are retained to avoid missing sources whose
    homepage does not expose kickoff metadata.
    """
    now = now or datetime.now(VN)
    cutoff = now - timedelta(hours=max(0, lookback_hours))
    ranked = []
    for index, link in enumerate(links):
        card = card_map.get(link, "") or ""
        status = extract_status(card)
        _, start = extract_card_time(card)
        kickoff = None
        if start:
            try:
                kickoff = datetime.fromisoformat(start).astimezone(VN)
            except ValueError:
                kickoff = None
        if kickoff and kickoff < cutoff and status != "live":
            continue
        if status == "live":
            rank = (0, kickoff.timestamp() if kickoff else 0, index)
        elif kickoff and kickoff >= now:
            rank = (1, kickoff.timestamp(), index)
        elif kickoff:
            rank = (2, -kickoff.timestamp(), index)
        else:
            rank = (3, index, index)
        ranked.append((rank, link))
    ranked.sort(key=lambda pair: pair[0])
    return [link for _, link in ranked]


async def _follow_async(home_url, pattern, max_pages, workers, budget=90, lookback_hours=3):
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            page = await browser.new_page(user_agent=UA)
            title, hrefs, card_map = "", [], {}
            try:
                await page.goto(home_url, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(3500)
                for _ in range(8):
                    await page.mouse.wheel(0, 2800)
                    await page.wait_for_timeout(650)
                data = await page.evaluate(PAGE_META_JS)
                title = data.get("title", "")
                card_map = data.get("cards", {})
                hrefs = await page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")
            except Exception as e:
                print(f"[!] Lỗi trang chủ {home_url}: {e}", file=sys.stderr)
            await page.close()

            base_host = (urlparse(home_url).hostname or "").lower()
            pattern = pattern.strip()
            links = []
            for href in hrefs:
                parsed = urlparse(href)
                host = (parsed.hostname or "").lower()
                if (parsed.scheme in ("http", "https") and host == base_host
                        and pattern in parsed.path and href != home_url):
                    links.append(href)
            links = list(dict.fromkeys(links))
            discovered_count = len(links)
            links = prioritize_discovered_links(links, card_map, lookback_hours=lookback_hours)
            print(f"    Trang chủ: {len(hrefs)} thẻ <a> | {discovered_count} trang trận khớp; còn {len(links)} sau lọc trận cũ hơn {lookback_hours} giờ")
            if not links:
                print("    -> Không tìm thấy link trận; cần kiểm tra lại mẫu URL hoặc cơ chế tải JS.")
                return []
            # Keep scheduled, live and finished matches; do not filter by status/time.
            if max_pages > 0:
                links = links[:max_pages]
            sem = asyncio.Semaphore(workers)
            tasks = [asyncio.create_task(_scan_match(browser, l, sem, card_map.get(l, ""))) for l in links]
            done, pending = await asyncio.wait(tasks, timeout=budget)
            if pending:
                print(f"    [!] Hết {budget}s, còn {len(pending)} trang chưa quét", file=sys.stderr)
                for t in pending:
                    t.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
            results = [t.result() for t in done if not t.cancelled() and t.exception() is None]
        finally:
            await browser.close()
    return [it for result in results for it in result]


def extract_follow_js(home_url, pattern, max_pages=0, workers=16, budget=90, lookback_hours=3):
    return asyncio.run(_follow_async(home_url, pattern, max_pages, workers, budget, lookback_hours))


def check_stream(item):
    """Probe HLS as an M3U8 manifest and FLV as a byte stream; retry with page Referer."""
    def probe(ref):
        headers = {"User-Agent": UA, "Range": "bytes=0-1023"}
        if ref:
            headers["Referer"] = ref
        try:
            r = requests.get(item["url"], headers=headers, timeout=12, stream=True)
            status = r.status_code
            content_type = (r.headers.get("Content-Type") or "").lower()
            head = next(r.iter_content(256), b"")
            r.close()
            if status not in (200, 206):
                return False
            fmt = stream_format(item["url"], content_type) or item.get("format", "HLS")
            if fmt == "FLV":
                return head.startswith(b"FLV") or "video" in content_type or "octet-stream" in content_type or status == 206
            if fmt == "DASH":
                return b"<MPD" in head or "dash+xml" in content_type
            return head.lstrip().startswith(b"#EXTM3U") or "mpegurl" in content_type
        except Exception:
            return False
    if probe(None):
        return True, ""
    if item.get("referer") and probe(item["referer"]):
        return True, item["referer"]
    return False, ""


def write_staging(items, path):
    streams = [{
        "url": i["url"], "verified": bool(i.get("verified", False)),
        "is_hls": i.get("format") == "HLS", "format": i.get("format", ""),
        "site": i["source_name"], "site_id": i["source"],
        "match": i.get("match", ""), "league": "",
        "logo": i.get("logo", ""), "page": i.get("referer", ""),
        "match_time": i.get("start", ""), "time_label": i.get("when", ""),
        "score": i.get("score", ""), "commentator": i.get("commentator", ""),
        "status": i.get("status", "unknown"),
        "referer": i.get("needs_referer", ""), "source_logo": i.get("source_logo", "")
    } for i in items]
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"streams": streams}, f, ensure_ascii=False, indent=2)


def read_sources(path):
    """Read every non-comment source line; adding a line needs no Python change.

    Syntax: Display name | homepage >> match-path-fragment ;; id=stable_id ;; logo=https://...
    Also accepts a bare homepage URL for a source that exposes streams on its homepage.
    """
    out = []
    with open(path, encoding="utf-8-sig") as f:
        for line_no, raw in enumerate(f, 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = [part.strip() for part in line.split(";;")]
            spec = parts[0]
            opts = {}
            for part in parts[1:]:
                if "=" in part:
                    key, value = part.split("=", 1)
                    opts[key.strip().lower()] = value.strip()
            group, sep, target = spec.partition("|")
            if sep:
                source_name = group.strip()
                target = target.strip()
            else:
                source_name = ""
                target = spec
            if not target:
                print(f"[!] Bỏ qua dòng {line_no}: thiếu URL", file=sys.stderr)
                continue
            if ">>" in target:
                home, pattern = [x.strip() for x in target.split(">>", 1)]
            else:
                home, pattern = target.strip(), ""
            if not home.startswith(("http://", "https://")):
                print(f"[!] Bỏ qua dòng {line_no}: URL không hợp lệ", file=sys.stderr)
                continue
            out.append({"name": source_name, "home": home, "pattern": pattern,
                        "opts": opts, "line": line_no})
    return out


def attr(key, value):
    return f' {key}="{str(value).replace(chr(34), chr(39))}"' if value not in (None, "") else ""


def write_m3u(items, path):
    with open(path, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for i in items:
            f.write("#EXTINF:-1"
                    + attr("tvg-id", i["tvg_id"])
                    + attr("tvg-name", i["tvg_name"])
                    + attr("tvg-logo", i.get("home_logo") or i.get("logo"))
                    + attr("x-home-logo", i.get("home_logo") or i.get("logo"))
                    + attr("x-away-logo", i.get("away_logo"))
                    + attr("group-title", i["group"])
                    + attr("start", i.get("start"))
                    + attr("x-source", i["source"])
                    + attr("x-source-name", i["source_name"])
                    + attr("x-source-logo", i.get("source_logo"))
                    + attr("x-score", i.get("score"))
                    + attr("x-commentator", i.get("commentator"))
                    + f',{i["name"]}\n')
            if i.get("referer"):
                f.write(f'#EXTVLCOPT:http-referrer={i["referer"]}\n')
            f.write(f'{i["url"]}\n')


def finalize(items):
    """Create stable per-match/per-stream identifiers and source-only groups."""
    import hashlib
    for n, it in enumerate(items, 1):
        match = clean_match_title(it.get("match", ""), it.get("referer", ""))
        it["match"] = match or it.get("source_name") or "Trận đấu"
        fmt = it.get("format") or stream_format(it["url"]) or "HLS"
        it["format"] = fmt
        parts = [it["match"], it.get("when", ""), it.get("score", ""),
                 it.get("commentator", ""), fmt]
        it["name"] = " | ".join(x for x in parts if x)
        it["tvg_name"] = it["match"]
        # Same match ID stays stable across runs; each distinct stream gets a stable suffix.
        mid = it.get("mid") or hashlib.sha1(it["match"].casefold().encode("utf-8")).hexdigest()[:12]
        stream_key = hashlib.sha1(it["url"].encode("utf-8")).hexdigest()[:8]
        it["tvg_id"] = f'{it["source"]}-{mid}-{stream_key}'
        it["group"] = it.get("source_name") or it.get("source") or "Unknown source"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sources", default="sources.txt")
    ap.add_argument("--out", default="playlist.m3u")
    ap.add_argument("--js", action="store_true")
    ap.add_argument("--no-check", action="store_true")
    ap.add_argument("--staging", default="")
    ap.add_argument("--no-playlist", action="store_true")
    ap.add_argument("--max-pages", type=int, default=0, help="maximum match pages per source; 0 = unlimited")
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--scan-budget", type=int, default=90, help="maximum seconds to scan match pages per source")
    ap.add_argument("--scan-lookback-hours", type=float, default=3, help="skip dated non-live cards older than this many hours; live and unknown-time cards are retained")
    args = ap.parse_args()

    items = []
    sources = read_sources(args.sources)
    print(f"[=] Đã nạp {len(sources)} nguồn từ {args.sources}")
    for source in sources:
        home, pattern, opts = source["home"], source["pattern"], source["opts"]
        host = urlparse(home).netloc.removeprefix("www.")
        display_name = source["name"] or opts.get("name") or host
        stem = opts.get("id") or re.sub(r"[^a-z0-9]+", "-", host.lower()).strip("-")
        print(f"[+] Quét nguồn {display_name}: {home}")
        source_logo = opts.get("logo", "").strip() or discover_source_logo(home)
        if source_logo:
            print(f"    [i] TVG logo: {source_logo}")
        else:
            print("    [i] Không tìm thấy URL logo thật; tvg-logo sẽ để trống")
        try:
            if pattern:
                found = extract_follow_js(home, pattern, args.max_pages, args.workers,
                                          args.scan_budget, args.scan_lookback_hours)
            else:
                found = extract_js(home) if args.js else extract_static(home)
        except Exception as e:
            print(f"[!] Nguồn {home} lỗi: {e}", file=sys.stderr)
            found = []
        print(f"    => {len(found)} link HLS/FLV/DASH thô")
        for it in found:
            it.update(source=stem, source_name=display_name,
                      logo=it.get("home_logo") or it.get("logo") or "",
                      home_logo=it.get("home_logo") or it.get("logo") or "",
                      away_logo=it.get("away_logo") or "",
                      source_logo=source_logo, group=display_name)
        items.extend(found)

    # Keep all detected live matches, plus scheduled kick-offs from now through tomorrow.
    now = datetime.now(VN)
    before_window = len(items)
    items = [item for item in items if match_in_playlist_window(item, now)]
    print(f"[=] Lọc theo lịch (trực tiếp + sắp diễn ra hôm nay/ngày mai): {len(items)}/{before_window} link")

    unique = {}
    for item in items:
        # The same URL may legitimately be listed by two different source groups.
        unique.setdefault((item.get("source", ""), item["url"]), item)
    items = list(unique.values())
    print(f"[=] Tìm thấy {len(items)} link HLS/FLV trong phạm vi thời gian")

    if not args.no_check and items:
        with ThreadPoolExecutor(max_workers=16) as pool:
            checks = list(pool.map(check_stream, items))
        for item, (ok, ref) in zip(items, checks):
            item["verified"] = ok
            item["needs_referer"] = ref
        stats = {}
        for item in items:
            ok, total = stats.get(item["source"], (0, 0))
            stats[item["source"]] = (ok + bool(item["verified"]), total + 1)
        for source, (ok, total) in stats.items():
            print(f"    {source}: {ok}/{total} link xác minh được")
        # Keep only playable streams in the M3U; failed probes remain visible in logs.
        items = [item for item in items if item["verified"]]
        print(f"[=] Còn {len(items)} link đã xác minh")

    if not items:
        print("[!] Không lấy được link hợp lệ -> giữ nguyên playlist cũ", file=sys.stderr)
        return 1
    finalize(items)
    if not args.no_playlist:
        write_m3u(items, args.out)
        print(f"[✓] Đã ghi {args.out}")
    if args.staging:
        write_staging(items, args.staging)
        print(f"[✓] Đã ghi {args.staging} ({len(items)} link)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
