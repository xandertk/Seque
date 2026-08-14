# Seque

Seque is a small Python tool for converting Rocscience Slide 6 `.slim` / `.sli`
geometry into ProRock `.pri` 2.2.0 files.

It has two entry points:

- `seque.py` - desktop GUI for converting multiple files.
- `slim_to_pri.py` - command-line converter and core conversion library.
- `fix_pri_geometry.py` - optional PRI geometry cleanup before ProRock meshing.

## Current Scope

Seque transfers:

- external geometry;
- material borders and material area assignments;
- material names and RGB colors;
- water table geometry as a ProRock `WaterLevel` border;
- optional Slide triangular mesh when `--keep-mesh` is used.

Seque intentionally does not transfer:

- Slide Mohr-Coulomb `c`, `phi`, or unit weight as ProRock mechanical parameters;
- material polygons that represent fractures as zero-thickness ProRock fault lines.

Fractures should currently be added or adjusted manually in ProRock. The automatic
material-to-fault conversion was left out because it can damage material
boundaries on real project files.

## Requirements

- Windows, Linux, or macOS with Python 3.10+
- No third-party Python packages are required at runtime.
- The desktop GUI uses Python's built-in `tkinter` module plus the bundled
  `tkinterdnd2` files in `.vendor/` for drag-and-drop support.
- `prepare_toolbar_icons.py` requires Pillow only when rebuilding toolbar icon
  assets; the generated PNGs are already stored under `assets/toolbar/`.

## Desktop Use

On Windows, run:

```bat
seque.bat
```

Or run the Python file directly:

```bash
python seque.py
```

In the window:

1. Click `Add .slim files` or drag `.slim`/`.sli` files from Explorer into the
   Seque window.
2. Choose an `Output folder`.
3. Optionally adjust `Advanced Settings`.
   `Fix PRI geometry before meshing` is optional because it changes CAD
   vertices. When enabled, it runs a ProRock-style cleanup with default
   thresholds of 2 m minimum distance and 18 degrees minimum angle.
   `Parallel files` controls how many independent files use CPU cores at once;
   the default is up to four. Set it to `1` when you need the most responsive
   machine while converting.
   `Add excavation stages` first opens a clean temporary PRI outside the
   selected output folder. Find the desired top vertex there, close the PRI,
   then enter its X/Y coordinates and the number of stages. The final output
   is converted only after this selection; all new areas use `excatated_area`.
   `Remove bolt and anchor geometry` removes only explicitly labelled Bolt,
   Anchor and Support objects; MaterialBorders remain intact.
4. Click `Convert`.

If several input files have the same file name, Seque preserves their relative
folder structure inside the output folder to avoid overwriting results.
The selected output folder is remembered for the next launch of Seque.
During conversion the table and progress log show the current processing stage,
including individual geometry-cleanup passes.

## Command-Line Use

Convert one file:

```bash
python slim_to_pri.py "input.slim" "output.pri"
```

Convert and write a JSON report:

```bash
python slim_to_pri.py "input.slim" "output.pri" --report "output.report.json"
```

Remove explicitly labelled bolt/anchor geometry during conversion:

```bash
python slim_to_pri.py "input.slim" "output.pri" --remove-bolts
```

Convert without water table:

```bash
python slim_to_pri.py "input.slim" "output.pri" --no-water
```

Keep the imported Slide mesh in the PRI file:

```bash
python slim_to_pri.py "input.slim" "output.pri" --keep-mesh
```

Convert and run PRI geometry cleanup before ProRock meshing:

```bash
python slim_to_pri.py "input.slim" "output.pri" --fix-geometry
```

Add excavation stages using a selected external-boundary top vertex:

```bash
python slim_to_pri.py "input.slim" "output.pri" --add-excavation-stages --excavation-stage-count 4 --excavation-top-x 123.4 --excavation-top-y 567.8
```

Clean an existing PRI directly:

```bash
python fix_pri_geometry.py "input.pri" "output_fixed.pri" --min-distance 2 --min-angle 18
```

Batch conversion uses up to four CPU processes by default. Override this when
needed, for example `python batch_convert_geometry.py Input Output --workers 8`.

By default the cleanup preserves the reconstructed PRI area count. If close
points cannot be merged without changing material-area topology or increasing
the total Check geometry error count, the script does not merge them. It then
tries to move the pair apart to the minimum distance. It also moves lithotype
vertices/segments away from fixed external or water borders, and separates
lithotype vertices from nearby lithotype segments. If the bad spot is on a
segment without a vertex, the cleanup can insert one and move it away. Moves are
accepted only when the error count improves and area topology stays safe;
fixed-border leftovers are reported separately.

Whenever cleanup writes a PRI file, Seque also saves the pre-cleanup PRI beside
the output as `<name>.base.pri` or `<name>.base.N.pri` if a copy already exists.

Check the version:

```bash
python slim_to_pri.py --version
python seque.py --version
```

## Project Layout

```text
Seque/
├── slim_to_pri.py              # Core parser, converter, PRI writer and CLI
├── seque.py                    # Tkinter desktop app
├── fix_pri_geometry.py         # Optional ProRock PRI geometry cleanup
├── batch_convert_geometry.py   # Folder batch converter with cleanup stats
├── prepare_toolbar_icons.py    # Development helper for toolbar PNGs
├── seque.bat                   # Windows launcher for the GUI
├── assets/
│   └── toolbar/                # Required GUI button icons
├── docs/
│   └── converter-overview.md   # Developer notes and conversion pipeline
├── .vendor/
│   └── tkinterdnd2/            # Bundled GUI drag-and-drop extension
├── README.md
├── .gitignore
└── .gitattributes
```

## Publishing Notes

The `.gitignore` excludes `.slim`, `.sli`, `.pri`, reports, backups and Python
cache files by default because project data may be large or confidential.

Before committing GUI changes, make sure required local assets are staged with
the code: `assets/toolbar/`, `.vendor/tkinterdnd2/` and
`.vendor/tkinterdnd2-0.6.2.dist-info/`.

If a sample file is approved for publication, add it deliberately:

```bash
git add -f samples/example.slim
```

## Validation

Useful checks before sharing a build:

```bash
python -m py_compile slim_to_pri.py seque.py fix_pri_geometry.py batch_convert_geometry.py
python slim_to_pri.py "example.slim" "example.pri" --report "example.report.json"
```

Open the generated `.pri` in ProRock and verify material colors, area
assignments, external boundary and water table before using it for analysis.
