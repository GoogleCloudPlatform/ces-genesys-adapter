# Linting Guide

This repository follows the
[Google Python Style Guide](https://google.github.io/styleguide/pyguide.html):
80-column lines and 4-space indentation.

| Tool | Purpose | Configuration |
| :--- | :--- | :--- |
| [pyink](https://github.com/google/pyink) | Code formatting (Google's fork of Black) | `[tool.pyink]` in `pyproject.toml` |
| [pylint](https://pylint.readthedocs.io/) | Lint rules from the style guide | `.pylintrc` (Google's published rcfile) |
| [isort](https://pycqa.github.io/isort/) | Import ordering | `[tool.isort]` in `pyproject.toml` (`google` profile) |

These are development tools only. They aren't needed to build, deploy or run
the adapter.

---
## Continuous integration

Every pull request runs the `tests` workflow (`.github/workflows/tests.yml`):

* **pytest**: the unit tests.
* **style**:
  * Python files **added** by the pull request must pass `pyink --check` and
    `pylint`. This check is blocking.
  * Pre-existing files that the pull request **modifies** are checked too, but
    findings are reported without failing the build. Bring them into
    compliance when you rewrite them.

---
## Running the tools locally

We use `uv` to manage the development environment.

**1. Install `uv`**

On macOS and Linux:
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

**2. Set up the environment** (once)

```bash
uv sync
```

**3. Format, sort imports and lint**

Run these from the repository root. Pass specific files to limit the changes to
the code you're working on:
```bash
uv run pyink path/to/file.py           # format
uv run isort path/to/file.py           # sort imports
PYTHONPATH=. uv run pylint --rcfile=.pylintrc path/to/file.py
uv run pytest                          # tests
```

Avoid reformatting files you aren't otherwise changing, so pull request diffs
stay reviewable.
