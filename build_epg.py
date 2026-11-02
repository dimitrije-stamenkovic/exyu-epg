#!/usr/bin/env python3
"""
Build an XMLTV guide for the EX-YU channels in channels.txt.

Downloads the epgshare01 guides and the mts.rs channel API, matches their channels to the playlist
channel names and writes public/epg.xml(.gz) where every channel carries the
playlist names as display-names, so players match the guide by name.

Usage:
    python build_epg.py [--cache DIR]
"""

# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "requests",
# ]
# ///
import argparse
import gzip
import io
import json
import re
import unicodedata
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

import requests

ROOT = Path(__file__).parent
CHANNELS_FILE = ROOT / "channels.txt"
OVERRIDES_FILE = ROOT / "overrides.txt"
OUTPUT_DIR = ROOT / "public"

# Source code -> URL. Code is also the country the source is preferred for.
SOURCES = {
    "RS": "https://epgshare01.online/epgshare01/epg_ripper_RS1.xml.gz",
    "HR": "https://epgshare01.online/epgshare01/epg_ripper_HR1.xml.gz",
    "BA": "https://epgshare01.online/epgshare01/epg_ripper_BA1.xml.gz",
    "SK": "https://epgshare01.online/epgshare01/epg_ripper_SPORTKLUB1.xml.gz",
}

# mts.rs channel/programme search (Serbian channels, ~1 day ahead). The API
# returns at most 50 channels per page, so it is fetched page by page.
MTS_URL = (
    "https://mts.rs/hybris/ecommerce/b2c/v1/products/search"
    "?sort=pozicija-rastuce&searchQueryContext=CHANNEL_PROGRAM"
    "&query=:pozicija-rastuce:tip-kanala-radio:TV+kanali&pageSize=50"
)

MTS_PROVIDER = "mts-api"

# Guides with wrong programme times: provider -> default correction in hours.
# mts.rs labels its times UTC but they are 2h late; epgshare's copy of the mts
# guide (RS1 channels with <url>www.mts.rs</url>) is 1h late. The actual shift
# is measured on every build against the correct guides below and the
# default is only used when it can't be measured.
TIME_FIXES = {MTS_PROVIDER: -2.0, "www.mts.rs": -1.0}
REFERENCE_PROVIDERS = {"www.epg.sbb.rs", "www.a1.hr", "www.telemach.ba"}

# Playlist name prefix / name hint -> country code
COUNTRY_ALIASES = {
    "RS": "RS", "SRB": "RS", "SR": "RS",
    "HR": "HR", "CRO": "HR",
    "BA": "BA", "BH": "BA", "BIH": "BA",
    "MO": "ME", "MNG": "ME", "CG": "ME", "MNE": "ME", "ME": "ME",
    "MK": "MK", "MKD": "MK",
    "SI": "SI", "SLO": "SI",
}

# Spellings that differ between playlist and guides, applied before matching
NAME_ALIASES = [
    (r"\bSPORT\s*KLUB\b", "SK"),
    (r"\bNICK(ELODEON)?\s+(JR|JUNIOR)\b", "NICKJR"),
    (r"\bAND\b", "&"),
    (r"\bNATIONAL\s+GEO(GRAPHIC)?\b", "NAT GEO"),
    (r"\bHBO\s+1\b", "HBO"),
    (r"\+", " PLUS "),
]

# Words that only describe quality/packaging and never tell channels apart
NOISE_WORDS = {
    "HD", "FHD", "UHD", "SD", "HEVC", "H265", "720P", "1080P", "RAW",
    "VIP", "EXTRA", "BACKUP", "TV", "TELEVIZIJA", "CHANNEL", "KANAL",
}


def fold(text):
    """Uppercase ASCII-ish form: 'Život' -> 'ZIVOT', 'Đ' -> 'D'."""
    text = unicodedata.normalize("NFKC", text).replace("Đ", "D").replace("đ", "d")
    text = unicodedata.normalize("NFKD", text)
    return "".join(c for c in text if not unicodedata.combining(c)).upper()


def channel_key(name):
    """Return (key, country hint) for a channel name from either side.

    'RS: RTS 1 [EXTRA]  [1080p]' -> ('RTS1', 'RS')
    'RTS 1 HD (RS)'              -> ('RTS1', 'RS')
    'BA: N1 BH [720p]'           -> ('N1', 'BA')
    """
    text = fold(name)
    country = None

    prefix = re.match(r"\s*([A-Z]{2,5})\s*:", text)
    if prefix:
        country = COUNTRY_ALIASES.get(prefix.group(1), prefix.group(1))
        text = text[prefix.end():]

    for hint in re.findall(r"\(([^)]*)\)", text):
        country = COUNTRY_ALIASES.get(hint.strip(), country)
    text = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", text)
    for pattern, replacement in NAME_ALIASES:
        text = re.sub(pattern, replacement, text)

    words = [w for w in re.findall(r"[A-Z0-9&]+", text) if w not in NOISE_WORDS]
    while words and words[-1] in COUNTRY_ALIASES:
        country = COUNTRY_ALIASES[words.pop()]
    return "".join(words), country


def download(url, cache_dir):
    name = url.rsplit("/", 1)[-1]
    if cache_dir and (cache_dir / name).exists():
        return (cache_dir / name).read_bytes()
    print(f"Downloading {url}")
    response = requests.get(url, timeout=120)
    response.raise_for_status()
    if cache_dir:
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / name).write_bytes(response.content)
    return response.content


def load_source(code, data):
    """Return {channel_id: {"source", "provider", "element", "programmes"}} for one guide.

    "provider" is the original guide owner from the channel's <url>, e.g.
    www.epg.sbb.rs or www.mts.rs (epgshare mixes several in one file).
    """
    channels = {}
    for _, elem in ET.iterparse(io.BytesIO(gzip.decompress(data))):
        if elem.tag == "channel":
            provider = (elem.findtext("url") or "").split("//")[-1].split("/")[0]
            channels[elem.get("id")] = {
                "source": code, "provider": provider, "element": elem, "programmes": [],
            }
        elif elem.tag == "programme":
            channel = channels.get(elem.get("channel"))
            if channel is not None:
                channel["programmes"].append(elem)
    return channels


def download_mts(cache_dir):
    cache = cache_dir / "mts.json" if cache_dir else None
    if cache and cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    print(f"Downloading {MTS_URL}")
    products, page, total_pages = [], 0, 1
    while page < total_pages:
        response = requests.get(f"{MTS_URL}&currentPage={page}", timeout=60)
        response.raise_for_status()
        data = response.json()
        products += data.get("products", [])
        total_pages = data.get("pagination", {}).get("totalPages", 1)
        page += 1
    if cache:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(products, ensure_ascii=False), encoding="utf-8")
    return products


def xmltv_time(value):
    """'2026-11-02T00:45:00.000Z' -> '20261102004500 +0000'."""
    return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%Y%m%d%H%M%S %z")


def load_mts(products):
    """Convert mts products to the same structure as load_source()."""
    channels = {}
    for product in products:
        channel_id = f"mts.{product['code']}"
        element = ET.Element("channel", id=channel_id)
        ET.SubElement(element, "display-name").text = product.get("name", product["code"])
        for image in product.get("images", []):
            if image.get("imageType") == "PRIMARY" and image.get("url"):
                ET.SubElement(element, "icon", src=image["url"])
                break
        programmes = []
        for program in product.get("programs", []):
            if not all(program.get(k) for k in ("start", "end", "title")):
                continue
            programme = ET.Element("programme", start=xmltv_time(program["start"]),
                                   stop=xmltv_time(program["end"]), channel=channel_id)
            ET.SubElement(programme, "title", lang="sr").text = program["title"]
            if program.get("description"):
                ET.SubElement(programme, "desc", lang="sr").text = program["description"]
            if program.get("category"):
                ET.SubElement(programme, "category", lang="sr").text = program["category"]
            if program.get("picture", {}).get("url"):
                ET.SubElement(programme, "icon", src=program["picture"]["url"])
            programmes.append(programme)
        channels[channel_id] = {
            "source": "RS", "provider": MTS_PROVIDER, "element": element, "programmes": programmes,
        }
    return channels


def load_channels():
    """Playlist channels as [(name, tvg_id)]."""
    channels = []
    for line in CHANNELS_FILE.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#"):
            name, _, tvg_id = line.partition("\t")
            channels.append((name, tvg_id.strip()))
    return channels


def load_overrides():
    """overrides.txt lines 'playlist name = source channel id' ('-' skips).

    Keyed by (channel key, country) so an override only applies to playlist
    channels with the same country prefix.
    """
    overrides = {}
    if OVERRIDES_FILE.exists():
        for line in OVERRIDES_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                name, _, target = line.rpartition("=")
                overrides[channel_key(name.strip())] = target.strip()
    return overrides


def build_index(sources):
    """channel key -> list of source channel ids."""
    index = defaultdict(list)
    for channel_id, channel in sources.items():
        names = [d.text or "" for d in channel["element"].findall("display-name")]
        keys = {channel_key(n) for n in names}
        for key, country in keys:
            if key:
                index[key].append((channel_id, country or channel["source"]))
    return index


def pick(candidates, country, sources):
    """Return (guide channel, filler channel) for one playlist channel.

    mts.rs comes first. It only covers about a day and a half, so a matching
    epgshare channel (filler) supplies the days after it. Without mts, the
    epgshare channel for the playlist's country with the fullest guide wins.
    """
    def score(candidate):
        channel_id, channel_country = candidate
        channel = sources[channel_id]
        return (
            channel_country == country,
            channel["source"] == country,
            channel["provider"] in REFERENCE_PROVIDERS,
            len(channel["programmes"]),
        )
    candidates = [c for c in candidates if sources[c[0]]["programmes"]]
    mts = [c for c in candidates if sources[c[0]]["provider"] == MTS_PROVIDER]
    others = [c for c in candidates if sources[c[0]]["provider"] != MTS_PROVIDER]
    best_other = max(others, key=score)[0] if others else None
    if mts:
        return max(mts, key=score)[0], best_other
    return best_other, None


def match_channels(playlist, sources, overrides):
    """Return ({source id: [playlist entries]}, {source id: filler id}, [unmatched names])."""
    index = build_index(sources)
    matched = defaultdict(list)
    fillers = {}
    unmatched = []
    for name, tvg_id in playlist:
        key, country = channel_key(name)
        target, filler = overrides.get((key, country)), None
        if target == "-":
            continue
        if target is None and len(key) > 1 and key in index:
            target, filler = pick(index[key], country, sources)
        if target in sources:
            matched[target].append((name, tvg_id))
            if filler:
                fillers.setdefault(target, filler)
        else:
            if target:
                print(f"Warning: override for {name!r} points to unknown channel {target!r}")
            unmatched.append(name)
    return matched, fillers, unmatched


def timestamp(value):
    return datetime.strptime(value, "%Y%m%d%H%M%S %z").timestamp()


def measure_offset(sources, provider):
    """Shift in hours of a provider's times against REFERENCE_PROVIDERS,
    from programmes with the same title on the same channel, or None."""
    reference = defaultdict(lambda: defaultdict(list))  # key -> title -> starts
    for channel in sources.values():
        if channel["provider"] in REFERENCE_PROVIDERS:
            for display in channel["element"].findall("display-name"):
                titles = reference[channel_key(display.text or "")[0]]
                for programme in channel["programmes"]:
                    titles[programme.findtext("title")].append(timestamp(programme.get("start")))

    shifts = Counter()
    for channel in sources.values():
        if channel["provider"] != provider:
            continue
        keys = {channel_key(d.text or "")[0] for d in channel["element"].findall("display-name")}
        for key in keys & reference.keys():
            for programme in channel["programmes"]:
                start = timestamp(programme.get("start"))
                for ref in reference[key].get(programme.findtext("title"), []):
                    shift = round((start - ref) / 1800) / 2  # half hours
                    if abs(shift) <= 4:
                        shifts[shift] += 1
    if not shifts:
        return None
    (best, count), *rest = shifts.most_common(2) + [(None, 0)]
    # Repeats of a show give other differences; the real shift must dominate
    return best if count >= 50 and count >= 2 * rest[0][1] else None


def fix_times(sources):
    for provider, default in TIME_FIXES.items():
        measured = measure_offset(sources, provider)
        hours = -measured if measured is not None else default
        how = "measured" if measured is not None else "default"
        print(f"Times: {provider} shifted {hours:+g}h ({how})")
        if not hours:
            continue
        for channel in sources.values():
            if channel["provider"] == provider:
                for programme in channel["programmes"]:
                    for attr in ("start", "stop"):
                        value = datetime.strptime(programme.get(attr), "%Y%m%d%H%M%S %z")
                        programme.set(attr, (value + timedelta(hours=hours)).strftime("%Y%m%d%H%M%S %z"))


def fix_url(url):
    """Some guides glue two URLs together ('https://a/https://b.png'); keep the inner one."""
    start = url.rfind("http")
    return (url[start:] if start > 0 else url).replace(" ", "%20")


def channel_logos(channel):
    return [fix_url(icon.get("src", "")) for icon in channel["element"].findall("icon") if icon.get("src")]


def build_logo_index(sources):
    """channel key -> logo URLs, mts.rs logos first (they are kept up to date)."""
    logos = defaultdict(list)
    ordered = sorted(sources.items(), key=lambda item: not item[0].startswith("mts."))
    for _, channel in ordered:
        for name in channel["element"].findall("display-name"):
            logos[channel_key(name.text or "")[0]] += channel_logos(channel)
    return logos


def logo_candidates(source, names, logos):
    """Logo URLs for one output channel, best first: an mts.rs logo, the
    channel's own logo, then logos other guides use for the same channel."""
    keys = [channel_key(d.text or "")[0] for d in source["element"].findall("display-name")]
    keys += [channel_key(name)[0] for name in names]
    others = [url for key in keys for url in logos.get(key, [])]
    mts = [url for url in others if url.startswith("https://mediasb2c.mts.rs/")]
    return list(dict.fromkeys(mts + channel_logos(source) + others))


def dead_urls(urls):
    """URLs answering 4xx/5xx. Many old SBB/EON (united.cloud) logo links 404.
    Network errors don't count as dead, so a flaky run keeps its logos."""
    session = requests.Session()

    def is_dead(url):
        try:
            return session.head(url, timeout=15, allow_redirects=True).status_code >= 400
        except requests.RequestException:
            return False

    with ThreadPoolExecutor(max_workers=16) as pool:
        return {url for url, dead in zip(urls, pool.map(is_dead, urls)) if dead}


def channel_programmes(channel_id, fillers, sources):
    """The channel's programmes, continued by its filler after they end."""
    programmes = list(sources[channel_id]["programmes"])
    filler = fillers.get(channel_id)
    if filler:
        end = max((timestamp(p.get("stop")) for p in programmes), default=0)
        programmes += [p for p in sources[filler]["programmes"] if timestamp(p.get("start")) >= end]
    return programmes


def build_xml(matched, fillers, sources):
    # A playlist tvg-id becomes an extra channel id only when every playlist
    # entry using it matched the same guide channel.
    tvg_targets = defaultdict(set)
    for channel_id, entries in matched.items():
        for _, tvg_id in entries:
            if tvg_id:
                tvg_targets[tvg_id].add(channel_id)
    aliases = defaultdict(list)
    for tvg_id, targets in tvg_targets.items():
        if len(targets) == 1 and tvg_id not in sources:
            aliases[targets.pop()].append(tvg_id)

    logos = build_logo_index(sources)
    candidates = {
        channel_id: logo_candidates(sources[channel_id], [name for name, _ in entries], logos)
        for channel_id, entries in matched.items()
    }
    dead = dead_urls(sorted({url for urls in candidates.values() for url in urls}))
    print(f"Logos: {len(dead)} dead links skipped")

    tv = ET.Element("tv", {"generator-info-name": "exyu-epg"})
    programmes = []
    for channel_id in sorted(matched):
        source = sources[channel_id]
        names = list(dict.fromkeys(name for name, _ in matched[channel_id]))
        logo = next((url for url in candidates[channel_id] if url not in dead), None)
        for out_id in [channel_id] + sorted(aliases[channel_id]):
            channel = ET.SubElement(tv, "channel", id=out_id)
            for name in names:
                ET.SubElement(channel, "display-name").text = name
            if logo:
                ET.SubElement(channel, "icon", src=logo)
            for programme in channel_programmes(channel_id, fillers, sources):
                copy = ET.Element(programme.tag, dict(programme.attrib, channel=out_id))
                for child in programme:
                    if child.tag == "icon":
                        child = ET.Element("icon", src=fix_url(child.get("src", "")))
                    copy.append(child)
                programmes.append(copy)
    tv.extend(programmes)
    ET.indent(tv, space=" ")
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(tv, encoding="utf-8")


INDEX_HTML = """<!doctype html>
<meta charset="utf-8">
<title>EX-YU EPG</title>
<h1>EX-YU EPG</h1>
<ul>
  <li><a href="epg.xml.gz">epg.xml.gz</a> (use this in the player)</li>
  <li><a href="epg.xml">epg.xml</a></li>
  <li><a href="report.txt">report.txt</a> (matched / unmatched channels)</li>
</ul>
"""


def write_report(matched, fillers, unmatched, sources):
    lines = [f"Matched {sum(map(len, matched.values()))} playlist channels, unmatched {len(unmatched)}", ""]
    lines.append("== Matched (guide channel [+ channel for later days] [programmes] <- playlist names)")
    for channel_id in sorted(matched):
        names = ", ".join(name for name, _ in matched[channel_id])
        filler = f" + {fillers[channel_id]}" if channel_id in fillers else ""
        lines.append(f"{channel_id}{filler} [{len(channel_programmes(channel_id, fillers, sources))}] <- {names}")
    lines += ["", "== Unmatched (add to overrides.txt if a guide exists)"]
    lines += sorted(unmatched)
    (OUTPUT_DIR / "report.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(lines[0])


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--cache", type=Path, help="Reuse/store downloaded guides in this directory")
    args = parser.parse_args()

    # A failing source is skipped so one outage doesn't stop the daily build
    sources = {}
    for code, url in SOURCES.items():
        try:
            channels = load_source(code, download(url, args.cache))
        except (requests.RequestException, OSError, ET.ParseError) as e:
            print(f"Warning: skipping {code} source: {e}")
            continue
        print(f"{code}: {len(channels)} channels")
        for channel_id, channel in channels.items():
            sources.setdefault(channel_id, channel)

    try:
        channels = load_mts(download_mts(args.cache))
        print(f"MTS: {len(channels)} channels")
        sources.update(channels)
    except (requests.RequestException, ValueError, KeyError) as e:
        print(f"Warning: skipping mts.rs source: {e}")
    if not sources:
        raise SystemExit("No guide source could be loaded")

    fix_times(sources)
    matched, fillers, unmatched = match_channels(load_channels(), sources, load_overrides())

    OUTPUT_DIR.mkdir(exist_ok=True)
    xml = build_xml(matched, fillers, sources)
    (OUTPUT_DIR / "epg.xml").write_bytes(xml)
    (OUTPUT_DIR / "epg.xml.gz").write_bytes(gzip.compress(xml, mtime=0))
    write_report(matched, fillers, unmatched, sources)
    (OUTPUT_DIR / "index.html").write_text(INDEX_HTML, encoding="utf-8")
    print(f"Wrote {OUTPUT_DIR / 'epg.xml'} ({len(xml) // 1024} KiB)")


if __name__ == "__main__":
    main()
