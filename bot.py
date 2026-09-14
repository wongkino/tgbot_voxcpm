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
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, TypeHandler, filters

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

START_TEXT = """VoxCPM 聲音克隆 Bot

先選生成方式，再輸入要唸的文字，Bot 會回傳 M4A 音檔。

【使用步驟】
1. 按下方按鈕選擇一種生成方式（✓ 是目前使用中）
2. 可控克隆／極致克隆：傳送語音訊息或音檔作為參考音色
3. 聲音設計／可控克隆：可按「設定風格」描述聲音
4. 極致克隆：系統會辨識逐字稿，不對就按「修改逐字稿」
5. 輸入要唸的文字，等待合成（官方站可能排隊）

【三種生成方式】
聲音設計
不模仿任何人。用風格描述創造聲音，例如「年輕女性，溫柔甜美」。不用傳參考音。

可控克隆
上傳參考音來模仿音色，可再加風格控制語氣、語速。

極致克隆
上傳參考音並提供逐字稿（可自動辨識）。以續寫方式還原原聲細節，不能同時用風格描述。

【下方按鈕】
聲音設計／可控克隆／極致克隆 — 切換方式
設定風格 — 描述語氣、性別、語速
修改逐字稿 — 改正參考音實際說的內容
狀態 — 查看目前設定與下一步
進階設定 — 參考音降噪、CFG、步數
清除 — 忘記參考音、風格與逐字稿（方式與進階參數不變）
取消任務 — 中止正在進行的辨識或生成
說明 — 再看一次本說明

【會記住的內容】
參考音色、逐字稿、風格、生成方式與進階參數會保存，重開 Bot 後仍可用。只有按「清除」才會忘記聲音與風格。

【注意】
官方示範站可能排隊或限流。轉發到 WhatsApp 請用 Bot 回傳的音檔，不要用語音泡泡。
"""

voxcpm = VoxCPMClient(download_dir=str(DATA_DIR / "generated"))

DEFAULT_CFG = 3.0
DEFAULT_STEPS = 30
DEFAULT_DENOISE = True
MODE_DESIGN = "design"
MODE_CLONE = "clone"
MODE_ULTIMATE = "ultimate"
DEFAULT_MODE = MODE_ULTIMATE
CFG_MIN, CFG_MAX = 1.0, 3.0
STEPS_MIN, STEPS_MAX = 1, 50
TELEGRAM_TEXT_LIMIT = 4096
TELEGRAM_CAPTION_LIMIT = 1024

MODE_LABELS = {
    MODE_DESIGN: "聲音設計",
    MODE_CLONE: "可控克隆",
    MODE_ULTIMATE: "極致克隆",
}
MODE_HINTS = {
    MODE_DESIGN: "不使用參考音，用風格描述創造聲音。",
    MODE_CLONE: "用參考音克隆音色，可再加風格描述。",
    MODE_ULTIMATE: "用參考音 + 逐字稿還原原聲細節，風格描述會停用。",
}

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
BTN_STYLE = "設定風格"
BTN_SETTINGS = "進階設定"
BTN_HELP = "說明"
BTN_BACK = "返回"
BTN_DENOISE = "參考音降噪"
BTN_TRANSCRIPT = "修改逐字稿"
BTN_CANCEL = "取消任務"
BTN_MODE_DESIGN = "聲音設計"
BTN_MODE_CLONE = "可控克隆"
BTN_MODE_ULTIMATE = "極致克隆"

MODE_BUTTONS = {
    BTN_MODE_DESIGN: MODE_DESIGN,
    BTN_MODE_CLONE: MODE_CLONE,
    BTN_MODE_ULTIMATE: MODE_ULTIMATE,
}
MODE_ALIASES = {
    "design": MODE_DESIGN,
    "voice": MODE_DESIGN,
    "聲音設計": MODE_DESIGN,
    "1": MODE_DESIGN,
    "clone": MODE_CLONE,
    "可控克隆": MODE_CLONE,
    "2": MODE_CLONE,
    "ultimate": MODE_ULTIMATE,
    "極致克隆": MODE_ULTIMATE,
    "3": MODE_ULTIMATE,
}

CFG_PRESETS = {"CFG 1.5": 1.5, "CFG 2.0": 2.0, "CFG 2.5": 2.5, "CFG 3": 3.0, "CFG 3.0": 3.0}
STEPS_PRESETS = {f"步數 {n}": n for n in (10, 20, 30, 40, 50)}

STATE_DEFAULTS: dict[str, Any] = {
    "ref_path": None,
    "style": "",
    "mode": DEFAULT_MODE,
    "awaiting_style": False,
    "awaiting_transcript": False,
    "in_settings": False,
    "denoise": DEFAULT_DENOISE,
    "cfg_value": DEFAULT_CFG,
    "dit_steps": DEFAULT_STEPS,
    "prompt_text": "",
}
PERSIST_KEYS = (
    "ref_path",
    "style",
    "mode",
    "denoise",
    "cfg_value",
    "dit_steps",
    "prompt_text",
)


def _markup(rows: list[list[str]], placeholder: str = "輸入要唸的文字") -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton(label) for label in row] for row in rows],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder=placeholder,
    )


def _mode_button(mode: str, current: str) -> str:
    label = MODE_LABELS[mode]
    return f"✓ {label}" if mode == current else label


def _parse_mode_button(text: str) -> str | None:
    cleaned = text.replace("✓", "").strip()
    return MODE_BUTTONS.get(cleaned) or MODE_ALIASES.get(cleaned) or MODE_ALIASES.get(cleaned.lower())


def _active_keyboard(state: dict[str, Any]) -> ReplyKeyboardMarkup:
    if state.get("in_settings"):
        return _markup(
            [
                [BTN_DENOISE],
                ["CFG 1.5", "CFG 2.0"],
                ["CFG 2.5", "CFG 3"],
                ["步數 10", "步數 20", "步數 30"],
                ["步數 40", "步數 50"],
                [BTN_BACK],
            ],
            "調整降噪、CFG 或步數",
        )
    current = _mode(state)
    action = [BTN_TRANSCRIPT] if current == MODE_ULTIMATE else [BTN_STYLE]
    return _markup(
        [
            [_mode_button(MODE_DESIGN, current), _mode_button(MODE_CLONE, current), _mode_button(MODE_ULTIMATE, current)],
            action + [BTN_STATUS],
            [BTN_SETTINGS, BTN_CLEAR],
            [BTN_HELP, BTN_CANCEL],
        ]
    )


def command_keyboard() -> ReplyKeyboardMarkup:
    return _active_keyboard({"mode": DEFAULT_MODE, "in_settings": False})


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
    keep = set(PERSIST_KEYS) | {"skip", "ultimate"}
    return {key: data[key] for key in keep if key in data}


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


async def _bind_user_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user:
        context._user_id = update.effective_user.id


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
    _migrate_mode(state)
    return state


def _migrate_mode(state: dict[str, Any]) -> None:
    if state.get("mode") in MODE_LABELS:
        return
    if state.get("skip"):
        state["mode"] = MODE_DESIGN
    elif state.get("ultimate") is False:
        state["mode"] = MODE_CLONE
    else:
        state["mode"] = DEFAULT_MODE


def _mode(state: dict[str, Any]) -> str:
    mode = state.get("mode")
    return mode if mode in MODE_LABELS else DEFAULT_MODE


def _mode_label(state: dict[str, Any]) -> str:
    return MODE_LABELS[_mode(state)]


def _has_ref(state: dict[str, Any]) -> bool:
    return bool(state.get("ref_path") and Path(state["ref_path"]).exists())


def _ref_status(state: dict[str, Any]) -> str:
    if not _has_ref(state):
        return "未使用"
    if _mode(state) == MODE_DESIGN:
        return "已記住（聲音設計中未使用）"
    return "已記住"


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
        f"生成方式：{_mode_label(state)}\n"
        f"參考音色：{_ref_status(state)}\n"
        f"風格：{state['style'] or '未設定'}\n"
        f"參考音逐字稿：{state['prompt_text'] or '尚未辨識／輸入'}\n"
        f"參考音降噪：{_on_off(state['denoise'])}\n"
        f"CFG：{state['cfg_value']}\n"
        f"LocDiT 步數：{state['dit_steps']}"
    )


def _next_hint(state: dict[str, Any]) -> str:
    mode = _mode(state)
    if mode == MODE_DESIGN:
        if not state["style"]:
            return "下一步：按「設定風格」，再輸入要唸的文字。"
        return "下一步：直接輸入要唸的文字。"
    if not _has_ref(state):
        return "下一步：傳送語音訊息或音檔作為參考音色。"
    if mode == MODE_ULTIMATE and not state["prompt_text"]:
        return "下一步：按「修改逐字稿」，或重新上傳語音讓系統辨識。"
    if mode == MODE_CLONE and not state["style"]:
        return "下一步：可按「設定風格」，或直接輸入要唸的文字。"
    return "下一步：直接輸入要唸的文字。"


def _ref_path(state: dict[str, Any]) -> str | None:
    if _mode(state) == MODE_DESIGN:
        return None
    if not _has_ref(state):
        return None
    return state["ref_path"]


async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled handler error", exc_info=context.error)
    message = update.effective_message if isinstance(update, Update) else None
    if not message:
        return
    try:
        await message.reply_text("處理訊息時發生錯誤，請再試一次。若持續沒反應請按「取消任務」。")
    except Exception:
        logger.debug("Could not send error reply", exc_info=True)


async def _reply(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    *,
    settings: bool | None = None,
) -> None:
    if not update.message:
        return
    if update.effective_user:
        context._user_id = update.effective_user.id
    state = _state(context)
    if settings is not None:
        state["in_settings"] = settings
    keyboard = _active_keyboard(state)
    _save_state(state)
    await update.message.reply_text(
        _truncate(text, TELEGRAM_TEXT_LIMIT),
        reply_markup=keyboard,
    )


async def _safe_delete(message: Message | None) -> None:
    if not message:
        return
    try:
        await message.delete()
    except Exception:
        logger.debug("Could not delete status message", exc_info=True)


async def _run_job(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    pending: str,
    fn: Callable,
    *args,
    fail_suffix: str = "",
    **kwargs,
) -> Any:
    if not update.message:
        return None
    user_id = update.effective_user.id if update.effective_user else 0
    if update.effective_user:
        context._user_id = update.effective_user.id
    job = UserJob(cancel_event=threading.Event())
    _user_jobs[user_id] = job
    kwargs.setdefault("cancel_event", job.cancel_event)
    keyboard = _active_keyboard(_state(context))
    status = None
    try:
        status = await update.message.reply_text(pending, reply_markup=keyboard)
        result = await asyncio.to_thread(fn, *args, **kwargs)
    except VoxCPMCancelled:
        if status:
            await status.edit_text("已取消上一個任務。", reply_markup=keyboard)
        return None
    except VoxCPMError as exc:
        if status:
            await status.edit_text(f"{exc}{fail_suffix}", reply_markup=keyboard)
        return None
    except Exception as exc:
        logger.exception("Background job failed")
        if status:
            await status.edit_text(f"操作失敗：{exc}{fail_suffix}", reply_markup=keyboard)
        else:
            await update.message.reply_text(f"操作失敗：{exc}{fail_suffix}", reply_markup=keyboard)
        return None
    finally:
        _user_jobs.pop(user_id, None)
    await _safe_delete(status)
    return result


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = _state(context)
    _clear_pending(state)
    user_id = update.effective_user.id if update.effective_user else 0
    job = _user_jobs.get(user_id)
    if job is None:
        await _reply(update, context, f"目前沒有進行中的任務。\n{_next_hint(state)}")
        return
    job.cancel_event.set()
    voxcpm.reset()
    await _reply(update, context, "已要求取消上一個任務，請稍候。")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = _state(context)
    extra = f"\n【目前】\n{_params_text(state)}\n{_next_hint(state)}"
    await _reply(update, context, START_TEXT + extra, settings=False)


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = _state(context)
    await _reply(update, context, f"{_params_text(state)}\n{_next_hint(state)}")


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
    state["prompt_text"] = ""
    _clear_pending(state)
    await _reply(
        update,
        context,
        f"已清除參考音色、風格與逐字稿。生成方式仍是{_mode_label(state)}，進階參數維持不變。\n{_next_hint(state)}",
        settings=False,
    )


async def style(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = _state(context)
    if _mode(state) == MODE_ULTIMATE:
        await _reply(
            update,
            context,
            "極致克隆會停用風格描述。請改選「可控克隆」或「聲音設計」，或按「修改逐字稿」。",
        )
        return
    instruction = _args_text(context)
    if not instruction:
        state["awaiting_style"] = True
        await _reply(
            update,
            context,
            f"目前風格：{state['style'] or '未設定'}\n請直接傳送風格描述，例如：年輕女性，溫柔甜美\n{_next_hint(state)}",
            settings=False,
        )
        return
    state["style"] = instruction
    state["awaiting_style"] = False
    await _reply(update, context, f"已設定風格：{instruction}\n{_next_hint(state)}", settings=False)


async def settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = _state(context)
    await _reply(
        update,
        context,
        "進階設定\n"
        "參考音降噪：克隆前清理參考音雜訊。\n"
        "CFG 越高越貼近參考／風格；步數越多可能更細但更慢。\n\n"
        f"{_params_text(state)}\n"
        f"{_next_hint(state)}\n\n"
        "也可輸入：/denoise、/cfg 2.0、/steps 30",
        settings=True,
    )


async def back_to_main(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = _state(context)
    await _reply(update, context, f"已返回主選單。\n{_next_hint(state)}", settings=False)


async def toggle_denoise(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = _state(context)
    state["denoise"] = _toggle_flag(state["denoise"], _args_text(context))
    await _reply(update, context, f"參考音降噪：{_on_off(state['denoise'])}\n{_params_text(state)}\n{_next_hint(state)}")


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


async def choose_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    raw = _args_text(context)
    if not raw:
        state = _state(context)
        await _reply(
            update,
            context,
            f"目前生成方式：{_mode_label(state)}\n請按下方按鈕，或輸入 /mode 聲音設計、可控克隆、極致克隆。\n{_next_hint(state)}",
            settings=False,
        )
        return
    mode = _parse_mode_button(raw)
    if not mode:
        await _reply(update, context, "請選擇：聲音設計、可控克隆 或 極致克隆。", settings=False)
        return
    await apply_mode(update, context, mode)


async def apply_mode(update: Update, context: ContextTypes.DEFAULT_TYPE, mode: str) -> None:
    state = _state(context)
    already = _mode(state) == mode
    state["mode"] = mode
    _clear_pending(state)

    extra = MODE_HINTS[mode]
    if mode == MODE_DESIGN and _has_ref(state):
        extra += " 已記住的參考音色仍會保留，此模式不會使用。"
    elif mode == MODE_CLONE:
        extra += " 請先傳送參考語音。" if not _has_ref(state) else " 將使用已記住的參考音色。"
    elif mode == MODE_ULTIMATE:
        extra += " 風格描述會停用。"
        if not _has_ref(state):
            extra += " 請先傳送參考語音。"
        elif not state["prompt_text"]:
            user_id = update.effective_user.id if update.effective_user else 0
            lock = await _try_user_lock(user_id)
            if lock is None:
                await _reply(update, context, "正在處理上一個任務，請稍候。", settings=False)
                return
            try:
                transcript = await _run_job(
                    update,
                    context,
                    "正在辨識參考音逐字稿…",
                    voxcpm.transcribe,
                    state["ref_path"],
                    fail_suffix="\n\n已記住參考音色。請按「修改逐字稿」手動輸入後再合成。",
                )
                if transcript:
                    state["prompt_text"] = transcript
            finally:
                lock.release()

    prefix = f"目前已是{_mode_label(state)}。" if already else f"已切換為{_mode_label(state)}。"
    await _reply(
        update,
        context,
        f"{prefix} {extra}\n\n{_params_text(state)}\n{_next_hint(state)}",
        settings=False,
    )


async def edit_transcript(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = _state(context)
    instruction = _args_text(context)
    if instruction:
        state["prompt_text"] = instruction
        state["awaiting_transcript"] = False
        await _reply(update, context, f"已更新參考音逐字稿：{instruction}\n{_next_hint(state)}")
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
            "prompt_text": "",
            "awaiting_style": False,
            "awaiting_transcript": False,
        }
    )
    _save_state(state)

    if _mode(state) == MODE_DESIGN:
        state["mode"] = MODE_CLONE
        _save_state(state)
        await _reply(
            update,
            context,
            "已記住參考音色，並改為可控克隆（聲音設計不會使用參考音）。若要更像原聲，可再按「極致克隆」。\n"
            f"{_params_text(state)}\n{_next_hint(state)}",
            settings=False,
        )
        return
    if _mode(state) != MODE_ULTIMATE:
        await _reply(
            update,
            context,
            f"已記住這段參考音色。\n{_params_text(state)}\n{_next_hint(state)}",
            settings=False,
        )
        return

    transcript = await _run_job(
        update,
        context,
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
        f"已記住參考音色，並辨識逐字稿：\n{transcript}\n\n若不正確請按「修改逐字稿」。\n{_next_hint(state)}",
        settings=False,
    )


def _result_caption(state: dict[str, Any], ref_path: str | None, chunks: int = 1) -> str:
    parts = [f"合成完成（{_mode_label(state)}）"]
    if ref_path:
        parts[0] += "（已使用參考音色）"
    if _mode(state) == MODE_ULTIMATE:
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
    selected_mode = _parse_mode_button(text)
    if selected_mode:
        await apply_mode(update, context, selected_mode)
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
        await _reply(update, context, f"已更新參考音逐字稿：{text}\n{_next_hint(state)}")
        return
    if state["awaiting_style"]:
        state["style"] = text
        state["awaiting_style"] = False
        await _reply(update, context, f"已設定風格：{text}\n{_next_hint(state)}", settings=False)
        return

    mode = _mode(state)
    ref_path = _ref_path(state)
    if mode != MODE_DESIGN and not ref_path:
        await _reply(update, context, f"{_mode_label(state)}需要參考語音。請先傳送語音或音檔，或改選「聲音設計」。")
        return
    if mode == MODE_ULTIMATE and not state["prompt_text"]:
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
            context,
            pending,
            voxcpm.generate,
            text=text,
            user_id=f"tg-{update.effective_user.id}",
            ref_wav=ref_path,
            control_instruction="" if mode == MODE_ULTIMATE else state["style"],
            cfg_value=state["cfg_value"],
            dit_steps=state["dit_steps"],
            denoise=state["denoise"],
            use_prompt_text=mode == MODE_ULTIMATE,
            prompt_text_value=state["prompt_text"] if mode == MODE_ULTIMATE else "",
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
                reply_markup=_active_keyboard(state),
            )
        await asyncio.to_thread(cleanup_data_dir, DATA_DIR)
    finally:
        lock.release()


SHORTCUTS = {
    BTN_HELP: start,
    BTN_STATUS: status,
    BTN_CLEAR: clear,
    BTN_STYLE: style,
    BTN_SETTINGS: settings,
    BTN_BACK: back_to_main,
    BTN_DENOISE: toggle_denoise,
    BTN_TRANSCRIPT: edit_transcript,
    BTN_CANCEL: cancel,
}

COMMAND_HANDLERS = [
    ("start", start),
    ("help", start),
    ("status", status),
    ("clear", clear),
    ("mode", choose_mode),
    ("style", style),
    ("denoise", toggle_denoise),
    ("cfg", set_cfg),
    ("steps", set_steps),
    ("settings", settings),
    ("transcript", edit_transcript),
    ("cancel", cancel),
]

BOT_COMMANDS = [
    BotCommand("start", "開始／說明"),
    BotCommand("mode", "選擇生成方式"),
    BotCommand("style", "設定說話風格"),
    BotCommand("clear", "清除聲音與風格"),
    BotCommand("status", "查看目前設定"),
    BotCommand("denoise", "開關參考音降噪"),
    BotCommand("cfg", "設定 CFG 1.0–3.0"),
    BotCommand("steps", "設定 LocDiT 步數 1–50"),
    BotCommand("settings", "進階參數與生成方式"),
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
    app.add_handler(TypeHandler(Update, _bind_user_id), group=-1)
    for name, handler in COMMAND_HANDLERS:
        app.add_handler(CommandHandler(name, handler))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO | filters.Document.ALL, handle_audio))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(_on_error)

    logger.info("Bot polling started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
