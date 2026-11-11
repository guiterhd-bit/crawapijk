#!/usr/bin/env python3
"""Crawl link m3u8 từ nhiều trang web -> playlist.m3u

Dùng:
    pip install requests
    python crawler.py                    # đọc sources.txt, ghi playlist.m3u
    python crawler.py --js               # trang dùng JavaScript (cần playwright)
    python crawler.py --no-check         # bỏ qua bước kiểm tra link sống
"""
import argparse
import html
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlparse

import requests

UA = ("Mozilla/5.0 (Linux; Android 11) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36")

ABS_RE = re.compile(r'https?://[^\s"\'<>\\]+?\.m3u8(?:\?[^\s"\'<>\\]*)?', re.I)
REL_RE = re.compile(r'["\']([^"\'\s<>]+?\.m3u8(?:\?[^"\'\s<>]*)?)["\']', re.I)
IFRAME_RE = re.compile(r'<iframe[^>]+src=["\']([^"\']+)["\']', re.I)
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)


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

    items = [{"url": u, "title": title, "referer": page_url} for u in found]

    if depth > 0:
        for src in IFRAME_RE.findall(text):
            items += extract_static(urljoin(page_url, src), depth - 1, seen)
    return items


def sniff(page, urls, timeout=25000):
    """Chờ tới khi bắt được request m3u8 (hoặc hết giờ), không đợi 'networkidle'."""
    waited = 0
    while waited < timeout and not urls:
        page.wait_for_timeout(500)
        waited += 500
    if urls:
        page.wait_for_timeout(2000)  # chờ thêm các link phụ (chất lượng khác)


def extract_js(page_url):
    """Mở trang bằng trình duyệt thật, bắt request .m3u8 (trang render bằng JS)."""
    from playwright.sync_api import sync_playwright

    urls = set()
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(user_agent=UA)
        page.on("request", lambda req: urls.add(req.url) if ".m3u8" in req.url else None)
        try:
            page.goto(page_url, wait_until="domcontentloaded", timeout=30000)
            sniff(page, urls)
            title = page.title() or urlparse(page_url).netloc
        except Exception as e:
            print(f"[!] Lỗi {page_url}: {e}", file=sys.stderr)
            title = urlparse(page_url).netloc
        browser.close()
    return [{"url": u, "title": title, "referer": page_url} for u in urls]


def slug_to_name(link):
    """.../doi-a-vs-doi-b-luc-1630-ngay-11-11-2026-vmqy9iyjzzvhgk9 -> tên trận."""
    slug = urlparse(link).path.rstrip("/").split("/")[-1]
    slug = re.sub(r"-[a-z0-9]{12,}$", "", slug)
    return slug.replace("-", " ").title()


def extract_follow_js(home_url, pattern, max_pages=40):
    """Mở trang chủ, lấy mọi link chứa `pattern` (vd /truc-tiep/),
    vào từng trang trận và bắt request .m3u8."""
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
            print(f"    {slug_to_name(link)}: {len(urls)} link")
            pg.close()
            name = slug_to_name(link)
            items += [{"url": u, "title": name, "name": name, "referer": link}
                      for u in urls]
        browser.close()
    return items


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
    """Mỗi dòng: `url` hoặc `Tên nhóm | url`. Dòng bắt đầu bằng # là chú thích."""
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            group, _, url = line.rpartition("|")
            out.append((group.strip(), url.strip()))
    return out


def write_m3u(items, path):
    with open(path, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for i in items:
            f.write(f'#EXTINF:-1 group-title="{i["group"]}",{i["name"]}\n')
            f.write(f'#EXTVLCOPT:http-referrer={i["referer"]}\n')
            f.write(f'{i["url"]}\n')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sources", default="sources.txt")
    ap.add_argument("--out", default="playlist.m3u")
    ap.add_argument("--js", action="store_true", help="dùng Playwright cho trang JS")
    ap.add_argument("--no-check", action="store_true", help="không kiểm tra link sống")
    args = ap.parse_args()

    items = []
    for group, url in read_sources(args.sources):
        print(f"[+] Quét {url}")
        if ">>" in url:  # trang chủ >> mẫu link trận
            home, pattern = [s.strip() for s in url.split(">>", 1)]
            found = extract_follow_js(home, pattern)
        else:
            home = url
            found = extract_js(url) if args.js else extract_static(url)
        for it in found:
            it["group"] = group or urlparse(home).netloc
        items += found

    # bỏ trùng theo URL
    uniq = {}
    for it in items:
        uniq.setdefault(it["url"], it)
    items = list(uniq.values())
    print(f"[=] Tìm thấy {len(items)} link")

    if not args.no_check and items:
        with ThreadPoolExecutor(max_workers=10) as ex:
            alive = list(ex.map(is_alive, items))
        items = [it for it, ok in zip(items, alive) if ok]
        print(f"[=] Còn sống: {len(items)}")

    count = {}
    for it in items:
        base = it.get("name") or it["title"][:60]
        count[base] = count.get(base, 0) + 1
        it["name"] = base if count[base] == 1 else f"{base} #{count[base]}"

    write_m3u(items, args.out)
    print(f"[✓] Đã ghi {args.out}")


if __name__ == "__main__":
    main()
