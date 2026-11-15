"""CalDAV calendar plugin for InkyPi.

Reads events (VEVENT) and tasks (VTODO) from any CalDAV server using plain
HTTP requests, so no extra Python packages are needed beyond what InkyPi
already ships (requests, icalendar, recurring_ical_events).

Works with any CalDAV server: Nextcloud, Radicale, Baikal, Vikunja, etc.
"""

import calendar as pycalendar
import json
import logging
import os
from datetime import date, datetime, time, timedelta
from urllib.parse import unquote, urljoin
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

import icalendar
import recurring_ical_events
import requests
from requests.auth import HTTPBasicAuth

from plugins.base_plugin.base_plugin import BasePlugin

logger = logging.getLogger(__name__)

DEFAULT_ENV_KEY = "CALDAV_TOKEN"
HTTP_TIMEOUT = 20

NS = {"d": "DAV:", "c": "urn:ietf:params:xml:ns:caldav"}

PROPFIND_BODY = """<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:prop>
    <d:resourcetype/>
    <d:displayname/>
    <d:current-user-principal/>
    <c:calendar-home-set/>
    <c:supported-calendar-component-set/>
  </d:prop>
</d:propfind>"""

REPORT_EVENTS_BODY = """<?xml version="1.0" encoding="utf-8"?>
<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:prop><d:getetag/><c:calendar-data/></d:prop>
  <c:filter>
    <c:comp-filter name="VCALENDAR">
      <c:comp-filter name="VEVENT">
        <c:time-range start="{start}" end="{end}"/>
      </c:comp-filter>
    </c:comp-filter>
  </c:filter>
</c:calendar-query>"""

# No time-range for tasks: many servers handle it poorly for
# VTODO, and overdue/undated tasks must be fetched anyway. Filtering is local.
REPORT_TODOS_BODY = """<?xml version="1.0" encoding="utf-8"?>
<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:prop><d:getetag/><c:calendar-data/></d:prop>
  <c:filter>
    <c:comp-filter name="VCALENDAR">
      <c:comp-filter name="VTODO"/>
    </c:comp-filter>
  </c:filter>
</c:calendar-query>"""

DEFAULT_LANGUAGE = "en"
LOCALES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "locales")

# Built-in English strings. They are the fallback for any key a translation
# file in locales/ does not define, so a partial translation still works.
LABELS_EN = {
    "name": "English",
    "weekdays": [
        "Monday",
        "Tuesday",
        "Wednesday",
        "Thursday",
        "Friday",
        "Saturday",
        "Sunday"
    ],
    "weekdays_short": [
        "Mon",
        "Tue",
        "Wed",
        "Thu",
        "Fri",
        "Sat",
        "Sun"
    ],
    "months": [
        "January",
        "February",
        "March",
        "April",
        "May",
        "June",
        "July",
        "August",
        "September",
        "October",
        "November",
        "December"
    ],
    "months_gen": [
        "January",
        "February",
        "March",
        "April",
        "May",
        "June",
        "July",
        "August",
        "September",
        "October",
        "November",
        "December"
    ],
    "today": "Today",
    "tomorrow": "Tomorrow",
    "overdue": "Overdue",
    "undated": "No due date",
    "empty": "Nothing planned",
    "all_day": "all day"
}

LIST_KEYS = {"weekdays": 7, "weekdays_short": 7, "months": 12, "months_gen": 12}


def available_languages():
    """Returns [(code, display name)] for every locales/<code>.json, English first."""
    languages = {DEFAULT_LANGUAGE: LABELS_EN["name"]}
    try:
        files = sorted(os.listdir(LOCALES_DIR))
    except OSError:
        files = []
    for file_name in files:
        code, ext = os.path.splitext(file_name)
        if ext != ".json" or code == DEFAULT_LANGUAGE:
            continue
        try:
            with open(os.path.join(LOCALES_DIR, file_name), encoding="utf-8") as f:
                languages[code] = str(json.load(f).get("name") or code)
        except (OSError, ValueError, AttributeError) as e:
            logger.warning("Skipping broken translation %s: %s", file_name, e)
    return list(languages.items())


def load_labels(code):
    """Loads locales/<code>.json on top of the English defaults."""
    labels = dict(LABELS_EN)
    code = os.path.basename(str(code or DEFAULT_LANGUAGE))  # no path tricks via settings
    path = os.path.join(LOCALES_DIR, f"{code}.json")
    if not os.path.isfile(path):
        return labels
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        logger.warning("Cannot read translation %s, using English: %s", path, e)
        return labels
    if not isinstance(data, dict):
        return labels
    for key, value in data.items():
        if key not in labels:
            continue
        if key in LIST_KEYS:
            if isinstance(value, list) and len(value) == LIST_KEYS[key]:
                labels[key] = [str(v) for v in value]
            else:
                logger.warning("Translation %s: \"%s\" must be a list of %d items", code, key, LIST_KEYS[key])
        elif isinstance(value, str):
            labels[key] = value
    return labels


class CalDavError(RuntimeError):
    pass


class CalDavClient:
    """Minimal read-only CalDAV client (discovery + calendar-query)."""

    def __init__(self, url, session):
        self.url = url
        self.session = session

    # -- HTTP -----------------------------------------------------------------
    def _request(self, method, url, body=None, depth=None):
        headers = {"Content-Type": "application/xml; charset=utf-8"}
        if depth is not None:
            headers["Depth"] = str(depth)
        try:
            resp = self.session.request(
                method, url, data=body.encode("utf-8") if body else None,
                headers=headers, timeout=HTTP_TIMEOUT,
            )
        except requests.exceptions.SSLError as e:
            raise CalDavError("TLS certificate check failed for the CalDAV server.") from e
        except requests.exceptions.RequestException as e:
            raise CalDavError(f"Cannot reach the CalDAV server: {type(e).__name__}") from e
        if resp.status_code in (401, 403):
            raise CalDavError("CalDAV authorization failed. Check the username and token.")
        return resp

    @staticmethod
    def _multistatus(resp):
        """Returns a list of (href, {prop_tag: element}) for a 207 response."""
        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError as e:
            raise CalDavError("CalDAV server returned an invalid XML response.") from e
        result = []
        for node in root.findall("d:response", NS):
            href = (node.findtext("d:href", default="", namespaces=NS) or "").strip()
            props = {}
            for propstat in node.findall("d:propstat", NS):
                status = propstat.findtext("d:status", default="", namespaces=NS)
                if status and " 200" not in status:
                    continue
                prop = propstat.find("d:prop", NS)
                if prop is not None:
                    for child in prop:
                        props[child.tag] = child
            result.append((href, props))
        return result

    @staticmethod
    def _href_of(props, tag):
        el = props.get(tag)
        if el is None:
            return None
        href = el.findtext("d:href", default="", namespaces=NS)
        return href.strip() or None

    @staticmethod
    def _is_calendar(props):
        rt = props.get("{DAV:}resourcetype")
        return rt is not None and rt.find("c:calendar", NS) is not None

    def _calendar_info(self, href, props, base=None):
        name_el = props.get("{DAV:}displayname")
        name = (name_el.text or "").strip() if name_el is not None else ""
        slug = unquote(href.rstrip("/").rsplit("/", 1)[-1])
        comps = None
        comp_set = props.get("{urn:ietf:params:xml:ns:caldav}supported-calendar-component-set")
        if comp_set is not None:
            found = {c.get("name", "").upper() for c in comp_set.findall("c:comp", NS)}
            found.discard("")
            comps = found or None
        return {"url": urljoin(base or self.url, href), "name": name or slug, "slug": slug, "components": comps}

    # -- Discovery ------------------------------------------------------------
    def discover(self):
        """Returns (calendars, ics_text).

        `calendars` is a list of calendar dicts. If the URL is a plain .ics
        resource rather than a CalDAV endpoint, calendars is empty and
        `ics_text` holds the downloaded file.
        """
        resp = self._request("PROPFIND", self.url, PROPFIND_BODY, depth=0)
        if resp.status_code != 207:
            if "BEGIN:VCALENDAR" in resp.text:
                return [], resp.text
            get = self._request("GET", self.url)
            if get.ok and "BEGIN:VCALENDAR" in get.text:
                return [], get.text
            raise CalDavError(f"CalDAV discovery failed (HTTP {resp.status_code}). Check the URL.")

        responses = self._multistatus(resp)
        if not responses:
            raise CalDavError("CalDAV server returned an empty discovery response.")
        href, props = responses[0]

        if self._is_calendar(props):
            return [self._calendar_info(href or self.url, props)], None

        home = self._href_of(props, "{urn:ietf:params:xml:ns:caldav}calendar-home-set")
        if not home:
            principal = self._href_of(props, "{DAV:}current-user-principal")
            if principal:
                p_resp = self._request("PROPFIND", urljoin(self.url, principal), PROPFIND_BODY, depth=0)
                if p_resp.status_code == 207:
                    for _, p_props in self._multistatus(p_resp):
                        home = self._href_of(p_props, "{urn:ietf:params:xml:ns:caldav}calendar-home-set")
                        if home:
                            break
        # Last resort: treat the given URL as the collection that holds calendars.
        home_url = urljoin(self.url, home) if home else self.url

        h_resp = self._request("PROPFIND", home_url, PROPFIND_BODY, depth=1)
        if h_resp.status_code != 207:
            raise CalDavError(f"Cannot list calendars (HTTP {h_resp.status_code}).")
        calendars = [
            # Hrefs are relative to the server that answered, which may differ
            # from self.url (iCloud moves calendars to a pNN-caldav.icloud.com host).
            self._calendar_info(h, p, home_url) for h, p in self._multistatus(h_resp) if self._is_calendar(p)
        ]
        if not calendars:
            raise CalDavError("No calendars found at this URL. Try the direct calendar URL.")
        return calendars, None

    # -- Data -----------------------------------------------------------------
    def _report(self, cal, body):
        resp = self._request("REPORT", cal["url"], body, depth=1)
        if resp.status_code == 207:
            chunks = []
            for _, props in self._multistatus(resp):
                data = props.get("{urn:ietf:params:xml:ns:caldav}calendar-data")
                if data is not None and data.text and "BEGIN:VCALENDAR" in data.text:
                    chunks.append(data.text)
            return chunks
        if resp.ok and "BEGIN:VCALENDAR" in resp.text:
            return [resp.text]
        raise CalDavError(f"Calendar \"{cal['name']}\" query failed (HTTP {resp.status_code}).")

    def fetch(self, cal, start, end, want_events=True, want_todos=True):
        """Returns (event_ics_chunks, todo_ics_chunks) for one calendar."""
        comps = cal.get("components")
        events, todos = [], []
        if want_events and (comps is None or "VEVENT" in comps):
            fmt = "%Y%m%dT%H%M%SZ"
            utc = ZoneInfo("UTC")
            body = REPORT_EVENTS_BODY.format(
                start=start.astimezone(utc).strftime(fmt), end=end.astimezone(utc).strftime(fmt)
            )
            events = self._report(cal, body)
        if want_todos and (comps is None or "VTODO" in comps):
            todos = self._report(cal, REPORT_TODOS_BODY)
        return events, todos


# -- iCalendar helpers --------------------------------------------------------
def _to_local(value, tz):
    """Normalises a date/datetime to an aware datetime in tz. Returns (dt, all_day)."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=tz)
        return value.astimezone(tz), False
    return datetime.combine(value, time.min, tzinfo=tz), True


def _prop_dt(component, name):
    prop = component.get(name)
    return getattr(prop, "dt", None) if prop is not None else None


def _text(component, name, default=""):
    value = component.get(name)
    return str(value).strip() if value is not None else default


def parse_events(ics_text, start, end, tz, calendar_name):
    items = []
    cal = icalendar.Calendar.from_ical(ics_text)
    for ev in recurring_ical_events.of(cal).between(start, end):
        raw_start = _prop_dt(ev, "DTSTART")
        if raw_start is None:
            continue
        ev_start, all_day = _to_local(raw_start, tz)
        raw_end = _prop_dt(ev, "DTEND")
        if raw_end is not None:
            ev_end, _ = _to_local(raw_end, tz)
        else:
            duration = _prop_dt(ev, "DURATION")
            if isinstance(duration, timedelta):
                ev_end = ev_start + duration
            else:
                ev_end = ev_start + timedelta(days=1) if all_day else ev_start
        if _text(ev, "STATUS").upper() == "CANCELLED":
            continue
        items.append({
            "kind": "event",
            "uid": _text(ev, "UID"),
            "title": _text(ev, "SUMMARY", "(no title)"),
            "start": ev_start,
            "end": ev_end,
            "all_day": all_day,
            "calendar": calendar_name,
        })
    return items


def parse_todos(ics_text, tz, calendar_name):
    items = []
    cal = icalendar.Calendar.from_ical(ics_text)
    for todo in cal.walk("VTODO"):
        status = _text(todo, "STATUS").upper()
        if status in ("COMPLETED", "CANCELLED") or todo.get("COMPLETED") is not None:
            continue
        raw_due = _prop_dt(todo, "DUE") or _prop_dt(todo, "DTSTART")
        due, all_day = (None, True)
        if raw_due is not None:
            due, all_day = _to_local(raw_due, tz)
        items.append({
            "kind": "task",
            "uid": _text(todo, "UID"),
            "title": _text(todo, "SUMMARY", "(no title)"),
            "start": due,
            "end": due,
            "all_day": all_day,
            "calendar": calendar_name,
        })
    return items


# -- Plugin -------------------------------------------------------------------
def _flag(settings, key, default):
    value = settings.get(key)
    if value is None or value == "":
        return default
    return str(value).lower() in ("true", "1", "on", "yes")


def _int(settings, key, default, lo, hi):
    try:
        return max(lo, min(hi, int(settings.get(key) or default)))
    except (TypeError, ValueError):
        return default


class CalDav(BasePlugin):
    def generate_settings_template(self):
        template_params = super().generate_settings_template()
        template_params["style_settings"] = True
        template_params["default_env_key"] = DEFAULT_ENV_KEY
        template_params["languages"] = available_languages()
        return template_params

    def generate_image(self, settings, device_config):
        url = (settings.get("caldavUrl") or "").strip()
        if not url:
            raise RuntimeError("CalDAV URL is required.")
        if not url.lower().startswith(("http://", "https://")):
            raise RuntimeError("CalDAV URL must start with http:// or https://")

        dimensions = device_config.get_resolution()
        if device_config.get_config("orientation") == "vertical":
            dimensions = dimensions[::-1]
        tz_name = device_config.get_config("timezone", default="UTC")
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            logger.warning("Unknown timezone %s, falling back to UTC", tz_name)
            tz = ZoneInfo("UTC")
        time_format = device_config.get_config("time_format", default="24h")

        view = settings.get("view") or "agenda"
        labels = load_labels(settings.get("language"))
        lang_code = os.path.basename(str(settings.get("language") or DEFAULT_LANGUAGE))
        show_tasks = _flag(settings, "showTasks", True)
        show_events = _flag(settings, "showEvents", True)
        show_undated = _flag(settings, "showUndated", False)
        week_start = 6 if settings.get("weekStart") == "sunday" else 0

        now = datetime.now(tz)
        today = now.date()
        if view == "month":
            weeks = pycalendar.Calendar(firstweekday=week_start).monthdatescalendar(today.year, today.month)
            first_day, last_day = weeks[0][0], weeks[-1][-1]
        else:
            weeks = None
            first_day = today
            days = 1 if view == "today" else _int(settings, "days", 7, 1, 60)
            last_day = today + timedelta(days=days - 1)
        range_start = datetime.combine(first_day, time.min, tzinfo=tz)
        range_end = datetime.combine(last_day + timedelta(days=1), time.min, tzinfo=tz)

        items = self._load_items(settings, device_config, url, range_start, range_end, tz,
                                 show_events, show_tasks)

        fmt_time = (lambda d: d.strftime("%H:%M")) if time_format == "24h" else \
                   (lambda d: d.strftime("%I:%M %p").lstrip("0"))
        by_day, overdue, undated = self._bucket(items, first_day, last_day, today, fmt_time, labels)

        base_px = max(11, round(min(dimensions) / 30))
        if view == "today":  # one day only, so there is room for larger type
            base_px = round(base_px * 1.25)
        params = {
            "labels": labels,
            "lang": lang_code,  # lets the browser apply language rules, e.g. Greek uppercasing
            "base_px": base_px,
            # Never hand the secret to the HTML template.
            "plugin_settings": {k: v for k, v in settings.items() if k != "token"},
        }
        custom_title = (settings.get("title") or "").strip()

        if view == "month":
            header_px = base_px * 4.6
            cell_h = (dimensions[1] - header_px) / len(weeks)
            max_lines = max(1, int((cell_h - base_px * 1.5) / (base_px * 0.95)))
            grid = []
            for week in weeks:
                row = []
                for d in week:
                    entries = by_day.get(d, [])
                    shown = entries if len(entries) <= max_lines else entries[:max(1, max_lines - 1)]
                    row.append({
                        "num": d.day,
                        "in_month": d.month == today.month,
                        "is_today": d == today,
                        "entries": shown,
                        "more": len(entries) - len(shown),
                    })
                grid.append(row)
            params.update({
                "title": custom_title or f"{labels['months'][today.month - 1]} {today.year}",
                "weekday_names": [labels["weekdays_short"][(week_start + i) % 7] for i in range(7)],
                "weeks": grid,
                "overdue_count": len(overdue),
            })
            html_file = "month.html"
        else:
            sections = []
            if overdue:
                sections.append({"label": labels["overdue"], "cls": "overdue", "entries": overdue})
            d = first_day
            while d <= last_day:
                entries = by_day.get(d, [])
                if entries or d == today:
                    sections.append({
                        "label": labels["today"] if view == "today" else self._day_label(d, today, labels),
                        "cls": "today" if d == today else "",
                        "entries": entries,
                    })
                d += timedelta(days=1)
            if show_undated and undated:
                sections.append({"label": labels["undated"], "cls": "undated", "entries": undated})
            params.update({
                "title": custom_title or labels["weekdays"][today.weekday()],
                "subtitle": f"{today.day} {labels['months_gen'][today.month - 1]}",
                "sections": sections,
                "columns": 1 if view == "today" or dimensions[0] < dimensions[1] * 1.2 else 2,
                # The header already carries the date; a lone "Today" heading is noise.
                "show_heads": not (view == "today" and len(sections) == 1),
            })
            html_file = "agenda.html"

        image = self.render_image(dimensions, html_file, "caldav.css", params)
        if not image:
            raise RuntimeError("Failed to render the calendar, please check logs.")
        return image

    # -- data loading ---------------------------------------------------------
    def _build_session(self, settings, device_config):
        session = requests.Session()
        session.headers["User-Agent"] = "InkyPi-CalDAV/1.0"
        session.verify = _flag(settings, "verifyTls", True)

        auth_type = settings.get("authType") or "basic"
        if auth_type == "none":
            return session

        secret = (settings.get("token") or "").strip()
        if not secret:
            env_key = (settings.get("tokenEnvKey") or "").strip() or DEFAULT_ENV_KEY
            loader = getattr(device_config, "load_env_key", None)
            secret = (loader(env_key) if loader else os.getenv(env_key)) or ""
            secret = secret.strip()
            if not secret:
                raise RuntimeError(f"CalDAV token is not set: fill the Token field or add {env_key} to .env")

        if auth_type == "bearer":
            session.headers["Authorization"] = f"Bearer {secret}"
        else:
            username = (settings.get("username") or "").strip()
            if not username:
                raise RuntimeError("Username is required for Basic authorization.")
            session.auth = HTTPBasicAuth(username.encode("utf-8"), secret.encode("utf-8"))
        return session

    def _load_items(self, settings, device_config, url, start, end, tz, show_events, show_tasks):
        session = self._build_session(settings, device_config)
        client = CalDavClient(url, session)
        try:
            calendars, ics_text = client.discover()

            if ics_text is not None:
                sources = [("", [ics_text] if show_events else [], [ics_text] if show_tasks else [])]
            else:
                wanted = [n.strip().lower() for n in (settings.get("calendars") or "").split(",") if n.strip()]
                if wanted:
                    calendars = [c for c in calendars
                                 if c["name"].lower() in wanted or c["slug"].lower() in wanted]
                    if not calendars:
                        raise RuntimeError("None of the listed calendars were found on the server.")
                sources, errors = [], []
                for cal in calendars:
                    try:
                        events, todos = client.fetch(cal, start, end, show_events, show_tasks)
                        sources.append((cal["name"], events, todos))
                    except CalDavError as e:
                        logger.warning("%s", e)
                        errors.append(str(e))
                if errors and not sources:
                    raise RuntimeError(errors[0])
        except CalDavError as e:
            raise RuntimeError(str(e)) from e
        finally:
            session.close()

        items, seen = [], set()
        for name, event_chunks, todo_chunks in sources:
            parsed = []
            for chunk in event_chunks:
                try:
                    parsed += parse_events(chunk, start, end, tz, name)
                except Exception as e:  # one broken resource must not blank the screen
                    logger.warning("Skipping unparsable event data from %s: %s", name, e)
            for chunk in todo_chunks:
                try:
                    parsed += parse_todos(chunk, tz, name)
                except Exception as e:
                    logger.warning("Skipping unparsable task data from %s: %s", name, e)
            for item in parsed:
                key = (item["kind"], item["uid"] or item["title"], item["start"])
                if key not in seen:
                    seen.add(key)
                    items.append(item)
        return items

    # -- view model -----------------------------------------------------------
    @staticmethod
    def _day_label(d, today, labels):
        text = f"{labels['weekdays_short'][d.weekday()]}, {d.day} {labels['months_gen'][d.month - 1]}"
        if d == today:
            return f"{labels['today']} · {text}"
        if d == today + timedelta(days=1):
            return f"{labels['tomorrow']} · {text}"
        return text

    @staticmethod
    def _bucket(items, first_day, last_day, today, fmt_time, labels):
        """Splits items into per-day entries, overdue tasks and undated tasks."""
        by_day, overdue, undated = {}, [], []

        def entry(item, time_text, sort_key):
            return {"kind": item["kind"], "title": item["title"], "time": time_text,
                    "calendar": item["calendar"], "_sort": sort_key}

        for item in items:
            start = item["start"]
            if item["kind"] == "task":
                if start is None:
                    undated.append(entry(item, "", (0, item["title"].lower())))
                    continue
                time_text = "" if item["all_day"] else fmt_time(start)
                if start.date() < today:
                    stamp = f"{start.day} {labels['months_gen'][start.month - 1]}"
                    overdue.append(entry(item, stamp, (start, item["title"].lower())))
                elif first_day <= start.date() <= last_day:
                    key = (1, time.min, item["title"].lower()) if item["all_day"] else \
                          (2, start.timetz().replace(tzinfo=None), item["title"].lower())
                    by_day.setdefault(start.date(), []).append(entry(item, time_text, key))
                continue

            end = item["end"]
            last = (end - timedelta(microseconds=1)).date() if end > start else start.date()
            d = max(start.date(), first_day)
            while d <= min(last, last_day):
                if item["all_day"]:
                    time_text, key = "", (0, time.min, item["title"].lower())
                elif d == start.date():
                    time_text = fmt_time(start)
                    key = (2, start.timetz().replace(tzinfo=None), item["title"].lower())
                else:  # continuation of a multi-day timed event
                    time_text, key = "→", (0, time.min, item["title"].lower())
                by_day.setdefault(d, []).append(entry(item, time_text, key))
                d += timedelta(days=1)

        for entries in list(by_day.values()) + [overdue, undated]:
            entries.sort(key=lambda e: e["_sort"])
        return by_day, overdue, undated
