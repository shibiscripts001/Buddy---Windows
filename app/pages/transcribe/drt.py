#!/usr/bin/env python3
"""
Subtitle tracks written straight into a Resolve timeline file (.drt) - how
Transcribe gives every language its own subtitle track. No Qt, no Resolve.

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
    try:
        with zipfile.ZipFile(src) as z:
            entries = [(info.filename, z.read(info.filename)) for info in z.infolist()]
    except (OSError, zipfile.BadZipFile) as exc:
        raise DrtError(f"Resolve's timeline export couldn't be read: {exc}") from None
    sequences = [name for name, _ in entries if name.startswith("SeqContainer/") and name.endswith(".xml")]
    if len(sequences) != 1:
        raise DrtError("The exported timeline isn't in a form Buddy knows – update Buddy, or add the "
                       "subtitle files by hand.")
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in entries:
            if name == sequences[0]:
                data = replace_subtitle_tracks(data.decode("utf-8"), tracks, start_frame, fps).encode("utf-8")
            z.writestr(name, data)
