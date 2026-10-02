"""The second opinion for BST Radio's stream check, from the owner's computer in Bulgaria, once a day.

check_streams.py runs on GitHub in a US data centre, which some servers refuse (on 2026-10-02, 39 of the 282 streams
that had failed there three days played from Bulgaria: Corus' leanstream, old Shoutcast servers, yesstreaming...).
A stream refused there would be left out of the catalogue and never come back, so the catalogue leaves out only the
streams that fail here too, on an ordinary connection (the owner's decision, 2026-10-02).

This script downloads the check's record, plays every stream that has failed there on CANDIDATE_DAYS days or more,
the same way the check does (ffmpeg, then curl; as a player and then as a browser), and publishes the ones that
failed here too as confirmed-dead.txt in the "bst-radio-health" release, its first line saying when. Nothing opens a
window and nothing is heard. The catalogue build warns the owner by e-mail when the file grows old (this computer
off, reinstalled, or the task gone); install_confirmation.ps1 sets the task up again.

    pythonw confirm_dead.py [--days N] [--dry-run]

Needs Python, ffmpeg (winget install Gyan.FFmpeg), curl (in Windows) and the GitHub CLI, logged in.
"""

import argparse
import datetime
import gzip
import json
import os
import shutil
import subprocess
import sys
import tempfile
import traceback
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import check_streams as check  # noqa: E402

REPOSITORY = "stefantsvyatkov/AppDistributions"
TAG = "bst-radio-health"
# Two days before the catalogue's seven, so the confirmation is there when a stream reaches them.
CANDIDATE_DAYS = check.DEAD_DAYS - 2
LOG = os.path.join(tempfile.gettempdir(), "bst-radio-confirmation.log")


def say(text):
    with open(LOG, "a", encoding="utf-8") as file:
        file.write(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {text}\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=CANDIDATE_DAYS)
    parser.add_argument("--dry-run", action="store_true", help="write the file but do not publish it")
    options = parser.parse_args()

    # Through the GitHub CLI, as the upload: a plain download of the release file was cut off here (2026-10-02).
    gh = shutil.which("gh") or r"C:\Program Files\GitHub CLI\gh.exe"
    folder = tempfile.mkdtemp(prefix="bst-radio-confirmation-")
    subprocess.run([gh, "release", "download", TAG, "--repo", REPOSITORY, "--pattern", "stream-health.json.gz",
                    "--dir", folder, "--clobber"], check=True, capture_output=True, creationflags=check.NO_WINDOW)
    with gzip.open(os.path.join(folder, "stream-health.json.gz"), "rt", encoding="utf-8") as file:
        record = json.load(file)["streams"]
    shutil.rmtree(folder, ignore_errors=True)
    candidates = sorted(address for address, entry in record.items() if entry.get("days", 0) >= options.days)

    gentle = check.HostLimit(2)

    def both(address):
        with gentle.of(address):
            state, _ = check.play(address)
            if state == "failed":
                state, _ = check.play(address, check.BROWSER_AGENT)
            return address, state

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(both, candidates))
    dead = [address for address, state in results if state == "failed"]

    path = os.path.join(tempfile.gettempdir(), "confirmed-dead.txt")
    checked = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    with open(path, "w", encoding="utf-8", newline="\n") as file:
        file.write(f"# Checked from Bulgaria {checked}: {len(dead)} of {len(candidates)} failed here too.\n")
        file.writelines(address + "\n" for address in dead)
    say(f"{len(dead)} of {len(candidates)} streams failed here too")

    if not options.dry_run:
        subprocess.run([gh, "release", "upload", TAG, path, "--repo", REPOSITORY, "--clobber"],
                       check=True, capture_output=True, creationflags=check.NO_WINDOW)
        say("published")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:  # the task has no window: the log is where a failure can be read
        said = getattr(error, "stderr", None)
        say("failed:\n" + traceback.format_exc() + (said.decode("utf-8", "replace") if isinstance(said, bytes) else ""))
        sys.exit(1)
