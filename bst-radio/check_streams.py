"""Once a day: does every stream of Radio Browser play?

BST Radio's catalogue (built in a private repository) leaves out a stream that has not played on five days in a
row. This script finds out: it downloads Radio Browser's list of stations, plays every stream address for one
second with ffmpeg (as a player would, so HLS, playlists, redirects and old Shoutcast servers all count) and keeps
a record of the days each stream failed. Only the stream addresses are used, which Radio Browser publishes anyway.

A stream is
  ok       when ffmpeg decoded a second of its sound;
  blocked  when its server answered 403 or 451: usually a station that plays only in its own country (this
           check runs in a data centre abroad), so it is never counted as failing;
  failed   otherwise (no answer, 404, a web page instead of sound, nothing decodable...).

stream-health.json.gz holds, per address: the last day it played ("ok"), and while it keeps failing the first
day of the run of failures ("since"), how many days of it ("days") and the last reason ("why"). A success ends
the run. dead-streams.txt lists the addresses at five days or more, by country, for a person to look at. Both
are published in this repository's "bst-radio-health" release (.github/workflows/bst-radio-stream-check.yml).

    python3 check_streams.py [--previous stream-health.json.gz] [--out stream-health.json.gz]
                             [--report dead-streams.txt] [--jobs 64] [--per-host 16] [--limit N]
"""

import argparse
import datetime
import gzip
import json
import random
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

USER_AGENT = "BSTRadio-StreamCheck/1.0 (+https://github.com/stefantsvyatkov/AppDistributions)"
SERVERS = ["de1", "de2", "nl1", "at1", "fi1", "fr1"]
DEAD_DAYS = 5


def fetch(url, timeout):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = response.read()
        return gzip.decompress(data) if response.headers.get("Content-Encoding") == "gzip" else data


def radio_browser():
    """The stations the catalogue is made of: Radio Browser's list without the ones it marks broken, fetched in
    pages of 5000 as the catalogue builder fetches it (the whole list at once is refused)."""
    try:
        servers = sorted({server["name"] for server in json.loads(fetch("https://all.api.radio-browser.info/json/servers", 30))})
    except Exception as error:
        print(f"Server list: {error}", file=sys.stderr)
        servers = []
    servers = servers or [f"{name}.api.radio-browser.info" for name in SERVERS]

    stations, offset, page = [], 0, 5000
    while True:
        for attempt in range(6):
            server = servers[attempt % len(servers)]
            url = f"https://{server}/json/stations/search?hidebroken=true&order=name&offset={offset}&limit={page}"
            try:
                batch = json.loads(fetch(url, 120))
                break
            except Exception as error:  # another server, or the same one a little later
                print(f"{server} offset {offset}: {error}", file=sys.stderr)
                time.sleep(5 * (attempt + 1))
        else:
            raise SystemExit("Radio Browser did not answer.")
        stations.extend(batch)
        if len(batch) < page:
            return stations
        offset += page


def first_in_pls(address):
    """A .pls playlist is followed to its first stream: ffmpeg does not read that format."""
    request = urllib.request.Request(address, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=10) as response:
        text = response.read(65536).decode("utf-8", "replace")
    for line in text.splitlines():
        key, _, value = line.partition("=")
        if key.strip().lower().startswith("file") and value.strip():
            return value.strip()
    return None


def play(address):
    """ok, blocked or failed, and why."""
    target = address
    try:
        if urllib.parse.urlparse(address).path.lower().endswith(".pls"):
            target = first_in_pls(address) or address
    except Exception as error:
        return "failed", f"playlist: {error}"[:120]
    command = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-rw_timeout", "10000000",
               "-user_agent", "VLC/3.0.20 LibVLC/3.0.20", "-i", target, "-t", "1", "-vn", "-f", "null", "-"]
    try:
        result = subprocess.run(command, capture_output=True, timeout=30)
    except subprocess.TimeoutExpired:
        return "failed", "no sound within 30 s"
    if result.returncode == 0:
        return "ok", ""
    lines = [line for line in result.stderr.decode("utf-8", "replace").splitlines() if line.strip()]
    why = (lines[-1] if lines else f"ffmpeg exit code {result.returncode}").strip()
    if any(code in why for code in ("403 Forbidden", "451 ", "Server returned 403", "Server returned 451")) or \
            any("403 Forbidden" in line or "451 Unavailable" in line for line in lines):
        return "blocked", why[:120]
    return "failed", why[:120]


class HostLimit:
    """At most so many streams of one server at a time: thousands of stations share a few hosting services."""

    def __init__(self, limit):
        self._limit, self._lock, self._semaphores = limit, threading.Lock(), {}

    def of(self, address):
        host = (urllib.parse.urlparse(address).hostname or "").lower()
        with self._lock:
            if host not in self._semaphores:
                self._semaphores[host] = threading.Semaphore(self._limit)
            return self._semaphores[host]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--previous")
    parser.add_argument("--out", default="stream-health.json.gz")
    parser.add_argument("--report", default="dead-streams.txt")
    parser.add_argument("--jobs", type=int, default=64)
    parser.add_argument("--per-host", type=int, default=16)
    parser.add_argument("--limit", type=int, default=0, help="check only so many addresses (a trial)")
    options = parser.parse_args()

    today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    previous = {}
    if options.previous:
        try:
            with gzip.open(options.previous, "rt", encoding="utf-8") as file:
                previous = json.load(file).get("streams", {})
        except FileNotFoundError:
            print("No earlier record; a new one is started.", file=sys.stderr)

    # Every address a player would open (Radio Browser's resolved one, else the given one), once, with a station
    # of it for the report.
    stations = {}
    for entry in radio_browser():
        address = (entry.get("url_resolved") or entry.get("url") or "").strip()
        if address.lower().startswith(("http://", "https://")) and address not in stations:
            stations[address] = (entry.get("countrycode") or "--", (entry.get("name") or "").strip())
    addresses = list(stations)
    random.shuffle(addresses)  # the hosting services' streams spread over the whole run
    if options.limit:
        addresses = addresses[:options.limit]

    limit = HostLimit(options.per_host)

    def check(address):
        with limit.of(address):
            return address, play(address)

    began, results = time.time(), {}
    with ThreadPoolExecutor(max_workers=options.jobs) as pool:
        for done, (address, outcome) in enumerate(pool.map(check, addresses), 1):
            results[address] = outcome
            if done % 2000 == 0:
                print(f"{done} of {len(addresses)} in {time.time() - began:.0f} s", file=sys.stderr, flush=True)

    streams = {}
    for address in addresses:
        state, why = results[address]
        record = dict(previous.get(address, {}))
        if state == "ok" or state == "blocked":
            record.pop("since", None), record.pop("days", None), record.pop("why", None)
            if state == "ok":
                record["ok"] = today
                record.pop("blocked", None)
            else:
                record["blocked"] = today
        else:
            if "since" not in record:
                record["since"], record["days"] = today, 1
            elif record.get("last") != today:
                record["days"] = record.get("days", 0) + 1
            record["why"] = why
        record["last"] = today
        streams[address] = record

    with gzip.open(options.out, "wt", encoding="utf-8") as file:
        json.dump({"checkedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
                   "deadDays": DEAD_DAYS, "streams": streams}, file, separators=(",", ":"))

    dead = sorted((stations[address][0], stations[address][1], address, record)
                  for address, record in streams.items() if record.get("days", 0) >= DEAD_DAYS)
    with open(options.report, "w", encoding="utf-8") as file:
        # Never empty: GitHub refuses to upload an empty file.
        file.write(f"# Streams that have not played on {DEAD_DAYS} days in a row, checked {today}: {len(dead)}.\n")
        file.write("# Country, station, address, days, last reason.\n")
        for country, name, address, record in dead:
            file.write(f"{country}\t{name}\t{address}\t{record['days']} days since {record['since']}\t{record['why']}\n")

    counts = {"ok": 0, "blocked": 0, "failed": 0}
    for state, _ in results.values():
        counts[state] += 1
    print(f"## Stream check {today}\n")
    print(f"{len(addresses)} stream addresses in {(time.time() - began) / 60:.0f} minutes: {counts['ok']} play, "
          f"{counts['blocked']} blocked abroad, {counts['failed']} failed today; "
          f"{len(dead)} have failed on {DEAD_DAYS} days or more.")


main()
