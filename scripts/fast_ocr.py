"""In-process parallel local OCR engine (ctypes libtesseract) for zero-round-trip text clicking.

Why this exists:
Chromium, Electron, VS Code, Brave, Discord, Spotify, and games often expose 0 accessibility
elements on Linux/Windows/macOS. Without local OCR, clicking a button labeled "Settings" or
"Export" requires uploading a screenshot to a cloud LLM (2-4s), guessing grid coordinates, and
re-screenshotting. With 8-strip parallel libtesseract (~80-165 ms full 1080p, ~30-55 ms modal
crop, ~4-12 ms tiny bbox, 0.08 ms cached), `desktop_act` resolves exact word and multi-word
phrase bounding boxes locally and executes multi-step `click(text=...)` + `wait_and_click` +
`wait(text=...)` + `wait(gone=...)` chains in a fraction of a second with zero extra LLM turns.
"""

from __future__ import annotations

import atexit
import ctypes
import ctypes.util
import json
import mmap
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import numpy as np


def _normalize_token(s: str) -> str:
    return "".join(ch for ch in s.lower() if ch.isalnum() or ch in ("-", "_", ".", "/"))


def _omp_is_clean() -> bool:
    """Return True if libgomp in this process initialized with OMP_THREAD_LIMIT=1 (no OpenMP thrashing)."""
    if sys.platform in ("win32", "darwin"):
        return True
    if os.environ.get("COMPUTER_USE_OCR_INPROCESS", "").strip() == "1":
        return True
    if os.environ.get("_AGCU_OMP_CLEAN") == "1":
        return True
    try:
        with open("/proc/self/environ", "rb") as f:
            raw = f.read()
        if b"OMP_THREAD_LIMIT=1\x00" in raw or raw.endswith(b"OMP_THREAD_LIMIT=1"):
            return True
    except OSError:
        return True
    return False


def _silence_leptonica() -> None:
    """Suppress noisy Leptonica C stderr messages (e.g. boxClipToRectangle on thin UI borders)."""
    if sys.platform == "win32":
        cands = [ctypes.util.find_library("leptonica"), "leptonica-1.84.1.dll", "liblept-5.dll"]
    elif sys.platform == "darwin":
        cands = [
            ctypes.util.find_library("leptonica"),
            "/opt/homebrew/lib/libleptonica.dylib",
            "/usr/local/lib/libleptonica.dylib",
        ]
    else:
        cands = [
            ctypes.util.find_library("leptonica"),
            ctypes.util.find_library("lept"),
            "libleptonica.so.6",
            "liblept.so.5",
        ]
    for name in cands:
        if not name:
            continue
        try:
            lept = ctypes.CDLL(name)
            if hasattr(lept, "setMsgSeverity"):
                lept.setMsgSeverity.argtypes = [ctypes.c_int]
                lept.setMsgSeverity.restype = ctypes.c_int
                lept.setMsgSeverity(6)  # L_SEVERITY_NONE
                return
        except OSError:
            continue


def match_ocr_phrase(
    words: list[dict[str, Any]],
    query: str,
) -> dict[str, Any] | None:
    """Find best exact or substring match (single word or multi-word phrase) in OCR word boxes.

    Each word dict has keys: text, x, y, w, h, conf.
    Returns {text, bbox: [x, y, w, h], center: [cx, cy], conf, match_type} or None.
    """
    q_raw = query.strip().lower()
    if not q_raw or not words:
        return None
    q_tokens = [t for t in (_normalize_token(p) for p in q_raw.split()) if t]
    if not q_tokens:
        q_tokens = q_raw.split()

    # Group words into visual lines (words whose vertical centers lie within ~0.7 * median height)
    sorted_w = sorted(words, key=lambda w: (w["y"], w["x"]))
    lines: list[list[dict[str, Any]]] = []
    for w in sorted_w:
        wy_c = w["y"] + w["h"] * 0.5
        placed = False
        for ln in reversed(lines[-8:]):
            ly_c = ln[0]["y"] + ln[0]["h"] * 0.5
            tol = max(10.0, max(w["h"], ln[0]["h"]) * 0.7)
            if abs(wy_c - ly_c) <= tol:
                ln.append(w)
                placed = True
                break
        if not placed:
            lines.append([w])
    for ln in lines:
        ln.sort(key=lambda w: w["x"])

    best: tuple[float, dict[str, Any]] | None = None

    def consider(score: float, matched_words: list[dict[str, Any]], kind: str) -> None:
        nonlocal best
        x0 = min(m["x"] for m in matched_words)
        y0 = min(m["y"] for m in matched_words)
        x1 = max(m["x"] + m["w"] for m in matched_words)
        y1 = max(m["y"] + m["h"] for m in matched_words)
        bw, bh = max(1, x1 - x0), max(1, y1 - y0)
        avg_conf = sum(m["conf"] for m in matched_words) / len(matched_words)
        rank = score + avg_conf * 0.01
        if best is None or rank > best[0]:
            best = (
                rank,
                {
                    "text": " ".join(m["text"] for m in matched_words),
                    "bbox": [x0, y0, bw, bh],
                    "center": [x0 + bw // 2, y0 + bh // 2],
                    "conf": round(avg_conf, 1),
                    "match_type": kind,
                },
            )

    def _is_isolated(ln: list[dict[str, Any]], start: int, end: int) -> bool:
        left_ok = (start == 0) or (ln[start]["x"] - (ln[start - 1]["x"] + ln[start - 1]["w"]) > 32)
        right_ok = (end >= len(ln)) or (ln[end]["x"] - (ln[end - 1]["x"] + ln[end - 1]["w"]) > 32)
        return left_ok and right_ok

    n_qt = len(q_tokens)
    for ln in lines:
        norm_ln = [_normalize_token(m["text"]) for m in ln]
        raw_ln = [m["text"].lower() for m in ln]
        n = len(ln)
        if n_qt == 1:
            qt = q_tokens[0]
            for i in range(n):
                iso_bonus = 5.0 if _is_isolated(ln, i, i + 1) else 0.0
                if raw_ln[i] == q_raw or norm_ln[i] == qt:
                    consider(100.0 + iso_bonus, [ln[i]], "exact")
                elif qt and qt in norm_ln[i]:
                    ratio = len(qt) / max(1, len(norm_ln[i]))
                    consider(60.0 + ratio * 25.0 + iso_bonus * 0.5, [ln[i]], "substring")
        else:
            for i in range(0, n - n_qt + 1):
                window = ln[i:i + n_qt]
                too_far = False
                for j in range(n_qt - 1):
                    gap = window[j + 1]["x"] - (window[j]["x"] + window[j]["w"])
                    if gap > max(160, window[j]["h"] * 8):
                        too_far = True
                        break
                if too_far:
                    continue
                wn = norm_ln[i:i + n_qt]
                iso_bonus = 5.0 if _is_isolated(ln, i, i + n_qt) else 0.0
                if wn == q_tokens:
                    consider(110.0 + iso_bonus, window, "phrase_exact")
                elif all(q_tokens[j] in wn[j] for j in range(n_qt)):
                    consider(80.0 + iso_bonus * 0.5, window, "phrase_sub")

    return best[1] if best else None


def format_ocr_compact(words: list[dict[str, Any]], *, max_lines: int = 60) -> str:
    """Format OCR word boxes into compact clickable phrases: `[cx,cy] "Phrase"`."""
    if not words:
        return "(no text detected)"
    sorted_w = sorted(words, key=lambda w: (w["y"], w["x"]))
    lines: list[list[dict[str, Any]]] = []
    for w in sorted_w:
        wy_c = w["y"] + w["h"] * 0.5
        placed = False
        for ln in reversed(lines[-8:]):
            ly_c = ln[0]["y"] + ln[0]["h"] * 0.5
            tol = max(10.0, max(w["h"], ln[0]["h"]) * 0.7)
            if abs(wy_c - ly_c) <= tol:
                ln.append(w)
                placed = True
                break
        if not placed:
            lines.append([w])

    out_lines: list[str] = []
    for ln in lines[:max_lines]:
        ln.sort(key=lambda w: w["x"])
        segs: list[list[dict[str, Any]]] = [[ln[0]]]
        for prev, cur in zip(ln, ln[1:]):
            gap = cur["x"] - (prev["x"] + prev["w"])
            if gap > max(48, prev["h"] * 3):
                segs.append([cur])
            else:
                segs[-1].append(cur)
        parts: list[str] = []
        for seg in segs:
            x0 = min(m["x"] for m in seg)
            y0 = min(m["y"] for m in seg)
            x1 = max(m["x"] + m["w"] for m in seg)
            y1 = max(m["y"] + m["h"] for m in seg)
            cx = (x0 + x1) // 2
            cy = (y0 + y1) // 2
            txt = " ".join(m["text"] for m in seg)[:64]
            parts.append(f'[{cx},{cy}] "{txt}"')
        out_lines.append(" | ".join(parts))
    if len(lines) > max_lines:
        out_lines.append(f"... (+{len(lines) - max_lines} more lines)")
    return "\n".join(out_lines)


class _IsolatedOCRClient:
    """Persistent OMP_THREAD_LIMIT=1 helper subprocess over memfd+mmap (~0.5 ms zero-copy IPC).

    Used automatically only when a hot-reloaded Linux host process initialized libgomp without
    OMP_THREAD_LIMIT=1, restoring full 5-15x parallel OCR throughput without restarting the host.
    """

    def __init__(self, workers: int) -> None:
        self._workers = workers
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[str] | None = None
        self._fd: int | None = None
        self._mm: mmap.mmap | None = None
        self._buf_size = 0
        self.tessdata_fast = False

    def start(self) -> bool:
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return True
            self._close_locked()
            try:
                if not hasattr(os, "memfd_create"):
                    return False
                init_size = 1920 * 1080
                fd = os.memfd_create("agcu-ocr", 0)
                os.ftruncate(fd, init_size)
                mm = mmap.mmap(fd, init_size, access=mmap.ACCESS_WRITE)
                self._fd = fd
                self._mm = mm
                self._buf_size = init_size

                env = dict(os.environ)
                env["OMP_THREAD_LIMIT"] = "1"
                env["_AGCU_OMP_CLEAN"] = "1"
                env["COMPUTER_USE_OCR_INPROCESS"] = "1"
                env["PYTHONUNBUFFERED"] = "1"
                proc = subprocess.Popen(
                    [sys.executable, os.path.abspath(__file__), "--worker", str(self._workers), str(fd)],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    pass_fds=(fd,),
                    text=True,
                    env=env,
                    bufsize=1,
                )
                assert proc.stdout is not None
                line = proc.stdout.readline()
                if not line:
                    proc.terminate()
                    self._close_locked()
                    return False
                meta = json.loads(line)
                if not meta.get("ready"):
                    proc.terminate()
                    self._close_locked()
                    return False
                self._proc = proc
                self.tessdata_fast = bool(meta.get("tessdata_fast"))
                return True
            except Exception:
                self._close_locked()
                return False

    def _ensure_buf_locked(self, nbytes: int) -> int:
        assert self._fd is not None
        if self._mm is not None and self._buf_size >= nbytes:
            return self._buf_size
        if self._mm is not None:
            self._mm.close()
            self._mm = None
        alloc = max(nbytes, 1920 * 1080)
        os.ftruncate(self._fd, alloc)
        self._mm = mmap.mmap(self._fd, alloc, access=mmap.ACCESS_WRITE)
        self._buf_size = alloc
        return alloc

    def scan_gray(
        self,
        green_u8: np.ndarray,
        origin_x: int,
        origin_y: int,
        scale: float,
        min_conf: float,
    ) -> list[dict[str, Any]] | None:
        h, w = green_u8.shape
        nbytes = h * w
        with self._lock:
            if self._proc is None or self._proc.poll() is not None or self._fd is None:
                return None
            assert self._proc.stdin is not None and self._proc.stdout is not None
            try:
                buf_sz = self._ensure_buf_locked(nbytes)
                assert self._mm is not None
                view = np.ndarray((h, w), dtype=np.uint8, buffer=self._mm)
                view[:] = green_u8
                req = {
                    "sz": buf_sz,
                    "w": w,
                    "h": h,
                    "ox": origin_x,
                    "oy": origin_y,
                    "sc": scale,
                    "mc": min_conf,
                }
                self._proc.stdin.write(json.dumps(req, separators=(",", ":")) + "\n")
                self._proc.stdin.flush()
                line = self._proc.stdout.readline()
                if not line:
                    self._close_locked()
                    return None
                resp = json.loads(line)
                if resp.get("ok"):
                    return resp.get("words", [])
                return None
            except Exception:
                self._close_locked()
                return None

    def _close_locked(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is not None:
            try:
                if proc.stdin:
                    proc.stdin.close()
            except Exception:
                pass
            try:
                if proc.stdout:
                    proc.stdout.close()
            except Exception:
                pass
            try:
                proc.wait(timeout=0.5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        mm = self._mm
        self._mm = None
        self._buf_size = 0
        if mm is not None:
            try:
                mm.close()
            except Exception:
                pass
        fd = self._fd
        self._fd = None
        if fd is not None:
            try:
                os.close(fd)
            except Exception:
                pass

    def close(self) -> None:
        with self._lock:
            self._close_locked()


class FastOCR:
    """Parallel libtesseract OCR engine with lazy pool init, OMP isolation fallback, and frame caching."""

    def __init__(self, workers: int | None = None) -> None:
        self._lock = threading.Lock()
        self._workers = workers or max(2, min(8, os.cpu_count() or 4))
        self._lib = None
        self._apis: list[int] = []
        self._pool: ThreadPoolExecutor | None = None
        self._isolated: _IsolatedOCRClient | None = None
        self._init_done = False
        self._available = False
        self._tessdata_fast = False
        self._cache_fp: bytes | None = None
        self._cache_rect: tuple[int, int, int, int, float] | None = None
        self._cache_words: list[dict[str, Any]] = []
        atexit.register(self.close)

    def _find_lib(self) -> str | None:
        if sys.platform == "win32":
            cands = [
                ctypes.util.find_library("tesseract"),
                "tesseract54.dll", "tesseract53.dll", "libtesseract-5.dll",
                r"C:\Program Files\Tesseract-OCR\libtesseract-5.dll",
            ]
        elif sys.platform == "darwin":
            cands = [
                ctypes.util.find_library("tesseract"),
                "/opt/homebrew/lib/libtesseract.dylib",
                "/usr/local/lib/libtesseract.dylib",
                "libtesseract.5.dylib",
            ]
        else:
            cands = [
                ctypes.util.find_library("tesseract"),
                "libtesseract.so.5",
                "libtesseract.so.4",
                "libtesseract.so",
            ]
        for c in cands:
            if not c:
                continue
            try:
                lib = ctypes.CDLL(c)
                if hasattr(lib, "TessBaseAPICreate") and hasattr(lib, "TessBaseAPIGetTsvText"):
                    return c
            except OSError:
                continue
        return None

    def _find_tessdata_dir(self) -> bytes | None:
        """Prefer fast 4MB tessdata_fast eng.traineddata (~5x faster than 23MB distro tessdata_best)."""
        plugin_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        cands = [
            os.environ.get("COMPUTER_USE_TESSDATA_DIR", "").strip(),
            os.path.join(plugin_root, "assets", "tessdata"),
            os.path.expanduser("~/.cache/computer-use/tessdata"),
        ]
        for d in cands:
            if d and os.path.isfile(os.path.join(d, "eng.traineddata")):
                return d.encode("utf-8")
        return None

    def prewarm(self) -> None:
        """Lazily initialize Tesseract handles on a background daemon thread so first OCR has 0 ms cold init."""
        if self._init_done:
            return
        t = threading.Thread(target=self._ensure_init, daemon=True, name="agcu-ocr-prewarm")
        t.start()

    def _ensure_init(self) -> bool:
        if self._init_done:
            return self._available
        with self._lock:
            if self._init_done:
                return self._available
            # If parent Linux process initialized libgomp without OMP_THREAD_LIMIT=1 (hot-reloaded old host),
            # run Tesseract inside a single persistent OMP_THREAD_LIMIT=1 child process over SharedMemory!
            if not _omp_is_clean() and self._find_lib() is not None:
                iso = _IsolatedOCRClient(self._workers)
                if iso.start():
                    self._isolated = iso
                    self._tessdata_fast = iso.tessdata_fast
                    self._available = True
                    self._init_done = True
                    return True
            os.environ.setdefault("OMP_THREAD_LIMIT", "1")
            try:
                _silence_leptonica()
                path = self._find_lib()
                if not path:
                    return False
                lib = ctypes.CDLL(path)
                lib.TessBaseAPICreate.argtypes = []
                lib.TessBaseAPICreate.restype = ctypes.c_void_p
                lib.TessBaseAPIInit3.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p]
                lib.TessBaseAPIInit3.restype = ctypes.c_int
                lib.TessBaseAPISetVariable.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p]
                lib.TessBaseAPISetVariable.restype = ctypes.c_bool
                lib.TessBaseAPISetPageSegMode.argtypes = [ctypes.c_void_p, ctypes.c_int]
                lib.TessBaseAPISetPageSegMode.restype = None
                lib.TessBaseAPISetImage.argtypes = [
                    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                ]
                lib.TessBaseAPISetImage.restype = None
                lib.TessBaseAPISetSourceResolution.argtypes = [ctypes.c_void_p, ctypes.c_int]
                lib.TessBaseAPISetSourceResolution.restype = None
                lib.TessBaseAPIGetTsvText.argtypes = [ctypes.c_void_p, ctypes.c_int]
                lib.TessBaseAPIGetTsvText.restype = ctypes.c_void_p
                lib.TessDeleteText.argtypes = [ctypes.c_void_p]
                lib.TessDeleteText.restype = None
                lib.TessBaseAPIDelete.argtypes = [ctypes.c_void_p]
                lib.TessBaseAPIDelete.restype = None

                tessdata_dir = self._find_tessdata_dir()
                apis: list[int] = []
                for _ in range(self._workers):
                    a = lib.TessBaseAPICreate()
                    if not a:
                        continue
                    rc = int(lib.TessBaseAPIInit3(a, tessdata_dir, b"eng"))
                    if rc != 0 and tessdata_dir is not None:
                        rc = int(lib.TessBaseAPIInit3(a, None, b"eng"))
                    if rc != 0:
                        lib.TessBaseAPIDelete(a)
                        continue
                    lib.TessBaseAPISetPageSegMode(a, 11)  # PSM_SPARSE_TEXT (ideal for UI labels & buttons)
                    lib.TessBaseAPISetVariable(a, b"tessedit_do_invert", b"0")
                    # Skip dictionary/DAWG beam search overhead on UI button/channel labels
                    lib.TessBaseAPISetVariable(a, b"load_system_dawg", b"0")
                    lib.TessBaseAPISetVariable(a, b"load_freq_dawg", b"0")
                    lib.TessBaseAPISetVariable(a, b"tessedit_enable_dict_correction", b"0")
                    lib.TessBaseAPISetVariable(a, b"debug_file", b"NUL" if sys.platform == "win32" else b"/dev/null")
                    apis.append(int(a))
                if not apis:
                    return False
                self._lib = lib
                self._apis = apis
                self._tessdata_fast = tessdata_dir is not None
                self._pool = ThreadPoolExecutor(max_workers=len(apis), thread_name_prefix="agcu-ocr")
                self._available = True
                return True
            except Exception:
                self.close()
                return False
            finally:
                self._init_done = True

    @property
    def available(self) -> bool:
        if self._init_done:
            return self._available
        return self._find_lib() is not None

    @property
    def backend_name(self) -> str:
        if not self.available:
            return "unavailable"
        if self._isolated is not None:
            return "libtesseract-parallel-isolated"
        return "libtesseract-parallel"

    def close(self) -> None:
        with self._lock:
            iso = self._isolated
            self._isolated = None
            pool = self._pool
            self._pool = None
            apis = self._apis
            self._apis = []
            lib = self._lib
            self._lib = None
            self._available = False
        if iso is not None:
            try:
                iso.close()
            except Exception:
                pass
        if pool is not None:
            try:
                pool.shutdown(wait=True)
            except Exception:
                pass
        if lib is not None:
            for a in apis:
                try:
                    lib.TessBaseAPIDelete(a)
                except Exception:
                    pass

    @staticmethod
    def _normalize_dark_light_gray(gray: np.ndarray) -> np.ndarray:
        """Smooth O(1) 4x-downsampled local-background polarity normalizer (~6-9 ms on 1080p).

        Handles dark apps, bright white modals over dark apps, and colored buttons (blurple/green/red
        with white text) inside white modals simultaneously without creating dark tile borders.
        """
        h, w = gray.shape
        if h < 32 or w < 32:
            return np.ascontiguousarray(255 - gray) if float(gray.mean()) < 128.0 else gray
        g_f = gray.astype(np.float32)
        step = 4 if (h >= 120 and w >= 120) else 2
        q = g_f[::step, ::step]
        r = min(3 if step == 4 else 6, max(1, min(q.shape[0], q.shape[1]) // 4))
        p = np.pad(q, ((r, r), (r, r)), mode="edge")
        cs = np.cumsum(p, axis=0)
        z0 = np.zeros((1, p.shape[1]), dtype=np.float32)
        inv_span = 1.0 / float(2 * r + 1)
        v = (cs[2 * r:, :] - np.vstack([z0, cs[:-2 * r - 1, :]])) * inv_span
        cs2 = np.cumsum(v, axis=1)
        z1 = np.zeros((v.shape[0], 1), dtype=np.float32)
        bg_q = (cs2[:, 2 * r:] - np.hstack([z1, cs2[:, :-2 * r - 1]])) * inv_span
        bg = np.repeat(np.repeat(bg_q, step, axis=0), step, axis=1)[:h, :w]
        dark = (bg < 150.0) | ((g_f >= 75.0) & (g_f < 175.0) & (bg < 220.0))
        if not dark.any():
            return gray
        inv_boost = np.clip(255.0 - np.maximum(0.0, g_f - bg) * (255.0 / np.maximum(64.0, 255.0 - bg)), 0.0, 255.0)
        return np.ascontiguousarray(np.where(dark, inv_boost, g_f).astype(np.uint8))

    def _scan_green_inprocess(
        self,
        green_u8: np.ndarray,
        ox: int,
        oy: int,
        sc: float,
        min_conf: float,
    ) -> list[dict[str, Any]]:
        h, w = green_u8.shape
        gray = self._normalize_dark_light_gray(green_u8)
        n_workers = min(len(self._apis), max(1, h // 48))
        lib = self._lib
        apis = self._apis

        def run_strip(idx: int) -> list[dict[str, Any]]:
            overlap = 20
            y0 = max(0, (idx * h) // n_workers - (overlap if idx > 0 else 0))
            y1 = min(h, ((idx + 1) * h) // n_workers + (overlap if idx < n_workers - 1 else 0))
            if y1 - y0 < 6:
                return []
            sub = np.ascontiguousarray(gray[y0:y1, :])
            # Skip solid blank strips in ~0.02 ms without invoking Tesseract
            if int((sub[::2, ::2] < 190).sum()) < 5:
                return []
            a = apis[idx]
            lib.TessBaseAPISetImage(a, sub.ctypes.data, w, y1 - y0, 1, w)
            lib.TessBaseAPISetSourceResolution(a, 96)
            ptr = lib.TessBaseAPIGetTsvText(a, 0)
            txt = ctypes.string_at(ptr).decode("utf-8", errors="replace") if ptr else ""
            if ptr:
                lib.TessDeleteText(ptr)
            out: list[dict[str, Any]] = []
            band_lo = (idx * h) // n_workers
            band_hi = ((idx + 1) * h) // n_workers
            for ln in txt.splitlines()[1:]:
                p = ln.split("\t")
                if len(p) < 12:
                    continue
                word = p[11].strip()
                if not word:
                    continue
                try:
                    conf = float(p[10])
                except ValueError:
                    continue
                if conf < min_conf:
                    continue
                px = int(p[6])
                py = y0 + int(p[7])
                pw = int(p[8])
                ph = int(p[9])
                if pw < 2 or ph < 4:
                    continue
                mid_y = py + ph * 0.5
                if idx > 0 and mid_y < band_lo:
                    continue
                if idx < n_workers - 1 and mid_y >= band_hi:
                    continue
                lx = ox + int(round(px / sc))
                ly = oy + int(round(py / sc))
                lw = max(1, int(round(pw / sc)))
                lh = max(1, int(round(ph / sc)))
                out.append({"text": word, "x": lx, "y": ly, "w": lw, "h": lh, "conf": conf})
            return out

        assert self._pool is not None
        if n_workers == 1:
            return run_strip(0)
        return [item for chunk in self._pool.map(run_strip, range(n_workers)) for item in chunk]

    def scan_frame(self, frame, *, min_conf: float = 35.0) -> list[dict[str, Any]]:
        """Extract all UI word boxes from a Frame in screen logical coordinates (cached per unchanged frame)."""
        if not self._ensure_init():
            return []
        rgba = frame.rgba
        h, w = rgba.shape[:2]
        if w < 8 or h < 8:
            return []

        step_x = max(1, w // 48)
        step_y = max(1, h // 27)
        fp = rgba[::step_y, ::step_x, 1].tobytes()
        rect_sig = (frame.origin_x, frame.origin_y, w, h, round(frame.scale, 3))
        if fp == self._cache_fp and rect_sig == self._cache_rect:
            return self._cache_words

        green_u8 = np.ascontiguousarray(rgba[:, :, 1])
        sc = frame.scale if frame.scale > 0 else 1.0
        ox, oy = frame.origin_x, frame.origin_y

        words: list[dict[str, Any]] | None = None
        if self._isolated is not None:
            words = self._isolated.scan_gray(green_u8, ox, oy, sc, min_conf)
            if words is None:
                # Worker died unexpectedly; re-init cleanly once
                self._isolated.close()
                if self._isolated.start():
                    words = self._isolated.scan_gray(green_u8, ox, oy, sc, min_conf)
        if words is None:
            if self._lib is None or not self._apis:
                return []
            words = self._scan_green_inprocess(green_u8, ox, oy, sc, min_conf)

        self._cache_fp = fp
        self._cache_rect = rect_sig
        self._cache_words = words
        return words

    def find_text(self, frame, query: str) -> dict[str, Any] | None:
        words = self.scan_frame(frame)
        return match_ocr_phrase(words, query)


def _run_worker_main(workers: int, mem_fd: int) -> int:
    os.environ["OMP_THREAD_LIMIT"] = "1"
    os.environ["_AGCU_OMP_CLEAN"] = "1"
    os.environ["COMPUTER_USE_OCR_INPROCESS"] = "1"
    ocr = FastOCR(workers=workers)
    ok = ocr._ensure_init()
    sys.stdout.write(json.dumps({"ready": ok, "tessdata_fast": ocr._tessdata_fast}) + "\n")
    sys.stdout.flush()
    if not ok:
        return 1

    mm: mmap.mmap | None = None
    mm_size = 0
    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
                sz = int(req["sz"])
                if mm is None or sz > mm_size:
                    if mm is not None:
                        mm.close()
                    mm = mmap.mmap(mem_fd, sz, access=mmap.ACCESS_READ)
                    mm_size = sz
                h, w = int(req["h"]), int(req["w"])
                green = np.ndarray((h, w), dtype=np.uint8, buffer=mm)
                words = ocr._scan_green_inprocess(
                    green,
                    int(req["ox"]),
                    int(req["oy"]),
                    float(req["sc"]),
                    float(req["mc"]),
                )
                sys.stdout.write(json.dumps({"ok": True, "words": words}, separators=(",", ":")) + "\n")
                sys.stdout.flush()
            except Exception as exc:
                sys.stdout.write(json.dumps({"ok": False, "error": str(exc)}) + "\n")
                sys.stdout.flush()
    finally:
        if mm is not None:
            try:
                mm.close()
            except Exception:
                pass
        try:
            os.close(mem_fd)
        except Exception:
            pass
        ocr.close()
    return 0


if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "--worker":
        sys.exit(_run_worker_main(int(sys.argv[2]), int(sys.argv[3])))
