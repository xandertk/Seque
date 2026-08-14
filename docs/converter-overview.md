# Converter Overview

This document is a short map for developers reviewing or extending Seque.

## Files

- `slim_to_pri.py` contains the core converter, command-line interface and PRI
  writer. Start here for conversion behavior.
- `fix_pri_geometry.py` contains post-write PRI geometry tools used by the CLI
  and GUI: bolt/support cleanup, geometry cleanup, base-copy handling and
  excavation-stage generation.
- `batch_convert_geometry.py` converts every `.slim`/`.sli` in a folder with
  geometry cleanup enabled and writes a CSV cleanup summary.
- `seque.py` contains the Tkinter/TkinterDnD GUI and parallel batch conversion
  workflow. It calls `slim_to_pri.convert()` for every selected file.
- `seque.bat` launches the GUI with `pythonw` in the background for Windows
  users.
- `assets/toolbar/` and `.vendor/tkinterdnd2/` are runtime GUI assets and must
  be included when publishing the GUI.

## Core Pipeline

The default conversion flow is:

1. `read_sli_from_slim(path)` reads either an unpacked `.sli` file or extracts
   the preferred `.sli` member from a `.slim` ZIP archive.
2. `parse_slide_model(text)` parses Slide vertices, triangular cells, material
   definitions, external boundary, material CAD lines and water table.
3. `build_pri_mesh(model, include_water_table=True)` builds the intermediate
   `PriMesh` object used by the writer.
4. `validate_mesh(model, mesh)` checks internal consistency before writing.
5. `write_pri(path, model, mesh, include_mesh=False)` serializes PRI 2.2.0 text.
6. `validate_written_pri(path)` checks section order and basic output counts.

`convert()` can then run optional post-write passes in this order:

1. `remove_bolt_geometry()` removes explicitly labelled Bolt, Anchor and
   Support geometry, while preserving `MaterialBorders`.
2. `clean_pri_geometry()` applies ProRock-style cleanup thresholds and records a
   `GeometryFixSummary`.
3. `add_excavation_stages()` extends the detected pit side from a user-selected
   external-boundary top point and assigns new areas to `excatated_area`.
4. `validate_written_pri()` runs again after the final PRI rewrite.

The public API is:

```python
from pathlib import Path
from slim_to_pri import convert

summary = convert(
    Path("input.slim"),
    Path("output.pri"),
    remove_bolts=True,
    fix_geometry=True,
    add_excavation_stages=True,
    excavation_stage_count=4,
    excavation_top_point=(123.4, 567.8),
)
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

Post-write geometry cleanup preserves the reconstructed PRI face count by
default. Risky close-point merges are skipped if they would change the area
topology or increase the total Check geometry error count. The cleanup may then
spread lithotype vertices away from fixed external/water borders or nearby
lithotype segments; segment vertices can be inserted when the bad spot is along
an edge rather than at an existing vertex.

Whenever cleanup writes a changed PRI, the original post-conversion PRI is saved
beside the output as `<name>.base.pri` or the next available numbered variant.
Warnings and before/after counts are returned in `ValidationSummary.geometry_fix`
for the CLI, GUI and batch stats.

## GUI Workflow

The GUI accepts file-dialog input, Explorer drag-and-drop and Explorer clipboard
paste. If two inputs would produce the same output name, `_build_jobs()` keeps
their relative folders under the chosen output directory.

Conversions run in a `ProcessPoolExecutor` so independent files can use multiple
CPU cores. Worker processes send stage messages through a manager queue; the GUI
thread relays those messages into the progress table and log.

Excavation stages are configured for one input file at a time. The GUI first
creates a clean temporary PRI under the user's local app data folder and opens it
in ProRock if available. The user reads the desired top-vertex X/Y coordinates
from that preview, then the final conversion runs with those explicit values.
The selected output folder is persisted in the user's app data settings file.

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
