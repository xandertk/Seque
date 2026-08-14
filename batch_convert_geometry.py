#!/usr/bin/env python3
"""Batch-convert Slide cuts to PRI with geometry cleanup statistics."""

from __future__ import annotations

import argparse
import csv
import os
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Sequence

from slim_to_pri import ConversionError, convert


def convert_file(
    input_path: Path,
    output_path: Path,
    min_distance: float,
    min_angle: float,
):
    """Convert one file; safe to execute in a process-pool worker."""
    return convert(
        input_path,
        output_path,
        include_water_table=True,
        fix_geometry=True,
        geometry_min_distance=min_distance,
        geometry_min_angle=min_angle,
    )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Convert all .slim/.sli files from an input folder to PRI and collect geometry cleanup stats.",
    )
    parser.add_argument("input_dir", type=Path, nargs="?", default=Path("Input"))
    parser.add_argument("output_dir", type=Path, nargs="?", default=Path("Output"))
    parser.add_argument("--min-distance", type=float, default=2.0)
    parser.add_argument("--min-angle", type=float, default=18.0)
    parser.add_argument(
        "--workers",
        type=int,
        default=min(4, os.cpu_count() or 1),
        help="Number of files to convert in parallel (default: up to 4).",
    )
    parser.add_argument("--stats", type=Path, default=None, help="Optional CSV statistics path.")
    return parser


def markdown_row(values: Sequence[object]) -> str:
    return "| " + " | ".join(str(value) for value in values) + " |"


def main(argv: Sequence[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    input_dir = args.input_dir
    output_dir = args.output_dir
    stats_path = args.stats or output_dir / "geometry_cleanup_stats.csv"

    files = sorted(
        [path for path in input_dir.iterdir() if path.is_file() and path.suffix.lower() in {".slim", ".sli"}],
        key=lambda path: path.name.lower(),
    )
    if not files:
        print(f"No .slim/.sli files found in {input_dir}", file=sys.stderr)
        return 1
    if args.workers < 1:
        parser.error("--workers must be at least 1")

    output_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, object]] = []

    jobs = [(input_path, output_dir / f"{input_path.stem}.pri") for input_path in files]
    print(f"Converting {len(jobs)} file(s) with {min(args.workers, len(jobs))} worker(s)", flush=True)
    with ProcessPoolExecutor(max_workers=min(args.workers, len(jobs))) as executor:
        futures = [
            executor.submit(convert_file, input_path, output_path, args.min_distance, args.min_angle)
            for input_path, output_path in jobs
        ]
        for index, ((input_path, output_path), future) in enumerate(zip(jobs, futures), start=1):
            print(f"[{index}/{len(files)}] {input_path.name} -> {output_path}", flush=True)
            try:
                summary = future.result()
            except Exception as exc:
                rows.append(
                    {
                        "file": input_path.name,
                        "status": "failed",
                        "errors_before": "",
                        "errors_after": "",
                        "areas": "",
                        "faces_before": "",
                        "faces_after": "",
                        "vertices_before": "",
                        "vertices_after": "",
                        "warnings": str(exc),
                        "output": str(output_path),
                        "base_copy": "",
                    }
                )
                print(f"  FAILED: {exc}", flush=True)
                continue

            fix = summary.geometry_fix
            if fix is None:
                rows.append(
                    {
                        "file": input_path.name,
                        "status": "no-cleanup",
                        "errors_before": "",
                        "errors_after": "",
                        "areas": summary.areas,
                        "faces_before": "",
                        "faces_after": "",
                        "vertices_before": "",
                        "vertices_after": "",
                        "warnings": "",
                        "output": str(output_path),
                        "base_copy": "",
                    }
                )
                print("  done without cleanup summary", flush=True)
                continue

            rows.append(
                {
                    "file": input_path.name,
                    "status": "ok" if not fix.warnings else "warning",
                    "errors_before": fix.errors_before,
                    "errors_after": fix.errors_after,
                    "areas": summary.areas,
                    "faces_before": fix.faces_before if fix.faces_before is not None else "",
                    "faces_after": fix.faces_after if fix.faces_after is not None else "",
                    "vertices_before": fix.vertices_before,
                    "vertices_after": fix.vertices_after,
                    "warnings": "; ".join(fix.warnings),
                    "output": str(output_path),
                    "base_copy": fix.backup_path or "",
                }
            )
            print(
                f"  errors {fix.errors_before}->{fix.errors_after}, "
                f"vertices {fix.vertices_before}->{fix.vertices_after}, "
                f"faces {fix.faces_before}->{fix.faces_after}",
                flush=True,
            )

    fieldnames = [
        "file",
        "status",
        "errors_before",
        "errors_after",
        "areas",
        "faces_before",
        "faces_after",
        "vertices_before",
        "vertices_after",
        "warnings",
        "output",
        "base_copy",
    ]
    with stats_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print()
    print(f"CSV: {stats_path}")
    print()
    print(markdown_row(["file", "status", "errors before", "errors after", "faces", "vertices"]))
    print(markdown_row(["---", "---", "---:", "---:", "---", "---"]))
    for row in rows:
        faces = f"{row['faces_before']} -> {row['faces_after']}" if row["faces_before"] != "" else ""
        vertices = f"{row['vertices_before']} -> {row['vertices_after']}" if row["vertices_before"] != "" else ""
        print(
            markdown_row(
                [
                    row["file"],
                    row["status"],
                    row["errors_before"],
                    row["errors_after"],
                    faces,
                    vertices,
                ]
            )
        )

    return 0 if all(row["status"] in {"ok", "warning"} for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
