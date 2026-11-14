import unittest
from datetime import datetime
from crawler import (read_sources, clean_match_title, stream_format, finalize,
                     match_in_playlist_window, extract_status, parse_match_datetime,
                     extract_time, extract_card_matchup, extract_card_score, extract_card_time,
                     merge_match_metadata, prioritize_discovered_links,
                     extract_stream_candidates, discover_source_logo, extract_team_names,
                     extract_team_logos, write_m3u)


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

    def test_extract_stream_candidates_from_inline_player_config(self):
        urls = extract_stream_candidates(
            "https://live11.chuoichientv.me/live/1644692/guatemala-vs-canada",
            '<script>var source="https://cdn.example/live/index.m3u8?token=abc";</script>'
        )
        self.assertIn("https://cdn.example/live/index.m3u8?token=abc", urls)

    def test_extract_relative_stream_candidate_from_html(self):
        urls = extract_stream_candidates(
            "https://live11.chuoichientv.me/live/1644692/guatemala-vs-canada",
            '<source src="/hls/guatemala-canada/playlist.m3u8">'
        )
        self.assertIn(
            "https://live11.chuoichientv.me/hls/guatemala-canada/playlist.m3u8", urls
        )

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


    def test_nembien_time_format_with_full_date(self):
        when, start = extract_time(
            "https://nembientv.com/truc-tiep/277eab011ff50b",
            "SV.League Women - Hisamitsu Springs W VS PFU Blue Cats W -11:05-14/11/2026"
        )
        self.assertEqual(when, "14/11 11:05")
        self.assertEqual(start, "2026-11-14T11:05:00+07:00")

    def test_nembien_parsed_match_enters_today_tomorrow_playlist(self):
        when, start = extract_time(
            "https://nembientv.com/truc-tiep/277eab011ff50b",
            "Hisamitsu Springs W VS PFU Blue Cats W -11:05-14/11/2026"
        )
        item = {"status": "scheduled", "when": when, "start": start}
        now = datetime.fromisoformat("2026-11-14T09:00:00+07:00")
        self.assertTrue(match_in_playlist_window(item, now))

    def test_card_matchup_parser_supports_league_prefix_and_v_s(self):
        raw = "SV.League Women - Hisamitsu Springs W VS PFU Blue Cats W -11:05-14/11/2026"
        self.assertEqual(
            extract_card_matchup(raw),
            "Hisamitsu Springs W vs PFU Blue Cats W"
        )

    def test_card_time_parser_supports_nembien_format(self):
        when, start = extract_card_time(
            "SV.League Women - Hisamitsu Springs W VS PFU Blue Cats W -11:05-14/11/2026"
        )
        self.assertEqual(when, "14/11 11:05")
        self.assertEqual(start, "2026-11-14T11:05:00+07:00")

    def test_card_score_parser_supports_set_and_full_time_labels(self):
        self.assertEqual(extract_card_score("SET 2 25-21"), "25-21")
        self.assertEqual(extract_card_score("FT 2-1"), "2-1")

    def test_supplemental_metadata_does_not_overwrite_existing_fields(self):
        current = {"match": "Existing A vs Existing B", "score": "1-0", "when": "14/11 10:00"}
        supplemental = {"match": "Card A vs Card B", "score": "2-0", "when": "14/11 11:05"}
        merged = merge_match_metadata(current, supplemental)
        self.assertEqual(merged["match"], "Existing A vs Existing B")
        self.assertEqual(merged["score"], "1-0")
        self.assertEqual(merged["when"], "14/11 10:00")

    def test_card_matchup_fills_weak_page_title(self):
        current = {"match": "Nembientv Truc Tiep"}
        supplemental = {"match": "Hisamitsu Springs W vs PFU Blue Cats W"}
        merged = merge_match_metadata(current, supplemental)
        self.assertEqual(merged["match"], supplemental["match"])

    def test_invalid_full_date_is_ignored(self):
        when, start = extract_time("https://nembientv.com/truc-tiep/test", "11:05-32/11/2026")
        self.assertEqual(when, "")
        self.assertEqual(start, "")

    def test_discovery_prioritizes_live_and_upcoming_and_skips_stale_cards(self):
        now = datetime.fromisoformat("2026-11-14T08:00:00+07:00")
        links = ["https://site.test/match/old", "https://site.test/match/tomorrow",
                 "https://site.test/match/live", "https://site.test/match/unknown"]
        cards = {
            links[0]: "Team A vs Team B - 20:00-11/11/2026",
            links[1]: "Team C vs Team D - 20:00-15/11/2026",
            links[2]: "LIVE Team E vs Team F",
        }
        ordered = prioritize_discovered_links(links, cards, now, lookback_hours=3)
        self.assertEqual(ordered, [links[2], links[1], links[3]])

    def test_discovery_retains_recent_past_cards_within_three_hours(self):
        now = datetime.fromisoformat("2026-11-14T08:00:00+07:00")
        links = ["https://site.test/match/recent", "https://site.test/match/stale"]
        cards = {
            links[0]: "Team A vs Team B - 06:00-14/11/2026",
            links[1]: "Team C vs Team D - 04:30-14/11/2026",
        }
        self.assertEqual(prioritize_discovered_links(links, cards, now, 3), [links[0]])

    def test_discovery_keeps_live_even_when_kickoff_is_older_than_three_hours(self):
        now = datetime.fromisoformat("2026-11-14T08:00:00+07:00")
        link = "https://site.test/match/live-old-kickoff"
        cards = {link: "LIVE Team A vs Team B - 01:00-14/11/2026"}
        self.assertEqual(prioritize_discovered_links([link], cards, now, 3), [link])

    def test_discovery_keeps_unknown_time_cards(self):
        now = datetime.fromisoformat("2026-11-14T08:00:00+07:00")
        link = "https://site.test/match/no-time"
        self.assertEqual(prioritize_discovered_links([link], {}, now, 3), [link])


    def test_discovers_real_logo_image_from_homepage(self):
        from unittest.mock import patch
        markup = '<img class="site-logo" src="/assets/brand-logo.svg" alt="Station logo">'
        with patch("crawler.fetch", return_value=markup):
            self.assertEqual(
                discover_source_logo("https://station.example/"),
                "https://station.example/assets/brand-logo.svg"
            )

    def test_team_name_parser_splits_home_and_away(self):
        self.assertEqual(extract_team_names("Guatemala vs Canada"), ("Guatemala", "Canada"))

    def test_team_logo_matcher_uses_team_images_not_station_branding(self):
        images = [
            {"src": "https://site.test/assets/guatemala.png", "alt": "Guatemala team logo"},
            {"src": "https://site.test/assets/canada.png", "alt": "Canada crest"},
            {"src": "https://site.test/assets/station-logo.png", "alt": "Chuoi Chien TV logo"},
        ]
        self.assertEqual(
            extract_team_logos(images, "Guatemala vs Canada"),
            ("https://site.test/assets/guatemala.png", "https://site.test/assets/canada.png")
        )

    def test_write_m3u_uses_team_logo_in_tvg_and_keeps_away_logo_separate(self):
        import tempfile
        from pathlib import Path
        row = {
            "url": "https://cdn.example/live.m3u8", "source": "station",
            "source_name": "Station TV", "match": "Team A vs Team B",
            "home_logo": "https://teams.example/team-a.png",
            "away_logo": "https://teams.example/team-b.png",
            "source_logo": "https://station.example/logo.png"
        }
        finalize([row])
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "playlist.m3u"
            write_m3u([row], path)
            content = path.read_text(encoding="utf-8")
        self.assertIn('tvg-logo="https://teams.example/team-a.png"', content)
        self.assertIn('x-away-logo="https://teams.example/team-b.png"', content)
        self.assertNotIn('tvg-logo="https://station.example/logo.png"', content)

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
