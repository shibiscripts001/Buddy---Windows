#!/usr/bin/env python3
"""
Subtitle tracks written straight into a Resolve timeline file (.drt) - how
Transcribe gives every language its own subtitle track - and video clips
moved onto tracks of their own there (Word-by-word's Stack, restack_video_clips
below). No Qt, no Resolve.

Why a file: Resolve's scripting places subtitles only on the track it has
picked as the destination (normally track 1). A new track inserted at the
top pushes the existing subtitles down, but the destination moves with
them; locking or disabling that track makes placement fail rather than
move; and subtitles added to a track that already has some go after its
end, not at their times. Placing subtitles with a full clipInfo (track,
record frame) crashed Resolve 21.1. So instead (resolve_ext.py):

  1. the current timeline is exported as .drt (Resolve's own format),
  2. with_subtitle_tracks() swaps its subtitle tracks for one per language,
  3. the result is imported as a NEW timeline, bound to the same Media
     Pool clips. The original timeline is never touched.

Measured on Resolve 21.1 (a 404-subtitle timeline round-trips exactly,
added tracks come in named and at the right frames). What the file holds:

  SeqContainer/<id>.xml
    <SubtitleTrackVec>                 one <Element><Sm2TiTrack> per track
      <Sm2TiTrack DbId=uuid>
        <FieldsBlob>TRACK_BLOB</...>   the same for every subtitle track
        <Type>2</Type> <SubType>0</SubType> <Sequence>seq uuid</Sequence>
        <Items> <Element><Sm2TiGenerator DbId=uuid> per subtitle:
            <Name>text as HTML, lines joined by "<br>"</Name>
            <Start>record frame, absolute</Start> <Duration>frames</Duration>
            <FieldsBlob>item_blob(start)</FieldsBlob>
        <UserDefinedName>the track's name</UserDefinedName>

An item's FieldsBlob is a 4-byte count and 4-byte length, then protobuf:
field 160 = 5, field 21 = the start frame again (a varint), field 2 = a
qCompress'd default style - decoded from Resolve's own export, and the
tests check item_blob() rebuilds it byte for byte.
"""

from __future__ import annotations

import html
import re
import uuid
import zipfile
from dataclasses import dataclass

TRACK_BLOB = ("000000010000000200000012004e0075006d004c006100790065007200730000000200000000000000003e00450078"
              "0063006c0075006400650054007200610063006b00460072006f006d00530065007100750065006e0063006500430061"
              "006300680069006e0067000000010001")
_ITEM_STYLE = bytes.fromhex("00000024789c6366606420040000b20005")
_SUBTITLE_VEC = re.compile(r"<SubtitleTrackVec>.*?</SubtitleTrackVec>|<SubtitleTrackVec/>", re.S)


class DrtError(RuntimeError):
    """The exported timeline isn't in the shape this module knows."""


@dataclass
class Track:
    name: str
    cues: list   # (start seconds, end seconds, text) from the timeline's start


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        low, n = n & 0x7F, n >> 7
        out.append(low | (0x80 if n else 0))
        if not n:
            return bytes(out)


def item_blob(start: int) -> str:
    payload = b"\x80\x0a\x05\xa8\x01" + _varint(start) + b"\x12" + bytes([len(_ITEM_STYLE)]) + _ITEM_STYLE
    return (b"\x00\x00\x00\x02" + len(payload).to_bytes(4, "big") + payload).hex()


def to_frames(cues, start_frame: int, fps: float) -> list[tuple[int, int, str]]:
    """(record frame, duration, text) per cue, in order, never overlapping
    (a subtitle track can't hold two at once) and at least a frame long."""
    out = []
    for start, end, text in sorted(cues, key=lambda c: c[0]):
        text = (text or "").strip()
        if not text:
            continue
        first = start_frame + round(max(0.0, start) * fps)
        last = start_frame + round(max(0.0, end) * fps)
        if out:
            prev_start, prev_len, prev_text = out[-1]
            if first < prev_start + prev_len:   # overlaps the one before: that one ends here
                out[-1] = (prev_start, max(1, first - prev_start), prev_text)
                first = max(first, prev_start + out[-1][1])
        out.append((first, max(1, last - first), text))
    return out


def _item_xml(start: int, duration: int, text: str) -> str:
    # The name is HTML to Resolve (a line break is "<br>", "&lt;3" shows as
    # "<3", a bare "<3" is taken for a tag and dropped) - escaped for that,
    # then again for the XML it sits in.
    name = "<br>".join(html.escape(line.strip(), quote=False) for line in text.split("\n"))
    return (f'     <Element>\n'
            f'      <Sm2TiGenerator DbId="{uuid.uuid4()}">\n'
            f'       <FieldsBlob>{item_blob(start)}</FieldsBlob>\n'
            f'       <PrettyType>Subtitle</PrettyType>\n'
            f'       <Name>{html.escape(name, quote=False)}</Name>\n'
            f'       <Start>{start}</Start>\n'
            f'       <Duration>{duration}</Duration>\n'
            f'       <LinkedItemSync/>\n'
            f'       <WasDisbanded>false</WasDisbanded>\n'
            f'       <MarkersBA/>\n'
            f'       <UiMemento>0</UiMemento>\n'
            f'       <Flags>0</Flags>\n'
            f'       <PriorityIndex>0</PriorityIndex>\n'
            f'       <EffectFiltersBA/>\n'
            f'       <ImportExportMetadataBA/>\n'
            f'       <RenderTextEnabled>true</RenderTextEnabled>\n'
            f'       <RenderTextGanged>true</RenderTextGanged>\n'
            f'       <RenderTextPrefixed>true</RenderTextPrefixed>\n'
            f'       <In/>\n'
            f'      </Sm2TiGenerator>\n'
            f'     </Element>\n')


def track_xml(sequence: str, name: str, items) -> str:
    body = "".join(_item_xml(*item) for item in items)
    return (f'  <Element>\n'
            f'   <Sm2TiTrack DbId="{uuid.uuid4()}">\n'
            f'    <FieldsBlob>{TRACK_BLOB}</FieldsBlob>\n'
            f'    <Type>2</Type>\n'
            f'    <SubType>0</SubType>\n'
            f'    <Flags>0</Flags>\n'
            f'    <Sequence>{sequence}</Sequence>\n'
            f'    <Items>\n{body}    </Items>\n'
            f'    <FusionCompHolderItems/>\n'
            f'    <UserDefinedName>{html.escape(name, quote=False)}</UserDefinedName>\n'
            f'    <LayersVec/>\n'
            f'   </Sm2TiTrack>\n'
            f'  </Element>\n')


def replace_subtitle_tracks(xml: str, tracks: list[Track], start_frame: int, fps: float) -> str:
    """The sequence XML with its subtitle tracks replaced by `tracks`."""
    found = re.search(r"<Sequence>([^<]+)</Sequence>", xml)
    if found is None or not _SUBTITLE_VEC.search(xml):
        raise DrtError("The exported timeline isn't in a form Buddy knows – update Buddy, or add the "
                       "subtitle files by hand.")
    block = "".join(track_xml(found.group(1), t.name, to_frames(t.cues, start_frame, fps)) for t in tracks)
    vec = f"<SubtitleTrackVec>\n{block} </SubtitleTrackVec>" if block else "<SubtitleTrackVec/>"
    return _SUBTITLE_VEC.sub(lambda _m: vec, xml, count=1)


def with_subtitle_tracks(src: str, dst: str, tracks: list[Track], start_frame: int, fps: float):
    """Copies the .drt at src to dst with its subtitle tracks replaced by
    one per Track - the rest of the timeline exactly as exported."""
    _rewrite_sequence(src, dst, lambda xml: replace_subtitle_tracks(xml, tracks, start_frame, fps),
                      "add the subtitle files")


# ------------------------------------------------------------ video clips onto tracks of their own --
#
# Word-by-word's Stack (text_animator/text_plus.py): chosen video clips each moved to a
# track and lengthened, the rest of the timeline as exported. A Text+ clip is an
# <Sm2TiVideoClip> with its whole Fusion composition inside; its <Start> is also in its
# FieldsBlob, which is why clips here keep theirs and only change track and <Duration>.
# Measured on 21.1: added tracks come in as the template's copies, and a lengthened Text+
# gets its composition lengthened by Resolve (0-119 became 0-599) and draws to the end.

_VIDEO_VEC = re.compile(r"<VideoTrackVec>(.*?)</VideoTrackVec>", re.S)
_ITEMS = re.compile(r"<Items>(.*?)</Items>|<Items/>", re.S)


def _elements(body: str) -> list[str]:
    """The top-level <Element>...</Element> blocks in `body`, whatever is nested in them."""
    blocks, depth, begin = [], 0, None
    for tag in re.finditer(r"<(/?)Element>", body):
        if not tag.group(1):
            if depth == 0:
                begin = tag.start()
            depth += 1
        else:
            depth -= 1
            if depth == 0 and begin is not None:
                blocks.append(body[begin:tag.end()])
    return blocks


def _start_of(block: str):
    found = re.search(r"<Start>(-?\d+)</Start>", block)
    return int(found.group(1)) if found else None


def restack_video_clips(xml: str, moves: dict) -> str:
    """The sequence XML with each clip in `moves` - {(video track, start frame): (new
    track, new duration)}, 1-based tracks - on its new track with its new length. Tracks
    past the last are added, copies of video track 1 with nothing on them and no name.
    Everything else on every track stays as it was."""
    vec = _VIDEO_VEC.search(xml)
    tracks = _elements(vec.group(1)) if vec else []
    if not tracks or not all(_ITEMS.search(t) for t in tracks):
        raise DrtError("The exported timeline isn't in a form Buddy knows – update Buddy, or stack the clips by hand.")
    kept, moved, found = [], {}, set()
    for index, track in enumerate(tracks, start=1):
        items = _ITEMS.search(track)
        blocks = []
        for block in _elements(items.group(1) or ""):
            key = (index, _start_of(block))
            if key in moves and "<Sm2TiVideoClip " in block and key not in found:
                found.add(key)
                new_track, duration = moves[key]
                block = re.sub(r"<Duration>\d+</Duration>", f"<Duration>{int(duration)}</Duration>", block, count=1)
                moved.setdefault(new_track, []).append(block)
            else:
                blocks.append(block)
        kept.append(blocks)
    missing = set(moves) - found
    if missing:
        raise DrtError(f"{len(missing)} of the clips weren't in the exported timeline – nothing was changed.")
    blank = re.sub(r"<UserDefinedName>.*?</UserDefinedName>", "<UserDefinedName/>", tracks[0], count=1, flags=re.S)
    while len(kept) < max(moved, default=0):
        kept.append([])
        tracks.append(re.sub(r'(<Sm2TiTrack DbId=")[^"]+', lambda m: m.group(1) + str(uuid.uuid4()), blank, count=1))
    out = []
    for index, (track, blocks) in enumerate(zip(tracks, kept), start=1):
        blocks = sorted(blocks + moved.get(index, []), key=lambda b: _start_of(b) or 0)
        body = f"<Items>\n     {chr(10).join('     ' + b.strip() for b in blocks).strip()}\n    </Items>" if blocks else "<Items/>"
        out.append(_ITEMS.sub(lambda _m: body, track, count=1).strip())
    return xml[:vec.start(1)] + "\n  " + "\n  ".join(out) + "\n " + xml[vec.end(1):]


def _rewrite_sequence(src: str, dst: str, transform, what: str):
    """Copies the .drt at src to dst with its one sequence's XML passed through transform."""
    try:
        with zipfile.ZipFile(src) as z:
            entries = [(info.filename, z.read(info.filename)) for info in z.infolist()]
    except (OSError, zipfile.BadZipFile) as exc:
        raise DrtError(f"Resolve's timeline export couldn't be read: {exc}") from None
    sequences = [name for name, _ in entries if name.startswith("SeqContainer/") and name.endswith(".xml")]
    if len(sequences) != 1:
        raise DrtError(f"The exported timeline isn't in a form Buddy knows – update Buddy, or {what} by hand.")
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in entries:
            if name == sequences[0]:
                data = transform(data.decode("utf-8")).encode("utf-8")
            z.writestr(name, data)


def with_video_clips_restacked(src: str, dst: str, moves: dict):
    """Copies the .drt at src to dst with the clips in `moves` restacked (restack_video_clips)."""
    _rewrite_sequence(src, dst, lambda xml: restack_video_clips(xml, moves), "stack the clips")
