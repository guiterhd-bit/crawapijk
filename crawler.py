#!/usr/bin/env python3
"""Crawl link m3u8 từ nhiều trang web -> playlist.m3u

Dùng:
    pip install requests playwright && playwright install chromium
    python crawler.py                # đọc sources.txt, ghi playlist.m3u
    python crawler.py --js           # ép dùng trình duyệt cho dòng không có '>>'
    python crawler.py --no-check     # bỏ qua bước kiểm tra link sống
"""
import argparse
import html
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urljoin, urlparse

import requests

UA = ("Mozilla/5.0 (Linux; Android 11) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36")

ABS_RE = re.compile(r'https?://[^\s"\'<>\\]+?\.m3u8(?:\?[^\s"\'<>\\]*)?', re.I)
REL_RE = re.compile(r'["\']([^"\'\s<>]+?\.m3u8(?:\?[^"\'\s<>]*)?)["\']', re.I)
IFRAME_RE = re.compile(r'<iframe[^>]+src=["\']([^"\']+)["\']', re.I)
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)

# Lấy ảnh og:image và h1 của trang trận (chỉnh selector ở đây nếu cần)
PAGE_META_JS = """() => {
  const q = s => document.querySelector(s) || {};
  return {
    img: q('meta[property="og:image"]').content || '',
    h1: (q('h1').innerText || '').trim()
  };
}"""


# ---------------------------------------------------------------- tải trang tĩnh
def fetch(url, referer=None):
    headers = {"User-Agent": UA}
    if referer:
        headers["Referer"] = referer
    r = requests.get(url, headers=headers, timeout=15)
    r.raise_for_status()
    return r.text


def normalize(text):
    """Giải mã &amp; và \\/ để regex bắt được link trong JSON/JS."""
    text = html.unescape(text)
    return text.replace("\\/", "/").replace("\\u0026", "&")


def extract_static(page_url, depth=1, seen=None):
    """Lấy m3u8 trong HTML + các iframe (tối đa `depth` cấp)."""
    seen = seen if seen is not None else set()
    if page_url in seen:
        return []
    seen.add(page_url)
    try:
        raw = fetch(page_url)
    except Exception as e:
        print(f"[!] Lỗi tải {page_url}: {e}", file=sys.stderr)
        return []

    text = normalize(raw)
    m = TITLE_RE.search(text)
    title = re.sub(r"\s+", " ", m.group(1)).strip() if m else urlparse(page_url).netloc

    found = set(ABS_RE.findall(text))
    for rel in REL_RE.findall(text):
        found.add(urljoin(page_url, rel))

    items = [{"url": u, "match": title, "referer": page_url} for u in sorted(found)]

    if depth > 0:
        for src in IFRAME_RE.findall(text):
            items += extract_static(urljoin(page_url, src), depth - 1, seen)
    return items


# ---------------------------------------------------------------- thông tin từ link trận
def _slug(link):
    return urlparse(link).path.rstrip("/").split("/")[-1]


def slug_to_name(link):
    """.../doi-a-vs-doi-b-luc-1630-ngay-11-11-2026-xxxx -> 'Doi A Vs Doi B'."""
    slug = re.sub(r"-[a-z0-9]{12,}$", "", _slug(link))
    slug = re.sub(r"-luc-\d{4}-ngay-.*$", "", slug)
    return slug.replace("-", " ").title()


def slug_to_time(link):
    """.../luc-1630-ngay-11-11-2026-... -> '11/11 16:30'."""
    m = re.search(r"luc-(\d{2})(\d{2})-ngay-(\d{2})-(\d{2})-\d{4}", link)
    return f"{m.group(3)}/{m.group(4)} {m.group(1)}:{m.group(2)}" if m else ""


def match_id(link):
    q = parse_qs(urlparse(link).query).get("houseId")
    if q:
        return q[0]
    m = re.search(r"-([a-z0-9]{12,})$", _slug(link))
    return m.group(1) if m else ""


# ---------------------------------------------------------------- trình duyệt (Playwright)
def sniff(page, urls, timeout=25000):
    """Chờ tới khi bắt được request m3u8 (hoặc hết giờ), không đợi 'networkidle'."""
    waited = 0
    while waited < timeout and not urls:
        page.wait_for_timeout(500)
        waited += 500
    if urls:
        page.wait_for_timeout(2000)  # chờ thêm các link phụ (chất lượng khác)


def extract_js(page_url):
    """Mở 1 trang bằng trình duyệt thật, bắt request .m3u8."""
    from playwright.sync_api import sync_playwright

    urls = set()
    title = urlparse(page_url).netloc
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(user_agent=UA)
        page.on("request", lambda req: urls.add(req.url) if ".m3u8" in req.url else None)
        try:
            page.goto(page_url, wait_until="domcontentloaded", timeout=30000)
            sniff(page, urls)
            title = page.title() or title
        except Exception as e:
            print(f"[!] Lỗi {page_url}: {e}", file=sys.stderr)
        browser.close()
    return [{"url": u, "match": title, "referer": page_url} for u in sorted(urls)]


def extract_follow_js(home_url, pattern, max_pages=40):
    """Mở trang chủ, lấy mọi link chứa `pattern` (vd /truc-tiep/),
    vào từng trang trận và bắt request .m3u8 + ảnh/tiêu đề trận."""
    from playwright.sync_api import sync_playwright

    items = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(user_agent=UA)
        try:
            page.goto(home_url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(4000)
            for _ in range(5):  # cuộn để trang nạp thêm trận
                page.mouse.wheel(0, 3000)
                page.wait_for_timeout(800)
            hrefs = page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")
        except Exception as e:
            print(f"[!] Lỗi trang chủ {home_url}: {e}", file=sys.stderr)
            hrefs = []
        page.close()

        links = list(dict.fromkeys(h for h in hrefs if pattern in h))[:max_pages]
        print(f"    Tìm thấy {len(links)} trang trận")

        for link in links:
            urls = set()
            pg = browser.new_page(user_agent=UA)
            pg.on("request", lambda r, s=urls: s.add(r.url) if ".m3u8" in r.url else None)
            try:
                pg.goto(link, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                print(f"[!] Lỗi tải {link}: {e}", file=sys.stderr)
            sniff(pg, urls)
            try:
                meta = pg.evaluate(PAGE_META_JS)
            except Exception:
                meta = {}
            pg.close()

            h1 = meta.get("h1", "")
            match = h1 if re.search(r"\bvs\b", h1, re.I) and len(h1) < 120 else slug_to_name(link)
            print(f"    {match}: {len(urls)} link")
            for u in sorted(urls):
                items.append({"url": u, "referer": link, "match": match,
                              "when": slug_to_time(link), "mid": match_id(link),
                              "logo": meta.get("img", "")})
        browser.close()
    return items


# ---------------------------------------------------------------- kiểm tra + ghi file
def is_alive(item):
    try:
        r = requests.get(item["url"], headers={"User-Agent": UA, "Referer": item["referer"]},
                         timeout=10, stream=True)
        head = next(r.iter_content(64), b"")
        r.close()
        return r.status_code == 200 and head.lstrip().startswith(b"#EXTM3U")
    except Exception:
        return False


def read_sources(path):
    """Mỗi dòng: [Nhóm |] url [>> mẫu-link-trận] [;; name=... ;; logo=... ;; id=...]"""
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            spec, *rest = [s.strip() for s in line.split(";;")]
            opts = dict(r.split("=", 1) for r in rest if "=" in r)
            opts = {k.strip(): v.strip() for k, v in opts.items()}
            group, _, url = spec.rpartition("|")
            out.append((group.strip(), url.strip(), opts))
    return out


def attr(key, value):
    return f' {key}="{str(value).replace(chr(34), chr(39))}"' if value else ""


def write_m3u(items, path):
    with open(path, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for i in items:
            f.write("#EXTINF:-1"
                    + attr("tvg-id", i["tvg_id"])
                    + attr("tvg-name", i["tvg_name"])
                    + attr("tvg-logo", i.get("logo"))
                    + attr("group-title", i["group"])
                    + attr("x-source", i["source"])
                    + attr("x-source-name", i["source_name"])
                    + attr("x-source-logo", i.get("source_logo"))
                    + f',{i["name"]}\n')
            f.write(f'#EXTVLCOPT:http-referrer={i["referer"]}\n')
            f.write(f'{i["url"]}\n')


def finalize(items):
    """Đặt tên hiển thị, tvg-id, tvg-name cho từng link."""
    count = {}
    for n, it in enumerate(items, 1):
        match = (it.get("match") or "")[:80]
        k = count[match] = count.get(match, 0) + 1
        label = f"Link {k}"
        it["name"] = " | ".join(x for x in (match, it.get("when"), label) if x)
        it["tvg_name"] = f"{match} - {label}"
        base = f'{it["source"]}-match-{it["mid"]}' if it.get("mid") else f'{it["source"]}-{n}'
        it["tvg_id"] = base if k == 1 else f"{base}-{k}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sources", default="sources.txt")
    ap.add_argument("--out", default="playlist.m3u")
    ap.add_argument("--js", action="store_true", help="dùng Playwright cho dòng không có '>>'")
    ap.add_argument("--no-check", action="store_true", help="không kiểm tra link sống")
    args = ap.parse_args()

    items = []
    for group, url, opts in read_sources(args.sources):
        print(f"[+] Quét {url}")
        if ">>" in url:
            home, pattern = [s.strip() for s in url.split(">>", 1)]
            found = extract_follow_js(home, pattern)
        else:
            home = url
            found = extract_js(url) if args.js else extract_static(url)
        host = urlparse(home).netloc
        stem = opts.get("id") or host.removeprefix("www.").split(".")[0]
        for it in found:
            it.update(group=group or host, source=stem,
                      source_name=opts.get("name", host),
                      source_logo=opts.get("logo", ""))
        items += found

    uniq = {}  # bỏ trùng theo URL
    for it in items:
        uniq.setdefault(it["url"], it)
    items = list(uniq.values())
    print(f"[=] Tìm thấy {len(items)} link")

    if not args.no_check and items:
        with ThreadPoolExecutor(max_workers=10) as ex:
            alive = list(ex.map(is_alive, items))
        items = [it for it, ok in zip(items, alive) if ok]
        print(f"[=] Còn sống: {len(items)}")

    finalize(items)
    write_m3u(items, args.out)
    print(f"[✓] Đã ghi {args.out}")


if __name__ == "__main__":
    main()
