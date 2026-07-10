#!/usr/bin/env -S uv run --env-file .env --script

# /// script
# requires-python = ">=3.8"
# dependencies = [
#     "bs4>=0.0.2",
#     "jinja2>=3.1.6",
#     "pytz>=2026.2",
#     "requests>=2.32.4",
# ]
# ///

import requests
from requests.adapters import HTTPAdapter, Retry
import bs4
import socket
from datetime import time
from datetime import timedelta
from datetime import datetime
from datetime import date
import json
from jinja2 import Environment
from jinja2 import FileSystemLoader
from sys import argv
import os
from sys import version_info
import pytz
import logging

if version_info < (3, 7, 0):
    # only required for versions of Python < 3.7
    from backports.datetime_fromisoformat import MonkeyPatch

    MonkeyPatch.patch_fromisoformat()

os.environ["TZ"] = "Europe/London"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

ROOT = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_DIR = os.path.join(ROOT, "templates")
CONTENT_DIR = os.path.join(ROOT, "content")
TEMPLATE_FILE = "index.html"
CONTENT_FILE = "index.html"

templateLoader = FileSystemLoader(searchpath=TEMPLATE_DIR)
env = Environment(loader=templateLoader)
template = env.get_template(TEMPLATE_FILE)

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


def round_time(dt=None, dateDelta=timedelta(minutes=1)):
    """Round a datetime object to a multiple of a timedelta
    dt : datetime.datetime object, default now.
    dateDelta : timedelta object, we round to a multiple of this, default 1 minute.
    Author: Thierry Husson 2012 - Use it as you want but don't blame me.
            Stijn Nevens 2014 - Changed to use only datetime objects as variables
    """
    roundTo = dateDelta.total_seconds()

    if dt == None:
        dt = datetime.now()
    seconds = (dt - dt.min).seconds
    # // is a floor division, not a comment on following line:
    rounding = (seconds + roundTo / 2) // roundTo * roundTo
    return dt + timedelta(0, rounding - seconds, -dt.microsecond)


# use second row for sorting
def take_second(elem):
    return elem[1]


# return the schedule, Mon-Fri, Sat and public holidays, Sun
def schedule():
    try:
        session = get_session("https://www.gov.uk")
        response = session.get("https://www.gov.uk/bank-holidays.json", timeout=10)
        response.raise_for_status()
        ph_json = response.json()
    except requests.exceptions.RequestException as e:
        logger.warning("Bank holiday lookup failed: %s", e)

    ph_json = json.loads(response.text)

    # the JSON just lists all dates, rather than breaking them down by year
    for ph in ph_json["england-and-wales"]["events"]:
        phdate = ph["date"]
        phdate = date.fromisoformat(phdate)
        if phdate == date.today():
            # public holiday today, use the right timetable
            return 1
        elif phdate > date.today():
            # if we are in the future, just stop the loop
            break

    wd = datetime.today().isoweekday()
    if wd >= 1 and wd <= 5:
        logger.info("Using weekday timetable")
        return 0
    elif wd == 6:
        logger.info("Using Saturday timetable")
        return 1
    else:
        logger.info("Using Sunday timetable")
        return 2


# convert a time object to a datetime object - for consistency we want to only use datetime
def get_time_as_datetime(time1):
    dt = datetime.strptime(datetime.now().strftime("%Y-%m-%d ") + time1.strftime("%H:%M"), "%Y-%m-%d %H:%M")
    dt = pytz.timezone("Europe/London").localize(dt)
    return dt


def munge_local_time_to_utc(loctime):
    localsample = pytz.timezone("Europe/London").localize(datetime.now())
    if localsample.dst() != None:
        offset = localsample.dst()
        utctime = loctime - offset
    else:
        utctime = loctime
    utctime = pytz.timezone("Europe/London").localize(utctime)
    return utctime


def convert_datetime_to_hm(dt):
    if type(dt) != str:
        # all the times we process are in the London timezone:
        # 1. Remove any tzinfo already defined
        # 2. Localise the datetime object to London timezone
        # 3. Get the current DST offset (if any) and add it to the datetime object
        # 4. Output the new datetime as a string with HH:MM format
        dt = dt.replace(tzinfo=None)
        dt = pytz.timezone("Europe/London").localize(dt)
        if dt.dst() != None:
            offset = dt.dst()
            newDt = dt + offset
        else:
            newDt = dt
        return newDt.strftime("%H:%M")
    else:
        # if it is a string just print directly
        return dt

arrivals = []
departures = []

for ferry in FERRY_URLS:
    try:
        session = get_session(ferry)
        response = session.get(ferry, timeout=10)
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("Ferry page fetch failed for %s: %s", format_env_url(ferry), exc)
        continue
    logger.info("Fetched ferry page: %s", format_env_url(ferry))

    result = bs4.BeautifulSoup(response.text, "html.parser")

    tbody = result.find("tbody")
    if tbody is None:
        logger.error("No tbody found in ferry page %s", format_env_url(ferry))
        continue

    rows = tbody.find_all("tr")
    logger.info("Parsing %d ferry rows from %s", len(rows), format_env_url(ferry))
    today_events = 0

    for row in rows:
        row_html = bs4.BeautifulSoup(str(row), "html.parser")
        # check if the date of the trip is today
        thisdate = row_html.select("tr > td:nth-of-type(3)")[0].text.split()[0]
        thisdate = date.fromisoformat(thisdate)
        if thisdate == date.today():
            # get if its an arrival or departure, the time, port and vessel
            arr_div = row_html.select("tr > td:nth-of-type(2)")[0].string
            thistime = row_html.select("tr > td:nth-of-type(3) > b")[0].string
            thistime = get_time_as_datetime(time.fromisoformat(thistime))
            port = row_html.select("tr > td:nth-of-type(4) > a")[0].text.strip()
            port = port.replace("WOOLWICH", "South Port")
            port = port.replace("SILVERTOWN", "North Port")
            vessel = row_html.select("tr > td:nth-of-type(5) > span > a")[0].string
            if vessel == "BEN WOOLLACOTT":
                vessel = "BW"
            else:
                vessel = "DVL"
            # add it to either the departures or arrival array
            if arr_div == "Departure":
                departures.append([str(arr_div), (thistime), str(port), str(vessel)])
            else:
                arrivals.append([str(arr_div), (thistime), str(port), str(vessel)])
            today_events += 1

    logger.info("Found %d today's ferry events from %s", today_events, format_env_url(ferry))

# sort both arrays by the time
sorted_arrivals = sorted(arrivals, key=take_second)
sorted_departures = sorted(departures, key=take_second)
logger.debug("ARRIVALS:")
logger.debug(sorted_arrivals)
logger.debug("DEPARTURES:")
logger.debug(sorted_departures)

trips = []
n_trips = []
s_trips = []

# for each departure
for departure in sorted_departures:
    from_time = departure[1]
    from_port = departure[2]
    vessel = departure[3]

    # get the opposite port
    if from_port == "South Port":
        to_port = "North Port"
    else:
        to_port = "South Port"
    to_time = "In Transit"

    # check for the first arrival after the departure, to the opposite port and on that vessel
    for arrival in sorted_arrivals:
        if arrival[1] >= from_time and arrival[2] == to_port and arrival[3] == vessel:
            # set the arrival time
            to_time = arrival[1]
            break

    if not isinstance(to_time, datetime):
        logger.info("No matching arrival found for %s departure at %s", from_port, str(departure[1]))

    # add the full departure and arrival to a single line in the array
    trips.append([str(from_port), from_time, str(to_port), to_time, str(vessel)])
    if from_port == "South Port":
        s_trips.append([str(from_port), convert_datetime_to_hm(from_time), str(to_port), convert_datetime_to_hm(to_time), str(vessel)])
    else:
        n_trips.append([str(from_port), convert_datetime_to_hm(from_time), str(to_port), convert_datetime_to_hm(to_time), str(vessel)])

# try to make a simple prediction of the next departures from each port

# create a reversed array of trips
rev_trips = trips[::-1]

# get the last trip from the south and the north port
last_from = [[], []]
for vess_trip in rev_trips:
    if vess_trip[0] == "South Port" and last_from[0] == []:
        last_from[0] = vess_trip
    elif vess_trip[0] == "North Port" and last_from[1] == []:
        last_from[1] = vess_trip

    if last_from[0] != [] and last_from[1] != []:
        break

# take the average time docked and the average time travelling with an average of 5 journeys
trip_history = 0
avg_time_docked = 0
avg_time_travelling = 0

# create prediction arrays
north_prediction = []
south_prediction = []

# we use this for generating timetables that include our predictions
last_predicted_arrival = datetime.now(tz=pytz.timezone("Europe/London"))
logger.debug('Last Predicted Trip:')
logger.debug(last_predicted_arrival)

# do this for the last ferry from south and north terminal
for last in last_from:
    # loop over all past trips, at least 5 times for each ferry and terminal
    for past_trip in rev_trips:
        if trip_history == 5:
            break

        # the last array is empty, just break out of the loop early
        if len(last) == 0:
            break

        # the from port is the same and the ferry isn't in transit
        if past_trip[0] == last[0] and past_trip[3] != "In Transit":
            # loop over the trips again
            for next_trip in rev_trips:
                # make sure this is a previous journey of the same type
                if next_trip[0] == past_trip[0] and next_trip[3] != "In Transit" and next_trip[1] < past_trip[1]:
                    # get the time docked and time travelling
                    docked = (past_trip[1] - next_trip[1]).seconds
                    travelling = (next_trip[3] - next_trip[1]).seconds

                    # if it's the first one just set the average to this value
                    if avg_time_docked == 0:
                        avg_time_docked = docked
                    else:
                        # otherwise create a simple average of this value and the previous one
                        avg_time_docked = (avg_time_docked + docked) / 2

                    # if it's the first one just set the average to this value
                    if avg_time_travelling == 0:
                        avg_time_travelling = travelling
                    else:
                        # otherwise create a simple average of this value and the previous one
                        avg_time_travelling = (avg_time_travelling + travelling) / 2

                    # we break out of the loop at the first opportunity
                    trip_history = trip_history + 1
                    break

    # use the averages to predict when the next ferry will depart and arrive
    avg_time_docked = int(avg_time_docked)
    logger.debug("Average Time Docked: " + str(avg_time_docked))
    avg_time_travelling = int(avg_time_travelling)

    if len(last) == 0:
        north_prediction = ["North Port", "TBC", "South Port", "TBC", "NA"]
        south_prediction = ["South Port", "TBC", "North Port", "TBC", "NA"]
        last_predicted_arrival = round_time() + timedelta(minutes=120)
    else:
        # round the departures and arrivals to the next minute, to fit in with the rest of the timetable
        next_departure = convert_datetime_to_hm(last[1] + timedelta(seconds=avg_time_docked))
        next_arrival = convert_datetime_to_hm(last[1] + timedelta(seconds=avg_time_docked + avg_time_travelling))
        last_predicted_arrival_this = last[1] + timedelta(seconds=avg_time_docked + avg_time_travelling)
        last_predicted_arrival_this = last_predicted_arrival_this.replace(tzinfo=pytz.timezone("Europe/London"))
        last_predicted_arrival = last_predicted_arrival.replace(tzinfo=pytz.timezone("Europe/London"))

        if last_predicted_arrival_this > last_predicted_arrival:
            last_predicted_arrival = last_predicted_arrival_this

        if last[0] == "North Port":
            north_prediction = [last[0], next_departure, last[2], next_arrival, last[4]]
        else:
            south_prediction = [last[0], next_departure, last[2], next_arrival, last[4]]


tt = TFL_TIMETABLE_URLS

# for each timetable, north to south and south to north, load the JSON into an object we can use
for timetable in tt:
    try:
        session = get_session(timetable["url"])
        response = session.get(timetable["url"], timeout=20)
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning("TfL timetable fetch failed for %s: %s", format_env_url(timetable["url"]), exc)
        continue

    timetable["json"] = payload

our_timetable = []
n_timetable = []
s_timetable = []
todays_schedule = schedule()

# get the timetables in both directions
for journ_dir in tt:
    timetable_journey_count = 0
    if journ_dir["direction"] == "N2S":
        from_port = "North Port"
        to_port = "South Port"
    else:
        from_port = "South Port"
        to_port = "North Port"

    # check the timetables for today
    for journey in journ_dir["json"]["timetable"]["routes"][0]["schedules"][todays_schedule]["knownJourneys"]:
        journey_time = datetime.combine(date.today(), time.fromisoformat(journey["hour"].zfill(2) + ":" + journey["minute"].zfill(2)), tzinfo=pytz.timezone("Europe/London"))

        # if the timetable is between the first and last ferry trips captures, add it to the array
        # this part doesn't fucking work at all I guess!
        if len(trips) >= 1:
            first_trip = trips[0][1]

            if journey_time >= (first_trip) and journey_time <= last_predicted_arrival:
                logger.debug('Journey Time:')
                logger.debug(journey_time)
                logger.debug(journey_time.tzinfo)
                logger.debug('First Trip:')
                logger.debug(trips[0][1])
                logger.debug('Last Trip:')
                logger.debug(last_predicted_arrival)
                our_timetable.append([str(from_port), convert_datetime_to_hm(journey_time), str(to_port), convert_datetime_to_hm(journey_time + timedelta(minutes=5)), "NOT KNOWN"])
                if from_port == "North Port":
                    n_timetable.append([str(from_port), convert_datetime_to_hm(journey_time), str(to_port), convert_datetime_to_hm(journey_time + timedelta(minutes=5)), "NOT KNOWN"])
                else:
                    s_timetable.append([str(from_port), convert_datetime_to_hm(journey_time), str(to_port), convert_datetime_to_hm(journey_time + timedelta(minutes=5)), "NOT KNOWN"])
                timetable_journey_count += 1
        else:
            first_trip = round_time() - timedelta(minutes=60)
            last_predicted_arrival = round_time() + timedelta(minutes=120)
    logger.info("Found %d timetabled journeys for %s", timetable_journey_count, journ_dir["direction"])

# sort the timetables and then print them out
sorted_timetables = sorted(our_timetable, key=take_second)
sorted_timetables.reverse()
sorted_n_timetables = sorted(n_timetable, key=take_second)
sorted_n_timetables.reverse()
sorted_s_timetables = sorted(s_timetable, key=take_second)
sorted_s_timetables.reverse()

if len(sorted_timetables) > 5:
    del sorted_timetables[5:]

if len(sorted_n_timetables) > 5:
    del sorted_n_timetables[5:]

if len(sorted_s_timetables) > 5:
    del sorted_s_timetables[5:]
 
sorted_timetables.reverse()
sorted_n_timetables.reverse()
sorted_s_timetables.reverse() 

jmcm = JAMCAM_URLS

# for each cam, north and south, load the JSON into an object we can use
for jamcam in jmcm:
    try:
        session = get_session(jamcam["url"])
        response = session.get(jamcam["url"], timeout=10)
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning("JamCam fetch failed for %s: %s", format_env_url(jamcam["url"]), exc)
        continue
    # just in case the jamcam API is not working...
    if "additionalProperties" in payload:
        for ap in payload["additionalProperties"]:
            if ap["key"] == "videoUrl":
                jamcam["video"] = ap["value"] + "?nocache=" + str(datetime.now())
                jamcam_dt = datetime.strptime(ap["modified"], "%Y-%m-%dT%H:%M:%S.%fZ")
                jamcam["time"] = convert_datetime_to_hm(jamcam_dt)
                logger.info("JamCam URL found for " + jamcam["cam"])
                break
    else:
        jamcam["video"] = "https://example.com/video_not_found.mp4"
        jamcam["time"] = convert_datetime_to_hm(datetime.now())

filename = os.path.join(CONTENT_DIR, CONTENT_FILE)

# get the times in a shorter format
for trip in trips:
    trip[1] = convert_datetime_to_hm(trip[1])
    trip[3] = convert_datetime_to_hm(trip[3])

trips.reverse()
if len(trips) > 5:
    del trips[5:]

n_trips.reverse()
if len(n_trips) > 5:
    del n_trips[5:]

s_trips.reverse()
if len(s_trips) > 5:
    del s_trips[5:]

updated = datetime.now()

with open(filename, "w") as fh:
    fh.write(template.render(
        trips=trips, 
        n_trips=n_trips, 
        s_trips=s_trips, 
        timetables=sorted_timetables, 
        n_timetables=sorted_n_timetables, 
        s_timetables=sorted_s_timetables, 
        n_prediction=north_prediction, 
        s_prediction=south_prediction, 
        n_cam=jmcm[0], 
        s_cam=jmcm[1], 
        last_updated=updated)
    )
logger.info("Wrote rendered page to %s", filename)
