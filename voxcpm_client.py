"""VoxCPM Gradio API client for speech generation."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Optional

from gradio_client import Client, handle_file

logger = logging.getLogger(__name__)

DEFAULT_API_URL = "https://voxcpm.modelbest.cn"
MAX_CHUNK_CHARS = 120
PREDICT_RETRIES = 3
RETRYABLE_MARKERS = (
    "timeout",
    "timed out",
    "connection",
    "temporarily",
    "429",
    "502",
    "503",
    "504",
    "queue",
    "unavailable",
    "reset",
    "broken pipe",
)


class VoxCPMError(RuntimeError):
    """Raised when speech generation or ASR fails."""


class VoxCPMCancelled(VoxCPMError):
    """Raised when the user cancels an in-flight job."""


def _ffmpeg() -> Optional[str]:
    return shutil.which("ffmpeg")


def _run_ffmpeg(src: str, dest: Path, extra_args: list[str]) -> Optional[str]:
    src_path = Path(src)
    if not src_path.exists():
        return None
    binary = _ffmpeg()
    if not binary:
        logger.warning("ffmpeg not found; using original audio %s", src_path)
        return str(src_path)
    cmd = [binary, "-y", "-i", str(src_path), *extra_args, str(dest)]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        logger.info("ffmpeg %s -> %s", src_path, dest)
        return str(dest)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or str(exc))[-400:]
        logger.warning("ffmpeg failed: %s", detail)
        return None


def prepare_reference_audio(src: str) -> str:
    """Convert incoming clips to WAV so VoxCPM ASR/cloning can read them."""
    src_path = Path(src)
    dest_path = src_path.with_name("ref.wav")
    if src_path.resolve() == dest_path.resolve() and src_path.suffix.lower() == ".wav":
        return str(src_path)
    return _run_ffmpeg(
        src,
        dest_path,
        ["-ac", "1", "-ar", "44100", "-sample_fmt", "s16"],
    ) or str(src_path)


def audio_duration(path: str) -> float:
    """Return media duration in seconds, or 0 if unknown."""
    src = Path(path)
    if not src.exists():
        return 0.0
    binary = shutil.which("ffprobe")
    if not binary:
        return 0.0
    cmd = [
        binary,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(src),
    ]
    try:
        result = subprocess.run(cmd, check=True, capture_output=True, text=True)
        return float((result.stdout or "0").strip())
    except (subprocess.CalledProcessError, ValueError):
        return 0.0


def _volume_stats(path: str) -> tuple[Optional[float], Optional[float]]:
    binary = _ffmpeg()
    if not binary:
        return None, None
    cmd = [binary, "-i", str(path), "-af", "volumedetect", "-f", "null", "-"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    mean = max_volume = None
    for line in (result.stderr or "").splitlines():
        if "mean_volume:" in line:
            try:
                mean = float(line.split("mean_volume:")[1].split("dB")[0].strip())
            except ValueError:
                pass
        if "max_volume:" in line:
            try:
                max_volume = float(line.split("max_volume:")[1].split("dB")[0].strip())
            except ValueError:
                pass
    return mean, max_volume


def _assert_usable_audio(path: str, text: str) -> None:
    src = Path(path)
    size = src.stat().st_size if src.exists() else 0
    duration = audio_duration(path)
    mean, max_volume = _volume_stats(path)
    min_duration = max(0.45, min(len(text) * 0.06, 8.0))
    logger.info(
        "Generated audio %s size=%s duration=%.2fs mean=%s max=%s",
        src,
        size,
        duration,
        mean,
        max_volume,
    )
    if duration < min_duration:
        raise VoxCPMError(
            "官方站回傳的音檔過短（只有雜音或不到 1 秒）。請改用較長的句子，檢查逐字稿，或先關閉極致克隆再試。"
        )
    if mean is not None and mean <= -50:
        raise VoxCPMError("官方站回傳的音檔幾乎是靜音。請再試一次，或先關閉極致克隆。")
    if max_volume is not None and max_volume <= -40:
        raise VoxCPMError("官方站回傳的音檔幾乎是靜音。請再試一次，或先關閉極致克隆。")


def prepare_output_audio(src: str) -> str:
    """Export AAC/M4A with trailing silence so forwarded WhatsApp audio is not clipped."""
    src_path = Path(src)
    dest_path = src_path.with_suffix(".m4a")
    converted = _run_ffmpeg(
        src,
        dest_path,
        [
            "-ar",
            "44100",
            "-ac",
            "1",
            "-af",
            "apad=pad_dur=0.5",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
        ],
    )
    if not converted:
        return str(src_path)
    src_duration = audio_duration(src)
    out_duration = audio_duration(converted)
    if src_duration and out_duration < max(0.4, src_duration * 0.5):
        logger.warning(
            "m4a duration %.2fs is shorter than source %.2fs; sending original",
            out_duration,
            src_duration,
        )
        return str(src_path)
    return converted


def split_text(text: str, max_chars: int = MAX_CHUNK_CHARS) -> list[str]:
    cleaned = re.sub(r"\s+", " ", text).strip()
    if not cleaned:
        return []
    if len(cleaned) <= max_chars:
        return [cleaned]

    pieces = re.split(r"(?<=[。！？!?；;.\n])", cleaned)
    chunks: list[str] = []
    buf = ""
    for piece in pieces:
        piece = piece.strip()
        if not piece:
            continue
        if len(buf) + len(piece) <= max_chars:
            buf += piece
            continue
        if buf:
            chunks.append(buf)
        if len(piece) <= max_chars:
            buf = piece
            continue
        for index in range(0, len(piece), max_chars):
            chunks.append(piece[index : index + max_chars])
        buf = ""
    if buf:
        chunks.append(buf)
    return chunks


def concat_audio(paths: list[str], dest: Path) -> str:
    existing = [str(Path(path).resolve()) for path in paths if Path(path).exists()]
    if not existing:
        raise VoxCPMError("沒有可合併的音檔。")
    if len(existing) == 1:
        return existing[0]

    dest.parent.mkdir(parents=True, exist_ok=True)
    list_file = dest.with_suffix(".concat.txt")
    list_file.write_text(
        "\n".join(f"file {json.dumps(path)}" for path in existing),
        encoding="utf-8",
    )
    binary = _ffmpeg()
    if not binary:
        return existing[0]
    cmd = [binary, "-y", "-f", "concat", "-safe", "0", "-i", str(list_file), str(dest)]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        return str(dest)
    except subprocess.CalledProcessError as exc:
        logger.warning("concat failed: %s", (exc.stderr or exc)[-400:])
        raise VoxCPMError("長文本音檔合併失敗。") from exc


def cleanup_data_dir(root: Path, max_age_hours: float = 12) -> int:
    if not root.exists():
        return 0
    cutoff = time.time() - max_age_hours * 3600
    keep_names = {".gitkeep", "ref.wav", "state.json"}
    removed = 0
    for path in root.rglob("*"):
        if not path.is_file() or path.name in keep_names:
            continue
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            logger.debug("Could not delete %s", path, exc_info=True)
    if removed:
        logger.info("Cleaned %s old files under %s", removed, root)
    return removed


def _walk_result(result: Any):
    if result is None:
        return
    yield result
    if hasattr(result, "value"):
        yield from _walk_result(getattr(result, "value", None))
    if isinstance(result, (list, tuple)):
        for item in result:
            yield from _walk_result(item)
    if isinstance(result, dict):
        for value in result.values():
            yield from _walk_result(value)


def _extract_text(result: Any) -> Optional[str]:
    for item in _walk_result(result):
        if isinstance(item, str) and item.strip():
            return item.strip()
    return None


def _summarize_result(result: Any) -> str:
    if result is None:
        return "None"
    if isinstance(result, (list, tuple)):
        return "[" + ", ".join(_summarize_result(item) for item in result[:6]) + "]"
    if isinstance(result, dict):
        keys = list(result.keys())
        path = result.get("path") or result.get("name")
        size = result.get("size")
        extra = f" path={path} size={size}" if path else ""
        return f"{{{', '.join(str(key) for key in keys[:8])}}}{extra}"
    text = str(result)
    return text if len(text) <= 160 else text[:160] + "..."


def _extract_audio_path(result: Any) -> Optional[str]:
    candidates: list[Path] = []
    for item in _walk_result(result):
        values: list[str] = []
        if isinstance(item, dict):
            for key in ("path", "name"):
                value = item.get(key)
                if isinstance(value, str) and value and not value.startswith("http"):
                    values.append(value)
        elif isinstance(item, str) and item and not item.startswith("http"):
            values.append(item)
        for value in values:
            path = Path(value)
            if path.is_file() and path.stat().st_size > 0:
                candidates.append(path)
    if not candidates:
        return None
    candidates.sort(key=lambda path: path.stat().st_size, reverse=True)
    return str(candidates[0])


def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, VoxCPMCancelled):
        return False
    message = str(exc).lower()
    return any(marker in message for marker in RETRYABLE_MARKERS)


def _raise_if_cancelled(cancel_event: Optional[threading.Event]) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise VoxCPMCancelled("任務已取消。")


class VoxCPMClient:
    def __init__(self, api_url: Optional[str] = None, download_dir: Optional[str] = None) -> None:
        self.api_url = api_url or os.getenv("VOXCPM_API_URL", DEFAULT_API_URL)
        self.download_dir = Path(download_dir or os.getenv("DATA_DIR", "data"))
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self._client: Optional[Client] = None
        self._lock = threading.Lock()

    def _get_client(self) -> Client:
        if self._client is None:
            logger.info("Connecting to VoxCPM API: %s", self.api_url)
            self._client = Client(self.api_url, download_files=str(self.download_dir))
        return self._client

    def reset(self) -> None:
        self._client = None

    def _predict(self, api_name: str, error_prefix: str, cancel_event: Optional[threading.Event] = None, **kwargs):
        last_error: Exception | None = None
        for attempt in range(1, PREDICT_RETRIES + 1):
            _raise_if_cancelled(cancel_event)
            try:
                with self._lock:
                    _raise_if_cancelled(cancel_event)
                    return self._get_client().predict(api_name=api_name, **kwargs)
            except VoxCPMCancelled:
                self.reset()
                raise
            except Exception as exc:
                last_error = exc
                logger.warning("VoxCPM %s attempt %s/%s failed: %s", api_name, attempt, PREDICT_RETRIES, exc)
                self.reset()
                if attempt >= PREDICT_RETRIES or not _is_retryable(exc):
                    break
                time.sleep(2 * attempt)
                _raise_if_cancelled(cancel_event)
        logger.exception("VoxCPM %s failed", api_name)
        raise VoxCPMError(f"{error_prefix}：{last_error}") from last_error

    def transcribe(self, audio_path: str, cancel_event: Optional[threading.Event] = None) -> str:
        _raise_if_cancelled(cancel_event)
        wav_path = prepare_reference_audio(audio_path)
        result = self._predict(
            "/_run_asr_if_needed",
            "參考音逐字稿辨識失敗",
            cancel_event=cancel_event,
            checked=True,
            audio_path=handle_file(wav_path),
        )
        logger.info("ASR raw result type=%s value=%r", type(result).__name__, result)
        text = _extract_text(result)
        if not text:
            raise VoxCPMError("沒有辨識到參考音逐字稿，請改用較清楚的語音，或按「修改逐字稿」手動輸入。")
        return text

    def _generate_one(
        self,
        text: str,
        user_id: str,
        ref_wav: Optional[str],
        control_instruction: str,
        cfg_value: float,
        dit_steps: int,
        do_normalize: bool,
        denoise: bool,
        use_prompt_text: bool,
        prompt_text_value: str,
        cancel_event: Optional[threading.Event] = None,
    ) -> str:
        result = self._predict(
            "/generate",
            "語音生成失敗",
            cancel_event=cancel_event,
            text=text,
            control_instruction="" if use_prompt_text else (control_instruction or ""),
            ref_wav=handle_file(ref_wav) if ref_wav else None,
            use_prompt_text=use_prompt_text,
            prompt_text_value=prompt_text_value or "",
            cfg_value=cfg_value,
            do_normalize=do_normalize,
            denoise=denoise,
            dit_steps=dit_steps,
            user_id=user_id,
        )
        logger.info("generate result %s", _summarize_result(result))
        audio_path = _extract_audio_path(result)
        if not audio_path:
            raise VoxCPMError("VoxCPM 沒有回傳音檔。")
        if not Path(audio_path).exists():
            raise VoxCPMError(f"找不到生成的音檔：{audio_path}")
        return audio_path

    def _generate_usable(
        self,
        text: str,
        user_id: str,
        ref_wav: Optional[str],
        control_instruction: str,
        cfg_value: float,
        dit_steps: int,
        do_normalize: bool,
        denoise: bool,
        use_prompt_text: bool,
        prompt_text_value: str,
        cancel_event: Optional[threading.Event] = None,
    ) -> str:
        last_error: Exception | None = None
        attempts = [
            (use_prompt_text, prompt_text_value, "first"),
            (use_prompt_text, prompt_text_value, "retry"),
        ]
        if use_prompt_text:
            attempts.append((False, "", "controllable clone fallback"))
        for prompt_on, prompt_text, label in attempts:
            _raise_if_cancelled(cancel_event)
            try:
                logger.info("Generate attempt (%s) ultimate=%s", label, prompt_on)
                audio_path = self._generate_one(
                    text,
                    user_id,
                    ref_wav,
                    control_instruction,
                    cfg_value,
                    dit_steps,
                    do_normalize,
                    denoise,
                    prompt_on,
                    prompt_text,
                    cancel_event,
                )
                _assert_usable_audio(audio_path, text)
                return audio_path
            except VoxCPMCancelled:
                raise
            except VoxCPMError as exc:
                last_error = exc
                logger.warning("Unusable generation (%s): %s", label, exc)
                self.reset()
        raise last_error or VoxCPMError("語音生成失敗。")

    def generate(
        self,
        text: str,
        user_id: str,
        ref_wav: Optional[str] = None,
        control_instruction: str = "",
        cfg_value: float = 2.0,
        dit_steps: int = 10,
        do_normalize: bool = False,
        denoise: bool = False,
        use_prompt_text: bool = False,
        prompt_text_value: str = "",
        cancel_event: Optional[threading.Event] = None,
    ) -> str:
        _raise_if_cancelled(cancel_event)
        if ref_wav:
            ref_wav = prepare_reference_audio(ref_wav)
        chunks = split_text(text)
        if not chunks:
            raise VoxCPMError("沒有可合成的文字。")

        paths = []
        for index, chunk in enumerate(chunks, 1):
            _raise_if_cancelled(cancel_event)
            logger.info("Generating chunk %s/%s (%s chars)", index, len(chunks), len(chunk))
            paths.append(
                self._generate_usable(
                    chunk,
                    user_id,
                    ref_wav,
                    control_instruction,
                    cfg_value,
                    dit_steps,
                    do_normalize,
                    denoise,
                    use_prompt_text,
                    prompt_text_value,
                    cancel_event,
                )
            )
        if len(paths) == 1:
            return paths[0]
        merged = self.download_dir / f"{user_id}-{int(time.time())}-merged.wav"
        return concat_audio(paths, merged)
