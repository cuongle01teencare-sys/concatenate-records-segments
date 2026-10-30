"""
Dựng MỘT video cho cả buổi học từ chunk capture: camera học sinh xếp lưới chạy
đồng bộ theo giờ thật + tiếng mentor và tiếng học sinh trộn chung, xuất MP4
(H.264/AAC) để app trên iOS/Android đều phát được.

Khác cách ghép cũ (_from_c.webm): bản cũ nối đuôi video từng luồng nên buổi 1 giờ
ra file 6 giờ, không tiếng, phụ huynh chỉ thấy lần lượt từng bạn khác.

Dữ liệu vào (extension ext-ggmeet, mỗi mảnh là output MediaRecorder 3 giây):

    captures/<meet>/<segment_b>/participants/<c>/video/chunk-<seq>-<epoch_ms>-<stream>-<n>.webm
    captures/<meet>/<segment_b>/mentor-audio/chunk-....webm      (mic mentor)
    captures/<meet>/<segment_b>/shared-audio/chunk-....webm      (tiếng phía học sinh, Meet gửi 3 luồng)

Những điều đo được trên dữ liệu thật (fji-qahg-tbd 28/10/2026, 11 capture session):

- Một buổi bị chia thành nhiều segment_b (service worker restart, mentor tải lại tab)
  nhưng recorder trong trang chạy tiếp, nên mảnh của CÙNG một stream nằm rải qua
  nhiều segment_b. Phải gom theo stream id trên toàn buổi rồi xếp theo epoch.
- Nhiều stream không có mảnh mở đầu (EBML header) nào trên GCS: 2/6 luồng video của
  buổi trên, một luồng 140 MB. Mượn header của luồng cùng loại rồi nối byte thì giải
  mã được (VP8 có keyframe khoảng 5 giây một lần).
- Mỗi đoạn được đặt lên trục giờ thật chung theo epoch_ms trong tên mảnh (lúc extension
  gửi mảnh), KHÔNG xếp nối tiếp nhau: học sinh vào muộn thì ô và tiếng của bạn đó cũng
  bắt đầu muộn đúng chừng ấy. Timecode WebM không đi kèm giờ thật và bị nén khi không có
  khung (xem split_stream), nên phải cắt đoạn ở chỗ độ lệch đổi.
- mentor-audio có lúc 2 recorder chạy song song; mỗi giây lấy bên to hơn (xem
  merge_by_loudest). Tiếng phía học sinh (3 luồng Meet) thì trộn chung.
"""

import base64
import math
import re
import shutil
import subprocess
import tempfile

from array import array
from collections import defaultdict
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date as date_cls, datetime, timedelta, timezone
from pathlib import Path


EBML_MAGIC = b"\x1a\x45\xdf\xa3"
CLUSTER_ID = b"\x1f\x43\xb6\x75"

# Header (EBML + Segment + Info + Tracks, tới trước Cluster đầu) cắt từ mảnh mở đầu
# của Chrome MediaRecorder. Chỉ dùng khi cả buổi không có mảnh mở đầu nào cùng loại.
FALLBACK_VP8_HEADER = base64.b64decode(
    "GkXfo59ChoEBQveBAULygQRC84EIQoKEd2VibUKHgQRChYECGFOAZwH/////////FUmpZpkq17GD"
    "D0JATYCGQ2hyb21lV0GGQ2hyb21lFlSua6quqNeBAXPFh64uldQ6HqeDgQFV7oEBhoVWX1ZQOOCL"
    "sIIBQLqBtFPAgQE="
)
FALLBACK_OPUS_HEADER = base64.b64decode(
    "GkXfo59ChoEBQveBAULygQRC84EIQoKEd2VibUKHgQRChYECGFOAZwH/////////FUmpZpkq17GD"
    "D0JATYCGQ2hyb21lV0GGQ2hyb21lFlSua7+uvdeBAXPFh4EkG+80VoqDgQKGhkFfT1BVU2Oik09w"
    "dXNIZWFkAQEAAIC7AAAAAADhjbWERzuAAJ+BAWJkgSA="
)

CHUNK_NAME = re.compile(
    r"^chunk-(?P<seq>\d+)-(?P<epoch>\d+)-(?P<stream>.+)-\d+\.webm$"
)
# captures/<meet>/<segment_b>/<phần còn lại>
CHUNK_PATH = re.compile(
    r"^captures/(?P<meet>[^/]+)/(?P<segment_b>[^/]+)/"
    r"(?:participants/(?P<participant>[^/]+)/video"
    r"|(?P<audio>mentor-audio|mentor_audio|shared-audio|shared_audio))"
    r"/(?P<name>chunk-[^/]+\.webm)$"
)

VN_TZ = timezone(timedelta(hours=7))

CHUNK_MS = 3000             # recorder.start(3000) trong hook.js
GAP_SPLIT_MS = 10_000       # timecode nhảy quá ngần này (ô bị ẩn) -> tách đoạn, chỗ trống để đen
OFFSET_STEP_MS = 1500       # độ lệch đổi quá ngần này -> tách đoạn, đặt lại lên trục
MIN_TILE_SECONDS = 20       # luồng video tổng cộng ngắn hơn thì không chiếm một ô
MAX_TILES = 16
MAX_DURATION_S = 3 * 3600
FPS = 10
CANVAS_W, CANVAS_H = 1280, 720
AUDIO_RATE = 16000
TILE_BG = "0x202124"


@dataclass
class Chunk:
    path: Path
    kind: str           # 'video' | 'mentor' | 'shared'
    stream: str
    epoch_ms: int
    seq: int
    participant: str = ""

    header: bool = False
    timecodes: list[int] = field(default_factory=list)


@dataclass
class Piece:
    """Một đoạn liền mạch của một stream: nối byte được thành một file WebM."""

    kind: str
    stream: str
    chunks: list[Chunk]
    borrowed_header: bool
    start_ms: float = 0.0   # giờ thật (epoch ms) của khung đầu
    end_ms: float = 0.0
    file: Path | None = None

    @property
    def seconds(self) -> float:
        return max(0.0, (self.end_ms - self.start_ms) / 1000)


@dataclass
class CompositeResult:
    output_file: Path | None
    duration_s: float
    tiles: int
    audio_tracks: int
    window_start_ms: float
    notes: list[str]


# ---------------------------------------------------------------------------
# EBML: chỉ cần nhận ra header và đọc Timecode của Cluster
# ---------------------------------------------------------------------------

def _read_vint(data: bytes, pos: int) -> tuple[int, int] | None:
    if pos >= len(data):
        return None
    first = data[pos]
    length = 1
    mask = 0x80
    while length <= 8 and not first & mask:
        mask >>= 1
        length += 1
    if length > 8 or pos + length > len(data):
        return None
    value = first & (mask - 1)
    for i in range(1, length):
        value = (value << 8) | data[pos + i]
    return value, length


def cluster_timecodes(data: bytes) -> list[int]:
    """Timecode (ms) của mọi Cluster bắt đầu trong mảnh. Byte trùng ID ngẫu nhiên
    bị loại vì phần tử con đầu tiên của Cluster thật luôn là Timecode (0xE7)."""
    out: list[int] = []
    index = data.find(CLUSTER_ID)
    while index != -1:
        size = _read_vint(data, index + 4)
        if size is not None:
            pos = index + 4 + size[1]
            if pos < len(data) and data[pos] == 0xE7:
                length = _read_vint(data, pos + 1)
                if length is not None and 1 <= length[0] <= 8:
                    start = pos + 1 + length[1]
                    end = start + length[0]
                    if end <= len(data):
                        out.append(int.from_bytes(data[start:end], "big"))
        index = data.find(CLUSTER_ID, index + 4)
    return out


def header_prefix(data: bytes) -> bytes | None:
    if not data.startswith(EBML_MAGIC):
        return None
    index = data.find(CLUSTER_ID)
    return data[:index] if index > 0 else None


# ---------------------------------------------------------------------------
# Thu thập mảnh
# ---------------------------------------------------------------------------

def parse_chunk_path(blob_name: str) -> dict[str, str] | None:
    match = CHUNK_PATH.match(blob_name)
    if match is None:
        return None
    name = CHUNK_NAME.match(match.group("name"))
    if name is None:
        return None
    audio = match.group("audio") or ""
    kind = "video" if match.group("participant") else (
        "mentor" if audio.startswith("mentor") else "shared"
    )
    return {
        "meet": match.group("meet"),
        "segment_b": match.group("segment_b"),
        "kind": kind,
        "participant": match.group("participant") or "",
        "stream": name.group("stream"),
        "epoch": name.group("epoch"),
        "seq": name.group("seq"),
    }


def vn_day_window_ms(vn_date: str) -> tuple[int, int]:
    day = date_cls.fromisoformat(vn_date)
    start = datetime(day.year, day.month, day.day, tzinfo=VN_TZ)
    return (
        int(start.timestamp() * 1000),
        int((start + timedelta(days=1)).timestamp() * 1000),
    )


def capture_prefixes(meet_code: str, vn_date: str) -> list[str]:
    """Tên segment_b mang giờ UTC: ngày VN trải trên hai ngày UTC."""
    day = date_cls.fromisoformat(vn_date)
    return [
        f"captures/{meet_code}/session-{(day - timedelta(days=1)).isoformat()}T",
        f"captures/{meet_code}/session-{day.isoformat()}T",
    ]


def download_class_chunks(
    *,
    bucket,
    meet_code: str,
    vn_date: str,
    destination: Path,
    workers: int = 16,
) -> list[Chunk]:
    window = vn_day_window_ms(vn_date)
    wanted: list[tuple[object, dict[str, str]]] = []

    for prefix in capture_prefixes(meet_code, vn_date):
        for blob in bucket.list_blobs(prefix=prefix):
            info = parse_chunk_path(blob.name or "")
            if info is None or not (blob.size or 0):
                continue
            if not window[0] <= int(info["epoch"]) < window[1]:
                continue
            wanted.append((blob, info))

    print(f"[composite] {meet_code} {vn_date}: {len(wanted)} chunk(s) to download")

    def fetch(item: tuple[object, dict[str, str]]) -> Chunk:
        blob, info = item
        local = destination / info["segment_b"] / (blob.name or "").rsplit("/", 1)[-1]
        if info["kind"] == "video":
            local = destination / info["segment_b"] / info["participant"] / local.name
        local.parent.mkdir(parents=True, exist_ok=True)
        blob.download_to_filename(str(local))
        return make_chunk(local, info)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(fetch, wanted))


def make_chunk(path: Path, info: dict[str, str]) -> Chunk:
    return Chunk(
        path=path,
        kind=info["kind"],
        stream=info["stream"],
        epoch_ms=int(info["epoch"]),
        seq=int(info["seq"]),
        participant=info["participant"],
    )


def chunks_from_local_dir(root: Path, meet_code: str) -> list[Chunk]:
    """Thư mục tải bằng `gcloud storage rsync` (root/<segment_b>/...), để thử local."""
    chunks: list[Chunk] = []
    for file in root.rglob("chunk-*.webm"):
        relative = file.relative_to(root).as_posix()
        info = parse_chunk_path(f"captures/{meet_code}/{relative}")
        if info is not None:
            chunks.append(make_chunk(file, info))
    return chunks


# ---------------------------------------------------------------------------
# Chia stream thành đoạn và đặt lên trục thời gian
# ---------------------------------------------------------------------------

def build_pieces(chunks: Iterable[Chunk], workdir: Path) -> list[Piece]:
    by_stream: dict[tuple[str, str], list[Chunk]] = defaultdict(list)
    headers: dict[str, bytes] = {}

    for chunk in chunks:
        data = chunk.path.read_bytes()
        chunk.header = data.startswith(EBML_MAGIC)
        chunk.timecodes = cluster_timecodes(data)
        if chunk.header and chunk.kind not in headers:
            prefix = header_prefix(data)
            if prefix:
                headers[chunk.kind] = prefix
        by_stream[(chunk.kind, chunk.stream)].append(chunk)

    pieces: list[Piece] = []
    for (kind, stream), items in by_stream.items():
        items.sort(key=lambda c: (c.epoch_ms, c.seq))
        # Mượn header: của chính stream, rồi cùng loại (shared-audio có thể khác số kênh
        # với mic mentor), rồi cùng codec, cuối cùng là header cắt sẵn.
        family_header = headers.get(kind) or (
            headers.get("video") if kind == "video"
            else headers.get("mentor") or headers.get("shared")
        ) or (FALLBACK_VP8_HEADER if kind == "video" else FALLBACK_OPUS_HEADER)
        own_header = next(
            (header_prefix(c.path.read_bytes()) for c in items if c.header), None
        )
        for group in split_stream(items):
            piece = Piece(kind=kind, stream=stream, chunks=group,
                          borrowed_header=not group[0].header)
            if not place_piece(piece):
                continue
            header = None
            if piece.borrowed_header:
                header = own_header or family_header
            piece.file = workdir / "pieces" / f"{kind}-{_safe(stream)}-{group[0].epoch_ms}-{group[0].seq}.webm"
            piece.file.parent.mkdir(parents=True, exist_ok=True)
            with piece.file.open("wb") as out:
                if header:
                    out.write(header)
                for chunk in group:
                    with chunk.path.open("rb") as source:
                        shutil.copyfileobj(source, out, length=1024 * 1024)
            pieces.append(piece)

    return pieces


def split_stream(items: list[Chunk]) -> list[list[Chunk]]:
    """Cắt một stream (đã xếp theo epoch) thành các đoạn mà trong mỗi đoạn timecode
    chạy song song với giờ thật:

    - mảnh có header = recorder mới, timecode về 0;
    - timecode nhảy tới quá GAP_SPLIT_MS hoặc lùi lại;
    - độ lệch giờ thật - timecode tăng bậc. Lúc không có khung (ô bị ẩn, mic tắt)
      MediaRecorder không cho timecode chạy, nên khoảng trống bị nén mất. Đo thật
      fji-qahg-tbd 28/10: luồng video 8894 lệch dần từ 285 s lên 330 s trong 15 phút,
      một recorder mentor-audio lệch 17-27 s; một độ lệch cho cả đoạn sẽ đặt phần sau
      sớm hơn thật chừng ấy giây.
    """
    groups: list[list[Chunk]] = []
    for segment in _split_on_timecodes(items):
        groups.extend(_split_on_offset_steps(segment))
    return groups


def _split_on_timecodes(items: list[Chunk]) -> list[list[Chunk]]:
    groups: list[list[Chunk]] = []
    last_tc: int | None = None
    for chunk in items:
        first_tc = chunk.timecodes[0] if chunk.timecodes else None
        jump = (
            first_tc is not None and last_tc is not None
            and (first_tc - last_tc > GAP_SPLIT_MS or first_tc < last_tc - 2000)
        )
        if not groups or chunk.header or jump:
            groups.append([])
            if chunk.header:
                last_tc = None
        groups[-1].append(chunk)
        if chunk.timecodes:
            last_tc = chunk.timecodes[-1]
    return groups


def _split_on_offset_steps(items: list[Chunk]) -> list[list[Chunk]]:
    """d = epoch - timecode = độ lệch thật + độ trễ gửi (>= 0). Trong một recorder độ
    lệch thật chỉ tăng theo bậc, nên min của d trên MỌI mảnh từ đây trở đi là ước lượng
    sát nhất: một đợt gửi chậm (đôi khi vài chục giây) không kéo được nó lên vì mảnh
    gửi đúng giờ phía sau kéo xuống lại. Cắt ở chỗ ước lượng tăng quá OFFSET_STEP_MS."""
    sampled = [i for i, chunk in enumerate(items) if chunk.timecodes]
    if len(sampled) < 2:
        return [items]
    diffs = [items[i].epoch_ms - items[i].timecodes[0] for i in sampled]
    offsets = diffs[:]
    for k in range(len(offsets) - 2, -1, -1):
        offsets[k] = min(offsets[k], offsets[k + 1])

    groups: list[list[Chunk]] = []
    previous = 0
    reference = offsets[0]
    for k in range(1, len(offsets)):
        # So với đầu đoạn, không với mảnh liền trước: lệch tăng từng chút cũng cộng dồn.
        if offsets[k] - reference > OFFSET_STEP_MS and sampled[k] > previous:
            groups.append(items[previous:sampled[k]])
            previous = sampled[k]
            reference = offsets[k]
    groups.append(items[previous:])
    return groups


def place_piece(piece: Piece) -> bool:
    """Đặt đoạn lên giờ thật. Mảnh gửi lúc epoch chứa media trước epoch, nên
    epoch - timecode(cluster đầu của mảnh) >= độ lệch; phân vị 10% gần đúng nhất."""
    samples = [
        chunk.epoch_ms - chunk.timecodes[0]
        for chunk in piece.chunks
        if chunk.timecodes
    ]
    timecodes = [tc for chunk in piece.chunks for tc in chunk.timecodes]
    if not samples or not timecodes:
        return False
    samples.sort()
    offset = samples[int(len(samples) * 0.1)]
    piece.start_ms = offset + timecodes[0]
    piece.end_ms = offset + timecodes[-1] + CHUNK_MS
    return piece.end_ms > piece.start_ms


def _safe(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", value)[:40]


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

def _run(command: list[str], label: str) -> None:
    result = subprocess.run(
        command, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if result.returncode != 0:
        raise RuntimeError(f"{label}: ffmpeg exit {result.returncode}\n{result.stderr[-3000:]}")


def grid_geometry(count: int) -> tuple[int, int, int, int]:
    cols = max(1, math.ceil(math.sqrt(count)))
    rows = max(1, math.ceil(count / cols))
    width = CANVAS_W // cols
    height = width * 9 // 16
    if height * rows > CANVAS_H:
        height = CANVAS_H // rows
        width = height * 16 // 9
    return cols, rows, width - width % 2, height - height % 2


def render_tile(
    *,
    pieces: list[Piece],
    window_start_ms: float,
    duration_s: float,
    width: int,
    height: int,
    output: Path,
) -> None:
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"color=c={TILE_BG}:s={width}x{height}:r={FPS}:d={duration_s:.3f}",
    ]
    filters = []
    last = "0:v"
    for index, piece in enumerate(pieces, start=1):
        command += ["-err_detect", "ignore_err", "-fflags", "+genpts+discardcorrupt", "-i", str(piece.file)]
        offset = (piece.start_ms - window_start_ms) / 1000
        filters.append(
            f"[{index}:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,"
            f"setpts=PTS-STARTPTS+{offset:.3f}/TB[s{index}]"
        )
        filters.append(f"[{last}][s{index}]overlay=eof_action=pass:shortest=0[o{index}]")
        last = f"o{index}"
    command += [
        "-filter_complex", ";".join(filters) if filters else "[0:v]null[out]",
        "-map", f"[{last}]" if filters else "[out]",
        "-r", str(FPS), "-t", f"{duration_s:.3f}",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "24", "-pix_fmt", "yuv420p",
        str(output),
    ]
    _run(command, f"tile {output.stem}")


def decode_audio_piece(piece: Piece, output: Path) -> int:
    """Giải mã thành PCM s16le mono 16 kHz, lấp lỗ theo timestamp. Trả số byte."""
    _run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-err_detect", "ignore_err", "-fflags", "+genpts+discardcorrupt",
            "-i", str(piece.file), "-vn",
            "-af", "aresample=async=1:first_pts=0",
            "-ar", str(AUDIO_RATE), "-ac", "1", "-f", "s16le", str(output),
        ],
        f"audio {piece.stream}",
    )
    return output.stat().st_size if output.exists() else 0


def render_audio_track(
    *,
    pieces: list[Piece],
    window_start_ms: float,
    duration_s: float,
    output: Path,
    workdir: Path,
) -> bool:
    """Ghi các đoạn của MỘT stream vào một file PCM theo vị trí giờ thật (chỗ trống là
    im lặng)."""
    total_bytes = int(duration_s * AUDIO_RATE) * 2
    wrote = False
    with output.open("wb") as track:
        track.truncate(total_bytes)
        for piece in sorted(pieces, key=lambda p: p.start_ms):
            raw = workdir / f"{piece.file.stem}.raw"
            try:
                size = decode_audio_piece(piece, raw)
            except RuntimeError as error:
                print(f"[composite] skip audio piece {piece.file.name}: {str(error)[:300]}")
                continue
            position = int((piece.start_ms - window_start_ms) / 1000 * AUDIO_RATE) * 2
            if position >= total_bytes or size <= 0:
                continue
            position = max(position, 0)
            with raw.open("rb") as source:
                track.seek(position)
                remaining = total_bytes - position
                while remaining > 0:
                    block = source.read(min(remaining, 1024 * 1024))
                    if not block:
                        break
                    track.write(block)
                    remaining -= len(block)
            raw.unlink(missing_ok=True)
            wrote = True
    return wrote


def merge_by_loudest(tracks: list[Path], output: Path) -> None:
    """Gộp các track mic mentor: mỗi giây lấy track to nhất, KHÔNG trộn.

    Một buổi thường có 2+ recorder mentor-audio chạy song song. Đo thật xby-qipu-tvr
    28/10: hai recorder lần lượt mang tiếng (một bên toàn mảnh im lặng 717 byte trong
    lúc bên kia có tiếng), nên chọn theo cả đoạn sẽ mất 4 phút tiếng; còn khi cả hai
    cùng thu một mic thì trộn sẽ thành tiếng vang.
    """
    block = AUDIO_RATE * 2
    sources = [track.open("rb") for track in tracks]
    try:
        with output.open("wb") as merged:
            while True:
                blocks = [source.read(block) for source in sources]
                if not any(blocks):
                    break
                merged.write(max(blocks, key=_loudness))
    finally:
        for source in sources:
            source.close()


def _loudness(pcm: bytes) -> int:
    samples = array("h", pcm[: len(pcm) - len(pcm) % 2])
    return sum(abs(value) for value in samples[::8])


def compose_final(
    *,
    tiles: list[Path],
    geometry: tuple[int, int, int, int],
    audio_tracks: list[Path],
    duration_s: float,
    output: Path,
) -> None:
    cols, _rows, width, height = geometry
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    for tile in tiles:
        command += ["-i", str(tile)]
    for track in audio_tracks:
        command += ["-f", "s16le", "-ar", str(AUDIO_RATE), "-ac", "1", "-i", str(track)]

    filters = []
    if not tiles:
        command += ["-f", "lavfi", "-i", f"color=c={TILE_BG}:s={CANVAS_W}x{CANVAS_H}:r={FPS}:d={duration_s:.3f}"]
        video_in = f"{len(audio_tracks)}:v"
        filters.append(f"[{video_in}]null[v]")
    elif len(tiles) == 1:
        filters.append(
            f"[0:v]scale={CANVAS_W}:{CANVAS_H}:force_original_aspect_ratio=decrease,"
            f"pad={CANVAS_W}:{CANVAS_H}:(ow-iw)/2:(oh-ih)/2:color=black[v]"
        )
    else:
        layout = "|".join(
            f"{(i % cols) * width}_{(i // cols) * height}" for i in range(len(tiles))
        )
        inputs = "".join(f"[{i}:v]" for i in range(len(tiles)))
        filters.append(
            f"{inputs}xstack=inputs={len(tiles)}:layout={layout}:fill=black,"
            f"pad={CANVAS_W}:{CANVAS_H}:(ow-iw)/2:(oh-ih)/2:color=black[v]"
        )

    audio_base = len(tiles)
    if audio_tracks:
        inputs = "".join(f"[{audio_base + i}:a]" for i in range(len(audio_tracks)))
        mix = (
            f"{inputs}amix=inputs={len(audio_tracks)}:duration=longest:normalize=0,"
            if len(audio_tracks) > 1 else f"{inputs}anull,"
        )
        filters.append(f"{mix}alimiter=limit=0.9[a]")
    else:
        command += ["-f", "lavfi", "-i", f"anullsrc=r={AUDIO_RATE}:cl=mono"]
        silent = len(tiles) + (0 if tiles else 1)
        filters.append(f"[{silent}:a]anull[a]")

    command += [
        "-filter_complex", ";".join(filters),
        "-map", "[v]", "-map", "[a]",
        "-t", f"{duration_s:.3f}", "-r", str(FPS),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "64k", "-ar", "44100",
        "-movflags", "+faststart",
        str(output),
    ]
    _run(command, "final")


def pack_tiles(video_pieces: list[Piece], notes: list[str]) -> list[list[Piece]]:
    """Mỗi stream video là một luồng Meet gửi về máy mentor. Meet xoay vòng người hiện
    trên cùng một luồng và học sinh vào lại thì sang stream mới, nên không gắn tên
    (tên extension bắt được hay sai) và cho các stream không trùng giờ dùng chung một ô
    để lưới không toàn ô đen."""
    by_stream: dict[str, list[Piece]] = defaultdict(list)
    for piece in video_pieces:
        by_stream[piece.stream].append(piece)
    streams = [
        items for items in by_stream.values()
        if sum(p.seconds for p in items) >= MIN_TILE_SECONDS
    ]
    streams.sort(key=lambda items: min(p.start_ms for p in items))

    tiles: list[list[Piece]] = []
    busy_until: list[float] = []
    for items in streams:
        start = min(p.start_ms for p in items)
        end = max(p.end_ms for p in items)
        for index, until in enumerate(busy_until):
            if until + 5000 <= start:
                tiles[index].extend(items)
                busy_until[index] = end
                break
        else:
            tiles.append(list(items))
            busy_until.append(end)

    if len(tiles) > MAX_TILES:
        notes.append(f"{len(tiles)} tiles needed, kept {MAX_TILES} with most video")
        tiles = sorted(tiles, key=lambda t: -sum(p.seconds for p in t))[:MAX_TILES]
        tiles.sort(key=lambda t: min(p.start_ms for p in t))
    return tiles


def build_composite(
    *,
    chunks: list[Chunk],
    workdir: Path,
    output: Path,
    delete_sources: bool = False,
) -> CompositeResult:
    """delete_sources: xoá mảnh đã tải ngay khi đã nối thành đoạn. /tmp trên Cloud Run
    nằm trong RAM (4 GiB), một buổi tải về 0,6-0,8 GB mảnh rồi lại nhân đôi khi nối."""
    notes: list[str] = []
    pieces = build_pieces(chunks, workdir)
    if delete_sources:
        for chunk in chunks:
            chunk.path.unlink(missing_ok=True)
    if not pieces:
        return CompositeResult(None, 0, 0, 0, 0, ["no decodable chunk"])

    window_start = min(p.start_ms for p in pieces)
    window_end = max(p.end_ms for p in pieces)
    duration = (window_end - window_start) / 1000
    if duration > MAX_DURATION_S:
        notes.append(f"duration {duration:.0f}s capped to {MAX_DURATION_S}s")
        duration = MAX_DURATION_S

    tile_pieces = pack_tiles([p for p in pieces if p.kind == "video"], notes)
    geometry = grid_geometry(len(tile_pieces)) if tile_pieces else (1, 1, CANVAS_W, CANVAS_H)
    tiles: list[Path] = []
    for index, items in enumerate(tile_pieces):
        tile = workdir / f"tile-{index:02d}.mkv"
        render_tile(
            pieces=sorted(items, key=lambda p: p.start_ms),
            window_start_ms=window_start,
            duration_s=duration,
            width=geometry[2],
            height=geometry[3],
            output=tile,
        )
        tiles.append(tile)
        print(f"[composite] tile {index}: streams={sorted({p.stream for p in items})} "
              f"pieces={len(items)} seconds={sum(p.seconds for p in items):.0f}")

    audio_tracks: list[Path] = []
    mentor_by_stream: dict[str, list[Piece]] = defaultdict(list)
    for piece in pieces:
        if piece.kind == "mentor":
            mentor_by_stream[piece.stream].append(piece)
    mentor_tracks: list[Path] = []
    for stream, items in mentor_by_stream.items():
        track = workdir / f"audio-mentor-{_safe(stream)}.pcm"
        if render_audio_track(pieces=items, window_start_ms=window_start,
                              duration_s=duration, output=track, workdir=workdir):
            mentor_tracks.append(track)
    if len(mentor_tracks) == 1:
        audio_tracks.append(mentor_tracks[0])
    elif mentor_tracks:
        merged = workdir / "audio-mentor.pcm"
        merge_by_loudest(mentor_tracks, merged)
        audio_tracks.append(merged)
    shared_by_stream: dict[str, list[Piece]] = defaultdict(list)
    for piece in pieces:
        if piece.kind == "shared":
            shared_by_stream[piece.stream].append(piece)
    for stream, items in shared_by_stream.items():
        track = workdir / f"audio-shared-{_safe(stream)}.pcm"
        if render_audio_track(pieces=items, window_start_ms=window_start,
                              duration_s=duration, output=track, workdir=workdir):
            audio_tracks.append(track)
    if not mentor_tracks:
        notes.append("no mentor audio")
    if not shared_by_stream:
        notes.append("no student audio")
    for piece in pieces:
        if piece.file is not None:
            piece.file.unlink(missing_ok=True)

    compose_final(
        tiles=tiles,
        geometry=geometry,
        audio_tracks=audio_tracks,
        duration_s=duration,
        output=output,
    )
    return CompositeResult(output, duration, len(tiles), len(audio_tracks), window_start, notes)


def composite_blob_name(meet_code: str, vn_date: str) -> str:
    return f"captures/{meet_code}/composite/{vn_date}/{meet_code}_{vn_date}.mp4"


def build_and_upload_class_composite(
    *,
    bucket,
    meet_code: str,
    vn_date: str,
    force: bool = False,
) -> str | None:
    """Dựng video cả buổi cho (meet_code, ngày VN) rồi upload. Trả gs:// hoặc None
    nếu buổi không có mảnh nào đọc được. Đã có bản dựng (không rỗng) thì dùng lại."""
    blob_name = composite_blob_name(meet_code, vn_date)
    existing = bucket.get_blob(blob_name)
    if existing is not None and (existing.size or 0) > 0 and not force:
        print(f"[composite] reuse gs://{bucket.name}/{blob_name}")
        return f"gs://{bucket.name}/{blob_name}"

    workdir = Path(tempfile.mkdtemp(prefix=f"composite-{meet_code}-{vn_date}-"))
    try:
        chunks = download_class_chunks(
            bucket=bucket, meet_code=meet_code, vn_date=vn_date, destination=workdir / "chunks"
        )
        if not chunks:
            print(f"[composite] {meet_code} {vn_date}: no chunk")
            return None
        output = workdir / Path(blob_name).name
        result = build_composite(chunks=chunks, workdir=workdir, output=output, delete_sources=True)
        if result.output_file is None:
            print(f"[composite] {meet_code} {vn_date}: {result.notes}")
            return None
        print(
            f"[composite] {meet_code} {vn_date}: {result.duration_s:.0f}s, tiles={result.tiles}, "
            f"audio_tracks={result.audio_tracks}, notes={result.notes}"
        )
        bucket.blob(blob_name).upload_from_filename(str(output), content_type="video/mp4")
        uri = f"gs://{bucket.name}/{blob_name}"
        print(f"[composite] uploaded {uri}")
        return uri
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
