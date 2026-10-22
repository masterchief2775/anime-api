import asyncio
import importlib
import re
import time
from urllib.parse import urljoin

import httpx

from src.providers._http import UA
from src.providers._media import build_ctx

RANKING = [
    "anineko", "anizone", "anikoto", "reanime", "aniwaves",
    "kaa", "anibd", "animegg", "mkissa", "animeonsen",
]

WATCH_TIMEOUT = 75.0
EPISODES_TIMEOUT = 60.0
WATCH_CACHE_TTL = 45.0

# The first provider to answer is the fastest, not the best: a provider that
# only mirrors a 480p playlist can beat the one holding the 1080p variants.
# Wait a short grace period, then weigh the candidates on the resolution
# advertised by their playlists. The providers that answer after that window
# are not worth another eight seconds: the caller is already waiting.
QUALITY_GRACE = 2.0
QUALITY_TIMEOUT = 4.0
QUALITY_STREAMS = 4
QUALITY_VERIFY = 5
QUALITY_DRAIN = 2.0
SEGMENT_TIMEOUT = 8.0

_latency: dict = {}
_ep_latency: dict = {}
_failures: dict = {}
_watch_cache: dict = {}


def _load(name):
    try:
        return importlib.import_module(f"src.providers.{name}")
    except Exception as e:
        print(f"[RACE] provider {name} unavailable: {e}")
        return None


def providers():
    mods = []
    for name in RANKING:
        mod = _load(name)
        if mod is not None:
            mods.append((name, mod))
    return mods


def record_latency(name: str, seconds: float, ok: bool, kind: str = "watch"):
    table = _latency if kind == "watch" else _ep_latency
    if ok:
        prev = table.get(name)
        table[name] = seconds if prev is None else round(0.7 * prev + 0.3 * seconds, 3)
        _failures[name] = 0
    else:
        _failures[name] = _failures.get(name, 0) + 1


def default_provider():
    if not _latency:
        return RANKING[0]
    return sorted(_latency.items(), key=lambda kv: kv[1])[0][0]


def latency_table() -> dict:
    return {name: {"avg_seconds": _latency.get(name),
                   "episodes_seconds": _ep_latency.get(name),
                   "failures": _failures.get(name, 0)}
            for name in RANKING}


async def _watch_one(mod, name, anilist_id, ep, audio, ctx):
    key = (name, anilist_id, ep, audio)
    hit = _watch_cache.get(key)
    if hit and hit[0] > time.time():
        return name, hit[1], 0.0, True
    t0 = time.time()
    try:
        streams = await asyncio.wait_for(mod.watch(anilist_id, audio, ep, ctx), WATCH_TIMEOUT)
        dt = round(time.time() - t0, 3)
        if streams:
            _watch_cache[key] = (time.time() + WATCH_CACHE_TTL, streams)
            return name, streams, dt, True
        return name, None, dt, False
    except Exception as e:
        return name, None, round(time.time() - t0, 3), False


def _hls_streams(streams: list) -> list:
    return [s for s in streams
            if isinstance(s, dict) and s.get("url")
            and str(s.get("type") or "hls").lower() == "hls"]


def _unique_hls(streams: list, limit: int | None = None) -> list:
    seen, out = set(), []
    for stream in _hls_streams(streams):
        url = stream.get("url")
        if url in seen:
            continue
        seen.add(url)
        out.append(stream)
        if limit is not None and len(out) >= limit:
            break
    return out


def _stream_headers(stream: dict) -> dict:
    headers = {"User-Agent": UA, "Accept": "*/*"}
    referer = stream.get("referer")
    if referer:
        headers["Referer"] = referer
    return headers


async def _probe_master(client, stream: dict) -> dict | None:
    url = stream.get("url")
    if not isinstance(url, str) or not url.startswith("http"):
        return None
    try:
        res = await client.get(url, headers=_stream_headers(stream))
        if res.status_code != 200:
            return None
        text = res.text
    except Exception:
        return None
    if "#EXTM3U" not in text:
        return None
    heights = [int(h) for h in re.findall(r"RESOLUTION=\d+x(\d+)", text)]
    return {
        "url": url,
        "text": text,
        "referer": stream.get("referer"),
        "height": max(heights) if heights else 0,
    }


async def _quality_evidence(streams: list) -> dict | None:
    probes = _unique_hls(streams, QUALITY_STREAMS)
    if not probes:
        return None
    async with httpx.AsyncClient(timeout=QUALITY_TIMEOUT, follow_redirects=True) as client:
        found = await asyncio.gather(*[_probe_master(client, s) for s in probes])
    usable = [item for item in found if item]
    return max(usable, key=lambda item: item["height"]) if usable else None


def _best_variant(evidence: dict) -> str | None:
    if not evidence or not evidence.get("height"):
        return None
    pattern = (r"RESOLUTION=\d+x" + str(evidence["height"])
               + r"[^\n]*\n\s*([^\s#][^\n]*)")
    match = re.search(pattern, evidence["text"])
    if not match:
        return None
    return urljoin(evidence["url"], match.group(1).strip())


async def _segment_answers(client, evidence: dict) -> bool:
    variant_url = _best_variant(evidence)
    headers = {"User-Agent": UA, "Accept": "*/*"}
    if evidence.get("referer"):
        headers["Referer"] = evidence["referer"]
    if variant_url:
        try:
            res = await client.get(variant_url, headers=headers)
            if res.status_code != 200 or "#EXTM3U" not in res.text:
                return False
            variant = res.text
        except Exception:
            return False
    else:
        # A provider that answers a media playlist, not a master: the first
        # segment is in that playlist already.
        if evidence.get("height"):
            return False
        variant_url = evidence.get("url")
        variant = evidence.get("text") or ""
        if not variant_url or "#EXTM3U" not in variant:
            return False
    segment = next((line.strip() for line in variant.splitlines()
                    if line.strip() and not line.strip().startswith("#")), None)
    if not segment:
        return False
    segment_url = urljoin(variant_url, segment)

    def _tag_uris(tag: str) -> list:
        pattern = r"#EXT-X-" + tag + r":[^\n]*URI=\"([^\"]+)\""
        return list(dict.fromkeys(urljoin(variant_url, match.group(1))
                                  for match in re.finditer(pattern, variant)))

    # An AES-128 segment starts with 16 random-looking bytes: only the key
    # tells whether it is a real segment or garbage with a video content type.
    for key_url in (u for u in _tag_uris(r"(?:SESSION-)?KEY") if u.startswith("http")):
        try:
            res = await client.get(key_url, headers=headers)
            if res.status_code != 200 or len(res.content) < 16:
                return False
        except Exception:
            return False
    for map_url in (u for u in _tag_uris("MAP") if u.startswith("http")):
        try:
            res = await client.get(map_url, headers=headers)
            if res.status_code != 200 or len(res.content) < 16:
                return False
        except Exception:
            return False
    try:
        async with client.stream(
            "GET", segment_url, headers={**headers, "Range": "bytes=0-2047"}
        ) as res:
            if res.status_code not in (200, 206):
                return False
            content_type = (res.headers.get("content-type") or "").lower()
            body = bytearray()
            async for chunk in res.aiter_bytes():
                body.extend(chunk)
                if len(body) >= 2048:
                    break
            body = bytes(body[:2048])
    except Exception:
        return False
    start = body.lstrip().lower()
    if start.startswith(b"<!doctype") or b"<html" in start[:200]:
        return False
    if body.startswith(b"\x47") or b"ftyp" in body[:64]:
        return True
    if body.startswith(b"\xff\xd8\xff") or body.startswith(b"\x89PNG"):
        return False
    return len(body) >= 2048 and (
        content_type.startswith("video/") or "octet-stream" in content_type)


def _candidate(name, streams, seconds):
    return {
        "provider": name,
        "streams": streams,
        "seconds": seconds,
        "rank": RANKING.index(name) if name in RANKING else len(RANKING),
    }


async def race_watch(anilist_id: int, ep: int, audio: str = "sub",
                     preferred: str | None = None) -> dict | None:
    mods = providers()
    if not mods:
        return None
    ctx = await build_ctx(anilist_id)
    if preferred:
        pick = next((m for m in mods if m[0] == preferred), None)
        if pick is not None:
            name, streams, dt, ok = await _watch_one(pick[1], pick[0], anilist_id, ep, audio, ctx)
            record_latency(name, dt, ok)
            if ok:
                return {"provider": name, "streams": streams,
                        "requestedProvider": preferred,
                        "timings": {name: {"seconds": dt, "ok": True}},
                        "defaultProvider": default_provider()}
            mods = [m for m in mods if m[0] != preferred]
            if not mods:
                return None
    pending = {asyncio.ensure_future(_watch_one(mod, name, anilist_id, ep, audio, ctx))
               for name, mod in mods}
    timings: dict = {}
    candidates: list[dict] = []
    first_at = None
    try:
        while pending:
            timeout = None
            if first_at is not None:
                timeout = QUALITY_GRACE - (time.time() - first_at)
                if timeout <= 0:
                    break
            done, pending = await asyncio.wait(
                pending, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                name, streams, dt, ok = task.result()
                timings[name] = {"seconds": dt, "ok": ok}
                record_latency(name, dt, ok)
                if ok:
                    if first_at is None:
                        first_at = time.time()
                    candidates.append(_candidate(name, streams, dt))
    finally:

        if pending:
            try:
                done2, pending2 = await asyncio.wait(pending, timeout=QUALITY_DRAIN)
                for task in done2:
                    try:
                        name, streams, dt, ok = task.result()
                        timings[name] = {"seconds": dt, "ok": ok}
                        record_latency(name, dt, ok)
                        if ok:
                            candidates.append(_candidate(name, streams, dt))
                    except Exception:
                        pass
                pending = pending2
            finally:
                for task in pending:
                    task.cancel()
    if not candidates:
        return None
    evidence = await asyncio.gather(*[_quality_evidence(c["streams"]) for c in candidates])
    for candidate, item in zip(candidates, evidence):
        candidate["evidence"] = item
        candidate["height"] = item["height"] if item else 0
    candidates.sort(
        key=lambda c: (c["height"], len(_unique_hls(c["streams"])),
                       -c["rank"], -c["seconds"]),
        reverse=True,
    )
    # A master playlist that announces 1080p proves nothing: several providers
    # answer it with a segment that is an HTML error page. Play the first
    # segment of the best variant before believing the resolution.
    best = None
    top = [c for c in candidates if c.get("evidence")][:QUALITY_VERIFY]
    if top:
        async with httpx.AsyncClient(timeout=SEGMENT_TIMEOUT, follow_redirects=True) as client:
            answers = await asyncio.gather(
                *[_segment_answers(client, c["evidence"]) for c in top])
        for candidate, answers_ok in zip(top, answers):
            if answers_ok:
                best = candidate
                break
    if best is None:
        best = candidates[0]
    winner = {"provider": best["provider"], "streams": best["streams"]}
    winner["timings"] = timings
    winner["defaultProvider"] = default_provider()
    if preferred:
        winner["requestedProvider"] = preferred
    return winner


async def _episodes_one(mod, name, anilist_id, ctx):
    t0 = time.time()
    try:
        data = await asyncio.wait_for(mod.get_episodes(anilist_id, ctx), EPISODES_TIMEOUT)
        eps = (data.get("episodes") or {})
        count = sum(len(v) for v in eps.values() if isinstance(v, list))
        record_latency(name, time.time() - t0, count > 0, kind="episodes")
        return name, data if count > 0 else None
    except Exception as e:
        print(f"[RACE] {name} episodes failed: {str(e)[:120]}")
        record_latency(name, time.time() - t0, False, kind="episodes")
        return name, None


async def merged_episodes(anilist_id: int) -> dict | None:
    mods = providers()
    if not mods:
        return None
    ctx = await build_ctx(anilist_id)
    results = await asyncio.gather(
        *[_episodes_one(mod, name, anilist_id, ctx) for name, mod in mods])
    merged = {name: data for name, data in results if data}
    if not merged:
        return None
    return {"anilistId": anilist_id, "source": "native", "providers": merged}


def merge_subtitles(streams: list) -> list:
    seen, out = set(), []
    for s in streams or []:
        for sub in s.get("subtitles") or []:
            url = sub.get("url")
            if url and url not in seen:
                seen.add(url)
                out.append(sub)
    return out


def providers_status() -> dict:
    return {
        "defaultProvider": default_provider(),
        "ranking": RANKING,
        "available": [name for name, _ in providers()],
        "latency": latency_table(),
    }
