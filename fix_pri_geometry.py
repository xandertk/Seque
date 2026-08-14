#!/usr/bin/env python3
"""Clean ProRock PRI CAD geometry before discrete mesh generation.

The script mirrors the useful part of ProRock's Check geometry / Auto Fix flow:
it finds CAD vertices closer than the configured distance and rewrites geometry
borders so those vertices become one point. It also reports small intersection
angles and removes simple non-junction path points that create angles below the
configured minimum. When merging would make geometry worse, it moves lithotype
vertices away from nearby fixed or lithotype segments, inserting a segment
vertex first when needed.
"""

from __future__ import annotations

import argparse
import math
import re
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Callable, Mapping, Sequence


DEFAULT_MIN_DISTANCE = 2.0
DEFAULT_MIN_ANGLE = 18.0
EPS = 1e-9
ProgressCallback = Callable[[str], None]


class GeometryFixError(RuntimeError):
    pass


def emit_progress(progress: ProgressCallback | None, message: str) -> None:
    if progress is not None:
        progress(message)


@dataclass(frozen=True)
class BorderRecord:
    header_line: str
    kind: str
    border_id: int
    vertex_ids: tuple[int, ...]


@dataclass(frozen=True)
class PriDocument:
    encoding: str
    line_ending: str
    prefix: tuple[str, ...]
    suffix: tuple[str, ...]
    vertices: tuple[tuple[float, float], ...]
    borders: tuple[BorderRecord, ...]
    area_count: int | None


@dataclass(frozen=True)
class SegmentEdge:
    border_index: int
    position: int
    first: int
    second: int
    kind: str


class SegmentSpatialIndex:
    """Uniform-grid index for nearby segment queries.

    Geometry checks are called repeatedly while candidates are evaluated.  A
    direct all-pairs scan makes these checks quadratic in the number of
    borders.  The grid only narrows down candidates; every caller still runs
    the same exact distance test, so it cannot change a cleanup decision.
    """

    _MAX_CELLS_PER_SEGMENT = 4096

    def __init__(
        self,
        coords: Sequence[tuple[float, float]],
        edges: Sequence[tuple[int, int]],
        minimum_distance: float,
    ) -> None:
        self.coords = coords
        self.edges = edges
        self.cell_size = minimum_distance
        self.cells: dict[tuple[int, int], list[int]] = defaultdict(list)
        self.overflow: list[int] = []

        for edge_index, (first, second) in enumerate(edges):
            start, end = coords[first], coords[second]
            min_x, max_x = sorted((start[0], end[0]))
            min_y, max_y = sorted((start[1], end[1]))
            first_x = math.floor(min_x / self.cell_size)
            last_x = math.floor(max_x / self.cell_size)
            first_y = math.floor(min_y / self.cell_size)
            last_y = math.floor(max_y / self.cell_size)
            cell_count = (last_x - first_x + 1) * (last_y - first_y + 1)
            # A very long border would otherwise fill a huge grid.  Keeping
            # it in overflow is still correct and is uncommon in real models.
            if cell_count > self._MAX_CELLS_PER_SEGMENT:
                self.overflow.append(edge_index)
                continue
            for cell_x in range(first_x, last_x + 1):
                for cell_y in range(first_y, last_y + 1):
                    self.cells[(cell_x, cell_y)].append(edge_index)

    def query_bbox(
        self,
        start: tuple[float, float],
        end: tuple[float, float],
        padding: float,
    ) -> list[tuple[int, int]]:
        min_x, max_x = sorted((start[0], end[0]))
        min_y, max_y = sorted((start[1], end[1]))
        first_x = math.floor((min_x - padding) / self.cell_size)
        last_x = math.floor((max_x + padding) / self.cell_size)
        first_y = math.floor((min_y - padding) / self.cell_size)
        last_y = math.floor((max_y + padding) / self.cell_size)
        candidate_indexes = set(self.overflow)
        for cell_x in range(first_x, last_x + 1):
            for cell_y in range(first_y, last_y + 1):
                candidate_indexes.update(self.cells.get((cell_x, cell_y), ()))
        # Preserve source-edge order. Some callers keep the first equally-good
        # candidate, so deterministic query order is part of cleanup behavior.
        return [self.edges[index] for index in sorted(candidate_indexes)]


@dataclass(frozen=True)
class GeometryFixSummary:
    input_path: str
    output_path: str
    backup_path: str | None
    minimum_distance: float
    minimum_angle: float
    fixed: bool
    errors_before: int
    errors_after: int
    vertices_before: int
    vertices_after: int
    borders_before: int
    borders_after: int
    merged_vertices: int
    merge_clusters: int
    spread_vertex_pairs: int
    spread_bad_parts: int
    angle_vertices_removed: int
    collinear_vertices_removed: int
    borders_removed: int
    skipped_merge_pairs: int
    skipped_worse_merges: int
    skipped_spread_pairs: int
    skipped_bad_part_spreads: int
    fixed_close_pairs_after: int
    fixed_near_edges_after: int
    close_pairs_before: int
    close_pairs_after: int
    near_segments_before: int
    near_segments_after: int
    near_fixed_edges_before: int
    near_fixed_edges_after: int
    near_material_segments_before: int
    near_material_segments_after: int
    small_angles_before: int
    small_angles_after: int
    areas_declared: int | None
    faces_before: int | None
    faces_after: int | None
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class PitBenchSummary:
    """Description of an automatically added excavation extension."""

    side: str
    floor_elevation: float
    crest_elevation: float
    added_areas: int


@dataclass(frozen=True)
class GeometryQuality:
    close_pairs: int
    near_segments: int
    near_fixed_edges: int
    near_material_segments: int
    small_angles: int

    @property
    def errors(self) -> int:
        return (
            self.close_pairs
            + self.near_segments
            + self.near_fixed_edges
            + self.near_material_segments
            + self.small_angles
        )


class UnionFind:
    def __init__(self, size: int) -> None:
        self.parent = list(range(size))
        self.members: dict[int, list[int]] = {i: [i] for i in range(size)}

    def find(self, value: int) -> int:
        parent = self.parent[value]
        if parent != value:
            self.parent[value] = self.find(parent)
        return self.parent[value]

    def union(self, a: int, b: int) -> int:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return ra
        root, other = (ra, rb) if ra < rb else (rb, ra)
        self.parent[other] = root
        self.members[root].extend(self.members.pop(other))
        return root

    def snapshot(self) -> tuple[list[int], dict[int, list[int]]]:
        return self.parent[:], {root: members[:] for root, members in self.members.items()}

    def restore(self, snapshot: tuple[list[int], dict[int, list[int]]]) -> None:
        parent, members = snapshot
        self.parent = parent[:]
        self.members = {root: values[:] for root, values in members.items()}


def fmt_number(value: float) -> str:
    if math.isclose(value, round(value), abs_tol=1e-12):
        return str(int(round(value)))
    return format(value, ".15g")


def decode_pri(raw: bytes) -> tuple[str, str]:
    if raw.startswith(b"\xef\xbb\xbf"):
        try:
            return raw.decode("utf-8-sig"), "utf-8-sig"
        except UnicodeDecodeError:
            pass
    for encoding in ("utf-8", "cp1251"):
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    raise GeometryFixError("PRI file is not valid UTF-8 or Windows-1251 text.")


def section_count(line: str, heading: str) -> int | None:
    m = re.match(rf"^\s*{re.escape(heading)}:\s*(\d+)\b", line, flags=re.IGNORECASE)
    return int(m.group(1)) if m else None


def find_counted_section(lines: Sequence[str], heading: str, start: int = 0) -> tuple[int, int]:
    for index in range(start, len(lines)):
        count = section_count(lines[index], heading)
        if count is not None:
            return index, count
    raise GeometryFixError(f"PRI file is missing section '{heading}:'.")


def parse_vertex(line: str, vertex_id: int) -> tuple[float, float]:
    parts = line.split()
    if len(parts) < 2:
        raise GeometryFixError(f"Vertex {vertex_id} row is incomplete.")
    try:
        return float(parts[0]), float(parts[1])
    except ValueError as exc:
        raise GeometryFixError(f"Vertex {vertex_id} row contains non-numeric coordinates.") from exc


def parse_border_header(line: str, border_index: int) -> tuple[str, str]:
    parts = line.split()
    if len(parts) < 2:
        raise GeometryFixError(f"Border {border_index} header is incomplete.")
    return line, parts[0]


def parse_border_path(line: str, border_index: int, vertex_count: int) -> tuple[int, tuple[int, ...]]:
    parts = line.split()
    if len(parts) < 2:
        raise GeometryFixError(f"Border {border_index} path is incomplete.")
    try:
        values = [int(part) for part in parts]
    except ValueError as exc:
        raise GeometryFixError(f"Border {border_index} path contains non-integer vertex ids.") from exc

    border_id, vertex_ids = values[0], tuple(values[1:])
    invalid = [value for value in vertex_ids if value < 0 or value >= vertex_count]
    if invalid:
        raise GeometryFixError(
            f"Border {border_index} references vertex ids outside 0..{vertex_count - 1}: {invalid[:8]}"
        )
    return border_id, vertex_ids


def read_pri(path: Path) -> PriDocument:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise GeometryFixError(f"Cannot read {path}: {exc}") from exc

    text, encoding = decode_pri(raw)
    line_ending = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines()

    vertices_index, vertex_count = find_counted_section(lines, "vertices")
    first_vertex_line = vertices_index + 1
    last_vertex_line = first_vertex_line + vertex_count
    if last_vertex_line > len(lines):
        raise GeometryFixError("PRI vertices section ends before the declared count.")

    vertices = tuple(
        parse_vertex(line, vertex_id)
        for vertex_id, line in enumerate(lines[first_vertex_line:last_vertex_line])
    )

    borders_index, border_count = find_counted_section(lines, "borders", last_vertex_line)
    borders: list[BorderRecord] = []
    cursor = borders_index + 1
    for border_index in range(border_count):
        while cursor < len(lines) and not lines[cursor].strip():
            cursor += 1
        if cursor >= len(lines):
            raise GeometryFixError("PRI borders section ends before the declared count.")
        header_line, kind = parse_border_header(lines[cursor], border_index)
        cursor += 1

        while cursor < len(lines) and not lines[cursor].strip():
            cursor += 1
        if cursor >= len(lines):
            raise GeometryFixError(f"Border {border_index} is missing its vertex path row.")
        border_id, vertex_ids = parse_border_path(lines[cursor], border_index, vertex_count)
        cursor += 1
        borders.append(BorderRecord(header_line, kind, border_id, vertex_ids))

    area_count: int | None = None
    for line in lines[cursor:]:
        area_count = section_count(line, "areas")
        if area_count is not None:
            break

    return PriDocument(
        encoding=encoding,
        line_ending=line_ending,
        prefix=tuple(lines[:vertices_index]),
        suffix=tuple(lines[cursor:]),
        vertices=vertices,
        borders=tuple(borders),
        area_count=area_count,
    )


def close_vertex_pairs(
    coords: Sequence[tuple[float, float]],
    minimum_distance: float,
) -> list[tuple[int, int, float]]:
    if minimum_distance <= 0:
        return []

    cell_size = minimum_distance
    buckets: dict[tuple[int, int], list[int]] = defaultdict(list)
    result: list[tuple[int, int, float]] = []
    for index, (x, y) in enumerate(coords):
        bx = math.floor(x / cell_size)
        by = math.floor(y / cell_size)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for other in buckets.get((bx + dx, by + dy), ()):
                    ox, oy = coords[other]
                    distance = math.hypot(x - ox, y - oy)
                    if distance <= minimum_distance + EPS:
                        result.append((other, index, distance))
        buckets[(bx, by)].append(index)
    result.sort(key=lambda item: (item[2], item[0], item[1]))
    return result


def fixed_segment_edges(borders: Sequence[BorderRecord]) -> list[tuple[int, int]]:
    return [(edge.first, edge.second) for edge in fixed_segment_records(borders)]


def fixed_segment_records(borders: Sequence[BorderRecord]) -> list[SegmentEdge]:
    edges: list[SegmentEdge] = []
    for border_index, border in enumerate(borders):
        if border.kind.lower() == "materialborders":
            continue
        path = list(border.vertex_ids)
        if border_is_external(border) and path and path[-1] != path[0]:
            path.append(path[0])
        for position, (first, second) in enumerate(zip(path, path[1:])):
            if first != second:
                edges.append(SegmentEdge(border_index, position, first, second, border.kind))
    return edges


def near_fixed_segment_points(
    coords: Sequence[tuple[float, float]],
    borders: Sequence[BorderRecord],
    minimum_distance: float,
) -> list[tuple[int, int, int, float, tuple[float, float]]]:
    if minimum_distance <= 0:
        return []

    movable = movable_vertex_ids(borders)
    fixed_edges = fixed_segment_edges(borders)
    fixed_index = SegmentSpatialIndex(coords, fixed_edges, minimum_distance)
    result: list[tuple[int, int, int, float, tuple[float, float]]] = []
    for vertex_id in sorted(movable):
        point = coords[vertex_id]
        for first, second in fixed_index.query_bbox(point, point, minimum_distance):
            if vertex_id in (first, second):
                continue
            distance, projection = point_segment_distance(point, coords[first], coords[second])
            if distance <= minimum_distance + EPS:
                result.append((vertex_id, first, second, distance, projection))
    result.sort(key=lambda item: (item[3], item[0], item[1], item[2]))
    return result


def material_segment_edges(borders: Sequence[BorderRecord]) -> list[tuple[int, int]]:
    return [(edge.first, edge.second) for edge in material_segment_records(borders)]


def material_segment_records(borders: Sequence[BorderRecord]) -> list[SegmentEdge]:
    edges: list[SegmentEdge] = []
    for border_index, border in enumerate(borders):
        if border.kind.lower() != "materialborders":
            continue
        for position, (first, second) in enumerate(zip(border.vertex_ids, border.vertex_ids[1:])):
            if first != second:
                edges.append(SegmentEdge(border_index, position, first, second, border.kind))
    return edges


def near_fixed_segment_edges(
    coords: Sequence[tuple[float, float]],
    borders: Sequence[BorderRecord],
    minimum_distance: float,
) -> list[tuple[int, int, int, int, float]]:
    if minimum_distance <= 0:
        return []

    movable = movable_vertex_ids(borders)
    fixed_edges = fixed_segment_edges(borders)
    fixed_index = SegmentSpatialIndex(coords, fixed_edges, minimum_distance)
    by_material_edge: dict[tuple[int, int], tuple[int, int, int, int, float]] = {}
    for material_first, material_second in material_segment_edges(borders):
        if material_first not in movable and material_second not in movable:
            continue
        material_start = coords[material_first]
        material_end = coords[material_second]
        for fixed_first, fixed_second in fixed_index.query_bbox(material_start, material_end, minimum_distance):
            if material_first in (fixed_first, fixed_second) or material_second in (fixed_first, fixed_second):
                continue
            fixed_start = coords[fixed_first]
            fixed_end = coords[fixed_second]
            if segment_bboxes_farther_than(material_start, material_end, fixed_start, fixed_end, minimum_distance):
                continue
            distance, material_t = segment_distance_with_first_parameter(
                material_start,
                material_end,
                fixed_start,
                fixed_end,
            )
            if distance <= minimum_distance + EPS:
                if material_t <= 1e-6 and material_first not in movable:
                    continue
                if material_t >= 1.0 - 1e-6 and material_second not in movable:
                    continue
                key = tuple(sorted((material_first, material_second)))
                current = by_material_edge.get(key)
                if current is None or distance < current[4]:
                    by_material_edge[key] = (material_first, material_second, fixed_first, fixed_second, distance)
    result = list(by_material_edge.values())
    result.sort(key=lambda item: (item[4], item[0], item[1], item[2], item[3]))
    return result


def near_material_segment_points(
    coords: Sequence[tuple[float, float]],
    borders: Sequence[BorderRecord],
    minimum_distance: float,
) -> list[tuple[int, int, int, float, tuple[float, float], float]]:
    if minimum_distance <= 0:
        return []

    material_edges = material_segment_records(borders)
    material_edge_pairs = [(edge.first, edge.second) for edge in material_edges]
    material_index = SegmentSpatialIndex(coords, material_edge_pairs, minimum_distance)
    material_vertices = {
        vertex_id
        for border in borders
        if border.kind.lower() == "materialborders"
        for vertex_id in border.vertex_ids
    }
    graph = border_graph(borders)
    result: list[tuple[int, int, int, float, tuple[float, float], float]] = []
    seen: set[tuple[int, int, int]] = set()
    for vertex_id in sorted(material_vertices):
        point = coords[vertex_id]
        neighbours = graph.get(vertex_id, set())
        for first, second in material_index.query_bbox(point, point, minimum_distance):
            if vertex_id in (first, second):
                continue
            if first in neighbours or second in neighbours:
                continue
            if segment_bboxes_farther_than(point, point, coords[first], coords[second], minimum_distance):
                continue
            distance, projection, t = point_segment_distance_with_parameter(point, coords[first], coords[second])
            if t <= 1e-6 or t >= 1.0 - 1e-6:
                continue
            if distance <= minimum_distance + EPS:
                key = (vertex_id, min(first, second), max(first, second))
                if key in seen:
                    continue
                seen.add(key)
                result.append((vertex_id, first, second, distance, projection, t))
    result.sort(key=lambda item: (item[3], item[0], item[1], item[2]))
    return result


def vertex_connected_to_edge_endpoint(
    borders: Sequence[BorderRecord],
    vertex_id: int,
    first: int,
    second: int,
) -> bool:
    graph = border_graph(borders)
    neighbours = graph.get(vertex_id, set())
    return first in neighbours or second in neighbours


def segment_bboxes_farther_than(
    first_start: tuple[float, float],
    first_end: tuple[float, float],
    second_start: tuple[float, float],
    second_end: tuple[float, float],
    distance: float,
) -> bool:
    left_gap = max(min(first_start[0], first_end[0]), min(second_start[0], second_end[0])) - min(
        max(first_start[0], first_end[0]),
        max(second_start[0], second_end[0]),
    )
    lower_gap = max(min(first_start[1], first_end[1]), min(second_start[1], second_end[1])) - min(
        max(first_start[1], first_end[1]),
        max(second_start[1], second_end[1]),
    )
    dx = max(0.0, left_gap)
    dy = max(0.0, lower_gap)
    return dx * dx + dy * dy > (distance + EPS) * (distance + EPS)


def point_segment_distance(
    point: tuple[float, float],
    first: tuple[float, float],
    second: tuple[float, float],
) -> tuple[float, tuple[float, float]]:
    distance, projection, _ = point_segment_distance_with_parameter(point, first, second)
    return distance, projection


def point_segment_distance_with_parameter(
    point: tuple[float, float],
    first: tuple[float, float],
    second: tuple[float, float],
) -> tuple[float, tuple[float, float], float]:
    px, py = point
    x1, y1 = first
    x2, y2 = second
    dx = x2 - x1
    dy = y2 - y1
    length2 = dx * dx + dy * dy
    if length2 <= EPS:
        return math.hypot(px - x1, py - y1), first, 0.0
    t = ((px - x1) * dx + (py - y1) * dy) / length2
    t = max(0.0, min(1.0, t))
    projection = (x1 + t * dx, y1 + t * dy)
    return math.hypot(px - projection[0], py - projection[1]), projection, t


def segment_distance(
    first_start: tuple[float, float],
    first_end: tuple[float, float],
    second_start: tuple[float, float],
    second_end: tuple[float, float],
) -> float:
    return segment_distance_with_first_parameter(first_start, first_end, second_start, second_end)[0]


def segment_distance_with_first_parameter(
    first_start: tuple[float, float],
    first_end: tuple[float, float],
    second_start: tuple[float, float],
    second_end: tuple[float, float],
) -> tuple[float, float]:
    if segments_intersect(first_start, first_end, second_start, second_end):
        return 0.0, 0.5
    first_distance = point_segment_distance(first_start, second_start, second_end)[0]
    second_distance = point_segment_distance(first_end, second_start, second_end)[0]
    third_distance, _, third_t = point_segment_distance_with_parameter(second_start, first_start, first_end)
    fourth_distance, _, fourth_t = point_segment_distance_with_parameter(second_end, first_start, first_end)
    return min(
        (first_distance, 0.0),
        (second_distance, 1.0),
        (third_distance, third_t),
        (fourth_distance, fourth_t),
        key=lambda item: item[0],
    )


def segments_intersect(
    a: tuple[float, float],
    b: tuple[float, float],
    c: tuple[float, float],
    d: tuple[float, float],
) -> bool:
    def orient(p: tuple[float, float], q: tuple[float, float], r: tuple[float, float]) -> float:
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    def on_segment(p: tuple[float, float], q: tuple[float, float], r: tuple[float, float]) -> bool:
        return (
            min(p[0], r[0]) - EPS <= q[0] <= max(p[0], r[0]) + EPS
            and min(p[1], r[1]) - EPS <= q[1] <= max(p[1], r[1]) + EPS
        )

    o1 = orient(a, b, c)
    o2 = orient(a, b, d)
    o3 = orient(c, d, a)
    o4 = orient(c, d, b)
    if o1 * o2 < -EPS and o3 * o4 < -EPS:
        return True
    return (
        abs(o1) <= EPS and on_segment(a, c, b)
        or abs(o2) <= EPS and on_segment(a, d, b)
        or abs(o3) <= EPS and on_segment(c, a, d)
        or abs(o4) <= EPS and on_segment(c, b, d)
    )


def cluster_diameter_ok(
    coords: Sequence[tuple[float, float]],
    members: Sequence[int],
    maximum_distance: float,
) -> bool:
    for i, first in enumerate(members):
        x1, y1 = coords[first]
        for second in members[i + 1:]:
            x2, y2 = coords[second]
            if math.hypot(x1 - x2, y1 - y2) > maximum_distance + EPS:
                return False
    return True


def merge_map_for_close_vertices(
    coords: Sequence[tuple[float, float]],
    minimum_distance: float,
    minimum_angle: float,
    borders: Sequence[BorderRecord] | None = None,
    target_face_count: int | None = None,
    preserve_face_count: bool = True,
    progress: ProgressCallback | None = None,
) -> tuple[dict[int, int], dict[int, tuple[float, float]], int, int, int, int, int, int]:
    uf = UnionFind(len(coords))
    adjusted_coords = list(coords)
    skipped_topology_merges = 0
    skipped_worse_merges = 0
    spread_vertex_pairs = 0
    skipped_spread_pairs = 0

    if borders is not None:
        base_borders: list[BorderRecord] = list(borders)
        current_quality = geometry_quality(adjusted_coords, base_borders, minimum_distance, minimum_angle)
        movable_roots = movable_vertex_ids(base_borders)
    else:
        base_borders = []
        current_quality = GeometryQuality(len(close_vertex_pairs(adjusted_coords, minimum_distance)), 0, 0, 0, 0)
        movable_roots = set(range(len(coords)))

    pairs = close_vertex_pairs(coords, minimum_distance)
    emit_progress(progress, f"geometry cleanup: checking {len(pairs)} close vertex pair(s)")
    for pair_index, (a, b, _) in enumerate(pairs, start=1):
        if pair_index == 1 or pair_index == len(pairs) or pair_index % 10 == 0:
            emit_progress(progress, f"geometry cleanup: close-pair merge check {pair_index}/{len(pairs)}")
        ra, rb = uf.find(a), uf.find(b)
        if ra == rb:
            continue
        members = uf.members[ra] + uf.members[rb]
        if not cluster_diameter_ok(adjusted_coords, members, minimum_distance):
            skipped_topology_merges += 1
            spread_result = try_spread_vertex_clusters(
                adjusted_coords,
                base_borders,
                uf,
                a,
                b,
                minimum_distance,
                minimum_angle,
                target_face_count,
                preserve_face_count,
                current_quality.errors,
                movable_roots,
            )
            if spread_result is not None:
                spread_vertex_pairs += 1
                current_quality = spread_result
            else:
                skipped_spread_pairs += 1
            continue

        snapshot = uf.snapshot()
        uf.union(ra, rb)
        accept_merge = True
        topology_rejected = False
        errors_rejected = False
        if preserve_face_count and borders is not None and target_face_count is not None:
            old_to_root = {index: uf.find(index) for index in range(len(coords))}
            root_to_coord = root_coordinates(adjusted_coords, old_to_root)
            try:
                merged_borders, _ = rewrite_borders_after_merge(base_borders, old_to_root)
                renumbered_vertices, renumbered_borders = renumber_vertices(root_to_coord, merged_borders)
                candidate_face_count = geometry_face_count(renumbered_vertices, renumbered_borders)
            except GeometryFixError:
                candidate_face_count = None
            if candidate_face_count != target_face_count:
                accept_merge = False
                topology_rejected = True

        if accept_merge and borders is not None:
            old_to_root = {index: uf.find(index) for index in range(len(coords))}
            root_to_coord = root_coordinates(adjusted_coords, old_to_root)
            try:
                merged_borders, _ = rewrite_borders_after_merge(base_borders, old_to_root)
                renumbered_vertices, renumbered_borders = renumber_vertices(root_to_coord, merged_borders)
                candidate_quality = geometry_quality(
                    renumbered_vertices,
                    renumbered_borders,
                    minimum_distance,
                    minimum_angle,
                )
            except GeometryFixError:
                candidate_quality = GeometryQuality(current_quality.errors + 1, 0, 0, 0, 0)
            if candidate_quality.errors > current_quality.errors:
                accept_merge = False
                errors_rejected = True
            else:
                current_quality = candidate_quality

        if not accept_merge:
            uf.restore(snapshot)
            if topology_rejected:
                skipped_topology_merges += 1
            if errors_rejected:
                skipped_worse_merges += 1
            spread_result = try_spread_vertex_clusters(
                adjusted_coords,
                base_borders,
                uf,
                a,
                b,
                minimum_distance,
                minimum_angle,
                target_face_count,
                preserve_face_count,
                current_quality.errors,
                movable_roots,
            )
            if spread_result is not None:
                spread_vertex_pairs += 1
                current_quality = spread_result
            else:
                skipped_spread_pairs += 1

    old_to_root = {index: uf.find(index) for index in range(len(coords))}
    root_to_coord = root_coordinates(adjusted_coords, old_to_root)
    merged_vertices, merge_clusters = merge_statistics(old_to_root)
    return (
        old_to_root,
        root_to_coord,
        merged_vertices,
        merge_clusters,
        skipped_topology_merges + skipped_worse_merges,
        spread_vertex_pairs,
        skipped_worse_merges,
        skipped_spread_pairs,
    )


def root_coordinates(
    coords: Sequence[tuple[float, float]],
    old_to_root: Mapping[int, int],
) -> dict[int, tuple[float, float]]:
    root_to_members: dict[int, list[int]] = defaultdict(list)
    for index, root in old_to_root.items():
        root_to_members[root].append(index)

    root_to_coord: dict[int, tuple[float, float]] = {}
    for root, members in root_to_members.items():
        xs = [coords[index][0] for index in members]
        ys = [coords[index][1] for index in members]
        root_to_coord[root] = (sum(xs) / len(xs), sum(ys) / len(ys))

    return root_to_coord


def merge_statistics(old_to_root: Mapping[int, int]) -> tuple[int, int]:
    root_to_count: dict[int, int] = defaultdict(int)
    for root in old_to_root.values():
        root_to_count[root] += 1

    merge_clusters = sum(1 for count in root_to_count.values() if count > 1)
    merged_vertices = sum(count - 1 for count in root_to_count.values() if count > 1)
    return merged_vertices, merge_clusters


def try_spread_vertex_clusters(
    coords: list[tuple[float, float]],
    borders: Sequence[BorderRecord],
    uf: UnionFind,
    first: int,
    second: int,
    minimum_distance: float,
    minimum_angle: float,
    target_face_count: int | None,
    preserve_face_count: bool,
    max_allowed_errors: int,
    movable_roots: set[int],
) -> GeometryQuality | None:
    root_first = uf.find(first)
    root_second = uf.find(second)
    if root_first == root_second:
        return None
    first_movable = root_first in movable_roots
    second_movable = root_second in movable_roots
    if not first_movable and not second_movable:
        return None

    members_first = uf.members[root_first]
    members_second = uf.members[root_second]
    center_first = cluster_center(coords, members_first)
    center_second = cluster_center(coords, members_second)
    dx = center_second[0] - center_first[0]
    dy = center_second[1] - center_first[1]
    distance = math.hypot(dx, dy)
    target = minimum_distance + max(1e-6, minimum_distance * 1e-6)

    if distance <= EPS:
        ux, uy = 1.0, 0.0
    else:
        ux, uy = dx / distance, dy / distance

    shift = max(0.0, target - distance) * 0.5
    first_shift = shift
    second_shift = shift
    if first_movable and not second_movable:
        first_shift = shift * 2.0
        second_shift = 0.0
    elif second_movable and not first_movable:
        first_shift = 0.0
        second_shift = shift * 2.0

    candidate_coords = list(coords)
    if first_shift:
        for member in members_first:
            x, y = candidate_coords[member]
            candidate_coords[member] = (x - ux * first_shift, y - uy * first_shift)
    if second_shift:
        for member in members_second:
            x, y = candidate_coords[member]
            candidate_coords[member] = (x + ux * second_shift, y + uy * second_shift)

    old_to_root = {index: uf.find(index) for index in range(len(coords))}
    root_to_coord = root_coordinates(candidate_coords, old_to_root)
    try:
        merged_borders, _ = rewrite_borders_after_merge(borders, old_to_root)
        renumbered_vertices, renumbered_borders = renumber_vertices(root_to_coord, merged_borders)
        if preserve_face_count and target_face_count is not None:
            candidate_face_count = geometry_face_count(renumbered_vertices, renumbered_borders)
            if candidate_face_count != target_face_count:
                return None
        candidate_quality = geometry_quality(
            renumbered_vertices,
            renumbered_borders,
            minimum_distance,
            minimum_angle,
        )
    except GeometryFixError:
        return None

    if candidate_quality.errors > max_allowed_errors:
        return None

    coords[:] = candidate_coords
    return candidate_quality


def cluster_center(
    coords: Sequence[tuple[float, float]],
    members: Sequence[int],
) -> tuple[float, float]:
    return (
        sum(coords[index][0] for index in members) / len(members),
        sum(coords[index][1] for index in members) / len(members),
    )


def spread_remaining_bad_geometry(
    coords: Sequence[tuple[float, float]],
    borders: Sequence[BorderRecord],
    minimum_distance: float,
    minimum_angle: float,
    target_face_count: int | None,
    preserve_face_count: bool,
    max_passes: int = 30,
    progress: ProgressCallback | None = None,
) -> tuple[list[tuple[float, float]], list[BorderRecord], int, int]:
    current_coords = list(coords)
    current_borders = list(borders)
    spread_count = 0
    skipped: set[tuple[object, ...]] = set()

    seen_blocked: set[tuple[int, int, int]] = set()

    previous_errors: int | None = None
    stagnant_passes = 0
    for pass_index in range(max_passes):
        movable = movable_vertex_ids(current_borders)
        quality = geometry_quality(current_coords, current_borders, minimum_distance, minimum_angle)
        emit_progress(
            progress,
            "geometry spread pass "
            f"{pass_index + 1}/{max_passes}: errors={quality.errors}, "
            f"close={quality.close_pairs}, near outer={quality.near_segments}, "
            f"near fixed={quality.near_fixed_edges}, near material={quality.near_material_segments}, "
            f"angles={quality.small_angles}",
        )
        if previous_errors == quality.errors:
            stagnant_passes += 1
        else:
            stagnant_passes = 0
        previous_errors = quality.errors
        if stagnant_passes >= 6:
            emit_progress(progress, "geometry spread stopped: no improvement for several passes")
            break
        changed = False

        for vertex_id, first, second, _, projection in near_fixed_segment_points(
            current_coords,
            current_borders,
            minimum_distance,
        ):
            candidates = spread_point_from_projection(
                current_coords,
                vertex_id,
                projection,
                minimum_distance,
                movable,
            )
            best = best_quality_candidate(
                candidates,
                current_borders,
                minimum_distance,
                minimum_angle,
                quality,
                target_face_count,
                preserve_face_count,
            )
            if best is None:
                skipped.add(("segment", vertex_id, first, second))
                continue
            current_coords = best
            spread_count += 1
            changed = True
            emit_progress(progress, f"moved material vertex {vertex_id} away from fixed segment {first}-{second}")
            break

        if changed:
            continue

        for material_first, material_second, fixed_first, fixed_second, _ in near_fixed_segment_edges(
            current_coords,
            current_borders,
            minimum_distance,
        ):
            candidates = spread_material_edge_from_fixed_edge(
                current_coords,
                material_first,
                material_second,
                fixed_first,
                fixed_second,
                minimum_distance,
                movable,
            )
            split_candidates = split_material_edge_candidates(
                current_coords,
                current_borders,
                material_first,
                material_second,
                fixed_first,
                fixed_second,
                minimum_distance,
                movable,
            )
            best = best_geometry_candidate(
                candidates,
                current_borders,
                minimum_distance,
                minimum_angle,
                quality,
                target_face_count,
                preserve_face_count,
            )
            best_split = best_split_candidate(
                split_candidates,
                minimum_distance,
                minimum_angle,
                quality,
                target_face_count,
                preserve_face_count,
            )
            if best_split is not None:
                split_quality, split_coords, split_borders = best_split
                if best is None or quality_sort_key(split_quality) < quality_sort_key(geometry_quality(best, current_borders, minimum_distance, minimum_angle)):
                    current_coords = split_coords
                    current_borders = split_borders
                    spread_count += 1
                    changed = True
                    emit_progress(progress, f"inserted/moved material edge vertex near fixed segment {fixed_first}-{fixed_second}")
                    break
            if best is None:
                skipped.add(("edge", material_first, material_second, fixed_first, fixed_second))
                continue
            current_coords = best
            spread_count += 1
            changed = True
            emit_progress(progress, f"moved material edge {material_first}-{material_second} away from fixed segment {fixed_first}-{fixed_second}")
            break

        if changed:
            continue

        for vertex_id, first, second, _, projection, _ in near_material_segment_points(
            current_coords,
            current_borders,
            minimum_distance,
        ):
            candidates = spread_point_from_projection(
                current_coords,
                vertex_id,
                projection,
                minimum_distance,
                movable,
            )
            split_candidates = split_material_edge_from_point_candidates(
                current_coords,
                current_borders,
                vertex_id,
                first,
                second,
                projection,
                minimum_distance,
            )
            best = best_geometry_candidate(
                candidates,
                current_borders,
                minimum_distance,
                minimum_angle,
                quality,
                target_face_count,
                preserve_face_count,
            )
            best_split = best_split_candidate(
                split_candidates,
                minimum_distance,
                minimum_angle,
                quality,
                target_face_count,
                preserve_face_count,
            )
            if best_split is not None:
                split_quality, split_coords, split_borders = best_split
                if best is None or quality_sort_key(split_quality) < quality_sort_key(
                    geometry_quality(best, current_borders, minimum_distance, minimum_angle)
                ):
                    current_coords = split_coords
                    current_borders = split_borders
                    spread_count += 1
                    changed = True
                    emit_progress(progress, f"inserted/moved material edge vertex near material vertex {vertex_id}")
                    break
            if best is None:
                skipped.add(("material-segment", vertex_id, first, second))
                continue
            current_coords = best
            spread_count += 1
            changed = True
            emit_progress(progress, f"moved material vertex {vertex_id} away from material segment {first}-{second}")
            break

        if changed:
            continue

        for first, second, _ in close_vertex_pairs(current_coords, minimum_distance):
            border_candidates = collapse_short_fixed_edge_candidates(
                current_coords,
                current_borders,
                first,
                second,
                minimum_distance,
            )
            best_border = best_split_candidate(
                border_candidates,
                minimum_distance,
                minimum_angle,
                quality,
                target_face_count,
                preserve_face_count,
            )
            if best_border is not None:
                _, border_coords, border_borders = best_border
                current_coords = border_coords
                current_borders = border_borders
                spread_count += 1
                changed = True
                emit_progress(progress, f"collapsed short fixed edge {first}-{second}")
                break

            candidates = spread_specific_pair(
                current_coords,
                first,
                second,
                minimum_distance,
                movable,
            )
            split_candidates = split_shared_material_vertex_candidates(
                current_coords,
                current_borders,
                first,
                second,
                minimum_distance,
            )
            if not candidates:
                best = None
            else:
                best = best_quality_candidate(
                    candidates,
                    current_borders,
                    minimum_distance,
                    minimum_angle,
                    quality,
                    target_face_count,
                    preserve_face_count,
                )
            best_split = best_split_candidate(
                split_candidates,
                minimum_distance,
                minimum_angle,
                quality,
                target_face_count,
                preserve_face_count,
            )
            if best_split is not None:
                split_quality, split_coords, split_borders = best_split
                if best is None or quality_sort_key(split_quality) < quality_sort_key(geometry_quality(best, current_borders, minimum_distance, minimum_angle)):
                    current_coords = split_coords
                    current_borders = split_borders
                    spread_count += 1
                    changed = True
                    emit_progress(progress, f"split shared material vertex near close pair {first}-{second}")
                    break
            if best is None:
                skipped.add(("close", first, second))
                continue
            current_coords = best
            spread_count += 1
            changed = True
            emit_progress(progress, f"spread close vertex pair {first}-{second}")
            break

        if changed:
            continue

        small_angle = first_small_angle(current_coords, current_borders, minimum_angle, seen_blocked)
        if small_angle is None:
            break
        vertex_id, first, second = small_angle
        candidate = best_spread_angle_candidate(
            current_coords,
            current_borders,
            vertex_id,
            first,
            second,
            minimum_distance,
            minimum_angle,
            movable,
            quality.errors,
            target_face_count,
            preserve_face_count,
        )
        if candidate is None:
            skipped.add(("angle", vertex_id, first, second))
            seen_blocked.add(small_angle)
            continue
        current_coords = candidate
        spread_count += 1
        seen_blocked.clear()
        emit_progress(progress, f"spread small-angle vertex {vertex_id}")

    return current_coords, current_borders, spread_count, len(skipped)


def best_quality_candidate(
    candidates: Sequence[Sequence[tuple[float, float]]],
    borders: Sequence[BorderRecord],
    minimum_distance: float,
    minimum_angle: float,
    current_quality: GeometryQuality,
    target_face_count: int | None,
    preserve_face_count: bool,
) -> list[tuple[float, float]] | None:
    best: tuple[GeometryQuality, list[tuple[float, float]]] | None = None
    for candidate in candidates:
        candidate_coords = list(candidate)
        try:
            candidate_quality = geometry_quality(candidate_coords, borders, minimum_distance, minimum_angle)
            if candidate_quality.errors >= current_quality.errors:
                continue
            if preserve_face_count and target_face_count is not None:
                candidate_face_count = geometry_face_count(candidate_coords, borders)
                if candidate_face_count != target_face_count:
                    continue
        except GeometryFixError:
            continue
        if best is None or quality_sort_key(candidate_quality) < quality_sort_key(best[0]):
            best = (candidate_quality, candidate_coords)
    return best[1] if best is not None else None


def best_geometry_candidate(
    candidates: Sequence[Sequence[tuple[float, float]]],
    borders: Sequence[BorderRecord],
    minimum_distance: float,
    minimum_angle: float,
    current_quality: GeometryQuality,
    target_face_count: int | None,
    preserve_face_count: bool,
) -> list[tuple[float, float]] | None:
    return best_quality_candidate(
        candidates,
        borders,
        minimum_distance,
        minimum_angle,
        current_quality,
        target_face_count,
        preserve_face_count,
    )


def best_split_candidate(
    candidates: Sequence[tuple[list[tuple[float, float]], list[BorderRecord]]],
    minimum_distance: float,
    minimum_angle: float,
    current_quality: GeometryQuality,
    target_face_count: int | None,
    preserve_face_count: bool,
) -> tuple[GeometryQuality, list[tuple[float, float]], list[BorderRecord]] | None:
    best: tuple[GeometryQuality, list[tuple[float, float]], list[BorderRecord]] | None = None
    for candidate_coords, candidate_borders in candidates:
        try:
            compact_coords, compact_borders = compact_geometry(candidate_coords, candidate_borders)
            candidate_quality = geometry_quality(compact_coords, compact_borders, minimum_distance, minimum_angle)
            if candidate_quality.errors >= current_quality.errors:
                continue
            if preserve_face_count and target_face_count is not None:
                candidate_face_count = geometry_face_count(compact_coords, compact_borders)
                if candidate_face_count != target_face_count:
                    continue
        except GeometryFixError:
            continue
        if best is None or quality_sort_key(candidate_quality) < quality_sort_key(best[0]):
            best = (candidate_quality, compact_coords, compact_borders)
    return best


def compact_geometry(
    coords: Sequence[tuple[float, float]],
    borders: Sequence[BorderRecord],
) -> tuple[list[tuple[float, float]], list[BorderRecord]]:
    used = sorted({vertex_id for border in borders for vertex_id in border.vertex_ids})
    if any(vertex_id < 0 or vertex_id >= len(coords) for vertex_id in used):
        raise GeometryFixError("Candidate border references a missing vertex.")
    old_to_new = {vertex_id: index for index, vertex_id in enumerate(used)}
    compact_coords = [coords[vertex_id] for vertex_id in used]
    compact_borders = [
        replace(border, vertex_ids=tuple(old_to_new[vertex_id] for vertex_id in border.vertex_ids))
        for border in borders
    ]
    return compact_coords, compact_borders


def quality_sort_key(quality: GeometryQuality) -> tuple[int, int, int, int]:
    return (
        quality.errors,
        quality.near_segments + quality.near_fixed_edges + quality.near_material_segments,
        quality.close_pairs,
        quality.small_angles,
    )


def normalized_directions(*vectors: tuple[float, float]) -> list[tuple[float, float]]:
    directions: list[tuple[float, float]] = []
    seen: set[tuple[int, int]] = set()
    defaults = (
        (1.0, 0.0),
        (-1.0, 0.0),
        (0.0, 1.0),
        (0.0, -1.0),
        (1.0, 1.0),
        (1.0, -1.0),
        (-1.0, 1.0),
        (-1.0, -1.0),
    )
    for x, y in (*vectors, *defaults):
        length = math.hypot(x, y)
        if length <= EPS:
            continue
        ux, uy = x / length, y / length
        key = (round(ux, 9), round(uy, 9))
        hashed = (int(key[0] * 1_000_000_000), int(key[1] * 1_000_000_000))
        if hashed in seen:
            continue
        seen.add(hashed)
        directions.append((ux, uy))
    return directions


def candidate_move_vectors(
    base_shift: float,
    *directions: tuple[float, float],
    scales: Sequence[float] = (0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 6.0, 8.0, 12.0, 16.0),
    include_defaults: bool = True,
) -> list[tuple[float, float, float]]:
    candidates: list[tuple[float, float, float]] = []
    seen: set[tuple[int, int, int]] = set()
    unit_directions = normalized_directions(*directions) if include_defaults else normalized_directions_only(*directions)
    for scale in scales:
        shift = max(base_shift * scale, 1e-6)
        for ux, uy in unit_directions:
            dx = ux * shift
            dy = uy * shift
            key = (round(dx, 6), round(dy, 6), round(shift, 6))
            hashed = (int(key[0] * 1_000_000), int(key[1] * 1_000_000), int(key[2] * 1_000_000))
            if hashed in seen:
                continue
            seen.add(hashed)
            candidates.append((dx, dy, shift))
    candidates.sort(key=lambda item: item[2])
    return candidates


def normalized_directions_only(*vectors: tuple[float, float]) -> list[tuple[float, float]]:
    directions: list[tuple[float, float]] = []
    seen: set[tuple[int, int]] = set()
    for x, y in vectors:
        length = math.hypot(x, y)
        if length <= EPS:
            continue
        ux, uy = x / length, y / length
        key = (round(ux, 9), round(uy, 9))
        hashed = (int(key[0] * 1_000_000_000), int(key[1] * 1_000_000_000))
        if hashed in seen:
            continue
        seen.add(hashed)
        directions.append((ux, uy))
    return directions


def moved_candidate(
    coords: Sequence[tuple[float, float]],
    moves: Mapping[int, tuple[float, float]],
) -> list[tuple[float, float]]:
    candidate = list(coords)
    for vertex_id, (dx, dy) in moves.items():
        x, y = candidate[vertex_id]
        candidate[vertex_id] = (x + dx, y + dy)
    return candidate


def fixed_close_pair_count(
    coords: Sequence[tuple[float, float]],
    borders: Sequence[BorderRecord],
    minimum_distance: float,
) -> int:
    movable = movable_vertex_ids(borders)
    return sum(1 for first, second, _ in close_vertex_pairs(coords, minimum_distance) if first not in movable and second not in movable)


def fixed_near_edge_count(
    coords: Sequence[tuple[float, float]],
    borders: Sequence[BorderRecord],
    minimum_distance: float,
) -> int:
    movable = movable_vertex_ids(borders)
    count = 0
    for material_first, material_second, _, _, _ in near_fixed_segment_edges(coords, borders, minimum_distance):
        if material_first not in movable or material_second not in movable:
            count += 1
    return count


def spread_specific_pair(
    coords: Sequence[tuple[float, float]],
    first: int,
    second: int,
    minimum_distance: float,
    movable: set[int],
) -> list[list[tuple[float, float]]]:
    first_movable = first in movable
    second_movable = second in movable
    if not first_movable and not second_movable:
        return []

    x1, y1 = coords[first]
    x2, y2 = coords[second]
    dx = x2 - x1
    dy = y2 - y1
    distance = math.hypot(dx, dy)
    target = minimum_distance + max(0.05, minimum_distance * 0.02)
    if distance >= target:
        return []

    base_shift = target - distance
    directions = normalized_directions((x1 - x2, y1 - y2), (x2 - x1, y2 - y1))
    candidates: list[list[tuple[float, float]]] = []
    for move_x, move_y, _ in candidate_move_vectors(base_shift, *directions):
        if first_movable:
            candidates.append(moved_candidate(coords, {first: (move_x, move_y)}))
        if second_movable:
            candidates.append(moved_candidate(coords, {second: (-move_x, -move_y)}))
        if first_movable and second_movable:
            candidates.append(
                moved_candidate(
                    coords,
                    {
                        first: (move_x * 0.5, move_y * 0.5),
                        second: (-move_x * 0.5, -move_y * 0.5),
                    },
                )
            )
    return candidates


def spread_point_from_projection(
    coords: Sequence[tuple[float, float]],
    vertex_id: int,
    projection: tuple[float, float],
    minimum_distance: float,
    movable: set[int],
) -> list[list[tuple[float, float]]]:
    if vertex_id not in movable:
        return []

    adjusted = list(coords)
    x, y = adjusted[vertex_id]
    dx = x - projection[0]
    dy = y - projection[1]
    distance = math.hypot(dx, dy)
    target = minimum_distance + max(1e-6, minimum_distance * 1e-6)
    if distance >= target:
        return []

    directions: list[tuple[float, float]] = []
    if distance > EPS:
        directions.append((dx / distance, dy / distance))
    directions.extend(((0.0, -1.0), (0.0, 1.0), (-1.0, 0.0), (1.0, 0.0)))

    shift = max(target - distance, max(0.05, minimum_distance * 0.05))
    candidates: list[list[tuple[float, float]]] = []
    for move_x, move_y, _ in candidate_move_vectors(
        shift,
        *directions,
        scales=(0.5, 1.0, 1.5, 2.0, 4.0, 8.0),
        include_defaults=False,
    ):
        candidate = list(coords)
        candidate[vertex_id] = (x + move_x, y + move_y)
        candidates.append(candidate)

    return candidates


def spread_material_edge_from_fixed_edge(
    coords: Sequence[tuple[float, float]],
    material_first: int,
    material_second: int,
    fixed_first: int,
    fixed_second: int,
    minimum_distance: float,
    movable: set[int],
) -> list[list[tuple[float, float]]]:
    movable_ids = [vertex_id for vertex_id in (material_first, material_second) if vertex_id in movable]
    if not movable_ids:
        return []

    m1 = coords[material_first]
    m2 = coords[material_second]
    f1 = coords[fixed_first]
    f2 = coords[fixed_second]
    distance, material_t = segment_distance_with_first_parameter(m1, m2, f1, f2)
    target = minimum_distance + max(0.05, minimum_distance * 0.02)
    base_shift = max(target - distance, max(0.05, minimum_distance * 0.05))
    closest_material = (
        m1[0] + (m2[0] - m1[0]) * material_t,
        m1[1] + (m2[1] - m1[1]) * material_t,
    )
    _, closest_fixed = point_segment_distance(closest_material, f1, f2)

    material_mid = ((m1[0] + m2[0]) * 0.5, (m1[1] + m2[1]) * 0.5)
    fixed_mid = ((f1[0] + f2[0]) * 0.5, (f1[1] + f2[1]) * 0.5)
    away_x = material_mid[0] - fixed_mid[0]
    away_y = material_mid[1] - fixed_mid[1]
    away_len = math.hypot(away_x, away_y)
    directions: list[tuple[float, float]] = []
    if away_len > EPS:
        directions.append((away_x / away_len, away_y / away_len))
    closest_away_x = closest_material[0] - closest_fixed[0]
    closest_away_y = closest_material[1] - closest_fixed[1]
    closest_away_len = math.hypot(closest_away_x, closest_away_y)
    if closest_away_len > EPS:
        directions.append((closest_away_x / closest_away_len, closest_away_y / closest_away_len))
    edge_dx = f2[0] - f1[0]
    edge_dy = f2[1] - f1[1]
    edge_len = math.hypot(edge_dx, edge_dy)
    if edge_len > EPS:
        directions.append((-edge_dy / edge_len, edge_dx / edge_len))
        directions.append((edge_dy / edge_len, -edge_dx / edge_len))
    directions.extend(((0.0, -1.0), (0.0, 1.0)))

    candidates: list[list[tuple[float, float]]] = []
    seen: set[tuple[float, float, float, tuple[int, ...]]] = set()
    for scale in (1.0, 2.0, 4.0, 8.0):
        shift = base_shift * scale
        for ux, uy in directions:
            key = (round(ux, 12), round(uy, 12), round(shift, 12), tuple(movable_ids))
            if key in seen:
                continue
            seen.add(key)
            candidate = list(coords)
            for vertex_id in movable_ids:
                x, y = candidate[vertex_id]
                candidate[vertex_id] = (x + ux * shift, y + uy * shift)
            candidates.append(candidate)
            if len(movable_ids) == 1:
                movable_id = movable_ids[0]
                pull = material_t if movable_id == material_first else 1.0 - material_t
                if pull > EPS:
                    candidate = list(coords)
                    x, y = candidate[movable_id]
                    candidate[movable_id] = (x + ux * shift / pull, y + uy * shift / pull)
                    candidates.append(candidate)
    return candidates


def split_material_edge_candidates(
    coords: Sequence[tuple[float, float]],
    borders: Sequence[BorderRecord],
    material_first: int,
    material_second: int,
    fixed_first: int,
    fixed_second: int,
    minimum_distance: float,
    movable: set[int],
) -> list[tuple[list[tuple[float, float]], list[BorderRecord]]]:
    if material_first not in movable and material_second not in movable:
        return []

    edge = find_material_edge_record(borders, material_first, material_second)
    if edge is None:
        return []

    m1 = coords[material_first]
    m2 = coords[material_second]
    f1 = coords[fixed_first]
    f2 = coords[fixed_second]
    distance, material_t = segment_distance_with_first_parameter(m1, m2, f1, f2)
    if material_t <= 1e-5 or material_t >= 1.0 - 1e-5:
        return []

    split_point = (
        m1[0] + (m2[0] - m1[0]) * material_t,
        m1[1] + (m2[1] - m1[1]) * material_t,
    )
    _, fixed_projection = point_segment_distance(split_point, f1, f2)
    base_shift = max(minimum_distance - distance, max(0.05, minimum_distance * 0.05))
    edge_dx = f2[0] - f1[0]
    edge_dy = f2[1] - f1[1]
    away = (split_point[0] - fixed_projection[0], split_point[1] - fixed_projection[1])
    directions = normalized_directions(
        away,
        (-edge_dy, edge_dx),
        (edge_dy, -edge_dx),
        (0.0, -1.0),
        (0.0, 1.0),
    )

    result: list[tuple[list[tuple[float, float]], list[BorderRecord]]] = []
    new_vertex_id = len(coords)
    for move_x, move_y, _ in candidate_move_vectors(base_shift, *directions):
        candidate_coords = list(coords)
        candidate_coords.append((split_point[0] + move_x, split_point[1] + move_y))
        candidate_borders = insert_vertex_into_border(borders, edge, new_vertex_id)
        result.append((candidate_coords, candidate_borders))
    return result


def split_material_edge_from_point_candidates(
    coords: Sequence[tuple[float, float]],
    borders: Sequence[BorderRecord],
    vertex_id: int,
    material_first: int,
    material_second: int,
    projection: tuple[float, float],
    minimum_distance: float,
) -> list[tuple[list[tuple[float, float]], list[BorderRecord]]]:
    edge = find_material_edge_record(borders, material_first, material_second)
    if edge is None:
        return []

    point = coords[vertex_id]
    start = coords[material_first]
    end = coords[material_second]
    distance, _, material_t = point_segment_distance_with_parameter(point, start, end)
    if material_t <= 1e-5 or material_t >= 1.0 - 1e-5:
        return []

    target = minimum_distance + max(0.05, minimum_distance * 0.02)
    base_shift = max(target - distance, max(0.05, minimum_distance * 0.05))
    edge_dx = end[0] - start[0]
    edge_dy = end[1] - start[1]
    away = (projection[0] - point[0], projection[1] - point[1])
    directions = normalized_directions_only(
        away,
        (-edge_dy, edge_dx),
        (edge_dy, -edge_dx),
        (0.0, -1.0),
        (0.0, 1.0),
        (-1.0, 0.0),
        (1.0, 0.0),
    )

    result: list[tuple[list[tuple[float, float]], list[BorderRecord]]] = []
    new_vertex_id = len(coords)
    for move_x, move_y, _ in candidate_move_vectors(
        base_shift,
        *directions,
        scales=(0.5, 1.0, 1.5, 2.0, 4.0, 8.0),
        include_defaults=False,
    ):
        candidate_coords = list(coords)
        candidate_coords.append((projection[0] + move_x, projection[1] + move_y))
        candidate_borders = insert_vertex_into_border(borders, edge, new_vertex_id)
        result.append((candidate_coords, candidate_borders))
    return result


def find_material_edge_record(
    borders: Sequence[BorderRecord],
    first: int,
    second: int,
) -> SegmentEdge | None:
    for edge in material_segment_records(borders):
        if (edge.first, edge.second) == (first, second) or (edge.first, edge.second) == (second, first):
            return edge
    return None


def insert_vertex_into_border(
    borders: Sequence[BorderRecord],
    edge: SegmentEdge,
    vertex_id: int,
) -> list[BorderRecord]:
    result = list(borders)
    border = result[edge.border_index]
    ids = list(border.vertex_ids)
    if edge.position >= len(ids) - 1:
        ids.insert(len(ids) - 1, vertex_id)
    elif ids[edge.position] == edge.first and ids[edge.position + 1] == edge.second:
        ids.insert(edge.position + 1, vertex_id)
    elif ids[edge.position] == edge.second and ids[edge.position + 1] == edge.first:
        ids.insert(edge.position + 1, vertex_id)
    else:
        for index, (first, second) in enumerate(zip(ids, ids[1:])):
            if (first, second) == (edge.first, edge.second) or (first, second) == (edge.second, edge.first):
                ids.insert(index + 1, vertex_id)
                break
        else:
            return result
    result[edge.border_index] = replace(border, vertex_ids=tuple(ids))
    return result


def split_shared_material_vertex_candidates(
    coords: Sequence[tuple[float, float]],
    borders: Sequence[BorderRecord],
    first: int,
    second: int,
    minimum_distance: float,
) -> list[tuple[list[tuple[float, float]], list[BorderRecord]]]:
    result: list[tuple[list[tuple[float, float]], list[BorderRecord]]] = []
    for vertex_id, other_id in ((first, second), (second, first)):
        if not vertex_can_be_split_from_material(borders, vertex_id):
            continue
        vx, vy = coords[vertex_id]
        ox, oy = coords[other_id]
        distance = math.hypot(vx - ox, vy - oy)
        base_shift = max(minimum_distance + max(0.05, minimum_distance * 0.02) - distance, max(0.05, minimum_distance * 0.05))
        pull_x, pull_y = material_pull_direction(coords, borders, vertex_id)
        directions = normalized_directions_only((vx - ox, vy - oy), (pull_x, pull_y), (0.0, -1.0), (0.0, 1.0))
        for move_x, move_y, _ in candidate_move_vectors(
            base_shift,
            *directions,
            scales=(1.0, 2.0, 4.0, 8.0),
            include_defaults=False,
        ):
            new_vertex_id = len(coords)
            candidate_coords = list(coords)
            candidate_coords.append((vx + move_x, vy + move_y))
            candidate_borders = replace_material_vertex_references(borders, vertex_id, new_vertex_id)
            result.append((candidate_coords, candidate_borders))
    return result


def collapse_short_fixed_edge_candidates(
    coords: Sequence[tuple[float, float]],
    borders: Sequence[BorderRecord],
    first: int,
    second: int,
    minimum_distance: float,
) -> list[tuple[list[tuple[float, float]], list[BorderRecord]]]:
    if math.hypot(coords[first][0] - coords[second][0], coords[first][1] - coords[second][1]) > minimum_distance + EPS:
        return []
    if not fixed_vertices_share_edge(borders, first, second):
        return []

    candidates: list[tuple[list[tuple[float, float]], list[BorderRecord]]] = []
    for remove_id, keep_id in ((first, second), (second, first)):
        candidate_borders = replace_vertex_references(borders, remove_id, keep_id)
        candidates.append((list(coords), candidate_borders))
    return candidates


def fixed_vertices_share_edge(
    borders: Sequence[BorderRecord],
    first: int,
    second: int,
) -> bool:
    for edge in fixed_segment_records(borders):
        if (edge.first, edge.second) == (first, second) or (edge.first, edge.second) == (second, first):
            return True
    return False


def replace_vertex_references(
    borders: Sequence[BorderRecord],
    old_vertex_id: int,
    new_vertex_id: int,
) -> list[BorderRecord]:
    result: list[BorderRecord] = []
    for border in borders:
        mapped = [new_vertex_id if vertex_id == old_vertex_id else vertex_id for vertex_id in border.vertex_ids]
        cleaned = clean_vertex_path(mapped, force_closed=border_is_external(border))
        if cleaned:
            result.append(replace(border, vertex_ids=cleaned))
    return result


def vertex_can_be_split_from_material(
    borders: Sequence[BorderRecord],
    vertex_id: int,
) -> bool:
    has_fixed = False
    has_material = False
    for border in borders:
        if vertex_id not in border.vertex_ids:
            continue
        if border.kind.lower() == "materialborders":
            has_material = True
        else:
            has_fixed = True
    return has_fixed and has_material


def material_pull_direction(
    coords: Sequence[tuple[float, float]],
    borders: Sequence[BorderRecord],
    vertex_id: int,
) -> tuple[float, float]:
    vx, vy = coords[vertex_id]
    total_x = 0.0
    total_y = 0.0
    for border in borders:
        if border.kind.lower() != "materialborders":
            continue
        ids = list(border.vertex_ids)
        for index, current in enumerate(ids):
            if current != vertex_id:
                continue
            neighbours: list[int] = []
            if index > 0:
                neighbours.append(ids[index - 1])
            if index + 1 < len(ids):
                neighbours.append(ids[index + 1])
            for neighbour in neighbours:
                nx, ny = coords[neighbour]
                total_x += nx - vx
                total_y += ny - vy
    return total_x, total_y


def replace_material_vertex_references(
    borders: Sequence[BorderRecord],
    old_vertex_id: int,
    new_vertex_id: int,
) -> list[BorderRecord]:
    result: list[BorderRecord] = []
    for border in borders:
        if border.kind.lower() != "materialborders":
            result.append(border)
            continue
        if old_vertex_id not in border.vertex_ids:
            result.append(border)
            continue
        result.append(
            replace(
                border,
                vertex_ids=tuple(new_vertex_id if vertex_id == old_vertex_id else vertex_id for vertex_id in border.vertex_ids),
            )
        )
    return result


def first_small_angle(
    coords: Sequence[tuple[float, float]],
    borders: Sequence[BorderRecord],
    minimum_angle: float,
    ignored: set[tuple[int, int, int]] | None = None,
) -> tuple[int, int, int] | None:
    ignored = ignored or set()
    graph = border_graph(borders)
    for vertex_id, neighbours in sorted(graph.items()):
        ordered = sorted(neighbours)
        for i, first in enumerate(ordered):
            for second in ordered[i + 1:]:
                key = (vertex_id, first, second)
                if key in ignored:
                    continue
                angle = angle_between(coords[vertex_id], coords[first], coords[second])
                if angle < minimum_angle - EPS:
                    return vertex_id, first, second
    return None


def best_spread_angle_candidate(
    coords: Sequence[tuple[float, float]],
    borders: Sequence[BorderRecord],
    vertex_id: int,
    first: int,
    second: int,
    minimum_distance: float,
    minimum_angle: float,
    movable: set[int],
    max_allowed_errors: int,
    target_face_count: int | None,
    preserve_face_count: bool,
) -> list[tuple[float, float]] | None:
    candidates: list[list[tuple[float, float]]] = []
    base_step = max(minimum_distance * 0.25, 1e-6)
    for scale in (1.0, 2.0, 4.0, 8.0, 16.0, 32.0):
        candidates.extend(
            spread_angle_vertex(coords, vertex_id, first, second, base_step * scale, movable)
        )

    best: tuple[GeometryQuality, list[tuple[float, float]]] | None = None
    current_quality = geometry_quality(coords, borders, minimum_distance, minimum_angle)
    for candidate in candidates:
        try:
            if preserve_face_count and target_face_count is not None:
                candidate_face_count = geometry_face_count(candidate, borders)
                if candidate_face_count != target_face_count:
                    continue
            candidate_quality = geometry_quality(candidate, borders, minimum_distance, minimum_angle)
        except GeometryFixError:
            continue
        if candidate_quality.errors > max_allowed_errors:
            continue
        if candidate_quality.small_angles >= current_quality.small_angles and candidate_quality.errors == current_quality.errors:
            continue
        if best is None or (
            candidate_quality.errors,
            candidate_quality.small_angles,
            candidate_quality.close_pairs
            + candidate_quality.near_segments
            + candidate_quality.near_fixed_edges
            + candidate_quality.near_material_segments,
        ) < (
            best[0].errors,
            best[0].small_angles,
            best[0].close_pairs
            + best[0].near_segments
            + best[0].near_fixed_edges
            + best[0].near_material_segments,
        ):
            best = (candidate_quality, candidate)

    return best[1] if best is not None else None


def spread_angle_vertex(
    coords: Sequence[tuple[float, float]],
    vertex_id: int,
    first: int,
    second: int,
    step: float,
    movable: set[int],
) -> list[list[tuple[float, float]]]:
    candidates: list[list[tuple[float, float]]] = []

    vx, vy = coords[vertex_id]
    p1 = coords[first]
    p2 = coords[second]
    mid = ((p1[0] + p2[0]) * 0.5, (p1[1] + p2[1]) * 0.5)
    dx = vx - mid[0]
    dy = vy - mid[1]
    length = math.hypot(dx, dy)
    if length <= EPS:
        edge_dx = p2[0] - p1[0]
        edge_dy = p2[1] - p1[1]
        edge_len = math.hypot(edge_dx, edge_dy)
        if edge_len <= EPS:
            dx, dy = 1.0, 0.0
        else:
            dx, dy = -edge_dy / edge_len, edge_dx / edge_len
    else:
        dx, dy = dx / length, dy / length

    from_center_dirs: list[tuple[float, float]] = []
    for target in (p1, p2, mid):
        tx = vx - target[0]
        ty = vy - target[1]
        tl = math.hypot(tx, ty)
        if tl > EPS:
            from_center_dirs.append((tx / tl, ty / tl))
    from_center_dirs.append((dx, dy))
    from_center_dirs.append((0.0, -1.0))
    from_center_dirs.append((0.0, 1.0))

    for sign in (1.0, -1.0):
        if vertex_id in movable:
            for ux, uy in from_center_dirs:
                adjusted = list(coords)
                adjusted[vertex_id] = (vx + ux * step * sign, vy + uy * step * sign)
                candidates.append(adjusted)
        if first in movable:
            direct_x = p1[0] - vx
            direct_y = p1[1] - vy
            direct_len = math.hypot(direct_x, direct_y)
            first_dirs: list[tuple[float, float]] = []
            if direct_len > EPS:
                first_dirs.append((direct_x / direct_len, direct_y / direct_len))
            first_dirs.extend(((0.0, -1.0), (0.0, 1.0), (1.0, 0.0), (-1.0, 0.0), (-dx, -dy), (dx, dy)))
            for ux, uy in first_dirs:
                adjusted = list(coords)
                x, y = adjusted[first]
                adjusted[first] = (x + ux * step * sign, y + uy * step * sign)
                candidates.append(adjusted)
            adjusted = list(coords)
            x, y = adjusted[first]
            adjusted[first] = (x - dx * step * sign, y - dy * step * sign)
            candidates.append(adjusted)
            adjusted = list(coords)
            x, y = adjusted[first]
            adjusted[first] = (x, y - step * sign)
            candidates.append(adjusted)
        if second in movable:
            direct_x = p2[0] - vx
            direct_y = p2[1] - vy
            direct_len = math.hypot(direct_x, direct_y)
            second_dirs: list[tuple[float, float]] = []
            if direct_len > EPS:
                second_dirs.append((direct_x / direct_len, direct_y / direct_len))
            second_dirs.extend(((0.0, -1.0), (0.0, 1.0), (1.0, 0.0), (-1.0, 0.0), (-dx, -dy), (dx, dy)))
            for ux, uy in second_dirs:
                adjusted = list(coords)
                x, y = adjusted[second]
                adjusted[second] = (x + ux * step * sign, y + uy * step * sign)
                candidates.append(adjusted)
            adjusted = list(coords)
            x, y = adjusted[second]
            adjusted[second] = (x - dx * step * sign, y - dy * step * sign)
            candidates.append(adjusted)
            adjusted = list(coords)
            x, y = adjusted[second]
            adjusted[second] = (x, y - step * sign)
            candidates.append(adjusted)
    return candidates


def border_is_external(border: BorderRecord) -> bool:
    return border.kind.lower() == "externalborders"


def border_is_water(border: BorderRecord) -> bool:
    return border.kind.lower() == "waterlevel"


def movable_vertex_ids(borders: Sequence[BorderRecord]) -> set[int]:
    material_vertices = {
        vertex_id
        for border in borders
        if border.kind.lower() == "materialborders"
        for vertex_id in border.vertex_ids
    }
    fixed_vertices = {
        vertex_id
        for border in borders
        if border.kind.lower() != "materialborders"
        for vertex_id in border.vertex_ids
    }
    return material_vertices - fixed_vertices


def clean_vertex_path(
    vertex_ids: Sequence[int],
    force_closed: bool = False,
) -> tuple[int, ...]:
    if not vertex_ids:
        return ()

    closed = force_closed or (len(vertex_ids) > 1 and vertex_ids[0] == vertex_ids[-1])
    working = list(vertex_ids[:-1] if closed and vertex_ids[0] == vertex_ids[-1] else vertex_ids)

    compact: list[int] = []
    for vertex_id in working:
        if not compact or compact[-1] != vertex_id:
            compact.append(vertex_id)

    if closed:
        while len(compact) > 1 and compact[0] == compact[-1]:
            compact.pop()
        if len(set(compact)) < 3:
            return ()
        compact.append(compact[0])
        return tuple(compact)

    return tuple(compact) if len(compact) >= 2 else ()


def rewrite_borders_after_merge(
    borders: Sequence[BorderRecord],
    old_to_root: Mapping[int, int],
) -> tuple[list[BorderRecord], int]:
    result: list[BorderRecord] = []
    removed = 0
    for border in borders:
        mapped = [old_to_root[vertex_id] for vertex_id in border.vertex_ids]
        cleaned = clean_vertex_path(mapped, force_closed=border_is_external(border))
        if not cleaned:
            if border_is_external(border):
                raise GeometryFixError("External border became degenerate after vertex merging.")
            removed += 1
            continue
        result.append(replace(border, vertex_ids=cleaned))
    return result, removed


def angle_between(
    center: tuple[float, float],
    first: tuple[float, float],
    second: tuple[float, float],
) -> float:
    v1 = (first[0] - center[0], first[1] - center[1])
    v2 = (second[0] - center[0], second[1] - center[1])
    l1 = math.hypot(v1[0], v1[1])
    l2 = math.hypot(v2[0], v2[1])
    if l1 <= EPS or l2 <= EPS:
        return 0.0
    cosine = max(-1.0, min(1.0, (v1[0] * v2[0] + v1[1] * v2[1]) / (l1 * l2)))
    return math.degrees(math.acos(cosine))


def border_graph(borders: Sequence[BorderRecord]) -> dict[int, set[int]]:
    graph: dict[int, set[int]] = defaultdict(set)
    for border in borders:
        for a, b in zip(border.vertex_ids, border.vertex_ids[1:]):
            if a == b:
                continue
            graph[a].add(b)
            graph[b].add(a)
    return graph


def vertex_border_uses(borders: Sequence[BorderRecord]) -> dict[int, set[int]]:
    uses: dict[int, set[int]] = defaultdict(set)
    for border_index, border in enumerate(borders):
        vertices = border.vertex_ids[:-1] if len(border.vertex_ids) > 1 and border.vertex_ids[0] == border.vertex_ids[-1] else border.vertex_ids
        for vertex_id in set(vertices):
            uses[vertex_id].add(border_index)
    return uses


def small_angle_count(
    coords: Sequence[tuple[float, float]],
    borders: Sequence[BorderRecord],
    minimum_angle: float,
) -> int:
    if minimum_angle <= 0:
        return 0

    graph = border_graph(borders)
    count = 0
    for vertex_id, neighbours in graph.items():
        ordered = sorted(neighbours)
        for i, first in enumerate(ordered):
            for second in ordered[i + 1:]:
                angle = angle_between(coords[vertex_id], coords[first], coords[second])
                if angle < minimum_angle - EPS:
                    count += 1
    return count


def geometry_quality(
    coords: Sequence[tuple[float, float]],
    borders: Sequence[BorderRecord],
    minimum_distance: float,
    minimum_angle: float,
) -> GeometryQuality:
    return GeometryQuality(
        close_pairs=len(close_vertex_pairs(coords, minimum_distance)),
        near_segments=len(near_fixed_segment_points(coords, borders, minimum_distance)),
        near_fixed_edges=len(near_fixed_segment_edges(coords, borders, minimum_distance)),
        near_material_segments=len(near_material_segment_points(coords, borders, minimum_distance)),
        small_angles=small_angle_count(coords, borders, minimum_angle),
    )


def removable_small_angle_positions(
    coords_by_id: Mapping[int, tuple[float, float]],
    borders: Sequence[BorderRecord],
    minimum_angle: float,
) -> dict[int, set[int]]:
    graph = border_graph(borders)
    uses = vertex_border_uses(borders)
    positions: dict[int, set[int]] = defaultdict(set)

    for border_index, border in enumerate(borders):
        closed = len(border.vertex_ids) > 1 and border.vertex_ids[0] == border.vertex_ids[-1]
        working = list(border.vertex_ids[:-1] if closed else border.vertex_ids)
        if len(working) < 3:
            continue

        candidate_range = range(len(working)) if closed else range(1, len(working) - 1)
        for position in candidate_range:
            vertex_id = working[position]
            if len(graph.get(vertex_id, ())) != 2 or len(uses.get(vertex_id, ())) != 1:
                continue
            prev_id = working[(position - 1) % len(working)]
            next_id = working[(position + 1) % len(working)]
            if prev_id == next_id:
                continue
            angle = angle_between(coords_by_id[vertex_id], coords_by_id[prev_id], coords_by_id[next_id])
            if angle < minimum_angle - EPS:
                positions[border_index].add(position)
    return positions


def remove_simple_small_angle_vertices(
    coords_by_id: Mapping[int, tuple[float, float]],
    borders: Sequence[BorderRecord],
    minimum_distance: float,
    minimum_angle: float,
    target_face_count: int | None = None,
    preserve_face_count: bool = True,
) -> tuple[list[BorderRecord], int, int]:
    if minimum_angle <= 0:
        return list(borders), 0, 0

    current = list(borders)
    removed_vertices = 0
    removed_borders = 0
    renumbered_coords, renumbered_current = renumber_vertices(coords_by_id, current)
    current_quality = geometry_quality(renumbered_coords, renumbered_current, minimum_distance, minimum_angle)

    while True:
        positions = removable_small_angle_positions(coords_by_id, current, minimum_angle)
        if not positions:
            return current, removed_vertices, removed_borders

        changed = False
        for border_index in sorted(positions):
            for position in sorted(positions[border_index]):
                if border_index >= len(current):
                    continue
                border = current[border_index]
                closed = len(border.vertex_ids) > 1 and border.vertex_ids[0] == border.vertex_ids[-1]
                working = list(border.vertex_ids[:-1] if closed else border.vertex_ids)
                if position >= len(working):
                    continue
                filtered = [vertex_id for i, vertex_id in enumerate(working) if i != position]
                cleaned = clean_vertex_path(filtered, force_closed=closed or border_is_external(border))

                candidate = current[:]
                candidate_removed_border = False
                if not cleaned:
                    if border_is_external(border):
                        continue
                    del candidate[border_index]
                    candidate_removed_border = True
                else:
                    candidate[border_index] = replace(border, vertex_ids=cleaned)

                if preserve_face_count and target_face_count is not None:
                    try:
                        candidate_face_count = geometry_face_count_from_root_ids(coords_by_id, candidate)
                    except GeometryFixError:
                        candidate_face_count = None
                    if candidate_face_count != target_face_count:
                        continue

                renumbered_coords, renumbered_candidate = renumber_vertices(coords_by_id, candidate)
                candidate_quality = geometry_quality(
                    renumbered_coords,
                    renumbered_candidate,
                    minimum_distance,
                    minimum_angle,
                )
                if candidate_quality.errors > current_quality.errors:
                    continue

                current = candidate
                current_quality = candidate_quality
                removed_vertices += 1
                if candidate_removed_border:
                    removed_borders += 1
                changed = True
                break
            if changed:
                break

        if not changed:
            return current, removed_vertices, removed_borders


def removable_collinear_close_positions(
    coords_by_id: Mapping[int, tuple[float, float]],
    borders: Sequence[BorderRecord],
    minimum_distance: float,
    angle_tolerance: float = 15.0,
) -> dict[int, set[int]]:
    if minimum_distance <= 0:
        return {}

    graph = border_graph(borders)
    uses = vertex_border_uses(borders)
    positions: dict[int, set[int]] = defaultdict(set)

    for border_index, border in enumerate(borders):
        closed = len(border.vertex_ids) > 1 and border.vertex_ids[0] == border.vertex_ids[-1]
        working = list(border.vertex_ids[:-1] if closed else border.vertex_ids)
        if len(working) < 3:
            continue

        candidate_range = range(len(working)) if closed else range(1, len(working) - 1)
        for position in candidate_range:
            vertex_id = working[position]
            if len(graph.get(vertex_id, ())) != 2 or len(uses.get(vertex_id, ())) != 1:
                continue
            prev_id = working[(position - 1) % len(working)]
            next_id = working[(position + 1) % len(working)]
            if prev_id == next_id:
                continue
            prev_distance = math.hypot(
                coords_by_id[vertex_id][0] - coords_by_id[prev_id][0],
                coords_by_id[vertex_id][1] - coords_by_id[prev_id][1],
            )
            next_distance = math.hypot(
                coords_by_id[vertex_id][0] - coords_by_id[next_id][0],
                coords_by_id[vertex_id][1] - coords_by_id[next_id][1],
            )
            if min(prev_distance, next_distance) > minimum_distance + EPS:
                continue
            angle = angle_between(coords_by_id[vertex_id], coords_by_id[prev_id], coords_by_id[next_id])
            if abs(180.0 - angle) <= angle_tolerance:
                positions[border_index].add(position)
    return positions


def remove_collinear_close_vertices(
    coords_by_id: Mapping[int, tuple[float, float]],
    borders: Sequence[BorderRecord],
    minimum_distance: float,
    minimum_angle: float,
    target_face_count: int | None = None,
    preserve_face_count: bool = True,
) -> tuple[list[BorderRecord], int, int]:
    current = list(borders)
    removed_vertices = 0
    removed_borders = 0
    renumbered_coords, renumbered_current = renumber_vertices(coords_by_id, current)
    current_quality = geometry_quality(renumbered_coords, renumbered_current, minimum_distance, minimum_angle)

    while True:
        positions = removable_collinear_close_positions(coords_by_id, current, minimum_distance)
        if not positions:
            return current, removed_vertices, removed_borders

        changed = False
        for border_index in sorted(positions):
            for position in sorted(positions[border_index]):
                if border_index >= len(current):
                    continue
                border = current[border_index]
                closed = len(border.vertex_ids) > 1 and border.vertex_ids[0] == border.vertex_ids[-1]
                working = list(border.vertex_ids[:-1] if closed else border.vertex_ids)
                if position >= len(working):
                    continue
                filtered = [vertex_id for i, vertex_id in enumerate(working) if i != position]
                cleaned = clean_vertex_path(filtered, force_closed=closed or border_is_external(border))

                candidate = current[:]
                candidate_removed_border = False
                if not cleaned:
                    if border_is_external(border):
                        continue
                    del candidate[border_index]
                    candidate_removed_border = True
                else:
                    candidate[border_index] = replace(border, vertex_ids=cleaned)

                if preserve_face_count and target_face_count is not None:
                    try:
                        candidate_face_count = geometry_face_count_from_root_ids(coords_by_id, candidate)
                    except GeometryFixError:
                        candidate_face_count = None
                    if candidate_face_count != target_face_count:
                        continue

                renumbered_coords, renumbered_candidate = renumber_vertices(coords_by_id, candidate)
                candidate_quality = geometry_quality(
                    renumbered_coords,
                    renumbered_candidate,
                    minimum_distance,
                    minimum_angle,
                )
                if candidate_quality.errors > current_quality.errors:
                    continue

                current = candidate
                current_quality = candidate_quality
                removed_vertices += 1
                if candidate_removed_border:
                    removed_borders += 1
                changed = True
                break
            if changed:
                break

        if not changed:
            return current, removed_vertices, removed_borders


def renumber_vertices(
    root_to_coord: Mapping[int, tuple[float, float]],
    borders: Sequence[BorderRecord],
) -> tuple[list[tuple[float, float]], list[BorderRecord]]:
    used_roots = {vertex_id for border in borders for vertex_id in border.vertex_ids}
    ordered_roots = sorted(used_roots)
    root_to_new = {root: index for index, root in enumerate(ordered_roots)}
    new_coords = [root_to_coord[root] for root in ordered_roots]
    new_borders = [
        replace(border, border_id=index, vertex_ids=tuple(root_to_new[vertex_id] for vertex_id in border.vertex_ids))
        for index, border in enumerate(borders)
    ]
    return new_coords, new_borders


def polygon_signed_area(coords: Sequence[tuple[float, float]], vertex_ids: Sequence[int]) -> float:
    total = 0.0
    for a, b in zip(vertex_ids, list(vertex_ids[1:]) + [vertex_ids[0]]):
        x1, y1 = coords[a]
        x2, y2 = coords[b]
        total += x1 * y2 - x2 * y1
    return total * 0.5


def trace_geometry_faces(
    coords: Sequence[tuple[float, float]],
    borders: Sequence[BorderRecord],
) -> list[list[int]]:
    edge_set: set[tuple[int, int]] = set()
    for border in borders:
        if border_is_water(border):
            continue
        path = list(border.vertex_ids)
        if border_is_external(border) and path and path[-1] != path[0]:
            path.append(path[0])
        for a, b in zip(path, path[1:]):
            if a != b:
                edge_set.add(tuple(sorted((a, b))))

    if not edge_set:
        return []

    graph: dict[int, list[int]] = defaultdict(list)
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

    visited: set[tuple[int, int]] = set()
    faces: list[list[int]] = []
    max_steps = len(edge_set) * 2 + 1
    for start in sorted(graph):
        for first in graph[start]:
            if (start, first) in visited:
                continue
            face: list[int] = []
            prev, cur = start, first
            steps = 0
            while (prev, cur) not in visited:
                visited.add((prev, cur))
                face.append(prev)
                neighbours = graph[cur]
                try:
                    incoming_idx = neighbours.index(prev)
                except ValueError as exc:
                    raise GeometryFixError("Broken geometry graph while tracing faces.") from exc
                nxt = neighbours[(incoming_idx - 1) % len(neighbours)]
                prev, cur = cur, nxt
                steps += 1
                if steps > max_steps:
                    raise GeometryFixError("Geometry face walk did not close.")
            if len(face) >= 3:
                faces.append(face)
    return faces


def geometry_face_count(
    coords: Sequence[tuple[float, float]],
    borders: Sequence[BorderRecord],
) -> int | None:
    faces = trace_geometry_faces(coords, borders)
    if not faces:
        return None
    positive = sum(1 for face in faces if polygon_signed_area(coords, face) > 0.0)
    if positive:
        return positive
    return max(0, len(faces) - 1)


def geometry_face_count_from_root_ids(
    coords_by_id: Mapping[int, tuple[float, float]],
    borders: Sequence[BorderRecord],
) -> int | None:
    used_ids = sorted({vertex_id for border in borders for vertex_id in border.vertex_ids})
    old_to_new = {vertex_id: index for index, vertex_id in enumerate(used_ids)}
    coords = [coords_by_id[vertex_id] for vertex_id in used_ids]
    renumbered = [
        replace(border, vertex_ids=tuple(old_to_new[vertex_id] for vertex_id in border.vertex_ids))
        for border in borders
    ]
    return geometry_face_count(coords, renumbered)


def _face_envelope_key(
    coords: Sequence[tuple[float, float]],
    face: Sequence[int],
) -> tuple[float, float, float, float]:
    points = [coords[vertex_id] for vertex_id in face]
    return (
        min(x for x, _ in points),
        min(y for _, y in points),
        max(x for x, _ in points),
        max(y for _, y in points),
    )


def _face_area_centroid(
    coords: Sequence[tuple[float, float]],
    face: Sequence[int],
) -> tuple[float, tuple[float, float]]:
    signed_area = polygon_signed_area(coords, face)
    area = abs(signed_area)
    if area <= EPS:
        first = coords[face[0]]
        return 0.0, first
    cross_total = 0.0
    x_total = 0.0
    y_total = 0.0
    for first, second in zip(face, list(face[1:]) + [face[0]]):
        x1, y1 = coords[first]
        x2, y2 = coords[second]
        cross = x1 * y2 - x2 * y1
        cross_total += cross
        x_total += (x1 + x2) * cross
        y_total += (y1 + y2) * cross
    if abs(cross_total) <= EPS:
        return area, coords[face[0]]
    return area, (x_total / (3.0 * cross_total), y_total / (3.0 * cross_total))


def _positive_faces_in_prorock_order(
    coords: Sequence[tuple[float, float]],
    borders: Sequence[BorderRecord],
) -> list[list[int]]:
    faces = [
        face
        for face in trace_geometry_faces(coords, borders)
        if polygon_signed_area(coords, face) > EPS
    ]
    return sorted(faces, key=lambda face: _face_envelope_key(coords, face))


def _reorder_area_rows_for_geometry(
    document: PriDocument,
    vertices: Sequence[tuple[float, float]],
    borders: Sequence[BorderRecord],
) -> PriDocument:
    """Keep PRI material rows attached to their original CAD regions.

    ProRock orders reconstructed polygons by envelope.  Cleanup can move a
    point by millimetres and change that order, while the ``areas`` records
    still refer to the old order.  Match old and new regions by centroid and
    area, then rewrite the records in the new polygon order.
    """
    if document.area_count is None:
        return document
    lines = list(document.suffix)
    section_index = next(
        (index for index, line in enumerate(lines) if section_count(line, "areas") is not None),
        None,
    )
    if section_index is None:
        return document
    count = section_count(lines[section_index], "areas")
    assert count is not None
    row_start = section_index + 1
    row_end = row_start + count
    if row_end > len(lines):
        raise GeometryFixError("PRI areas section ends before its declared count.")

    old_faces = _positive_faces_in_prorock_order(document.vertices, document.borders)
    new_faces = _positive_faces_in_prorock_order(vertices, borders)
    if len(old_faces) != count or len(new_faces) != count:
        # Do not guess when topology changed. The caller will retain the
        # original area order and report the geometry topology warning.
        return document

    old_info = [_face_area_centroid(document.vertices, face) for face in old_faces]
    new_info = [_face_area_centroid(vertices, face) for face in new_faces]
    candidates: list[tuple[float, int, int]] = []
    for new_index, (new_area, (new_x, new_y)) in enumerate(new_info):
        for old_index, (old_area, (old_x, old_y)) in enumerate(old_info):
            scale = max(math.sqrt(max(old_area, 0.0)), math.sqrt(max(new_area, 0.0)), 1.0)
            distance_score = ((new_x - old_x) ** 2 + (new_y - old_y) ** 2) / (scale * scale)
            area_score = abs(new_area - old_area) / max(old_area, new_area, 1.0)
            candidates.append((distance_score + area_score * 0.25, new_index, old_index))

    assigned_new: set[int] = set()
    assigned_old: set[int] = set()
    match: dict[int, int] = {}
    for _, new_index, old_index in sorted(candidates):
        if new_index in assigned_new or old_index in assigned_old:
            continue
        match[new_index] = old_index
        assigned_new.add(new_index)
        assigned_old.add(old_index)
        if len(match) == count:
            break
    if len(match) != count:
        raise GeometryFixError("Could not preserve PRI material assignments after geometry cleanup.")

    original_rows = lines[row_start:row_end]
    reordered_rows = [original_rows[match[index]] for index in range(count)]
    if reordered_rows == original_rows:
        return document
    lines[row_start:row_end] = reordered_rows
    return replace(document, suffix=tuple(lines))


def render_pri(
    document: PriDocument,
    vertices: Sequence[tuple[float, float]],
    borders: Sequence[BorderRecord],
    renumber_border_ids: bool = False,
) -> str:
    lines: list[str] = list(document.prefix)
    lines.append(f"vertices: {len(vertices)}")
    for x, y in vertices:
        lines.append(f"\t{fmt_number(x)}\t{fmt_number(y)}")
    lines.append("")

    lines.append(f"borders: {len(borders)}")
    for index, border in enumerate(borders):
        lines.append(border.header_line)
        vertex_ids = "\t".join(str(vertex_id) for vertex_id in border.vertex_ids)
        border_id = index if renumber_border_ids else border.border_id
        lines.append(f"\t{border_id}\t{vertex_ids}")

    if document.suffix and document.suffix[0].strip():
        lines.append("")
    lines.extend(document.suffix)
    return document.line_ending.join(lines) + document.line_ending


def _cyclic_path(vertex_ids: Sequence[int], start: int, end: int, step: int) -> list[int]:
    result = [vertex_ids[start]]
    index = start
    while index != end:
        index = (index + step) % len(vertex_ids)
        result.append(vertex_ids[index])
        if len(result) > len(vertex_ids):
            raise GeometryFixError("External border does not form a closed path.")
    return result


def _pit_ramp_candidate(
    coords: Sequence[tuple[float, float]],
    vertex_ids: Sequence[int],
    side: str,
) -> tuple[int, int, int, list[int]] | None:
    """Find a pit ramp from a low side wall to its last bench slope.

    The topographic surface often continues above the pit.  It must not be
    treated as the crest.  Open-pit bench faces are recognised by their
    30--50 degree incline; the upper endpoint of the last ascending face in
    that range is the excavation crest.
    """
    points = [coords[vertex_id] for vertex_id in vertex_ids]
    min_x, max_x = min(x for x, _ in points), max(x for x, _ in points)
    min_y, max_y = min(y for _, y in points), max(y for _, y in points)
    width, height = max_x - min_x, max_y - min_y
    if width <= EPS or height <= EPS:
        return None
    side_x = min_x if side == "left" else max_x
    side_tolerance = max(EPS * 10.0, width * 1e-6)
    floor_limit = max_y - height * 0.05
    candidates = [
        index
        for index, (x, y) in enumerate(points)
        if abs(x - side_x) <= side_tolerance and min_y + height * 0.02 < y < floor_limit
    ]
    if not candidates:
        return None
    floor_index = max(candidates, key=lambda index: points[index][1])
    inward_sign = 1.0 if side == "left" else -1.0
    neighbours = ((floor_index - 1) % len(vertex_ids), (floor_index + 1) % len(vertex_ids))
    inward = [
        index
        for index in neighbours
        if (points[index][0] - side_x) * inward_sign > side_tolerance
    ]
    if len(inward) != 1:
        return None
    step = 1 if inward[0] == (floor_index + 1) % len(vertex_ids) else -1
    ramp_indices = [floor_index]
    index = floor_index
    # A real bench face has a meaningful vertical extent.  Short fragments on
    # the ground surface can have a similar local angle, but must not move the
    # excavation crest.  Scale the threshold to the model height so it works
    # for models in different coordinate systems.
    minimum_bench_rise = height * 0.02
    last_bench_crest_index: int | None = None
    bench_start_index: int | None = None
    bench_end_index: int | None = None

    def finish_bench_face() -> None:
        nonlocal last_bench_crest_index, bench_start_index, bench_end_index
        if bench_start_index is not None and bench_end_index is not None:
            face_rise = points[bench_end_index][1] - points[bench_start_index][1]
            if face_rise >= minimum_bench_rise:
                last_bench_crest_index = bench_end_index
        bench_start_index = None
        bench_end_index = None

    while len(ramp_indices) <= len(vertex_ids):
        next_index = (index + step) % len(vertex_ids)
        if next_index == floor_index:
            break
        first_x, first_y = points[index]
        second_x, second_y = points[next_index]
        rise = second_y - first_y
        run = abs(second_x - first_x)
        incline = math.degrees(math.atan2(abs(rise), run)) if run > EPS or abs(rise) > EPS else 0.0
        # Travel direction is from the pit floor upward.  Ignore descending
        # faces and shallow topographic surface segments.
        # Allow a small numerical tolerance: Slide exports nominal 30-degree
        # bench faces as 29.98... degrees in this model.
        if rise > EPS and 29.0 <= incline <= 51.0:
            if bench_start_index is None:
                bench_start_index = index
            bench_end_index = next_index
        else:
            finish_bench_face()
        index = next_index
        ramp_indices.append(index)
    finish_bench_face()
    if last_bench_crest_index is None:
        return None
    crest_index = last_bench_crest_index
    crest_path_position = ramp_indices.index(crest_index)
    ramp_indices = ramp_indices[: crest_path_position + 1]
    floor_y = points[floor_index][1]
    crest_y = points[crest_index][1]
    if crest_y - floor_y < height * 0.20:
        return None
    # A pit ramp may have short, nearly-flat berms, but it must not descend
    # materially while climbing from floor to the final bench crest.
    if min(points[index][1] for index in ramp_indices) < floor_y - height * 0.02:
        return None
    return floor_index, crest_index, step, ramp_indices


def _insert_bench_intersections(
    coords: list[tuple[float, float]],
    ramp_vertex_ids: Sequence[int],
    elevations: Sequence[float],
) -> tuple[list[int], list[int]]:
    """Split the preserved ramp at each bench elevation."""
    inserted_by_elevation: dict[float, int] = {}
    result = [ramp_vertex_ids[0]]
    for first, second in zip(ramp_vertex_ids, ramp_vertex_ids[1:]):
        p1, p2 = coords[first], coords[second]
        intersections: list[tuple[float, float, int]] = []
        for elevation in elevations:
            if elevation in inserted_by_elevation:
                continue
            if abs(p1[1] - elevation) <= EPS:
                inserted_by_elevation[elevation] = first
                continue
            if abs(p2[1] - elevation) <= EPS:
                inserted_by_elevation[elevation] = second
                continue
            if (p1[1] - elevation) * (p2[1] - elevation) < 0.0:
                ratio = (elevation - p1[1]) / (p2[1] - p1[1])
                vertex_id = len(coords)
                coords.append((p1[0] + (p2[0] - p1[0]) * ratio, elevation))
                inserted_by_elevation[elevation] = vertex_id
                intersections.append((ratio, elevation, vertex_id))
        for _, _, vertex_id in sorted(intersections):
            result.append(vertex_id)
        result.append(second)
    try:
        return result, [inserted_by_elevation[elevation] for elevation in elevations]
    except KeyError as exc:
        raise GeometryFixError("A pit bench does not intersect the detected ramp.") from exc


def _pit_bench_crest_ids(
    coords: Sequence[tuple[float, float]],
    ramp_vertex_ids: Sequence[int],
) -> list[int]:
    """Return existing ramp vertices at the upper end of bench faces.

    Stage borders must start from an original pit-bench point.  Splitting a
    sloping CAD line at an arbitrary elevation can create a very short angle
    at the new point.  A bench crest is the last vertex of one or more
    ascending 30--50 degree segments before a berm or the next bench begins.
    """
    crests: list[int] = []
    on_bench_face = False
    for first_id, second_id in zip(ramp_vertex_ids, ramp_vertex_ids[1:]):
        first_x, first_y = coords[first_id]
        second_x, second_y = coords[second_id]
        rise = second_y - first_y
        run = abs(second_x - first_x)
        incline = math.degrees(math.atan2(rise, run)) if rise > EPS else 0.0
        is_bench_face = rise > EPS and 25.0 <= incline <= 55.0
        if is_bench_face:
            on_bench_face = True
        elif on_bench_face:
            crests.append(first_id)
            on_bench_face = False
    if on_bench_face:
        crests.append(ramp_vertex_ids[-1])
    return crests


def _select_stage_bench_ids(
    coords: Sequence[tuple[float, float]],
    ramp_vertex_ids: Sequence[int],
    stage_count: int,
) -> list[int]:
    """Choose existing bench crests nearest to evenly spaced elevations."""
    if stage_count == 1:
        return []
    candidates = _pit_bench_crest_ids(coords, ramp_vertex_ids)
    candidates = list(dict.fromkeys(candidates))
    required = stage_count - 1
    if len(candidates) < required:
        raise GeometryFixError(
            f"The selected pit wall has only {len(candidates)} usable bench point(s); "
            f"choose at most {len(candidates) + 1} excavation stages."
        )
    floor_y = coords[ramp_vertex_ids[0]][1]
    crest_y = coords[ramp_vertex_ids[-1]][1]
    remaining = set(candidates)
    selected: list[int] = []
    for part in range(1, stage_count):
        target_y = floor_y + (crest_y - floor_y) * part / stage_count
        candidate = min(remaining, key=lambda vertex_id: abs(coords[vertex_id][1] - target_y))
        selected.append(candidate)
        remaining.remove(candidate)
    return sorted(selected, key=lambda vertex_id: coords[vertex_id][1])


def _external_vertex_at_point(
    coords: Sequence[tuple[float, float]],
    vertex_ids: Sequence[int],
    point: tuple[float, float],
) -> int:
    """Find the contour vertex selected by the user as excavation top."""
    x, y = point
    index = min(
        range(len(vertex_ids)),
        key=lambda candidate: math.hypot(coords[vertex_ids[candidate]][0] - x, coords[vertex_ids[candidate]][1] - y),
    )
    min_x = min(coords[vertex_id][0] for vertex_id in vertex_ids)
    max_x = max(coords[vertex_id][0] for vertex_id in vertex_ids)
    min_y = min(coords[vertex_id][1] for vertex_id in vertex_ids)
    max_y = max(coords[vertex_id][1] for vertex_id in vertex_ids)
    tolerance = max(0.02, math.hypot(max_x - min_x, max_y - min_y) * 1e-5)
    matched = coords[vertex_ids[index]]
    if math.hypot(matched[0] - x, matched[1] - y) > tolerance:
        raise GeometryFixError(
            "Excavation top point must match an ExternalBorders vertex "
            f"(nearest: {fmt_number(matched[0])}, {fmt_number(matched[1])})."
        )
    return index


def _excavated_area_property(document: PriDocument) -> tuple[PriDocument, int]:
    """Rename the unused converter fallback material for excavation stages."""
    lines = list(document.prefix)
    property_index: int | None = None
    for index, line in enumerate(lines):
        count = section_count(line, "element properties")
        if count is None:
            continue
        row_start = index + 1
        row_end = row_start + count * 2
        if row_end > len(lines) or count < 1:
            raise GeometryFixError("PRI element properties section is incomplete.")
        for property_id in range(count):
            name_index = row_start + property_id * 2
            if lines[name_index].strip().lower() == "new material":
                lines[name_index] = "\texcatated_area"
                property_index = property_id
                break
        break
    if property_index is None:
        raise GeometryFixError(
            "Excavation stages require the converter's reserved 'New material' property."
        )

    for index, line in enumerate(lines):
        count = section_count(line, "joint properties")
        if count is None:
            continue
        row_start = index + 1
        row_end = row_start + count * 2
        if row_end > len(lines):
            raise GeometryFixError("PRI joint properties section is incomplete.")
        for joint_id in range(count):
            name_index = row_start + joint_id * 2
            if lines[name_index].strip().lower() == "new material":
                lines[name_index] = "\texcatated_area"
                break
        break
    return replace(document, prefix=tuple(lines)), property_index


def _area_rows_for_excavation_stages(
    document: PriDocument,
    vertices: Sequence[tuple[float, float]],
    borders: Sequence[BorderRecord],
    stage_vertex_ids: set[int],
    excavation_property: int,
) -> PriDocument:
    """Preserve original material rows and assign only new faces to excavation."""
    lines = list(document.suffix)
    section_index = next(
        (index for index, line in enumerate(lines) if section_count(line, "areas") is not None),
        None,
    )
    if section_index is None:
        raise GeometryFixError("PRI file is missing an areas section.")
    count = section_count(lines[section_index], "areas")
    assert count is not None
    row_start = section_index + 1
    row_end = row_start + count
    if row_end > len(lines):
        raise GeometryFixError("PRI areas section is incomplete.")

    original_rows = lines[row_start:row_end]
    old_faces = _positive_faces_in_prorock_order(document.vertices, document.borders)
    new_faces = _positive_faces_in_prorock_order(vertices, borders)
    new_stage_indexes = [
        index for index, face in enumerate(new_faces) if any(vertex_id in stage_vertex_ids for vertex_id in face)
    ]
    if len(old_faces) != count or len(new_stage_indexes) == 0 or len(new_faces) != count + len(new_stage_indexes):
        raise GeometryFixError("Excavation stages did not produce a safe one-to-one PRI area mapping.")

    old_info = [_face_area_centroid(document.vertices, face) for face in old_faces]
    remaining_old = set(range(len(old_faces)))
    result_rows: list[str | None] = [None] * len(new_faces)
    excavation_row = f"\t0\t{excavation_property}\t{excavation_property}"
    for index in new_stage_indexes:
        result_rows[index] = excavation_row

    for new_index, face in enumerate(new_faces):
        if result_rows[new_index] is not None:
            continue
        new_area, (new_x, new_y) = _face_area_centroid(vertices, face)
        old_index = min(
            remaining_old,
            key=lambda candidate: (
                ((new_x - old_info[candidate][1][0]) ** 2 + (new_y - old_info[candidate][1][1]) ** 2)
                / max(new_area, old_info[candidate][0], 1.0)
                + abs(new_area - old_info[candidate][0]) / max(new_area, old_info[candidate][0], 1.0) * 0.25
            ),
        )
        result_rows[new_index] = original_rows[old_index]
        remaining_old.remove(old_index)

    if remaining_old or any(row is None for row in result_rows):
        raise GeometryFixError("Could not preserve all original material assignments for excavation stages.")
    lines[section_index] = f"areas: {len(result_rows)}"
    lines[row_start:row_end] = [row for row in result_rows if row is not None]
    return replace(document, suffix=tuple(lines), area_count=len(result_rows))


def add_excavation_stages(
    input_path: Path,
    output_path: Path | None = None,
    stage_count: int = 4,
    top_point: tuple[float, float] | None = None,
    progress: ProgressCallback | None = None,
) -> PitBenchSummary:
    """Extend the detected pit wall and split the preserved ramp into stages.

    The original pit ramp is retained as a MaterialBorders path.  The outer
    contour is extended from the model edge at the pit floor to a new crest,
    allowing four horizontal MaterialBorders to be formed against that ramp.
    """
    if stage_count < 1:
        raise GeometryFixError("Excavation stage count must be at least 1.")
    if top_point is None:
        raise GeometryFixError("Excavation stages require a user-selected top point (X and Y).")
    input_path = input_path.expanduser()
    output_path = output_path.expanduser() if output_path is not None else input_path
    emit_progress(progress, "pit benches: reading PRI geometry")
    document = read_pri(input_path)
    external_indexes = [index for index, border in enumerate(document.borders) if border_is_external(border)]
    if len(external_indexes) != 1:
        raise GeometryFixError("Pit benches require exactly one ExternalBorders path.")
    external_index = external_indexes[0]
    external = document.borders[external_index]
    external_ids = list(external.vertex_ids)
    if len(external_ids) > 1 and external_ids[0] == external_ids[-1]:
        external_ids.pop()
    if len(external_ids) < 4:
        raise GeometryFixError("ExternalBorders path is too short to detect a pit wall.")
    crest_index = _external_vertex_at_point(document.vertices, external_ids, top_point)

    candidates = [
        (side, candidate)
        for side in ("left", "right")
        if (candidate := _pit_ramp_candidate(document.vertices, external_ids, side)) is not None
    ]
    if not candidates:
        raise GeometryFixError(
            "Could not detect a pit ramp connected to the left or right model edge."
        )
    # The user explicitly selects the crest. Automatic detection is used only
    # to identify the connected left/right pit floor and travel direction.
    eligible = [
        (side, candidate)
        for side, candidate in candidates
        if document.vertices[external_ids[crest_index]][1]
        > document.vertices[external_ids[candidate[0]]][1] + EPS
    ]
    if not eligible:
        raise GeometryFixError("Excavation top point must be above the detected pit floor.")
    side, (floor_index, _, step, _) = max(
        eligible,
        key=lambda item: document.vertices[external_ids[crest_index]][1]
        - document.vertices[external_ids[item[1][0]]][1],
    )
    ramp_indexes = []
    index = floor_index
    while True:
        ramp_indexes.append(index)
        if index == crest_index:
            break
        index = (index + step) % len(external_ids)
        if len(ramp_indexes) > len(external_ids):
            raise GeometryFixError("Excavation top point is not reachable along the detected pit wall.")
    floor_id, crest_id = external_ids[floor_index], external_ids[crest_index]
    floor_y = document.vertices[floor_id][1]
    crest_y = document.vertices[crest_id][1]
    side_x = min(document.vertices[vertex_id][0] for vertex_id in external_ids) if side == "left" else max(
        document.vertices[vertex_id][0] for vertex_id in external_ids
    )
    coords = list(document.vertices)
    ramp_ids = [external_ids[index] for index in ramp_indexes]
    ramp_bench_ids = _select_stage_bench_ids(coords, ramp_ids, stage_count)
    wall_bench_ids: list[int] = []
    for ramp_bench_id in ramp_bench_ids:
        wall_bench_ids.append(len(coords))
        _, elevation = coords[ramp_bench_id]
        coords.append((side_x, elevation))
    crest_wall_id = len(coords)
    coords.append((side_x, crest_y))

    other_side = _cyclic_path(external_ids, crest_index, floor_index, step)
    new_external_ids = [floor_id, *wall_bench_ids, crest_wall_id, *other_side]
    new_external = replace(external, vertex_ids=tuple(new_external_ids))
    next_border_id = max(border.border_id for border in document.borders) + 1
    new_borders = list(document.borders)
    new_borders[external_index] = new_external
    new_borders.append(BorderRecord("\tMaterialBorders\t-1", "MaterialBorders", next_border_id, tuple(ramp_ids)))
    for wall_id, ramp_id in zip(wall_bench_ids, ramp_bench_ids):
        next_border_id += 1
        new_borders.append(BorderRecord("\tMaterialBorders\t-1", "MaterialBorders", next_border_id, (wall_id, ramp_id)))

    old_faces = geometry_face_count(document.vertices, document.borders)
    new_faces = geometry_face_count(coords, new_borders)
    if old_faces is None or new_faces != old_faces + stage_count:
        raise GeometryFixError(
            "Detected pit geometry did not create the requested number of new areas; no changes were written."
        )
    updated_document, excavation_property = _excavated_area_property(document)
    stage_vertex_ids = set(wall_bench_ids)
    stage_vertex_ids.add(crest_wall_id)
    updated_document = _area_rows_for_excavation_stages(
        updated_document,
        coords,
        new_borders,
        stage_vertex_ids,
        excavation_property,
    )
    payload = render_pri(updated_document, coords, new_borders, renumber_border_ids=True)
    emit_progress(
        progress,
        "excavation stages: detected "
        f"{side} floor {fmt_number(floor_y)} m, user top {fmt_number(document.vertices[crest_id][0])}, {fmt_number(crest_y)} m",
    )
    emit_progress(progress, "excavation stages: using original pit-bench points for stage borders")
    emit_progress(progress, f"pit benches: writing {stage_count} excavation stages with excatated_area")
    if input_path.resolve() == output_path.resolve():
        temporary = output_path.with_name(output_path.name + ".pit.tmp")
        write_output(temporary, payload, document.encoding)
        temporary.replace(output_path)
    else:
        write_output(output_path, payload, document.encoding)
    return PitBenchSummary(side, floor_y, crest_y, stage_count)


def add_four_pit_benches(
    input_path: Path,
    output_path: Path | None = None,
    progress: ProgressCallback | None = None,
) -> PitBenchSummary:
    """Compatibility wrapper for the former four-stage excavation option."""
    raise GeometryFixError("Use add_excavation_stages with an explicit excavation top point.")


_BOLT_BORDER_KINDS = frozenset(
    {
        "bolt",
        "boltborders",
        "anchor",
        "anchorborders",
        "support",
        "supportborders",
    }
)


def remove_bolt_geometry(
    input_path: Path,
    output_path: Path | None = None,
    progress: ProgressCallback | None = None,
) -> int:
    """Remove explicitly labelled bolt, anchor and support data from a PRI.

    MaterialBorders are deliberately retained: they describe lithology, not
    reinforcement.  The function also clears populated PRI bolt sections when
    present, so imported support definitions cannot leave visible bolt lines.
    """
    input_path = input_path.expanduser()
    output_path = output_path.expanduser() if output_path is not None else input_path
    emit_progress(progress, "bolt cleanup: reading PRI geometry")
    document = read_pri(input_path)

    retained_borders = [
        border
        for border in document.borders
        if border.kind.strip().lower() not in _BOLT_BORDER_KINDS
    ]
    removed_borders = len(document.borders) - len(retained_borders)

    suffix = list(document.suffix)
    removed_rows = 0
    index = 0
    while index < len(suffix):
        for heading in ("bolt elements", "bolt properties"):
            count = section_count(suffix[index], heading)
            if count is None:
                continue
            if count:
                end = index + 1 + count
                if end > len(suffix):
                    raise GeometryFixError(
                        f"PRI {heading} section ends before its declared count."
                    )
                del suffix[index + 1:end]
                removed_rows += count
            suffix[index] = f"{heading}: 0"
            break
        index += 1

    removed = removed_borders + removed_rows
    if not removed:
        emit_progress(progress, "bolt cleanup: no bolt or anchor geometry found")
        return 0

    updated_document = replace(document, suffix=tuple(suffix))
    payload = render_pri(
        updated_document,
        document.vertices,
        retained_borders,
        renumber_border_ids=True,
    )
    emit_progress(
        progress,
        f"bolt cleanup: removing {removed_borders} border(s) and {removed_rows} bolt record(s)",
    )
    emit_progress(progress, "bolt cleanup: writing cleaned PRI")
    if input_path.resolve() == output_path.resolve():
        temporary = output_path.with_name(output_path.name + ".bolt.tmp")
        write_output(temporary, payload, document.encoding)
        temporary.replace(output_path)
    else:
        write_output(output_path, payload, document.encoding)
    return removed


def cleaned_pri_text(
    document: PriDocument,
    minimum_distance: float = DEFAULT_MIN_DISTANCE,
    minimum_angle: float = DEFAULT_MIN_ANGLE,
    fix_angles: bool = True,
    preserve_area_count: bool = True,
    progress: ProgressCallback | None = None,
) -> tuple[str, GeometryFixSummary]:
    emit_progress(progress, "geometry cleanup: checking input geometry")
    quality_before = geometry_quality(document.vertices, document.borders, minimum_distance, minimum_angle)
    emit_progress(
        progress,
        "geometry cleanup: input errors "
        f"{quality_before.errors} "
        f"(close={quality_before.close_pairs}, near outer={quality_before.near_segments}, "
        f"near fixed={quality_before.near_fixed_edges}, near material={quality_before.near_material_segments}, "
        f"angles={quality_before.small_angles})",
    )
    close_before = quality_before.close_pairs
    near_segments_before = quality_before.near_segments
    near_fixed_edges_before = quality_before.near_fixed_edges
    near_material_segments_before = quality_before.near_material_segments
    small_angles_before = quality_before.small_angles
    try:
        faces_before = geometry_face_count(document.vertices, document.borders)
    except GeometryFixError:
        faces_before = None

    target_face_count = faces_before if preserve_area_count else None
    if target_face_count is None:
        target_face_count = document.area_count

    emit_progress(progress, "geometry cleanup: merging close vertices")
    (
        old_to_root,
        root_to_coord,
        merged_vertices,
        merge_clusters,
        skipped_merge_pairs,
        spread_vertex_pairs,
        skipped_worse_merges,
        skipped_spread_pairs,
    ) = merge_map_for_close_vertices(
        document.vertices,
        minimum_distance,
        minimum_angle,
        borders=document.borders,
        target_face_count=target_face_count,
        preserve_face_count=preserve_area_count,
        progress=progress,
    )
    merged_borders, removed_after_merge = rewrite_borders_after_merge(document.borders, old_to_root)
    emit_progress(progress, f"geometry cleanup: merged {merged_vertices} vertices in {merge_clusters} cluster(s)")

    if fix_angles:
        emit_progress(progress, "geometry cleanup: removing simple small-angle vertices")
        fixed_borders, angle_vertices_removed, removed_after_angles = remove_simple_small_angle_vertices(
            root_to_coord,
            merged_borders,
            minimum_distance,
            minimum_angle,
            target_face_count=target_face_count,
            preserve_face_count=preserve_area_count,
        )
    else:
        fixed_borders = merged_borders
        angle_vertices_removed = 0
        removed_after_angles = 0

    emit_progress(progress, "geometry cleanup: removing close collinear path vertices")
    fixed_borders, collinear_vertices_removed, removed_after_collinear = remove_collinear_close_vertices(
        root_to_coord,
        fixed_borders,
        minimum_distance,
        minimum_angle,
        target_face_count=target_face_count,
        preserve_face_count=preserve_area_count,
    )

    fixed_vertices, renumbered_borders = renumber_vertices(root_to_coord, fixed_borders)
    emit_progress(progress, "geometry cleanup: spreading remaining bad geometry")
    fixed_vertices, renumbered_borders, spread_bad_parts, skipped_bad_part_spreads = spread_remaining_bad_geometry(
        fixed_vertices,
        renumbered_borders,
        minimum_distance,
        minimum_angle,
        target_face_count=target_face_count,
        preserve_face_count=preserve_area_count,
        progress=progress,
    )
    quality_after = geometry_quality(fixed_vertices, renumbered_borders, minimum_distance, minimum_angle)
    emit_progress(progress, f"geometry cleanup: final errors {quality_before.errors}->{quality_after.errors}")
    close_after = quality_after.close_pairs
    near_segments_after = quality_after.near_segments
    near_fixed_edges_after = quality_after.near_fixed_edges
    near_material_segments_after = quality_after.near_material_segments
    small_angles_after = quality_after.small_angles
    fixed_close_pairs_after = fixed_close_pair_count(fixed_vertices, renumbered_borders, minimum_distance)
    fixed_near_edges_after = fixed_near_edge_count(fixed_vertices, renumbered_borders, minimum_distance)
    try:
        faces_after = geometry_face_count(fixed_vertices, renumbered_borders)
    except GeometryFixError:
        faces_after = None

    warnings: list[str] = []
    if quality_after.errors > quality_before.errors:
        warnings.append(f"geometry errors increased from {quality_before.errors} to {quality_after.errors}")
    if skipped_worse_merges:
        warnings.append(f"{skipped_worse_merges} merge candidate(s) were skipped because errors would increase")
    unresolved_skipped_merges = max(0, skipped_merge_pairs - spread_vertex_pairs)
    if unresolved_skipped_merges and close_after:
        warnings.append(f"{unresolved_skipped_merges} close merge candidate(s) were skipped")
    if skipped_spread_pairs and close_after:
        warnings.append(f"{skipped_spread_pairs} close pair(s) could not be spread safely")
    if skipped_bad_part_spreads and quality_after.errors:
        warnings.append(f"{skipped_bad_part_spreads} bad geometry part(s) could not be spread safely")
    if close_after:
        warnings.append(f"{close_after} vertex pair(s) are still closer than {fmt_number(minimum_distance)} m")
    if fixed_close_pairs_after:
        warnings.append(f"{fixed_close_pairs_after} remaining close pair(s) are on fixed borders and were not moved")
    if near_segments_after:
        warnings.append(f"{near_segments_after} material vertex/outer segment distance(s) are still below {fmt_number(minimum_distance)} m")
    if near_fixed_edges_after:
        warnings.append(f"{near_fixed_edges_after} material/fixed border segment distance(s) are still below {fmt_number(minimum_distance)} m")
    if near_material_segments_after:
        warnings.append(f"{near_material_segments_after} material vertex/material segment distance(s) are still below {fmt_number(minimum_distance)} m")
    if fixed_near_edges_after:
        warnings.append(f"{fixed_near_edges_after} remaining material/fixed edge issue(s) touch a fixed endpoint and were not moved")
    if small_angles_after:
        warnings.append(f"{small_angles_after} geometry angle(s) are still below {fmt_number(minimum_angle)} deg")
    if document.area_count is not None and faces_after is not None and faces_after != document.area_count:
        warnings.append(
            "geometry face count after cleanup "
            f"({faces_after}) differs from declared PRI areas ({document.area_count})"
        )
    if faces_after is None:
        warnings.append("geometry faces could not be reconstructed for the post-cleanup check")

    updated_document = _reorder_area_rows_for_geometry(document, fixed_vertices, renumbered_borders)
    rendered = render_pri(updated_document, fixed_vertices, renumbered_borders, renumber_border_ids=True)
    fixed = rendered != render_pri(document, document.vertices, document.borders)
    summary = GeometryFixSummary(
        input_path="",
        output_path="",
        backup_path=None,
        minimum_distance=minimum_distance,
        minimum_angle=minimum_angle,
        fixed=fixed,
        errors_before=quality_before.errors,
        errors_after=quality_after.errors,
        vertices_before=len(document.vertices),
        vertices_after=len(fixed_vertices),
        borders_before=len(document.borders),
        borders_after=len(renumbered_borders),
        merged_vertices=merged_vertices,
        merge_clusters=merge_clusters,
        spread_vertex_pairs=spread_vertex_pairs,
        spread_bad_parts=spread_bad_parts,
        angle_vertices_removed=angle_vertices_removed,
        collinear_vertices_removed=collinear_vertices_removed,
        borders_removed=removed_after_merge + removed_after_angles + removed_after_collinear,
        skipped_merge_pairs=skipped_merge_pairs,
        skipped_worse_merges=skipped_worse_merges,
        skipped_spread_pairs=skipped_spread_pairs,
        skipped_bad_part_spreads=skipped_bad_part_spreads,
        fixed_close_pairs_after=fixed_close_pairs_after,
        fixed_near_edges_after=fixed_near_edges_after,
        close_pairs_before=close_before,
        close_pairs_after=close_after,
        near_segments_before=near_segments_before,
        near_segments_after=near_segments_after,
        near_fixed_edges_before=near_fixed_edges_before,
        near_fixed_edges_after=near_fixed_edges_after,
        near_material_segments_before=near_material_segments_before,
        near_material_segments_after=near_material_segments_after,
        small_angles_before=small_angles_before,
        small_angles_after=small_angles_after,
        areas_declared=document.area_count,
        faces_before=faces_before,
        faces_after=faces_after,
        warnings=tuple(warnings),
    )
    return rendered, summary


def write_output(path: Path, payload: str, encoding: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload.encode(encoding))
    except OSError as exc:
        raise GeometryFixError(f"Cannot write {path}: {exc}") from exc


def backup_path_for(output_path: Path) -> Path:
    return output_path.with_name(f"{output_path.stem}.base{output_path.suffix}")


def unique_backup_path(output_path: Path) -> Path:
    candidate = backup_path_for(output_path)
    if not candidate.exists():
        return candidate

    counter = 1
    while True:
        numbered = output_path.with_name(f"{output_path.stem}.base.{counter}{output_path.suffix}")
        if not numbered.exists():
            return numbered
        counter += 1


def save_base_copy(source_path: Path, output_path: Path) -> Path:
    backup_path = unique_backup_path(output_path)
    try:
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        backup_path.write_bytes(source_path.read_bytes())
    except OSError as exc:
        raise GeometryFixError(f"Cannot save base PRI copy to {backup_path}: {exc}") from exc
    return backup_path


def clean_pri_geometry(
    input_path: Path,
    output_path: Path | None = None,
    minimum_distance: float = DEFAULT_MIN_DISTANCE,
    minimum_angle: float = DEFAULT_MIN_ANGLE,
    fix_angles: bool = True,
    preserve_area_count: bool = True,
    dry_run: bool = False,
    progress: ProgressCallback | None = None,
) -> GeometryFixSummary:
    if minimum_distance < 0:
        raise GeometryFixError("Minimum distance must be non-negative.")
    if minimum_angle < 0 or minimum_angle >= 180:
        raise GeometryFixError("Minimum angle must be in the range [0, 180).")

    input_path = input_path.expanduser()
    output_path = output_path.expanduser() if output_path is not None else input_path
    emit_progress(progress, f"geometry cleanup: reading {input_path}")
    document = read_pri(input_path)
    payload, summary = cleaned_pri_text(
        document,
        minimum_distance=minimum_distance,
        minimum_angle=minimum_angle,
        fix_angles=fix_angles,
        preserve_area_count=preserve_area_count,
        progress=progress,
    )
    backup_path: Path | None = None

    if not dry_run:
        emit_progress(progress, "geometry cleanup: saving base copy")
        backup_path = save_base_copy(input_path, output_path)
        if input_path.resolve() == output_path.resolve():
            temp_path = output_path.with_name(output_path.name + ".tmp")
            emit_progress(progress, f"geometry cleanup: writing {output_path}")
            write_output(temp_path, payload, document.encoding)
            temp_path.replace(output_path)
        else:
            emit_progress(progress, f"geometry cleanup: writing {output_path}")
            write_output(output_path, payload, document.encoding)
    summary = replace(
        summary,
        input_path=str(input_path),
        output_path=str(output_path),
        backup_path=str(backup_path) if backup_path is not None else None,
    )
    return summary


def default_output_path(input_path: Path) -> Path:
    return input_path.with_name(f"{input_path.stem}_fixed{input_path.suffix}")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Clean ProRock PRI geometry by merging close CAD vertices and checking small angles.",
    )
    parser.add_argument("input", type=Path, help="Input .pri file")
    parser.add_argument("output", type=Path, nargs="?", help="Output .pri path; default: <input>_fixed.pri")
    parser.add_argument(
        "--in-place",
        action="store_true",
        help="Overwrite the input .pri after successful cleanup.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Check and report cleanup statistics without writing a PRI file.",
    )
    parser.add_argument(
        "--min-distance",
        type=float,
        default=DEFAULT_MIN_DISTANCE,
        help="Merge geometry vertices closer than this distance in metres. Default: 2.",
    )
    parser.add_argument(
        "--min-angle",
        type=float,
        default=DEFAULT_MIN_ANGLE,
        help="Report and optionally remove simple path angles smaller than this value in degrees. Default: 18.",
    )
    parser.add_argument(
        "--no-fix-angles",
        action="store_true",
        help="Only report small angles; do not remove simple non-junction angle vertices.",
    )
    parser.add_argument(
        "--allow-area-count-change",
        action="store_true",
        help="Allow cleanup steps that change the reconstructed CAD face count.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the cleanup summary as JSON.",
    )
    return parser


def print_summary(summary: GeometryFixSummary) -> None:
    print(f"OK: {summary.input_path} -> {summary.output_path}")
    if summary.backup_path is not None:
        print(f"  base copy:         {summary.backup_path}")
    print(f"  geometry errors:   {summary.errors_before} -> {summary.errors_after}")
    print(f"  vertices:          {summary.vertices_before} -> {summary.vertices_after}")
    print(f"  borders:           {summary.borders_before} -> {summary.borders_after}")
    print(f"  close pairs:       {summary.close_pairs_before} -> {summary.close_pairs_after}")
    print(f"  near segments:     {summary.near_segments_before} -> {summary.near_segments_after}")
    print(f"  near edges:        {summary.near_fixed_edges_before} -> {summary.near_fixed_edges_after}")
    print(f"  near material:     {summary.near_material_segments_before} -> {summary.near_material_segments_after}")
    print(f"  small angles:      {summary.small_angles_before} -> {summary.small_angles_after}")
    print(f"  merged vertices:   {summary.merged_vertices} in {summary.merge_clusters} cluster(s)")
    if summary.spread_vertex_pairs:
        print(f"  spread pairs:      {summary.spread_vertex_pairs}")
    if summary.spread_bad_parts:
        print(f"  spread bad parts:  {summary.spread_bad_parts}")
    if summary.skipped_merge_pairs:
        print(f"  skipped merges:    {summary.skipped_merge_pairs}")
    if summary.fixed_close_pairs_after:
        print(f"  fixed close:       {summary.fixed_close_pairs_after}")
    if summary.fixed_near_edges_after:
        print(f"  fixed near edges:  {summary.fixed_near_edges_after}")
    if summary.angle_vertices_removed:
        print(f"  angle vertices:    {summary.angle_vertices_removed} removed")
    if summary.collinear_vertices_removed:
        print(f"  collinear vertices:{summary.collinear_vertices_removed} removed")
    if summary.borders_removed:
        print(f"  removed borders:   {summary.borders_removed}")
    if summary.faces_after is not None:
        print(f"  geometry faces:    {summary.faces_before} -> {summary.faces_after}")
    if summary.areas_declared is not None:
        print(f"  declared areas:    {summary.areas_declared}")
    for warning in summary.warnings:
        print(f"  WARNING: {warning}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if args.in_place and args.output is not None:
        parser.error("Use either an output path or --in-place, not both.")

    output_path = args.input if args.in_place else (args.output or default_output_path(args.input))

    try:
        summary = clean_pri_geometry(
            args.input,
            output_path,
            minimum_distance=args.min_distance,
            minimum_angle=args.min_angle,
            fix_angles=not args.no_fix_angles,
            preserve_area_count=not args.allow_area_count_change,
            dry_run=args.dry_run,
        )
    except GeometryFixError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"UNEXPECTED ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3

    if args.json:
        import json

        print(json.dumps(asdict(summary), ensure_ascii=False, indent=2))
    else:
        print_summary(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
