from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import jdatetime

from enamad import (
    OUTPUT_KEYS,
    ResultSink,
    _error_row,
    collect_domains,
    discover_homepage_search,
    extract_trustseal_url,
    json_payload,
    load_done_domains,
    normalize_domain,
    parse_jalali_date,
    parse_profile_page,
    parse_search_api_payload,
    parse_search_page,
    parse_sweetalert_html,
    render_csv,
    to_ascii_digits,
)

FIXTURES = Path(__file__).parent / "fixtures"


class NormalizeDomainTests(unittest.TestCase):
    def test_strips_scheme_www_and_path(self) -> None:
        self.assertEqual(normalize_domain("https://www.Example.ir/path"), "example.ir")

    def test_accepts_bare_host(self) -> None:
        self.assertEqual(normalize_domain("shop.ir"), "shop.ir")


class SearchPageTests(unittest.TestCase):
    def test_finds_matching_domain(self) -> None:
        html = (FIXTURES / "search_hit.html").read_text(encoding="utf-8")
        row = parse_search_page(html, "example.ir")
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row["domain"], "example.ir")
        self.assertTrue(row["profile_url"].endswith("/Profile/12345"))
        self.assertEqual(row["expire_date"], "1405/01/17")

    def test_returns_none_when_missing(self) -> None:
        html = (FIXTURES / "search_miss.html").read_text(encoding="utf-8")
        self.assertIsNone(parse_search_page(html, "missing-domain.ir"))

    def test_finds_table_row_even_with_empty_placeholder(self) -> None:
        html = (FIXTURES / "search_table.html").read_text(encoding="utf-8")
        row = parse_search_page(html, "example.ir")
        self.assertIsNotNone(row)
        assert row is not None
        self.assertIn("trustseal.enamad.ir", row["profile_url"])
        self.assertEqual(row["expire_date"], "1405/01/17")


class TrustsealBadgeTests(unittest.TestCase):
    def test_extracts_code_even_when_id_is_empty(self) -> None:
        html = (FIXTURES / "site_badge.html").read_text(encoding="utf-8")
        url = extract_trustseal_url(html)
        self.assertIsNotNone(url)
        assert url is not None
        self.assertIn("trustseal.enamad.ir", url)
        self.assertIn("SAMPLECODE123", url)


class ProfilePageTests(unittest.TestCase):
    def test_extracts_php_compatible_fields(self) -> None:
        html = (FIXTURES / "profile.html").read_text(encoding="utf-8")
        data = parse_profile_page(html)
        self.assertEqual(data["name"], "علی رضایی")
        self.assertEqual(data["start_date"], "1403/01/18")
        self.assertEqual(data["expire_date"], "1405/01/17")
        self.assertEqual(data["address"], "تهران، خیابان مثال")
        self.assertEqual(data["phone"], "02112345678")
        self.assertEqual(data["email"], "info@example.ir")
        self.assertEqual(data["work_time"], "۹ تا ۱۷")
        self.assertEqual(data["history"], "بیش از ۵ سال")
        self.assertEqual(data["star"], 2)


class CurrentLayoutTests(unittest.TestCase):
    def test_parses_current_enamad_profile_layout(self) -> None:
        html = (FIXTURES / "profile_layout.html").read_text(encoding="utf-8")
        data = parse_profile_page(html)
        self.assertIn("علی محمدی", data["name"])
        self.assertEqual(data["title"], "فروشگاه نمونه")
        self.assertEqual(data["start_date"], "1403/09/29")
        self.assertEqual(data["expire_date"], "1405/09/28")
        self.assertIn("تهران", data["address"])
        self.assertEqual(data["phone"], "02112345678")
        self.assertEqual(data["email"], "info@example.ir")
        self.assertEqual(data["work_time"], "09:00 الی 20:00")
        self.assertIn("سال", data["history"])
        self.assertEqual(data["star"], 1)


class SweetAlertTests(unittest.TestCase):
    def test_parses_homepage_alert(self) -> None:
        html = (FIXTURES / "sweetalert.html").read_text(encoding="utf-8")
        alert = parse_sweetalert_html(html)
        self.assertIsNotNone(alert)
        assert alert is not None
        self.assertEqual(alert["name"], "فروشگاه نمونه")
        self.assertEqual(alert["start_date"], "1403/09/29")
        self.assertEqual(alert["expire_date"], "1405/09/28")
        self.assertEqual(alert["star"], 1)
        self.assertEqual(alert["status"], "معتبر")

    def test_parses_json_payload_with_url(self) -> None:
        payload = {
            "success": True,
            "Name": "فروشگاه نمونه",
            "Url": "https://trustseal.enamad.ir/?id=12345&code=abc",
            "html": (FIXTURES / "sweetalert.html").read_text(encoding="utf-8"),
        }
        parsed = parse_search_api_payload(payload)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed["profile_url"], "https://trustseal.enamad.ir/?id=12345&code=abc")
        self.assertEqual(parsed["expire_date"], "1405/09/28")

    def test_parses_getdata_object(self) -> None:
        payload = {
            "id": 12345,
            "code": "abc123",
            "persian_name": "فروشگاه نمونه",
            "approvedate": "1403/09/29",
            "expdate": "1405/09/28",
            "rating": 1,
            "statename": "تهران",
            "cityname": "تهران",
            "enamad_status": 1,
            "domain_address": None,
        }
        parsed = parse_search_api_payload(payload, "example.ir")
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed["id"], 12345)
        self.assertEqual(parsed["title"], "فروشگاه نمونه")
        self.assertEqual(parsed["name"], "")
        self.assertEqual(parsed["start_date"], "1403/09/29")
        self.assertEqual(parsed["expire_date"], "1405/09/28")
        self.assertEqual(parsed["star"], 1)
        self.assertIn("تهران", parsed["address"])
        self.assertIn("id=12345", parsed["profile_url"])

    def test_parses_getdata_table_row(self) -> None:
        payload = {
            "data": [
                [
                    "1",
                    "<a href='https://trustseal.enamad.ir/?id=12345&Code=abc'>example.ir</a>",
                    "فروشگاه نمونه",
                    "1403/09/29",
                    "1405/09/28",
                ]
            ]
        }
        parsed = parse_search_api_payload(payload, "example.ir")
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertIn("12345", parsed["profile_url"])
        self.assertEqual(parsed["expire_date"], "1405/09/28")


class HomepageDiscoveryTests(unittest.TestCase):
    def test_reads_btn_search_domain_ajax(self) -> None:
        html = (FIXTURES / "homepage.js.html").read_text(encoding="utf-8")
        found = discover_homepage_search(html)
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual(found["url"], "/Home/CheckEnamad")
        self.assertEqual(found["method"], "POST")
        self.assertEqual(found["param"], "domain")


class JalaliTests(unittest.TestCase):
    def test_persian_digits(self) -> None:
        self.assertEqual(to_ascii_digits("۱۴۰۵/۰۱/۱۷"), "1405/01/17")

    def test_expired_date_is_past(self) -> None:
        expire = parse_jalali_date("تاریخ انقضا: ۱۴۰۳/۰۱/۰۱")
        self.assertIsNotNone(expire)
        assert expire is not None
        self.assertTrue(expire < jdatetime.date.today())


class DomainListTests(unittest.TestCase):
    def test_splits_commas_and_deduplicates(self) -> None:
        domains = collect_domains(["example.ir, shop.ir", "example.ir"])
        self.assertEqual(domains, ["example.ir", "shop.ir"])

    def test_reads_file(self) -> None:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", suffix=".txt", delete=False
        ) as handle:
            handle.write("# comment\nexample.ir\nhttps://www.shop.ir/path\n")
            path = handle.name
        try:
            domains = collect_domains([], path)
        finally:
            Path(path).unlink(missing_ok=True)
        self.assertEqual(domains, ["example.ir", "shop.ir"])


class CsvTests(unittest.TestCase):
    def test_writes_header_and_nulls(self) -> None:
        text = render_csv(
            [{"domain": "example.ir", "id": 12345, "title": "فروشگاه نمونه", "star": 1}],
            OUTPUT_KEYS,
        )
        lines = text.strip().split("\n")
        self.assertEqual(lines[0], ",".join(OUTPUT_KEYS))
        self.assertTrue(lines[1].startswith("example.ir,"))


class ResultSinkTests(unittest.TestCase):
    def test_csv_file_updates_after_each_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.csv"
            sink = ResultSink(str(path), use_csv=True, check=False, expired=False, total=2)
            sink.add({"domain": "example.ir", "id": 1, "title": "الف"})
            first = path.read_text(encoding="utf-8-sig")
            self.assertIn("example.ir", first)
            self.assertNotIn("shop.ir", first)
            sink.add({"domain": "shop.ir", "id": 2, "title": "ب"})
            second = path.read_text(encoding="utf-8-sig")
            self.assertIn("shop.ir", second)
            sink.close()

    def test_json_file_stays_valid_after_each_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.json"
            sink = ResultSink(str(path), use_csv=False, check=False, expired=False, total=2)
            sink.add({"domain": "example.ir", "id": 1, "title": "الف"})
            first = json.loads(path.read_text(encoding="utf-8"))
            self.assertIsInstance(first, list)
            self.assertEqual(len(first), 1)
            self.assertEqual(first[0]["domain"], "example.ir")
            sink.add({"domain": "shop.ir", "id": 2, "title": "ب"})
            second = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(len(second), 2)
            self.assertEqual(second[1]["domain"], "shop.ir")
            sink.close()

    def test_json_payload_single_object(self) -> None:
        payload = json_payload(
            [{"domain": "example.ir", "id": 1}],
            check=False,
            expired=False,
            total=1,
        )
        self.assertIsInstance(payload, dict)
        self.assertEqual(payload["domain"], "example.ir")


class ResumeAndStatusTests(unittest.TestCase):
    def test_load_done_domains_from_csv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.csv"
            path.write_text("domain,status\nexample.ir,ok\nshop.ir,missing\n", encoding="utf-8")
            self.assertEqual(load_done_domains(str(path)), {"example.ir", "shop.ir"})

    def test_load_done_domains_from_json_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.json"
            path.write_text(
                json.dumps([{"domain": "example.ir"}, {"domain": "https://www.shop.ir"}]),
                encoding="utf-8",
            )
            self.assertEqual(load_done_domains(str(path)), {"example.ir", "shop.ir"})

    def test_csv_resume_appends_without_new_header(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.csv"
            first = ResultSink(str(path), use_csv=True, check=False, expired=False, total=2)
            first.add({"domain": "example.ir", "status": "ok", "id": 1})
            first.close()
            second = ResultSink(
                str(path), use_csv=True, check=False, expired=False, total=1, resume=True
            )
            second.add({"domain": "shop.ir", "status": "ok", "id": 2})
            second.close()
            text = path.read_text(encoding="utf-8-sig")
            self.assertEqual(text.count("domain,status"), 1)
            self.assertIn("example.ir", text)
            self.assertIn("shop.ir", text)

    def test_error_row_has_status(self) -> None:
        row = _error_row("example.ir", check=False, expired=False, message="timeout")
        self.assertEqual(row["status"], "error")
        self.assertEqual(row["error"], "timeout")


if __name__ == "__main__":
    unittest.main()
