"""
DISCLAIMER:
This tool captures rendered PDF pages from screen to reconstruct new
document(s) -- one file, or a batch of many files in one run.
It is intended ONLY for use with content you own or are otherwise legally
permitted to copy/reproduce (e.g. your own documents, or material explicitly
licensed for personal archival/accessibility use).

Copying, redistributing, or circumventing access/DRM protections on licensed,
copyrighted, or otherwise protected documents without the rights holder's
permission is STRICTLY PROHIBITED and may violate copyright law and/or
anti-circumvention provisions (e.g. DMCA, EUCD) in your jurisdiction.

The creator/author of this program does NOT intend, endorse, or support its
use for any such unauthorized copying, redistribution, or DRM circumvention.
By using this software, you acknowledge that YOU are solely and 100%
responsible/liable for how you use it and for ensuring your use complies
with all applicable laws, licenses, and terms of service. The author accepts
no liability whatsoever for misuse of this tool.

---------------------------------------------------------------------------
UNIFIED VERSION: replaces the separate single-file and batch scripts.
Drop ONE PDF for the classic single-file workflow (custom page ranges, an
exact output filename), or drop SEVERAL PDFs at once for unattended batch
processing (per-file failure isolation, a shared settings dialog, and a
final summary report). The same engine (focus handling, fail-safe recovery,
OCR, B&W DPI, duplicate-page trimming) backs both.

Still GUI automation, not a headless pipeline: each file is opened in your
actual reader, taken fullscreen, and captured in real time.
---------------------------------------------------------------------------
"""

import sys
import os
import io
import time
import tempfile
import subprocess
import pyautogui
import psutil
import ctypes
from ctypes import wintypes
from PIL import Image, ImageEnhance
from pypdf import PdfReader, PdfWriter
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

try:
    import pytesseract
    PYTESSERACT_AVAILABLE = True
except ImportError:
    PYTESSERACT_AVAILABLE = False

DEFAULT_TESSERACT_PATH = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

root = tk.Tk()
root.withdraw()

# ---------------------------------------------------------------------------
# Visual theme (cosmetic only -- does not touch variable names, validation,
# or capture logic anywhere below). Applied once; every dialog inherits it
# automatically since ttk styles are global.
# ---------------------------------------------------------------------------
APP_BG = "#f4f6f9"
APP_FG = "#1c1c1c"
ACCENT = "#2f6fed"
ACCENT_ACTIVE = "#1d54c4"
MUTED = "#5a6472"
WARN_RED = "#c0392b"


def setup_style(root):
    style = ttk.Style(root)
    # NOTE: "vista" is Windows' native ttk theme, but it renders buttons via
    # the OS theme engine -- which silently IGNORES custom style.map colors
    # (background/foreground) on standard ttk.Button widgets. "clam" is a
    # theme-engine-free (pure Tk drawing) ttk theme that fully respects our
    # custom colors, so it's tried FIRST now. Still looks clean/modern, just
    # not pixel-identical to native Windows chrome.
    for candidate in ("clam", "vista"):
        try:
            style.theme_use(candidate)
            break
        except tk.TclError:
            continue

    root.configure(bg=APP_BG)
    base_font = ("Segoe UI", 9)
    style.configure(".", background=APP_BG, foreground=APP_FG, font=base_font)
    style.configure("TFrame", background=APP_BG)
    style.configure("TLabel", background=APP_BG, foreground=APP_FG, font=base_font)
    style.configure("TCheckbutton", background=APP_BG, foreground=APP_FG, font=base_font)
    style.configure("TMenubutton", padding=4, font=base_font)
    style.configure("TEntry", padding=3)
    style.configure("TButton", padding=6, font=base_font)
    style.configure("TLabelframe", background=APP_BG, bordercolor="#c4cbd4")
    style.configure("TLabelframe.Label", background=APP_BG, foreground="#20407a",
                     font=("Segoe UI", 9, "bold"))
    style.configure("Header.TLabel", background=APP_BG, foreground="#15325c",
                     font=("Segoe UI", 12, "bold"))
    style.configure("SubHeader.TLabel", background=APP_BG, foreground=MUTED, font=base_font)
    style.configure("Warn.TLabel", background=APP_BG, foreground=WARN_RED, font=("Segoe UI", 8, "bold"))
    style.configure("Accent.TButton", padding=9, font=("Segoe UI", 10, "bold"))
    style.map(
        "Accent.TButton",
        background=[("!disabled", ACCENT), ("active", ACCENT_ACTIVE)],
        foreground=[("!disabled", "white")],
    )
    return style


setup_style(root)

READER_CONFIG = {
    "Adobe Acrobat":    {"fullscreen": ("ctrl", "l"),   "home": ("home",),        "jump": ("ctrl", "shift", "n")},
    "Foxit Reader":      {"fullscreen": ("f11",),        "home": ("ctrl", "home"), "jump": ("ctrl", "g")},
    "Bluebeam Revu":     {"fullscreen": ("ctrl", "f11"), "home": ("ctrl", "home"), "jump": ("ctrl", "g")},
    "PDF-XChange":       {"fullscreen": ("f11",),        "home": ("ctrl", "home"), "jump": ("ctrl", "g")},
}

TARGET_READER_EXE = ['acrobat.exe', 'acrord32.exe', 'foxitpdfreader.exe', 'revu.exe', 'pdfxcview.exe']

# Placeholder page count for any file whose real count could NOT be
# auto-detected (e.g. needs a real open-password) AND wasn't manually typed
# in the Page Count Review step. The duplicate-page detector then finds the
# true end on its own and the trailing repeats get trimmed before saving.
HIGH_CEILING_PAGES = 1500

engine_cfg = None  # set once the settings dialog closes; read by exit_fullscreen_safely

# ---------------------------------------------------------------------------
# Performance mode: controls the few FIXED one-shot sleeps that aren't
# already user-tunable (fullscreen-transition, page-jump-dialog-open,
# home-key-settle). On a slow PC these could fire too early. Four modes,
# chosen via the gear icon in either settings dialog:
#   "fast"    - original fixed sleeps, unchanged (default, zero behavior
#               change versus before this feature existed)
#   "slow"    - same fixed-sleep approach, every value scaled up safely
#   "adaptive"- polls the screen until it stops changing, like the existing
#               Adaptive Stability Wait, applied to these transitions too
#   "manual"  - user types their own three values directly
# ---------------------------------------------------------------------------
PERF = {
    "mode": "fast",
    "fullscreen_s": 2.0,
    "jump_s": 0.4,
    "home_s": 0.5,
}
SLOW_SCALE = 2.5


def _perf_base(key):
    return {"fullscreen_s": 2.0, "jump_s": 0.4, "home_s": 0.5}[key]


def wait_fullscreen_transition():
    _perf_wait("fullscreen_s")


def wait_jump_dialog():
    _perf_wait("jump_s")


def wait_home_settle():
    _perf_wait("home_s")


def _perf_wait(key):
    mode = PERF["mode"]
    if mode == "fast":
        time.sleep(_perf_base(key))
    elif mode == "slow":
        time.sleep(max(_perf_base(key) * SLOW_SCALE, _perf_base(key) + 0.5))
    elif mode == "manual":
        time.sleep(PERF[key])
    else:  # adaptive
        time.sleep(min(_perf_base(key), 0.3))  # tiny floor so the UI has started reacting
        prev_hash = None
        elapsed = 0.0
        max_extra = max(_perf_base(key) * 2, 1.5)
        while elapsed < max_extra:
            shot = pyautogui.screenshot()
            h = quick_hash(shot)
            if prev_hash is not None and h == prev_hash:
                return
            prev_hash = h
            time.sleep(0.05)
            elapsed += 0.05


def press_combo(keys):
    if len(keys) == 1:
        pyautogui.press(keys[0])
    else:
        pyautogui.hotkey(*keys)


class ToolTip:
    def __init__(self, widget, text):
        self.widget = widget
        self.text = text
        self.tip_window = None
        self.widget.bind("<Enter>", self.show_tip)
        self.widget.bind("<Leave>", self.hide_tip)

    def show_tip(self, event=None):
        if self.tip_window or not self.text:
            return
        top = self.widget.winfo_toplevel()
        top.update_idletasks()
        x = top.winfo_x() + top.winfo_width() + 10
        y = top.winfo_y()
        self.tip_window = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        tw.attributes('-topmost', True)
        label = tk.Label(tw, text=self.text, justify="left",
                         background="#fffceb", foreground="#2a2a2a",
                         relief="solid", borderwidth=1, highlightbackground="#d8c67a",
                         font=("Segoe UI", 9, "normal"), padx=10, pady=9)
        label.pack(ipadx=1)

    def hide_tip(self, event=None):
        tw = self.tip_window
        self.tip_window = None
        if tw:
            tw.destroy()


def is_fullscreen():
    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    win_w = rect.right - rect.left
    win_h = rect.bottom - rect.top
    screen_w, screen_h = pyautogui.size()
    return (win_w >= screen_w) and (win_h >= screen_h)


def find_reader_hwnd(proc_names):
    """Finds the top-level visible window handle belonging to one of the
    given process names, by walking all windows and matching owning process."""
    user32 = ctypes.windll.user32
    found_hwnd = [None]

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def _enum_proc(hwnd, lparam):
        if user32.IsWindowVisible(hwnd):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            try:
                p = psutil.Process(pid.value)
                if p.name().lower() in proc_names:
                    found_hwnd[0] = hwnd
                    return False
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                pass
        return True

    user32.EnumWindows(_enum_proc, 0)
    return found_hwnd[0]


def force_foreground(hwnd):
    """Plain SetForegroundWindow is blocked by Windows' foreground-lock
    restriction once ANY other window (a messagebox, Explorer, a topmost
    tkinter dialog, the folder-open after a previous batch file, etc.) has
    recently taken focus -- a background script generally cannot steal it
    back with a single API call, and the call fails silently. This is the
    standard bypass: temporarily attach our thread's input queue to the
    target window's thread, which lets SetForegroundWindow actually
    succeed, then detach again immediately."""
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    SW_RESTORE = 9

    target_pid = wintypes.DWORD()
    target_thread = user32.GetWindowThreadProcessId(hwnd, ctypes.byref(target_pid))
    current_thread = kernel32.GetCurrentThreadId()

    attached = False
    if target_thread and target_thread != current_thread:
        attached = bool(user32.AttachThreadInput(current_thread, target_thread, True))

    try:
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(current_thread, target_thread, False)

    return user32.GetForegroundWindow() == hwnd


def bring_reader_to_focus(proc_names, attempts=5, delay=0.3):
    """Ensures the PDF reader window is foreground before sending it
    fullscreen/navigation hotkeys. Returns True once confirmed focused,
    False if it couldn't be found/focused after several attempts."""
    for _ in range(attempts):
        hwnd = find_reader_hwnd(proc_names)
        if hwnd:
            if force_foreground(hwnd):
                return True
        time.sleep(delay)
    return False


def parse_page_ranges(range_str, max_pages):
    """Only used in single-file mode (a custom range across files of
    different lengths doesn't make sense, so batch mode always captures
    all pages)."""
    pages = set()
    parts = range_str.split(',')

    for part in parts:
        part = part.strip()
        if not part:
            continue
        if '-' in part:
            sub_parts = part.split('-', 1)
            if len(sub_parts) != 2:
                raise ValueError(f"Invalid range format: '{part}'")
            start_str, end_str = sub_parts
            try:
                start = int(start_str.strip())
                end = int(end_str.strip())
            except ValueError:
                raise ValueError(f"Non-numeric values found in range: '{part}'")
            if start > end:
                raise ValueError(f"Start page cannot be greater than end page: '{part}'")
            if start < 1 or end > max_pages:
                raise ValueError(f"Pages in range '{part}' must be between 1 and {max_pages}.")
            pages.update(range(start, end + 1))
        else:
            try:
                page = int(part)
            except ValueError:
                raise ValueError(f"Non-numeric page value found: '{part}'")
            if page < 1 or page > max_pages:
                raise ValueError(f"Page '{part}' must be between 1 and {max_pages}.")
            pages.add(page)

    if not pages:
        raise ValueError("No valid pages were provided. Please enter a range like '1-5, 8'.")
    return sorted(list(pages))


def quick_hash(img, size=(64, 64)):
    return img.convert("L").resize(size, Image.Resampling.BILINEAR).tobytes()


def trim_trailing_duplicate_pages(saved_paths, run_length):
    """Removes trailing near-identical pages that triggered the duplicate
    detector, keeping exactly ONE copy. run_length is the count of
    CONSECUTIVE repeats registered AFTER the first occurrence."""
    for _ in range(run_length):
        if len(saved_paths) <= 1:
            break
        p = saved_paths.pop()
        try:
            os.remove(p)
        except OSError:
            pass


def detect_page_count(path):
    """Best-effort real page count via pypdf. Returns (count, reason):
    count is None if the file can't be parsed at all, with reason a short
    human-readable explanation shown to the user instead of silently
    guessing."""
    try:
        rdr = PdfReader(path)
        if rdr.is_encrypted:
            ok = rdr.decrypt("")
            if not ok:
                return None, "Password-protected (owner/open password required)"
        n = len(rdr.pages)
        if n > 0:
            return n, None
        return None, "PDF reports 0 pages (malformed page tree)"
    except Exception as e:
        msg = str(e).strip()
        if "password" in msg.lower() or "decrypt" in msg.lower():
            return None, "Password-protected (owner/open password required)"
        if not msg:
            return None, "Corrupted or non-standard PDF structure"
        return None, f"Unreadable: {msg[:80]}"


def wait_for_stable_render(min_wait, max_extra_wait=2.0, poll_interval=0.05):
    """Waits min_wait seconds, then polls until two consecutive screenshots
    match (page finished rendering) or max_extra_wait elapses."""
    time.sleep(min_wait)
    prev_hash = None
    elapsed = 0.0
    while elapsed < max_extra_wait:
        shot = pyautogui.screenshot()
        h = quick_hash(shot)
        if prev_hash is not None and h == prev_hash:
            return
        prev_hash = h
        time.sleep(poll_interval)
        elapsed += poll_interval


def close_reader_processes(proc_names, graceful_timeout=4.0):
    """Closes running reader instances between batch files. A hard
    terminate() doesn't let Acrobat/Foxit flush their 'closed cleanly'
    state, which triggers an in-app 'wasn't closed properly, reopen?'
    recovery prompt on next launch -- that dialog sits ON TOP of fullscreen
    and instantly fails is_fullscreen(). So: graceful WM_CLOSE first, hard
    kill only as a last-resort fallback."""
    user32 = ctypes.windll.user32
    WM_CLOSE = 0x0010
    pids = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def _enum_proc(hwnd, lparam):
        if user32.IsWindowVisible(hwnd):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            try:
                p = psutil.Process(pid.value)
                if p.name().lower() in proc_names:
                    pids.append(pid.value)
                    user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                pass
        return True

    user32.EnumWindows(_enum_proc, 0)

    deadline = time.time() + graceful_timeout
    while time.time() < deadline:
        still_alive = [pid for pid in pids if psutil.pid_exists(pid)]
        if not still_alive:
            break
        time.sleep(0.2)

    for proc in psutil.process_iter(['name', 'pid']):
        try:
            if proc.info['name'].lower() in proc_names:
                psutil.Process(proc.info['pid']).terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass
    time.sleep(1.0)


def wait_for_reader_process(proc_names, timeout_loops=20):
    active_exe = None
    for _ in range(timeout_loops):
        for proc in psutil.process_iter(['name']):
            try:
                proc_name = proc.info['name'].lower()
                if proc_name in proc_names:
                    active_exe = proc_name
                    break
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                pass
        if active_exe:
            break
        time.sleep(1)
    return active_exe


def get_reader_label(active_exe):
    if active_exe:
        if "foxit" in active_exe:
            return "Foxit Reader"
        elif "revu" in active_exe:
            return "Bluebeam Revu"
        elif "pdfxcview" in active_exe:
            return "PDF-XChange"
    return "Adobe Acrobat"


def exit_fullscreen_safely(cursor_pos=None, attempts=4):
    """Forces the reader out of fullscreen reliably. A single Esc can get
    swallowed if the reader is mid-render, or if a popup/folder window has
    stolen focus (always the case right before the final success dialog) --
    so this re-focuses the reader via the AttachThreadInput bypass before
    each Esc attempt, and resends the reader's own fullscreen combo if Esc
    alone didn't undo it (e.g. Bluebeam's ctrl+f11).

    If cursor_pos is given, the cursor is moved there first via raw ctypes
    -- needed right after a fail-safe trigger, since the cursor sitting in
    the corner would make any pyautogui call re-raise FailSafeException
    immediately (pyautogui rechecks the corner on EVERY call, not just
    mouse moves)."""
    try:
        if cursor_pos is not None:
            ctypes.windll.user32.SetCursorPos(*cursor_pos)
            time.sleep(0.15)

        for _ in range(attempts):
            if not is_fullscreen():
                return True
            bring_reader_to_focus(TARGET_READER_EXE)
            time.sleep(0.15)
            pyautogui.press('esc')
            time.sleep(0.4)
            if is_fullscreen() and engine_cfg is not None:
                bring_reader_to_focus(TARGET_READER_EXE)
                press_combo(engine_cfg["fullscreen"])
                time.sleep(0.4)

        return not is_fullscreen()
    except Exception:
        return False


def open_output_folder(path):
    try:
        subprocess.run(['explorer', '/select,', os.path.normpath(path)])
    except Exception:
        pass


def ocr_page_to_pdf_bytes(pil_image, lang, ocr_config):
    return pytesseract.image_to_pdf_or_hocr(
        pil_image, extension='pdf', lang=lang, config=ocr_config, timeout=120
    )


# ---------------------------------------------------------------------------
# Multi-file drop reunification
# ---------------------------------------------------------------------------
# Windows launches ONE process per dropped file when multiple files are
# dragged onto a .bat/.exe that isn't registered as a multi-select drop
# target -- it does NOT bundle them into one argv list. So dropping several
# PDFs actually starts several independent copies of this script.
#
# The first process to start claims a named OS mutex and becomes the
# "leader"; every other near-simultaneous process ("follower") detects the
# mutex already exists, silently drops its file path(s) into a shared queue
# folder, and exits immediately with no dialog. The leader waits a short
# window for followers to register, then proceeds with the combined list --
# whether that list ends up being 1 file (classic single-file flow) or many
# (batch flow) is decided automatically from the final count.
QUEUE_DIR = os.path.join(tempfile.gettempdir(), "pdf_capture_tool_queue")
MUTEX_NAME = "Global\\PDFCaptureToolUnifiedMutex"
COLLECT_WINDOW = 1.0
COLLECT_MAX_EXTEND = 3.0
_kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
ERROR_ALREADY_EXISTS = 183
_MUTEX_HANDLE = None


def _is_leader():
    global _MUTEX_HANDLE
    _MUTEX_HANDLE = _kernel32.CreateMutexW(None, False, MUTEX_NAME)
    return ctypes.get_last_error() != ERROR_ALREADY_EXISTS


def _write_queue_entry(pdf_paths):
    os.makedirs(QUEUE_DIR, exist_ok=True)
    entry_path = os.path.join(QUEUE_DIR, f"{os.getpid()}_{int(time.time() * 1000)}.txt")
    try:
        with open(entry_path, "w", encoding="utf-8") as f:
            f.write("\n".join(pdf_paths))
    except Exception:
        pass


def _collect_queue():
    deadline = time.time() + COLLECT_WINDOW
    hard_deadline = time.time() + COLLECT_MAX_EXTEND
    last_count = -1
    while time.time() < deadline and time.time() < hard_deadline:
        try:
            current_count = len(os.listdir(QUEUE_DIR))
        except FileNotFoundError:
            current_count = 0
        if current_count != last_count:
            deadline = min(time.time() + COLLECT_WINDOW, hard_deadline)
            last_count = current_count
        time.sleep(0.15)

    collected = []
    try:
        for fname in sorted(os.listdir(QUEUE_DIR)):
            fpath = os.path.join(QUEUE_DIR, fname)
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    collected.extend(line.strip() for line in f if line.strip())
            except Exception:
                pass
            try:
                os.remove(fpath)
            except Exception:
                pass
    except FileNotFoundError:
        pass
    return collected


# ---------------------------------------------------------------------------
# Command-line input: one or more dropped PDFs
# ---------------------------------------------------------------------------
if len(sys.argv) < 2:
    messagebox.showerror("Error", "Please drag and drop one or more PDF files directly onto this script icon.")
    sys.exit()

all_args = sys.argv[1:]
own_pdfs = [p for p in all_args if p.lower().endswith('.pdf') and os.path.isfile(p)]
own_skipped = [p for p in all_args if p not in own_pdfs]

if _is_leader():
    _write_queue_entry(own_pdfs)
    sibling_paths = _collect_queue()
    seen = set()
    PDF_FILES = []
    for p in sibling_paths:
        if p not in seen and p.lower().endswith('.pdf') and os.path.isfile(p):
            seen.add(p)
            PDF_FILES.append(p)
    skipped = own_skipped
else:
    if own_pdfs:
        _write_queue_entry(own_pdfs)
    sys.exit()

if not PDF_FILES:
    messagebox.showerror("Error", "None of the dropped files were valid PDFs.")
    sys.exit()

if skipped:
    messagebox.showwarning(
        "Some Files Skipped",
        f"{len(skipped)} dropped file(s) were not valid PDFs and will be skipped:\n\n"
        + "\n".join(os.path.basename(p) for p in skipped)
    )

IS_SINGLE = (len(PDF_FILES) == 1)

DETECTED_PAGES = {p: detect_page_count(p) for p in PDF_FILES}  # path -> (count_or_None, reason_or_None)


class PerformanceDialog(tk.Toplevel):
    """Opened via the gear icon. Controls the few FIXED one-shot waits that
    aren't already exposed elsewhere (fullscreen-transition, page-jump-
    dialog-open, home-key-settle) -- these are the ones most at risk on a
    slow PC, since on a fast one they're already generous."""
    def __init__(self, parent):
        super().__init__(parent)
        self.title("Performance Settings")
        self.configure(bg=APP_BG)
        self.attributes('-topmost', True)
        self.grab_set()

        outer = ttk.Frame(self, padding=(16, 14, 16, 14))
        outer.pack(fill="both", expand=True)

        ttk.Label(outer, text="Performance Settings", style="Header.TLabel").pack(anchor="w")
        ttk.Label(outer, justify="left", style="SubHeader.TLabel", text=(
            "Controls 3 short, fixed waits around fullscreen-entry and page-jump\n"
            "navigation that aren't covered by 'Adaptive Stability Wait'. These rarely\n"
            "matter on a fast PC, but can be too short on a slow/loaded one."
        )).pack(anchor="w", pady=(4, 12))

        self.mode_var = tk.StringVar(self, value=PERF["mode"])
        modes = [
            ("fast", "Fast PC (default)", "Original fixed waits, unchanged. Fastest, recommended if things already work reliably."),
            ("slow", "Slow PC (safe)", f"Same fixed-wait approach, every value scaled up ~{SLOW_SCALE}x. Simple and predictable."),
            ("adaptive", "Adaptive (smart wait)", "Polls the screen and proceeds once it stops changing, like Adaptive Stability Wait, but applied to these transitions too."),
            ("manual", "Manual (advanced)", "Type your own exact wait times below."),
        ]
        box = ttk.LabelFrame(outer, text="Mode", padding=(10, 8))
        box.pack(fill="x", pady=(0, 10))
        for key, label, desc in modes:
            rb = ttk.Radiobutton(box, text=label, value=key, variable=self.mode_var, command=self._toggle_manual)
            rb.pack(anchor="w", pady=(2, 0))
            ttk.Label(box, text=desc, style="SubHeader.TLabel", wraplength=440,
                      justify="left").pack(anchor="w", padx=(22, 0), pady=(0, 4))

        self.manual_box = ttk.LabelFrame(outer, text="Manual Wait Times (seconds)", padding=(10, 8))
        self.manual_box.pack(fill="x", pady=(0, 10))
        self.e_fullscreen = self._manual_row(self.manual_box, 0, "Fullscreen Transition:", PERF["fullscreen_s"])
        self.e_jump = self._manual_row(self.manual_box, 1, "Page-Jump Dialog:", PERF["jump_s"])
        self.e_home = self._manual_row(self.manual_box, 2, "Home-Key Settle:", PERF["home_s"])

        ttk.Button(outer, text="Save", style="Accent.TButton", command=self.on_ok).pack(pady=(4, 0))
        self._toggle_manual()
        self.lift()
        self.focus_force()

    def _manual_row(self, parent, row, label, default):
        ttk.Label(parent, text=label).grid(row=row, column=0, padx=(0, 8), pady=3, sticky="e")
        e = ttk.Entry(parent, width=10)
        e.insert(0, str(default))
        e.grid(row=row, column=1, padx=(0, 8), pady=3, sticky="w")
        return e

    def _toggle_manual(self):
        state = "normal" if self.mode_var.get() == "manual" else "disabled"
        for e in (self.e_fullscreen, self.e_jump, self.e_home):
            e.config(state=state)

    def on_ok(self):
        mode = self.mode_var.get()
        if mode == "manual":
            try:
                fs = float(self.e_fullscreen.get())
                js = float(self.e_jump.get())
                hs = float(self.e_home.get())
                if min(fs, js, hs) < 0:
                    raise ValueError
            except ValueError:
                messagebox.showerror("Invalid Input", "Please enter valid non-negative numbers for all three waits.", parent=self)
                return
            PERF["fullscreen_s"], PERF["jump_s"], PERF["home_s"] = fs, js, hs
        PERF["mode"] = mode
        self.destroy()


def add_gear_button(header_row):
    """Small ⚙ button added to a dialog's header row, opening PerformanceDialog."""
    btn = ttk.Button(header_row, text="⚙ Performance", command=lambda: PerformanceDialog(header_row.winfo_toplevel()))
    btn.pack(side="right")
    ToolTip(btn, "Adjust fixed wait times for fullscreen-entry and page-jump\nnavigation -- useful if this PC is slower than average.")
    return btn


class PageReviewDialog(tk.Toplevel):
    """Shown once at startup, listing every queued file with its total page
    count -- auto-detected where possible and always editable, with the
    REASON shown when detection failed instead of silently guessing."""
    def __init__(self, parent, pdf_files, detected):
        super().__init__(parent)
        self.title("Confirm Page Count" if IS_SINGLE else "Confirm Page Counts Per File")
        self.configure(bg=APP_BG)
        self.attributes('-topmost', True)
        self.result = None
        self.entries = {}

        outer = ttk.Frame(self, padding=(16, 14, 16, 14))
        outer.pack(fill="both", expand=True)

        header_row = ttk.Frame(outer)
        header_row.pack(fill="x")
        ttk.Label(header_row, text="Confirm Page Count" if IS_SINGLE else "Confirm Page Counts Per File",
                  style="Header.TLabel").pack(side="left", anchor="w")
        add_gear_button(header_row)
        ttk.Label(outer, text="Correct any value below before starting.",
                  style="SubHeader.TLabel").pack(anchor="w", pady=(2, 10))

        note = ttk.LabelFrame(outer, text="How this works", padding=(10, 8))
        note.pack(fill="x", pady=(0, 10))
        ttk.Label(note, justify="left", style="SubHeader.TLabel", text=(
            "Auto-detected counts are pre-filled automatically. Items shown in RED could\n"
            f"NOT be auto-read (reason given) and default to a high placeholder\n"
            f"({HIGH_CEILING_PAGES}) -- the duplicate-page detector will then find the real end on\n"
            "its own and trim the repeated tail pages, OR type the real count yourself."
        )).pack(anchor="w")

        container = ttk.Frame(outer)
        container.pack(fill="both", expand=True)
        canvas = tk.Canvas(container, height=min(320, 60 * len(pdf_files) + 20), width=640,
                            bg="white", highlightthickness=1, highlightbackground="#c4cbd4")
        scrollbar = ttk.Scrollbar(container, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        for i, path in enumerate(pdf_files):
            fname = os.path.basename(path)
            count, reason = detected.get(path, (None, None))
            row_bg = "white" if i % 2 == 0 else "#f3f5f8"
            row = tk.Frame(inner, bg=row_bg)
            row.pack(fill="x")
            top_line = tk.Frame(row, bg=row_bg)
            top_line.pack(fill="x", padx=8, pady=(6, 1 if reason else 6))

            # Page count entry is packed FIRST on the right, with a fixed
            # width -- the filename label then fills/wraps into whatever
            # space remains instead of using a fixed character width that
            # overflows (and overlaps the entry) on long filenames.
            e = ttk.Entry(top_line, width=8)
            e.insert(0, str(count if count else HIGH_CEILING_PAGES))
            e.pack(side="right", padx=(5, 0))
            self.entries[path] = e

            label_text = fname if count else f"{fname}  (auto-detect failed)"
            label_fg = APP_FG if count else WARN_RED
            label_font = ("Segoe UI", 9) if count else ("Segoe UI", 9, "bold")
            tk.Label(top_line, text=label_text, fg=label_fg, bg=row_bg, anchor="w",
                      justify="left", font=label_font, wraplength=460).pack(side="left", fill="x", expand=True)

            if reason:
                tk.Label(row, text=f"Reason: {reason}", fg=WARN_RED, bg=row_bg, anchor="w",
                          justify="left", font=("Segoe UI", 8), wraplength=600).pack(fill="x", padx=(8, 0), pady=(0, 6))

        ttk.Button(outer, text="Continue", style="Accent.TButton", command=self.on_ok).pack(pady=(14, 0))
        self.bind('<Return>', lambda e: self.on_ok())
        self.lift()
        self.focus_force()

    def on_ok(self):
        result = {}
        for path, entry in self.entries.items():
            try:
                val = int(entry.get())
                if val < 1:
                    raise ValueError
            except ValueError:
                messagebox.showerror("Invalid Input", f"Enter a valid whole number of pages for:\n{os.path.basename(path)}", parent=self)
                return
            result[path] = val
        self.result = result
        self.destroy()


page_review = PageReviewDialog(root, PDF_FILES, DETECTED_PAGES)
root.wait_window(page_review)
if page_review.result is None:
    sys.exit()
MANUAL_PAGE_COUNTS = page_review.result

try:
    os.startfile(PDF_FILES[0])
except Exception as e:
    messagebox.showerror("Error", f"Error launching file:\n{e}")
    sys.exit()

active_exe = wait_for_reader_process(TARGET_READER_EXE)
time.sleep(2 if active_exe else 3)


class SettingsDialog(tk.Toplevel):
    def __init__(self, parent, file_count):
        super().__init__(parent)
        title = "Extraction Settings" if IS_SINGLE else f"Batch Extraction Settings -- {file_count} file(s)"
        self.title(title)
        self.configure(bg=APP_BG)
        self.attributes('-topmost', True)
        self.result = None

        def info_icon(master):
            lbl = tk.Label(master, text="ⓘ", fg=ACCENT, bg=APP_BG, cursor="hand2", font=("Segoe UI", 10, "bold"))
            return lbl

        outer = ttk.Frame(self, padding=(18, 14, 18, 14))
        outer.pack(fill="both", expand=True)

        header_row = ttk.Frame(outer)
        header_row.pack(fill="x")
        ttk.Label(header_row, text=title, style="Header.TLabel").pack(side="left", anchor="w")
        add_gear_button(header_row)
        if not IS_SINGLE:
            ttk.Label(outer, text=f"{file_count} PDF(s) queued -- all files share the settings below.",
                      style="SubHeader.TLabel").pack(anchor="w", pady=(2, 0))
        ttk.Separator(outer).pack(fill="x", pady=(10, 12))

        # ---- Section: Reader & Pages ----------------------------------
        sec_reader = ttk.LabelFrame(outer, text="Reader & Pages", padding=(12, 8))
        sec_reader.pack(fill="x", pady=(0, 10))

        ttk.Label(sec_reader, text="PDF Reader Engine:").grid(row=0, column=0, padx=(0, 8), pady=4, sticky="e")
        self.reader_var = tk.StringVar(self)
        self.reader_var.set(get_reader_label(active_exe))
        self.reader_menu = tk.OptionMenu(sec_reader, self.reader_var, *READER_CONFIG.keys())
        self.reader_menu.grid(row=0, column=1, columnspan=2, padx=(0, 8), pady=4, sticky="we")

        self.single_total = MANUAL_PAGE_COUNTS.get(PDF_FILES[0], HIGH_CEILING_PAGES) if IS_SINGLE else None

        if IS_SINGLE:
            ttk.Label(sec_reader, text="Total Document Pages:").grid(row=1, column=0, padx=(0, 8), pady=4, sticky="e")
            self.e_pages = ttk.Entry(sec_reader, width=10)
            self.e_pages.insert(0, str(self.single_total))
            self.e_pages.config(state="readonly")
            self.e_pages.grid(row=1, column=1, padx=(0, 8), pady=4, sticky="w")
            ToolTip(self.e_pages, "Set in the 'Confirm Page Count' step before this dialog.")

            ttk.Label(sec_reader, text="Pages to Capture (e.g. 1-5, 8):").grid(row=2, column=0, padx=(0, 8), pady=4, sticky="e")
            self.e_ranges = ttk.Entry(sec_reader, width=18)
            self.e_ranges.insert(0, f"1-{self.single_total}")
            self.e_ranges.grid(row=2, column=1, padx=(0, 8), pady=4, sticky="w")

            self.capture_all_var = tk.BooleanVar(value=True)
            self.cb_capture_all = ttk.Checkbutton(sec_reader, text="Capture All", variable=self.capture_all_var, command=self.toggle_capture_all)
            self.cb_capture_all.grid(row=2, column=2, padx=(0, 8), pady=4, sticky="w")
        else:
            self.capture_all_var = tk.BooleanVar(value=True)
            self.e_ranges = None

        # ---- Section: Capture Timing -----------------------------------
        sec_timing = ttk.LabelFrame(outer, text="Capture Timing", padding=(12, 8))
        sec_timing.pack(fill="x", pady=(0, 10))

        frame_time = ttk.Frame(sec_timing)
        frame_time.grid(row=0, column=0, padx=(0, 8), pady=4, sticky="e")
        ttk.Label(frame_time, text="Min. Time Between Pages (sec):").pack(side="left")
        lbl_info = info_icon(frame_time)
        lbl_info.pack(side="left", padx=(5, 0))
        ToolTip(lbl_info, (
            "If set too fast, the script may outpace your reader, causing:\n"
            "• Duplicate Pages • Blurry Text • Blank/Grey Boxes\n\n"
            "If 'Adaptive Stability Wait' is ON, this is a MINIMUM wait and\n"
            "the script polls afterward until rendering settles (safer, slower).\n"
            "If OFF, used as a fixed flat delay (fastest, if your setup is reliable)."
        ))
        self.e_time = ttk.Entry(sec_timing, width=10)
        self.e_time.insert(0, "0.3")
        self.e_time.grid(row=0, column=1, padx=(0, 8), pady=4, sticky="w")

        self.stability_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(sec_timing, text="Adaptive Stability Wait (slower, safer)",
                        variable=self.stability_var).grid(row=0, column=2, padx=(0, 8), pady=4, sticky="w")

        self.btn_benchmark = ttk.Button(sec_timing, text="Run Benchmark...", command=self.run_benchmark)
        self.btn_benchmark.grid(row=0, column=3, padx=(0, 0), pady=4, sticky="w")
        ToolTip(self.btn_benchmark, (
            "Briefly takes over the screen: switches the reader to fullscreen,\n"
            "turns a few pages, and measures real render timing on this PC.\n"
            "Fills the time field above with a safe starting estimate."
            + ("" if IS_SINGLE else "\n\nMeasured against the FIRST queued file; applied to all files.")
        ))

        frame_dup = ttk.Frame(sec_timing)
        frame_dup.grid(row=1, column=0, columnspan=4, padx=0, pady=(6, 4), sticky="w")
        dup_label = "On possible end-of-doc" + ("" if IS_SINGLE else " (unconfirmed-count files only)") + ":"
        ttk.Label(frame_dup, text=dup_label).pack(side="left")
        lbl_dup_info = info_icon(frame_dup)
        lbl_dup_info.pack(side="left", padx=(5, 0))
        ToolTip(lbl_dup_info, (
            "Only applies when the page count could NOT be auto-detected and was\n"
            "left at the high placeholder in the Page Count Review step (shown in\n"
            "red there). For any confirmed/typed page count, this check never runs\n"
            "-- so a document with genuinely repeated pages near the end (blank\n"
            "separators, boilerplate) won't have real pages wrongly trimmed.\n\n"
            "Triggered when 5 identical pages in a row are captured.\n\n"
            "'Stop automatically' stays fully unattended -- no popup, saves what\n"
            "was captured and moves on (recommended).\n\n"
            "'Ask me each time' pauses and waits for your answer.\n\n"
            "'Ignore, keep capturing' disables the check entirely for this run."
        ))
        self.dup_behavior_var = tk.StringVar(self)
        self.dup_behavior_var.set("Stop automatically (recommended)")
        tk.OptionMenu(frame_dup, self.dup_behavior_var, "Stop automatically (recommended)",
                      "Ask me each time", "Ignore, keep capturing").pack(side="left", padx=(5, 0))

        frame_delay = ttk.Frame(sec_timing)
        frame_delay.grid(row=2, column=0, padx=(0, 8), pady=(4, 0), sticky="e")
        ttk.Label(frame_delay, text="Initial Fullscreen Delay (sec):").pack(side="left")
        lbl_info_delay = info_icon(frame_delay)
        lbl_info_delay.pack(side="left", padx=(5, 0))
        ToolTip(lbl_info_delay, (
            "Allows time for your mouse cursor to auto-hide after going fullscreen.\n"
            "The script centers the mouse to avoid black borders; this delay\n"
            "ensures the bright white cursor vanishes before the first capture."
        ))
        self.e_init_delay = ttk.Entry(sec_timing, width=10)
        self.e_init_delay.insert(0, "3.0")
        self.e_init_delay.grid(row=2, column=1, padx=(0, 8), pady=(4, 0), sticky="w")

        ttk.Label(sec_timing, text="Delay Between Files (sec):").grid(row=2, column=2, padx=(0, 8), pady=(4, 0), sticky="e")
        self.e_file_delay = ttk.Entry(sec_timing, width=8)
        self.e_file_delay.insert(0, "3.0")
        self.e_file_delay.grid(row=2, column=3, padx=0, pady=(4, 0), sticky="w")
        if IS_SINGLE:
            self.e_file_delay.config(state="disabled")
        ToolTip(self.e_file_delay, "N/A for a single file." if IS_SINGLE else
                "Pause after closing/opening readers between files,\nletting the next PDF fully load before capture starts.")

        # ---- Section: Page & Image Output -------------------------------
        sec_image = ttk.LabelFrame(outer, text="Page & Image Output", padding=(12, 8))
        sec_image.pack(fill="x", pady=(0, 10))

        ttk.Label(sec_image, text="Margin Crop Value:").grid(row=0, column=0, padx=(0, 8), pady=4, sticky="e")
        self.e_margin = ttk.Entry(sec_image, width=10)
        self.e_margin.insert(0, "0")
        self.e_margin.grid(row=0, column=1, padx=(0, 8), pady=4, sticky="w")

        self.unit_var = tk.StringVar(self)
        self.unit_var.set("Millimeters (mm)")
        self.unit_var.trace_add("write", self.toggle_doc_height)
        self.unit_menu = tk.OptionMenu(sec_image, self.unit_var, "Pixels", "Millimeters (mm)", "Inches (in)")
        self.unit_menu.grid(row=0, column=2, padx=(0, 8), pady=4, sticky="w")

        ttk.Label(sec_image, text="Fallback Longest Edge (mm/in):\n(Used only if DRM blocks auto-sizing)",
                  justify="right").grid(row=1, column=0, padx=(0, 8), pady=4, sticky="e")
        self.e_doc_height = ttk.Entry(sec_image, width=10)
        self.e_doc_height.insert(0, "297")
        self.e_doc_height.grid(row=1, column=1, padx=(0, 8), pady=4, sticky="w")

        ttk.Label(sec_image, text="Color Mode:").grid(row=2, column=0, padx=(0, 8), pady=4, sticky="e")
        self.color_var = tk.StringVar(self)
        self.color_var.set("Color (RGB)")
        self.color_var.trace_add("write", self.toggle_quality_state)
        self.color_menu = tk.OptionMenu(sec_image, self.color_var, "Color (RGB)", "Grayscale", "Pure Black & White (Lossless)")
        self.color_menu.grid(row=2, column=1, columnspan=2, padx=(0, 8), pady=4, sticky="we")

        ttk.Label(sec_image, text="JPEG Quality % (1-100):").grid(row=3, column=0, padx=(0, 8), pady=4, sticky="e")
        self.e_quality = ttk.Entry(sec_image, width=10)
        self.e_quality.insert(0, "100")
        self.e_quality.grid(row=3, column=1, padx=(0, 8), pady=4, sticky="w")

        frame_bw_dpi = ttk.Frame(sec_image)
        frame_bw_dpi.grid(row=3, column=2, padx=(0, 8), pady=4, sticky="w")
        ttk.Label(frame_bw_dpi, text="B&W Output DPI:").pack(side="left")
        lbl_info_dpi = info_icon(frame_bw_dpi)
        lbl_info_dpi.pack(side="left", padx=(5, 0))
        ToolTip(lbl_info_dpi, (
            "Only applies to 'Pure Black & White (Lossless)' mode.\n\n"
            "The captured screenshot is upscaled to this DPI before\n"
            "thresholding, then embedded in the PDF at that resolution.\n"
            "Higher values (e.g. 1200+) give crisper text/line edges but\n"
            "produce larger files and take longer to process per page.\n"
            "600 is usually a good balance."
        ))
        self.e_bw_dpi = ttk.Entry(frame_bw_dpi, width=8)
        self.e_bw_dpi.insert(0, "600")
        self.e_bw_dpi.pack(side="left", padx=(5, 0))

        self.sharpen_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(sec_image, text="Apply Text Sharpening (Recommended)",
                        variable=self.sharpen_var).grid(row=4, column=0, columnspan=3, pady=(6, 0), sticky="w")

        # ---- Section: Output File ---------------------------------------
        sec_output = ttk.LabelFrame(outer, text="Output File", padding=(12, 8))
        sec_output.pack(fill="x", pady=(0, 10))

        if IS_SINGLE:
            base_name = os.path.basename(os.path.splitext(PDF_FILES[0])[0])
            ttk.Label(sec_output, text="Output Filename:").grid(row=0, column=0, padx=(0, 8), pady=4, sticky="e")
            self.e_name = ttk.Entry(sec_output, width=38)
            self.e_name.insert(0, f"{base_name}_unlocked.pdf")
            self.e_name.grid(row=0, column=1, columnspan=2, padx=(0, 8), pady=4, sticky="we")
        else:
            ttk.Label(sec_output, text="Output Filename Suffix:").grid(row=0, column=0, padx=(0, 8), pady=4, sticky="e")
            self.e_name = ttk.Entry(sec_output, width=16)
            self.e_name.insert(0, "_unlocked")
            self.e_name.grid(row=0, column=1, padx=(0, 8), pady=4, sticky="w")
            ToolTip(self.e_name, "Each output file is named:\n<original filename><suffix>.pdf")

            self.close_reader_var = tk.BooleanVar(value=True)
            ttk.Checkbutton(sec_output, text="Close reader between files (recommended)",
                            variable=self.close_reader_var).grid(row=0, column=2, padx=(0, 8), pady=4, sticky="w")

        self.save_orig_var = tk.BooleanVar(value=True)
        save_text = "Save in original folder" if IS_SINGLE else "Save each file in its own original folder"
        ttk.Checkbutton(sec_output, text=save_text, variable=self.save_orig_var,
                        command=self.toggle_save_dir).grid(row=1, column=0, columnspan=3, pady=(6, 2), sticky="w")

        self.lbl_custom_dir = ttk.Label(sec_output, text="Custom Save Folder:", state="disabled")
        self.lbl_custom_dir.grid(row=2, column=0, padx=(0, 8), pady=4, sticky="e")
        self.dir_frame = ttk.Frame(sec_output)
        self.dir_frame.grid(row=2, column=1, columnspan=2, padx=(0, 8), pady=4, sticky="we")
        self.e_out_dir = ttk.Entry(self.dir_frame, state="disabled")
        self.e_out_dir.insert(0, os.path.dirname(PDF_FILES[0]))
        self.e_out_dir.pack(side="left", fill="x", expand=True)
        self.btn_browse = ttk.Button(self.dir_frame, text="Browse...", state="disabled", command=self.browse_dir)
        self.btn_browse.pack(side="left", padx=(5, 0))
        sec_output.columnconfigure(1, weight=1)

        # ---- Section: OCR -------------------------------------------------
        ocr_frame = ttk.LabelFrame(outer, text="Make Searchable (OCR)", padding=(12, 8))
        ocr_frame.pack(fill="x", pady=(0, 10))
        top_row = ttk.Frame(ocr_frame)
        top_row.pack(fill="x", pady=(0, 4))
        self.ocr_var = tk.BooleanVar(value=False)
        self.cb_ocr = ttk.Checkbutton(top_row, text="Make Searchable (OCR, offline via Tesseract)",
                                       variable=self.ocr_var, command=self.toggle_ocr_state)
        self.cb_ocr.pack(side="left")
        lbl_info_ocr = info_icon(top_row)
        lbl_info_ocr.pack(side="left", padx=(5, 0))
        ToolTip(lbl_info_ocr, (
            "Adds an invisible, selectable/searchable text layer behind each\n"
            "page image. Runs fully offline using the free Tesseract OCR engine.\n\n"
            "OCR is pattern recognition, not a verbatim transcript: 'Accurate'\n"
            "mode is typically 95-99%+ correct on clean typed text but can still\n"
            "misread occasional characters. Always spot-check anything that matters.\n\n"
            "Requires Tesseract OCR installed separately (not bundled)."
        ))

        opts_row = ttk.Frame(ocr_frame)
        opts_row.pack(fill="x", pady=(2, 2))
        ttk.Label(opts_row, text="Mode:").pack(side="left")
        self.ocr_mode_var = tk.StringVar(self)
        self.ocr_mode_var.set("Accurate (exact, slower)")
        self.ocr_mode_menu = tk.OptionMenu(opts_row, self.ocr_mode_var, "Accurate (exact, slower)", "Fast (approximate, quicker)")
        self.ocr_mode_menu.pack(side="left", padx=(5, 15))
        ttk.Label(opts_row, text="Language:").pack(side="left")
        self.e_ocr_lang = ttk.Entry(opts_row, width=8)
        self.e_ocr_lang.insert(0, "eng")
        self.e_ocr_lang.pack(side="left", padx=(5, 0))
        lbl_info_lang = info_icon(opts_row)
        lbl_info_lang.pack(side="left", padx=(5, 0))
        ToolTip(lbl_info_lang, "Tesseract language code (e.g. eng, fra, deu, ell).\nThe matching language pack must be installed.\nCombine with '+' for multiple, e.g. eng+fra.")

        path_row = ttk.Frame(ocr_frame)
        path_row.pack(fill="x", pady=(2, 0))
        ttk.Label(path_row, text="Tesseract Path:").pack(side="left")
        self.e_tesseract_path = ttk.Entry(path_row)
        self.e_tesseract_path.insert(0, DEFAULT_TESSERACT_PATH)
        self.e_tesseract_path.pack(side="left", fill="x", expand=True, padx=(5, 5))
        ttk.Button(path_row, text="Browse...", command=self.browse_tesseract).pack(side="left")

        self.ocr_widgets = [self.ocr_mode_menu, self.e_ocr_lang, self.e_tesseract_path]
        if not PYTESSERACT_AVAILABLE:
            self.ocr_var.set(False)
            self.cb_ocr.config(state="disabled", text="Make Searchable (OCR) -- requires 'pip install pytesseract'")

        # ---- Footer ---------------------------------------------------
        abort_text = "⚠️ Emergency Abort: Slam mouse pointer into ANY screen corner."
        if not IS_SINGLE:
            abort_text += "\n(you'll then be asked: skip this file, or abort the whole batch)"
        ttk.Label(outer, text=abort_text, style="Warn.TLabel", justify="center").pack(pady=(2, 10))

        start_text = "Start Extraction" if IS_SINGLE else "Start Batch"
        ttk.Button(outer, text=start_text, style="Accent.TButton", command=self.on_ok).pack(pady=(0, 2))

        self.bind('<Return>', lambda e: self.on_ok())
        if IS_SINGLE:
            self.toggle_capture_all()
        self.toggle_doc_height()
        self.toggle_quality_state()
        self.toggle_ocr_state()
        self.lift()
        self.focus_force()

    def toggle_capture_all(self):
        if self.e_ranges is None:
            return
        self.e_ranges.config(state="disabled" if self.capture_all_var.get() else "normal")

    def toggle_doc_height(self, *args):
        self.e_doc_height.config(state="disabled" if self.unit_var.get() == "Pixels" else "normal")

    def toggle_quality_state(self, *args):
        if self.color_var.get() == "Pure Black & White (Lossless)":
            self.e_quality.config(state="disabled")
            self.e_bw_dpi.config(state="normal")
        else:
            self.e_quality.config(state="normal")
            self.e_bw_dpi.config(state="disabled")

    def toggle_save_dir(self):
        state = "disabled" if self.save_orig_var.get() else "normal"
        self.lbl_custom_dir.config(state=state)
        self.e_out_dir.config(state=state)
        self.btn_browse.config(state=state)

    def browse_dir(self):
        selected_dir = filedialog.askdirectory(parent=self, initialdir=self.e_out_dir.get(), title="Select Output Folder")
        if selected_dir:
            self.e_out_dir.config(state="normal")
            self.e_out_dir.delete(0, tk.END)
            self.e_out_dir.insert(0, selected_dir)
            if self.save_orig_var.get():
                self.e_out_dir.config(state="disabled")

    def toggle_ocr_state(self):
        state = "normal" if (self.ocr_var.get() and PYTESSERACT_AVAILABLE) else "disabled"
        for w in self.ocr_widgets:
            w.config(state=state)

    def browse_tesseract(self):
        selected = filedialog.askopenfilename(parent=self, title="Locate tesseract.exe",
                                                filetypes=[("Tesseract executable", "tesseract.exe"), ("All files", "*.*")])
        if selected:
            self.e_tesseract_path.delete(0, tk.END)
            self.e_tesseract_path.insert(0, selected)

    def run_benchmark(self):
        if not messagebox.askyesno(
            "Run Benchmark?",
            "This will briefly switch the PDF reader to fullscreen and turn a\n"
            "few pages to measure real render timing on this PC.\n\n"
            "Make sure the reader is open and the document is at the start.\n\nProceed?",
            parent=self
        ):
            return

        reader_engine = self.reader_var.get()
        cfg = READER_CONFIG[reader_engine]
        n_samples = 5
        self.withdraw()
        try:
            time.sleep(0.3)
            if not bring_reader_to_focus(TARGET_READER_EXE):
                messagebox.showerror("Reader Not Focused", "Could not bring the PDF reader window to focus.", parent=self)
                return
            press_combo(cfg["fullscreen"])
            wait_fullscreen_transition()
            press_combo(cfg["home"])
            wait_home_settle()
            sw, sh = pyautogui.size()
            pyautogui.moveTo(sw // 2, sh // 2)
            time.sleep(1.5)

            page_times = []
            try:
                for _ in range(n_samples):
                    pyautogui.press('pagedown')
                    start = time.time()
                    prev_hash = None
                    settled = False
                    while time.time() - start < 4.0:
                        shot = pyautogui.screenshot()
                        h = quick_hash(shot)
                        if prev_hash is not None and h == prev_hash:
                            settled = True
                            break
                        prev_hash = h
                        time.sleep(0.05)
                    elapsed = time.time() - start
                    page_times.append(elapsed if settled else 4.0)
            except pyautogui.FailSafeException:
                messagebox.showwarning("Benchmark Aborted", "Fail-safe triggered -- benchmark cancelled.", parent=self)
                return
            finally:
                try:
                    pyautogui.press('esc')
                    time.sleep(0.3)
                    if is_fullscreen():
                        press_combo(cfg["fullscreen"])
                except Exception:
                    pass
        finally:
            self.deiconify()
            self.lift()
            self.focus_force()

        if not page_times:
            messagebox.showerror("Benchmark Failed", "No page timings were captured.", parent=self)
            return

        slowest = max(page_times)
        suggested = round(slowest + 0.05, 2)
        self.e_time.delete(0, tk.END)
        self.e_time.insert(0, str(suggested))
        messagebox.showinfo(
            "Benchmark Complete",
            f"Measured {len(page_times)} page turns.\n\nSlowest page took: {slowest:.2f}s\n"
            f"Suggested 'Time Between Pages': {suggested}s (filled in above)\n\n"
            "This is a starting point, not a guarantee -- pages with heavier\n"
            "content later in the document may still take longer. 'Adaptive\n"
            "Stability Wait' remains the safest option for variable documents.",
            parent=self
        )

    def on_ok(self):
        try:
            tbp = float(self.e_time.get())
            init_delay = float(self.e_init_delay.get())
            file_delay = 0.0 if IS_SINGLE else float(self.e_file_delay.get())
            margin_val = float(self.e_margin.get())
            unit = self.unit_var.get()
            doc_height = float(self.e_doc_height.get()) if unit != "Pixels" else 0.0
            color_mode = self.color_var.get()
            sharpen_img = self.sharpen_var.get()
            reader_engine = self.reader_var.get()
            if color_mode == "Pure Black & White (Lossless)":
                quality_pct = 100
                bw_dpi = int(self.e_bw_dpi.get())
            else:
                quality_pct = int(self.e_quality.get())
                bw_dpi = 600
        except ValueError:
            messagebox.showerror("Invalid Input", "Please enter valid numbers in all active fields.", parent=self)
            return

        if color_mode != "Pure Black & White (Lossless)" and not (1 <= quality_pct <= 100):
            messagebox.showerror("Invalid Input", "Image Quality must be between 1 and 100.", parent=self)
            return
        if color_mode == "Pure Black & White (Lossless)":
            if bw_dpi < 72:
                messagebox.showerror("Invalid Input", "B&W Output DPI must be at least 72.", parent=self)
                return
            if bw_dpi > 2400 and not messagebox.askyesno(
                "Very High DPI", f"{bw_dpi} DPI will produce very large files and slow per-page processing.\n\nProceed anyway?", parent=self
            ):
                return

        name_val = self.e_name.get().strip()
        if not name_val:
            messagebox.showerror("Invalid Input", "Please enter an output filename" + ("" if IS_SINGLE else " suffix") + ".", parent=self)
            return
        if IS_SINGLE and not name_val.lower().endswith(".pdf"):
            name_val += ".pdf"

        if self.save_orig_var.get():
            out_dir_mode = "original"
            shared_out_dir = None
        else:
            out_dir_mode = "shared"
            shared_out_dir = self.e_out_dir.get().strip()
            if not os.path.isdir(shared_out_dir):
                messagebox.showerror("Invalid Input", "The selected output folder does not exist.", parent=self)
                return

        target_pages_single = None
        if IS_SINGLE:
            try:
                if self.capture_all_var.get():
                    target_pages_single = list(range(1, self.single_total + 1))
                else:
                    target_pages_single = parse_page_ranges(self.e_ranges.get(), self.single_total)
            except ValueError as e:
                messagebox.showerror("Invalid Range Format", str(e), parent=self)
                return

        if tbp < 0.15:
            if not messagebox.askyesno("Warning: Too Fast", f"{tbp} seconds is extremely fast. Your reader may not render pages fast enough.\n\nAre you sure you want to proceed?", parent=self):
                return
        elif tbp > 1.5:
            if not messagebox.askyesno("Hint: Needlessly Slow", f"{tbp} seconds per page will take a very long time to finish.\n\nAre you sure you want to proceed with this speed?", parent=self):
                return

        if unit in ["Millimeters (mm)", "Inches (in)"] and doc_height <= 0:
            messagebox.showerror("Invalid Input", "Fallback Document Height must be greater than 0 to scale correctly.", parent=self)
            return

        do_ocr = self.ocr_var.get() and PYTESSERACT_AVAILABLE
        ocr_lang = self.e_ocr_lang.get().strip() or "eng"
        ocr_fast = (self.ocr_mode_var.get() == "Fast (approximate, quicker)")
        tesseract_path = self.e_tesseract_path.get().strip()
        if do_ocr and (not tesseract_path or not os.path.isfile(tesseract_path)):
            messagebox.showerror("Invalid Input", "OCR is enabled but the Tesseract path does not point to a valid file.\n\nInstall Tesseract OCR (offline, free) or browse to its tesseract.exe.", parent=self)
            return

        self.result = dict(
            tbp=tbp, init_delay=init_delay, file_delay=file_delay, margin_val=margin_val, unit=unit,
            doc_height=doc_height, color_mode=color_mode, sharpen_img=sharpen_img, quality=quality_pct,
            reader_engine=reader_engine, stability=self.stability_var.get(), bw_dpi=bw_dpi,
            name_val=name_val, out_dir_mode=out_dir_mode, shared_out_dir=shared_out_dir,
            close_reader_between_files=(False if IS_SINGLE else self.close_reader_var.get()),
            do_ocr=do_ocr, ocr_lang=ocr_lang, ocr_fast=ocr_fast, tesseract_path=tesseract_path,
            dup_behavior=self.dup_behavior_var.get(),
            target_pages_single=target_pages_single,
        )
        self.destroy()


dialog = SettingsDialog(root, len(PDF_FILES))
root.wait_window(dialog)

if dialog.result is None:
    sys.exit()

S = dialog.result

if S["do_ocr"]:
    pytesseract.pytesseract.tesseract_cmd = S["tesseract_path"]

SCREEN_CAPTURE_DPI = 96.0
BW_UPSCALE_FACTOR = max(1.0, S["bw_dpi"] / SCREEN_CAPTURE_DPI)


def page_wait(min_wait, use_stability, max_extra_wait=2.0):
    if use_stability:
        wait_for_stable_render(min_wait, max_extra_wait=max_extra_wait)
    else:
        time.sleep(min_wait)


def capture_single_pdf(pdf_path, target_pages, using_high_ceiling):
    """Runs the full capture+save pipeline for ONE pdf. Returns a dict with
    success flag, message, pages captured, and ocr failure count. Lets
    pyautogui.FailSafeException propagate up so the caller can decide
    abort vs skip-file; all other errors are caught and reported."""
    original_dir = os.path.dirname(pdf_path)
    base_name = os.path.basename(os.path.splitext(pdf_path)[0])
    out_name = S["name_val"] if IS_SINGLE else f"{base_name}{S['name_val']}.pdf"
    out_dir = original_dir if S["out_dir_mode"] == "original" else S["shared_out_dir"]
    output_path = os.path.join(out_dir, out_name)

    reader = None
    try:
        reader = PdfReader(pdf_path)
        if reader.is_encrypted:
            reader.decrypt("")
    except Exception:
        reader = None

    if not bring_reader_to_focus(TARGET_READER_EXE):
        return dict(success=False, message="Could not bring the PDF reader window to focus.", pages=0, ocr_failures=0)

    press_combo(engine_cfg["fullscreen"])
    wait_fullscreen_transition()
    press_combo(engine_cfg["home"])
    wait_home_settle()
    screen_w, screen_h = pyautogui.size()
    pyautogui.moveTo(screen_w // 2, screen_h // 2)
    time.sleep(S["init_delay"])

    current_page = 1
    consecutive_duplicates = 0
    last_image_hash = None
    ask_again_on_duplicates = True

    temp_dir = tempfile.mkdtemp(prefix="pdf_capture_")
    saved_paths = []

    def cleanup():
        for p in saved_paths:
            try:
                os.remove(p)
            except OSError:
                pass
        try:
            os.rmdir(temp_dir)
        except OSError:
            pass

    try:
        for target_page in target_pages:
            if not is_fullscreen():
                exit_fullscreen_safely()
                cleanup()
                return dict(success=False, message=f"Reader lost fullscreen focus at page {target_page}.", pages=0, ocr_failures=0)

            gap = target_page - current_page
            if gap == 0:
                pass
            elif 1 <= gap <= 3:
                for _ in range(gap):
                    pyautogui.press('pagedown')
                    time.sleep(0.1)
                page_wait(S["tbp"], S["stability"])
            else:
                press_combo(engine_cfg["jump"])
                wait_jump_dialog()
                pyautogui.write(str(target_page))
                pyautogui.press('enter')
                page_wait(S["tbp"] + 0.5, S["stability"])

            current_page = target_page
            screenshot = pyautogui.screenshot()
            if screenshot.mode != 'RGB':
                screenshot = screenshot.convert('RGB')

            gray_mask = screenshot.convert("L")
            bw_mask = gray_mask.point(lambda p: 0 if p < 15 else 255)
            raw_box = bw_mask.getbbox()

            if raw_box is None:
                exit_fullscreen_safely()
                if using_high_ceiling and saved_paths:
                    break
                cleanup()
                return dict(success=False, message=f"Pure black screen at target page {target_page} -- 'Total Pages' likely wrong.", pages=0, ocr_failures=0)

            if S["unit"] in ["Millimeters (mm)", "Inches (in)"]:
                page_pixel_width = raw_box[2] - raw_box[0]
                page_pixel_height = raw_box[3] - raw_box[1]
                longest_edge_pixels = max(page_pixel_width, page_pixel_height)
                actual_doc_length = None
                if reader:
                    try:
                        pdf_page = reader.pages[target_page - 1]
                        width_pts = float(pdf_page.mediabox.width)
                        height_pts = float(pdf_page.mediabox.height)
                        longest_edge_pts = max(width_pts, height_pts)
                        actual_doc_length = (longest_edge_pts * (25.4 / 72.0)) if S["unit"] == "Millimeters (mm)" else (longest_edge_pts / 72.0)
                    except Exception:
                        pass
                if not actual_doc_length or actual_doc_length <= 0:
                    actual_doc_length = S["doc_height"]
                current_margin_pixels = int((S["margin_val"] / actual_doc_length) * longest_edge_pixels)
            else:
                current_margin_pixels = int(S["margin_val"])

            crop_box = (
                raw_box[0] + current_margin_pixels, raw_box[1] + current_margin_pixels,
                raw_box[2] - current_margin_pixels, raw_box[3] - current_margin_pixels
            )
            cropped = screenshot.crop(crop_box)

            current_image_hash = quick_hash(cropped)
            if using_high_ceiling and last_image_hash is not None and current_image_hash == last_image_hash:
                consecutive_duplicates += 1
                if consecutive_duplicates >= 5:
                    behavior = S["dup_behavior"]
                    if behavior == "Ignore, keep capturing":
                        pass
                    elif behavior == "Stop automatically (recommended)":
                        exit_fullscreen_safely()
                        trim_trailing_duplicate_pages(saved_paths, consecutive_duplicates - 1)
                        break
                    elif behavior == "Ask me each time" and ask_again_on_duplicates:
                        root.attributes('-topmost', True)
                        keep_going = messagebox.askyesno(
                            "Possible End of Document",
                            f"[{base_name}] 5 identical pages in a row around page {target_page}.\n\n"
                            "YES = keep capturing anyway. NO = stop here, trim the repeated\n"
                            "pages down to one copy, and save."
                        )
                        if not keep_going:
                            exit_fullscreen_safely()
                            trim_trailing_duplicate_pages(saved_paths, consecutive_duplicates - 1)
                            break
                        else:
                            ask_again_on_duplicates = False
                            consecutive_duplicates = 0
            else:
                consecutive_duplicates = 0
            last_image_hash = current_image_hash

            if S["color_mode"] == "Pure Black & White (Lossless)":
                new_size = (round(cropped.width * BW_UPSCALE_FACTOR), round(cropped.height * BW_UPSCALE_FACTOR))
                cropped = cropped.resize(new_size, Image.Resampling.BICUBIC)
                temp_gray = cropped.convert("L")
                cropped = temp_gray.point(lambda p: 255 if p >= 200 else 0).convert('1')
            else:
                if S["sharpen_img"]:
                    cropped = ImageEnhance.Sharpness(cropped).enhance(2.0)
                if S["color_mode"] == "Grayscale":
                    cropped = cropped.convert("L")

            page_path = os.path.join(temp_dir, f"page_{len(saved_paths):05d}.bmp")
            cropped.save(page_path)
            saved_paths.append(page_path)
            cropped.close()

        exit_fullscreen_safely()

    except pyautogui.FailSafeException:
        exit_fullscreen_safely(cursor_pos=(screen_w // 2, screen_h // 2))
        cleanup()
        raise

    if not saved_paths:
        cleanup()
        return dict(success=False, message="No pages were captured.", pages=0, ocr_failures=0)

    try:
        page_dpi = float(S["bw_dpi"]) if S["color_mode"] == "Pure Black & White (Lossless)" else 300.0
        save_kwargs = {"save_all": True, "resolution": page_dpi}

        def lazy_open_images(paths):
            for p in paths:
                with Image.open(p) as im:
                    yield im.copy()

        if len(saved_paths) > 1:
            save_kwargs["append_images"] = lazy_open_images(saved_paths[1:])
        if S["color_mode"] != "Pure Black & White (Lossless)":
            save_kwargs["quality"] = S["quality"]

        first_img = Image.open(saved_paths[0])
        first_img.save(output_path, **save_kwargs)
        first_img.close()

        ocr_failures = 0
        if S["do_ocr"]:
            ocr_config = "--oem 0" if S["ocr_fast"] else "--oem 1"
            if IS_SINGLE:
                root.attributes('-topmost', True)
                ocr_progress = tk.Toplevel(root)
                ocr_progress.title("OCR in progress")
                ocr_progress.attributes('-topmost', True)
                tk.Label(ocr_progress, text="Running offline OCR, please wait...", padx=20, pady=20).pack()
                ocr_progress.update()

            writer = PdfWriter()
            for p in saved_paths:
                with Image.open(p) as im:
                    rgb_im = im.convert("RGB") if im.mode != "RGB" else im
                    try:
                        page_pdf_bytes = ocr_page_to_pdf_bytes(rgb_im, S["ocr_lang"], ocr_config)
                        page_reader = PdfReader(io.BytesIO(page_pdf_bytes))
                        writer.add_page(page_reader.pages[0])
                    except Exception:
                        ocr_failures += 1
                        fallback_buf = io.BytesIO()
                        rgb_im.save(fallback_buf, format="PDF", resolution=page_dpi)
                        fallback_reader = PdfReader(io.BytesIO(fallback_buf.getvalue()))
                        writer.add_page(fallback_reader.pages[0])
            with open(output_path, "wb") as f:
                writer.write(f)
            if IS_SINGLE:
                ocr_progress.destroy()

        # Belt-and-braces: confirm the reader is actually out of fullscreen
        # before returning/showing results, in case the end-of-loop call
        # didn't fully land (e.g. reader still finishing a render then).
        exit_fullscreen_safely()

        return dict(success=True, message=output_path, pages=len(saved_paths), ocr_failures=ocr_failures)

    except Exception as e:
        return dict(success=False, message=f"Captured pages but failed to save: {e}", pages=len(saved_paths), ocr_failures=0)
    finally:
        cleanup()


# ---------------------------------------------------------------------------
# Main loop (runs once for a single file, or sequentially for a batch)
# ---------------------------------------------------------------------------
engine_cfg = READER_CONFIG[S["reader_engine"]]
results = []
last_success_path = None

time.sleep(1.5)

try:
    for idx, pdf_path in enumerate(PDF_FILES):
        fname = os.path.basename(pdf_path)
        try:
            if idx > 0:
                if S["close_reader_between_files"]:
                    close_reader_processes(TARGET_READER_EXE)
                os.startfile(pdf_path)
                wait_for_reader_process(TARGET_READER_EXE)
                time.sleep(S["file_delay"])

            root.attributes('-topmost', False)

            total_pages = MANUAL_PAGE_COUNTS.get(pdf_path, HIGH_CEILING_PAGES)
            using_high_ceiling = (DETECTED_PAGES.get(pdf_path, (None, None))[0] is None and total_pages == HIGH_CEILING_PAGES)
            if IS_SINGLE and S["target_pages_single"] is not None:
                target_pages = S["target_pages_single"]
            else:
                target_pages = list(range(1, total_pages + 1))

            outcome = capture_single_pdf(pdf_path, target_pages, using_high_ceiling)
            results.append((fname, outcome["success"], outcome["message"], outcome["pages"], outcome["ocr_failures"]))
            if outcome["success"]:
                last_success_path = outcome["message"]

        except pyautogui.FailSafeException:
            root.attributes('-topmost', True)
            if IS_SINGLE:
                saved_hint = results[-1][3] if results else 0
                if messagebox.askyesno(
                    "Capture Aborted",
                    "Emergency abort triggered.\n\nThe capture process was stopped.\n\n"
                    "(Nothing was saved for this run since it was interrupted mid-capture.)"
                ):
                    pass
                results.append((fname, False, "Aborted by user (fail-safe)", 0, 0))
            else:
                choice = messagebox.askyesnocancel(
                    "Emergency Abort Triggered",
                    f"Fail-safe triggered while processing '{fname}'.\n\n"
                    "YES = skip this file and continue with the next one.\n"
                    "NO = abort the ENTIRE remaining batch now.\n"
                    "CANCEL = abort the entire remaining batch now."
                )
                if choice is True:
                    results.append((fname, False, "Skipped (fail-safe triggered by user)", 0, 0))
                    continue
                else:
                    results.append((fname, False, "Batch aborted by user (fail-safe)", 0, 0))
                    break

        except Exception as e:
            results.append((fname, False, f"Unexpected error: {e}", 0, 0))
            continue

finally:
    if S["close_reader_between_files"]:
        close_reader_processes(TARGET_READER_EXE)

# ---------------------------------------------------------------------------
# Final summary / result dialog
# ---------------------------------------------------------------------------
root.attributes('-topmost', True)
succeeded = [r for r in results if r[1]]
failed = [r for r in results if not r[1]]

if IS_SINGLE:
    fname, success, message, pages, ocr_failures = results[0]
    if success:
        summary = f"Successfully captured {pages} pages.\n\nSaved to:\n{message}\n\n"
        if S["do_ocr"]:
            summary += f"Searchable text layer added (offline OCR, '{S['ocr_lang']}').\n"
            if ocr_failures:
                summary += f"Note: {ocr_failures} page(s) could not be OCR'd and were saved as image-only.\n"
            summary += "\n"
        summary += "The containing folder will now open."
        messagebox.showinfo("Operation Successful", summary)
    else:
        messagebox.showerror("Capture Failed", message)
else:
    lines = [f"Batch complete: {len(succeeded)}/{len(results)} file(s) succeeded.\n"]
    for fname, success, message, pages, ocr_failures in results:
        if success:
            line = f"✔ {fname} -- {pages} page(s)"
            if ocr_failures:
                line += f" ({ocr_failures} page(s) failed OCR, saved as image-only)"
            lines.append(line)
        else:
            lines.append(f"✘ {fname} -- {message}")
    messagebox.showinfo("Batch Operation Summary", "\n".join(lines))

root.attributes('-topmost', False)

if last_success_path:
    open_output_folder(last_success_path)
