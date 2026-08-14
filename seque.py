#!/usr/bin/env python3
"""Seque desktop front-end for SLIM to PRI conversion.

This file contains only the Tkinter user interface and batch-job orchestration.
The conversion logic lives in ``slim_to_pri.py`` so command-line and GUI output
stay consistent.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime
from multiprocessing import Manager
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Iterable, Sequence
from ctypes import wintypes

from fix_pri_geometry import DEFAULT_MIN_ANGLE, DEFAULT_MIN_DISTANCE
from slim_to_pri import ConversionError, ValidationSummary, __version__, convert


# Keep the native Tk drag-and-drop package with the application, so users do
# not have to install anything into their Python environment.
VENDOR_PATH = Path(__file__).with_name(".vendor")
if str(VENDOR_PATH) not in sys.path:
    sys.path.insert(0, str(VENDOR_PATH))

from tkinterdnd2 import COPY, DND_FILES, TkinterDnD


APP_NAME = "Seque"
APP_VERSION = __version__
SETTINGS_PATH = Path(os.environ.get("APPDATA", Path.home())) / APP_NAME / "settings.json"
TOOLBAR_ICON_DIR = Path(__file__).with_name("assets") / "toolbar"
PREVIEW_FOLDER = Path(os.environ.get("LOCALAPPDATA", Path.home())) / APP_NAME / "excavation-previews"
PROROCK_EXE = Path(r"C:\Program Files (x86)\Prorock\Prorock.exe")


def explorer_clipboard_paths() -> list[str]:
    """Read files copied by Explorer from the Windows CF_HDROP clipboard."""
    if sys.platform != "win32":
        return []
    user32 = ctypes.windll.user32
    shell32 = ctypes.windll.shell32
    # ctypes otherwise assumes 32-bit integer return values. Clipboard HDROP
    # handles are pointers, so that truncates them on 64-bit Python/Windows.
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = wintypes.BOOL
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE
    shell32.DragQueryFileW.argtypes = [wintypes.HANDLE, wintypes.UINT, wintypes.LPWSTR, wintypes.UINT]
    shell32.DragQueryFileW.restype = wintypes.UINT
    CF_HDROP = 15
    if not user32.OpenClipboard(None):
        return []
    try:
        handle = user32.GetClipboardData(CF_HDROP)
        if not handle:
            return []
        count = shell32.DragQueryFileW(handle, 0xFFFFFFFF, None, 0)
        paths: list[str] = []
        for index in range(count):
            length = shell32.DragQueryFileW(handle, index, None, 0)
            buffer = ctypes.create_unicode_buffer(length + 1)
            shell32.DragQueryFileW(handle, index, buffer, len(buffer))
            paths.append(buffer.value)
        return paths
    finally:
        user32.CloseClipboard()


@dataclass
class ConversionJob:
    input_path: Path
    output_path: Path


@dataclass(frozen=True)
class ExcavationPreview:
    input_path: Path
    preview_path: Path


def convert_job(
    job: ConversionJob,
    job_index: int,
    transfer_water: bool,
    fix_geometry: bool,
    add_excavation_stages: bool,
    excavation_stage_count: int,
    excavation_top_point: tuple[float, float] | None,
    remove_bolts: bool,
    min_distance: float,
    min_angle: float,
    progress_queue: object,
) -> ValidationSummary:
    """Run one independent conversion in a separate Python process."""
    def report(message: str) -> None:
        progress_queue.put((job_index, message))

    job.output_path.parent.mkdir(parents=True, exist_ok=True)
    return convert(
        job.input_path,
        job.output_path,
        include_water_table=transfer_water,
        fix_geometry=fix_geometry,
        add_excavation_stages=add_excavation_stages,
        excavation_stage_count=excavation_stage_count,
        excavation_top_point=excavation_top_point,
        remove_bolts=remove_bolts,
        geometry_min_distance=min_distance,
        geometry_min_angle=min_angle,
        progress=report,
    )


def common_parent(paths: Iterable[Path]) -> Path | None:
    resolved = [path.resolve() for path in paths]
    if not resolved:
        return None

    common = resolved[0].parent
    for path in resolved[1:]:
        while common != common.parent and common not in path.parents:
            common = common.parent
    return common


class SequeApp(TkinterDnD.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_NAME)
        self.minsize(860, 560)

        self.files: list[Path] = []
        self.output_folder = tk.StringVar(value=self._load_output_folder())
        self.transfer_water = tk.BooleanVar(value=True)
        # Geometry cleanup changes CAD vertices. Keep source geometry intact
        # unless the user explicitly requests the repair pass.
        self.fix_geometry = tk.BooleanVar(value=False)
        self.add_excavation_stages = tk.BooleanVar(value=False)
        self.excavation_stage_count = tk.IntVar(value=4)
        self.excavation_top_x = tk.DoubleVar(value=0.0)
        self.excavation_top_y = tk.DoubleVar(value=0.0)
        self.excavation_top_point: tuple[float, float] | None = None
        self._preview_in_progress = False
        self._preview_for: Path | None = None
        self.remove_bolts = tk.BooleanVar(value=True)
        self.geometry_min_distance = tk.StringVar(value=str(int(DEFAULT_MIN_DISTANCE)))
        self.geometry_min_angle = tk.StringVar(value=str(int(DEFAULT_MIN_ANGLE)))
        self.worker_count = tk.StringVar(value=str(min(4, os.cpu_count() or 1)))
        self.is_converting = False
        self.result_queue: queue.Queue[tuple[str, object]] = queue.Queue()

        self._build_ui()
        self.output_folder.trace_add("write", self._save_output_folder)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._bind_file_paste_shortcut()
        self._enable_file_drop()
        self._set_status("Ready")

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=14)
        root.pack(fill=tk.BOTH, expand=True)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(1, weight=1)
        root.rowconfigure(4, weight=1)

        header = ttk.Frame(root)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        header.columnconfigure(0, weight=1)

        title = ttk.Label(header, text=APP_NAME, font=("Segoe UI", 20, "bold"))
        title.grid(row=0, column=0, sticky="w")

        actions = ttk.Frame(header)
        actions.grid(row=0, column=1, sticky="e")
        self._toolbar_icons = {
            name: tk.PhotoImage(file=TOOLBAR_ICON_DIR / f"{name}.png")
            for name in ("add", "remove", "clear")
        }
        self.add_button = ttk.Button(
            actions,
            image=self._toolbar_icons["add"],
            width=3,
            command=self.add_files,
        )
        self.add_button.grid(row=0, column=0, padx=(0, 6))
        self.remove_button = ttk.Button(
            actions,
            image=self._toolbar_icons["remove"],
            width=3,
            command=self.remove_selected,
        )
        self.remove_button.grid(row=0, column=1, padx=(0, 6))
        self.clear_button = ttk.Button(
            actions,
            image=self._toolbar_icons["clear"],
            width=3,
            command=self.clear_files,
        )
        self.clear_button.grid(row=0, column=2)
        self._add_tooltip(self.add_button, "Add .slim/.sli files")
        self._add_tooltip(self.remove_button, "Remove selected files")
        self._add_tooltip(self.clear_button, "Clear file list")

        list_frame = ttk.LabelFrame(root, text="Input Files", padding=10)
        list_frame.grid(row=1, column=0, sticky="nsew")
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)

        columns = ("file", "folder", "status")
        self.tree = ttk.Treeview(list_frame, columns=columns, show="headings", selectmode="extended")
        self.tree.heading("file", text="File")
        self.tree.heading("folder", text="Folder")
        self.tree.heading("status", text="Status")
        self.tree.column("file", width=210, minwidth=150)
        self.tree.column("folder", width=470, minwidth=220)
        self.tree.column("status", width=110, minwidth=90, anchor="w")
        self.tree.grid(row=0, column=0, sticky="nsew")

        scroll_y = ttk.Scrollbar(list_frame, orient="vertical", command=self.tree.yview)
        scroll_y.grid(row=0, column=1, sticky="ns")
        scroll_x = ttk.Scrollbar(list_frame, orient="horizontal", command=self.tree.xview)
        scroll_x.grid(row=1, column=0, sticky="ew")
        self.tree.configure(yscrollcommand=scroll_y.set, xscrollcommand=scroll_x.set)
        self.file_menu = tk.Menu(self, tearoff=False)
        self.file_menu.add_command(label="Paste input file(s)", command=self._paste_input_files)
        self.tree.bind("<Button-3>", self._show_file_menu)

        output_frame = ttk.LabelFrame(root, text="Output folder", padding=10)
        output_frame.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        output_frame.columnconfigure(0, weight=1)

        self.output_entry = ttk.Entry(output_frame, textvariable=self.output_folder)
        self.output_entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.output_button = ttk.Button(output_frame, text="Choose Folder", command=self.choose_output_folder)
        self.output_button.grid(row=0, column=1)

        advanced = ttk.LabelFrame(root, text="Advanced Settings", padding=10)
        advanced.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        self.water_check = ttk.Checkbutton(
            advanced,
            text="Transfer water table from SLIM",
            variable=self.transfer_water,
        )
        self.water_check.grid(row=0, column=0, sticky="w")
        self.fix_geometry_check = ttk.Checkbutton(
            advanced,
            text="Fix PRI geometry before meshing (changes CAD vertices)",
            variable=self.fix_geometry,
            command=self._refresh_geometry_controls,
        )
        self.fix_geometry_check.grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.excavation_stages_check = ttk.Checkbutton(
            advanced,
            text="Add excavation stages",
            variable=self.add_excavation_stages,
            command=self._configure_excavation_stages,
        )
        self.excavation_stages_check.grid(row=2, column=0, sticky="w", pady=(6, 0))
        self.remove_bolts_check = ttk.Checkbutton(
            advanced,
            text="Remove bolt and anchor geometry",
            variable=self.remove_bolts,
        )
        self.remove_bolts_check.grid(row=3, column=0, sticky="w", pady=(6, 0))

        geometry_settings = ttk.Frame(advanced)
        geometry_settings.grid(row=4, column=0, sticky="w", pady=(6, 0))
        self.min_distance_label = ttk.Label(geometry_settings, text="Minimum distance, m")
        self.min_distance_label.grid(row=0, column=0, sticky="w")
        self.min_distance_entry = ttk.Entry(geometry_settings, width=8, textvariable=self.geometry_min_distance)
        self.min_distance_entry.grid(row=0, column=1, sticky="w", padx=(6, 14))
        self.min_angle_label = ttk.Label(geometry_settings, text="Minimum angle, deg")
        self.min_angle_label.grid(row=0, column=2, sticky="w")
        self.min_angle_entry = ttk.Entry(geometry_settings, width=8, textvariable=self.geometry_min_angle)
        self.min_angle_entry.grid(row=0, column=3, sticky="w", padx=(6, 0))
        self.worker_count_label = ttk.Label(geometry_settings, text="Parallel files")
        self.worker_count_label.grid(row=0, column=4, sticky="w", padx=(18, 0))
        self.worker_count_entry = ttk.Entry(geometry_settings, width=5, textvariable=self.worker_count)
        self.worker_count_entry.grid(row=0, column=5, sticky="w", padx=(6, 0))
        self._refresh_geometry_controls()

        log_frame = ttk.LabelFrame(root, text="Progress log", padding=8)
        log_frame.grid(row=4, column=0, sticky="nsew", pady=(10, 0))
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)

        self.log_text = tk.Text(
            log_frame,
            height=9,
            wrap="word",
            state=tk.DISABLED,
            font=("Consolas", 9),
        )
        self.log_text.grid(row=0, column=0, sticky="nsew")
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        log_scroll.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=log_scroll.set)

        footer = ttk.Frame(root)
        footer.grid(row=5, column=0, sticky="ew", pady=(12, 0))
        footer.columnconfigure(0, weight=1)

        self.status_label = ttk.Label(footer, text="")
        self.status_label.grid(row=0, column=0, sticky="w")

        self.progress = ttk.Progressbar(footer, mode="determinate")
        self.progress.grid(row=0, column=1, sticky="e", padx=(10, 8))

        self.convert_button = ttk.Button(footer, text="Convert", command=self.start_conversion)
        self.convert_button.grid(row=0, column=2, sticky="e")

    def add_files(self) -> None:
        selected = filedialog.askopenfilenames(
            title="Select .slim files",
            filetypes=((".slim files", "*.slim"), (".sli files", "*.sli"), ("All files", "*.*")),
        )
        if not selected:
            return

        self._add_input_paths(selected)

    def _add_input_paths(self, paths: Iterable[str | Path]) -> None:
        """Add valid input files from a dialog or a native Windows drop."""
        known = {path.resolve() for path in self.files}
        added = 0
        ignored = 0
        for value in paths:
            path = Path(value)
            if not path.is_file() or path.suffix.lower() not in {".slim", ".sli"}:
                ignored += 1
                continue
            resolved = path.resolve()
            if resolved in known:
                continue
            self.files.append(path)
            known.add(resolved)
            added += 1
        if added:
            self.refresh_file_list()
        if ignored:
            self._append_log(f"Ignored {ignored} dropped item(s): only .slim and .sli files are supported")
        if added:
            self._set_status(f"{len(self.files)} file(s) loaded")

    def _enable_file_drop(self) -> None:
        """Use the maintained TkDND extension instead of ctypes callbacks."""
        for widget in (self, self.tree):
            widget.drop_target_register(DND_FILES)
            widget.dnd_bind("<<Drop>>", self._on_file_drop)

    def _on_file_drop(self, event: object) -> str:
        # Tcl parses braced paths and paths containing spaces correctly.
        paths = self.tk.splitlist(event.data)
        self._add_input_paths(paths)
        return COPY

    def _paste_input_files(self, _: object | None = None) -> str | None:
        """Add .slim/.sli file paths copied from Windows Explorer."""
        # Explorer uses CF_HDROP rather than text, so query it before the
        # normal Tk text clipboard. This is the path used by ordinary Ctrl+C.
        explorer_paths = explorer_clipboard_paths()
        if explorer_paths:
            self._add_input_paths(explorer_paths)
            return "break"
        try:
            raw = self.clipboard_get()
        except tk.TclError:
            return None
        # Explorer puts multiple selected paths into the clipboard as a Tcl
        # list; other file managers often use one path per line.
        try:
            values = list(self.tk.splitlist(raw))
        except tk.TclError:
            values = raw.splitlines()
        paths = [value.strip().strip('"') for value in values if value.strip()]
        # A manually copied single path may not be Tcl-braced, so Tcl splits
        # its spaces. Keep the raw line as an additional candidate.
        paths.extend(line.strip().strip('"') for line in raw.splitlines() if line.strip())
        input_paths = [
            path for path in paths
            if Path(path).suffix.lower() in {".slim", ".sli"} and Path(path).is_file()
        ]
        if not input_paths:
            # Let Entry/Text widgets handle ordinary Ctrl+V text paste.
            return None
        self._add_input_paths(input_paths)
        return "break"

    def _show_file_menu(self, event: tk.Event[tk.Misc]) -> str:
        self.file_menu.tk_popup(event.x_root, event.y_root)
        return "break"

    def _bind_file_paste_shortcut(self) -> None:
        """Bind Ctrl+V before default Entry/Text paste handling in this window."""
        def visit(widget: tk.Misc) -> None:
            widget.bind("<Control-v>", self._paste_input_files, add=True)
            widget.bind("<Control-V>", self._paste_input_files, add=True)
            for child in widget.winfo_children():
                visit(child)

        visit(self)

    def _add_tooltip(self, widget: tk.Widget, message: str) -> None:
        """Show a small delayed hint for an icon-only button."""
        popup: tk.Toplevel | None = None
        timer: str | None = None

        def hide(_: object | None = None) -> None:
            nonlocal popup, timer
            if timer is not None:
                widget.after_cancel(timer)
                timer = None
            if popup is not None:
                popup.destroy()
                popup = None

        def show() -> None:
            nonlocal popup, timer
            timer = None
            if popup is not None or str(widget.cget("state")) == str(tk.DISABLED):
                return
            popup = tk.Toplevel(widget)
            popup.wm_overrideredirect(True)
            popup.wm_geometry(f"+{widget.winfo_rootx()}+{widget.winfo_rooty() + widget.winfo_height() + 4}")
            ttk.Label(popup, text=message, padding=(6, 3)).pack()

        def schedule(_: object) -> None:
            nonlocal timer
            hide()
            timer = widget.after(500, show)

        widget.bind("<Enter>", schedule, add=True)
        widget.bind("<Leave>", hide, add=True)
        widget.bind("<ButtonPress>", hide, add=True)

    def _configure_excavation_stages(self) -> None:
        """Create and open a clean temporary PRI before asking for its point."""
        if not self.add_excavation_stages.get():
            self.excavation_top_point = None
            return
        if len(self.files) != 1:
            self.add_excavation_stages.set(False)
            messagebox.showwarning(
                APP_NAME,
                "Excavation stages use a manually selected top point. Add exactly one input file, then enable this option.",
            )
            return
        self._start_excavation_preview(self.files[0])

    def _preview_path_for(self, input_path: Path) -> Path:
        digest = hashlib.sha256(str(input_path.resolve()).encode("utf-8")).hexdigest()[:12]
        return PREVIEW_FOLDER / f"{input_path.stem}-{digest}.pri"

    def _start_excavation_preview(self, input_path: Path) -> None:
        if self._preview_in_progress:
            return
        self._preview_in_progress = True
        self._preview_for = input_path
        self.excavation_stages_check.configure(state=tk.DISABLED)
        self._set_status("Creating clean PRI preview for excavation top point...")
        self._append_log(f"Creating temporary clean PRI preview: {input_path.name}")
        transfer_water = self.transfer_water.get()

        def build_preview() -> None:
            preview_path = self._preview_path_for(input_path)
            try:
                # Deliberately no bolt removal, geometry cleanup or stages:
                # coordinates displayed in the preview are the coordinates
                # that the user will subsequently enter.
                preview_path.parent.mkdir(parents=True, exist_ok=True)
                convert(
                    input_path,
                    preview_path,
                    include_water_table=transfer_water,
                )
            except Exception as exc:
                self.result_queue.put(("excavation_preview_failed", str(exc)))
            else:
                self.result_queue.put(("excavation_preview", ExcavationPreview(input_path, preview_path)))

        threading.Thread(target=build_preview, daemon=True).start()
        self.after(100, self._poll_results)

    def _finish_excavation_preview(self, preview: ExcavationPreview) -> None:
        self._preview_in_progress = False
        self.excavation_stages_check.configure(state=tk.NORMAL)
        try:
            if PROROCK_EXE.is_file():
                subprocess.Popen([str(PROROCK_EXE), str(preview.preview_path)])
            else:
                # Keep the workflow usable on PCs where ProRock was installed
                # elsewhere or is not yet installed.
                os.startfile(preview.preview_path)  # type: ignore[attr-defined]
        except OSError as exc:
            self.add_excavation_stages.set(False)
            messagebox.showerror(APP_NAME, f"Could not open the temporary PRI preview:\n{exc}")
            return
        messagebox.showinfo(
            APP_NAME,
            "A clean temporary PRI was opened in ProRock.\n\n"
            "Find the excavation top vertex, note its X/Y coordinates, close the preview, then click OK.",
        )
        values = self._ask_excavation_settings()
        if values is None:
            self.add_excavation_stages.set(False)
            return
        count, top_x, top_y = values
        self.excavation_stage_count.set(count)
        self.excavation_top_x.set(top_x)
        self.excavation_top_y.set(top_y)
        self.excavation_top_point = (top_x, top_y)
        self._append_log(
            f"Excavation stages configured from preview: {count}, top X={top_x:g}, Y={top_y:g}"
        )
        self._set_status("Excavation stages configured")

    def _ask_excavation_settings(self) -> tuple[int, float, float] | None:
        """Show stage count and top-point coordinates in one small table."""
        dialog = tk.Toplevel(self)
        dialog.title("Excavation stages")
        dialog.transient(self)
        dialog.resizable(False, False)
        dialog.grab_set()

        body = ttk.Frame(dialog, padding=14)
        body.pack(fill=tk.BOTH, expand=True)
        ttk.Label(body, text="Enter values from the temporary ProRock preview.").grid(
            row=0, column=0, columnspan=4, sticky="w", pady=(0, 10)
        )
        stage_value = tk.StringVar(value=str(self.excavation_stage_count.get()))
        x_value = tk.StringVar(value=f"{self.excavation_top_x.get():g}")
        y_value = tk.StringVar(value=f"{self.excavation_top_y.get():g}")
        ttk.Label(body, text="Stages").grid(row=1, column=0, sticky="w")
        stage_entry = ttk.Entry(body, width=8, textvariable=stage_value)
        stage_entry.grid(row=1, column=1, sticky="w", padx=(8, 20))
        ttk.Label(body, text="Top point").grid(row=2, column=0, sticky="w", pady=(10, 0))
        ttk.Label(body, text="X").grid(row=2, column=1, sticky="e", padx=(8, 4), pady=(10, 0))
        x_entry = ttk.Entry(body, width=14, textvariable=x_value)
        x_entry.grid(row=2, column=2, sticky="w", pady=(10, 0))
        ttk.Label(body, text="Y").grid(row=2, column=3, sticky="e", padx=(14, 4), pady=(10, 0))
        y_entry = ttk.Entry(body, width=14, textvariable=y_value)
        y_entry.grid(row=2, column=4, sticky="w", pady=(10, 0))

        result: list[tuple[int, float, float] | None] = [None]

        def accept() -> None:
            try:
                count = int(stage_value.get())
                top_x = float(x_value.get().replace(",", "."))
                top_y = float(y_value.get().replace(",", "."))
                if not 1 <= count <= 100:
                    raise ValueError
            except ValueError:
                messagebox.showwarning(APP_NAME, "Stages must be 1 to 100; X and Y must be numbers.", parent=dialog)
                return
            result[0] = (count, top_x, top_y)
            dialog.destroy()

        buttons = ttk.Frame(body)
        buttons.grid(row=3, column=0, columnspan=5, sticky="e", pady=(14, 0))
        ttk.Button(buttons, text="Cancel", command=dialog.destroy).grid(row=0, column=0, padx=(0, 6))
        ttk.Button(buttons, text="OK", command=accept).grid(row=0, column=1)
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        dialog.bind("<Return>", lambda _: accept())
        dialog.bind("<Escape>", lambda _: dialog.destroy())
        stage_entry.focus_set()
        self.wait_window(dialog)
        return result[0]

    def remove_selected(self) -> None:
        selected = set(self.tree.selection())
        if not selected:
            return

        self.files = [path for index, path in enumerate(self.files) if str(index) not in selected]
        self.refresh_file_list()
        self._set_status(f"{len(self.files)} file(s) loaded")

    def clear_files(self) -> None:
        self.files.clear()
        self.refresh_file_list()
        self._set_status("File list cleared")

    def choose_output_folder(self) -> None:
        current = Path(self.output_folder.get()).expanduser()
        selected = filedialog.askdirectory(
            title="Choose output folder",
            initialdir=current if current.is_dir() else None,
        )
        if selected:
            self.output_folder.set(selected)

    def _load_output_folder(self) -> str:
        try:
            settings = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return ""
        value = settings.get("output_folder")
        return value if isinstance(value, str) else ""

    def _save_output_folder(self, *_: object) -> None:
        try:
            SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
            SETTINGS_PATH.write_text(
                json.dumps({"output_folder": self.output_folder.get().strip()}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            # A read-only profile must not stop conversion; the selected path
            # remains available for this session.
            pass

    def _on_close(self) -> None:
        self.destroy()

    def refresh_file_list(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for index, path in enumerate(self.files):
            self.tree.insert("", "end", iid=str(index), values=(path.name, str(path.parent), "Queued"))

    def start_conversion(self) -> None:
        if self.is_converting:
            return
        if not self.files:
            messagebox.showwarning(APP_NAME, "Add one or more .slim files first.")
            return
        if not self.output_folder.get().strip():
            messagebox.showwarning(APP_NAME, "Choose an output folder first.")
            return
        if self.add_excavation_stages.get() and self.excavation_top_point is None:
            messagebox.showwarning(
                APP_NAME,
                "Enable Add excavation stages and finish selecting the top point in the temporary PRI preview first.",
            )
            return

        output_root = Path(self.output_folder.get()).expanduser()
        jobs = self._build_jobs(output_root)
        if not jobs:
            messagebox.showwarning(APP_NAME, "No conversion jobs were created.")
            return
        if self.fix_geometry.get():
            try:
                min_distance, min_angle = self._geometry_thresholds()
            except ValueError as exc:
                messagebox.showwarning(APP_NAME, str(exc))
                return
        else:
            min_distance, min_angle = DEFAULT_MIN_DISTANCE, DEFAULT_MIN_ANGLE
        try:
            workers = self._worker_limit()
        except ValueError as exc:
            messagebox.showwarning(APP_NAME, str(exc))
            return
        workers = min(workers, len(jobs))

        self.is_converting = True
        self._set_controls_enabled(False)
        self.progress.configure(maximum=len(jobs), value=0)
        self.refresh_file_list()
        self._clear_log()
        self._append_log(f"Starting conversion: {len(jobs)} file(s), {workers} parallel worker(s)")
        self._set_status(f"Converting 0 of {len(jobs)}...")

        worker = threading.Thread(
            target=self._run_conversion,
            args=(
                jobs,
                self.transfer_water.get(),
                self.fix_geometry.get(),
                self.add_excavation_stages.get(),
                self.excavation_stage_count.get(),
                self.excavation_top_point,
                self.remove_bolts.get(),
                min_distance,
                min_angle,
                workers,
            ),
            daemon=True,
        )
        worker.start()
        self.after(100, self._poll_results)

    def _build_jobs(self, output_root: Path) -> list[ConversionJob]:
        output_targets = [output_root / file_path.with_suffix(".pri").name for file_path in self.files]
        duplicate_targets = len({target.name.lower() for target in output_targets}) != len(output_targets)
        base = common_parent(self.files) if duplicate_targets else None

        jobs: list[ConversionJob] = []
        for input_path in self.files:
            if base is not None:
                relative = input_path.resolve().relative_to(base).with_suffix(".pri")
                output_path = output_root / relative
            else:
                output_path = output_root / input_path.with_suffix(".pri").name
            jobs.append(ConversionJob(input_path=input_path, output_path=output_path))
        return jobs

    def _run_conversion(
        self,
        jobs: list[ConversionJob],
        transfer_water: bool,
        fix_geometry: bool,
        add_excavation_stages: bool,
        excavation_stage_count: int,
        excavation_top_point: tuple[float, float] | None,
        remove_bolts: bool,
        min_distance: float,
        min_angle: float,
        workers: int,
    ) -> None:
        completed = 0
        failed: list[str] = []
        warnings: list[str] = []
        backups: list[str] = []
        for index, job in enumerate(jobs):
            self.result_queue.put(("status", (index, "Queued")))
            self.result_queue.put(("log", f"[{index + 1}/{len(jobs)}] {job.input_path.name} -> {job.output_path}"))

        with ProcessPoolExecutor(max_workers=workers) as executor:
            # A manager queue is picklable on Windows, unlike a raw
            # multiprocessing.Queue. It carries live cleanup progress from
            # the worker processes back to this GUI thread.
            with Manager() as manager:
                worker_progress = manager.Queue()
                futures = {
                    executor.submit(
                        convert_job,
                        job,
                        index,
                        transfer_water,
                        fix_geometry,
                        add_excavation_stages,
                        excavation_stage_count,
                        excavation_top_point,
                        remove_bolts,
                        min_distance,
                        min_angle,
                        worker_progress,
                    ): (index, job)
                    for index, job in enumerate(jobs)
                }
                while futures:
                    done, _ = wait(futures, timeout=0.2, return_when=FIRST_COMPLETED)
                    self._forward_worker_progress(worker_progress, jobs)
                    for future in done:
                        index, job = futures.pop(future)
                        try:
                            summary = future.result()
                        except (ConversionError, OSError) as exc:
                            failed.append(f"{job.input_path.name}: {exc}")
                            self.result_queue.put(("log", f"[{index + 1}/{len(jobs)}] FAILED: {exc}"))
                            self.result_queue.put(("status", (index, "Failed")))
                        except Exception as exc:
                            failed.append(f"{job.input_path.name}: {type(exc).__name__}: {exc}")
                            self.result_queue.put(("log", f"[{index + 1}/{len(jobs)}] FAILED: {type(exc).__name__}: {exc}"))
                            self.result_queue.put(("status", (index, "Failed")))
                        else:
                            completed += 1
                            if summary.geometry_fix is not None and summary.geometry_fix.warnings:
                                for warning in summary.geometry_fix.warnings:
                                    warnings.append(f"{job.input_path.name}: {warning}")
                            if summary.geometry_fix is not None and summary.geometry_fix.backup_path:
                                backups.append(f"{job.output_path.name}: {summary.geometry_fix.backup_path}")
                            self.result_queue.put(("status", (index, self._summary_text(summary))))
                            self.result_queue.put(("log", f"[{index + 1}/{len(jobs)}] done: {self._summary_text(summary)}"))
                        self.result_queue.put(("progress", completed + len(failed)))
                self._forward_worker_progress(worker_progress, jobs)
        self.result_queue.put(("done", (completed, failed, warnings, backups)))

    def _forward_worker_progress(self, worker_progress: object, jobs: list[ConversionJob]) -> None:
        """Relay queued worker stages without blocking the conversion loop."""
        while True:
            try:
                index, message = worker_progress.get_nowait()
            except queue.Empty:
                return
            self.result_queue.put(("log", f"[{index + 1}/{len(jobs)}] {jobs[index].input_path.name}: {message}"))
            self.result_queue.put(("status", (index, self._stage_status(message))))

    @staticmethod
    def _stage_status(message: str) -> str:
        lowered = message.lower()
        if "bolt cleanup" in lowered or "bolt and anchor" in lowered:
            return "Removing bolts"
        if "excavation" in lowered or "pit bench" in lowered or "four-bench" in lowered:
            return "Building excavation stages"
        if "cleanup" in lowered or "geometry spread" in lowered or "close-pair" in lowered:
            return "Cleaning geometry"
        if "writing pri" in lowered:
            return "Writing PRI"
        if "validating" in lowered:
            return "Validating"
        if "parsing" in lowered or "building" in lowered:
            return "Building geometry"
        if "reading" in lowered:
            return "Reading"
        return "Converting"

    def _poll_results(self) -> None:
        try:
            while True:
                kind, payload = self.result_queue.get_nowait()
                if kind == "status":
                    index, status = payload
                    self._set_row_status(index, status)
                elif kind == "log":
                    self._append_log(str(payload))
                elif kind == "progress":
                    done_count = payload
                    total = int(float(self.progress.cget("maximum")))
                    self.progress.configure(value=done_count)
                    self._set_status(f"Converting {done_count} of {total}...")
                elif kind == "excavation_preview":
                    self._finish_excavation_preview(payload)
                elif kind == "excavation_preview_failed":
                    self._preview_in_progress = False
                    self.excavation_stages_check.configure(state=tk.NORMAL)
                    self.add_excavation_stages.set(False)
                    self._set_status("Could not create excavation preview")
                    messagebox.showerror(APP_NAME, f"Could not create the temporary PRI preview:\n{payload}")
                elif kind == "done":
                    completed, failed, warnings, backups = payload
                    self._finish_conversion(completed, failed, warnings, backups)
                    return
        except queue.Empty:
            pass

        if self.is_converting or self._preview_in_progress:
            self.after(100, self._poll_results)

    def _finish_conversion(
        self,
        completed: int,
        failed: list[str],
        warnings: list[str],
        backups: list[str],
    ) -> None:
        self.is_converting = False
        self._set_controls_enabled(True)
        if failed:
            self._set_status(f"Finished with {completed} converted, {len(failed)} failed")
            messagebox.showerror(APP_NAME, "Some files failed:\n\n" + "\n".join(failed[:12]))
        elif warnings:
            self._set_status(f"Finished: {completed} file(s) converted with geometry warnings")
            messagebox.showwarning(
                APP_NAME,
                f"Converted {completed} file(s), but geometry cleanup left warnings:\n\n"
                + "\n".join(warnings[:12])
                + self._backup_message_suffix(backups),
            )
        else:
            self._set_status(f"Finished: {completed} file(s) converted")
            messagebox.showinfo(
                APP_NAME,
                f"Converted {completed} file(s)." + self._backup_message_suffix(backups),
            )

    def _backup_message_suffix(self, backups: list[str]) -> str:
        if not backups:
            return ""
        return "\n\nBase PRI copies:\n" + "\n".join(backups[:12])

    def _set_row_status(self, index: int, status: str) -> None:
        iid = str(index)
        if not self.tree.exists(iid):
            return
        values = list(self.tree.item(iid, "values"))
        values[2] = status
        self.tree.item(iid, values=values)

    def _summary_text(self, summary: ValidationSummary) -> str:
        suffix = ""
        if summary.geometry_fix is not None:
            if summary.geometry_fix.warnings:
                suffix = f", geometry {summary.geometry_fix.errors_before}->{summary.geometry_fix.errors_after}, warnings"
            elif summary.geometry_fix.fixed:
                suffix = f", geometry {summary.geometry_fix.errors_before}->{summary.geometry_fix.errors_after}"
            else:
                suffix = ", geometry ok"
        if summary.water_table_vertices:
            return f"Done, {summary.areas} areas, water{suffix}"
        return f"Done, {summary.areas} areas{suffix}"

    def _set_controls_enabled(self, enabled: bool) -> None:
        state = tk.NORMAL if enabled else tk.DISABLED
        for widget in (
            self.add_button,
            self.remove_button,
            self.clear_button,
            self.output_entry,
            self.output_button,
            self.water_check,
            self.fix_geometry_check,
            self.excavation_stages_check,
            self.remove_bolts_check,
            self.convert_button,
        ):
            widget.configure(state=state)
        if enabled:
            self._refresh_geometry_controls()
        else:
            self.min_distance_entry.configure(state=tk.DISABLED)
            self.min_angle_entry.configure(state=tk.DISABLED)
            self.worker_count_entry.configure(state=tk.DISABLED)

    def _refresh_geometry_controls(self) -> None:
        state = tk.NORMAL if self.fix_geometry.get() else tk.DISABLED
        self.min_distance_entry.configure(state=state)
        self.min_angle_entry.configure(state=state)
        self.worker_count_entry.configure(state=tk.NORMAL)

    def _geometry_thresholds(self) -> tuple[float, float]:
        try:
            min_distance = float(self.geometry_min_distance.get().replace(",", "."))
            min_angle = float(self.geometry_min_angle.get().replace(",", "."))
        except ValueError as exc:
            raise ValueError("Geometry cleanup thresholds must be numeric.") from exc
        if min_distance < 0:
            raise ValueError("Minimum distance must be zero or greater.")
        if min_angle < 0 or min_angle >= 180:
            raise ValueError("Minimum angle must be in the range 0..180 degrees.")
        return min_distance, min_angle

    def _worker_limit(self) -> int:
        try:
            workers = int(self.worker_count.get())
        except ValueError as exc:
            raise ValueError("Parallel files must be a whole number.") from exc
        if workers < 1:
            raise ValueError("Parallel files must be at least 1.")
        return workers

    def _set_status(self, text: str) -> None:
        self.status_label.configure(text=text)

    def _clear_log(self) -> None:
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _append_log(self, text: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, f"{timestamp}  {text}\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Open the Seque SLIM to PRI conversion window.")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {APP_VERSION}")
    parser.parse_args(argv)

    app = SequeApp()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
