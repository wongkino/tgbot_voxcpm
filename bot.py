"""Telegram bot: clone a voice, then synthesize text with VoxCPM."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from dotenv import load_dotenv
from telegram import BotCommand, KeyboardButton, Message, ReplyKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from voxcpm_client import (
    VoxCPMCancelled,
    VoxCPMClient,
    VoxCPMError,
    cleanup_data_dir,
    audio_duration,
    prepare_output_audio,
    prepare_reference_audio,
    split_text,
)

load_dotenv()

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

START_TEXT = """你好，這是 VoxCPM 聲音克隆 Bot。

使用方式：
1. 傳送語音訊息或音檔作為參考音色（可跳過）
2. 輸入要唸的文字
3. Bot 會回傳合成語音

上次的參考音色會記住，重開 Bot 後仍可直接輸入文字合成。按「清除」才會忘記。

指令：
/style 年輕女性，溫柔甜美 — 設定說話風格
/skip — 不使用參考音，改用預設／風格描述
/clear — 清除已記住的聲音與風格（不影響進階參數）
/status — 查看目前設定
/denoise — 開關參考音降噪
/cfg 2.0 — CFG 引導強度（1.0–3.0）
/steps 10 — LocDiT 步數（1–50）
/ultimate — 開關極致克隆（參考音 + 逐字稿）
/transcript — 查看或修改參考音逐字稿
/cancel — 取消正在進行的辨識或生成
"""

voxcpm = VoxCPMClient(download_dir=str(DATA_DIR / "generated"))

DEFAULT_CFG = 3.0
DEFAULT_STEPS = 30
DEFAULT_DENOISE = True
DEFAULT_ULTIMATE = True
CFG_MIN, CFG_MAX = 1.0, 3.0
STEPS_MIN, STEPS_MAX = 1, 50
TELEGRAM_TEXT_LIMIT = 4096
TELEGRAM_CAPTION_LIMIT = 1024

ON_VALUES = {"on", "1", "true", "開", "开"}
OFF_VALUES = {"off", "0", "false", "關", "关"}
AUDIO_EXTS = {".wav", ".mp3", ".ogg", ".m4a", ".flac", ".opus"}
AUDIO_SUFFIX_BY_MIME = {
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/mp4": ".m4a",
    "audio/m4a": ".m4a",
    "audio/ogg": ".ogg",
}

BTN_STATUS = "狀態"
BTN_CLEAR = "清除"
BTN_SKIP = "跳過參考音"
BTN_STYLE = "設定風格"
BTN_SETTINGS = "進階設定"
BTN_HELP = "說明"
BTN_BACK = "返回"
BTN_DENOISE = "參考音降噪"
BTN_ULTIMATE = "極致克隆"
BTN_TRANSCRIPT = "修改逐字稿"
BTN_CANCEL = "取消任務"

CFG_PRESETS = {"CFG 1.5": 1.5, "CFG 2.0": 2.0, "CFG 2.5": 2.5, "CFG 3": 3.0, "CFG 3.0": 3.0}
STEPS_PRESETS = {f"步數 {n}": n for n in (10, 20, 30, 40, 50)}

STATE_DEFAULTS: dict[str, Any] = {
    "ref_path": None,
    "style": "",
    "skip": False,
    "awaiting_style": False,
    "awaiting_transcript": False,
    "in_settings": False,
    "denoise": DEFAULT_DENOISE,
    "cfg_value": DEFAULT_CFG,
    "dit_steps": DEFAULT_STEPS,
    "ultimate": DEFAULT_ULTIMATE,
    "prompt_text": "",
}
PERSIST_KEYS = (
    "ref_path",
    "style",
    "skip",
    "denoise",
    "cfg_value",
    "dit_steps",
    "ultimate",
    "prompt_text",
)


def _markup(rows: list[list[str]]) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton(label) for label in row] for row in rows],
        resize_keyboard=True,
        is_persistent=True,
    )


def command_keyboard() -> ReplyKeyboardMarkup:
    return _markup(
        [
            [BTN_STATUS, BTN_CLEAR],
            [BTN_SKIP, BTN_STYLE],
            [BTN_SETTINGS, BTN_HELP],
            [BTN_CANCEL],
        ]
    )


def settings_keyboard() -> ReplyKeyboardMarkup:
    return _markup(
        [
            [BTN_ULTIMATE, BTN_TRANSCRIPT],
            [BTN_DENOISE],
            ["CFG 1.5", "CFG 2.0"],
            ["CFG 2.5", "CFG 3"],
            ["步數 10", "步數 20", "步數 30"],
            ["步數 40", "步數 50"],
            [BTN_BACK],
        ]
    )


def _user_dir(user_id: int) -> Path:
    path = DATA_DIR / str(user_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _state_path(user_id: int) -> Path:
    return _user_dir(user_id) / "state.json"


def _load_persisted(user_id: int) -> dict[str, Any]:
    path = _state_path(user_id)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("Could not read %s", path, exc_info=True)
        return {}
    if not isinstance(data, dict):
        return {}
    return {key: data[key] for key in PERSIST_KEYS if key in data}


def _restore_ref(user_id: int, state: dict[str, Any]) -> None:
    stored = Path(state["ref_path"]) if state.get("ref_path") else None
    if stored and stored.exists():
        state["ref_path"] = str(stored.resolve())
        return
    wav = _user_dir(user_id) / "ref.wav"
    state["ref_path"] = str(wav.resolve()) if wav.exists() else None


def _save_state(state: dict[str, Any]) -> None:
    user_id = state.get("_user_id")
    if not user_id:
        return
    payload = {key: state.get(key, STATE_DEFAULTS[key]) for key in PERSIST_KEYS}
    path = _state_path(user_id)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _state(context: ContextTypes.DEFAULT_TYPE) -> dict[str, Any]:
    state = context.user_data.setdefault("vox", {})
    user_id = getattr(context, "_user_id", None)
    if user_id:
        state["_user_id"] = user_id
    if user_id and not state.get("_loaded"):
        state.update(_load_persisted(user_id))
        state["_loaded"] = True
        _restore_ref(user_id, state)
    for key, value in STATE_DEFAULTS.items():
        state.setdefault(key, value)
    return state


def _ref_status(state: dict[str, Any]) -> str:
    has_ref = bool(state.get("ref_path") and Path(state["ref_path"]).exists())
    if has_ref and state["skip"]:
        return "已記住（目前跳過使用）"
    if has_ref:
        return "已記住"
    return "未使用"


def _on_off(flag: bool) -> str:
    return "開" if flag else "關"


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


@dataclass
class UserJob:
    cancel_event: threading.Event


_user_locks: dict[int, asyncio.Lock] = {}
_user_jobs: dict[int, UserJob] = {}


def _user_lock(user_id: int) -> asyncio.Lock:
    lock = _user_locks.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        _user_locks[user_id] = lock
    return lock


async def _try_user_lock(user_id: int) -> asyncio.Lock | None:
    lock = _user_lock(user_id)
    if lock.locked():
        if user_id in _user_jobs:
            return None
        logger.warning("Clearing stale lock for user %s", user_id)
        lock = asyncio.Lock()
        _user_locks[user_id] = lock
    await lock.acquire()
    return lock


def _args_text(context: ContextTypes.DEFAULT_TYPE) -> str:
    return " ".join(context.args).strip() if context.args else ""


def _toggle_flag(current: bool, arg: str) -> bool:
    lowered = arg.lower()
    if lowered in ON_VALUES:
        return True
    if lowered in OFF_VALUES:
        return False
    return not current


def _clear_pending(state: dict[str, Any]) -> None:
    state["awaiting_style"] = False
    state["awaiting_transcript"] = False


def _params_text(state: dict[str, Any]) -> str:
    return (
        f"極致克隆：{_on_off(state['ultimate'])}\n"
        f"參考音逐字稿：{state['prompt_text'] or '尚未辨識／輸入'}\n"
        f"參考音降噪：{_on_off(state['denoise'])}\n"
        f"CFG：{state['cfg_value']}\n"
        f"LocDiT 步數：{state['dit_steps']}"
    )


def _ref_path(state: dict[str, Any]) -> str | None:
    if state["skip"]:
        return None
    return state["ref_path"]


async def _reply(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    *,
    settings: bool | None = None,
) -> None:
    if not update.message:
        return
    state = _state(context)
    if settings is not None:
        state["in_settings"] = settings
    keyboard = settings_keyboard() if state["in_settings"] else command_keyboard()
    _save_state(state)
    await update.message.reply_text(_truncate(text, TELEGRAM_TEXT_LIMIT), reply_markup=keyboard)


async def _safe_delete(message: Message | None) -> None:
    if not message:
        return
    try:
        await message.delete()
    except Exception:
        logger.debug("Could not delete status message", exc_info=True)


async def _run_job(update: Update, pending: str, fn: Callable, *args, fail_suffix: str = "", **kwargs) -> Any:
    if not update.message:
        return None
    user_id = update.effective_user.id if update.effective_user else 0
    job = UserJob(cancel_event=threading.Event())
    _user_jobs[user_id] = job
    kwargs.setdefault("cancel_event", job.cancel_event)
    status = await update.message.reply_text(pending)
    try:
        result = await asyncio.to_thread(fn, *args, **kwargs)
    except VoxCPMCancelled:
        await status.edit_text("已取消上一個任務。")
        return None
    except VoxCPMError as exc:
        await status.edit_text(f"{exc}{fail_suffix}")
        return None
    except Exception as exc:
        logger.exception("Background job failed")
        await status.edit_text(f"操作失敗：{exc}{fail_suffix}")
        return None
    finally:
        _user_jobs.pop(user_id, None)
    await _safe_delete(status)
    return result


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id if update.effective_user else 0
    job = _user_jobs.get(user_id)
    if job is None:
        await _reply(update, context, "目前沒有進行中的任務。")
        return
    job.cancel_event.set()
    voxcpm.reset()
    await _reply(update, context, "已要求取消上一個任務，請稍候。")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = _state(context)
    extra = ""
    if state.get("ref_path") and Path(state["ref_path"]).exists():
        extra = (
            "\n\n已記住上次的參考音色，但目前設為跳過。再按一次「跳過參考音」可恢復使用。"
            if state["skip"]
            else "\n\n已記住上次的參考音色，直接輸入文字即可合成。"
        )
    await _reply(update, context, START_TEXT + extra, settings=False)


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = _state(context)
    style = "極致克隆中已停用" if state["ultimate"] else (state["style"] or "未設定")
    await _reply(update, context, f"參考音色：{_ref_status(state)}\n風格：{style}\n{_params_text(state)}")


def _forget_ref_file(state: dict[str, Any]) -> None:
    paths = []
    if state.get("ref_path"):
        paths.append(Path(state["ref_path"]))
    user_id = state.get("_user_id")
    if user_id:
        paths.append(_user_dir(user_id) / "ref.wav")
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.debug("Could not delete %s", path, exc_info=True)


async def clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = _state(context)
    _forget_ref_file(state)
    state["ref_path"] = None
    state["style"] = ""
    state["skip"] = False
    state["prompt_text"] = ""
    _clear_pending(state)
    await _reply(
        update,
        context,
        "已清除參考音色、風格與逐字稿。進階參數（含極致克隆開關）維持不變。",
        settings=False,
    )


async def skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = _state(context)
    _clear_pending(state)
    has_ref = bool(state.get("ref_path") and Path(state["ref_path"]).exists())
    if state["skip"]:
        state["skip"] = False
        if has_ref:
            await _reply(update, context, "已改回使用上次記住的參考音色。請輸入要唸的文字。", settings=False)
        else:
            await _reply(update, context, "目前沒有已記住的參考音色。請先傳送語音或音檔。", settings=False)
        return
    state["skip"] = True
    kept = "上次的參考音色仍會保留，再按一次「跳過參考音」即可恢復使用。" if has_ref else ""
    await _reply(
        update,
        context,
        "已改為不使用參考音。接下來輸入文字即可合成。可用「設定風格」描述想要的聲音。"
        + (f"\n{kept}" if kept else ""),
        settings=False,
    )


async def style(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = _state(context)
    if state["ultimate"]:
        await _reply(
            update,
            context,
            "極致克隆開啟時會停用風格描述（Control Instruction）。請先關閉極致克隆，或改用「修改逐字稿」。",
        )
        return
    instruction = _args_text(context)
    if not instruction:
        state["awaiting_style"] = True
        await _reply(
            update,
            context,
            f"目前風格：{state['style'] or '未設定'}\n請直接傳送風格描述，例如：年輕女性，溫柔甜美",
            settings=False,
        )
        return
    state["style"] = instruction
    state["awaiting_style"] = False
    await _reply(update, context, f"已設定風格：{instruction}", settings=False)


async def settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = _state(context)
    await _reply(
        update,
        context,
        "進階設定（對應 VoxCPM 官網選項）\n"
        "極致克隆會用參考音逐字稿做音訊續寫，並停用風格描述。\n"
        "更高 CFG → 更貼近參考／提示；更多步數 → 可能更細但更慢。\n\n"
        f"{_params_text(state)}\n\n"
        "也可輸入：/ultimate、/transcript、/denoise、/cfg 2.0、/steps 10",
        settings=True,
    )


async def back_to_main(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply(update, context, "已返回主選單。", settings=False)


async def toggle_denoise(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = _state(context)
    state["denoise"] = _toggle_flag(state["denoise"], _args_text(context))
    await _reply(update, context, f"參考音降噪：{_on_off(state['denoise'])}\n{_params_text(state)}")


async def _set_number(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    key: str,
    value: float | int | None,
    cast: Callable,
    lo: float,
    hi: float,
    label: str,
    usage: str,
    digits: int | None = None,
) -> None:
    state = _state(context)
    if value is None:
        raw = _args_text(context)
        if not raw:
            await _reply(update, context, f"目前 {label}：{state[key]}\n用法：{usage}（範圍 {lo}–{hi}）")
            return
        try:
            value = cast(raw.split()[0])
        except ValueError:
            await _reply(update, context, f"{label} 必須是數字，範圍 {lo}–{hi}。")
            return
    if value < lo or value > hi:
        await _reply(update, context, f"{label} 範圍是 {lo}–{hi}。")
        return
    state[key] = round(value, digits) if digits is not None else value
    await _reply(update, context, f"已設定 {label}：{state[key]}\n{_params_text(state)}")


async def set_cfg(update: Update, context: ContextTypes.DEFAULT_TYPE, value: float | None = None) -> None:
    await _set_number(
        update,
        context,
        key="cfg_value",
        value=value,
        cast=float,
        lo=CFG_MIN,
        hi=CFG_MAX,
        label="CFG",
        usage="/cfg 2.0",
        digits=1,
    )


async def set_steps(update: Update, context: ContextTypes.DEFAULT_TYPE, value: int | None = None) -> None:
    await _set_number(
        update,
        context,
        key="dit_steps",
        value=value,
        cast=int,
        lo=STEPS_MIN,
        hi=STEPS_MAX,
        label="LocDiT 步數",
        usage="/steps 10",
    )


async def toggle_ultimate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = _state(context)
    state["ultimate"] = _toggle_flag(state["ultimate"], _args_text(context))
    _clear_pending(state)

    if state["ultimate"] and _ref_path(state) and not state["prompt_text"]:
        user_id = update.effective_user.id if update.effective_user else 0
        lock = await _try_user_lock(user_id)
        if lock is None:
            await _reply(update, context, "正在處理上一個任務，請稍候。")
            return
        try:
            transcript = await _run_job(
                update,
                "正在辨識參考音逐字稿…",
                voxcpm.transcribe,
                state["ref_path"],
                fail_suffix="\n\n已記住參考音色。請按「修改逐字稿」手動輸入後再合成。",
            )
            if transcript:
                state["prompt_text"] = transcript
        finally:
            lock.release()

    if not state["ultimate"]:
        await _reply(update, context, f"已關閉極致克隆，可再使用風格描述。\n\n{_params_text(state)}")
        return
    extra = "已開啟極致克隆，風格描述會停用。請先上傳參考音；若逐字稿不準，可按「修改逐字稿」。"
    if not _ref_path(state):
        extra = "已開啟極致克隆。請先上傳參考語音，系統會自動辨識逐字稿。"
    elif state["prompt_text"]:
        extra = f"已開啟極致克隆。\n逐字稿：{state['prompt_text']}"
    await _reply(update, context, f"{extra}\n\n{_params_text(state)}")


async def edit_transcript(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = _state(context)
    instruction = _args_text(context)
    if instruction:
        state["prompt_text"] = instruction
        state["awaiting_transcript"] = False
        await _reply(update, context, f"已更新參考音逐字稿：{instruction}")
        return
    state["awaiting_transcript"] = True
    state["awaiting_style"] = False
    await _reply(
        update,
        context,
        f"目前逐字稿：{state['prompt_text'] or '尚未辨識／輸入'}\n請直接傳送參考音裡實際說的文字。",
    )


def _audio_suffix(file_name: str, mime: str) -> str:
    suffix = Path(file_name).suffix.lower()
    if suffix in AUDIO_EXTS:
        return suffix
    return AUDIO_SUFFIX_BY_MIME.get(mime, ".ogg")


async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return

    lock = await _try_user_lock(update.effective_user.id)
    if lock is None:
        await _reply(update, context, "正在處理上一個任務，請稍候再傳送語音。")
        return
    try:
        await _handle_audio_locked(update, context)
    finally:
        lock.release()


async def _handle_audio_locked(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return

    file_obj = update.message.voice or update.message.audio or update.message.document
    if file_obj is None:
        await _reply(update, context, "請傳送語音訊息或音檔。")
        return

    mime = getattr(file_obj, "mime_type", None) or ""
    file_name = getattr(file_obj, "file_name", None) or ""
    if update.message.document and not (mime.startswith("audio/") or Path(file_name).suffix.lower() in AUDIO_EXTS):
        await _reply(update, context, "請傳送音檔（wav / mp3 / ogg / m4a）。")
        return

    dest = _user_dir(update.effective_user.id) / f"ref{_audio_suffix(file_name, mime)}"
    telegram_file = await context.bot.get_file(file_obj.file_id)
    await telegram_file.download_to_drive(custom_path=str(dest))
    dest = Path(await asyncio.to_thread(prepare_reference_audio, str(dest)))

    state = _state(context)
    state.update(
        {
            "ref_path": str(dest),
            "skip": False,
            "prompt_text": "",
            "awaiting_style": False,
            "awaiting_transcript": False,
        }
    )
    _save_state(state)

    if not state["ultimate"]:
        await _reply(update, context, "已記住這段參考音色。請輸入要唸的文字。", settings=False)
        return

    transcript = await _run_job(
        update,
        "正在辨識參考音逐字稿…",
        voxcpm.transcribe,
        str(dest),
        fail_suffix="\n\n已記住參考音色。請按「修改逐字稿」手動輸入後再合成。",
    )
    if not transcript:
        return
    state["prompt_text"] = transcript
    _save_state(state)
    await _reply(
        update,
        context,
        f"已記住參考音色，並辨識逐字稿：\n{transcript}\n\n若不正確請按「修改逐字稿」。接著輸入要唸的文字。",
        settings=False,
    )


def _result_caption(state: dict[str, Any], ref_path: str | None, chunks: int = 1) -> str:
    parts = ["合成完成"]
    if ref_path:
        parts[0] += "（已使用參考音色）"
    if state["ultimate"]:
        parts[0] += "（極致克隆）"
        if state["prompt_text"]:
            parts.append(f"逐字稿：{state['prompt_text']}")
    elif state["style"]:
        parts.append(f"風格：{state['style']}")
    if chunks > 1:
        parts.append(f"已分 {chunks} 段合成")
    parts.append(
        f"CFG {state['cfg_value']} · 步數 {state['dit_steps']} · 降噪 {_on_off(state['denoise'])}"
    )
    return _truncate("\n".join(parts), TELEGRAM_CAPTION_LIMIT)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text or not update.effective_user:
        return

    text = update.message.text.strip()
    if not text:
        return

    shortcut = SHORTCUTS.get(text)
    if shortcut:
        await shortcut(update, context)
        return
    if text in CFG_PRESETS:
        await set_cfg(update, context, CFG_PRESETS[text])
        return
    if text in STEPS_PRESETS:
        await set_steps(update, context, STEPS_PRESETS[text])
        return

    state = _state(context)
    if state["awaiting_transcript"]:
        state["prompt_text"] = text
        state["awaiting_transcript"] = False
        await _reply(update, context, f"已更新參考音逐字稿：{text}")
        return
    if state["awaiting_style"]:
        state["style"] = text
        state["awaiting_style"] = False
        await _reply(update, context, f"已設定風格：{text}", settings=False)
        return

    ref_path = _ref_path(state)
    if state["ultimate"] and not ref_path:
        await _reply(update, context, "極致克隆需要參考語音。請先傳送語音或音檔，或先關閉極致克隆。")
        return
    if state["ultimate"] and not state["prompt_text"]:
        await _reply(update, context, "極致克隆需要參考音逐字稿。請按「修改逐字稿」，或重新上傳語音讓系統辨識。")
        return

    lock = await _try_user_lock(update.effective_user.id)
    if lock is None:
        await _reply(update, context, "正在處理上一個任務，請稍候再傳送文字。")
        return
    chunks = split_text(text)
    pending = (
        f"正在生成語音（共 {len(chunks)} 段），請稍候（官方站可能排隊）…"
        if len(chunks) > 1
        else "正在生成語音，請稍候（官方站可能排隊）…"
    )
    try:
        audio_path = await _run_job(
            update,
            pending,
            voxcpm.generate,
            text=text,
            user_id=f"tg-{update.effective_user.id}",
            ref_wav=ref_path,
            control_instruction="" if state["ultimate"] else state["style"],
            cfg_value=state["cfg_value"],
            dit_steps=state["dit_steps"],
            denoise=state["denoise"],
            use_prompt_text=bool(state["ultimate"] and ref_path),
            prompt_text_value=state["prompt_text"] if state["ultimate"] else "",
        )
        if not audio_path:
            return
        state["in_settings"] = False
        playback_path = await asyncio.to_thread(prepare_output_audio, audio_path)
        seconds = await asyncio.to_thread(audio_duration, playback_path)
        with open(playback_path, "rb") as audio_file:
            await update.message.reply_audio(
                audio=audio_file,
                caption=_result_caption(state, ref_path, len(chunks)),
                filename=Path(playback_path).name,
                duration=max(1, int(round(seconds))) if seconds else None,
                reply_markup=command_keyboard(),
            )
        await asyncio.to_thread(cleanup_data_dir, DATA_DIR)
    finally:
        lock.release()


SHORTCUTS = {
    BTN_HELP: start,
    BTN_STATUS: status,
    BTN_CLEAR: clear,
    BTN_SKIP: skip,
    BTN_STYLE: style,
    BTN_SETTINGS: settings,
    BTN_BACK: back_to_main,
    BTN_DENOISE: toggle_denoise,
    BTN_ULTIMATE: toggle_ultimate,
    BTN_TRANSCRIPT: edit_transcript,
    BTN_CANCEL: cancel,
}

COMMAND_HANDLERS = [
    ("start", start),
    ("help", start),
    ("status", status),
    ("clear", clear),
    ("skip", skip),
    ("style", style),
    ("denoise", toggle_denoise),
    ("cfg", set_cfg),
    ("steps", set_steps),
    ("settings", settings),
    ("ultimate", toggle_ultimate),
    ("transcript", edit_transcript),
    ("cancel", cancel),
]

BOT_COMMANDS = [
    BotCommand("start", "開始／說明"),
    BotCommand("style", "設定說話風格"),
    BotCommand("skip", "不使用參考音"),
    BotCommand("clear", "清除聲音與風格"),
    BotCommand("status", "查看目前設定"),
    BotCommand("denoise", "開關參考音降噪"),
    BotCommand("cfg", "設定 CFG 1.0–3.0"),
    BotCommand("steps", "設定 LocDiT 步數 1–50"),
    BotCommand("settings", "進階參數選單"),
    BotCommand("ultimate", "開關極致克隆"),
    BotCommand("transcript", "修改參考音逐字稿"),
    BotCommand("cancel", "取消進行中的任務"),
]


def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token or token == "your_bot_token_here":
        raise SystemExit("請在 .env 設定 TELEGRAM_BOT_TOKEN")

    async def post_init(application: Application) -> None:
        await application.bot.set_my_commands(BOT_COMMANDS)
        removed = await asyncio.to_thread(cleanup_data_dir, DATA_DIR)
        logger.info("Startup cleanup removed %s files", removed)

    app = Application.builder().token(token).post_init(post_init).build()
    for name, handler in COMMAND_HANDLERS:
        app.add_handler(CommandHandler(name, handler))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO | filters.Document.ALL, handle_audio))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Bot polling started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
