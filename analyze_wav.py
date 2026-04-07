#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import html
import logging
import math
import struct
import sys
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional

try:
    import numpy as np
except ImportError as exc:  # pragma: no cover
    print("This tool requires numpy. Install it with: pip install numpy", file=sys.stderr)
    raise SystemExit(1) from exc


PCM_FORMAT = 0x0001
EXTENSIBLE_FORMAT = 0xFFFE
PCM_SUBFORMAT_GUID_SUFFIX = b"\x00\x00\x10\x00\x80\x00\x00\xaa\x008\x9bq"
STATUS_OK = "ok"
STATUS_SUSPECT = "suspect"
STATUS_ERROR = "error"
SUSPECT_RATIO = 0.85
MIN_EFFECTIVE_HZ = 1000.0
DEFAULT_LOG_LEVEL = "INFO"
HIGH_RESOLUTION_THRESHOLD = 48000


@dataclass
class AnalysisResult:
    directory: str
    filename: str
    container_sample_rate: Optional[int]
    estimated_sample_rate: Optional[int]
    bit_depth: Optional[int]
    status: str
    note: str


LOGGER = logging.getLogger("analyze_wav")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Analyze PCM WAV files and generate an HTML report."
    )
    parser.add_argument("--input", required=True, help="Directory containing WAV files.")
    parser.add_argument(
        "--output",
        help="Output HTML report path. Defaults to <input>/wav-analysis-report.html.",
    )
    parser.add_argument(
        "--threshold-db",
        type=float,
        default=-55.0,
        help="Relative FFT energy threshold in dB for effective bandwidth detection.",
    )
    parser.add_argument(
        "--min-ratio",
        type=float,
        default=0.12,
        help="Minimum fraction of FFT windows that must contain a frequency bin.",
    )
    parser.add_argument(
        "--max-seconds",
        type=float,
        default=30.0,
        help="Maximum total seconds analyzed per file.",
    )
    parser.add_argument(
        "--window-size",
        type=int,
        default=8192,
        help="FFT window size. Must be a power of two.",
    )
    parser.add_argument(
        "--log-level",
        default=DEFAULT_LOG_LEVEL,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log verbosity level. Defaults to INFO.",
    )
    return parser.parse_args()


def configure_logging(level_name: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level_name.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )


def log_result(result: AnalysisResult) -> None:
    LOGGER.info(
        "Completed file status=%s file=%s container_sr=%s estimated_sr=%s bit_depth=%s note=%s",
        result.status,
        result.filename,
        result.container_sample_rate,
        result.estimated_sample_rate,
        result.bit_depth,
        result.note,
    )


def render_progress(current: int, total: int, filename: str) -> None:
    if total <= 0:
        return
    width = 24
    filled = int(width * current / total)
    bar = "#" * filled + "-" * (width - filled)
    message = f"\r[{bar}] {current}/{total} {filename[:60]}"
    print(message, end="", file=sys.stderr, flush=True)


def finish_progress(total: int) -> None:
    if total > 0:
        print(file=sys.stderr, flush=True)


def find_wav_files(root: Path) -> Iterable[Path]:
    return sorted(path for path in root.rglob("*.wav") if path.is_file())


def sniff_wav_format(path: Path) -> tuple[Optional[int], Optional[int]]:
    with path.open("rb") as handle:
        if handle.read(4) != b"RIFF":
            return None, None
        handle.seek(8)
        if handle.read(4) != b"WAVE":
            return None, None

        while True:
            header = handle.read(8)
            if len(header) < 8:
                break
            chunk_id, chunk_size = struct.unpack("<4sI", header)
            chunk_data = handle.read(chunk_size)
            if chunk_size % 2 == 1:
                handle.read(1)

            if chunk_id == b"fmt ":
                if len(chunk_data) < 16:
                    return None, None
                audio_format, _channels, _sample_rate, _byte_rate, _block_align, bits = (
                    struct.unpack("<HHIIHH", chunk_data[:16])
                )
                if audio_format == EXTENSIBLE_FORMAT:
                    if len(chunk_data) < 40:
                        return None, bits
                    subformat_guid = chunk_data[24:40]
                    subformat_code = struct.unpack("<I", subformat_guid[:4])[0] & 0xFFFF
                    if subformat_guid[4:] != PCM_SUBFORMAT_GUID_SUFFIX:
                        return None, bits
                    audio_format = subformat_code
                return audio_format, bits
    return None, None


def decode_pcm_frames(raw: bytes, sample_width: int, channels: int) -> np.ndarray:
    if sample_width == 1:
        data = np.frombuffer(raw, dtype=np.uint8).astype(np.float32)
        data = (data - 128.0) / 128.0
    elif sample_width == 2:
        data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif sample_width == 3:
        bytes_array = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3)
        signed = (
            bytes_array[:, 0].astype(np.int32)
            | (bytes_array[:, 1].astype(np.int32) << 8)
            | (bytes_array[:, 2].astype(np.int32) << 16)
        )
        signed = np.where(signed & 0x800000, signed - 0x1000000, signed)
        data = signed.astype(np.float32) / 8388608.0
    elif sample_width == 4:
        data = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        raise ValueError(f"Unsupported PCM sample width: {sample_width} bytes")

    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    return data


def sample_offsets(total_frames: int, sample_rate: int, max_seconds: float) -> List[int]:
    if total_frames <= 0 or sample_rate <= 0 or max_seconds <= 0:
        return [0]

    segment_seconds = min(max_seconds, 5.0)
    segment_frames = min(total_frames, max(1, int(sample_rate * segment_seconds)))
    if total_frames <= segment_frames:
        return [0]

    desired_segments = min(6, max(1, int(math.ceil(max_seconds / 5.0))))
    if desired_segments == 1:
        return [0]

    last_start = total_frames - segment_frames
    return [int(round(i * last_start / (desired_segments - 1))) for i in range(desired_segments)]


def collect_analysis_audio(
    wav_file: wave.Wave_read, sample_rate: int, max_seconds: float
) -> np.ndarray:
    frame_count = wav_file.getnframes()
    sample_width = wav_file.getsampwidth()
    channels = wav_file.getnchannels()
    offsets = sample_offsets(frame_count, sample_rate, max_seconds)
    segment_frames = min(frame_count, max(1, int(sample_rate * min(max_seconds, 5.0))))

    chunks: List[np.ndarray] = []
    for offset in offsets:
        wav_file.setpos(offset)
        raw = wav_file.readframes(segment_frames)
        if not raw:
            continue
        decoded = decode_pcm_frames(raw, sample_width, channels)
        if decoded.size:
            chunks.append(decoded)

    if not chunks:
        return np.array([], dtype=np.float32)

    audio = np.concatenate(chunks)
    max_frames = int(sample_rate * max_seconds)
    if max_frames > 0 and audio.size > max_frames:
        audio = audio[:max_frames]
    return audio.astype(np.float32, copy=False)


def estimate_effective_sample_rate(
    samples: np.ndarray,
    sample_rate: int,
    threshold_db: float,
    min_ratio: float,
    window_size: int,
) -> Optional[int]:
    if samples.size < window_size or sample_rate <= 0:
        return None
    if window_size & (window_size - 1):
        raise ValueError("window-size must be a power of two")
    if not (0.0 < min_ratio <= 1.0):
        raise ValueError("min-ratio must be between 0 and 1")

    step = window_size // 2
    window = np.hanning(window_size).astype(np.float32)
    frequency_hits: Optional[np.ndarray] = None
    processed_frames = 0

    for start in range(0, samples.size - window_size + 1, step):
        frame = samples[start : start + window_size]
        windowed = frame * window
        spectrum = np.fft.rfft(windowed)
        magnitude = np.abs(spectrum)
        peak = float(magnitude.max())
        if peak <= 0.0:
            continue

        relative_db = 20.0 * np.log10(np.maximum(magnitude / peak, 1e-12))
        hits = relative_db >= threshold_db
        if frequency_hits is None:
            frequency_hits = hits.astype(np.int32)
        else:
            frequency_hits += hits
        processed_frames += 1

    if not processed_frames or frequency_hits is None:
        return None

    required_hits = max(1, int(math.ceil(processed_frames * min_ratio)))
    effective_bins = np.where(frequency_hits >= required_hits)[0]
    if effective_bins.size == 0:
        return None

    freqs = np.fft.rfftfreq(window_size, d=1.0 / sample_rate)
    valid_bins = effective_bins[freqs[effective_bins] >= MIN_EFFECTIVE_HZ]
    if valid_bins.size == 0:
        return None

    highest_freq = float(freqs[valid_bins[-1]])
    estimated = int(round(min(sample_rate, highest_freq * 2.0)))
    return estimated if estimated > 0 else None


def analyze_file(
    path: Path,
    root: Path,
    threshold_db: float,
    min_ratio: float,
    max_seconds: float,
    window_size: int,
) -> AnalysisResult:
    LOGGER.debug("Analyzing file path=%s", path)
    rel_parent = path.parent.relative_to(root) if path.parent != root else Path(".")
    directory = str(rel_parent).replace("\\", "/")
    audio_format, bits_from_header = sniff_wav_format(path)

    if audio_format not in {PCM_FORMAT, EXTENSIBLE_FORMAT}:
        result = AnalysisResult(
            directory=directory,
            filename=path.name,
            container_sample_rate=None,
            estimated_sample_rate=None,
            bit_depth=bits_from_header,
            status=STATUS_ERROR,
            note="fmt chunk does not describe PCM data.",
        )
        log_result(result)
        return result

    try:
        with contextlib.closing(wave.open(str(path), "rb")) as wav_file:
            sample_rate = wav_file.getframerate()
            sample_width = wav_file.getsampwidth()
            bit_depth = bits_from_header or sample_width * 8
            LOGGER.debug(
                "Loaded wav metadata path=%s sample_rate=%s sample_width=%s channels=%s frames=%s",
                path,
                sample_rate,
                sample_width,
                wav_file.getnchannels(),
                wav_file.getnframes(),
            )
            samples = collect_analysis_audio(wav_file, sample_rate, max_seconds)
    except (wave.Error, EOFError, ValueError, OSError) as exc:
        result = AnalysisResult(
            directory=directory,
            filename=path.name,
            container_sample_rate=None,
            estimated_sample_rate=None,
            bit_depth=bits_from_header,
            status=STATUS_ERROR,
            note=str(exc),
        )
        log_result(result)
        return result

    try:
        estimated = estimate_effective_sample_rate(
            samples=samples,
            sample_rate=sample_rate,
            threshold_db=threshold_db,
            min_ratio=min_ratio,
            window_size=window_size,
        )
    except ValueError as exc:
        result = AnalysisResult(
            directory=directory,
            filename=path.name,
            container_sample_rate=sample_rate,
            estimated_sample_rate=None,
            bit_depth=bit_depth,
            status=STATUS_ERROR,
            note=str(exc),
        )
        log_result(result)
        return result

    if estimated is None:
        status = STATUS_ERROR
        note = "Not enough usable audio content for spectral estimation."
    elif estimated < int(sample_rate * SUSPECT_RATIO):
        status = STATUS_SUSPECT
        note = "Estimated effective bandwidth is significantly below container sample rate."
    else:
        status = STATUS_OK
        note = "Estimated effective bandwidth is close to the container sample rate."

    result = AnalysisResult(
        directory=directory,
        filename=path.name,
        container_sample_rate=sample_rate,
        estimated_sample_rate=estimated,
        bit_depth=bit_depth,
        status=status,
        note=note,
    )
    log_result(result)
    return result


def format_rate(value: Optional[int]) -> str:
    return "-" if value is None else f"{value:,} Hz"


def format_bits(value: Optional[int]) -> str:
    return "-" if value is None else f"{value} bit"


def format_resolution_label(sample_rate: Optional[int]) -> str:
    if sample_rate is None:
        return "-"
    return "\u9ad8\u89e3\u6790" if sample_rate > HIGH_RESOLUTION_THRESHOLD else "-"


def status_label(status: str) -> str:
    return {
        STATUS_OK: "\u6b63\u5e38",
        STATUS_SUSPECT: "\u7591\u4f3c\u4f2a\u65e0\u635f",
        STATUS_ERROR: "\u975e PCM / \u65e0\u6cd5\u5206\u6790",
    }.get(status, status)


def render_html(results: List[AnalysisResult], root: Path) -> str:
    total = len(results)
    high_resolution = sum(
        1
        for item in results
        if format_resolution_label(item.container_sample_rate) == "\u9ad8\u89e3\u6790"
    )
    suspect = sum(1 for item in results if item.status == STATUS_SUSPECT)
    failed = sum(1 for item in results if item.status == STATUS_ERROR)
    rows = []

    for item in results:
        rows.append(
            "<tr>"
            f"<td>{html.escape(item.directory)}</td>"
            f"<td>{html.escape(item.filename)}</td>"
            f"<td>{html.escape(format_rate(item.container_sample_rate))}</td>"
            f"<td>{html.escape(format_rate(item.estimated_sample_rate))}</td>"
            f"<td>{html.escape(format_resolution_label(item.container_sample_rate))}</td>"
            f"<td>{html.escape(format_bits(item.bit_depth))}</td>"
            f"<td class=\"{item.status}\">{html.escape(status_label(item.status))}</td>"
            f"<td>{html.escape(item.note)}</td>"
            "</tr>"
        )

    if not rows:
        rows.append(
            "<tr><td colspan=\"8\" class=\"empty\">"
            "\u672a\u627e\u5230 WAV \u6587\u4ef6\u3002"
            "</td></tr>"
        )

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>WAV \u771f\u65e0\u635f\u9274\u522b\u62a5\u544a</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f4f1ea;
      --panel: #fffdf8;
      --ink: #1f2933;
      --muted: #5b6773;
      --grid: #d6d0c4;
      --ok: #1f7a4c;
      --suspect: #a64b00;
      --error: #a12828;
      --accent: #183a5a;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
      background:
        radial-gradient(circle at top right, rgba(24,58,90,0.10), transparent 30%),
        linear-gradient(180deg, #f7f4ee 0%, var(--bg) 100%);
      color: var(--ink);
    }}
    .wrap {{
      max-width: 1320px;
      margin: 0 auto;
      padding: 32px 24px 48px;
    }}
    .hero {{
      background: linear-gradient(135deg, rgba(24,58,90,0.98), rgba(64,107,76,0.92));
      color: white;
      border-radius: 18px;
      padding: 28px 30px;
      box-shadow: 0 18px 50px rgba(24, 58, 90, 0.18);
    }}
    .hero h1 {{
      margin: 0 0 12px;
      font-size: 28px;
      letter-spacing: 0.02em;
    }}
    .hero p {{
      margin: 6px 0;
      line-height: 1.6;
      color: rgba(255,255,255,0.88);
    }}
    .summary {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 14px;
      margin: 22px 0;
    }}
    .card {{
      background: var(--panel);
      border: 1px solid rgba(24,58,90,0.08);
      border-radius: 16px;
      padding: 16px 18px;
      box-shadow: 0 8px 26px rgba(31, 41, 51, 0.06);
    }}
    .card .label {{
      display: block;
      font-size: 12px;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0.08em;
      margin-bottom: 8px;
    }}
    .card .value {{
      font-size: 28px;
      font-weight: 700;
      color: var(--accent);
    }}
    .table-panel {{
      background: var(--panel);
      border-radius: 18px;
      overflow: hidden;
      box-shadow: 0 12px 30px rgba(31, 41, 51, 0.08);
      border: 1px solid rgba(24,58,90,0.08);
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
    }}
    thead {{
      background: rgba(24,58,90,0.08);
    }}
    th, td {{
      padding: 14px 16px;
      border-bottom: 1px solid var(--grid);
      text-align: left;
      vertical-align: top;
      font-size: 14px;
    }}
    th {{
      color: var(--accent);
      font-size: 13px;
      letter-spacing: 0.04em;
    }}
    tbody tr:nth-child(even) {{
      background: rgba(24,58,90,0.025);
    }}
    .ok {{ color: var(--ok); font-weight: 700; }}
    .suspect {{ color: var(--suspect); font-weight: 700; }}
    .error {{ color: var(--error); font-weight: 700; }}
    .empty {{
      text-align: center;
      color: var(--muted);
      padding: 28px;
    }}
    @media (max-width: 900px) {{
      .wrap {{ padding: 20px 12px 36px; }}
      .hero {{ padding: 22px 18px; }}
      th, td {{ padding: 12px; font-size: 13px; }}
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <section class="hero">
      <h1>WAV \u771f\u65e0\u635f\u9274\u522b\u62a5\u544a</h1>
      <p>\u626b\u63cf\u76ee\u5f55\uff1a{html.escape(str(root.resolve()))}</p>
      <p>\u201c\u6587\u4ef6\u91c7\u6837\u7387\u201d\u6765\u81ea WAV \u5bb9\u5668\u5143\u6570\u636e\uff1b\u201c\u5b9e\u9645\u91c7\u6837\u7387\u201d\u6765\u81ea\u9891\u8c31\u622a\u6b62\u4f30\u7b97\uff0c\u7528\u4e8e\u7b5b\u67e5\u7591\u4f3c\u5347\u91c7\u6837\u4f2a\u65e0\u635f\uff0c\u4e0d\u4ee3\u8868\u6587\u4ef6\u5934\u5b57\u6bb5\u3002</p>
    </section>
    <section class="summary">
      <div class="card">
        <span class="label">\u6587\u4ef6\u603b\u6570</span>
        <span class="value">{total}</span>
      </div>
      <div class="card">
        <span class="label">\u7591\u4f3c\u4f2a\u65e0\u635f</span>
        <span class="value">{suspect}</span>
      </div>
      <div class="card">
        <span class="label">\u9ad8\u89e3\u6790</span>
        <span class="value">{high_resolution}</span>
      </div>
      <div class="card">
        <span class="label">\u65e0\u6cd5\u5206\u6790</span>
        <span class="value">{failed}</span>
      </div>
    </section>
    <section class="table-panel">
      <table>
        <thead>
          <tr>
            <th>\u6587\u4ef6\u76ee\u5f55</th>
            <th>\u6587\u4ef6\u540d</th>
            <th>\u6587\u4ef6\u91c7\u6837\u7387</th>
            <th>\u5b9e\u9645\u91c7\u6837\u7387\uff08\u4f30\u7b97\uff09</th>
            <th>\u89c4\u683c</th>
            <th>\u91c7\u6837\u6bd4\u7279</th>
            <th>\u72b6\u6001</th>
            <th>\u8bf4\u660e</th>
          </tr>
        </thead>
        <tbody>
          {"".join(rows)}
        </tbody>
      </table>
    </section>
  </div>
</body>
</html>
"""


def main() -> int:
    args = parse_args()
    configure_logging(args.log_level)
    root = Path(args.input).expanduser()
    output = (
        Path(args.output).expanduser()
        if args.output
        else root / "wav-analysis-report.html"
    )

    if not root.exists() or not root.is_dir():
        print(f"Input directory does not exist: {root}", file=sys.stderr)
        return 2

    LOGGER.info("Starting analysis input=%s output=%s", root.resolve(), output.resolve())
    LOGGER.info(
        "Parameters threshold_db=%s min_ratio=%s max_seconds=%s window_size=%s log_level=%s",
        args.threshold_db,
        args.min_ratio,
        args.max_seconds,
        args.window_size,
        args.log_level,
    )
    wav_files = list(find_wav_files(root))
    total_files = len(wav_files)
    LOGGER.info("Discovered %s wav file(s)", total_files)

    results: List[AnalysisResult] = []
    for index, path in enumerate(wav_files, start=1):
        render_progress(index, total_files, path.name)
        LOGGER.info("Processing file %s/%s path=%s", index, total_files, path)
        results.append(
            analyze_file(
                path=path,
                root=root,
                threshold_db=args.threshold_db,
                min_ratio=args.min_ratio,
                max_seconds=args.max_seconds,
                window_size=args.window_size,
            )
        )
    finish_progress(total_files)

    output.parent.mkdir(parents=True, exist_ok=True)
    LOGGER.info("Writing HTML report path=%s", output.resolve())
    output.write_text(render_html(results, root), encoding="utf-8")
    suspect = sum(1 for item in results if item.status == STATUS_SUSPECT)
    failed = sum(1 for item in results if item.status == STATUS_ERROR)
    LOGGER.info(
        "Analysis finished total=%s suspect=%s failed=%s output=%s",
        len(results),
        suspect,
        failed,
        output.resolve(),
    )
    print(f"Generated report: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
