"""The subtitle files, in the only format a browser reads.

The providers serve .srt and .ass. A <track> element reads WebVTT and
nothing else, so the files are converted on the way out: the times and the
text of the dialogue, without the styling.

Nothing is lost that the player could have shown anyway: the styles of the
.ass are dropped, the positioning with them.
"""
import re
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

from .providers._http import HTML_HEADERS, UA

router = APIRouter()

# "00:00:12,340" (srt) or "0:00:12.34" (ass) -> "00:00:12.340" (vtt)
_SRT_TIME = re.compile(r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})")
_ASS_TIME = re.compile(r"(\d{1,2}):(\d{2}):(\d{2})[.,](\d{1,3})")
_ASS_DIALOGUE = re.compile(
    r"^Dialogue:\s*\d+,\s*([^,]+),\s*([^,]+),(.*)$", re.IGNORECASE
)
_ASS_TAG = re.compile(r"\{[^}]*\}")


def _vtt_time(value: str) -> str | None:
    match = _SRT_TIME.search(value or "")
    if not match:
        return None
    hours, minutes, seconds, millis = match.groups()
    millis = (millis + "00")[:3]
    return f"{int(hours):02d}:{minutes}:{seconds}.{millis}"


def _srt_to_vtt(text: str) -> str:
    blocks = re.split(r"\r?\n\s*\r?\n", text.strip())
    cues = []
    for block in blocks:
        lines = [line for line in block.splitlines() if line.strip()]
        if len(lines) < 2:
            continue
        start = _vtt_time(lines[0])
        end = _vtt_time(lines[1])
        if not start or not end:
            continue
        body = "\n".join(lines[2:]).strip()
        if body:
            cues.append(f"{start} --> {end}\n{body}")
    return "WEBVTT\n\n" + "\n\n".join(cues) + ("\n" if cues else "")


def _ass_to_vtt(text: str) -> str:
    cues = []
    for line in text.splitlines():
        match = _ASS_DIALOGUE.match(line.strip())
        if not match:
            continue
        start = _vtt_time(match.group(1))
        end = _vtt_time(match.group(2))
        # A Dialogue line is "Layer,Start,End,Style,Name,ML,MR,Effect,Text":
        # eight fields before the text, and the text may itself hold commas.
        rest = match.group(3).split(",", 5)
        body = _ASS_TAG.sub("", rest[5] if len(rest) > 5 else "").replace("\\N", "\n").strip()
        if start and end and body:
            cues.append(f"{start} --> {end}\n{body}")
    return "WEBVTT\n\n" + "\n\n".join(cues) + ("\n" if cues else "")


@router.get("/subtitle")
async def subtitle(url: str = Query(..., description="The subtitle file to convert")):
    """One subtitle file, as WebVTT."""
    if not url.startswith("http://") and not url.startswith("https://"):
        raise HTTPException(status_code=400, detail="bad subtitle url")
    # The files sit behind the same walls as the episodes: a plain request is
    # refused. A browser asks with its name and with the page it came from,
    # and it is answered.
    parts = urlsplit(url)
    origin = f"{parts.scheme}://{parts.netloc}/"
    headers = dict(HTML_HEADERS)
    headers["Referer"] = origin
    headers["Origin"] = origin.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            res = await client.get(url, headers=headers)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"subtitle unreachable: {exc}")
    if res.status_code != 200:
        raise HTTPException(status_code=res.status_code, detail="subtitle not found")

    text = res.text
    if url.lower().endswith(".ass") or "[Script Info]" in text[:400]:
        body = _ass_to_vtt(text)
    else:
        body = _srt_to_vtt(text)
    return Response(content=body, media_type="text/vtt")
