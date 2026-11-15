#!/usr/bin/env python3
"""Check which Telegram bot usernames appear to have no public t.me page."""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from itertools import permutations
import re
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEFAULT_WORDS = ("yt", "save", "video", "share", "clip", "ytdlp", "clips")
USERNAME_RE = re.compile(r"[a-z][a-z0-9_]{4,31}", re.ASCII)
NOTE = (
    "Free here means no public t.me page; BotFather has the final say "
    "(reserved or recently deleted names can still be refused)."
)


def candidates(words):
    words = list(dict.fromkeys(words))
    stems = words + [first + second for first, second in permutations(words, 2)]
    names = {
        stem + suffix
        for stem in stems
        for suffix in ("bot", "_bot")
        if USERNAME_RE.fullmatch(stem + suffix)
    }
    return sorted(names, key=lambda name: (len(name), name))


def check_name(username):
    for attempt in range(2):
        if attempt:
            time.sleep(2)
        time.sleep(0.3)
        request = Request(
            "https://t.me/" + username, headers={"User-Agent": "Mozilla/5.0"}
        )
        try:
            with urlopen(request, timeout=20) as response:
                html = response.read()
            return "taken" if b"tgme_page_title" in html else "free"
        except (HTTPError, URLError, OSError):
            if attempt:
                return "error"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("words", nargs="*", help="words to use instead of the defaults")
    parser.add_argument("--all", action="store_true", help="also list taken names")
    args = parser.parse_args()
    names = candidates(args.words or DEFAULT_WORDS)
    results = {"free": [], "taken": [], "error": []}
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(check_name, name): name for name in names}
        for future in as_completed(futures):
            results[future.result()].append(futures[future])

    for group in results.values():
        group.sort(key=lambda name: (len(name), name))
    for name in results["free"]:
        print(f"{name} ({len(name)})")
    if args.all:
        for name in results["taken"]:
            print(f"TAKEN {name} ({len(name)})")
    print(
        f"{len(results['free'])} free, {len(results['taken'])} taken, "
        f"{len(results['error'])} errors"
    )
    for name in results["error"]:
        print(f"ERROR {name}")
    print(NOTE)


if __name__ == "__main__":
    main()
