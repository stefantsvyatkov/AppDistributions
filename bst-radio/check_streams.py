"""Twice a day, twelve hours apart: does every stream of Radio Browser play?

BST Radio's catalogue (built in a private repository) leaves out, by itself, a stream that has not played on seven
nights in a row (the owner's decision, 2026-10-02, with the wish that no stream that plays is ever left out). This
script finds out: it downloads Radio Browser's list of stations, plays every stream address for one second with
ffmpeg (as a player would, so HLS, playlists, redirects and old Shoutcast servers all count) and keeps a record of
the days each stream failed. Only the stream addresses are used, which Radio Browser publishes anyway.

A stream is
  ok       when ffmpeg decoded a second of its sound, or opened the stream and only its null output took none
           of it (HLS of some broadcasters), or, after ffmpeg waited 30 s in vain, curl got 16 KB of it that is not
           a web page (old Shoutcast servers keep this ffmpeg waiting);
  blocked  when its server refused (401, 403, or another 4xx but 400 and 404): usually a station that plays
           only in its own country or not to data centres (this check runs in one abroad), so it never fails;
  failed   otherwise (no answer, 404, 5xx, a web page instead of sound, nothing decodable...), twice: every
           failure is played once more at the end of the run, with a browser's name and at most two streams of
           a server at a time, because big hosts (zeno.fm, sharp-stream) stop answering when asked too often.
A day counts as failed only when every run of it failed (a stream that plays at either time is fine that day). A
night on which more than 6% of the streams fail is the check's own trouble (its network, a broken ffmpeg),
not the stations': its failures are not counted.

On 2026-10-01 a sample of the streams that had failed three nights was played again from Bulgaria: of the ones
this check now counts as failing, nine in ten did not play there either; the rest were refused to data centres,
which the second try and the seven nights are for.

stream-health.json.gz holds, per address: the last day it played ("ok"), and while it keeps failing the first
day of the run of failures ("since"), how many days of it ("days") and the last reason ("why"). A success ends
the run. dead-streams.txt lists the addresses at seven days or more, by country. Both are published in this
repository's "bst-radio-health" release (.github/workflows/bst-radio-stream-check.yml).

    python3 check_streams.py [--previous stream-health.json.gz] [--out stream-health.json.gz]
                             [--report dead-streams.txt] [--jobs 64] [--per-host 8] [--limit N]
"""

import argparse
import datetime
import gzip
import json
import os
import random
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

USER_AGENT = "BSTRadio-StreamCheck/1.0 (+https://github.com/stefantsvyatkov/AppDistributions)"
PLAYER_AGENT = "VLC/3.0.20 LibVLC/3.0.20"
BROWSER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
SERVERS = ["de1", "de2", "nl1", "at1", "fi1", "fr1"]
DEAD_DAYS = 7
BAD_NIGHT = 0.06


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


def sends_sound(address, agent):
    """
    A second, plain look at a stream ffmpeg waited for in vain: curl (which also takes the "ICY 200 OK" of old
    Shoutcast servers) reads it for up to 15 s, and 16 KB or more of something that is not a web page is sound.
    """
    with tempfile.TemporaryDirectory() as folder:
        body, head = os.path.join(folder, "body"), os.path.join(folder, "head")
        try:
            subprocess.run(["curl", "-sS", "-L", "--max-time", "15", "-A", agent, "-o", body, "-D", head, address],
                           capture_output=True, timeout=25)
        except subprocess.TimeoutExpired:
            return False
        if not os.path.exists(body) or os.path.getsize(body) < 16384:
            return False
        with open(head, encoding="latin-1") as file:
            last = [block for block in file.read().split("\r\n\r\n") if block.strip()][-1:] or [""]
        return "content-type: text/html" not in last[0].lower()


# ffmpeg's words for a server that refused: 401, 403, or any other 4xx but 400 and 404 ("4XX Client Error, but not
# one of 40{0,1,3,4}": 405, 407, 429, 451...).
REFUSED = ("Server returned 401", "Server returned 403", "Server returned 4XX", "403 Forbidden", "451 Unavailable")


def play(address, agent=PLAYER_AGENT):
    """ok, blocked or failed, and why."""
    target = address
    try:
        if urllib.parse.urlparse(address).path.lower().endswith(".pls"):
            target = first_in_pls(address) or address
    except Exception as error:
        return "failed", f"playlist: {error}"[:120]
    command = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-rw_timeout", "10000000",
               "-user_agent", agent, "-i", target, "-t", "1", "-vn", "-f", "null", "-"]
    try:
        result = subprocess.run(command, capture_output=True, timeout=30)
    except subprocess.TimeoutExpired:
        return ("ok", "") if sends_sound(target, agent) else ("failed", "no sound within 30 s")
    if result.returncode == 0:
        return "ok", ""
    lines = [line for line in result.stderr.decode("utf-8", "replace").splitlines() if line.strip()]
    why = (lines[-1] if lines else f"ffmpeg exit code {result.returncode}").strip()
    if "Error opening output files" in why:
        return "ok", ""  # the input opened: the server sends a stream, only the null output took none of it
    if any(word in line for line in lines for word in REFUSED):
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
    parser.add_argument("--per-host", type=int, default=8)
    parser.add_argument("--limit", type=int, default=0, help="check only so many addresses (a trial)")
    parser.add_argument("--extra-streams", help="BST Radio's own stations' addresses, one a line (extra-streams.txt)")
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
    # BST Radio's own stations that Radio Browser lacks (published by the catalogue build): played the same way.
    if options.extra_streams:
        try:
            with open(options.extra_streams, encoding="utf-8") as file:
                for line in file:
                    address = line.strip()
                    if address.lower().startswith(("http://", "https://")) and address not in stations:
                        stations[address] = ("--", "(BST Radio)")
        except FileNotFoundError:
            print("No list of BST Radio's own stations.", file=sys.stderr)
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

    # Every failure once more, as a browser and gently: a stream fails the night only when both tries fail.
    retry, rescued = [address for address, (state, _) in results.items() if state == "failed"], 0
    gentle = HostLimit(2)

    def again(address):
        with gentle.of(address):
            return address, play(address, BROWSER_AGENT)

    with ThreadPoolExecutor(max_workers=32) as pool:
        for address, outcome in pool.map(again, retry):
            if outcome[0] != "failed":
                results[address], rescued = outcome, rescued + 1

    failed_tonight = sum(1 for state, _ in results.values() if state == "failed")
    bad_night = len(addresses) >= 1000 and failed_tonight > len(addresses) * BAD_NIGHT

    streams = {}
    for address in addresses:
        state, why = results[address]
        record = dict(previous.get(address, {}))
        if state == "failed" and bad_night:
            streams[address] = record  # the check's own trouble: the night does not count
            continue
        if state == "ok" or state == "blocked":
            record.pop("since", None), record.pop("days", None), record.pop("why", None)
            if state == "ok":
                record["ok"] = today
                record.pop("blocked", None), record.pop("refused", None)
            else:
                record["blocked"], record["refused"] = today, why  # what the server said, to see who refuses and how
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
          f"{counts['blocked']} refused abroad, {counts['failed']} failed twice tonight ({rescued} more played at "
          f"the second try); {len(dead)} have failed on {DEAD_DAYS} days or more.")
    if bad_night:
        print(f"\nMore than {BAD_NIGHT:.0%} failed: the check's own trouble, so tonight's failures are not counted.")


if __name__ == "__main__":
    main()
