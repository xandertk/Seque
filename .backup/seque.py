#!/usr/bin/env python3
"""Seque - a small desktop front-end for SLIM to PRI conversion."""

from __future__ import annotations

import argparse
import queue
import threading
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Iterable, Sequence

from slim_to_pri import ConversionError, ValidationSummary, convert


APP_NAME = "Seque"


@dataclass
class ConversionJob:
    input_path: Path
    output_path: Path


def common_parent(paths: Iterable[Path]) -> Path | None:
    resolved = [path.resolve() for path in paths]
    if not resolved:
        return None

    common = resolved[0].parent
    for path in resolved[1:]:
        while common != common.parent and common not in path.parents:
            common = common.parent
    return common


class SequeApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_NAME)
        self.minsize(860, 560)

        self.files: list[Path] = []
        self.output_folder = tk.StringVar()
        self.transfer_water = tk.BooleanVar(value=True)
        self.is_converting = False
        self.result_queue: queue.Queue[tuple[str, object]] = queue.Queue()

        self._build_ui()
        self._set_status("Ready")

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=14)
        root.pack(fill=tk.BOTH, expand=True)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(1, weight=1)

        header = ttk.Frame(root)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        header.columnconfigure(0, weight=1)

        title = ttk.Label(header, text=APP_NAME, font=("Segoe UI", 20, "bold"))
        title.grid(row=0, column=0, sticky="w")

        actions = ttk.Frame(header)
        actions.grid(row=0, column=1, sticky="e")
        self.add_button = ttk.Button(actions, text="Add .slim files", command=self.add_files)
        self.add_button.grid(row=0, column=0, padx=(0, 6))
        self.remove_button = ttk.Button(actions, text="Remove Selected", command=self.remove_selected)
        self.remove_button.grid(row=0, column=1, padx=(0, 6))
        self.clear_button = ttk.Button(actions, text="Clear List", command=self.clear_files)
        self.clear_button.grid(row=0, column=2)

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
        self.tree.column("status", width=110, minwidth=90, anchor="center")
        self.tree.grid(row=0, column=0, sticky="nsew")

        scroll_y = ttk.Scrollbar(list_frame, orient="vertical", command=self.tree.yview)
        scroll_y.grid(row=0, column=1, sticky="ns")
        scroll_x = ttk.Scrollbar(list_frame, orient="horizontal", command=self.tree.xview)
        scroll_x.grid(row=1, column=0, sticky="ew")
        self.tree.configure(yscrollcommand=scroll_y.set, xscrollcommand=scroll_x.set)

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

        footer = ttk.Frame(root)
        footer.grid(row=4, column=0, sticky="ew", pady=(12, 0))
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

        known = {path.resolve() for path in self.files}
        for value in selected:
            path = Path(value)
            if path.resolve() in known:
                continue
            self.files.append(path)
            known.add(path.resolve())
        self.refresh_file_list()
        self._set_status(f"{len(self.files)} file(s) loaded")

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
        selected = filedialog.askdirectory(title="Choose output folder")
        if selected:
            self.output_folder.set(selected)

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

        output_root = Path(self.output_folder.get()).expanduser()
        jobs = self._build_jobs(output_root)
        if not jobs:
            messagebox.showwarning(APP_NAME, "No conversion jobs were created.")
            return

        self.is_converting = True
        self._set_controls_enabled(False)
        self.progress.configure(maximum=len(jobs), value=0)
        self.refresh_file_list()
        self._set_status(f"Converting 0 of {len(jobs)}...")

        worker = threading.Thread(
            target=self._run_conversion,
            args=(jobs, self.transfer_water.get()),
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

    def _run_conversion(self, jobs: list[ConversionJob], transfer_water: bool) -> None:
        completed = 0
        failed: list[str] = []
        for index, job in enumerate(jobs):
            self.result_queue.put(("status", (index, "Converting")))
            try:
                job.output_path.parent.mkdir(parents=True, exist_ok=True)
                summary = convert(
                    job.input_path,
                    job.output_path,
                    include_water_table=transfer_water,
                )
            except (ConversionError, OSError) as exc:
                failed.append(f"{job.input_path.name}: {exc}")
                self.result_queue.put(("status", (index, "Failed")))
            except Exception as exc:
                failed.append(f"{job.input_path.name}: {type(exc).__name__}: {exc}")
                self.result_queue.put(("status", (index, "Failed")))
            else:
                completed += 1
                self.result_queue.put(("status", (index, self._summary_text(summary))))
            self.result_queue.put(("progress", completed + len(failed)))
        self.result_queue.put(("done", (completed, failed)))

    def _poll_results(self) -> None:
        try:
            while True:
                kind, payload = self.result_queue.get_nowait()
                if kind == "status":
                    index, status = payload
                    self._set_row_status(index, status)
                elif kind == "progress":
                    done_count = payload
                    total = int(float(self.progress.cget("maximum")))
                    self.progress.configure(value=done_count)
                    self._set_status(f"Converting {done_count} of {total}...")
                elif kind == "done":
                    completed, failed = payload
                    self._finish_conversion(completed, failed)
                    return
        except queue.Empty:
            pass

        if self.is_converting:
            self.after(100, self._poll_results)

    def _finish_conversion(self, completed: int, failed: list[str]) -> None:
        self.is_converting = False
        self._set_controls_enabled(True)
        if failed:
            self._set_status(f"Finished with {completed} converted, {len(failed)} failed")
            messagebox.showerror(APP_NAME, "Some files failed:\n\n" + "\n".join(failed[:12]))
        else:
            self._set_status(f"Finished: {completed} file(s) converted")
            messagebox.showinfo(APP_NAME, f"Converted {completed} file(s).")

    def _set_row_status(self, index: int, status: str) -> None:
        iid = str(index)
        if not self.tree.exists(iid):
            return
        values = list(self.tree.item(iid, "values"))
        values[2] = status
        self.tree.item(iid, values=values)

    def _summary_text(self, summary: ValidationSummary) -> str:
        if summary.water_table_vertices:
            return f"Done, {summary.areas} areas, water"
        return f"Done, {summary.areas} areas"

    def _set_controls_enabled(self, enabled: bool) -> None:
        state = tk.NORMAL if enabled else tk.DISABLED
        for widget in (
            self.add_button,
            self.remove_button,
            self.clear_button,
            self.output_entry,
            self.output_button,
            self.water_check,
            self.convert_button,
        ):
            widget.configure(state=state)

    def _set_status(self, text: str) -> None:
        self.status_label.configure(text=text)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Open the Seque SLIM to PRI conversion window.")
    parser.parse_args(argv)

    app = SequeApp()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
