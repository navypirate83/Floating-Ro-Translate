"""
Hover Translator (Romanian / Ukrainian -> English).

A small always-on-top window. Whenever the mouse rests over text anywhere on
screen, the region around the cursor is OCR'd with the Windows built-in OCR
engine. If the text looks like the chosen language it is sent to Google
Translate and the English shows in the window; otherwise the window stays idle.
Highlighting text translates the whole selection; a reply panel translates
English into the chosen language.

Languages are defined in the LANGUAGES table below — add an entry to add one.
Requires Windows 10/11 with that language's pack installed
(Settings > Time & Language > Language & region > Add a language).
"""

import asyncio
import ctypes
import re
import sys
import threading
import time
import tkinter as tk
from collections import OrderedDict
from ctypes import wintypes

import mss
import requests
try:
    import uiautomation as uia          # reads text straight from apps (no OCR needed)
    UIA_IMPORT_ERROR = ""
except Exception as _e:                 # pragma: no cover
    uia = None
    UIA_IMPORT_ERROR = str(_e)
from winrt.windows.globalization import Language
from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
from winrt.windows.media.ocr import OcrEngine

# ---------------------------------------------------------------- settings ---
HOVER_DELAY = 0.5          # seconds the mouse must rest before we look
CAPTURE_W, CAPTURE_H = 900, 260   # pixel area grabbed around the cursor
MAX_LINES = 6              # lines of the paragraph under the cursor to keep
POLL_INTERVAL = 0.08       # cursor polling rate in seconds
WINDOW_W = 340             # floating window width
APP_NAME = "Hover Translator"
APP_VERSION = "15.5"
APP_BUILT = "September 2026"
SELECT_MIN_DRAG = 12       # pixels of mouse drag that count as highlighting text
SELECT_MAX_CHARS = 1500    # longest highlighted passage sent for translation
CACHE_MAX_BYTES = 256 * 1024 * 1024  # LRU cap for source + translation strings

# --------------------------------------------------------- Windows plumbing --
user32 = ctypes.windll.user32
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)   # per-monitor DPI aware
except Exception:
    user32.SetProcessDPIAware()


def cursor_pos():
    pt = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def mouse_down() -> bool:
    return bool(user32.GetAsyncKeyState(0x01) & 0x8000)  # VK_LBUTTON


kernel32 = ctypes.windll.kernel32
CF_UNICODETEXT = 13
# 64-bit handles/pointers must be declared, or ctypes truncates them to 32 bits
user32.OpenClipboard.argtypes = [wintypes.HWND]
user32.GetClipboardData.restype = wintypes.HANDLE
user32.GetClipboardData.argtypes = [wintypes.UINT]
user32.SetClipboardData.restype = wintypes.HANDLE
user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalLock.restype = wintypes.LPVOID
kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalFree.restype = wintypes.HGLOBAL
kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
user32.EmptyClipboard.restype = wintypes.BOOL

GMEM_MOVEABLE = 0x0002
_clipboard_lock = threading.Lock()


def _open_clipboard(tries=10):
    for _ in range(tries):                   # another app may hold it briefly
        if user32.OpenClipboard(None):
            return True
        time.sleep(0.02)
    return False


def clipboard_text():
    """Current clipboard text, or None if the clipboard holds something else."""
    if not _open_clipboard():
        return None
    try:
        if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
            return None
        h = user32.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return None
        p = kernel32.GlobalLock(h)
        if not p:
            return None
        try:
            return ctypes.wstring_at(p)
        finally:
            kernel32.GlobalUnlock(h)
    finally:
        user32.CloseClipboard()


def clear_clipboard() -> bool:
    """Empty the clipboard. Used to drop our marker without writing dummy text."""
    if not _open_clipboard():
        return False
    try:
        return bool(user32.EmptyClipboard())
    finally:
        user32.CloseClipboard()


def set_clipboard_text(text: str) -> bool:
    """Put unicode text on the clipboard. Returns False and does not EmptyClipboard on failure."""
    if text is None:
        return False
    data = text.encode("utf-16-le") + b"\x00\x00"
    h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
    if not h:
        return False
    p = kernel32.GlobalLock(h)
    if not p:
        kernel32.GlobalFree(h)
        return False
    ctypes.memmove(p, data, len(data))
    kernel32.GlobalUnlock(h)
    if not _open_clipboard():
        kernel32.GlobalFree(h)
        return False
    owned_by_clipboard = False
    try:
        if not user32.EmptyClipboard():
            return False
        if not user32.SetClipboardData(CF_UNICODETEXT, h):
            return False
        owned_by_clipboard = True
        return True
    finally:
        user32.CloseClipboard()
        if not owned_by_clipboard:
            kernel32.GlobalFree(h)


ULONG_PTR = ctypes.c_uint64 if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("_pad", ctypes.c_byte * 32)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUT_UNION)]


user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wintypes.UINT
INPUT_KEYBOARD, KEYEVENTF_KEYUP = 1, 0x0002
VK_SHIFT, VK_CONTROL, VK_MENU, VK_C = 0x10, 0x11, 0x12, 0x43


def modifier_held() -> bool:
    """True while the user holds Ctrl, Shift or Alt — don't inject keys into that."""
    return any(user32.GetAsyncKeyState(vk) & 0x8000 for vk in (VK_SHIFT, VK_CONTROL, VK_MENU))


kernel32.GetCurrentThreadId.restype = wintypes.DWORD
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.SetFocus.argtypes = [wintypes.HWND]
user32.GetForegroundWindow.restype = wintypes.HWND


def activate_window(hwnd) -> None:
    """Make hwnd the active, keyboard-focused window.

    Borderless (WS_POPUP) windows are not activated by a click, so typing can keep
going to the previously active app. Windows only lets a process steal the
    foreground if it shares input state with the current foreground thread, hence
    the AttachThreadInput dance.
    """
    try:
        fg = user32.GetForegroundWindow()
        if fg == hwnd:
            user32.SetFocus(hwnd)
            return
        our = kernel32.GetCurrentThreadId()
        pid = wintypes.DWORD()
        theirs = user32.GetWindowThreadProcessId(fg, ctypes.byref(pid)) if fg else 0
        attached = bool(theirs and theirs != our and user32.AttachThreadInput(theirs, our, True))
        try:
            user32.SetForegroundWindow(hwnd)
            user32.SetFocus(hwnd)
        finally:
            if attached:
                user32.AttachThreadInput(theirs, our, False)
    except Exception as e:
        log(f"activate_window failed: {e}")


def send_ctrl_c() -> bool:
    """Inject Ctrl+C with SendInput. Returns False if the keystroke wasn't delivered."""
    def key(vk, flags=0):
        inp = INPUT(type=INPUT_KEYBOARD)
        inp.u.ki = KEYBDINPUT(wVk=vk, wScan=0, dwFlags=flags, time=0, dwExtraInfo=0)
        return inp
    seq = (INPUT * 4)(key(VK_CONTROL), key(VK_C), key(VK_C, KEYEVENTF_KEYUP),
                      key(VK_CONTROL, KEYEVENTF_KEYUP))
    return user32.SendInput(4, seq, ctypes.sizeof(INPUT)) == 4


def virtual_screen():
    """(left, top, width, height) of all monitors combined."""
    SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN, SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 76, 77, 78, 79
    return (user32.GetSystemMetrics(SM_XVIRTUALSCREEN),
            user32.GetSystemMetrics(SM_YVIRTUALSCREEN),
            user32.GetSystemMetrics(SM_CXVIRTUALSCREEN),
            user32.GetSystemMetrics(SM_CYVIRTUALSCREEN))


# --------------------------------------------------- language definitions ----
# Each entry is everything the app needs to know about one source language.
# To add a language, add an entry here (and the Windows OCR pack for it).
#   code      Google Translate / Windows OCR language code
#   name      shown in the UI
#   markers   letters that only this language (not English) uses
#   fold      maps accented letters to plain ones so unaccented typing still matches
#   words     everyday words WITHOUT accents (family-chat vocabulary)
#   abbrev    text-speak expanded before translating
#   suffixes  common word endings (weak evidence)
#   also      other codes Google may report that we still accept
#   confusable  a language Google may confuse it with; markers settle it

EN_WORDS = set("""
the and is are of to in that it for with as on this be by at from or an was were you your we
our they their have has not but what when where who how why will would can could should
about just like get got make made know think want need time good very also there here
""".split())

LANGUAGES = {
    "ro": dict(
        code="ro", name="Romanian", flag="RO",
        markers=set("ăâîșțĂÂÎȘȚşţŞŢ"),
        fold=str.maketrans("ăâîșțşţĂÂÎȘȚŞŢ", "aaiststAAISTST"),
        words=set("""
si este sunt esti suntem sunteti e fost fi fie fii fiu fiti pentru pana care cu de la in nu mai
din pe sa se ca dar daca sau dupa cand cum un o al a ai ale lui ei lor acest aceasta acesta
acestea acesti aceste acel acea aceea acela acei acele toate tot toti toata foarte avea are am
ati ne va ii le prin catre fara intre asupra despre unde deci atunci insa numai doar inca aici
acolo azi astazi maine ieri acum apoi deja trebuie poate putem pot poti puteti mult multe multi
putin putina fiecare orice noi voi dumneavoastra vor eu tu el ea ele dansul dansa imi iti isi mi
ti il o ii ne va le mie tie meu mea mei mele tau ta tai tale nostru noastra nostri noastre vostru
voastra vostri voastre sau ori ci nici deci asa astfel chiar tocmai totusi macar cel cea cei cele
ce cine cat cata cati cate careva ceva cineva nimic nimeni niciodata mereu uneori iar iarasi
bine rau bun buna bine frumos frumoasa mare mic mica noua nou vechi
da nu ok okay mersi multumesc multumim multam pupici pup te iubesc iubire dragul draga dragilor
mama mami tata tati bunica bunicu bunicul bunici fratele sora sotul sotia copii copil copiii
baiat baiatul fata fetita fetele nepotul nepoata unchiul matusa
salut buna ceau pa hai haide haideti bine ati venit noapte dimineata seara ziua ziua zi zile
saptamana luna anul ani ora ore minute
casa acasa masina bani banii timp lucru munca serviciu scoala gradinita spital doctor
vin vine venim veniti vino merg mergem mergeti merge duc ducem fac faci face facem faceti
facut stiu stii stie stim stiti cred crezi crede vreau vrei vrea vrem vreti spun spui spune
spunem zic zici zice zis vad vezi vede vedem vazut aud auzi dau dai da dam iau iei ia luam
ajung ajungi ajunge ajuns plec pleci pleaca plecat stau stai sta stam ramai ramane
mananc mananci mananca mancare mancat beau bei bea dorm dormi doarme dormit
vorbim vorbesc vorbesti vorbeste sun suna sunat scriu scrii scrie trimit trimite trimis
astept astepti asteapta asteptam uita uite lasa lasam
poate probabil sigur normal desigur bineinteles exact clar gata
cf ms msj pt dc cnv nb bn tb vb sal
""".split()),
        abbrev={
    "cf": "ce faci", "cmf": "ce mai faci", "ms": "mersi", "mrs": "mersi", "msj": "mesaj",
    "pt": "pentru", "ptr": "pentru", "dc": "de ce", "cnv": "cineva", "nb": "noapte bună",
    "bn": "bine", "tb": "trebuie", "vb": "vorbim", "sal": "salut", "kk": "ok",
    "ns": "nu știu", "nush": "nu știu", "nustiu": "nu știu", "imd": "imediat",
    "cnd": "când", "dak": "dacă", "dp": "după", "acs": "acasă", "dimi": "dimineața",
    "iub": "iubire", "pup": "pupici", "mn": "mâine", "sapt": "săptămâna",
},
        suffixes=("ului", "ilor", "elor", "tiei", "eaza", "este", "esc", "esti", "ati", "ile",
                  "ul", "ea", "ie", "oasa", "os", "ez", "ezi"),
        also=("mo",),
        confusable=(),
        script=None,
        ocr_standins=(),
    ),
    "uk": dict(
        code="uk", name="Ukrainian", flag="UA",
        # letters Ukrainian has and Russian doesn't
        markers=set("іїєґІЇЄҐ"),
        fold=str.maketrans("ІЇЄҐ", "іїєґ"),
        words=set("""
i та й що це не як на в у з із до для він вона воно ми ви вони я ти мене тебе мені тобі його її
нас вас їх так ні добре дякую дуже привіт бувай будь ласка прошу вибач вибачте є був була були
буде бути зараз сьогодні завтра вчора коли де чому бо або але вже ще тут там всі все всього
хто чи щоб про від після перед між без без може можна треба хочу хочеш хоче знаю знаєш знає люблю
кохаю мама тато бабуся дідусь син дочка дитина діти брат сестра сім'я родина дім додому робота
школа машина гроші час день ночі ранок вечір тиждень місяць рік година хвилина
йду йдеш іде їду їдеш їде прийду прийшов прийшла дзвони подзвоню напиши пиши скажи кажу
поїли їсти спати спиш добраніч доброго ранку добрий вечір смачного гаразд окей ок
""".split()),
        abbrev={
            "дяк": "дякую", "спс": "дякую", "пж": "будь ласка", "пжл": "будь ласка",
            "плз": "будь ласка", "нз": "не знаю", "хз": "не знаю", "щас": "зараз",
            "сьодні": "сьогодні", "норм": "нормально", "здр": "здрастуйте", "прив": "привіт",
            "дн": "добраніч", "др": "доброго ранку", "тк": "так", "мб": "може бути",
        },
        suffixes=("ння", "ться", "ити", "ати", "ові", "ами", "ого", "ому", "ий", "ій"),
        also=(),
        confusable=("ru",),
        script=("Cyrillic", re.compile(r"[\u0400-\u04FF]")),
        # Windows has no Ukrainian OCR at all; these read the same alphabet
        ocr_standins=("ru", "sr", "bg"),
    ),
}

# names for languages Google may report, used in "Cyrillic, but Russian" notes
LANG_NAMES = {
    "ru": "Russian", "uk": "Ukrainian", "be": "Belarusian", "bg": "Bulgarian", "sr": "Serbian",
    "mk": "Macedonian", "kk": "Kazakh", "ky": "Kyrgyz", "tg": "Tajik", "mn": "Mongolian",
    "ro": "Romanian", "mo": "Moldovan", "en": "English", "it": "Italian", "es": "Spanish",
    "fr": "French", "pt": "Portuguese", "de": "German", "pl": "Polish", "hu": "Hungarian",
    "la": "Latin", "ca": "Catalan",
}


def same_script(text: str, L) -> bool:
    """True if the text is written in the language's script (e.g. Cyrillic for Ukrainian)."""
    return bool(L["script"]) and bool(L["script"][1].search(text))


def not_target_note(text: str, detected: str, L) -> str:
    """Status line for text that isn't the chosen language."""
    if same_script(text, L):
        name = LANG_NAMES.get(detected)
        script = L["script"][0]
        if name and detected != L["code"]:
            return f"{script}, but {name} — not {L['name']}"
        return f"{script}, but not {L['name']}"
    return f"Not {L['name']} — nothing to translate"

WORD_RE = re.compile(r"[^\W\d_]+")      # any run of letters, any script


def fold(word: str, L) -> str:
    """Lower-case and strip the language's accents: 'Mulțumesc' -> 'multumesc'."""
    return word.translate(L["fold"]).lower()


def language_score(text: str, L):
    """Returns (score, word_count). Higher = more likely the target language."""
    words = [fold(w, L) for w in WORD_RE.findall(text)]
    if not words:
        return 0.0, 0
    abbrev_hits = sum(w in L["abbrev"] for w in words)
    hits = sum(w in L["words"] for w in words) + abbrev_hits
    en_hits = sum(w in EN_WORDS for w in words)
    markers = sum(ch in L["markers"] for ch in text)
    suffix_hits = sum(w.endswith(L["suffixes"]) and len(w) > 4 and w not in EN_WORDS
                      for w in words)
    score = hits + 0.5 * suffix_hits + 1.5 * markers - 1.5 * en_hits
    return score, len(words)


def maybe_target(text: str, L) -> bool:
    """Local pre-filter: only text that could plausibly be the language goes to Google."""
    score, n = language_score(text, L)
    words = [fold(w, L) for w in WORD_RE.findall(text)]
    if words and all(w in L["abbrev"] or (w in L["words"] and len(w) >= 3 and w not in EN_WORDS)
                     for w in words):
        return True
    if n < 2:
        return any(ch in L["markers"] for ch in text)
    return score >= 1


def surely_target(text: str, L) -> bool:
    """Strong enough that we tell Google the language instead of letting it guess."""
    score, n = language_score(text, L)
    words = [fold(w, L) for w in WORD_RE.findall(text)]
    if words and sum(w in L["abbrev"] for w in words) >= max(1, len(words) // 2):
        return True
    return score >= 2 and score / n >= 0.3


def expand_abbreviations(text: str, L) -> str:
    return WORD_RE.sub(lambda m: L["abbrev"].get(fold(m.group(0), L), m.group(0))
                       if len(m.group(0)) <= 6 else m.group(0), text)


class RateLimited(Exception):
    pass


ENDPOINTS = [
    ("https://translate.googleapis.com/translate_a/single", "gtx"),
    ("https://clients5.google.com/translate_a/single", "dict-chrome-ex"),
]
_last_call = 0.0
MIN_GAP = 1.2  # seconds between Google calls


def google_translate(text: str, source: str = "auto", target: str = "en"):
    """Returns (translation, detected_language). Raises RateLimited on 429."""
    global _last_call
    wait = MIN_GAP - (time.time() - _last_call)
    if wait > 0:
        time.sleep(wait)
    _last_call = time.time()
    last_err = None
    for url, client in ENDPOINTS:
        try:
            r = requests.get(
                url,
                params={"client": client, "sl": source, "tl": target, "dt": "t", "q": text},
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                       "AppleWebKit/537.36 Chrome/124.0 Safari/537.36"},
                timeout=8,
            )
            if r.status_code == 429:
                last_err = RateLimited()
                continue
            r.raise_for_status()
            data = r.json()
            if isinstance(data, dict):           # clients5 shape
                translated = "".join(s.get("trans", "") for s in data.get("sentences", []))
                detected = data.get("src", "")
            else:                                # gtx shape
                translated = "".join(part[0] for part in data[0] if part and part[0])
                detected = data[2] or ""
            return translated.strip(), detected.lower()
        except RateLimited:
            raise
        except Exception as e:
            last_err = e
    raise last_err


def is_target(detected: str, text: str, L) -> bool:
    if detected == L["code"] or detected in L["also"]:
        return True
    # a confusable language (e.g. Russian for Ukrainian) with our marker letters present
    if detected in L["confusable"] and any(ch in L["markers"] for ch in text):
        return True
    # unaccented text often gets mis-tagged; trust our own score when strong and not English
    if detected != "en" and surely_target(text, L):
        return True
    if detected in ("", "auto", "und") and any(ch in L["markers"] for ch in text):
        return True
    return False


import json
import os


def app_dir() -> str:
    """Folder the .exe (or the .py) lives in — the only place the app writes files."""
    base = sys.executable if getattr(sys, "frozen", False) else __file__
    return os.path.dirname(os.path.abspath(base))


LOG_FILE = os.path.join(app_dir(), "ro_hover.log")
SETTINGS_FILE = os.path.join(app_dir(), "ro_hover_settings.json")


def load_settings() -> dict:
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(data: dict) -> None:
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except OSError as e:
        log(f"could not save settings: {e}")


class TranslationCache:
    """LRU map of OCR/source text -> translation (or None). Capped by byte size."""

    def __init__(self, max_bytes=CACHE_MAX_BYTES):
        self.max_bytes = max_bytes
        self._data = OrderedDict()
        self._bytes = 0

    @staticmethod
    def _size(key, value):
        return sys.getsizeof(key) + sys.getsizeof(value)

    def __contains__(self, key):
        return key in self._data

    def __getitem__(self, key):
        self._data.move_to_end(key)
        return self._data[key]

    def __setitem__(self, key, value):
        item = self._size(key, value)
        if key in self._data:
            old = self._data.pop(key)
            self._bytes -= self._size(key, old)
        if item > self.max_bytes:
            return
        while self._data and self._bytes + item > self.max_bytes:
            k, v = self._data.popitem(last=False)
            self._bytes -= self._size(k, v)
        self._data[key] = value
        self._bytes += item


def log(msg: str):
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(time.strftime("%H:%M:%S ") + msg + "\n")
    except OSError:
        pass


# ------------------------------------------------- UI Automation reader -----
# Browsers, Electron chat apps (WhatsApp, Signal, Telegram, Discord), Office and
# most modern Windows apps expose their text through UI Automation — the same
# channel screen readers use. Reading it gives the exact characters, in any
# language, with no OCR and no language packs. OCR stays as the fallback for
# images, screenshots and apps that don't expose text.

UIA_MAX_CHARS = 2000
UIA_PARAGRAPH_MAX = 700   # longer than this and the "paragraph" is really the whole page


def _text_pattern_for(control):
    """Walk up from a control to the nearest ancestor that exposes a TextPattern."""
    depth = 0
    while control is not None and depth < 12:
        try:
            tp = control.GetPattern(uia.PatternId.TextPattern)
            if tp:
                return tp
        except Exception:
            pass
        try:
            control = control.GetParentControl()
        except Exception:
            return None
        depth += 1
    return None


def _control_text(control):
    """Best text for a control that has no TextPattern: its own name, or its
    children's names joined (Chrome exposes a paragraph as a row of text leaves)."""
    try:
        name = (control.Name or "").strip()
        if name and len(name.split()) >= 2:
            return name
        parts = []
        for child in control.GetChildren()[:40]:
            n = (child.Name or "").strip()
            if n:
                parts.append(n)
        text = " ".join(parts).strip()
        if text and len(text.split()) >= 2:
            return text
        return name or None
    except Exception:
        return None


def uia_text_under(x, y):
    """Paragraph of text under the screen point, or None if the app exposes none."""
    if uia is None:
        return None
    try:
        control = uia.ControlFromPoint(x, y)
        if control is None:
            log("uia: no control at point")
            return None
        ctype = control.ControlTypeName
        own_name = (control.Name or "").strip()
        tp = _text_pattern_for(control)
        if tp is not None:
            rng = tp.RangeFromPoint(x, y)
            if rng is not None:
                rng.ExpandToEnclosingUnit(uia.TextUnit.Paragraph)
                text = (rng.GetText(UIA_MAX_CHARS) or "").strip()
                # Chromium often ignores the point and returns the top of the whole
                # page; only trust a range that is paragraph-sized and contains the
                # text of the element actually under the cursor
                probe = own_name[:25]
                plausible = (text and len(text) <= UIA_PARAGRAPH_MAX
                             and (not probe or probe.lower() in text.lower()))
                if plausible:
                    log(f"uia: text via TextPattern ({ctype}): {text[:60]!r}")
                    return text
                log(f"uia: TextPattern range rejected ({len(text)} chars, "
                    f"contains element text: {bool(probe and probe.lower() in text.lower())})")
            else:
                log(f"uia: TextPattern present but no range at point ({ctype})")
        # no usable text pattern: use accessible names — the control's own, then
        # its parent's row of text leaves (how Chrome exposes a paragraph)
        text = _control_text(control)
        if not text:
            try:
                parent = control.GetParentControl()
                text = _control_text(parent) if parent is not None else None
            except Exception:
                text = None
        if text:
            log(f"uia: text via Name ({ctype}): {text[:60]!r}")
            return text
        log(f"uia: control {ctype} name={ (control.Name or '')[:40]!r} — no text exposed")
    except Exception as e:
        log(f"uia hover read failed: {e}")
    return None


def uia_selection_at(x, y):
    """The app's current text selection near the point, or None."""
    if uia is None:
        return None
    try:
        control = uia.ControlFromPoint(x, y)
        tp = _text_pattern_for(control) if control is not None else None
        if tp is None:
            return None
        parts = []
        for rng in tp.GetSelection() or []:
            t = (rng.GetText(UIA_MAX_CHARS) or "").strip()
            if t:
                parts.append(t)
        text = " ".join(parts).strip()
        return text or None
    except Exception as e:
        log(f"uia selection read failed: {e}")
        return None


# ------------------------------------------------------------------- OCR -----
class Ocr:
    def __init__(self, code: str, standins=()):
        self.code = code
        self.used = None            # tag of the engine actually in use
        # Windows lists OCR languages with region tags (uk-UA, ro-RO); match on the
        # language part so "uk" finds "uk-UA" regardless of how the pack was installed
        try:
            available = list(OcrEngine.available_recognizer_languages)
        except Exception:
            available = []
        self.installed = [l.language_tag for l in available]
        def find(c):
            m = next((l for l in available if l.language_tag.lower().split("-")[0] == c.lower()), None)
            eng = OcrEngine.try_create_from_language(m) if m is not None else None
            if eng is None:
                eng = OcrEngine.try_create_from_language(Language(c))
            return eng, ((m.language_tag if m is not None else c) if eng else None)

        self.engine, self.used = find(code)
        self.has_language = self.engine is not None
        self.standin = None
        if self.engine is None:
            for alt in standins:
                self.engine, self.used = find(alt)
                if self.engine is not None:
                    self.standin = alt
                    break
        state = 'ok' if self.has_language else ('stand-in ' + str(self.used) if self.standin else 'MISSING')
        log(f"OCR for {code}: {state}; installed OCR languages: {', '.join(self.installed) or 'none'}")
        if self.engine is None:
            self.engine = OcrEngine.try_create_from_user_profile_languages()
        if self.engine is None:
            raise RuntimeError("Windows OCR is not available on this PC.")

    def recognize(self, bgra: bytes, width: int, height: int):
        bitmap = SoftwareBitmap.create_copy_from_buffer(
            bgra, BitmapPixelFormat.BGRA8, width, height)
        result = asyncio.run(self._run(bitmap))
        lines = []
        for line in result.lines:
            words = list(line.words)
            if not words:
                continue
            l = min(w.bounding_rect.x for w in words)
            t = min(w.bounding_rect.y for w in words)
            r = max(w.bounding_rect.x + w.bounding_rect.width for w in words)
            b = max(w.bounding_rect.y + w.bounding_rect.height for w in words)
            lines.append((line.text, l, t, r, b))
        return lines

    async def _run(self, bitmap):
        return await self.engine.recognize_async(bitmap)


def paragraph_under(lines, cx, cy):
    """Pick the OCR line under the cursor and grow it into its paragraph."""
    if not lines:
        return None
    # cursor may sit slightly beside the text; allow some slack
    def hit(ln):
        _, l, t, r, b = ln
        return l - 20 <= cx <= r + 20 and t - 6 <= cy <= b + 6
    hits = [ln for ln in lines if hit(ln)]
    if not hits:
        return None

    def distance(ln):
        _, l, t, r, b = ln
        dy = abs((t + b) / 2 - cy)
        dx = 0 if l <= cx <= r else min(abs(cx - l), abs(cx - r))
        return (dy, dx)
    anchor = min(hits, key=distance)
    ordered = sorted(lines, key=lambda ln: ln[2])
    idx = ordered.index(anchor)
    line_h = max(8, anchor[4] - anchor[2])
    gap = line_h * 0.9
    block = [anchor]
    # walk upward
    i = idx - 1
    while i >= 0 and len(block) < MAX_LINES:
        cur = ordered[i]
        if block[0][2] - cur[4] <= gap and _overlaps(cur, anchor):
            block.insert(0, cur)
            i -= 1
        else:
            break
    # walk downward
    i = idx + 1
    while i < len(ordered) and len(block) < MAX_LINES:
        cur = ordered[i]
        if cur[2] - block[-1][4] <= gap and _overlaps(cur, anchor):
            block.append(cur)
            i += 1
        else:
            break
    text = " ".join(ln[0] for ln in block)
    return re.sub(r"\s+", " ", text).strip()


def _overlaps(a, b):
    return min(a[3], b[3]) - max(a[1], b[1]) > 0


# ----------------------------------------------------------------- themes -----
# Windows 11 (Fluent) palettes
LIGHT = dict(
    border="#e3e3e3", bg="#f6f6f6", hover="#eaeaea", text="#1a1a1a", secondary="#5a5a5a",
    muted="#767676", hint="#9a9a9a", dim="#b5b5b5", ok="#0f7b3f", error="#c42b1c",
    accent="#0067c0", accent_hover="#1975c5", on_accent="#ffffff", panel="#fbfbfb",
    field="#ffffff", field_border="#e0e0e0", output="#f0f0f0", pill="#ffffff",
    pill_border="#dcdcdc", pill_hover="#f3f3f3", close_hover="#c42b1c", on_close="#ffffff",
    pin="#d13438",
)
DARK = dict(
    border="#3a3a3a", bg="#202020", hover="#333333", text="#ffffff", secondary="#c5c5c5",
    muted="#9d9d9d", hint="#8a8a8a", dim="#5e5e5e", ok="#6ccb5f", error="#ff99a4",
    accent="#60cdff", accent_hover="#8ad8ff", on_accent="#000000", panel="#272727",
    field="#2d2d2d", field_border="#3f3f3f", output="#323232", pill="#3a3a3a",
    pill_border="#4a4a4a", pill_hover="#444444", close_hover="#c42b1c", on_close="#ffffff",
    pin="#ff6b6e",
)


def system_prefers_dark() -> bool:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        return value == 0
    except Exception:
        return False


def apply_win11_chrome(root, dark: bool):
    """Rounded corners + drop shadow through the Desktop Window Manager (Win11)."""
    try:
        hwnd = user32.GetParent(root.winfo_id()) or root.winfo_id()
        dwm = ctypes.windll.dwmapi
        DWMWA_USE_IMMERSIVE_DARK_MODE, DWMWA_WINDOW_CORNER_PREFERENCE = 20, 33
        DWMWCP_ROUND = 2
        pref = ctypes.c_int(DWMWCP_ROUND)
        dwm.DwmSetWindowAttribute(wintypes.HWND(hwnd), DWMWA_WINDOW_CORNER_PREFERENCE,
                                  ctypes.byref(pref), ctypes.sizeof(pref))
        mode = ctypes.c_int(1 if dark else 0)
        dwm.DwmSetWindowAttribute(wintypes.HWND(hwnd), DWMWA_USE_IMMERSIVE_DARK_MODE,
                                  ctypes.byref(mode), ctypes.sizeof(mode))
        # popup windows get no shadow by default; CS_DROPSHADOW gives them one
        GCL_STYLE, CS_DROPSHADOW = -26, 0x00020000
        user32.SetClassLongPtrW.restype = ctypes.c_void_p
        user32.SetClassLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
        user32.GetClassLongPtrW.restype = ctypes.c_void_p
        user32.GetClassLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
        style = user32.GetClassLongPtrW(hwnd, GCL_STYLE) or 0
        user32.SetClassLongPtrW(hwnd, GCL_STYLE, style | CS_DROPSHADOW)
    except Exception as e:
        log(f"win11 chrome not applied: {e}")


def ui_font(size, weight="normal"):
    return (UI_FAMILY, size, weight)


UI_FAMILY = "Segoe UI"


# ------------------------------------------------------- rounded widgets ----
def round_rect(canvas, x1, y1, x2, y2, r, **kw):
    """A smooth polygon that renders as a rounded rectangle."""
    r = max(0, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
           x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return canvas.create_polygon(pts, smooth=True, **kw)


class RoundButton(tk.Canvas):
    """A flat, rounded button drawn on a canvas (Tk buttons can't have round corners)."""

    def __init__(self, parent, text, command, font, fill, fg, hover_fill, hover_fg=None,
                 outline=None, canvas_bg=None, padx=12, pady=6, radius=6, min_width=0, icon=None):
        super().__init__(parent, highlightthickness=0, bd=0, bg=canvas_bg or parent.cget("bg"),
                         cursor="hand2")
        self.command, self.text, self.font = command, text, font
        self.icon = icon              # None, "pause" or "play": drawn instead of text
        self.fill, self.fg, self.hover_fill = fill, fg, hover_fill
        self.hover_fg, self.outline = hover_fg or fg, outline
        self.padx, self.pady, self.radius, self.min_width = padx, pady, radius, min_width
        self.hovered = False
        self.bind("<Enter>", lambda e: self._set_hover(True))
        self.bind("<Leave>", lambda e: self._set_hover(False))
        self.bind("<Button-1>", lambda e: self.command())
        self.redraw()

    def _set_hover(self, on):
        self.hovered = on
        self.redraw()

    def config_colors(self, fill=None, fg=None, hover_fill=None, hover_fg=None, outline="keep",
                      canvas_bg=None):
        if fill: self.fill = fill
        if fg: self.fg = fg
        if hover_fill: self.hover_fill = hover_fill
        if hover_fg: self.hover_fg = hover_fg
        if outline != "keep": self.outline = outline
        if canvas_bg: self.configure(bg=canvas_bg)
        self.redraw()

    def set_text(self, text):
        self.text = text
        self.redraw()

    def set_icon(self, icon):
        self.icon = icon
        self.redraw()

    def _draw_icon(self, cx, cy, color):
        if self.icon == "pause":
            for dx in (-3.5, 1.5):
                round_rect(self, cx + dx, cy - 5, cx + dx + 2.5, cy + 5, 1.2, fill=color, outline=color)
        elif self.icon == "play":
            self.create_polygon(cx - 3.5, cy - 5.5, cx - 3.5, cy + 5.5, cx + 5, cy,
                                fill=color, outline=color, joinstyle="round", smooth=False)

    def redraw(self):
        import tkinter.font as tkfont
        f = tkfont.Font(font=self.font)
        w = max(self.min_width, f.measure(self.text) + 2 * self.padx)
        h = f.metrics("linespace") + 2 * self.pady
        self.configure(width=w, height=h)
        self.delete("all")
        fill = self.hover_fill if self.hovered else self.fill
        fg = self.hover_fg if self.hovered else self.fg
        outline = self.outline or fill
        round_rect(self, 1, 1, w - 1, h - 1, self.radius, fill=fill, outline=outline, width=1)
        if self.icon:
            self._draw_icon(w / 2, h / 2, fg)
        else:
            self.create_text(w / 2, h / 2, text=self.text, font=self.font, fill=fg)


class RoundBox(tk.Canvas):
    """A rounded container. Create the child with master=box, then box.set_child(child)."""

    def __init__(self, parent, fill, outline, radius=6, pad=1, canvas_bg=None):
        super().__init__(parent, highlightthickness=0, bd=0, bg=canvas_bg or parent.cget("bg"))
        self.fill, self.outline, self.radius, self.pad = fill, outline, radius, pad
        self.child = None
        self.bind("<Configure>", lambda e: self.redraw())

    def set_child(self, child):
        self.child = child
        self.redraw()

    def config_colors(self, fill=None, outline=None, canvas_bg=None):
        if fill: self.fill = fill
        if outline: self.outline = outline
        if canvas_bg: self.configure(bg=canvas_bg)
        self.redraw()

    def redraw(self):
        self.delete("all")
        w = self.winfo_width() if self.winfo_width() > 1 else self.winfo_reqwidth()
        inset = self.pad + 3          # keeps the child's square corners inside the arc
        if self.child is not None:
            self.child.update_idletasks()
            h = self.child.winfo_reqheight() + 2 * inset
            self.configure(height=h)
        else:
            h = self.winfo_height()
        round_rect(self, 1, 1, w - 1, h - 1, self.radius, fill=self.fill, outline=self.outline, width=1)
        if self.child is not None:
            self.create_window(inset, inset, anchor="nw", window=self.child, width=w - 2 * inset)


# ------------------------------------------------------------- the window ----
class App:
    def __init__(self):
        global UI_FAMILY
        self.dark = system_prefers_dark()
        self.t = DARK if self.dark else LIGHT

        self.settings = load_settings()
        self.lang = LANGUAGES.get(self.settings.get("lang", "ro"), LANGUAGES["ro"])

        self.root = tk.Tk()
        self.root.title(f"{self.lang['flag']} → EN")
        self.root.overrideredirect(True)
        self.lang_changed = False          # tells the worker to reload the OCR engine
        self.root.attributes("-topmost", bool(self.settings.get("pinned", True)))
        self.root.configure(bg=self.t["border"])
        self.root.geometry(f"{WINDOW_W}x150+{self._start_x()}+{self._start_y()}")
        self.root.protocol("WM_DELETE_WINDOW", self.quit)
        try:
            import tkinter.font as tkfont
            fams = set(tkfont.families())
            for cand in ("Segoe UI Variable Text", "Segoe UI Variable", "Segoe UI"):
                if cand in fams:
                    UI_FAMILY = cand
                    break
        except Exception:
            pass

        self.paused = False
        self.resumed = False
        self.ocr_missing = False
        self.ocr_standin = None
        self.read_via = "ocr"          # how the last text was obtained: app / clipboard / ocr
        self.pinned = bool(self.settings.get("pinned", True))
        self.hovered = False
        self.cache = TranslationCache()  # text -> translation, or None if not Romanian
        self.backoff = 5         # seconds to wait after a 429, doubles each time
        self.backoff_until = 0.0
        self.drag_widgets = []   # anything you can grab to move the window
        self.icon_btns = []      # (button, is_close)

        t = self.t
        frame = tk.Frame(self.root, bg=t["bg"])
        frame.pack(fill="both", expand=True, padx=1, pady=1)

        # -- top row: status line at left, faint icon controls at right --
        top = tk.Frame(frame, bg=t["bg"])
        top.pack(fill="x", padx=(14, 6), pady=(4, 0))
        # the icon strip is packed FIRST so it always owns the right edge; the status
        # label gets whatever width is left and wraps inside it
        ctl = tk.Frame(top, bg=t["bg"])
        ctl.pack(side="right", anchor="n")
        self.close_btn = self._icon_btn(ctl, "✕", self.quit, is_close=True)
        self.pause_btn = self._icon_btn(ctl, "", self.toggle_pause, icon="pause")
        self.pin_btn = self._icon_btn(ctl, "📌", self.toggle_pin)
        self.menu_btn = self._icon_btn(ctl, "☰", self.open_menu)
        self.menu_win = None
        self.about_win = None
        for b in (self.close_btn, self.pause_btn, self.pin_btn, self.menu_btn):
            b.pack(side="right", padx=1)
        self.ctl = ctl
        ICONS_W = 4 * 30 + 8
        self.status = tk.Label(top, text="", bg=t["bg"], fg=t["hint"], font=ui_font(8),
                               anchor="w", justify="left", wraplength=WINDOW_W - 20 - ICONS_W - 12)
        self.status.pack(side="left", fill="x", expand=True, pady=4, padx=(0, 8))

        body = tk.Frame(frame, bg=t["bg"])
        body.pack(fill="both", expand=True, padx=14, pady=(0, 4))
        self.source = tk.Label(body, text="", bg=t["bg"], fg=t["muted"], justify="left",
                               font=ui_font(8), anchor="w", wraplength=WINDOW_W - 32)
        self.source.pack(fill="x", pady=(0, 2))
        self.target = tk.Label(body, text="", bg=t["bg"], fg=t["text"], justify="left",
                               font=ui_font(10), anchor="w", wraplength=WINDOW_W - 32)
        self.target.pack(fill="x")

        # -- "Reply in Romanian" accent button, lower-right --
        btn_row = tk.Frame(frame, bg=t["bg"])
        btn_row.pack(fill="x", padx=14, pady=(6, 12))
        self.reply_btn = RoundButton(btn_row, self._reply_label(False), self.toggle_reply,
                                     ui_font(9, "bold"), fill=t["accent"], fg=t["on_accent"],
                                     hover_fill=t["accent_hover"], padx=12, pady=6, radius=6)
        self.reply_btn.pack(side="right")

        # -- collapsible reply panel --
        self.reply_open = False
        self.reply_job = None
        self.panel = tk.Frame(frame, bg=t["panel"])
        self.panel_sep = tk.Frame(self.panel, bg=t["border"], height=1)
        self.panel_sep.pack(fill="x")
        inner = tk.Frame(self.panel, bg=t["panel"])
        inner.pack(fill="both", expand=True, padx=14, pady=(10, 14))
        self.panel_title = tk.Label(inner, text=f"English → {self.lang['name']}", bg=t["panel"], fg=t["secondary"],
                                    font=ui_font(8), anchor="w")
        self.panel_title.pack(fill="x")

        # rounded text box; outline turns accent while it has focus
        self.entry_box = RoundBox(inner, fill=t["field"], outline=t["field_border"], radius=6)
        self.entry_box.pack(fill="x", pady=(4, 0))
        self.entry = tk.Text(self.entry_box, height=3, wrap="word", font=ui_font(10), bg=t["field"],
                             fg=t["text"], insertbackground=t["text"], relief="flat", bd=0,
                             highlightthickness=0, padx=7, pady=4, undo=True)
        self.entry_box.set_child(self.entry)
        self.entry.bind("<KeyRelease>", self._on_type)
        self.entry.bind("<Return>", self._on_enter)
        self.entry.bind("<FocusIn>", lambda e: self.entry_box.config_colors(outline=self.t["accent"]))
        self.entry.bind("<FocusOut>", lambda e: self.entry_box.config_colors(outline=self.t["field_border"]))
        self.panel_hint = tk.Label(inner, text="Translates as you type · Enter also translates",
                                   bg=t["panel"], fg=t["hint"], font=ui_font(8), anchor="w")
        self.panel_hint.pack(fill="x", pady=(4, 8))

        self.out_box = RoundBox(inner, fill=t["output"], outline=t["output"], radius=6)
        self.out_box.pack(fill="x")
        self.out = out = tk.Frame(self.out_box, bg=t["output"])
        self.output = tk.Label(out, text="", bg=t["output"], fg=t["text"], justify="left", anchor="nw",
                               font=ui_font(10), wraplength=WINDOW_W - 135, cursor="hand2")
        self.output.pack(side="left", fill="both", expand=True, padx=(6, 6), pady=5)
        self.output.bind("<Button-1>", lambda e: self.copy_output())
        self.copy_btn = RoundButton(out, "⧉  Copy", self.copy_output, ui_font(8), fill=t["pill"],
                                    fg=t["text"], hover_fill=t["pill_hover"], outline=t["pill_border"],
                                    canvas_bg=t["output"], padx=9, pady=3, radius=5)
        self.copy_btn.pack(side="right", anchor="n", padx=4, pady=5)
        self.out_box.set_child(out)
        self.copied_lbl = tk.Label(inner, text="", bg=t["panel"], fg=t["ok"], font=ui_font(8), anchor="e")
        self.copied_lbl.pack(fill="x", pady=(6, 0))

        # anything that isn't a control moves the window
        self.drag_widgets = [frame, top, self.status, body, self.source, self.target, btn_row,
                             self.panel, inner, self.panel_title, self.panel_hint, self.copied_lbl]
        for w in self.drag_widgets:
            w.bind("<ButtonPress-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag_move)
            w.bind("<ButtonRelease-1>", self._remember_position)

        self.frame, self.top, self.body, self.btn_row, self.inner = frame, top, body, btn_row, inner
        self.root.bind_all("<ButtonPress-1>", self._on_click_anywhere, add="+")
        self.set_idle(self.idle_msg())
        self.root.update_idletasks()
        apply_win11_chrome(self.root, self.dark)
        self._set_controls_faint(True)
        threading.Thread(target=self.worker, daemon=True).start()
        self.root.after(2000, self._watch_theme)
        self.root.after(200, self._watch_hover)

    # -- fluent icon buttons --
    def _icon_btn(self, parent, glyph, command, is_close=False, icon=None):
        t = self.t
        b = RoundButton(parent, glyph, command, ui_font(9), fill=t["bg"], fg=t["dim"],
                        hover_fill=t["close_hover"] if is_close else t["hover"],
                        hover_fg=t["on_close"] if is_close else t["text"],
                        padx=7, pady=3, radius=5, min_width=28, icon=icon)
        b.is_close = is_close
        self.icon_btns.append(b)
        return b

    def _control_fg(self):
        return self.t["secondary"] if self.hovered else self.t["dim"]

    def _set_controls_faint(self, faint: bool):
        self.hovered = not faint
        for b in self.icon_btns:
            b.config_colors(fg=self._control_fg(), fill=self.t["bg"], canvas_bg=self.t["bg"])
        self._style_pin()

    def _style_pin(self):
        """Red pin while always-on-top is on, faint gray when it's off."""
        if self.pinned:
            self.pin_btn.config_colors(fg=self.t["pin"], hover_fg=self.t["pin"])
        else:
            self.pin_btn.config_colors(fg=self.t["dim"], hover_fg=self.t["text"])

    # -- language --
    def _reply_label(self, open_):
        return f"✎   Reply in {self.lang['name']}   " + ("▴" if open_ else "▾")

    def idle_msg(self):
        return f"Hover over {self.lang['name']} text"

    def set_language(self, code):
        if code not in LANGUAGES or LANGUAGES[code] is self.lang:
            return
        self.lang = LANGUAGES[code]
        self.settings["lang"] = code
        save_settings(self.settings)
        self.cache = TranslationCache()
        self.lang_changed = True
        self.root.title(f"{self.lang['flag']} → EN")
        self.reply_btn.set_text(self._reply_label(self.reply_open))
        self.panel_title.config(text=f"English → {self.lang['name']}")
        self.entry.delete("1.0", "end")
        self.output.config(text="")
        self.set_idle(f"Switched to {self.lang['name']}")
        log(f"language -> {code}")

    def _load_ocr(self):
        """OCR engine for the current language; warns if its Windows pack is missing."""
        L = self.lang
        try:
            ocr = Ocr(L["code"], L.get("ocr_standins", ()))
        except Exception as e:
            self.show_error(str(e))
            return None
        self.ocr_standin = ocr.standin          # e.g. "ru" when reading Ukrainian with Russian OCR
        self.ocr_missing = not ocr.has_language and not ocr.standin
        if ocr.standin:
            name = LANG_NAMES.get(ocr.standin, ocr.standin)
            self.set_idle(f"Windows has no {L['name']} OCR — reading with {name} OCR instead")
        elif self.ocr_missing and uia is not None:
            self.set_idle(f"{L['name']}: reading text from apps directly (no {L['name']} OCR on this PC)")
        elif self.ocr_missing:
            have = ", ".join(ocr.installed) or "none"
            log(f"OCR pack for {L['code']} missing; installed OCR languages: {have}")
            self.show_error(self.ocr_missing_msg())
        return ocr

    def ocr_missing_msg(self):
        L = self.lang
        if L.get("ocr_standins"):
            alt = L["ocr_standins"][0]
            tag = {"ru": "ru-RU", "sr": "sr-CYRL-RS", "bg": "bg-BG"}.get(alt, alt)
            return (f"⚠ Windows doesn't offer {L['name']} OCR, so the screen is being read as "
                    f"English. Install {LANG_NAMES.get(alt, alt)} OCR as a stand-in — PowerShell as "
                    f"administrator: Add-WindowsCapability -Online -Name \"Language.OCR~~~{tag}~0.0.1.0\" "
                    f"— then restart the app.")
        native = {"ro": "Română"}.get(L["code"], L["name"])
        return (f"⚠ Windows has no {L['name']} OCR, so the screen is being read as English. "
                f"Settings › Time & Language › Language & region › {native} › Language options › "
                f"make sure \"Optical character recognition\" is installed, then switch languages "
                f"again or restart the app.")

    def accept_detected(self, detected, text):
        """Is Google's detected language good enough to show? Lenient under stand-in OCR,
        which loses the marker letters that separate Ukrainian from Russian."""
        L = self.lang
        if is_target(detected, text, L):
            return True
        return (bool(getattr(self, "ocr_standin", None)) and self.read_via == "ocr"
                and detected in L["confusable"])

    # -- "☰" menu and About dialog --
    def _popup(self, w, h, x, y):
        """A themed, rounded, borderless toplevel placed at screen coords."""
        win = tk.Toplevel(self.root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg=self.t["border"])
        win.geometry(f"{w}x{h}+{x}+{y}")
        win.update_idletasks()
        apply_win11_chrome(win, self.dark)
        return win

    def open_menu(self):
        if self.menu_win is not None:
            self.close_menu()
            return
        t = self.t
        bx, by = self.menu_btn.winfo_rootx(), self.menu_btn.winfo_rooty()
        win = self._popup(190, 120, bx + self.menu_btn.winfo_width() - 190, by + self.menu_btn.winfo_height() + 4)
        self.menu_win = win
        inner = tk.Frame(win, bg=t["panel"])
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        self.menu_inner = inner
        self.lang_expanded = False
        self._build_menu_items()
        win.bind("<Escape>", lambda e: self.close_menu())
        # clicks in other apps don't reach Tk, so also close if the mouse presses elsewhere
        self._menu_poll()

    def _build_menu_items(self):
        """(Re)draws the menu: Languages ▸ (expandable) and About."""
        t = self.t
        inner = self.menu_inner
        for child in inner.winfo_children():
            child.destroy()

        def item(label, cmd, indent=0, keep_open=False):
            action = cmd if keep_open else (lambda c=cmd: (self.close_menu(), c()))
            b = RoundButton(inner, label, action, ui_font(9), fill=t["panel"], fg=t["text"],
                            hover_fill=t["hover"], padx=10, pady=6, radius=5, min_width=180 - indent)
            b.pack(padx=(4 + indent, 4), pady=2, anchor="w")
            return b

        arrow = "▾" if self.lang_expanded else "▸"
        item(f"🌐   Languages   {arrow}", self._toggle_lang_list, keep_open=True)
        if self.lang_expanded:
            for code, L in LANGUAGES.items():
                mark = "●" if L is self.lang else "○"
                item(f"{mark}   {L['name']}", lambda c=code: self.set_language(c), indent=16)
        tk.Frame(inner, bg=t["border"], height=1).pack(fill="x", padx=8, pady=3)
        item("ⓘ   About " + APP_NAME, self.open_about)
        win = self.menu_win
        win.update_idletasks()
        win.geometry(f"190x{inner.winfo_reqheight() + 2}")

    def _toggle_lang_list(self):
        self.lang_expanded = not self.lang_expanded
        self._build_menu_items()

    def _menu_poll(self):
        if self.menu_win is None:
            return
        if mouse_down() and not self.over_own_window(*cursor_pos()):
            self.close_menu()
            return
        self.root.after(100, self._menu_poll)

    def close_menu(self):
        if self.menu_win is not None:
            self.menu_win.destroy()
            self.menu_win = None

    def open_about(self):
        if self.about_win is not None:
            self.take_focus(self.about_win)
            return
        t = self.t
        w, h = 360, 330
        x = self.root.winfo_rootx() + (self.root.winfo_width() - w) // 2
        y = self.root.winfo_rooty() + 40
        win = self._popup(w, h, x, y)
        self.about_win = win
        inner = tk.Frame(win, bg=t["bg"])
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        for widget in (win, inner):
            widget.bind("<ButtonPress-1>", self._drag_start_win)
            widget.bind("<B1-Motion>", self._drag_move_win)

        tk.Label(inner, text=APP_NAME, bg=t["bg"], fg=t["text"], font=ui_font(12, "bold"),
                 anchor="w").pack(fill="x", padx=20, pady=(18, 0))
        tk.Label(inner, text=f"Version {APP_VERSION}  ·  {APP_BUILT}", bg=t["bg"], fg=t["muted"],
                 font=ui_font(8), anchor="w").pack(fill="x", padx=20, pady=(2, 12))
        body = (
            "Translates Romanian or Ukrainian into English while you use your PC. "
            "Rest the mouse on text in the chosen language to see it in English, "
            "highlight a passage to translate all of it, or type English in the reply "
            "box to get the chosen language back, ready to paste into a chat.\n\n"
            "The screen is read with Windows' built-in OCR and translated with Google "
            "Translate. Only text that appears to be in the chosen language is sent to "
            "Google; everything else stays on this PC. Choose the language from the ☰ menu.\n\n"
            "Written in Python with Tkinter, PyWinRT, mss and requests.\n"
            "Designed and built by NavyPirate83 together with Claude (Anthropic)."
        )
        tk.Label(inner, text=body, bg=t["bg"], fg=t["secondary"], font=ui_font(9), justify="left",
                 anchor="nw", wraplength=w - 42).pack(fill="x", padx=20)
        row = tk.Frame(inner, bg=t["bg"])
        row.pack(fill="x", padx=20, pady=(16, 16))
        RoundButton(row, "Close", self.close_about, ui_font(9, "bold"), fill=t["accent"],
                    fg=t["on_accent"], hover_fill=t["accent_hover"], padx=18, pady=5,
                    radius=6).pack(side="right")
        # size the dialog to its content so nothing is ever cut off
        win.update_idletasks()
        h = inner.winfo_reqheight() + 2
        win.geometry(f"{w}x{h}+{x}+{y}")
        win.bind("<Escape>", lambda e: self.close_about())
        self.take_focus(win)

    def close_about(self):
        if self.about_win is not None:
            self.about_win.destroy()
            self.about_win = None

    def _drag_start_win(self, e):
        win = e.widget.winfo_toplevel()
        self._wdx, self._wdy = e.x_root - win.winfo_x(), e.y_root - win.winfo_y()

    def _drag_move_win(self, e):
        win = e.widget.winfo_toplevel()
        win.geometry(f"+{e.x_root - self._wdx}+{e.y_root - self._wdy}")

    def _watch_hover(self):
        over = self.over_own_window(*cursor_pos())
        if over != self.hovered:
            self._set_controls_faint(not over)
        self.root.after(200, self._watch_hover)

    # -- theme --
    def _watch_theme(self):
        dark = system_prefers_dark()
        if dark != self.dark:
            self.dark = dark
            self.t = DARK if dark else LIGHT
            self.apply_theme()
        self.root.after(2000, self._watch_theme)

    def apply_theme(self):
        t = self.t
        self.root.configure(bg=t["border"])
        for w in (self.frame, self.top, self.ctl, self.body, self.btn_row, self.status, self.source, self.target):
            w.configure(bg=t["bg"])
        self.source.configure(fg=t["muted"])
        self.target.configure(fg=t["text"])
        for b in self.icon_btns:
            b.config_colors(hover_fill=t["close_hover"] if b.is_close else t["hover"],
                            hover_fg=t["on_close"] if b.is_close else t["text"])
        self._set_controls_faint(not self.hovered)
        self.reply_btn.config_colors(fill=t["accent"], fg=t["on_accent"], hover_fill=t["accent_hover"],
                                     canvas_bg=t["bg"])
        for w in (self.panel, self.inner, self.panel_title, self.panel_hint, self.copied_lbl):
            w.configure(bg=t["panel"])
        self.panel_sep.configure(bg=t["border"])
        self.panel_title.configure(fg=t["secondary"])
        self.panel_hint.configure(fg=t["hint"])
        self.entry_box.config_colors(fill=t["field"], outline=t["field_border"], canvas_bg=t["panel"])
        self.entry.configure(bg=t["field"], fg=t["text"], insertbackground=t["text"])
        self.out_box.config_colors(fill=t["output"], outline=t["output"], canvas_bg=t["panel"])
        self.out.configure(bg=t["output"])
        self.output.configure(bg=t["output"], fg=t["text"])
        self.copy_btn.config_colors(fill=t["pill"], fg=t["text"], hover_fill=t["pill_hover"],
                                    outline=t["pill_border"], canvas_bg=t["output"])
        apply_win11_chrome(self.root, self.dark)
        self._show(*self._last_shown)

    # -- reply panel --
    def toggle_reply(self):
        self.reply_open = not self.reply_open
        if self.reply_open:
            self.panel.pack(fill="x")
            self.reply_btn.set_text(self._reply_label(True))
            self.take_focus(self.entry)
            self.root.after(60, lambda: self.take_focus(self.entry))   # again after layout
        else:
            self.panel.pack_forget()
            self.reply_btn.set_text(self._reply_label(False))
        self._fit()

    def hwnd(self, win=None):
        win = win or self.root
        return user32.GetParent(win.winfo_id()) or win.winfo_id()

    def take_focus(self, widget=None):
        """Activate the widget's toplevel at the OS level, then focus the widget."""
        widget = widget or self.root
        top = widget.winfo_toplevel()
        activate_window(self.hwnd(top))
        top.focus_force()
        widget.focus_set()

    def _on_click_anywhere(self, event):
        try:
            top = event.widget.winfo_toplevel()
        except Exception:
            return
        # a click outside the ⋯ menu closes it (the menu button itself toggles it)
        if self.menu_win is not None and top is not self.menu_win and event.widget is not self.menu_btn:
            self.close_menu()
        target = self.entry if event.widget is self.entry else None
        if user32.GetForegroundWindow() != self.hwnd(top):
            self.take_focus(target or top)
        elif target is not None:
            target.focus_set()

    def _on_type(self, event=None):
        if event and event.keysym in ("Shift_L", "Shift_R", "Control_L", "Control_R", "Return"):
            return
        if self.reply_job:
            self.root.after_cancel(self.reply_job)
        self.reply_job = self.root.after(700, self.translate_reply)

    def _on_enter(self, event):
        if event.state & 0x0001:          # Shift+Enter inserts a newline
            return None
        if self.reply_job:
            self.root.after_cancel(self.reply_job)
        self.translate_reply()
        return "break"

    def translate_reply(self):
        self.reply_job = None
        text = self.entry.get("1.0", "end").strip()
        if not text:
            self.output.config(text="")
            self._fit()
            return
        self.copied_lbl.config(text="Translating…", fg=self.t["hint"])
        threading.Thread(target=self._translate_reply_bg, args=(text,), daemon=True).start()

    def _translate_reply_bg(self, text):
        try:
            translated, _ = google_translate(text, "en", self.lang["code"])
            msg, color = "", "ok"
        except RateLimited:
            translated, msg, color = "", "Google is rate-limiting us — try again in a moment", "error"
        except Exception as e:
            translated, msg, color = "", f"Error: {e}", "error"
        def apply():
            if translated:
                self.output.config(text=translated)
            self.copied_lbl.config(text=msg, fg=self.t[color])
            self._fit()
        self.root.after(0, apply)

    def copy_output(self):
        text = self.output.cget("text").strip()
        if not text:
            return
        with _clipboard_lock:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.root.update()                 # push to the Windows clipboard now
        self.copied_lbl.config(text="✓  Copied — paste into your chat", fg=self.t["ok"])
        # clear the English draft so the box is ready for the next message;
        # the Romanian stays visible until the note fades, then goes too
        if self.reply_job:
            self.root.after_cancel(self.reply_job)
            self.reply_job = None
        self.entry.delete("1.0", "end")
        self.entry.edit_reset()
        self.take_focus(self.entry)

        def fade():
            self.copied_lbl.config(text="")
            self.output.config(text="")
            self._fit()
        self.root.after(2500, fade)

    def _fit(self):
        """Resize the window to whatever is currently packed."""
        if self.reply_open:
            self.out_box.redraw()
            self.entry_box.redraw()
        self.root.update_idletasks()
        h = self.root.winfo_children()[0].winfo_reqheight() + 2
        self.root.geometry(f"{WINDOW_W}x{h}")

    # -- window chrome --
    def _drag_start(self, e):
        self._dx, self._dy = e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y()

    def _start_x(self):
        vx, vy, vw, vh = virtual_screen()
        x = self.settings.get("x")
        if isinstance(x, int) and vx - 20 <= x <= vx + vw - 60:
            return x
        return self.root.winfo_screenwidth() - WINDOW_W - 40

    def _start_y(self):
        vx, vy, vw, vh = virtual_screen()
        y = self.settings.get("y")
        if isinstance(y, int) and vy - 20 <= y <= vy + vh - 60:
            return y
        return 80

    def _remember_position(self, e=None):
        pos = {"x": self.root.winfo_x(), "y": self.root.winfo_y()}
        if pos != {"x": self.settings.get("x"), "y": self.settings.get("y")}:
            self.settings.update(pos)
            save_settings(self.settings)

    def quit(self):
        self._remember_position()
        self.root.destroy()

    def _drag_move(self, e):
        self.root.geometry(f"+{e.x_root - self._dx}+{e.y_root - self._dy}")

    def toggle_pause(self):
        self.paused = not self.paused
        self.pause_btn.set_icon("play" if self.paused else "pause")
        if self.paused:
            self.set_idle("Paused — click ▶ to resume")
        else:
            self.resumed = True            # worker forgets the last hover spot
            self.set_idle(self.idle_msg())

    def toggle_pin(self):
        self.pinned = not self.pinned
        self.root.attributes("-topmost", self.pinned)
        self.settings["pinned"] = self.pinned
        save_settings(self.settings)
        self._set_controls_faint(not self.hovered)

    def over_own_window(self, x, y):
        for win in (self.root, getattr(self, "menu_win", None), getattr(self, "about_win", None)):
            if win is None:
                continue
            try:
                wx, wy = win.winfo_rootx(), win.winfo_rooty()
                if wx <= x <= wx + win.winfo_width() and wy <= y <= wy + win.winfo_height():
                    return True
            except tk.TclError:
                pass
        return False

    # -- display (always called on the tk thread via after) --
    def set_idle(self, msg):
        self.root.after(0, lambda: self._show(msg, "", "", "hint"))

    def show_translation(self, src, dst, status=None):
        status = status or f"{self.lang['name']} detected"
        self.root.after(0, lambda: self._show(status, src, dst, "ok"))

    def show_not_target(self, text, detected=""):
        snippet = text if len(text) <= 90 else text[:90] + "…"
        if getattr(self, "ocr_missing", False) and self.lang["script"] and self.read_via == "ocr":
            # the OCR can't even read this alphabet — say so instead of "Not Ukrainian"
            self.root.after(0, lambda: self._show(self.ocr_missing_msg(), "Read: " + snippet, "", "error"))
            return
        note = not_target_note(text, detected, self.lang)
        self.root.after(0, lambda: self._show(note,
                                              "Read: " + snippet, "", "hint"))

    def show_error(self, msg):
        self.root.after(0, lambda: self._show(msg, "", "", "error"))

    _last_shown = ("", "", "", "hint")

    def _show(self, status, src, dst, color):
        """status goes on the top line with a colored dot; src/target below it."""
        self._last_shown = (status, src, dst, color)
        self.status.config(text=("●  " + status) if status else "",
                           fg=self.t["ok"] if color == "ok" else
                           self.t["error"] if color == "error" else self.t["muted"])
        self.source.config(text=src)
        self.target.config(text=dst, fg=self.t["text"])
        if src:
            self.source.pack_configure(pady=(2, 2))
        else:
            self.source.pack_configure(pady=(0, 0))
        self._fit()

    # -- background loop --
    def worker(self):
        ocr = self._load_ocr()
        if ocr is None:
            return
        uia_ctx = None
        if uia is None:
            log(f"UI Automation reader NOT available (OCR only): {UIA_IMPORT_ERROR}")
            self.show_error("Direct text reading isn't available in this build — rebuild with "
                            "build.bat so the uiautomation package is included. Using OCR.")
            time.sleep(5)
            self.set_idle(self.idle_msg())
        if uia is not None:
            try:
                uia_ctx = uia.UIAutomationInitializerInThread()
                uia.SetGlobalSearchTimeout(1.0)
                log("UI Automation reader active")
            except Exception as e:
                log(f"UI Automation unavailable, OCR only: {e}")

        sct = mss.mss()
        vx, vy, vw, vh = virtual_screen()
        last_pos, rest_since, last_done = None, time.time(), None
        press_pos, was_down = None, False
        while True:
            time.sleep(POLL_INTERVAL)
            if self.paused:
                continue
            if self.lang_changed:
                self.lang_changed = False
                ocr = self._load_ocr() or ocr
                last_done = None
            if self.resumed:
                self.resumed = False
                last_done, press_pos, was_down = None, None, False
            pos = cursor_pos()

            # -- highlight detection: left button pressed, dragged, released --
            down = mouse_down()
            if down and not was_down:
                press_pos = pos if not self.over_own_window(*pos) else None
            elif not down and was_down and press_pos:
                dx, dy = pos[0] - press_pos[0], pos[1] - press_pos[1]
                if dx * dx + dy * dy >= SELECT_MIN_DRAG ** 2 and not self.over_own_window(*pos):
                    try:
                        self.process_selection(sct, ocr, press_pos, pos, (vx, vy, vw, vh))
                    except Exception as e:
                        self.show_error(f"Error: {e}")
                    last_done = pos
                press_pos = None
            was_down = down
            if down:
                last_pos = pos          # don't hover-translate mid-drag
                continue

            if pos != last_pos:
                last_pos, rest_since = pos, time.time()
                continue
            if time.time() - rest_since < HOVER_DELAY or pos == last_done:
                continue
            last_done = pos
            if self.over_own_window(*pos):
                continue
            try:
                self.process(sct, ocr, pos, (vx, vy, vw, vh))
            except Exception as e:
                self.show_error(f"Error: {e}")

    def selection_via_clipboard(self):
        """Ask the focused app to copy its selection; returns the text or None."""
        marker = "\u200b__rohover__\u200b"
        if not _clipboard_lock.acquire(timeout=0.3):
            return None
        try:
            previous = clipboard_text()
            if not set_clipboard_text(marker):
                return None
            if modifier_held() or not send_ctrl_c():
                log("selection: skipped Ctrl+C (modifier held or SendInput blocked)")
                if previous is not None:
                    set_clipboard_text(previous)
                else:
                    clear_clipboard()
                return None
            text = None
            for _ in range(8):                     # up to ~0.8 s for the app to respond
                time.sleep(0.1)
                got = clipboard_text()
                if got is not None and got != marker:
                    text = got
                    break
            if previous is not None:
                if not set_clipboard_text(previous):
                    log("selection: failed to restore previous clipboard text")
            else:
                # Marker replaced whatever was there (or empty). Do not write "".
                if not clear_clipboard():
                    log("selection: failed to clear clipboard marker")
            return text
        finally:
            _clipboard_lock.release()

    def selection_via_ocr(self, sct, ocr, start, end, vscreen):
        """Fallback: OCR the rectangle spanned by the drag, widened to full lines."""
        vx, vy, vw, vh = vscreen
        (x1, y1), (x2, y2) = start, end
        top, bottom = min(y1, y2) - 8, max(y1, y2) + 8
        # a multi-line selection covers whole lines, so grab generously sideways
        left, right = min(x1, x2) - 450, max(x1, x2) + 450
        left, top = max(vx, left), max(vy, top)
        right, bottom = min(vx + vw, right), min(vy + vh, bottom)
        w, h = right - left, bottom - top
        if w < 20 or h < 8:
            return None
        h = min(h, 1200)
        shot = sct.grab({"left": left, "top": top, "width": w, "height": h})
        lines = ocr.recognize(bytes(shot.raw), shot.width, shot.height)
        if not lines:
            return None
        # keep lines whose vertical extent falls inside the drag; on the first and
        # last line, keep only the words to the right/left of the drag endpoints
        first_y, last_y = y1 - top, y2 - top
        if y2 < y1:
            first_y, last_y, x1, x2 = last_y, first_y, x2, x1
        ordered = sorted(lines, key=lambda ln: ln[2])
        kept = [ln[0] for ln in ordered if ln[4] >= first_y - 4 and ln[2] <= last_y + 4]
        return " ".join(kept) if kept else None

    def process_selection(self, sct, ocr, start, end, vscreen):
        """Translate everything the user just highlighted."""
        time.sleep(0.15)                       # let the app finish its selection
        text = uia_selection_at(*end)
        self.read_via = "app" if text else "clipboard"
        if not text or not text.strip():
            text = self.selection_via_clipboard()
        if not text or not text.strip():
            log("selection: app did not copy; falling back to OCR of the dragged area")
            self.read_via = "ocr"
            text = self.selection_via_ocr(sct, ocr, start, end, vscreen)
        if not text or not text.strip():
            log("selection: nothing found")
            self.set_idle("Couldn't read the highlighted text")
            return
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) > SELECT_MAX_CHARS:
            text = text[:SELECT_MAX_CHARS]
        log(f"selection: {text!r}")

        if text in self.cache and self.cache[text]:
            self.show_translation(text, self.cache[text], "Highlighted text")
            return
        if time.time() < self.backoff_until:
            self.show_error("Google is rate-limiting us; try again shortly")
            return
        self.root.after(0, lambda: self._show("Translating highlighted text…", text, "", "ok"))
        try:
            L = self.lang
            if surely_target(text, L):
                translated, _ = google_translate(expand_abbreviations(text, L), L["code"])
                detected = L["code"]
            else:
                translated, detected = google_translate(text)
                if detected != "en" and self.accept_detected(detected, text):
                    translated, _ = google_translate(expand_abbreviations(text, L), L["code"])
                    detected = L["code"]
        except RateLimited:
            self.backoff = min(self.backoff * 2, 300)
            self.backoff_until = time.time() + self.backoff
            self.show_error(f"Google is rate-limiting us; waiting {self.backoff}s")
            return
        self.backoff = 5
        log(f"selection detected={detected!r} -> {translated!r}")
        if not self.accept_detected(detected, text) or not translated:
            note = ("Already English — nothing to translate" if detected == "en"
                    else not_target_note(text, detected, self.lang))
            self.root.after(0, lambda: self._show("Highlighted text", text[:200], note, "hint"))
            return
        self.cache[text] = translated
        self.show_translation(text, translated, "Highlighted text")

    def process(self, sct, ocr, pos, vscreen):
        vx, vy, vw, vh = vscreen
        x, y = pos
        text = uia_text_under(x, y)
        self.read_via = "app" if text else "ocr"
        if not text:
            left = max(vx, min(x - CAPTURE_W // 2, vx + vw - CAPTURE_W))
            top = max(vy, min(y - CAPTURE_H // 2, vy + vh - CAPTURE_H))
            shot = sct.grab({"left": left, "top": top, "width": CAPTURE_W, "height": CAPTURE_H})
            lines = ocr.recognize(bytes(shot.raw), shot.width, shot.height)
            text = paragraph_under(lines, x - left, y - top)
        else:
            text = re.sub(r"\s+", " ", text).strip()
            lines = []
        if not text:
            log(f"no text under cursor; OCR saw {len(lines)} lines: "
                + " | ".join(ln[0] for ln in lines[:6]))
            self.set_idle("No text under cursor")
            return
        if text in self.cache:
            cached = self.cache[text]
            if cached is None:
                self.show_not_target(text)
            else:
                self.show_translation(text, cached)
            return
        if not maybe_target(text, self.lang) and not same_script(text, self.lang):
            log(f"skipped (looks non-{self.lang['name']}): {text}")
            self.cache[text] = None
            self.show_not_target(text)
            return
        if time.time() < self.backoff_until:
            secs = int(self.backoff_until - time.time()) + 1
            self.show_error(f"Google is rate-limiting us; waiting {secs}s")
            return
        self.root.after(0, lambda: self._show("Checking…", text, "", "ok"))
        try:
            L = self.lang
            if surely_target(text, L):
                translated, _ = google_translate(expand_abbreviations(text, L), L["code"])
                detected = L["code"]
            else:
                translated, detected = google_translate(text)
                if detected != "en" and self.accept_detected(detected, text):
                    # Google guessed another language; redo it as ours
                    translated, _ = google_translate(expand_abbreviations(text, L), L["code"])
                    detected = L["code"]
        except RateLimited:
            self.backoff = min(self.backoff * 2, 300)
            self.backoff_until = time.time() + self.backoff
            log(f"429 from Google; backing off {self.backoff}s")
            self.show_error(f"Google is rate-limiting us; waiting {self.backoff}s")
            return
        self.backoff = 5
        log(f"detected={detected!r} text={text!r} -> {translated!r}")
        if not self.accept_detected(detected, text) or not translated:
            self.cache[text] = None
            self.show_not_target(text, detected)
            return
        self.cache[text] = translated
        status = None
        if self.ocr_standin and self.read_via == "ocr" and detected != self.lang["code"]:
            status = f"{LANG_NAMES.get(detected, 'Cyrillic')} detected (read with stand-in OCR)"
        self.show_translation(text, translated, status)

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    App().run()
