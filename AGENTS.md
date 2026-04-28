# Repository Guidelines

## Project Structure & Module Organization
- `seaf2drawio.py` — generate DrawIO diagrams from SEAF YAML data using patterns in `data/patterns/`. Algorithm overview: `docs/seaf2drawio-algorithm.md`.
- `drawio2seaf.py` — parse DrawIO back into YAML using `data/seaf_schema.yaml`.
- `lib/` — shared helpers (`seaf_drawio.py`, `link_manager.py`).
- `data/` — example YAML inputs, DrawIO templates, schema, and patterns; `data/example/` contains sample datasets.
- `result/` — build artifacts (e.g., `Sample_graph.drawio`, `seaf.yaml`).

## Build, Test, and Development Commands
- Install Python deps (3.9+):
  - `python -m pip install -U pip`
  - `python -m pip install N2G PyYAML deepmerge`
- Generate diagram:
  - `python -X utf8 seaf2drawio.py` (uses `config.yaml`), or
  - `python -X utf8 seaf2drawio.py -s data/example/...yaml -d result/out.drawio -p data/base.drawio`
- Reverse conversion to YAML:
  - `python -X utf8 drawio2seaf.py -s result/out.drawio -p data/seaf_schema.yaml -d result/seaf.yaml`
- Windows tip: set `PYTHONUTF8=1` if you see encoding issues.

## Coding Style & Naming Conventions
- Python, 4‑space indentation, UTF‑8 source.
- Prefer explicit, descriptive names (`snake_case` for functions/vars, `CapWords` for classes).
- Keep changes minimal and localized; avoid unrelated refactors.
- Follow existing patterns for XML string templates and YAML merging.

## Testing Guidelines
- No formal test suite. Validate by:
  - Running generation and ensuring `result/*.drawio` opens and pages render.
  - Checking console output is free of unexpected errors (informational lines only).
  - For changes to patterns, verify objects appear on expected pages and do not overlap.

## Commit & Pull Request Guidelines
- Commit messages: imperative mood, concise scope + summary.
  - Example: `seaf2drawio: handle WAN segment arrays; reduce log noise`
- PRs should include:
  - What changed and why, affected files/modules.
  - Repro/verify steps (commands) and before/after notes (optionally attach `.drawio`).
  - Any configuration or data updates (`config.yaml`, `data/patterns/*`, `data/example/*`).

## Security & Configuration Tips
- Do not commit secrets; `config.yaml` should reference local files only.
- Keep inputs in `data/example/`; write artifacts to `result/`.
- Large edits to patterns can impact layout across pages—test both DC and Office pages.

