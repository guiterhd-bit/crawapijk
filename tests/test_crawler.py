import unittest
from crawler import read_sources, clean_match_title, stream_format, finalize


class CrawlerConfigTests(unittest.TestCase):
    def test_all_eleven_sources_are_loaded(self):
        sources = read_sources("sources.txt")
        self.assertEqual(len(sources), 11)
        self.assertEqual(sources[0]["name"], "Cola TV")
        self.assertEqual(sources[-1]["name"], "Xôi Lạc TV")

    def test_future_source_line_needs_no_code_change(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "sources.txt"
            path.write_text("Nguồn mới | https://example.org/ >> /match/ ;; id=news\n", encoding="utf-8")
            item = read_sources(path)[0]
            self.assertEqual(item["name"], "Nguồn mới")
            self.assertEqual(item["home"], "https://example.org/")
            self.assertEqual(item["pattern"], "/match/")
            self.assertEqual(item["opts"]["id"], "news")

    def test_title_cleanup(self):
        title = clean_match_title("Đức U21 vs Bỉ U21 - Trực Tiếp Bóng đá | Website", "")
        self.assertEqual(title, "Đức U21 vs Bỉ U21")

    def test_stream_formats(self):
        self.assertEqual(stream_format("https://cdn.test/a.m3u8"), "HLS")
        self.assertEqual(stream_format("https://cdn.test/a.flv"), "FLV")
        self.assertEqual(stream_format("https://cdn.test/a.mpd"), "DASH")
        self.assertEqual(stream_format("https://cdn.test/live", "application/dash+xml"), "DASH")

    def test_stream_ids_are_stable_and_unique_per_url(self):
        rows = [
            {"url": "https://cdn.test/a.m3u8", "source": "example", "source_name": "Example TV", "match": "A vs B", "mid": "match1"},
            {"url": "https://cdn.test/b.m3u8", "source": "example", "source_name": "Example TV", "match": "A vs B", "mid": "match1"},
        ]
        finalize(rows)
        ids = [row["tvg_id"] for row in rows]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(row["group"] == "Example TV" for row in rows))
        before = ids[:]
        finalize(rows)
        self.assertEqual(before, [row["tvg_id"] for row in rows])


if __name__ == "__main__":
    unittest.main()
