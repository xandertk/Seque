# Converter Overview

This document is a short map for developers reviewing or extending Seque.

## Files

- `slim_to_pri.py` contains the core converter, command-line interface and PRI
  writer. Start here for conversion behavior.
- `seque.py` contains the Tkinter GUI and batch conversion workflow. It calls
  `slim_to_pri.convert()` for every selected file.
- `seque.bat` is a convenience launcher for Windows users.

## Core Pipeline

The main flow is:

1. `read_sli_from_slim(path)` reads either an unpacked `.sli` file or extracts
   the preferred `.sli` member from a `.slim` ZIP archive.
2. `parse_slide_model(text)` parses Slide vertices, triangular cells, material
   definitions, external boundary, material CAD lines and water table.
3. `build_pri_mesh(model, include_water_table=True)` builds the intermediate
   `PriMesh` object used by the writer.
4. `validate_mesh(model, mesh)` checks internal consistency before writing.
5. `write_pri(path, model, mesh, include_mesh=False)` serializes PRI 2.2.0 text.
6. `validate_written_pri(path)` checks section order and basic output counts.

The public API is:

```python
from pathlib import Path
from slim_to_pri import convert

summary = convert(Path("input.slim"), Path("output.pri"))
```

## Geometry Strategy

Slide files contain triangular cells and separate CAD geometry. Seque uses the
triangular cells to determine material connectivity and area properties, but the
default PRI output omits the mesh so ProRock does not show internal triangulation
lines.

The converter first tries to use Slide material CAD lines for the PRI material
borders. If those lines do not form a face graph matching the detected material
regions, it falls back to mesh-derived material interfaces. This fallback is why
real files with inconsistent CAD lines can still be converted.

## Area Property Assignment

Connected cells with the same Slide material form one material component. PRI
area rows must be written in the same order as ProRock reconstructs CAD faces, so
`order_area_components()` traces CAD faces, sorts them by envelope and matches
them to material components by area and centroid.

## PRI Output Scope

By default `write_pri()` writes:

- `nodes: 0`
- `elements: 0 0`
- `mesh sets: 0`
- geometry `vertices`
- `ExternalBorders`
- `MaterialBorders`
- optional `WaterLevel`
- `areas`
- empty/zero mechanical sections needed by ProRock

When `--keep-mesh` is passed, Slide triangular nodes, elements, neighbours and
mesh sets are also written.

## Known Limitations

- Mechanical material properties are not converted. Slide Mohr-Coulomb values
  are not equivalent to ProRock FDEM parameters.
- Joint/fracture materials are currently imported as ordinary material areas.
  Automatic conversion from thin material polygons to zero-thickness fault lines
  is intentionally not enabled because it can break material boundaries.
- The parser targets Slide 6 text structures observed in current project files.
  If another Slide version changes section formatting, parsing may need updates.

## Suggested Future Work

- Add a controlled, opt-in fracture conversion mode after a reliable geometry
  rule is agreed with domain experts.
- Add small anonymized test fixtures if project data can be shared.
- Add automated regression tests around `parse_slide_model()`, `build_pri_mesh()`
  and `validate_written_pri()`.
