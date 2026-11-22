"""In-process parallel local OCR engine (ctypes libtesseract) for zero-round-trip text clicking.

Why this exists:
Chromium, Electron, VS Code, Brave, Discord, Spotify, and games often expose 0 accessibility
elements on Linux/Windows/macOS. Without local OCR, clicking a button labeled "Settings" or
"Export" requires uploading a screenshot to a cloud LLM (2-4s), guessing grid coordinates, and
re-screenshotting. With 8-strip parallel in-process libtesseract (~80-165 ms full 1080p, ~30-60 ms
active window, 0.05 ms cached), `desktop_act` resolves exact word and multi-word phrase bounding
boxes locally and executes multi-step `click(text=...)` + `wait_and_click(text=...)` chains in
a fraction of a second with zero extra LLM turns.
"""

from __future__ import annotations

import atexit
import ctypes
import ctypes.util
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import numpy as np


def _normalize_token(s: str) -> str:
    return "".join(ch for ch in s.lower() if ch.isalnum() or ch in ("-", "_", ".", "/"))


def match_ocr_phrase(
    words: list[dict[str, Any]],
    query: str,
) -> dict[str, Any] | None:
    """Find best exact or substring match (single word or multi-word phrase) in OCR word boxes.

    Each word dict has keys: text, x, y, w, h, conf, line_key.
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

    n_qt = len(q_tokens)
    for ln in lines:
        norm_ln = [_normalize_token(m["text"]) for m in ln]
        raw_ln = [m["text"].lower() for m in ln]
        n = len(ln)
        if n_qt == 1:
            qt = q_tokens[0]
            for i in range(n):
                if raw_ln[i] == q_raw or norm_ln[i] == qt:
                    consider(100.0, [ln[i]], "exact")
                elif qt and qt in norm_ln[i]:
                    # Prefer tighter matches over huge unrelated words
                    ratio = len(qt) / max(1, len(norm_ln[i]))
                    consider(60.0 + ratio * 25.0, [ln[i]], "substring")
        else:
            # Exact / near-contiguous phrase match along a visual line
            for i in range(0, n - n_qt + 1):
                window = ln[i:i + n_qt]
                # Reject words separated by huge horizontal gaps (> 180px across columns)
                too_far = False
                for j in range(n_qt - 1):
                    gap = window[j + 1]["x"] - (window[j]["x"] + window[j]["w"])
                    if gap > max(160, window[j]["h"] * 8):
                        too_far = True
                        break
                if too_far:
                    continue
                wn = norm_ln[i:i + n_qt]
                if wn == q_tokens:
                    consider(110.0, window, "phrase_exact")
                elif all(q_tokens[j] in wn[j] for j in range(n_qt)):
                    consider(80.0, window, "phrase_sub")

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
        # Split visual line into separate clickable segments across wide horizontal gaps
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


class FastOCR:
    """Parallel in-process libtesseract OCR engine with lazy pool init and frame caching."""

    def __init__(self, workers: int | None = None) -> None:
        self._lock = threading.Lock()
        self._workers = workers or max(2, min(8, os.cpu_count() or 4))
        self._lib = None
        self._apis: list[int] = []
        self._pool: ThreadPoolExecutor | None = None
        self._init_done = False
        self._available = False
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

    def _ensure_init(self) -> bool:
        if self._init_done:
            return self._available
        with self._lock:
            if self._init_done:
                return self._available
            self._init_done = True
            # Prevent internal OpenMP oversubscription when running N strip workers in parallel
            os.environ.setdefault("OMP_THREAD_LIMIT", "1")
            path = self._find_lib()
            if not path:
                return False
            try:
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

                apis: list[int] = []
                for _ in range(self._workers):
                    a = lib.TessBaseAPICreate()
                    if not a:
                        continue
                    rc = int(lib.TessBaseAPIInit3(a, None, b"eng"))
                    if rc != 0:
                        lib.TessBaseAPIDelete(a)
                        continue
                    lib.TessBaseAPISetPageSegMode(a, 11)  # PSM_SPARSE_TEXT (ideal for UI labels & buttons)
                    lib.TessBaseAPISetVariable(a, b"tessedit_do_invert", b"0")
                    lib.TessBaseAPISetVariable(a, b"debug_file", b"NUL" if sys.platform == "win32" else b"/dev/null")
                    apis.append(int(a))
                if not apis:
                    return False
                self._lib = lib
                self._apis = apis
                self._pool = ThreadPoolExecutor(max_workers=len(apis), thread_name_prefix="agcu-ocr")
                self._available = True
                return True
            except Exception:
                self.close()
                return False

    @property
    def available(self) -> bool:
        if self._init_done:
            return self._available
        return self._find_lib() is not None

    def close(self) -> None:
        with self._lock:
            pool = self._pool
            self._pool = None
            apis = self._apis
            self._apis = []
            lib = self._lib
            self._lib = None
            self._available = False
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

    def scan_frame(self, frame, *, min_conf: float = 35.0) -> list[dict[str, Any]]:
        """Extract all UI word boxes from a Frame in screen logical coordinates (cached per unchanged frame)."""
        if not self._ensure_init():
            return []
        rgba = frame.rgba
        h, w = rgba.shape[:2]
        if w < 8 or h < 8:
            return []

        # Ultra-fast frame fingerprint (~0.08 ms) so repeat lookups on the same frame hit cache in <0.1 ms
        step_x = max(1, w // 48)
        step_y = max(1, h // 27)
        fp = rgba[::step_y, ::step_x, 1].tobytes()
        rect_sig = (frame.origin_x, frame.origin_y, w, h, round(frame.scale, 3))
        if fp == self._cache_fp and rect_sig == self._cache_rect:
            return self._cache_words

        # Green channel is an accurate ~0.1ms luminance proxy; auto-invert dark-theme UIs for Tesseract
        gray = np.ascontiguousarray(rgba[:, :, 1])
        if float(gray[::4, ::4].mean()) < 120.0:
            gray = np.ascontiguousarray(255 - gray)

        n_workers = min(len(self._apis), max(1, h // 90))
        lib = self._lib
        apis = self._apis
        sc = frame.scale if frame.scale > 0 else 1.0
        ox, oy = frame.origin_x, frame.origin_y

        def run_strip(idx: int) -> list[dict[str, Any]]:
            overlap = 12
            y0 = max(0, (idx * h) // n_workers - (overlap if idx > 0 else 0))
            y1 = min(h, ((idx + 1) * h) // n_workers + (overlap if idx < n_workers - 1 else 0))
            if y1 - y0 < 6:
                return []
            sub = np.ascontiguousarray(gray[y0:y1, :])
            a = apis[idx]
            lib.TessBaseAPISetImage(a, sub.ctypes.data, w, y1 - y0, 1, w)
            lib.TessBaseAPISetSourceResolution(a, 96)
            ptr = lib.TessBaseAPIGetTsvText(a, 0)
            txt = ctypes.string_at(ptr).decode("utf-8", errors="replace") if ptr else ""
            if ptr:
                lib.TessDeleteText(ptr)
            out: list[dict[str, Any]] = []
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
                # Deduplicate overlap boundary: keep word in the strip owning its vertical center
                mid_y = py + ph * 0.5
                band_lo = (idx * h) // n_workers
                band_hi = ((idx + 1) * h) // n_workers
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
            words = run_strip(0)
        else:
            words = [item for chunk in self._pool.map(run_strip, range(n_workers)) for item in chunk]

        self._cache_fp = fp
        self._cache_rect = rect_sig
        self._cache_words = words
        return words

    def find_text(self, frame, query: str) -> dict[str, Any] | None:
        words = self.scan_frame(frame)
        return match_ocr_phrase(words, query)
