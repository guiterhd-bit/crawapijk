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
PAGE_META_JS = """() => {
  const q = s => document.querySelector(s) || {};
  const a = [...document.querySelectorAll('a[href]')];
  const cards = {};
  for (const el of a) {
    let p = el;
    for (let i=0; i<5 && p; i++, p=p.parentElement) {
      const t = (p.innerText || '').trim();
      if (t.length >= 12 && t.length <= 1800) { cards[el.href] = t; break; }
    }
  }
  return {
    img: q('meta[property="og:image"]').content || '',
    h1: (q('h1').innerText || '').trim(),
    title: (q('meta[property="og:title"]').content || document.title || '').trim(),
    description: q('meta[name="description"]').content || '',
    body: (document.body && document.body.innerText || '').slice(0, 30000),
    scripts: [...document.scripts].map(s => s.textContent || '').join('\\n').slice(0, 150000),
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


def normalize(text):
    text = html.unescape(text)
    return text.replace("\\/", "/").replace("\\u0026", "&").replace("\\u003A", ":")


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
        (r"\b(\d{2})[/.](\d{2})[/.](20\d{2})\s+(\d{2}):(\d{2})\b", "full"),
        (r"\b(\d{2})/(\d{2})\s+(\d{2}):(\d{2})\b", "day-first"),
        (r"\b(\d{2}):(\d{2})\s+(\d{2})/(\d{2})\b", "time-first"),
    )
    for pattern, kind in patterns:
        m = re.search(pattern, text)
        if not m:
            continue
        if kind == "full":
            d, mo, year, h, mi = m.groups()
            dt = datetime(int(year), int(mo), int(d), int(h), int(mi), tzinfo=VN)
            return dt.strftime("%d/%m %H:%M"), dt.isoformat()
        if kind == "day-first":
            d, mo, h, mi = m.groups()
        else:
            h, mi, d, mo = m.groups()
        return f"{d}/{mo} {h}:{mi}", ""

    return slug_to_time(link), slug_to_start(link)


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


async def _record_async_response(response, urls):
    if _is_stream_url(response.url):
        urls.add(response.url)
        return
    try:
        headers = await response.all_headers()
        content_type = (headers.get("content-type") or "").lower()
        if any(x in content_type for x in (
            "mpegurl", "x-flv", "video/flv", "application/flv", "dash+xml"
        )):
            urls.add(response.url)
    except Exception:
        pass


async def _scan_match(browser, link, sem, card_text="", wait_ms=6000):
    """Scan one match page for every HLS/FLV request and extract visible match metadata."""
    async with sem:
        urls = set()
        ctx = await browser.new_context(user_agent=UA)
        pg = await ctx.new_page()
        pg.on("request", lambda req: urls.add(req.url) if _is_stream_url(req.url) else None)
        pg.on("response", lambda res: asyncio.create_task(_record_async_response(res, urls)))
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

    h1, title = meta.get("h1", ""), meta.get("title", "")
    match = clean_match_title(h1 if is_match_title(h1) else title, link)
    combined = "\n".join([card_text, meta.get("body", ""), meta.get("description", ""), meta.get("scripts", "")])
    when, start = extract_time(link, combined)
    score = extract_score(card_text + "\n" + meta.get("body", ""))
    commentator = extract_commentator(card_text + "\n" + meta.get("body", ""))
    print(f"    {match}: {len(urls)} link HLS/FLV")
    return [{
        "url": u, "referer": link, "match": match, "when": when, "start": start,
        "mid": match_id(link), "logo": "", "score": score,
        "commentator": commentator, "format": stream_format(u) or "HLS"
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


async def _follow_async(home_url, pattern, max_pages, workers, budget=45):
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
            print(f"    Trang chủ: {len(hrefs)} thẻ <a> | {len(links)} trang trận khớp '{pattern}'")
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


def extract_follow_js(home_url, pattern, max_pages=0, workers=16):
    return asyncio.run(_follow_async(home_url, pattern, max_pages, workers))


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
                    + attr("tvg-logo", i.get("logo"))
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
        try:
            if pattern:
                found = extract_follow_js(home, pattern, args.max_pages, args.workers)
            else:
                found = extract_js(home) if args.js else extract_static(home)
        except Exception as e:
            print(f"[!] Nguồn {home} lỗi: {e}", file=sys.stderr)
            found = []
        print(f"    => {len(found)} link HLS/FLV/DASH thô")
        for it in found:
            it.update(source=stem, source_name=display_name,
                      source_logo=opts.get("logo", ""), group=display_name)
        items.extend(found)

    unique = {}
    for item in items:
        # The same URL may legitimately be listed by two different source groups.
        unique.setdefault((item.get("source", ""), item["url"]), item)
    items = list(unique.values())
    print(f"[=] Tìm thấy {len(items)} link HLS/FLV")

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
