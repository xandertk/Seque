# Seque

Seque is a small Python tool for converting Rocscience Slide 6 `.slim` / `.sli`
geometry into ProRock `.pri` 2.2.0 files.

It has two entry points:

- `seque.py` - desktop GUI for converting multiple files.
- `slim_to_pri.py` - command-line converter and core conversion library.

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
- No third-party Python packages are required.
- The desktop GUI uses Python's built-in `tkinter` module.

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

1. Click `Add .slim files`.
2. Choose an `Output folder`.
3. Optionally disable `Transfer water table from SLIM` in `Advanced Settings`.
4. Click `Convert`.

If several input files have the same file name, Seque preserves their relative
folder structure inside the output folder to avoid overwriting results.

## Command-Line Use

Convert one file:

```bash
python slim_to_pri.py "input.slim" "output.pri"
```

Convert and write a JSON report:

```bash
python slim_to_pri.py "input.slim" "output.pri" --report "output.report.json"
```

Convert without water table:

```bash
python slim_to_pri.py "input.slim" "output.pri" --no-water
```

Keep the imported Slide mesh in the PRI file:

```bash
python slim_to_pri.py "input.slim" "output.pri" --keep-mesh
```

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
├── seque.bat                   # Windows launcher for the GUI
├── docs/
│   └── converter-overview.md   # Developer notes and conversion pipeline
├── README.md
├── .gitignore
└── .gitattributes
```

## Publishing Notes

The `.gitignore` excludes `.slim`, `.sli`, `.pri`, reports, backups and Python
cache files by default because project data may be large or confidential.

If a sample file is approved for publication, add it deliberately:

```bash
git add -f samples/example.slim
```

## Validation

Useful checks before sharing a build:

```bash
python -m py_compile slim_to_pri.py seque.py
python slim_to_pri.py "example.slim" "example.pri" --report "example.report.json"
```

Open the generated `.pri` in ProRock and verify material colors, area
assignments, external boundary and water table before using it for analysis.
