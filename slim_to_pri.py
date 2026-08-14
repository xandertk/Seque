#!/usr/bin/env python3
"""Seque core converter: Rocscience Slide SLIM/SLI -> ProRock PRI.

This module is intentionally self-contained and uses only Python's standard
library. It can be used directly as a CLI tool or imported by the Tkinter desktop
front-end in ``seque.py``.

Conversion scope
----------------
Transferred:
* clean geometry as PRI vertices, borders and areas;
* external boundary;
* material interfaces as ``MaterialBorders``;
* Slide water table as a ``WaterLevel`` border;
* Slide material names and RGB colours;
* connected regions of equal material as PRI areas;
* optional triangular mesh when ``--keep-mesh`` is used.

Not transferred automatically:
* Slide Mohr-Coulomb c/phi/unit-weight values as ProRock FDEM parameters;
* material polygons that represent fractures as zero-thickness fault lines.

Pipeline map for developers
---------------------------
``read_sli_from_slim`` extracts the SLI text from a SLIM archive.
``parse_slide_model`` builds the neutral in-memory model.
``build_pri_mesh`` reconstructs geometry, material regions and water borders.
``write_pri`` serializes the ProRock PRI 2.2.0 file.
``convert`` is the public API used by both the CLI and Seque GUI.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import sys
import zipfile
from collections import defaultdict, deque
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Sequence, Tuple

from fix_pri_geometry import (
    DEFAULT_MIN_ANGLE as DEFAULT_GEOMETRY_MIN_ANGLE,
    DEFAULT_MIN_DISTANCE as DEFAULT_GEOMETRY_MIN_DISTANCE,
    GeometryFixError,
    GeometryFixSummary,
    add_excavation_stages as add_excavation_stage_geometry,
    ProgressCallback,
    clean_pri_geometry,
    remove_bolt_geometry,
)


PRI_VERSION = "2.2.0"
DEFAULT_MAX_TIME_STEP = 2_000_000
DEFAULT_TIME_STEP_SIZE = "1E-06"
DEFAULT_OUTPUT_FREQUENCY = 2_000
DEFAULT_DYNAMIC_STABILIZATION = 0
DEFAULT_WATER_UNIT_WEIGHT = 9810
DEFAULT_RGB = (230, 230, 250)
DEFAULT_JOINT_RGB = (255, 165, 0)
__version__ = "0.1.0"


class ConversionError(RuntimeError):
    pass


# Neutral data structures used between the Slide parser and PRI writer.
@dataclass(frozen=True)
class Material:
    soil_id: str
    name: str
    red: int
    green: int
    blue: int
    slide_c: float | None = None
    slide_phi: float | None = None
    slide_unit_weight: float | None = None


@dataclass(frozen=True)
class Cell:
    source_id: int
    vertices: Tuple[int, int, int]  # source Slide vertex ids
    material: str


@dataclass
class SlideModel:
    vertices: Dict[int, Tuple[float, float]]
    cells: List[Cell]
    materials: List[Material]
    exterior: List[int]
    material_lines: List[List[int]]
    water_table: List[int]


@dataclass
class PriMesh:
    node_source_ids: List[int]
    nodes: List[Tuple[float, float]]
    elements: List[Tuple[int, int, int]]
    element_materials: List[str]
    neighbours: List[Tuple[int, int, int]]
    boundary_flags: List[int]
    mesh_sets: List[int]
    components: List[List[int]]  # element indices per area
    component_material: List[str]
    component_target_area: List[float]
    area_component_ids: List[int]
    geometry_source_ids: List[int]
    geometry_vertices: List[Tuple[float, float]]
    exterior_geometry_ids: List[int]
    material_border_paths: List[List[int]]
    water_table_geometry_ids: List[int]


@dataclass
class ValidationSummary:
    nodes: int
    elements: int
    areas: int
    borders: int
    source_materials: int
    used_materials: int
    boundary_edges: int
    internal_edges: int
    water_table_vertices: int
    geometry_fix: GeometryFixSummary | None = None


SECTION_RE_TEMPLATE = r"(?ms)^%s:\s*\n(.*?)(?=^[^\s#][^\n]*:\s*(?:\n|$)|\Z)"


# SLI parsing helpers.
def section(text: str, heading: str) -> str:
    """Return a top-level SLI section body."""
    pattern = SECTION_RE_TEMPLATE % re.escape(heading)
    m = re.search(pattern, text)
    return m.group(1) if m else ""


def clean_name(value: str) -> str:
    value = value.replace("\r", " ").replace("\n", " ").replace("\t", " ").strip()
    return re.sub(r"\s+", " ", value) or "Unnamed material"


def read_sli_from_slim(path: Path) -> Tuple[str, str]:
    if not path.exists():
        raise ConversionError(f"Input file does not exist: {path}")

    if path.suffix.lower() == ".sli":
        try:
            return path.read_text(encoding="utf-8", errors="replace"), path.name
        except OSError as exc:
            raise ConversionError(f"Cannot read {path}: {exc}") from exc

    if path.suffix.lower() != ".slim":
        raise ConversionError("Input must be a Slide .slim archive or an unpacked .sli file.")

    try:
        with zipfile.ZipFile(path, "r") as zf:
            sli_members = [n for n in zf.namelist() if n.lower().endswith(".sli")]
            if not sli_members:
                raise ConversionError(f"No .sli member found inside {path.name}")

            preferred = f"{path.stem}.sli".lower()
            member = next((n for n in sli_members if Path(n).name.lower() == preferred), sli_members[0])
            raw = zf.read(member)
    except zipfile.BadZipFile as exc:
        raise ConversionError(f"{path} is not a valid SLIM/ZIP archive") from exc
    except OSError as exc:
        raise ConversionError(f"Cannot open {path}: {exc}") from exc

    # Slide 6 files encountered in practice are plain ASCII/UTF-8-compatible text.
    return raw.decode("utf-8", errors="replace"), member


def parse_vertices(text: str) -> Dict[int, Tuple[float, float]]:
    body = section(text, "vertices")
    vertices: Dict[int, Tuple[float, float]] = {}
    rx = re.compile(
        r"^\s*(\d+)\s+x:\s*([-+0-9.eE]+)\s+y:\s*([-+0-9.eE]+)\s*$",
        re.MULTILINE,
    )
    for m in rx.finditer(body):
        vertex_id = int(m.group(1))
        vertices[vertex_id] = (float(m.group(2)), float(m.group(3)))

    if not vertices:
        raise ConversionError("No Slide vertices were found in the .sli file.")
    return vertices


def parse_cells(text: str) -> List[Cell]:
    body = section(text, "cells")
    cells: List[Cell] = []
    rx = re.compile(
        r"^\s*(\d+)\s+vertices:\s*\[\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\]"
        r"\s+material:\s*(soil\d+)\s*$",
        re.MULTILINE | re.IGNORECASE,
    )
    for m in rx.finditer(body):
        cells.append(
            Cell(
                source_id=int(m.group(1)),
                vertices=(int(m.group(2)), int(m.group(3)), int(m.group(4))),
                material=m.group(5).lower(),
            )
        )

    if not cells:
        raise ConversionError("No triangular Slide cells were found in the .sli file.")
    cells.sort(key=lambda c: c.source_id)
    return cells


def parse_material_types(text: str) -> Dict[str, Tuple[float | None, float | None, float | None]]:
    body = section(text, "material types")
    result: Dict[str, Tuple[float | None, float | None, float | None]] = {}
    for line in body.splitlines():
        m = re.match(r"\s*(soil\d+)\s*=\s*(.*)$", line, flags=re.IGNORECASE)
        if not m:
            continue
        soil = m.group(1).lower()
        tail = m.group(2)

        def get_float(key: str) -> float | None:
            fm = re.search(rf"(?:^|\s){re.escape(key)}:\s*([-+0-9.eE]+)", tail, flags=re.IGNORECASE)
            return float(fm.group(1)) if fm else None

        result[soil] = (get_float("c"), get_float("phi"), get_float("uw"))
    return result


def parse_material_names_and_colours(text: str) -> List[Tuple[str, int, int, int]]:
    body = section(text, "material properties")
    result: List[Tuple[str, int, int, int]] = []
    rx = re.compile(
        r"^\s*(.*?)\s+red:\s*(\d+)\s+green:\s*(\d+)\s+blue:\s*(\d+)(?:\s+hatch:.*)?$",
        re.MULTILINE,
    )
    for m in rx.finditer(body):
        name = clean_name(m.group(1))
        # Stop at support/anchor colours; Slide material properties are listed first.
        if re.match(r"(?i)^(Support|Anchor)\s+\d+", name):
            break
        result.append((name, int(m.group(2)), int(m.group(3)), int(m.group(4))))
    return result


def parse_vertex_list_section(text: str, heading: str) -> List[int]:
    body = section(text, heading)
    m = re.search(r"vertices:\s*\[([^\]]*)\]", body, flags=re.IGNORECASE | re.DOTALL)
    if not m:
        return []
    return [int(x) for x in re.findall(r"\d+", m.group(1))]


def parse_geometry_info_paths(
    text: str,
    vertices: Mapping[int, Tuple[float, float]],
    geometry_type: int,
) -> List[List[int]]:
    body = section(text, "geometry info")
    if not body:
        return []

    coord_to_vertex: Dict[Tuple[float, float], int] = {}
    for vertex_id, (x, y) in vertices.items():
        coord_to_vertex[(round(x, 9), round(y, 9))] = vertex_id

    paths: List[List[int]] = []
    current: List[int] = []
    rx = re.compile(
        r"^\s*([0-4])\s+([01])\s+([-+0-9.eE]+),([-+0-9.eE]+)\b",
        re.MULTILINE,
    )
    for m in rx.finditer(body):
        row_type = int(m.group(1))
        if row_type != geometry_type:
            continue

        end_flag = int(m.group(2))
        x = float(m.group(3))
        y = float(m.group(4))
        vertex_id = coord_to_vertex.get((round(x, 9), round(y, 9)))
        if vertex_id is None:
            raise ConversionError(
                "Geometry info references a coordinate that is missing from the vertices section: "
                f"{fmt_number(x)}, {fmt_number(y)}"
            )
        if not current or current[-1] != vertex_id:
            current.append(vertex_id)
        if end_flag:
            if len(current) >= 2:
                paths.append(current)
            current = []

    if current:
        if len(current) >= 2:
            paths.append(current)
    return paths


def build_materials(text: str, cells: Sequence[Cell]) -> List[Material]:
    types = parse_material_types(text)
    named = parse_material_names_and_colours(text)

    indices = [int(m.group(1)) for soil in set(c.material for c in cells) if (m := re.fullmatch(r"soil(\d+)", soil))]
    declared = [int(m.group(1)) for soil in types if (m := re.fullmatch(r"soil(\d+)", soil))]
    n = max(indices + declared + [len(named), 1])

    materials: List[Material] = []
    for i in range(1, n + 1):
        soil = f"soil{i}"
        if i <= len(named):
            name, r, g, b = named[i - 1]
        else:
            name, (r, g, b) = soil, DEFAULT_RGB
        c, phi, uw = types.get(soil, (None, None, None))
        materials.append(Material(soil, name, r, g, b, c, phi, uw))
    return materials


def parse_slide_model(text: str) -> SlideModel:
    vertices = parse_vertices(text)
    cells = parse_cells(text)
    materials = build_materials(text, cells)
    exterior = parse_vertex_list_section(text, "exterior")
    material_lines = parse_geometry_info_paths(text, vertices, geometry_type=3)
    water_table = parse_vertex_list_section(text, "water table")

    missing_vertices = sorted({v for c in cells for v in c.vertices if v not in vertices})
    if missing_vertices:
        raise ConversionError(f"Cells reference missing Slide vertices: {missing_vertices[:20]}")

    if not exterior:
        raise ConversionError("The Slide model has no external boundary in the 'exterior' section.")
    missing_exterior = [v for v in exterior if v not in vertices]
    if missing_exterior:
        raise ConversionError(f"External boundary references missing vertices: {missing_exterior[:20]}")

    missing_material_line_vertices = sorted({v for path in material_lines for v in path if v not in vertices})
    if missing_material_line_vertices:
        raise ConversionError(
            f"Material geometry references missing vertices: {missing_material_line_vertices[:20]}"
        )

    missing_water_table_vertices = [v for v in water_table if v not in vertices]
    if missing_water_table_vertices:
        raise ConversionError(f"Water table references missing vertices: {missing_water_table_vertices[:20]}")

    return SlideModel(vertices, cells, materials, exterior, material_lines, water_table)


def signed_double_area(a: Tuple[float, float], b: Tuple[float, float], c: Tuple[float, float]) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


# Mesh graph and polygon helpers.
def triangle_area(coords: Mapping[int, Tuple[float, float]], vertices: Sequence[int]) -> float:
    a, b, c = (coords[v] for v in vertices)
    return abs(signed_double_area(a, b, c)) * 0.5


def build_edge_to_elements(elements: Sequence[Tuple[int, int, int]]) -> Dict[Tuple[int, int], List[int]]:
    edge_to_elements: Dict[Tuple[int, int], List[int]] = defaultdict(list)
    for i, (a, b, c) in enumerate(elements):
        for u, v in ((a, b), (b, c), (c, a)):
            edge_to_elements[tuple(sorted((u, v)))].append(i)
    return edge_to_elements


def components_by_material(
    elements: Sequence[Tuple[int, int, int]],
    materials: Sequence[str],
    edge_to_elements: Mapping[Tuple[int, int], Sequence[int]],
) -> Tuple[List[List[int]], List[str], List[int]]:
    adjacency: Dict[int, List[int]] = defaultdict(list)
    for owners in edge_to_elements.values():
        if len(owners) != 2:
            continue
        a, b = owners
        if materials[a] == materials[b]:
            adjacency[a].append(b)
            adjacency[b].append(a)

    visited: set[int] = set()
    components: List[List[int]] = []
    component_material: List[str] = []
    element_component = [-1] * len(elements)

    for start in range(len(elements)):
        if start in visited:
            continue
        material = materials[start]
        queue = [start]
        visited.add(start)
        comp: List[int] = []
        while queue:
            cur = queue.pop()
            comp.append(cur)
            for nxt in adjacency.get(cur, []):
                if nxt not in visited:
                    visited.add(nxt)
                    queue.append(nxt)
        comp.sort()
        cid = len(components)
        for e in comp:
            element_component[e] = cid
        components.append(comp)
        component_material.append(material)

    return components, component_material, element_component


def chain_edges(edges: Iterable[Tuple[int, int]]) -> List[List[int]]:
    """Join undirected interface edges into stable polylines/cycles."""
    edge_set = {tuple(sorted(e)) for e in edges}
    if not edge_set:
        return []

    graph: Dict[int, List[int]] = defaultdict(list)
    for a, b in edge_set:
        graph[a].append(b)
        graph[b].append(a)
    for neighbours in graph.values():
        neighbours.sort()

    unused = set(edge_set)
    paths: List[List[int]] = []

    def take_path(start: int, first: int) -> List[int]:
        path = [start, first]
        unused.discard(tuple(sorted((start, first))))
        prev, cur = start, first
        while True:
            candidates = [
                n for n in graph[cur]
                if n != prev and tuple(sorted((cur, n))) in unused
            ]
            if not candidates:
                # For a cycle, allow returning to the start.
                closing = tuple(sorted((cur, start)))
                if cur != start and closing in unused:
                    unused.discard(closing)
                    path.append(start)
                break
            nxt = candidates[0]
            unused.discard(tuple(sorted((cur, nxt))))
            path.append(nxt)
            prev, cur = cur, nxt
            if cur == start:
                break
        return path

    # Open paths / junction-to-junction paths first.
    for start in sorted(graph):
        if len(graph[start]) == 2:
            continue
        for nxt in graph[start]:
            if tuple(sorted((start, nxt))) in unused:
                paths.append(take_path(start, nxt))

    # Remaining edges are cycles or residual segments.
    while unused:
        a, b = min(unused)
        paths.append(take_path(a, b))

    return paths


def polygon_signed_area(coords: Sequence[Tuple[float, float]], vertex_ids: Sequence[int]) -> float:
    total = 0.0
    for a, b in zip(vertex_ids, list(vertex_ids[1:]) + [vertex_ids[0]]):
        x1, y1 = coords[a]
        x2, y2 = coords[b]
        total += x1 * y2 - x2 * y1
    return total * 0.5


def polygon_centroid(coords: Sequence[Tuple[float, float]], vertex_ids: Sequence[int]) -> Tuple[float, float]:
    area = polygon_signed_area(coords, vertex_ids)
    if math.isclose(area, 0.0, abs_tol=1e-12):
        xs = [coords[v][0] for v in vertex_ids]
        ys = [coords[v][1] for v in vertex_ids]
        return sum(xs) / len(xs), sum(ys) / len(ys)

    cx = 0.0
    cy = 0.0
    for a, b in zip(vertex_ids, list(vertex_ids[1:]) + [vertex_ids[0]]):
        x1, y1 = coords[a]
        x2, y2 = coords[b]
        cross = x1 * y2 - x2 * y1
        cx += (x1 + x2) * cross
        cy += (y1 + y2) * cross
    return cx / (6.0 * area), cy / (6.0 * area)


def polygon_envelope_key(
    coords: Sequence[Tuple[float, float]],
    vertex_ids: Sequence[int],
) -> Tuple[float, float, float, float]:
    xs = [coords[v][0] for v in vertex_ids]
    ys = [coords[v][1] for v in vertex_ids]
    return min(xs), min(ys), max(xs), max(ys)


def point_in_polygon_strict(
    point: Tuple[float, float],
    coords: Sequence[Tuple[float, float]],
    vertex_ids: Sequence[int],
) -> bool:
    x, y = point
    inside = False
    eps = 1e-10
    for a, b in zip(vertex_ids, list(vertex_ids[1:]) + [vertex_ids[0]]):
        x1, y1 = coords[a]
        x2, y2 = coords[b]
        cross = (x - x1) * (y2 - y1) - (y - y1) * (x2 - x1)
        if (
            abs(cross) <= eps
            and min(x1, x2) - eps <= x <= max(x1, x2) + eps
            and min(y1, y2) - eps <= y <= max(y1, y2) + eps
        ):
            return False
        if (y1 > y) != (y2 > y):
            x_at_y = (x2 - x1) * (y - y1) / (y2 - y1) + x1
            if x_at_y > x:
                inside = not inside
    return inside


def geometry_face_area_centroids(
    coords: Sequence[Tuple[float, float]],
    faces: Sequence[Sequence[int]],
) -> List[Tuple[float, Tuple[float, float]]]:
    ring_info = [
        (
            abs(polygon_signed_area(coords, face)),
            polygon_centroid(coords, face),
            polygon_envelope_key(coords, face),
            face,
        )
        for face in faces
    ]
    direct_children: Dict[int, List[int]] = {i: [] for i in range(len(faces))}

    for child_id, (_, _, child_env, child_face) in enumerate(ring_info):
        point = coords[child_face[0]]
        containers: List[Tuple[float, int]] = []
        for parent_id, (parent_area, _, parent_env, parent_face) in enumerate(ring_info):
            if parent_id == child_id or parent_area <= ring_info[child_id][0]:
                continue
            if not (
                parent_env[0] < child_env[0]
                and parent_env[1] < child_env[1]
                and parent_env[2] > child_env[2]
                and parent_env[3] > child_env[3]
            ):
                continue
            if point_in_polygon_strict(point, coords, parent_face):
                containers.append((parent_area, parent_id))
        if containers:
            _, parent_id = min(containers)
            direct_children[parent_id].append(child_id)

    result: List[Tuple[float, Tuple[float, float]]] = []
    for face_id, (ring_area, ring_centroid, _, _) in enumerate(ring_info):
        area = ring_area
        cx = ring_centroid[0] * ring_area
        cy = ring_centroid[1] * ring_area
        for child_id in direct_children[face_id]:
            child_area, child_centroid, _, _ = ring_info[child_id]
            area -= child_area
            cx -= child_centroid[0] * child_area
            cy -= child_centroid[1] * child_area
        if area <= 0.0:
            result.append((0.0, ring_centroid))
        else:
            result.append((area, (cx / area, cy / area)))
    return result


def trace_geometry_faces(
    coords: Sequence[Tuple[float, float]],
    exterior_geometry_ids: Sequence[int],
    material_border_paths: Sequence[Sequence[int]],
) -> List[List[int]]:
    edge_set: set[Tuple[int, int]] = set()

    def add_path(path: Sequence[int]) -> None:
        for a, b in zip(path, path[1:]):
            if a != b:
                edge_set.add(tuple(sorted((a, b))))

    ext = list(exterior_geometry_ids)
    if ext and ext[-1] != ext[0]:
        ext.append(ext[0])
    add_path(ext)
    for path in material_border_paths:
        add_path(path)

    graph: Dict[int, List[int]] = defaultdict(list)
    for a, b in edge_set:
        graph[a].append(b)
        graph[b].append(a)

    for vertex_id, neighbours in graph.items():
        x, y = coords[vertex_id]
        neighbours.sort(
            key=lambda n: (
                math.atan2(coords[n][1] - y, coords[n][0] - x),
                math.hypot(coords[n][0] - x, coords[n][1] - y),
                n,
            )
        )

    visited: set[Tuple[int, int]] = set()
    faces: List[List[int]] = []
    max_steps = len(edge_set) * 2 + 1

    for start in sorted(graph):
        for first in graph[start]:
            if (start, first) in visited:
                continue
            face: List[int] = []
            prev, cur = start, first
            steps = 0
            while (prev, cur) not in visited:
                visited.add((prev, cur))
                face.append(prev)
                neighbours = graph[cur]
                try:
                    incoming_idx = neighbours.index(prev)
                except ValueError as exc:
                    raise ConversionError("Internal validation failed: broken geometry graph.") from exc
                # Clockwise successor keeps the walked face on the left side of
                # each directed edge and matches the CAD face order.
                nxt = neighbours[(incoming_idx - 1) % len(neighbours)]
                prev, cur = cur, nxt
                steps += 1
                if steps > max_steps:
                    raise ConversionError("Internal validation failed: geometry face walk did not close.")
            if len(face) >= 3:
                faces.append(face)

    if not faces:
        raise ConversionError("No closed PRI geometry areas could be reconstructed.")
    return faces


def component_area_centroids(
    nodes: Sequence[Tuple[float, float]],
    elements: Sequence[Tuple[int, int, int]],
    components: Sequence[Sequence[int]],
) -> List[Tuple[float, Tuple[float, float]]]:
    result: List[Tuple[float, Tuple[float, float]]] = []
    for comp in components:
        total_area = 0.0
        cx = 0.0
        cy = 0.0
        for element_id in comp:
            a, b, c = elements[element_id]
            pts = (nodes[a], nodes[b], nodes[c])
            area = abs(signed_double_area(*pts)) * 0.5
            tri_cx = (pts[0][0] + pts[1][0] + pts[2][0]) / 3.0
            tri_cy = (pts[0][1] + pts[1][1] + pts[2][1]) / 3.0
            total_area += area
            cx += tri_cx * area
            cy += tri_cy * area
        if math.isclose(total_area, 0.0, abs_tol=1e-12):
            result.append((0.0, (0.0, 0.0)))
        else:
            result.append((total_area, (cx / total_area, cy / total_area)))
    return result


def order_area_components(
    nodes: Sequence[Tuple[float, float]],
    elements: Sequence[Tuple[int, int, int]],
    components: Sequence[Sequence[int]],
    geometry_vertices: Sequence[Tuple[float, float]],
    exterior_geometry_ids: Sequence[int],
    material_border_paths: Sequence[Sequence[int]],
) -> List[int]:
    faces = trace_geometry_faces(geometry_vertices, exterior_geometry_ids, material_border_paths)
    positive_faces = [face for face in faces if polygon_signed_area(geometry_vertices, face) > 0.0]
    if len(positive_faces) == len(components):
        area_faces = positive_faces
    elif len(faces) - 1 == len(components):
        outside_idx = max(range(len(faces)), key=lambda i: abs(polygon_signed_area(geometry_vertices, faces[i])))
        area_faces = [face for i, face in enumerate(faces) if i != outside_idx]
    else:
        raise ConversionError(
            "Geometry area count does not match material regions: "
            f"{len(positive_faces)} CAD faces, {len(components)} material regions"
        )
    # ProRock rebuilds CAD polygons through NetTopologySuite.Polygonizer.
    # Its EdgeRing.EnvelopeComparator returns polygons sorted lexicographically
    # by envelope: MinX, MinY, MaxX, MaxY. Keep the PRI area properties in the
    # same order so material/property rows land on the intended geometry areas.
    area_faces = sorted(area_faces, key=lambda face: polygon_envelope_key(geometry_vertices, face))

    face_info = geometry_face_area_centroids(geometry_vertices, area_faces)
    comp_info = component_area_centroids(nodes, elements, components)
    unmatched = set(range(len(components)))
    ordered: List[int] = []

    for face_area, (face_cx, face_cy) in face_info:
        area_tolerance = max(1e-6, face_area * 1e-7)
        candidates = [
            comp_id
            for comp_id in unmatched
            if abs(comp_info[comp_id][0] - face_area) <= area_tolerance
        ]
        if not candidates:
            candidates = list(unmatched)

        def score(comp_id: int) -> Tuple[float, float, int]:
            comp_area, (comp_cx, comp_cy) = comp_info[comp_id]
            area_delta = abs(comp_area - face_area) / max(face_area, 1.0)
            dist2 = (comp_cx - face_cx) ** 2 + (comp_cy - face_cy) ** 2
            return area_delta, dist2, comp_id

        best = min(candidates, key=score)
        best_area = comp_info[best][0]
        if abs(best_area - face_area) > max(1e-4, face_area * 1e-5):
            raise ConversionError(
                "Cannot match PRI geometry face to a material region: "
                f"face area {fmt_number(face_area)}, closest component area {fmt_number(best_area)}"
            )
        ordered.append(best)
        unmatched.remove(best)

    if unmatched:
        raise ConversionError(f"Material regions were not assigned to PRI areas: {sorted(unmatched)}")
    return ordered


def build_pri_mesh(model: SlideModel, include_water_table: bool = True) -> PriMesh:
    # Slide's vertices section can contain non-mesh geometry such as water-table
    # points. PRI mesh nodes should only include vertices referenced by elements.
    node_source_ids = sorted({v for cell in model.cells for v in cell.vertices})
    source_to_node = {source_id: i for i, source_id in enumerate(node_source_ids)}
    nodes = [model.vertices[source_id] for source_id in node_source_ids]

    elements: List[Tuple[int, int, int]] = []
    element_materials: List[str] = []

    # Normalize Slide cells into counter-clockwise PRI-style triangles.
    for cell in model.cells:
        src = list(cell.vertices)
        a, b, c = (model.vertices[v] for v in src)
        area2 = signed_double_area(a, b, c)
        if math.isclose(area2, 0.0, abs_tol=1e-12):
            raise ConversionError(f"Degenerate Slide cell {cell.source_id}: {cell.vertices}")
        if area2 < 0:
            src[1], src[2] = src[2], src[1]
        elements.append(tuple(source_to_node[v] for v in src))
        element_materials.append(cell.material)

    edge_to_elements = build_edge_to_elements(elements)
    non_manifold = [(edge, owners) for edge, owners in edge_to_elements.items() if len(owners) > 2]
    if non_manifold:
        edge, owners = non_manifold[0]
        raise ConversionError(f"Non-manifold mesh edge {edge} belongs to elements {owners}")

    # Neighbour order in PRI: opposite node 0, opposite node 1, opposite node 2.
    neighbours: List[Tuple[int, int, int]] = []
    boundary_flags: List[int] = []
    for i, (a, b, c) in enumerate(elements):
        opposite_edges = ((b, c), (c, a), (a, b))
        nbs: List[int] = []
        for u, v in opposite_edges:
            owners = edge_to_elements[tuple(sorted((u, v)))]
            if len(owners) == 1:
                nbs.append(-1)
            else:
                nbs.append(owners[0] if owners[1] == i else owners[1])
        neighbours.append(tuple(nbs))
        boundary_flags.append(1 if -1 in nbs else 0)

    components, component_material, element_component = components_by_material(
        elements, element_materials, edge_to_elements
    )

    # Median existing cell area is used as a conservative per-region remeshing target.
    node_coords = dict(enumerate(nodes))
    component_target_area: List[float] = []
    for comp in components:
        vals = [
            triangle_area(node_coords, elements[e])
            for e in comp
        ]
        target = float(statistics.median(vals)) if vals else 0.0
        component_target_area.append(target)

    for path in model.material_lines:
        missing = next((source_id for source_id in path if source_id not in source_to_node), None)
        if missing is not None:
            raise ConversionError(
                "Material geometry references a vertex that is not part of the Slide mesh: "
                f"{missing}"
            )

    material_line_source_ids = {v for path in model.material_lines for v in path}

    # Geometry/CAD vertices: vertices used by the external boundary, material
    # interfaces, and optional water level. Water-table points can be Slide
    # geometry vertices that are not part of the triangular mesh.
    node_to_source = {v: k for k, v in source_to_node.items()}
    interface_edges_by_components: Dict[Tuple[int, int], List[Tuple[int, int]]] = defaultdict(list)
    interface_source_ids: set[int] = set()

    for edge, owners in edge_to_elements.items():
        if len(owners) != 2:
            continue
        e1, e2 = owners
        c1, c2 = element_component[e1], element_component[e2]
        if c1 == c2:
            continue
        pair = tuple(sorted((c1, c2)))
        interface_edges_by_components[pair].append(edge)
        interface_source_ids.update(node_to_source[n] for n in edge)

    water_table_source_ids = set(model.water_table) if include_water_table else set()
    geometry_source_ids = sorted(
        set(model.exterior)
        | interface_source_ids
        | material_line_source_ids
        | water_table_source_ids
    )
    source_to_geometry = {source_id: i for i, source_id in enumerate(geometry_source_ids)}
    geometry_vertices = [model.vertices[source_id] for source_id in geometry_source_ids]
    exterior_geometry_ids = [source_to_geometry[source_id] for source_id in model.exterior]

    mesh_material_border_paths: List[List[int]] = []
    for pair in sorted(interface_edges_by_components):
        for node_path in chain_edges(interface_edges_by_components[pair]):
            mesh_material_border_paths.append([source_to_geometry[node_to_source[n]] for n in node_path])

    slide_material_border_paths = [
        [source_to_geometry[source_id] for source_id in path]
        for path in model.material_lines
        if len(path) >= 2
    ]
    water_table_geometry_ids = (
        [source_to_geometry[source_id] for source_id in model.water_table]
        if include_water_table and len(model.water_table) >= 2
        else []
    )

    material_border_paths = mesh_material_border_paths
    area_component_ids: List[int] | None = None
    slide_geometry_error: ConversionError | None = None
    if slide_material_border_paths:
        try:
            area_component_ids = order_area_components(
                nodes,
                elements,
                components,
                geometry_vertices,
                exterior_geometry_ids,
                slide_material_border_paths,
            )
        except ConversionError as exc:
            slide_geometry_error = exc
        else:
            material_border_paths = slide_material_border_paths

    if area_component_ids is None:
        try:
            area_component_ids = order_area_components(
                nodes,
                elements,
                components,
                geometry_vertices,
                exterior_geometry_ids,
                material_border_paths,
            )
        except ConversionError as exc:
            if slide_geometry_error is not None:
                raise ConversionError(
                    "Cannot reconstruct material areas from Slide CAD lines "
                    f"({slide_geometry_error}) or mesh-derived material borders ({exc})."
                ) from exc
            raise

    return PriMesh(
        node_source_ids=node_source_ids,
        nodes=nodes,
        elements=elements,
        element_materials=element_materials,
        neighbours=neighbours,
        boundary_flags=boundary_flags,
        mesh_sets=element_component,
        components=components,
        component_material=component_material,
        component_target_area=component_target_area,
        area_component_ids=area_component_ids,
        geometry_source_ids=geometry_source_ids,
        geometry_vertices=geometry_vertices,
        exterior_geometry_ids=exterior_geometry_ids,
        material_border_paths=material_border_paths,
        water_table_geometry_ids=water_table_geometry_ids,
    )


def fmt_number(value: float) -> str:
    if math.isclose(value, round(value), abs_tol=1e-12):
        return str(int(round(value)))
    return format(value, ".15g")


def material_index(materials: Sequence[Material]) -> Dict[str, int]:
    return {m.soil_id: i for i, m in enumerate(materials)}


def write_pri(path: Path, model: SlideModel, mesh: PriMesh, include_mesh: bool = False) -> None:
    mat_index = material_index(model.materials)
    missing = sorted((set(mesh.component_material) | set(mesh.element_materials)) - set(mat_index))
    if missing:
        raise ConversionError(f"Missing material definitions for: {', '.join(missing)}")

    # One additional generic material mirrors the structure of the reference PRI.
    n_element_properties = len(model.materials) + 1
    n_joint_properties = len(model.materials) + 2

    lines: List[str] = []
    add = lines.append

    add(f"version: {PRI_VERSION}")
    add(f"max time step: {DEFAULT_MAX_TIME_STEP}")
    add(f"time step size: {DEFAULT_TIME_STEP_SIZE}")
    add(f"output frequency: {DEFAULT_OUTPUT_FREQUENCY}")
    add(f"dynamic stabilization: {DEFAULT_DYNAMIC_STABILIZATION}")
    add("")

    output_node_count = len(mesh.nodes) if include_mesh else 0
    output_element_count = len(mesh.elements) if include_mesh else 0

    add(f"nodes: {output_node_count}")
    if include_mesh:
        for x, y in mesh.nodes:
            add(f"\t{fmt_number(x)}\t{fmt_number(y)}")
    add("")

    add(f"elements: {output_element_count} 0")
    if include_mesh:
        for vertices, soil, boundary, neighbours in zip(
            mesh.elements, mesh.element_materials, mesh.boundary_flags, mesh.neighbours
        ):
            a, b, c = vertices
            n0, n1, n2 = neighbours
            add(f"\t{a}\t{b}\t{c}\t{mat_index[soil]}\t{boundary}\t{n0}\t{n1}\t{n2}")
    add("")

    add(f"mesh sets: {len(mesh.mesh_sets) if include_mesh else 0}")
    if include_mesh:
        for mesh_set in mesh.mesh_sets:
            add(f"\t{mesh_set}")
    add("")

    add("joints: 0")
    add("")
    add("boundary conditions: 0")
    add("")

    add(f"element properties: {n_element_properties}")
    for material in model.materials:
        add(f"\t{clean_name(material.name)}")
        add(f"\t0\t0\t0\t0\t0\t{material.red}\t{material.green}\t{material.blue}")
    add("\tNew material")
    add(f"\t0\t0\t0\t0\t0\t{DEFAULT_RGB[0]}\t{DEFAULT_RGB[1]}\t{DEFAULT_RGB[2]}")
    add("")

    add(f"joint properties: {n_joint_properties}")
    for material in model.materials:
        add(f"\t{clean_name(material.name)}")
        add(
            f"\t0\t0\t0\t0\t0\t0\t0\t0\t0\t"
            f"{material.red}\t{material.green}\t{material.blue}"
        )
    add("\tNew material")
    add(
        f"\t0\t0\t0\t0\t0\t0\t0\t0\t0\t"
        f"{DEFAULT_RGB[0]}\t{DEFAULT_RGB[1]}\t{DEFAULT_RGB[2]}"
    )
    add("\tNew Joint")
    add(
        f"\t0\t0\t0\t0\t0\t0\t0\t0\t0\t"
        f"{DEFAULT_JOINT_RGB[0]}\t{DEFAULT_JOINT_RGB[1]}\t{DEFAULT_JOINT_RGB[2]}"
    )
    add("")

    add("bolt elements: 0")
    add("")
    add("bolt properties: 0")
    add("")

    # Disable initial mechanical loading: Slide c/phi/gamma are deliberately not
    # interpreted as FDEM parameters.
    add("loading: 0")
    add("")

    add("anisotropy:\t0")
    add("")
    add("lateral forces:")
    add("\t0\t0\t0\t0")
    add("")

    add(f"vertices: {len(mesh.geometry_vertices)}")
    for x, y in mesh.geometry_vertices:
        add(f"\t{fmt_number(x)}\t{fmt_number(y)}")
    add("")

    water_table_border = len(mesh.water_table_geometry_ids) >= 2
    border_count = 1 + len(mesh.material_border_paths) + (1 if water_table_border else 0)
    add(f"borders: {border_count}")
    add("\tExternalBorders\t-1")
    ext = mesh.exterior_geometry_ids[:]
    if ext and ext[-1] != ext[0]:
        ext.append(ext[0])
    add("\t0\t" + "\t".join(str(v) for v in ext))
    border_id = 1
    for path_ids in mesh.material_border_paths:
        if len(path_ids) < 2:
            continue
        add("\tMaterialBorders\t-1")
        add(f"\t{border_id}\t" + "\t".join(str(v) for v in path_ids))
        border_id += 1
    if water_table_border:
        add("\tWaterLevel\t-1")
        add(f"\t{border_id}\t" + "\t".join(str(v) for v in mesh.water_table_geometry_ids))
        border_id += 1
    add("")

    add(f"areas: {len(mesh.area_component_ids)}")
    for component_id in mesh.area_component_ids:
        target_area = mesh.component_target_area[component_id]
        soil = mesh.component_material[component_id]
        prop = mat_index[soil]
        add(f"\t{fmt_number(target_area)}\t{prop}\t{prop}")
    add("")

    add("joint groups: 0")
    add("")
    add("distributed loads: 0")
    add("")
    add("stages: 0")
    add("")

    add("pressure solver:")
    add("\t0\t0")
    add(f"\t{DEFAULT_WATER_UNIT_WEIGHT}")
    for _ in range(n_element_properties):
        add("\t0\t1\t0")
    add("")

    add(f"press_sat: {output_node_count}")
    if include_mesh:
        for i in range(len(mesh.nodes)):
            add(f"\t0\t0\t0\t{i}\t0")
    add("")

    add("SRF: 0")
    add("\t10000\t0.01\t5000")
    add("")
    add("individual properties:")
    add(f"\telements:\t{output_element_count}")
    add("\tjoints:\t0")

    # Reference PRI uses CRLF. Keep that convention for maximum compatibility.
    payload = "\r\n".join(lines) + "\r\n"
    try:
        path.write_bytes(payload.encode("utf-8"))
    except OSError as exc:
        raise ConversionError(f"Cannot write {path}: {exc}") from exc


def validate_mesh(model: SlideModel, mesh: PriMesh) -> ValidationSummary:
    n_nodes = len(mesh.nodes)
    n_elements = len(mesh.elements)

    if not (
        len(mesh.element_materials)
        == len(mesh.neighbours)
        == len(mesh.boundary_flags)
        == len(mesh.mesh_sets)
        == n_elements
    ):
        raise ConversionError("Internal validation failed: element array lengths differ.")

    edge_to_elements = build_edge_to_elements(mesh.elements)
    boundary_edges = sum(1 for owners in edge_to_elements.values() if len(owners) == 1)
    internal_edges = sum(1 for owners in edge_to_elements.values() if len(owners) == 2)
    used_nodes = {node for element in mesh.elements for node in element}
    unused_nodes = sorted(set(range(n_nodes)) - used_nodes)
    if unused_nodes:
        raise ConversionError(f"PRI mesh contains unused nodes: {unused_nodes[:20]}")

    for i, ((a, b, c), nbs, flag) in enumerate(zip(mesh.elements, mesh.neighbours, mesh.boundary_flags)):
        if min(a, b, c) < 0 or max(a, b, c) >= n_nodes:
            raise ConversionError(f"Element {i} references a node outside 0..{n_nodes - 1}")
        area2 = signed_double_area(mesh.nodes[a], mesh.nodes[b], mesh.nodes[c])
        if area2 <= 0:
            raise ConversionError(f"Element {i} is not counter-clockwise or is degenerate.")

        expected_edges = ((b, c), (c, a), (a, b))
        for nb, edge in zip(nbs, expected_edges):
            owners = edge_to_elements[tuple(sorted(edge))]
            if len(owners) == 1:
                if nb != -1:
                    raise ConversionError(f"Boundary edge of element {i} has neighbour {nb}, expected -1")
            elif len(owners) == 2:
                other = owners[0] if owners[1] == i else owners[1]
                if nb != other:
                    raise ConversionError(f"Element {i} neighbour mismatch: got {nb}, expected {other}")
            else:
                raise ConversionError(f"Element {i} belongs to a non-manifold edge {edge}")
        if flag != (1 if -1 in nbs else 0):
            raise ConversionError(f"Element {i} boundary flag is inconsistent with neighbours")

    if mesh.mesh_sets and (min(mesh.mesh_sets) < 0 or max(mesh.mesh_sets) >= len(mesh.components)):
        raise ConversionError("Mesh-set id is outside the PRI areas range.")

    if sorted(mesh.area_component_ids) != list(range(len(mesh.components))):
        raise ConversionError("PRI geometry areas are not a one-to-one match with material regions.")

    used = set(c.material for c in model.cells)
    return ValidationSummary(
        nodes=n_nodes,
        elements=n_elements,
        areas=len(mesh.components),
        borders=1 + len(mesh.material_border_paths) + (1 if len(mesh.water_table_geometry_ids) >= 2 else 0),
        source_materials=len(model.materials),
        used_materials=len(used),
        boundary_edges=boundary_edges,
        internal_edges=internal_edges,
        water_table_vertices=len(mesh.water_table_geometry_ids),
    )


def validate_written_pri(path: Path) -> None:
    """Lightweight syntax/count validation for the generated PRI file."""
    text = path.read_text(encoding="utf-8", errors="strict")
    lines = text.splitlines()

    required = [
        "version:", "nodes:", "elements:", "mesh sets:", "joints:",
        "boundary conditions:", "element properties:", "joint properties:",
        "vertices:", "borders:", "areas:", "pressure solver:", "press_sat:",
        "SRF:", "individual properties:",
    ]
    positions = []
    for prefix in required:
        try:
            positions.append(next(i for i, line in enumerate(lines) if line.startswith(prefix)))
        except StopIteration as exc:
            raise ConversionError(f"Generated PRI is missing section '{prefix}'") from exc
    if positions != sorted(positions):
        raise ConversionError("Generated PRI sections are out of order.")

    def count_after(prefix: str, first_field_only: bool = True) -> int:
        line = next(line for line in lines if line.startswith(prefix))
        rhs = line.split(":", 1)[1].strip()
        return int(rhs.split()[0] if first_field_only else rhs)

    n_nodes = count_after("nodes:")
    n_elements = count_after("elements:")
    n_mesh_sets = count_after("mesh sets:")
    n_press = count_after("press_sat:")
    if n_mesh_sets != n_elements:
        raise ConversionError(f"Generated PRI mesh-set count {n_mesh_sets} != element count {n_elements}")
    if n_press != n_nodes:
        raise ConversionError(f"Generated PRI press_sat count {n_press} != node count {n_nodes}")


def write_report(path: Path, input_path: Path, sli_member: str, model: SlideModel, summary: ValidationSummary) -> None:
    used_materials = sorted({c.material for c in model.cells}, key=lambda s: int(re.search(r"\d+", s).group()))
    material_rows = []
    for i, mat in enumerate(model.materials):
        material_rows.append({
            "pri_property_index": i,
            "slide_soil_id": mat.soil_id,
            "name": mat.name,
            "rgb": [mat.red, mat.green, mat.blue],
            "used_by_cells": mat.soil_id in used_materials,
            "slide_c": mat.slide_c,
            "slide_phi": mat.slide_phi,
            "slide_unit_weight": mat.slide_unit_weight,
        })

    report = {
        "input": str(input_path),
        "sli_member": sli_member,
        "summary": asdict(summary),
        "materials": material_rows,
        "notes": [
            "Slide CAD geometry is exported as PRI vertices, borders and areas.",
            "Slide triangular cells are omitted from PRI output by default; --keep-mesh preserves them as PRI triangular elements.",
            "Connected cells with the same Slide material form one PRI area/mesh set.",
            "Slide material names and RGB colours are preserved.",
            "Slide c, phi and unit weight are recorded in this report only; they are not mapped to ProRock FDEM properties.",
            "Slide water-table geometry is exported as a PRI WaterLevel border when enabled and present.",
            "Generated mechanical loading is disabled and all ProRock element/joint mechanical properties are zero.",
        ],
    }
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def convert(
    input_path: Path,
    output_path: Path,
    report_path: Path | None = None,
    include_mesh: bool = False,
    include_water_table: bool = True,
    fix_geometry: bool = False,
    add_excavation_stages: bool = False,
    excavation_stage_count: int = 4,
    excavation_top_point: tuple[float, float] | None = None,
    remove_bolts: bool = False,
    geometry_min_distance: float = DEFAULT_GEOMETRY_MIN_DISTANCE,
    geometry_min_angle: float = DEFAULT_GEOMETRY_MIN_ANGLE,
    preserve_geometry_area_count: bool = True,
    progress: ProgressCallback | None = None,
) -> ValidationSummary:
    if progress is not None:
        progress(f"reading Slide file: {input_path.name}")
    text, sli_member = read_sli_from_slim(input_path)
    if progress is not None:
        progress("parsing Slide model")
    model = parse_slide_model(text)
    if progress is not None:
        progress("building PRI geometry")
    mesh = build_pri_mesh(model, include_water_table=include_water_table)
    if progress is not None:
        progress(f"validating reconstructed geometry: {len(mesh.geometry_vertices)} vertices")
    summary = validate_mesh(model, mesh)
    if progress is not None:
        progress(f"writing PRI: {output_path}")
    write_pri(output_path, model, mesh, include_mesh=include_mesh)
    if remove_bolts:
        if progress is not None:
            progress("starting bolt and anchor cleanup")
        try:
            remove_bolt_geometry(output_path, output_path, progress=progress)
        except GeometryFixError as exc:
            raise ConversionError(f"Bolt cleanup failed: {exc}") from exc
    if fix_geometry:
        if progress is not None:
            progress("starting PRI geometry cleanup")
        try:
            geometry_fix = clean_pri_geometry(
                output_path,
                output_path,
                minimum_distance=geometry_min_distance,
                minimum_angle=geometry_min_angle,
                preserve_area_count=preserve_geometry_area_count,
                progress=progress,
            )
        except GeometryFixError as exc:
            raise ConversionError(f"Geometry cleanup failed: {exc}") from exc
        summary = replace(summary, geometry_fix=geometry_fix)
    if add_excavation_stages:
        if progress is not None:
            progress(f"starting {excavation_stage_count}-stage excavation extension using user top point")
        try:
            add_excavation_stage_geometry(
                output_path,
                output_path,
                stage_count=excavation_stage_count,
                top_point=excavation_top_point,
                progress=progress,
            )
        except GeometryFixError as exc:
            raise ConversionError(f"Excavation-stage extension failed: {exc}") from exc
        summary = replace(
            summary,
            areas=summary.areas + excavation_stage_count,
            borders=summary.borders + excavation_stage_count,
        )
    if progress is not None:
        progress("validating written PRI")
    validate_written_pri(output_path)
    if report_path is not None:
        if progress is not None:
            progress(f"writing report: {report_path}")
        write_report(report_path, input_path, sli_member, model, summary)
    if progress is not None:
        progress("conversion complete")
    return summary


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Convert a Rocscience Slide 6 .slim/.sli model into a ProRock PRI 2.2.0 geometry file.",
    )
    parser.add_argument("--version", action="version", version=f"Seque converter {__version__}")
    parser.add_argument("input", type=Path, help="Input .slim archive or unpacked .sli file")
    parser.add_argument("output", type=Path, nargs="?", help="Output .pri path; default: <input>.pri")
    parser.add_argument(
        "--report",
        type=Path,
        default=None,
        help="Optional JSON conversion report containing Slide c/phi/unit-weight values and material mapping",
    )
    parser.add_argument(
        "--keep-mesh",
        action="store_true",
        help="Keep imported Slide triangular mesh elements. By default they are omitted to hide internal lines.",
    )
    parser.add_argument(
        "--no-water",
        action="store_true",
        help="Do not transfer the Slide water table as a PRI WaterLevel border.",
    )
    parser.add_argument(
        "--fix-geometry",
        action="store_true",
        help="Run PRI geometry cleanup after conversion before ProRock meshing.",
    )
    parser.add_argument(
        "--add-excavation-stages",
        action="store_true",
        help="Detect a left or right pit wall and add equal-height excavation stages after geometry cleanup.",
    )
    parser.add_argument(
        "--excavation-stage-count",
        type=int,
        default=4,
        help="Number of equal-height excavation stages. Default: 4.",
    )
    parser.add_argument("--excavation-top-x", type=float, help="X coordinate of an ExternalBorders excavation-top vertex.")
    parser.add_argument("--excavation-top-y", type=float, help="Y coordinate of an ExternalBorders excavation-top vertex.")
    parser.add_argument(
        "--remove-bolts",
        action="store_true",
        help="Remove explicitly labelled bolt, anchor and support geometry before cleanup.",
    )
    parser.add_argument(
        "--geometry-min-distance",
        type=float,
        default=DEFAULT_GEOMETRY_MIN_DISTANCE,
        help="Merge geometry vertices closer than this distance in metres when --fix-geometry is used. Default: 2.",
    )
    parser.add_argument(
        "--geometry-min-angle",
        type=float,
        default=DEFAULT_GEOMETRY_MIN_ANGLE,
        help="Report and clean simple geometry angles smaller than this value in degrees. Default: 18.",
    )
    parser.add_argument(
        "--allow-geometry-area-count-change",
        action="store_true",
        help="Allow geometry cleanup to change the reconstructed CAD face count.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    input_path: Path = args.input
    output_path: Path = args.output or input_path.with_suffix(".pri")
    report_path: Path | None = args.report

    try:
        summary = convert(
            input_path,
            output_path,
            report_path,
            include_mesh=args.keep_mesh,
            include_water_table=not args.no_water,
            fix_geometry=args.fix_geometry,
            add_excavation_stages=args.add_excavation_stages,
            excavation_stage_count=args.excavation_stage_count,
            excavation_top_point=(args.excavation_top_x, args.excavation_top_y)
            if args.excavation_top_x is not None and args.excavation_top_y is not None
            else None,
            remove_bolts=args.remove_bolts,
            geometry_min_distance=args.geometry_min_distance,
            geometry_min_angle=args.geometry_min_angle,
            preserve_geometry_area_count=not args.allow_geometry_area_count_change,
        )
    except ConversionError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # Make unexpected failures visible in command-line use.
        print(f"UNEXPECTED ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3

    print(f"OK: {input_path} -> {output_path}")
    if args.keep_mesh:
        print(f"  nodes:             {summary.nodes}")
        print(f"  triangular elems:  {summary.elements}")
    else:
        print(f"  source mesh nodes: {summary.nodes} (omitted from PRI output)")
        print(f"  source triangles:  {summary.elements} (omitted from PRI output)")
        print("  internal lines:    cleaned; use --keep-mesh to preserve Slide triangulation")
    print(f"  material areas:    {summary.areas}")
    print(f"  geometry borders:  {summary.borders}")
    print(f"  materials:         {summary.source_materials} total, {summary.used_materials} used")
    print(f"  boundary edges:    {summary.boundary_edges}")
    print(f"  internal edges:    {summary.internal_edges}")
    if summary.water_table_vertices:
        print(f"  water table:       {summary.water_table_vertices} vertices transferred")
    if summary.geometry_fix is not None:
        fix = summary.geometry_fix
        print("  geometry cleanup:")
        if fix.backup_path is not None:
            print(f"    base copy:       {fix.backup_path}")
        print(f"    errors:          {fix.errors_before} -> {fix.errors_after}")
        print(f"    vertices:        {fix.vertices_before} -> {fix.vertices_after}")
        print(f"    close pairs:     {fix.close_pairs_before} -> {fix.close_pairs_after}")
        print(f"    near segments:   {fix.near_segments_before} -> {fix.near_segments_after}")
        print(f"    near edges:      {fix.near_fixed_edges_before} -> {fix.near_fixed_edges_after}")
        print(f"    near material:   {fix.near_material_segments_before} -> {fix.near_material_segments_after}")
        print(f"    small angles:    {fix.small_angles_before} -> {fix.small_angles_after}")
        if fix.spread_vertex_pairs:
            print(f"    spread pairs:    {fix.spread_vertex_pairs}")
        if fix.spread_bad_parts:
            print(f"    spread bad parts: {fix.spread_bad_parts}")
        if fix.warnings:
            for warning in fix.warnings:
                print(f"    WARNING: {warning}")
    print("  WARNING: ProRock mechanical element/joint properties are zero by design.")
    if report_path is not None:
        print(f"  report:            {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
