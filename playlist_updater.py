#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Automatic IPTV Channel List Updater
===================================

What it does:
  1. Tests every channel URL in the playlist next to this script.
  2. For dead channels, fetches fresh candidates from iptv-org and tests them.
  3. Replaces only the URL line; channel order, #EXTINF, name, logo, group and
     tvg-id stay exactly the same.
  4. Backs up the old file and writes a CSV report.

Easiest usage:
  Double-click UPDATE-PLAYLIST.bat.

  The script finds the .m3u / .m3u8 file in its own folder. The filename must
  contain the keyword "update". If several matching playlists exist, it asks
  once and remembers the choice in "playlist.txt".

Command line:
  python playlist_updater.py                 # find, scan, repair, save
  python playlist_updater.py --dry-run       # do not write any file
  python playlist_updater.py --scan-only     # scan only
  python playlist_updater.py --playlist X.m3u
  python playlist_updater.py --deep          # also verify the first segment
  python playlist_updater.py --remove-dead   # remove channels with no alternative

Requirement: Python 3.9+ (the requests package is installed automatically on
the first run).
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urljoin, urlsplit, urlunsplit

def _load_requests():
    try:
        import requests as _r
        return _r
    except ImportError:
        print("The requests package is missing; installing it automatically (the first run may take a few minutes)...")
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install", "--user", "requests"])
        except Exception as exc:
            print("\nAutomatic installation failed:", exc)
            print("Install manually with:  python -m pip install requests")
            input("\nPress Enter to close...")
            raise SystemExit(1)
        import requests as _r
        return _r


requests = _load_requests()
try:
    import urllib3

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except Exception:
    pass

# ---------------------------------------------------------------------------
# Ayarlar
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
PLAYLIST_CONFIG = BASE_DIR / "playlist.txt"
# Only playlist files containing this keyword are touched.
# Example: update-channels.m3u, my-update-list.m3u
PLAYLIST_KEYWORD = "update"
STREAMS_API = "https://iptv-org.github.io/api/streams.json"
CHANNELS_API = "https://iptv-org.github.io/api/channels.json"
TR_PLAYLIST = "https://iptv-org.github.io/iptv/countries/tr.m3u"

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Manual aliases for channels listed under a different ID in the source.
# Anahtar: playlist'teki tvg-id'nin "@" oncesi kismi.
ALIASES: Dict[str, List[str]] = {
    # "OrnekTV.tr": ["Ornek.tr"],
}

CACHE_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "iptv-playlist-updater" / "cache"

QUALITY_RE = re.compile(r"\((\d{3,4})[pi]\)", re.IGNORECASE)
ATTR_RE = re.compile(r'([\w-]+)="([^"]*)"')
EXTVLCOPT_RE = re.compile(r"^#EXTVLCOPT:\s*([\w-]+)\s*=\s*(.+)$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Veri modelleri
# ---------------------------------------------------------------------------


@dataclass
class Channel:
    extinf_idx: int
    url_idx: int
    extra_idxs: List[int]
    tvg_id: str
    name: str
    url: str
    referrer: Optional[str] = None
    user_agent: Optional[str] = None
    quality_hint: Optional[int] = None
    referrer_idx: Optional[int] = None
    user_agent_idx: Optional[int] = None
    extinf_raw: str = ""


@dataclass
class ProbeResult:
    ok: bool
    note: str = ""
    status: Optional[int] = None


@dataclass
class Change:
    channel: Channel
    action: str  # OK | REPLACE | REMOVE | DEAD | SKIPPED
    new_url: Optional[str] = None
    new_referrer: Optional[str] = None
    new_user_agent: Optional[str] = None
    note: str = ""
    old_note: str = ""


@dataclass
class SourceData:
    by_channel: Dict[str, List[dict]] = field(default_factory=lambda: defaultdict(list))
    # (country code, normalized name) -> channel ID
    by_name: Dict[Tuple[str, str], str] = field(default_factory=dict)
    stream_count: int = 0
    channel_count: int = 0


# ---------------------------------------------------------------------------
# Yardimcilar
# ---------------------------------------------------------------------------


def strip_eol(line: str) -> str:
    return line.rstrip("\r\n")


def get_eol(line: str) -> str:
    return line[len(strip_eol(line)):]


def normalize_name(text: str) -> str:
    text = text.lower()
    text = re.sub(r"\[[^\]]*\]", " ", text)   # [Not 24/7], [Geo-blocked]
    text = re.sub(r"\([^)]*\)", " ", text)    # (1080p)
    text = re.sub(r"\b(uhd|fhd|hd|sd|4k|hevc)\b", " ", text)
    for a, b in (("ı", "i"), ("ş", "s"), ("ğ", "g"), ("ü", "u"), ("ö", "o"), ("ç", "c")):
        text = text.replace(a, b)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "", text)


def parse_quality(value: Optional[str]) -> Optional[int]:
    if not value:
        return None
    m = re.search(r"(\d{3,4})", str(value))
    return int(m.group(1)) if m else None


def quality_from_name(name: str) -> Optional[int]:
    m = QUALITY_RE.search(name)
    return int(m.group(1)) if m else None


def is_probably_hls(url: str) -> bool:
    u = url.lower()
    if "youtube.com" in u or "youtu.be" in u or "twitch.tv" in u:
        return False
    return True


def host_of(url: str) -> str:
    try:
        return url.split("://", 1)[1].split("/", 1)[0].lower()
    except Exception:
        return url.lower()


class HostThrottle:
    """Ayni sunucuya ardi ardina yuklenmemek icin kucuk bir bekleme."""

    def __init__(self, interval: float = 0.15):
        self.interval = interval
        self.lock = threading.Lock()
        self.last: Dict[str, float] = {}

    def wait(self, url: str) -> None:
        if self.interval <= 0:
            return
        host = host_of(url)
        with self.lock:
            now = time.monotonic()
            prev = self.last.get(host, 0.0)
            delay = self.interval - (now - prev)
            if delay > 0:
                time.sleep(delay)
            self.last[host] = time.monotonic()


# ---------------------------------------------------------------------------
# Playlist okuma
# ---------------------------------------------------------------------------


def parse_playlist(text: str) -> Tuple[List[str], List[Channel]]:
    lines = text.splitlines(keepends=True)
    channels: List[Channel] = []
    i = 0
    while i < len(lines):
        raw = strip_eol(lines[i])
        if not raw.startswith("#EXTINF"):
            i += 1
            continue

        extra_idxs: List[int] = []
        j = i + 1
        url_idx = -1
        while j < len(lines):
            t = strip_eol(lines[j])
            if not t.strip():
                j += 1
                continue
            if t.startswith("#EXTINF"):
                break
            if t.startswith("#"):
                extra_idxs.append(j)
                j += 1
                continue
            url_idx = j
            break

        if url_idx < 0:
            i = j if j > i else i + 1
            continue

        attrs = dict(ATTR_RE.findall(raw))
        name = raw.rsplit(",", 1)[-1].strip()
        referrer = attrs.get("http-referrer") or attrs.get("referrer")
        user_agent = attrs.get("user-agent")
        referrer_idx = None
        user_agent_idx = None

        for idx in extra_idxs:
            m = EXTVLCOPT_RE.match(strip_eol(lines[idx]).strip())
            if not m:
                continue
            key, val = m.group(1).lower(), m.group(2).strip()
            if key == "http-referrer":
                referrer = val
                referrer_idx = idx
            elif key == "user-agent":
                user_agent = val
                user_agent_idx = idx

        channels.append(
            Channel(
                extinf_idx=i,
                url_idx=url_idx,
                extra_idxs=extra_idxs,
                tvg_id=attrs.get("tvg-id", ""),
                name=name,
                url=strip_eol(lines[url_idx]).strip(),
                referrer=referrer,
                user_agent=user_agent,
                quality_hint=quality_from_name(name),
                referrer_idx=referrer_idx,
                user_agent_idx=user_agent_idx,
                extinf_raw=raw,
            )
        )
        i = url_idx + 1

    return lines, channels


# ---------------------------------------------------------------------------
# HTTP / HLS dogrulama
# ---------------------------------------------------------------------------


class Prober:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.timeout = (args.connect_timeout, args.timeout)
        self.throttle = HostThrottle(args.host_delay)
        self.local = threading.local()

    def session(self) -> requests.Session:
        s = getattr(self.local, "session", None)
        if s is None:
            s = requests.Session()
            adapter = requests.adapters.HTTPAdapter(pool_connections=8, pool_maxsize=8, max_retries=0)
            s.mount("http://", adapter)
            s.mount("https://", adapter)
            self.local.session = s
        return s

    def fetch(self, url: str, headers: Dict[str, str], retries: Optional[int] = None) -> Tuple[Optional[int], str, bytes, str]:
        """(status, final_url, body, error). status None means a network error."""
        h = {"User-Agent": DEFAULT_UA, "Accept": "*/*"}
        h.update(headers or {})
        max_retries = self.args.retries if retries is None else retries

        last_err = ""
        for attempt in range(max_retries + 1):
            self.throttle.wait(url)
            try:
                r = self.session().get(
                    url, headers=h, timeout=self.timeout, allow_redirects=True, stream=True
                )
                status = r.status_code
                final = r.url
                if status not in (200, 206):
                    r.close()
                    last_err = f"HTTP {status}"
                    if status in (400, 401, 404, 410, 451):
                        return status, final, b"", last_err
                    if attempt < max_retries:
                        time.sleep(0.8 * (attempt + 1))
                        continue
                    return status, final, b"", last_err

                body = bytearray()
                for chunk in r.iter_content(16384):
                    if chunk:
                        body.extend(chunk)
                    if len(body) >= self.args.max_bytes:
                        break
                r.close()
                return status, final, bytes(body), ""

            except requests.exceptions.SSLError as exc:
                last_err = f"SSL: {exc.__class__.__name__}"
                if not self.args.insecure:
                    try:
                        r = self.session().get(
                            url, headers=h, timeout=self.timeout,
                            allow_redirects=True, stream=True, verify=False
                        )
                        if r.status_code in (200, 206):
                            body = bytearray()
                            for chunk in r.iter_content(16384):
                                if chunk:
                                    body.extend(chunk)
                                if len(body) >= self.args.max_bytes:
                                    break
                            r.close()
                            return r.status_code, r.url, bytes(body), ""
                        r.close()
                    except Exception:
                        pass
            except requests.exceptions.Timeout:
                last_err = "timeout"
            except requests.exceptions.ConnectionError as exc:
                txt = str(exc)
                if "NameResolution" in txt or "getaddrinfo" in txt or "Name or service" in txt:
                    last_err = "DNS error"
                else:
                    last_err = "connection error"
            except requests.exceptions.RequestException as exc:
                last_err = exc.__class__.__name__
            except Exception as exc:  # beklenmeyen
                last_err = exc.__class__.__name__

            if attempt < max_retries:
                time.sleep(0.7 * (attempt + 1))

        return None, url, b"", last_err or "unknown error"

    def validate(
        self,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        depth: int = 0,
        retries: Optional[int] = None,
    ) -> ProbeResult:
        headers = headers or {}
        status, final, body, err = self.fetch(url, headers, retries)
        if status is None:
            return ProbeResult(False, err)

        text = body.decode("utf-8", "replace").lstrip("\ufeff")
        if "#EXTM3U" not in text[:4096]:
            return ProbeResult(False, f"not an M3U8 ({err or 'HTTP ' + str(status)})", status)

        uri_lines = [
            line.strip()
            for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]

        if "#EXT-X-STREAM-INF" in text:
            if depth >= 2:
                return ProbeResult(True, "master playlist", status)
            variants = [urljoin(final, v) for v in uri_lines[: self.args.variant_probe]]
            if not variants:
                return ProbeResult(False, "master: no variants", status)
            errs = []
            for variant in variants:
                sub = self.validate(variant, headers, depth + 1, retries)
                if sub.ok:
                    return ProbeResult(True, "master -> variant OK", status)
                errs.append(sub.note)
            return ProbeResult(False, "variants failed: " + (errs[0] if errs else ""), status)

        if "#EXTINF" in text or "#EXT-X-TARGETDURATION" in text:
            if self.args.deep:
                seg = self._first_segment(text, final, headers)
                if not seg.ok:
                    return ProbeResult(False, "media playlist found but segment failed: " + seg.note, status)
            return ProbeResult(True, "media playlist OK", status)

        return ProbeResult(False, "empty/invalid playlist", status)

    def _first_segment(self, text: str, base: str, headers: Dict[str, str]) -> ProbeResult:
        lines = text.splitlines()
        for i, line in enumerate(lines):
            if not line.strip().startswith("#EXTINF"):
                continue
            for nxt in lines[i + 1:]:
                nxt = nxt.strip()
                if not nxt or nxt.startswith("#"):
                    continue
                seg_url = urljoin(base, nxt)
                try:
                    self.throttle.wait(seg_url)
                    r = self.session().get(
                        seg_url, headers={"User-Agent": DEFAULT_UA, **headers},
                        timeout=self.timeout, stream=True
                    )
                    code = r.status_code
                    chunk = next(r.iter_content(2048), b"")
                    r.close()
                    if code in (200, 206) and chunk:
                        return ProbeResult(True, "segment OK", code)
                    return ProbeResult(False, f"segment HTTP {code}", code)
                except Exception as exc:
                    return ProbeResult(False, f"segment {exc.__class__.__name__}")
        return ProbeResult(False, "segment not found")


# ---------------------------------------------------------------------------
# Source (iptv-org) data
# ---------------------------------------------------------------------------


def download_json(url: str, cache_name: str, max_age_hours: float, offline: bool) -> list:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = CACHE_DIR / cache_name

    if cache.exists():
        age = (time.time() - cache.stat().st_mtime) / 3600.0
        if offline or age < max_age_hours:
            try:
                return json.loads(cache.read_text(encoding="utf-8"))
            except Exception:
                pass

    if offline:
        raise SystemExit(f"No valid cache in offline mode: {cache}")

    try:
        resp = requests.get(url, headers={"User-Agent": DEFAULT_UA}, timeout=(15, 90))
        resp.raise_for_status()
        data = resp.json()
        cache.write_bytes(resp.content)
        return data
    except Exception as exc:
        if cache.exists():
            age = (time.time() - cache.stat().st_mtime) / 3600.0
            print(f"  ! Could not download source ({exc}); using cache ({age:.1f} hours old).")
            return json.loads(cache.read_text(encoding="utf-8"))
        raise SystemExit(f"Could not download source and no cache exists: {exc}")


def build_name_index(channels: List[dict]) -> Dict[Tuple[str, str], str]:
    """(country, normalized name) -> channel ID.

    If more than one channel in the same country has the same name, that name
    is considered ambiguous and is not added to the index.
    """
    name_sets: Dict[Tuple[str, str], set] = defaultdict(set)
    for c in channels:
        countries = c.get("country") or ""
        if isinstance(countries, str):
            countries = [countries]
        countries = [str(x).upper() for x in countries if x]
        if not countries:
            continue
        names = [c.get("name") or ""]
        alt = c.get("alt_names") or []
        if isinstance(alt, str):
            alt = [alt]
        names.extend(alt)
        cid = c.get("id")
        if not cid:
            continue
        for country in countries:
            for nm in names:
                key = normalize_name(nm)
                if key:
                    name_sets[(country, key)].add(cid)

    return {k: next(iter(ids)) for k, ids in name_sets.items() if len(ids) == 1}


def load_source(offline: bool) -> SourceData:
    print("Loading source data (iptv-org)...")
    streams = download_json(STREAMS_API, "streams.json", 6, offline)
    channels = download_json(CHANNELS_API, "channels.json", 24 * 30, offline)

    src = SourceData(stream_count=len(streams), channel_count=len(channels))
    for s in streams:
        ch = s.get("channel")
        if ch:
            src.by_channel[ch].append(s)
            src.by_channel[ch.lower()].append(s)

    src.by_name = build_name_index(channels)

    print(f"  Source: {src.stream_count} streams, {src.channel_count} channels")
    return src


def country_of(tvg_id: str) -> str:
    base = tvg_id.split("@", 1)[0].strip()
    if "." not in base:
        return ""
    cc = base.rsplit(".", 1)[1]
    return cc.upper() if len(cc) == 2 and cc.isalpha() else ""


def resolve_keys(ch: Channel, src: SourceData, allow_name: bool = False) -> List[str]:
    """Match keys.

    Default (safe) mode: exact tvg-id and manually defined aliases only.
    If allow_name=True: exact normalized name match inside the same country.
    """
    base = ch.tvg_id.split("@", 1)[0].strip()
    keys: List[str] = []

    def add(k: Optional[str]) -> None:
        if k and k not in keys:
            keys.append(k)

    add(base)
    for alias in ALIASES.get(base, []):
        add(alias)

    if not allow_name or not base:
        return keys

    country = country_of(ch.tvg_id)
    if not country:
        return keys

    # Exact name match within the same country (TRT Belgesel HD -> TRT Belgesel).
    add(src.by_name.get((country, normalize_name(ch.name))))

    return keys


def scheme_swap(url: str) -> Optional[str]:
    p = urlsplit(url)
    if p.scheme == "https":
        return urlunsplit(p._replace(scheme="http"))
    if p.scheme == "http":
        return urlunsplit(p._replace(scheme="https"))
    return None


def quality_siblings(url: str) -> List[Tuple[str, int]]:
    """Quality variants in the same directory (e.g. atv_1080p -> atv_720p)."""
    p = urlsplit(url)
    m = re.search(r"(?i)(\d{3,4})p", p.path)
    if not m:
        return []
    out: List[Tuple[str, int]] = []
    for q in ("1080p", "720p", "576p", "540p", "480p", "360p", "240p"):
        if q.lower() == m.group(0).lower():
            continue
        new_path = p.path[: m.start(0)] + q + p.path[m.end(0):]
        out.append((urlunsplit(p._replace(path=new_path)), int(q[:-1])))
    return out


def gather_candidates(ch: Channel, src: SourceData, args: argparse.Namespace) -> List[dict]:
    """Priority: same feed -> higher quality -> trusted source.

    - candidates from the iptv-org source (most trusted)
    - the http/https variant of the same URL
    - quality variants in the same directory (e.g. 1080p -> 720p)
    """
    keys = resolve_keys(ch, src, getattr(args, "name_match", False))
    orig_feed = ch.tvg_id.split("@", 1)[1] if "@" in ch.tvg_id else ""
    seen = set()
    pool: List[dict] = []

    def add(url: str, quality: Optional[int], trust: int,
            referrer: Optional[str] = None, user_agent: Optional[str] = None,
            origin: str = "source", geo: int = 0, feed: str = "") -> None:
        url = (url or "").strip()
        if not url or not is_probably_hls(url):
            return
        key = (url, referrer, user_agent)
        if key in seen:
            return
        # The original URL was already tested with the same headers; do not retry it.
        if url == ch.url and referrer == ch.referrer and user_agent == ch.user_agent:
            return
        seen.add(key)
        pool.append({
            "url": url,
            "quality": quality or 0,
            "trust": trust,
            "geo": geo,
            "feed": feed or "",
            "referrer": referrer,
            "user_agent": user_agent,
            "origin": origin,
        })

    # 1) iptv-org kaynagi
    for key in keys:
        for s in src.by_channel.get(key, []) + src.by_channel.get(key.lower(), []):
            labels = " ".join(s.get("labels") or []).lower()
            q = parse_quality(s.get("quality")) or quality_from_name(s.get("title") or "") or ch.quality_hint
            add(s.get("url"), q, 2, s.get("referrer"), s.get("user_agent"), "source",
                1 if "geo" in labels else 0, s.get("feed") or "")

    # 2) Ayni URL'nin diger semasi (https -> http veya tersi)
    swap = scheme_swap(ch.url)
    if swap:
        add(swap, ch.quality_hint, 3, ch.referrer, ch.user_agent, "http/https", 0, orig_feed)

    # 3) Quality variants in the same directory
    if args.siblings:
        for sib, q in quality_siblings(ch.url):
            add(sib, q, 1, ch.referrer, ch.user_agent, "same directory", 0, orig_feed)

    # Same feed first; then non-geo-blocked, higher quality and trusted source.
    def sort_key(x: dict) -> tuple:
        feed_pen = 0 if (not orig_feed or x["feed"] == orig_feed) else 1
        return (x["geo"], feed_pen, -x["quality"], -x["trust"])

    pool.sort(key=sort_key)
    return pool[: max(1, args.max_candidates)]


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------


def process_channel(ch: Channel, src: SourceData, prober: Prober, args: argparse.Namespace) -> Change:
    h: Dict[str, str] = {}
    if ch.referrer:
        h["Referer"] = ch.referrer
    if ch.user_agent:
        h["User-Agent"] = ch.user_agent

    first = prober.validate(ch.url, h)

    # If the first attempt failed, do not replace immediately: some streams are
    # intermittent. Retry a few times with short delays; keep it if it works.
    if not first.ok and args.confirm > 0:
        for i in range(args.confirm):
            time.sleep(0.8 + i * 0.7)
            again = prober.validate(ch.url, h, retries=0)
            if again.ok:
                return Change(
                    ch, "UNSTABLE",
                    note="failed on the first attempt, succeeded on retry (link kept)",
                    old_note=first.note,
                )

    if first.ok:
        if not args.refresh_different:
            return Change(ch, "OK", note=first.note)
        # It works, but if the source has a different URL and that URL works, refresh.
        for cand in gather_candidates(ch, src, args):
            new_url = cand["url"]
            if new_url == ch.url:
                continue
            chd: Dict[str, str] = {}
            if cand.get("referrer"):
                chd["Referer"] = cand["referrer"]
            if cand.get("user_agent"):
                chd["User-Agent"] = cand["user_agent"]
            r = prober.validate(new_url, chd)
            if r.ok:
                return Change(
                    ch, "REPLACE", new_url=new_url,
                    new_referrer=cand.get("referrer"),
                    new_user_agent=cand.get("user_agent"),
                    note=f"changed in source ({cand.get('origin', 'source')})",
                    old_note=first.note,
                )
        return Change(ch, "OK", note=first.note)

    # It does not work: try candidates from the source.
    cands = gather_candidates(ch, src, args)
    if not cands:
        return Change(ch, "DEAD", note=f"{first.note}; no match in source")

    errors: List[str] = []
    for cand in cands:
        new_url = cand["url"]
        same_url = new_url == ch.url
        # Same URL: only test if the source requires different headers (referrer/user-agent).
        if same_url and not (cand.get("referrer") or cand.get("user_agent")):
            continue
        chd = {}
        if cand.get("referrer"):
            chd["Referer"] = cand["referrer"]
        if cand.get("user_agent"):
            chd["User-Agent"] = cand["user_agent"]
        r = prober.validate(new_url, chd)
        if r.ok:
            if same_url and (
                cand.get("referrer") == ch.referrer and cand.get("user_agent") == ch.user_agent
            ):
                continue
            return Change(
                ch, "REPLACE", new_url=new_url,
                new_referrer=cand.get("referrer"),
                new_user_agent=cand.get("user_agent"),
                note=f"{first.note} -> new link works ({cand.get('origin', 'source')})",
                old_note=first.note,
            )
        errors.append(f"{host_of(new_url)}: {r.note}")

    detail = errors[0] if errors else "no candidates"
    return Change(
        ch, "DEAD",
        note=f"{first.note}; no working alternative in source "
             f"({len(errors)} candidates tried: {detail})",
    )


def render(
    lines: List[str],
    changes: List[Change],
    args: argparse.Namespace,
) -> Tuple[str, List[Change]]:
    by_channel = {id(c.channel): c for c in changes}

    remove_idxs = set()
    url_replace: Dict[int, str] = {}
    line_replace: Dict[int, str] = {}
    line_insert: Dict[int, List[str]] = {}
    extinf_replace: Dict[int, str] = {}

    applied: List[Change] = []

    for ch in (c.channel for c in changes):
        change = by_channel[id(ch)]
        if change.action == "REPLACE" and change.new_url:
            # Replace the URL line
            url_replace[ch.url_idx] = change.new_url

            # If the referrer changed, update both the EXTINF attribute and the EXTVLCOPT line
            if change.new_referrer and change.new_referrer != ch.referrer:
                if ch.referrer_idx is not None:
                    line_replace[ch.referrer_idx] = f"#EXTVLCOPT:http-referrer={change.new_referrer}"
                else:
                    line_insert.setdefault(ch.url_idx, []).append(
                        f"#EXTVLCOPT:http-referrer={change.new_referrer}"
                    )
                if "http-referrer=" in ch.extinf_raw:
                    extinf_replace[ch.extinf_idx] = re.sub(
                        r'http-referrer="[^"]*"',
                        f'http-referrer="{change.new_referrer}"',
                        ch.extinf_raw,
                    )

            # If the user-agent changed, update/add the EXTVLCOPT line
            if change.new_user_agent and change.new_user_agent != ch.user_agent:
                if ch.user_agent_idx is not None:
                    line_replace[ch.user_agent_idx] = (
                        f"#EXTVLCOPT:http-user-agent={change.new_user_agent}"
                    )
                else:
                    line_insert.setdefault(ch.url_idx, []).append(
                        f"#EXTVLCOPT:http-user-agent={change.new_user_agent}"
                    )

            applied.append(change)

        elif change.action == "REMOVE":
            for idx in range(ch.extinf_idx, ch.url_idx + 1):
                remove_idxs.add(idx)
            applied.append(change)

    if not applied:
        return "".join(lines), applied

    eol = "\r\n" if any(l.endswith("\r\n") for l in lines) else "\n"
    out: List[str] = []

    for idx, line in enumerate(lines):
        if idx in remove_idxs:
            continue

        if idx in extinf_replace:
            out.append(extinf_replace[idx] + get_eol(line))
            continue

        if idx in line_replace:
            out.append(line_replace[idx] + get_eol(line))
            continue

        if idx in line_insert:
            for extra in line_insert[idx]:
                out.append(extra + eol)
            if idx in url_replace:
                out.append(url_replace[idx] + get_eol(line))
                continue

        if idx in url_replace:
            out.append(url_replace[idx] + get_eol(line))
        else:
            out.append(line)

    return "".join(out), applied


def update_header_time(text: str) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    pattern = re.compile(r"^#(?:G[üu]ncelleme|Updated):.*$", re.MULTILINE)
    if pattern.search(text):
        return pattern.sub(f"#Updated: {now}", text, count=1)
    return text


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def write_report(changes: List[Change], path: Path) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["status", "channel", "tvg_id", "old_url", "new_url", "note"])
        for c in changes:
            durum = {
                "OK": "WORKING",
                "UNSTABLE": "UNSTABLE (WORKING)",
                "REPLACE": "REPLACED",
                "DEAD": "DEAD - NO ALTERNATIVE",
                "REMOVE": "REMOVED",
                "SKIPPED": "SKIPPED",
            }.get(c.action, c.action)
            w.writerow([durum, c.channel.name, c.channel.tvg_id,
                        c.channel.url, c.new_url or "", c.note])


# ---------------------------------------------------------------------------
# Ana akis
# ---------------------------------------------------------------------------


def _filename_key(text: str) -> str:
    text = text.lower()
    for a, b in (("ı", "i"), ("ş", "s"), ("ğ", "g"), ("ü", "u"), ("ö", "o"), ("ç", "c")):
        text = text.replace(a, b)
    return text


def find_target_playlist(explicit: Optional[Path], base: Path) -> Path:
    """Find the playlist whose name contains PLAYLIST_KEYWORD; ask if multiple."""
    if explicit is not None:
        # A file explicitly provided on the command line is always accepted.
        p = explicit if explicit.is_absolute() else (base / explicit)
        p = p.resolve()
        if not p.exists():
            raise SystemExit(f"Playlist not found: {p}")
        return p

    all_playlists = sorted(
        p for p in base.iterdir()
        if p.is_file() and p.suffix.lower() in (".m3u", ".m3u8")
    )
    candidates = [p for p in all_playlists if PLAYLIST_KEYWORD in _filename_key(p.stem)]

    # If a choice was made before, use it (for one-click runs).
    if PLAYLIST_CONFIG.exists():
        try:
            name = PLAYLIST_CONFIG.read_text(encoding="utf-8-sig").strip().strip('"')
        except Exception:
            name = ""
        if name:
            saved = base / name
            if saved.exists() and saved.is_file() and PLAYLIST_KEYWORD in _filename_key(saved.stem):
                return saved
            print(f"! playlist.txt is invalid or does not contain the keyword: {name}")
            print("  You will be asked again.\n")

    if not candidates:
        raise SystemExit(
            f'No .m3u or .m3u8 file containing "{PLAYLIST_KEYWORD}" was found in this folder.\n'
            f"Folder: {base}\n\n"
            f'Rename your playlist to something like "update-channels.m3u".\n'
            f'This way the script cannot touch other playlists by mistake.'
        )

    if len(candidates) == 1:
        return candidates[0]

    print(f'\nMore than one playlist contains "{PLAYLIST_KEYWORD}". Which one should be updated?\n')
    for i, p in enumerate(candidates, 1):
        print(f"  {i}) {p.name}")
    print("\n(The choice is saved in 'playlist.txt' and will not be asked again.)")

    while True:
        try:
            answer = input("\nChoice [1]: ").strip() or "1"
            index = int(answer)
            if 1 <= index <= len(candidates):
                break
        except (ValueError, EOFError):
            pass
        print("Invalid choice, try again.")

    chosen = candidates[index - 1]
    try:
        PLAYLIST_CONFIG.write_text(chosen.name + "\n", encoding="utf-8")
    except Exception as exc:
        print(f"! Could not save selection: {exc}")
    return chosen


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="IPTV playlist health check and automatic link updater",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--playlist", type=Path, default=None,
                   help=f"Specific M3U file (default: the single playlist containing '{PLAYLIST_KEYWORD}')")
    p.add_argument("--output", type=Path, default=None, help="Output file (default: the playlist itself)")
    p.add_argument("--dry-run", action="store_true", help="Do not write files, only report")
    p.add_argument("--scan-only", action="store_true", help="Scan only; do not change/add links")
    p.add_argument("--refresh-different", action="store_true",
                   help="Also update working channels whose source URL changed")
    p.add_argument("--remove-dead", action="store_true",
                   help="Remove channels with no working alternative")
    p.add_argument("--only", default=None, help="Only channels whose name/ID contains this text")
    p.add_argument("--limit", type=int, default=0, help="First N channels (for testing)")
    p.add_argument("--workers", type=int, default=8, help="Number of parallel channels (default 8)")
    p.add_argument("--timeout", type=float, default=12.0, help="Read timeout in seconds")
    p.add_argument("--connect-timeout", type=float, default=8.0, help="Connection timeout in seconds")
    p.add_argument("--retries", type=int, default=2, help="Retries for failed requests")
    p.add_argument("--confirm", type=int, default=3,
                   help="Extra attempts before replacing a failed original link")
    p.add_argument("--variant-probe", type=int, default=2, help="Number of variants to try in a master playlist")
    p.add_argument("--max-bytes", type=int, default=512 * 1024, help="Maximum bytes to read for a playlist")
    p.add_argument("--host-delay", type=float, default=0.15, help="Delay between requests to the same host")
    p.add_argument("--deep", action="store_true", help="Also download the first video segment (slower)")
    p.add_argument("--siblings", action=argparse.BooleanOptionalAction, default=True,
                   help="Try quality variants such as 720p/1080p in the same directory (default: on)")
    p.add_argument("--max-candidates", type=int, default=10,
                   help="Maximum candidate links per channel")
    p.add_argument("--name-match", action="store_true",
                   help="If tvg-id is missing, try an exact name match in the same country (default: off)")
    p.add_argument("--offline", action="store_true", help="Use the cache instead of downloading source data")
    p.add_argument("--insecure", action="store_true", help="Skip certificate verification on SSL errors")
    p.add_argument("--no-header", action="store_true", help="Do not change the #Updated timestamp")
    p.add_argument("--no-backup", action="store_true", help="Do not create a backup")
    p.add_argument("--report", type=Path, default=None, help="Report file (CSV)")
    p.add_argument("--verbose", "-v", action="store_true", help="Print a line for every channel")
    return p.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)

    # Windows konsolunda Turkce karakterler icin
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    playlist: Path = find_target_playlist(args.playlist, BASE_DIR)
    if not playlist.exists():
        print(f"ERROR: playlist not found: {playlist}")
        return 1

    output: Path = args.output or playlist
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    print("=" * 68)
    print(" IPTV PLAYLIST UPDATER")
    print("=" * 68)
    print(f"Playlist : {playlist}")

    raw = playlist.read_bytes()
    had_bom = raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig")
    lines, channels = parse_playlist(text)

    if not channels:
        print("ERROR: no #EXTINF entries found in the playlist.")
        return 1

    selected = channels
    if args.only:
        needle = args.only.lower()
        selected = [c for c in selected if needle in c.name.lower() or needle in c.tvg_id.lower()]
    if args.limit and args.limit > 0:
        selected = selected[: args.limit]

    print(f"Channels : {len(channels)} (to scan: {len(selected)})")
    print(f"Parallel : {args.workers} workers, timeout: {args.timeout}s\n")

    src = load_source(args.offline)

    prober = Prober(args)
    changes: List[Change] = []
    total = len(selected)
    done = 0

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(process_channel, ch, src, prober, args): ch for ch in selected}
        for fut in as_completed(futures):
            ch = futures[fut]
            done += 1
            try:
                change = fut.result()
            except Exception as exc:
                change = Change(ch, "DEAD", note=f"unexpected error: {exc.__class__.__name__}: {exc}")

            if args.scan_only and change.action == "REPLACE":
                # In scan-only mode the link is not changed; but we still report that the
                # channel is currently dead and that an alternative exists in the source.
                change = Change(
                    ch,
                    "DEAD",
                    note=f"{change.old_note}; alternative exists in source: {change.new_url}",
                )

            if change.action == "DEAD" and args.remove_dead:
                change.action = "REMOVE"

            changes.append(change)

            if args.verbose or change.action != "OK":
                tag = {
                    "OK": "OK",
                    "UNSTABLE": "UNSTABLE",
                    "REPLACE": "REPLACED",
                    "DEAD": "DEAD",
                    "REMOVE": "REMOVE",
                }.get(change.action, change.action)
                print(f"[{done:>3}/{total}] {tag:<8} {ch.name}")
                if change.action in ("REPLACE", "DEAD", "REMOVE"):
                    print(f"        old : {ch.url[:110]}")
                    if change.new_url:
                        print(f"        new : {change.new_url[:110]}")
                    if change.note:
                        print(f"        note: {change.note[:160]}")
            elif done % 10 == 0 or done == total:
                print(f"[{done:>3}/{total}] scanning...")

    # Sort results by playlist order to preserve channel order
    order = {id(c): i for i, c in enumerate(channels)}
    changes.sort(key=lambda c: order.get(id(c.channel), 10**9))

    new_text, applied = render(lines, changes, args)

    if not args.no_header and not args.scan_only:
        new_text = update_header_time(new_text)

    # Ozet
    ok = sum(1 for c in changes if c.action == "OK")
    unstable = sum(1 for c in changes if c.action == "UNSTABLE")
    repl = sum(1 for c in changes if c.action == "REPLACE")
    dead = sum(1 for c in changes if c.action in ("DEAD", "REMOVE"))
    print("\n" + "=" * 68)
    print(f"Scanned        : {len(changes)}")
    print(f"Working        : {ok}")
    if unstable:
        print(f"Unstable       : {unstable} (link kept)")
    print(f"Updated links  : {repl}")
    print(f"Not working    : {dead}")
    print("=" * 68)

    if repl:
        print("\nUpdated links:")
        for c in changes:
            if c.action == "REPLACE":
                print(f"  - {c.channel.name} ({c.channel.tvg_id})")
                print(f"      {c.channel.url[:120]}")
                print(f"   -> {str(c.new_url)[:120]}")

    if dead:
        print("\nNot working / no alternative found:")
        for c in changes:
            if c.action in ("DEAD", "REMOVE"):
                print(f"  - {c.channel.name} ({c.channel.tvg_id}) -> {c.note[:140]}")

    # Rapor
    if args.report:
        report_path = args.report
        report_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        report_dir = output.parent / "reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / f"update_report_{stamp}.csv"
    try:
        write_report(changes, report_path)
        print(f"\nReport: {report_path}")
    except Exception as exc:
        print(f"! Could not write report: {exc}")

    # Write to file
    if args.dry_run:
        print("\nDRY-RUN: the playlist was not written.")
        return 0

    if args.scan_only:
        print("\nSCAN-ONLY: the playlist was not written.")
        return 0

    if not applied and args.no_header:
        print("\nNo changes; file not written.")
        return 0

    output.parent.mkdir(parents=True, exist_ok=True)

    if not args.no_backup and output.exists():
        backup_dir = output.parent / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup = backup_dir / f"{output.stem}_{stamp}{output.suffix}"
        shutil.copy2(output, backup)
        print(f"Backup: {backup}")

    data = new_text.encode("utf-8")
    if had_bom:
        data = b"\xef\xbb\xbf" + data

    try:
        tmp = output.with_suffix(output.suffix + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, output)
    except PermissionError:
        print(
            "\nERROR: The playlist may be open in another program.\n"
            "Close your IPTV player/editor and try again."
        )
        return 1

    print(f"Written: {output}")
    print("\nDone.")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except KeyboardInterrupt:
        print("\nCancelled.")
        raise SystemExit(1)
    except Exception as exc:
        print(f"\nERROR: {exc}")
        raise SystemExit(1)
