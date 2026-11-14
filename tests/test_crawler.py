import unittest
from datetime import datetime
from crawler import (read_sources, clean_match_title, stream_format, finalize,
                     match_in_playlist_window, extract_status, parse_match_datetime)


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

    def test_live_match_is_kept_even_without_kickoff(self):
        now = datetime.fromisoformat("2026-11-14T08:00:00+07:00")
        self.assertTrue(match_in_playlist_window({"status": "live"}, now))

    def test_today_and_tomorrow_scheduled_matches_are_kept(self):
        now = datetime.fromisoformat("2026-11-14T08:00:00+07:00")
        today = {"status": "scheduled", "start": "2026-11-14T20:00:00+07:00"}
        tomorrow = {"status": "scheduled", "start": "2026-11-15T21:00:00+07:00"}
        self.assertTrue(match_in_playlist_window(today, now))
        self.assertTrue(match_in_playlist_window(tomorrow, now))

    def test_finished_and_beyond_tomorrow_are_excluded(self):
        now = datetime.fromisoformat("2026-11-14T08:00:00+07:00")
        finished = {"status": "finished", "start": "2026-11-14T07:00:00+07:00"}
        later = {"status": "scheduled", "start": "2026-11-16T20:00:00+07:00"}
        self.assertFalse(match_in_playlist_window(finished, now))
        self.assertFalse(match_in_playlist_window(later, now))

    def test_status_detection(self):
        self.assertEqual(extract_status("ĐANG DIỄN RA - LIVE"), "live")
        self.assertEqual(extract_status("Sắp diễn ra 20:00"), "scheduled")
        self.assertEqual(extract_status("FT 2-1 Đã kết thúc"), "finished")

    def test_day_month_time_parses_to_current_year(self):
        now = datetime.fromisoformat("2026-11-14T08:00:00+07:00")
        parsed = parse_match_datetime({"when": "15/11 20:30"}, now)
        self.assertEqual(parsed.isoformat(), "2026-11-15T20:30:00+07:00")

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
