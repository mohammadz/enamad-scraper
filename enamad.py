#!/usr/bin/env python3
"""Lookup Enamad (Iranian e-trust seal) information for a domain."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _reexec_venv() -> None:
    """Re-run this file with .venv so `python3 enamad.py` works without activate."""
    root = Path(__file__).resolve().parent
    venv_root = root / ".venv"
    if sys.platform == "win32":
        target = venv_root / "Scripts" / "python.exe"
    else:
        target = venv_root / "bin" / "python"
    if not target.exists():
        return
    if Path(sys.prefix).resolve() == venv_root.resolve():
        return
    os.execv(os.fsdecode(target), [os.fsdecode(target), *sys.argv])


if __name__ == "__main__":
    _reexec_venv()

import argparse
import csv
import json
import logging
import re
import socket
import ssl
import threading
import time
from functools import cached_property
from io import StringIO
from typing import Any, TextIO
from urllib.parse import parse_qs, urljoin, urlparse

import jdatetime
import requests
import urllib3
from bs4 import BeautifulSoup, Tag
from requests.adapters import HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

log = logging.getLogger("enamad")

HOMEPAGE_URLS = (
    "https://enamad.ir/",
    "https://www.enamad.ir/",
)
HOME_SEARCH_PATHS = (
    "/Home/CheckEnamad",
    "/Home/CheckDomain",
    "/Home/SearchDomain",
    "/Home/GetEnamad",
)
SEARCH_URLS = (
    "https://enamad.ir/DomainListForMIMT",
    "https://www.enamad.ir/DomainListForMIMT",
    "http://enamad.ir/DomainListForMIMT",
)
DEFAULT_TIMEOUT = 60
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
PROFILE_KEYS = (
    "id",
    "title",
    "name",
    "start_date",
    "expire_date",
    "address",
    "phone",
    "email",
    "work_time",
    "history",
    "star",
)
OUTPUT_KEYS = ("domain",) + PROFILE_KEYS
POSITIONAL_KEYS = (
    "name",
    "start_date",
    "expire_date",
    "address",
    "phone",
    "email",
    "work_time",
    "history",
)
TRUSTSEAL_HOSTS = ("trustseal.enamad.ir", "logo.enamad.ir")
PROFILE_LABELS = {
    "name": ("صاحب امتیاز", "نام صاحب امتیاز", "نام شخص", "مدیر مسئول"),
    "start_date": ("تاریخ اعطا", "تاریخ صدور", "تاریخ اعطای نماد"),
    "expire_date": ("تاریخ اعتبار", "تاریخ انقضا", "انقضای نماد"),
    "address": ("آدرس", "نشانی"),
    "phone": ("تلفن", "شماره تماس", "تلفن ثابت"),
    "email": ("پست الکترونیک", "ایمیل", "پست الکترونیکی"),
    "work_time": ("ساعت پاسخگویی", "ساعت کاری", "ساعات پاسخگویی"),
    "history": ("سابقه کسب و کار", "سابقه فعالیت", "سابقه"),
}


class EnamadError(Exception):
    """Raised when Enamad cannot be reached or the page cannot be parsed."""


class TLSAdapter(HTTPAdapter):
    """Permit older TLS used by some Iranian government sites."""

    def init_poolmanager(self, *args: Any, **kwargs: Any) -> None:
        context = create_urllib3_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        try:
            context.set_ciphers("DEFAULT:@SECLEVEL=1")
        except ssl.SSLError:
            pass
        kwargs["ssl_context"] = context
        super().init_poolmanager(*args, **kwargs)


def _force_ipv4() -> None:
    """Match the original PHP client's force_ip_resolve=v4."""
    import urllib3.util.connection as urllib3_connection

    urllib3_connection.allowed_gai_family = lambda: socket.AF_INET


def normalize_domain(value: str) -> str:
    text = (value or "").strip()
    if not text:
        raise EnamadError("دامنه خالی است.")
    if "://" not in text:
        text = "https://" + text
    parsed = urlparse(text)
    host = (parsed.hostname or parsed.path.split("/")[0] or "").lower()
    host = host.removeprefix("www.")
    if not host:
        raise EnamadError("دامنه نامعتبر است.")
    return host


def to_ascii_digits(text: str) -> str:
    return text.translate(PERSIAN_DIGITS)


def parse_jalali_date(text: str) -> jdatetime.date | None:
    cleaned = to_ascii_digits(text or "").strip()
    match = re.search(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})", cleaned)
    if not match:
        return None
    year, month, day = (int(part) for part in match.groups())
    try:
        return jdatetime.date(year, month, day)
    except ValueError:
        return None


def _make_soup(html: str) -> BeautifulSoup:
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:
        return BeautifulSoup(html, "html.parser")


def _text(node: Tag | None) -> str:
    if node is None:
        return ""
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()


def _absolute_url(href: str, base: str = "https://enamad.ir/") -> str:
    return urljoin(base, href)


def _normalize_fa(text: str) -> str:
    return (text or "").replace("ك", "ک").replace("ي", "ی")


def _clean_email(value: str) -> str:
    return value.replace("[at]", "@").replace("[AT]", "@")


def _jalali_date_only(value: str) -> str:
    match = re.search(r"(\d{4}/\d{1,2}/\d{1,2})", to_ascii_digits(value or ""))
    return match.group(1) if match else ""


def _as_enamad_id(value: Any) -> int | None:
    if value in (None, "", 0, "0"):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _enamad_id_from_url(url: str) -> int | None:
    query = parse_qs(urlparse(url or "").query)
    return _as_enamad_id((query.get("id") or query.get("Id") or [""])[0])


def _row_mentions_domain(row: Tag, domain: str) -> bool:
    blob = _text(row).lower()
    return domain in blob or f"www.{domain}" in blob


def parse_search_page(html: str, domain: str) -> dict[str, str] | None:
    """Return the matching list-row, or None if the domain is not listed."""
    soup = _make_soup(html)
    rows = _search_rows(soup)
    log.debug("search rows=%s html_len=%s", len(rows), len(html))
    for row in rows:
        parsed = _parse_search_row(row, domain)
        if parsed:
            return parsed
    return None


def _search_rows(soup: BeautifulSoup) -> list[Tag]:
    rows: list[Tag] = []
    container = soup.select_one("#ListContent #Div_Content") or soup.select_one(
        "#ListContent"
    )
    if container:
        rows.extend(row for row in container.select(".row"))
    table = soup.select_one("div.table") or soup.find("table")
    if table:
        rows.extend(row for row in table.select(".row"))
        rows.extend(table.select("tr"))
    # De-duplicate while preserving order.
    seen: set[int] = set()
    unique: list[Tag] = []
    for row in rows:
        marker = id(row)
        if marker in seen:
            continue
        seen.add(marker)
        unique.append(row)
    return unique


def _parse_search_row(row: Tag, expected_domain: str) -> dict[str, str] | None:
    if not _row_mentions_domain(row, expected_domain):
        return None

    links = row.find_all("a", href=True)
    domain = expected_domain
    profile_url = None
    for link in links:
        href = link.get("href", "").strip()
        classes = link.get("class") or []
        link_text = _text(link).lower()
        if "exlink" in classes or _looks_like_domain(link_text):
            try:
                domain = normalize_domain(link_text or href)
            except EnamadError:
                domain = expected_domain
        elif profile_url is None and href and not href.lower().startswith("javascript"):
            profile_url = _absolute_url(href)

    if not profile_url and links:
        profile_url = _absolute_url(links[0]["href"])
    if not profile_url:
        return None

    cells = [_text(cell) for cell in row.find_all(["td", "th", "div"]) if _text(cell)]
    expire_date = ""
    start_date = ""
    dates = [cell for cell in cells if parse_jalali_date(cell) and len(cell) <= 20]
    if len(dates) >= 2:
        start_date, expire_date = dates[-2], dates[-1]
    elif dates:
        expire_date = dates[-1]

    return {
        "domain": domain,
        "profile_url": profile_url,
        "start_date": start_date,
        "expire_date": expire_date,
    }


def _looks_like_domain(value: str) -> bool:
    host = value.strip().lower().removeprefix("http://").removeprefix("https://")
    host = host.split("/")[0].removeprefix("www.")
    return bool(re.match(r"^[a-z0-9.-]+\.[a-z]{2,}$", host))


def build_trustseal_url(enamad_id: str | int | None, code: str | None = None) -> str:
    query = f"id={enamad_id or ''}"
    if code:
        query += f"&code={code}"
    return f"https://trustseal.enamad.ir/?{query}"


def normalize_trustseal_url(url: str) -> str:
    parsed = urlparse(url)
    if (parsed.hostname or "").lower() not in TRUSTSEAL_HOSTS:
        return url
    query = parse_qs(parsed.query)
    enamad_id = (query.get("id") or query.get("Id") or [""])[0]
    code = (query.get("code") or query.get("Code") or [""])[0]
    return build_trustseal_url(enamad_id, code or None)


def extract_trustseal_url(html: str) -> str | None:
    """Find the official Enamad badge URL embedded on a business website."""
    soup = _make_soup(html)
    candidates: list[str] = []
    for tag in soup.find_all(["a", "img"]):
        href = (tag.get("href") or tag.get("src") or "").strip()
        if not href:
            continue
        host = (urlparse(_absolute_url(href)).hostname or "").lower()
        if host in TRUSTSEAL_HOSTS or host.endswith(".enamad.ir"):
            if "logo.aspx" in href.lower():
                href = href.replace("logo.aspx", "").replace("Logo.aspx", "")
            candidates.append(_absolute_url(href))
    for raw in re.findall(
        r"https?://(?:trustseal|logo)\.enamad\.ir[^\"'\s<>]*", html, flags=re.I
    ):
        candidates.append(raw.replace("logo.aspx", "").replace("Logo.aspx", ""))

    for url in candidates:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if host not in TRUSTSEAL_HOSTS:
            continue
        query = parse_qs(parsed.query)
        if query.get("Code") or query.get("code") or query.get("id") or query.get("Id"):
            return normalize_trustseal_url(url.split("#")[0])
    return candidates[0] if candidates else None


def parse_profile_page(html: str) -> dict[str, Any]:
    soup = _make_soup(html)
    data: dict[str, Any] = {key: "" for key in PROFILE_KEYS}
    data.update(_parse_labeled_rows(soup))
    labeled = _parse_labeled_profile(soup)
    for key, value in labeled.items():
        if value and not data.get(key):
            data[key] = value

    if not data.get("name"):
        data.update(_parse_positional_profile(soup))

    if data.get("email"):
        data["email"] = _clean_email(str(data["email"]))
    for key in ("start_date", "expire_date"):
        cleaned = _jalali_date_only(str(data.get(key) or ""))
        if cleaned:
            data[key] = cleaned
    if data.get("history"):
        data["history"] = re.sub(r"\s+", " ", str(data["history"])).strip()

    shop = soup.select_one("#shopLink")
    if shop and not data.get("title"):
        data["title"] = _text(shop)
    if not data.get("id"):
        for tag in soup.find_all(["a", "img"]):
            href = (tag.get("href") or tag.get("src") or "").strip()
            found_id = _enamad_id_from_url(_absolute_url(href)) if href else None
            if found_id:
                data["id"] = found_id
                break
    data["id"] = _as_enamad_id(data.get("id"))

    star = _parse_star(soup)
    if star:
        data["star"] = star
    if data.get("star") in ("", None):
        data["star"] = None
    else:
        try:
            data["star"] = int(str(data["star"]).strip())
        except ValueError:
            pass
    return data


def _parse_labeled_rows(soup: BeautifulSoup) -> dict[str, str]:
    found: dict[str, str] = {}
    for row in soup.select(".row"):
        label_el = row.select_one(".txtbold")
        if label_el is None:
            continue
        label = _normalize_fa(_text(label_el))
        value_el = None
        for el in row.select(".contentinformation"):
            classes = el.get("class") or []
            if el is label_el or "txtbold" in classes:
                continue
            value_el = el
            break
        if value_el is None:
            continue
        _assign_profile_field(found, label, _text(value_el))
    blob = _normalize_fa(soup.get_text(" ", strip=True))
    history = re.search(r"سابقه کسب و کار\s*:\s*(.+?)(?:سطح|تعداد|اطلاعات|$)", blob)
    if history and not found.get("history"):
        found["history"] = re.sub(r"\s+", " ", history.group(1)).strip()
    return found


def _assign_profile_field(found: dict[str, str], label: str, value: str) -> None:
    if not value:
        return
    for key, labels in PROFILE_LABELS.items():
        if any(_normalize_fa(item) in label for item in labels):
            if not found.get(key):
                found[key] = value
            return


def _parse_positional_profile(soup: BeautifulSoup) -> dict[str, str]:
    values: list[str] = []
    values.extend(
        _text(node)
        for node in soup.select("div.contentinformation.col-md-6")
        if _text(node) and "txtbold" not in (node.get("class") or [])
    )
    values.extend(
        _text(node)
        for node in soup.select("div.contentinformation.col-md-8")
        if _text(node) and "txtbold" not in (node.get("class") or [])
    )
    skill_spans = soup.select(".myskill_area p.txtcenter span")
    if len(skill_spans) > 5 and _text(skill_spans[5]):
        values.append(_text(skill_spans[5]))
    values = values[: len(POSITIONAL_KEYS)]
    while len(values) < len(POSITIONAL_KEYS):
        values.append("")
    return dict(zip(POSITIONAL_KEYS, values))


def _parse_labeled_profile(soup: BeautifulSoup) -> dict[str, str]:
    blob = _normalize_fa(soup.get_text("\n", strip=True))
    found: dict[str, str] = {}
    for key, labels in PROFILE_LABELS.items():
        for label in labels:
            match = re.search(
                rf"{re.escape(_normalize_fa(label))}\s*[:：]?\s*(.+)",
                blob,
            )
            if match:
                value = re.split(r"[\n\r]", match.group(1), maxsplit=1)[0].strip()
                if value:
                    found[key] = value
                    break
    return found


def discover_homepage_search(html: str) -> dict[str, str] | None:
    """Read the homepage BtnSearchDomain handler and return url/method/param."""
    idx = html.find("BtnSearchDomain")
    if idx == -1:
        idx = html.find("website_url")
    window = html[max(0, idx) : max(0, idx) + 5000] if idx != -1 else html
    url_match = re.search(r"""url\s*:\s*['\"]([^'\"]+)['\"]""", window, re.I)
    if not url_match:
        url_match = re.search(
            r"""(?:\$\.(?:post|get)\()\s*['\"]([^'\"]+)['\"]""",
            window,
            re.I,
        )
    if not url_match:
        return None
    method_match = re.search(r"""type\s*:\s*['\"](POST|GET)['\"]""", window, re.I)
    data_match = re.search(r"data\s*:\s*\{([^}]+)\}", window, re.I | re.S)
    param_match = None
    if data_match:
        param_match = re.search(
            r"""['\"]?(domain|website_url|your-site|site|url)['\"]?\s*:""",
            data_match.group(1),
            re.I,
        )
    return {
        "url": url_match.group(1),
        "method": (method_match.group(1).upper() if method_match else "POST"),
        "param": (param_match.group(1) if param_match else "domain"),
    }


def parse_sweetalert_html(html: str) -> dict[str, Any] | None:
    soup = _make_soup(html)
    alert = soup.select_one(".sweet-alert, .showSweetAlert") or soup
    text = _text(alert)
    if not text:
        return None
    if "یافت نشد" in text and "معتبر" not in text:
        return None
    if "وضعیت اینماد" not in text and alert.select_one(".sa-success") is None:
        if "معتبر" not in text:
            return None

    name = _text(alert.find("h2"))
    stars = len(alert.select(".fa-star, i.fa-star"))
    start_date = ""
    expire_date = ""
    start_match = re.search(r"تاریخ اعطا\s*:\s*([0-9۰-۹/]+)", text)
    expire_match = re.search(r"تاریخ اعتبار\s*:\s*([0-9۰-۹/]+)", text)
    if start_match:
        start_date = to_ascii_digits(start_match.group(1))
    if expire_match:
        expire_date = to_ascii_digits(expire_match.group(1))
    profile_url = ""
    for link in alert.find_all("a", href=True):
        href = link.get("href", "")
        if "enamad.ir" in href or "trustseal" in href:
            profile_url = _absolute_url(href)
            break
    return {
        "name": name,
        "title": name,
        "star": stars or None,
        "start_date": start_date,
        "expire_date": expire_date,
        "profile_url": profile_url,
        "status": "معتبر" if "معتبر" in text else "",
    }


def parse_search_api_payload(
    payload: Any, domain: str | None = None
) -> dict[str, Any] | None:
    if payload is None:
        return None
    if isinstance(payload, (bytes, bytearray)):
        payload = payload.decode("utf-8", errors="replace")
    if isinstance(payload, str):
        stripped = payload.strip()
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            return parse_sweetalert_html(stripped)
    if isinstance(payload, dict) and "d" in payload and len(payload) <= 3:
        return parse_search_api_payload(payload["d"], domain)
    if isinstance(payload, list):
        for item in payload:
            parsed = parse_search_api_payload(item, domain)
            if parsed:
                return parsed
        return None
    if not isinstance(payload, dict):
        return parse_sweetalert_html(str(payload))

    if "data" in payload and isinstance(payload["data"], list):
        parsed = _parse_datatables_rows(payload["data"], domain)
        if parsed:
            return parsed
        if payload.get("recordsFiltered") == 1 and payload["data"]:
            parsed = _extract_record(payload["data"][0], domain, require_domain=False)
            if parsed:
                return parsed

    html = (
        payload.get("html")
        or payload.get("Html")
        or payload.get("text")
        or payload.get("Text")
        or payload.get("Message")
        or payload.get("message")
        or payload.get("result")
        or payload.get("Result")
        or ""
    )
    if isinstance(html, (dict, list)):
        nested = parse_search_api_payload(html, domain)
        if nested:
            return nested
        html = ""
    alert = parse_sweetalert_html(str(html)) if html and isinstance(html, str) else None
    extracted = _extract_record(payload, domain)
    success = payload.get("success", payload.get("Success", payload.get("IsSuccess")))
    failed = success is False or success == 0
    if failed and not extracted and not alert:
        return None

    profile_url = (
        (extracted or {}).get("profile_url")
        or payload.get("Url")
        or payload.get("url")
        or payload.get("profileUrl")
        or payload.get("ProfileUrl")
        or payload.get("Link")
        or (alert or {}).get("profile_url")
        or ""
    )
    if not extracted and alert is None and not profile_url and success not in (True, 1, "true", "True"):
        if "معتبر" not in json.dumps(payload, ensure_ascii=False):
            return None

    title = (
        (extracted or {}).get("title")
        or payload.get("persian_name")
        or payload.get("title")
        or payload.get("Title")
        or (alert or {}).get("title")
        or (alert or {}).get("name")
        or ""
    )
    name = (extracted or {}).get("name") or payload.get("Name") or ""
    start_date = (
        (extracted or {}).get("start_date")
        or (alert or {}).get("start_date")
        or ""
    )
    expire_date = (
        (extracted or {}).get("expire_date")
        or (alert or {}).get("expire_date")
        or str(payload.get("ExpireDate") or payload.get("expireDate") or "")
    )
    return {
        "id": _as_enamad_id(
            (extracted or {}).get("id")
            or payload.get("id")
            or _enamad_id_from_url(str(profile_url))
        ),
        "title": title,
        "name": name,
        "star": (extracted or {}).get("star")
        or payload.get("rating")
        or payload.get("Star")
        or payload.get("star")
        or (alert or {}).get("star"),
        "start_date": start_date,
        "expire_date": expire_date,
        "profile_url": profile_url,
        "address": (extracted or {}).get("address") or "",
        "status": (alert or {}).get("status") or "",
        "alert": alert,
    }


def _parse_datatables_rows(rows: list[Any], domain: str | None) -> dict[str, Any] | None:
    for row in rows:
        parsed = _extract_record(row, domain, require_domain=bool(domain))
        if parsed:
            return parsed
    return None


def _extract_record(
    record: Any, domain: str | None, require_domain: bool = False
) -> dict[str, Any] | None:
    if isinstance(record, str):
        if domain and domain not in record.lower() and f"www.{domain}" not in record.lower():
            soup_hit = parse_sweetalert_html(record)
            return soup_hit
        urls = re.findall(
            r"https?://(?:trustseal\.|logo\.|www\.)?enamad\.ir[^\"'\s<>]*",
            record,
            re.I,
        )
        if urls:
            start_date, expire_date = _dates_from_text(record)
            return {
                "name": "",
                "profile_url": normalize_trustseal_url(_absolute_url(urls[0])),
                "start_date": start_date,
                "expire_date": expire_date,
                "star": None,
            }
        return None
    if isinstance(record, list):
        blob = " ".join(str(cell) for cell in record)
        if domain and domain not in blob.lower() and f"www.{domain}" not in blob.lower():
            return None
        parsed: dict[str, Any] = {
            "name": "",
            "profile_url": "",
            "start_date": "",
            "expire_date": "",
            "star": None,
        }
        for cell in record:
            nested = _extract_record(cell, domain)
            if nested:
                for key, value in nested.items():
                    if value and not parsed.get(key):
                        parsed[key] = value
        start_date, expire_date = _dates_from_text(blob)
        parsed["start_date"] = parsed["start_date"] or start_date
        parsed["expire_date"] = parsed["expire_date"] or expire_date
        return parsed if parsed.get("profile_url") or domain and domain in blob.lower() else None
    if not isinstance(record, dict):
        return None

    blob = json.dumps(record, ensure_ascii=False)
    if require_domain and domain:
        if domain not in blob.lower() and f"www.{domain}" not in blob.lower():
            return None
    elif domain and domain not in blob.lower() and f"www.{domain}" not in blob.lower():
        if not any(key in record for key in ("id", "Id", "code", "Code", "Url", "url")):
            return None

    enamad_id = _first_value(record, ("id", "Id", "ID", "namadId", "NamadId", "enamadId"))
    code = _first_value(record, ("code", "Code", "p", "P"))
    profile_url = _first_value(
        record, ("Url", "url", "profileUrl", "ProfileUrl", "Link", "href", "Href")
    )
    if not profile_url and (enamad_id or code):
        profile_url = build_trustseal_url(enamad_id, code)
    elif profile_url:
        profile_url = normalize_trustseal_url(_absolute_url(str(profile_url)))

    text_blob = " ".join(str(v) for v in record.values() if isinstance(v, (str, int)))
    start_date, expire_date = _dates_from_text(text_blob)
    start_date = (
        _first_value(record, ("approvedate", "approve_date", "ApproveDate", "start_date"))
        or start_date
    )
    expire_date = (
        _first_value(record, ("expdate", "expire_date", "ExpireDate", "exp_date"))
        or expire_date
    )
    title = _first_value(
        record,
        (
            "persian_name",
            "PersianName",
            "ShopName",
            "businessName",
            "title",
            "Title",
        ),
    )
    name = _first_value(record, ("Name", "name", "FullName"))
    star = _first_value(record, ("rating", "Rating", "Star", "star", "stars", "Stars"))
    if star not in (None, ""):
        try:
            star = int(star)
        except (TypeError, ValueError):
            pass
    province = _first_value(record, ("statename", "province", "Province"))
    city = _first_value(record, ("cityname", "city", "City"))
    address_parts = [part for part in (province, city) if part and part != "-"]
    address = "، ".join(dict.fromkeys(address_parts))
    if not profile_url and not name and not title and not start_date:
        return None
    return {
        "id": _as_enamad_id(enamad_id) or _enamad_id_from_url(str(profile_url or "")),
        "title": str(title or ""),
        "name": str(name or ""),
        "profile_url": str(profile_url or ""),
        "start_date": str(start_date or ""),
        "expire_date": str(expire_date or ""),
        "star": star,
        "address": address,
    }


def _first_value(record: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if record.get(key) not in (None, ""):
            return record[key]
    for key, value in record.items():
        if key.lower() in {item.lower() for item in keys} and value not in (None, ""):
            return value
    return None


def _dates_from_text(text: str) -> tuple[str, str]:
    found = re.findall(r"(\d{4}/\d{1,2}/\d{1,2})", to_ascii_digits(text or ""))
    if len(found) >= 2:
        return found[0], found[1]
    if len(found) == 1:
        return "", found[0]
    return "", ""


def _parse_star(soup: BeautifulSoup) -> str:
    for img in soup.select("h4.mobiledes img[src], img[src]"):
        src = img.get("src") or ""
        match = re.search(r"Star/star(\d+)\.png", src, re.I)
        if match:
            return match.group(1)
    stars = soup.select("img[src*='star'], img[src*='Star']")
    if stars:
        return str(len(stars))
    return ""


class LookupCache:
    """Reuse the discovered homepage search endpoint across domains."""

    def __init__(self) -> None:
        self.search_url: str | None = None
        self.search_method: str = "POST"
        self.search_param: str = "domain"


def build_session() -> requests.Session:
    _force_ipv4()
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "fa-IR,fa;q=0.9,en;q=0.8",
        }
    )
    adapter = TLSAdapter()
    session.mount("https://", adapter)
    session.mount("http://", HTTPAdapter())
    session.verify = False
    return session


class Enamad:
    def __init__(
        self,
        domain: str,
        session: requests.Session | None = None,
        timeout: int = DEFAULT_TIMEOUT,
        cache: LookupCache | None = None,
    ) -> None:
        self.domain = normalize_domain(domain)
        self.timeout = timeout
        self.session = session or build_session()
        self.cache = cache or LookupCache()
        self._search_html = ""
        self._site_html = ""
        self._search_row_data = self._lookup()

    def _get(self, url: str, params: dict[str, str] | None = None) -> str:
        try:
            response = self.session.get(
                url,
                params=params,
                timeout=self.timeout,
                allow_redirects=True,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise EnamadError(f"اتصال به {urlparse(url).hostname} برقرار نشد: {exc}") from exc
        response.encoding = response.apparent_encoding or "utf-8"
        return response.text

    def _lookup(self) -> dict[str, Any] | None:
        row = self._search_homepage()
        if row:
            log.debug("found via homepage search: %s", row.get("profile_url"))
            return row
        row = self._search_official_list()
        if row:
            log.debug("found in official list: %s", row.get("profile_url"))
            return row
        log.debug("not in official list; looking up trustseal on https://%s", self.domain)
        return self._search_site_badge()

    def _search_homepage(self) -> dict[str, Any] | None:
        if self.cache.search_url:
            parsed = self._call_home_search(
                self.cache.search_url,
                self.cache.search_method,
                self.cache.search_param,
            )
            if parsed:
                return parsed
            self.cache.search_url = None

        homepage_html = ""
        base = HOMEPAGE_URLS[0]
        for url in HOMEPAGE_URLS:
            try:
                homepage_html = self._get(url)
                base = url
                log.debug("homepage loaded %s len=%s", url, len(homepage_html))
                break
            except EnamadError as exc:
                log.debug("homepage failed %s: %s", url, exc)
        if not homepage_html:
            return None

        discovered = discover_homepage_search(homepage_html)
        soup = _make_soup(homepage_html)
        script_origin = base
        if discovered is None:
            for script in soup.find_all("script", src=True):
                src = _absolute_url(script.get("src", ""), base)
                try:
                    js = self._get(src)
                except EnamadError:
                    continue
                discovered = discover_homepage_search(js)
                if discovered:
                    parsed_src = urlparse(src)
                    script_origin = f"{parsed_src.scheme}://{parsed_src.netloc}/"
                    log.debug("search endpoint from %s: %s", src, discovered)
                    break

        endpoints: list[dict[str, str]] = []
        if discovered:
            path = discovered["url"]
            for origin in ("https://enamad.ir/", base, script_origin, "https://reg2.enamad.ir/"):
                endpoints.append(
                    {
                        "url": _absolute_url(path, origin),
                        "method": discovered.get("method") or "POST",
                        "param": discovered.get("param") or "domain",
                    }
                )
        else:
            for path in HOME_SEARCH_PATHS:
                endpoints.append({"url": path, "method": "POST", "param": "domain"})

        tried: set[tuple[str, str]] = set()
        for endpoint in endpoints:
            raw_url = endpoint["url"]
            abs_url = _absolute_url(raw_url, base)
            param = endpoint.get("param") or "domain"
            key = (abs_url, param)
            if key in tried:
                continue
            tried.add(key)
            parsed = self._call_home_search(abs_url, endpoint.get("method") or "POST", param)
            if parsed:
                self.cache.search_url = abs_url
                self.cache.search_method = endpoint.get("method") or "POST"
                self.cache.search_param = param
                return parsed
        return None

    def _call_home_search(self, url: str, method: str, param: str) -> dict[str, Any] | None:
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": "https://enamad.ir/",
            "Accept": "application/json, text/javascript, */*; q=0.01",
        }
        bodies: list[dict[str, Any]] = [{param: self.domain}]
        log.debug("home search %s %s param=%s", method, url, param)
        last_body = ""
        for data in bodies:
            try:
                if method.upper() == "GET":
                    response = self.session.get(
                        url, params=data, headers=headers, timeout=self.timeout
                    )
                else:
                    response = self.session.post(
                        url, data=data, headers=headers, timeout=self.timeout
                    )
            except requests.RequestException as exc:
                log.debug("home search error: %s", exc)
                continue
            if response.status_code >= 400:
                log.debug("home search status %s for %s", response.status_code, url)
                if response.status_code == 404:
                    return None
                continue
            response.encoding = response.apparent_encoding or "utf-8"
            last_body = response.text
            content_type = response.headers.get("content-type", "")
            payload: Any = response.text
            if "json" in content_type.lower() or payload.strip()[:1] in "{[":
                try:
                    payload = response.json()
                except ValueError:
                    payload = response.text
            log.debug("home search body: %s", str(payload)[:1000])
            parsed = parse_search_api_payload(payload, self.domain)
            if parsed:
                profile_url = parsed.get("profile_url") or ""
                return {
                    "domain": self.domain,
                    "profile_url": profile_url,
                    "id": parsed.get("id"),
                    "title": parsed.get("title") or "",
                    "start_date": parsed.get("start_date") or "",
                    "expire_date": parsed.get("expire_date") or "",
                    "name": parsed.get("name") or "",
                    "star": parsed.get("star"),
                    "address": parsed.get("address") or "",
                }
        if last_body:
            log.debug("could not parse GetData response")
        return None

    def _search_official_list(self) -> dict[str, str] | None:
        errors: list[str] = []
        for url in SEARCH_URLS:
            try:
                html = self._get(url, params={"se": self.domain})
            except EnamadError as exc:
                errors.append(str(exc))
                log.debug("search failed %s: %s", url, exc)
                continue
            self._search_html = html
            row = parse_search_page(html, self.domain)
            if row:
                return row
            log.debug("no matching row on %s (len=%s)", url, len(html))
            break
        if errors and not self._search_html:
            log.debug("official list unreachable: %s", " | ".join(errors))
        return None

    def _search_site_badge(self) -> dict[str, str] | None:
        html = self._fetch_site()
        if not html:
            return None
        self._site_html = html
        trustseal = extract_trustseal_url(html)
        if not trustseal:
            log.debug("no trustseal URL on %s", self.domain)
            return None
        log.debug("trustseal URL: %s", trustseal)
        return {
            "domain": self.domain,
            "profile_url": trustseal,
            "start_date": "",
            "expire_date": "",
        }

    def _fetch_site(self) -> str | None:
        urls = (
            f"https://{self.domain}",
            f"https://www.{self.domain}",
            f"http://{self.domain}",
        )
        for url in urls:
            try:
                return self._get(url)
            except EnamadError as exc:
                log.debug("site fetch failed %s: %s", url, exc)
        return None

    def has_enamad(self) -> bool:
        return self._search_row_data is not None

    @cached_property
    def _profile(self) -> dict[str, Any] | None:
        row = self._search_row_data
        if row is None:
            return None
        profile_url = row.get("profile_url") or ""
        data: dict[str, Any] | None = None
        if profile_url:
            html = self._get_profile_html(profile_url)
            if html:
                data = parse_profile_page(html)
        if data is None:
            data = {key: None for key in PROFILE_KEYS}
        for key in (
            "id",
            "title",
            "name",
            "start_date",
            "expire_date",
            "star",
            "address",
            "phone",
            "email",
            "work_time",
            "history",
        ):
            if not data.get(key) and row.get(key) not in (None, ""):
                data[key] = row[key]
        if not data.get("id"):
            data["id"] = _enamad_id_from_url(str(profile_url))
        data["id"] = _as_enamad_id(data.get("id"))
        if data.get("star") not in (None, ""):
            try:
                data["star"] = int(data["star"])
            except (TypeError, ValueError):
                pass
        if not any(data.get(key) for key in PROFILE_KEYS) and not profile_url:
            return None
        return {"domain": self.domain, **data}

    def _get_profile_html(self, url: str) -> str | None:
        candidates = []
        normalized = normalize_trustseal_url(url)
        candidates.append(normalized)
        parsed = urlparse(normalized)
        query = parse_qs(parsed.query)
        enamad_id = (query.get("id") or [""])[0]
        code = (query.get("code") or [""])[0]
        if enamad_id:
            candidates.append(f"https://trustseal.enamad.ir/?id={enamad_id}&code={code}")
        seen: set[str] = set()
        for candidate in candidates:
            if candidate in seen:
                continue
            seen.add(candidate)
            html = self._fetch_maybe_error(candidate)
            if html and ("صاحب امتیاز" in html or "contentinformation" in html):
                return html
            if html:
                log.debug("profile response was not a business page (%s bytes)", len(html))
        log.debug("profile page unavailable, using search payload only")
        return None

    def _fetch_maybe_error(self, url: str) -> str | None:
        headers = {
            "Referer": "https://enamad.ir/",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        try:
            response = self.session.get(
                url, headers=headers, timeout=self.timeout, allow_redirects=True
            )
        except requests.RequestException as exc:
            log.debug("profile fetch failed %s: %s", url, exc)
            return None
        response.encoding = response.apparent_encoding or "utf-8"
        if response.status_code >= 400:
            log.debug(
                "profile status %s for %s (%s bytes)",
                response.status_code,
                url,
                len(response.text),
            )
            return response.text or None
        return response.text

    def get(self) -> dict[str, Any] | None:
        return self._profile

    def is_expired(self) -> bool | None:
        data = self.get()
        if data is None:
            return None
        expire = parse_jalali_date(str(data.get("expire_date") or ""))
        if expire is None:
            return None
        return expire < jdatetime.date.today()


def collect_domains(values: list[str], file_path: str | None = None) -> list[str]:
    raw: list[str] = []
    for item in values:
        raw.extend(part.strip() for part in item.replace(",", " ").split())
    if file_path:
        path = Path(file_path)
        if file_path == "-":
            text = sys.stdin.read()
        else:
            text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            raw.extend(part.strip() for part in line.replace(",", " ").split())
    seen: set[str] = set()
    domains: list[str] = []
    for item in raw:
        if not item:
            continue
        domain = normalize_domain(item)
        if domain in seen:
            continue
        seen.add(domain)
        domains.append(domain)
    if not domains:
        raise EnamadError("دامنه‌ای داده نشده است.")
    return domains


def lookup_many(
    domains: list[str],
    session: requests.Session | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> list[dict[str, Any]]:
    session = session or build_session()
    cache = LookupCache()
    rows: list[dict[str, Any]] = []
    for domain in domains:
        try:
            client = Enamad(domain, session=session, timeout=timeout, cache=cache)
            data = client.get()
            if data is None:
                rows.append({"domain": client.domain, **{key: None for key in PROFILE_KEYS}})
            else:
                rows.append(data)
        except EnamadError as exc:
            log.error("%s: %s", domain, exc)
            rows.append(
                {
                    "domain": normalize_domain(domain),
                    **{key: None for key in PROFILE_KEYS},
                    "error": str(exc),
                }
            )
    return rows


def render_csv(rows: list[dict[str, Any]], fieldnames: tuple[str, ...] | list[str]) -> str:
    buffer = StringIO()
    write_csv(rows, buffer, fieldnames)
    return buffer.getvalue()


def write_csv(
    rows: list[dict[str, Any]],
    dest: TextIO,
    fieldnames: tuple[str, ...] | list[str],
) -> None:
    writer = csv.DictWriter(
        dest,
        fieldnames=list(fieldnames),
        extrasaction="ignore",
        lineterminator="\n",
    )
    writer.writeheader()
    for row in rows:
        writer.writerow(_csv_cells(row, fieldnames))


def _csv_cells(row: dict[str, Any], fieldnames: tuple[str, ...] | list[str]) -> dict[str, Any]:
    return {key: "" if row.get(key) is None else row.get(key) for key in fieldnames}


def json_payload(
    rows: list[dict[str, Any]],
    check: bool,
    expired: bool,
    total: int | None = None,
) -> Any:
    count = total if total is not None else len(rows)
    if check:
        if count == 1:
            return rows[0]["has_enamad"] if rows else None
        return rows
    if expired:
        if count == 1:
            return rows[0]["expired"] if rows else None
        return rows
    if count == 1:
        return {key: rows[0].get(key) for key in OUTPUT_KEYS} if rows else None
    return [{key: row.get(key) for key in OUTPUT_KEYS} for row in rows]


def _json_print(value: Any, dest: TextIO) -> None:
    json.dump(value, dest, ensure_ascii=False, indent=2)
    dest.write("\n")


def _lookup_row(client: Enamad, check: bool, expired: bool) -> dict[str, Any]:
    if check:
        return {"domain": client.domain, "has_enamad": client.has_enamad()}
    if expired:
        return {"domain": client.domain, "expired": client.is_expired()}
    data = client.get()
    if data is None:
        return {"domain": client.domain, **{key: None for key in PROFILE_KEYS}}
    return data


def _error_row(domain: str, check: bool, expired: bool) -> dict[str, Any]:
    if check:
        return {"domain": domain, "has_enamad": False}
    if expired:
        return {"domain": domain, "expired": None}
    return {"domain": domain, **{key: None for key in PROFILE_KEYS}}


def _duration(seconds: float) -> str:
    total = max(0, int(seconds))
    minutes, secs = divmod(total, 60)
    if minutes >= 60:
        hours, minutes = divmod(minutes, 60)
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


class ProgressReporter:
    """Live progress on stderr so stdout stays clean JSON/CSV."""

    def __init__(self, total: int, enabled: bool = True) -> None:
        self.total = max(1, total)
        self.enabled = enabled
        self.tty = enabled and sys.stderr.isatty()
        self._lock = threading.Lock()
        self._index = 0
        self._domain = ""
        self._ok = 0
        self._fail = 0
        self._started = time.monotonic()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        if self.tty:
            sys.stderr.write("\033[?25l")
            sys.stderr.flush()
            self._thread = threading.Thread(target=self._spin, daemon=True)
            self._thread.start()

    def begin(self, index: int, domain: str) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._index = index
            self._domain = domain
        if not self.tty:
            print(
                f"استعلام {index}/{self.total}  {domain}",
                file=sys.stderr,
                flush=True,
            )

    def succeed(self) -> None:
        with self._lock:
            self._ok += 1

    def fail(self) -> None:
        with self._lock:
            self._fail += 1

    def close(self, stopped: bool = False, saved_to: str | None = None) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=0.3)
        try:
            if not self.enabled:
                return
            elapsed = _duration(time.monotonic() - self._started)
            if stopped:
                summary = f"متوقف شد  {self._ok}/{self.total}  ·  {elapsed}"
                if saved_to:
                    summary += f"\nذخیره شد: {saved_to}"
                frame = "■"
                done = False
            elif self._fail:
                summary = f"تمام شد  {self._ok} موفق  {self._fail} ناموفق  ·  {elapsed}"
                frame = "✓"
                done = True
            else:
                summary = f"تمام شد  {self._ok}/{self.total}  ·  {elapsed}"
                frame = "✓"
                done = True
            if self.tty:
                line = self._line(done=done, frame=frame)
                self._write(f"\r{line}\n{summary}\n")
            else:
                print(summary, file=sys.stderr, flush=True)
        finally:
            if self.tty:
                try:
                    sys.stderr.write("\033[?25h")
                    sys.stderr.flush()
                except OSError:
                    pass

    def _spin(self) -> None:
        frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        n = 0
        while not self._stop.wait(0.08):
            self._write("\r" + self._line(done=False, frame=frames[n % len(frames)]))
            n += 1

    def _line(self, done: bool, frame: str) -> str:
        with self._lock:
            index = self._index
            domain = self._domain
        width = 20
        filled = width if done else int(width * max(index, 0) / self.total)
        bar = "█" * filled + "░" * (width - filled)
        percent = 100 if done else int(100 * max(index, 0) / self.total)
        current = domain or "…"
        text = f"{frame}  {index}/{self.total}  {bar}  {percent:3d}%  {current}"
        try:
            columns = os.get_terminal_size(sys.stderr.fileno()).columns
        except OSError:
            columns = 80
        if len(text) > columns:
            text = text[: max(0, columns - 1)] + "…"
        return text.ljust(max(columns, len(text)))

    def _write(self, text: str) -> None:
        try:
            sys.stderr.write(text)
            sys.stderr.flush()
        except OSError:
            pass


class ResultSink:
    """Write results as they arrive. Files stay valid after every domain."""

    def __init__(
        self,
        dest_path: str | None,
        use_csv: bool,
        check: bool,
        expired: bool,
        total: int,
    ) -> None:
        self.dest_path = dest_path
        self.use_csv = use_csv
        self.check = check
        self.expired = expired
        self.total = total
        self.rows: list[dict[str, Any]] = []
        self.fieldnames = _csv_fieldnames(check, expired)
        self._csv_file: TextIO | None = None
        self._csv_writer: Any = None
        self._owns_csv = False
        if dest_path and use_csv:
            self._open_csv(dest_path)
            self._owns_csv = True
        elif use_csv:
            self._csv_file = sys.stdout
            self._csv_writer = csv.DictWriter(
                self._csv_file,
                fieldnames=list(self.fieldnames),
                extrasaction="ignore",
                lineterminator="\n",
            )
            self._csv_writer.writeheader()
            self._csv_file.flush()

    def add(self, row: dict[str, Any]) -> None:
        self.rows.append(row)
        if self.use_csv:
            if self._csv_writer is None or self._csv_file is None:
                return
            self._csv_writer.writerow(_csv_cells(row, self.fieldnames))
            self._flush(self._csv_file)
            return
        if self.dest_path:
            self._rewrite_json()

    def finish_stdout(self, dest: TextIO) -> None:
        if self.dest_path or self.use_csv:
            return
        _json_print(
            json_payload(self.rows, self.check, self.expired, self.total),
            dest,
        )

    def close(self) -> None:
        if self._owns_csv and self._csv_file is not None:
            self._csv_file.close()
            self._csv_file = None
        if self.dest_path and not self.use_csv:
            tmp = Path(self.dest_path).with_name(Path(self.dest_path).name + ".tmp")
            tmp.unlink(missing_ok=True)

    def _open_csv(self, path: str) -> None:
        self._csv_file = Path(path).open("w", encoding="utf-8-sig", newline="")
        self._csv_writer = csv.DictWriter(
            self._csv_file,
            fieldnames=list(self.fieldnames),
            extrasaction="ignore",
            lineterminator="\n",
        )
        self._csv_writer.writeheader()
        self._flush(self._csv_file)

    def _rewrite_json(self) -> None:
        assert self.dest_path is not None
        path = Path(self.dest_path)
        payload = json_payload(self.rows, self.check, self.expired, self.total)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        tmp.replace(path)

    @staticmethod
    def _flush(handle: TextIO) -> None:
        handle.flush()
        try:
            os.fsync(handle.fileno())
        except OSError:
            pass


def _configure_logging(debug: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.WARNING,
        format="%(levelname)s %(message)s",
        stream=sys.stderr,
    )


def _csv_fieldnames(check: bool, expired: bool) -> tuple[str, ...]:
    if check:
        return ("domain", "has_enamad")
    if expired:
        return ("domain", "expired")
    return OUTPUT_KEYS


def _resolve_output(
    csv_path: str | None,
    json_path: str | None,
    output_path: str | None,
) -> tuple[bool, str | None]:
    """Return (use_csv, destination path or None for stdout)."""
    if csv_path not in (None, "-"):
        return True, csv_path
    if json_path not in (None, "-"):
        return False, json_path
    if output_path:
        return str(output_path).lower().endswith(".csv"), output_path
    if csv_path == "-":
        return True, output_path
    return False, output_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="استعلام اینماد یک یا چند دامنه از enamad.ir",
    )
    parser.add_argument(
        "domains",
        nargs="*",
        help="یک یا چند دامنه؛ با کاما هم می‌شود جدا کرد",
    )
    parser.add_argument(
        "-f",
        "--file",
        help="فایل متنی دامنه‌ها (هر خط یک دامنه). از - برای stdin استفاده کنید",
    )
    parser.add_argument(
        "--csv",
        nargs="?",
        const="-",
        metavar="FILE",
        help="خروجی CSV؛ بدون مسیر روی stdout چاپ می‌شود",
    )
    parser.add_argument(
        "--json",
        nargs="?",
        const="-",
        metavar="FILE",
        help="خروجی JSON؛ بدون مسیر روی stdout چاپ می‌شود",
    )
    parser.add_argument(
        "-o",
        "--output",
        help="نوشتن خروجی در فایل",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="فقط وجود اینماد را چاپ می‌کند",
    )
    parser.add_argument(
        "--expired",
        action="store_true",
        help="فقط منقضی بودن اینماد را چاپ می‌کند",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="جزئیات جست‌وجو را در stderr چاپ می‌کند",
    )
    args = parser.parse_args(argv)
    _configure_logging(args.debug)
    if not args.domains and not args.file:
        parser.error("حداقل یک دامنه یا --file لازم است")
    if args.csv is not None and args.json is not None:
        parser.error("فقط یکی از --csv یا --json را مشخص کنید")

    try:
        domains = collect_domains(args.domains, args.file)
    except (EnamadError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    session = build_session()
    cache = LookupCache()
    use_csv, dest_path = _resolve_output(args.csv, args.json, args.output)
    sink = ResultSink(
        dest_path,
        use_csv=use_csv,
        check=args.check,
        expired=args.expired,
        total=len(domains),
    )
    progress = ProgressReporter(len(domains), enabled=not args.debug)
    had_error = False
    stopped = False
    try:
        for index, domain in enumerate(domains, start=1):
            progress.begin(index, domain)
            try:
                client = Enamad(domain, session=session, cache=cache)
                sink.add(_lookup_row(client, args.check, args.expired))
                progress.succeed()
            except EnamadError as exc:
                had_error = True
                log.error("%s: %s", domain, exc)
                sink.add(_error_row(domain, args.check, args.expired))
                progress.fail()
        sink.finish_stdout(sys.stdout)
    except KeyboardInterrupt:
        stopped = True
    finally:
        sink.close()
        progress.close(stopped=stopped, saved_to=dest_path)

    if stopped:
        return 130
    return 2 if had_error else 0


if __name__ == "__main__":
    raise SystemExit(main())
