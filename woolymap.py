#!/usr/bin/env -S uv run --env-file .env --script

# /// script
# requires-python = ">=3.8"
# dependencies = [
#     "bs4>=0.0.2",
#     "jinja2>=3.1.6",
#     "requests>=2.32.4",
# ]
# ///

import getpass
import logging
import os
import statistics
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import bs4
import requests
from jinja2 import Environment, FileSystemLoader
from requests.adapters import HTTPAdapter, Retry

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

LONDON = ZoneInfo("Europe/London")

ROOT = Path(__file__).resolve().parent
TEMPLATE_DIR = ROOT / "templates"
CONTENT_DIR = ROOT / "content"
TEMPLATE_FILE = "index.html"

def get_env_url(name: str) -> str:
    value = os.getenv(name)
    if value:
        return value
    raise RuntimeError(f"Required environment variable {name} is not set")


def format_env_url(value: str) -> str:
    for env_name in [
        "BW_ARRIVALS_DEPARTURES_URL",
        "DVL_ARRIVALS_DEPARTURES_URL",
        "N2S_TIMETABLE_URL",
        "S2N_TIMETABLE_URL",
        "JAMCAM_N_URL",
        "JAMCAM_S_URL",
    ]:
        if os.getenv(env_name) == value:
            return env_name
    return value


FERRY_URLS = (
    get_env_url("BW_ARRIVALS_DEPARTURES_URL"),
    get_env_url("DVL_ARRIVALS_DEPARTURES_URL"),
)

TFL_TIMETABLE_URLS = (
    {
        "direction": "N2S",
        "url": get_env_url("N2S_TIMETABLE_URL"),
    },
    {
        "direction": "S2N",
        "url": get_env_url("S2N_TIMETABLE_URL"),
    },
)

JAMCAM_URLS = (
    {"cam": "N", "url": get_env_url("JAMCAM_N_URL")},
    {"cam": "S", "url": get_env_url("JAMCAM_S_URL")},
)


def get_session(base_url: str) -> requests.Session:
    session = requests.Session()
    retries = Retry(total=5, backoff_factor=1, status_forcelist=[502, 503, 504])
    session.mount("https://", HTTPAdapter(max_retries=retries))
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Ubuntu Chromium/34.0.1847.116 Chrome/34.0.1847.116 Safari/537.36",
            "Host": requests.compat.urlparse(base_url).netloc,
        }
    )
    return session


def round_time(dt: datetime | None = None, date_delta: timedelta = timedelta(minutes=1)) -> datetime:
    if dt is None:
        dt = datetime.now(LONDON)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LONDON)

    base = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    seconds_since_midnight = int((dt - base).total_seconds())
    round_to = int(date_delta.total_seconds())
    rounding = ((seconds_since_midnight + round_to / 2) // round_to) * round_to
    return dt + timedelta(seconds=rounding - seconds_since_midnight)


def take_second(elem: list[Any]) -> datetime:
    return elem[1]


def get_schedule_index() -> int:
    logger.info("Checking bank holiday timetable for %s", date.today())
    try:
        session = get_session("https://www.gov.uk")
        response = session.get("https://www.gov.uk/bank-holidays.json", timeout=10)
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError, KeyError) as exc:
        logger.warning("Bank holiday lookup failed: %s", exc)
        return 0

    if not isinstance(data, dict):
        logger.error("Unexpected bank holiday payload type: %s", type(data).__name__)
        return 0

    events = data.get("england-and-wales", {}).get("events", [])
    if not isinstance(events, list):
        logger.error("Missing or malformed 'events' list in bank holiday JSON")
        return 0

    today = date.today()
    for event in events:
        if not isinstance(event, dict) or "date" not in event:
            logger.warning("Skipping malformed holiday entry: %r", event)
            continue
        try:
            event_date = date.fromisoformat(event["date"])
        except ValueError:
            logger.warning("Skipping invalid holiday date: %r", event.get("date"))
            continue
        if event_date == today:
            logger.info("Today is a public holiday; using holiday timetable")
            return 1
        if event_date > today:
            break

    weekday = datetime.today().isoweekday()
    if weekday <= 5:
        logger.info("Using weekday timetable")
        return 0
    if weekday == 6:
        logger.info("Using Saturday timetable")
        return 1
    logger.info("Using Sunday timetable")
    return 2


def get_time_as_datetime(value: time) -> datetime:
    return datetime.combine(date.today(), value, tzinfo=LONDON)


def format_datetime(value: datetime | str) -> str:
    if isinstance(value, str):
        return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=LONDON)
    return value.astimezone(LONDON).strftime("%H:%M")


def fetch_ferry_events() -> tuple[list[list[Any]], list[list[Any]]]:
    arrivals: list[list[Any]] = []
    departures: list[list[Any]] = []

    for ferry_url in FERRY_URLS:
        try:
            session = get_session(ferry_url)
            response = session.get(ferry_url, timeout=10)
            response.raise_for_status()
        except requests.RequestException as exc:
            logger.warning("Ferry page fetch failed for %s: %s", format_env_url(ferry_url), exc)
            continue

        logger.info("Fetched ferry page: %s", format_env_url(ferry_url))
        soup = bs4.BeautifulSoup(response.text, "html.parser")
        tbody = soup.find("tbody")
        if tbody is None:
            logger.error("No tbody found in ferry page %s", format_env_url(ferry_url))
            continue

        rows = tbody.find_all("tr")
        logger.info("Parsing %d ferry rows from %s", len(rows), format_env_url(ferry_url))
        today_events = 0

        for row in rows:
            cells = row.find_all("td")
            if len(cells) < 5:
                logger.debug("Skipping ferry row with %d cells: %s", len(cells), row)
                continue

            try:
                row_date_text = cells[2].get_text(" ", strip=True).split()[0]
                row_date = date.fromisoformat(row_date_text)
            except (IndexError, ValueError):
                logger.warning("Skipping ferry row with invalid date text: %s", row)
                continue

            if row_date != date.today():
                continue

            today_events += 1
            try:
                direction = cells[1].get_text(" ", strip=True)
                if not direction:
                    raise ValueError("missing direction")

                time_element = cells[2].find("b")
                if time_element is None:
                    raise ValueError("missing time element")
                time_text = time_element.get_text(" ", strip=True)
                departure_time = get_time_as_datetime(time.fromisoformat(time_text))

                port = cells[3].get_text(" ", strip=True)
                if not port:
                    raise ValueError("missing port")
                port = port.replace("WOOLWICH", "South Port").replace("SILVERTOWN", "North Port")

                vessel_link = cells[4].find("a")
                if vessel_link is None:
                    raise ValueError("missing vessel link")
                vessel_name = vessel_link.get_text(" ", strip=True)
                vessel = "BW" if vessel_name == "BEN WOOLLACOTT" else "DVL"
            except (AttributeError, IndexError, ValueError) as exc:
                logger.warning("Skipping malformed ferry row from %s: %s (%s)", format_env_url(ferry_url), exc, row)
                continue

            entry = [direction, departure_time, port, vessel]
            if direction == "Departure":
                departures.append(entry)
            else:
                arrivals.append(entry)

        logger.info("Found %d today's ferry events from %s", today_events, format_env_url(ferry_url))

    return arrivals, departures


def build_trips(arrivals: list[list[Any]], departures: list[list[Any]]) -> tuple[list[list[Any]], list[list[Any]], list[list[Any]]]:
    sorted_arrivals = sorted(arrivals, key=take_second)
    sorted_departures = sorted(departures, key=take_second)

    trips: list[list[Any]] = []
    north_trips: list[list[Any]] = []
    south_trips: list[list[Any]] = []

    for departure in sorted_departures:
        from_port = departure[2]
        vessel = departure[3]
        to_port = "North Port" if from_port == "South Port" else "South Port"
        to_time: datetime | str = "In Transit"

        for arrival in sorted_arrivals:
            if arrival[1] >= departure[1] and arrival[2] == to_port and arrival[3] == vessel:
                to_time = arrival[1]
                break

        trip = [from_port, departure[1], to_port, to_time, vessel]
        trips.append(trip)
        if not isinstance(to_time, datetime):
            logger.info("No matching arrival found for %s departure at %s", from_port, format_datetime(departure[1]))
        if from_port == "South Port":
            south_trips.append([from_port, format_datetime(departure[1]), to_port, format_datetime(to_time) if isinstance(to_time, datetime) else to_time, vessel])
        else:
            north_trips.append([from_port, format_datetime(departure[1]), to_port, format_datetime(to_time) if isinstance(to_time, datetime) else to_time, vessel])

    return trips, north_trips, south_trips


def build_predictions(trips: list[list[Any]]) -> tuple[list[Any], list[Any], datetime]:
    if not trips:
        return ["North Port", "TBC", "South Port", "TBC", "NA"], ["South Port", "TBC", "North Port", "TBC", "NA"], datetime.now(LONDON) + timedelta(minutes=120)

    reversed_trips = list(reversed(trips))
    last_from: list[list[Any] | None] = [None, None]

    for trip in reversed_trips:
        if trip[0] == "South Port" and last_from[0] is None:
            last_from[0] = trip
        elif trip[0] == "North Port" and last_from[1] is None:
            last_from[1] = trip

        if last_from[0] is not None and last_from[1] is not None:
            break

    north_prediction = ["North Port", "TBC", "South Port", "TBC", "NA"]
    south_prediction = ["South Port", "TBC", "North Port", "TBC", "NA"]
    last_predicted_arrival = datetime.now(LONDON) + timedelta(minutes=120)

    for last_trip in last_from:
        if last_trip is None:
            continue

        same_port_history = [trip for trip in trips if trip[0] == last_trip[0] and isinstance(trip[3], datetime)]
        if len(same_port_history) < 2:
            continue

        docked_samples: list[float] = []
        travel_samples: list[float] = []
        for current_trip, previous_trip in zip(same_port_history, same_port_history[1:]):
            docked_samples.append((current_trip[1] - previous_trip[1]).total_seconds())
            travel_samples.append((current_trip[3] - current_trip[1]).total_seconds())

        avg_docked = int(statistics.fmean(docked_samples[:5])) if docked_samples else 0
        avg_travel = int(statistics.fmean(travel_samples[:5])) if travel_samples else 0

        next_departure = last_trip[1] + timedelta(seconds=avg_docked)
        next_arrival = last_trip[1] + timedelta(seconds=avg_docked + avg_travel)
        prediction = [last_trip[0], format_datetime(next_departure), last_trip[2], format_datetime(next_arrival), last_trip[4]]

        if last_trip[0] == "North Port":
            north_prediction = prediction
        else:
            south_prediction = prediction

        if next_arrival > last_predicted_arrival:
            last_predicted_arrival = next_arrival

    return north_prediction, south_prediction, last_predicted_arrival


def fetch_tfl_timetables(trips: list[list[Any]], last_predicted_arrival: datetime) -> tuple[list[list[Any]], list[list[Any]], list[list[Any]]]:
    timetables: list[list[Any]] = []
    north_timetables: list[list[Any]] = []
    south_timetables: list[list[Any]] = []
    schedule_index = get_schedule_index()

    for timetable in TFL_TIMETABLE_URLS:
        try:
            session = get_session(timetable["url"])
            response = session.get(timetable["url"], timeout=20)
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            logger.warning("TfL timetable fetch failed for %s: %s", format_env_url(timetable["url"]), exc)
            continue

        if not isinstance(payload, dict):
            logger.error("Unexpected TfL timetable payload type for %s: %s", format_env_url(timetable["url"]), type(payload).__name__)
            continue

        try:
            routes = payload["timetable"]["routes"]
            schedules = routes[0]["schedules"]
            known_journeys = schedules[schedule_index]["knownJourneys"]
        except (KeyError, IndexError, TypeError) as exc:
            logger.error("Unexpected TfL timetable structure for %s: %s", format_env_url(timetable["url"]), exc)
            continue

        if not isinstance(known_journeys, list):
            logger.error("Expected 'knownJourneys' list in TfL payload for %s", format_env_url(timetable["url"]))
            continue

        logger.info("Fetched %d TfL journeys for %s", len(known_journeys), timetable["direction"])
        from_port = "North Port" if timetable["direction"] == "N2S" else "South Port"
        to_port = "South Port" if timetable["direction"] == "N2S" else "North Port"

        for journey in known_journeys:
            if not isinstance(journey, dict):
                logger.warning("Skipping malformed TfL journey entry: %r", journey)
                continue
            try:
                hour = int(journey["hour"])
                minute = int(journey["minute"])
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning("Skipping TfL journey with invalid time fields: %s (%r)", exc, journey)
                continue

            journey_time = datetime.combine(date.today(), time(hour, minute), tzinfo=LONDON)
            if trips and journey_time >= trips[0][1] and journey_time <= last_predicted_arrival:
                entry = [from_port, format_datetime(journey_time), to_port, format_datetime(journey_time + timedelta(minutes=5)), "NOT KNOWN"]
                timetables.append(entry)
                if from_port == "North Port":
                    north_timetables.append(entry)
                else:
                    south_timetables.append(entry)

    sorted_timetables = sorted(timetables, key=take_second, reverse=True)
    sorted_n_timetables = sorted(north_timetables, key=take_second, reverse=True)
    sorted_s_timetables = sorted(south_timetables, key=take_second, reverse=True)

    return sorted_timetables[:5], sorted_n_timetables[:5], sorted_s_timetables[:5]


def fetch_jamcams() -> tuple[dict[str, Any], dict[str, Any]]:
    cam_data: tuple[dict[str, Any], dict[str, Any]] = ({"cam": "N", "video": "", "time": ""}, {"cam": "S", "video": "", "time": ""})

    for idx, jamcam in enumerate(cam_data):
        try:
            session = get_session(JAMCAM_URLS[idx]["url"])
            response = session.get(JAMCAM_URLS[idx]["url"], timeout=10)
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            logger.warning("JamCam fetch failed for %s: %s", format_env_url(JAMCAM_URLS[idx]["url"]), exc)
            jamcam["video"] = "https://example.com/video_not_found.mp4"
            jamcam["time"] = format_datetime(datetime.now(LONDON))
            continue

        if not isinstance(payload, dict):
            logger.error("Unexpected JamCam payload type for %s: %s", format_env_url(JAMCAM_URLS[idx]["url"]), type(payload).__name__)
            jamcam["video"] = "https://example.com/video_not_found.mp4"
            jamcam["time"] = format_datetime(datetime.now(LONDON))
            continue

        props = payload.get("additionalProperties", [])
        if not isinstance(props, list):
            logger.error("JamCam metadata for %s is not a list", format_env_url(JAMCAM_URLS[idx]["url"]))
            jamcam["video"] = "https://example.com/video_not_found.mp4"
            jamcam["time"] = format_datetime(datetime.now(LONDON))
            continue

        for prop in props:
            if not isinstance(prop, dict):
                logger.warning("Skipping malformed jamcam property: %r", prop)
                continue
            if prop.get("key") == "videoUrl":
                jamcam["video"] = prop.get("value", "") + f"?nocache={datetime.now()}"
                try:
                    jamcam_dt = datetime.strptime(prop["modified"], "%Y-%m-%dT%H:%M:%S.%fZ")
                    jamcam["time"] = format_datetime(jamcam_dt)
                except (KeyError, ValueError):
                    logger.warning("JamCam timestamp missing or invalid for %s", format_env_url(JAMCAM_URLS[idx]["url"]))
                    jamcam["time"] = format_datetime(datetime.now(LONDON))
                break
        else:
            logger.warning("JamCam video URL not found for %s", format_env_url(JAMCAM_URLS[idx]["url"]))
            jamcam["video"] = "https://example.com/video_not_found.mp4"
            jamcam["time"] = format_datetime(datetime.now(LONDON))

    return cam_data[0], cam_data[1]


def render_page(trips: list[list[Any]], north_trips: list[list[Any]], south_trips: list[list[Any]], timetables: list[list[Any]], north_timetables: list[list[Any]], south_timetables: list[list[Any]], north_prediction: list[Any], south_prediction: list[Any], north_cam: dict[str, Any], south_cam: dict[str, Any], updated: datetime) -> None:
    env = Environment(loader=FileSystemLoader(TEMPLATE_DIR))
    template = env.get_template(TEMPLATE_FILE)

    output_path = CONTENT_DIR / "index.html"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    rendered = template.render(
        trips=trips,
        n_trips=north_trips,
        s_trips=south_trips,
        timetables=timetables,
        n_timetables=north_timetables,
        s_timetables=south_timetables,
        n_prediction=north_prediction,
        s_prediction=south_prediction,
        n_cam=north_cam,
        s_cam=south_cam,
        last_updated=updated,
    )
    output_path.write_text(rendered, encoding="utf-8")
    logger.info("Wrote rendered page to %s", output_path)


def main() -> None:
    arrivals, departures = fetch_ferry_events()
    trips, north_trips, south_trips = build_trips(arrivals, departures)

    north_prediction, south_prediction, last_predicted_arrival = build_predictions(trips)
    timetables, north_timetables, south_timetables = fetch_tfl_timetables(trips, last_predicted_arrival)
    north_cam, south_cam = fetch_jamcams()

    trips = [list(trip) for trip in trips]
    for trip in trips:
        trip[1] = format_datetime(trip[1])
        trip[3] = format_datetime(trip[3]) if isinstance(trip[3], datetime) else trip[3]

    north_trips = list(reversed(north_trips[-5:]))
    south_trips = list(reversed(south_trips[-5:]))
    trips = list(reversed(trips[-5:]))

    updated = datetime.now(LONDON)
    render_page(
        trips=trips,
        north_trips=north_trips,
        south_trips=south_trips,
        timetables=timetables,
        north_timetables=north_timetables,
        south_timetables=south_timetables,
        north_prediction=north_prediction,
        south_prediction=south_prediction,
        north_cam=north_cam,
        south_cam=south_cam,
        updated=updated,
    )


if __name__ == "__main__":
    main()
