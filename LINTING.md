# Linting Guide

This document explains how to run the linters to check and format the code in this repository. Following these steps helps maintain a consistent and high-quality codebase.

---
## Manual Linting

You can run the linters manually at any time to check your work.

Python files are linted using Black, isort, and flake8. We use `uv` to manage the development environment.

**1. Install `uv`**

If you don't have `uv` installed, follow the official installation instructions. On macOS and Linux, you can run:
```bash
curl -LsSf [https://astral.sh/uv/install.sh](https://astral.sh/uv/install.sh) | sh
```

**2. Set Up the Environment**

From the root of the repository, run this command to install the linting tools in a virtual environment. You only need to do this once.
```bash
uv pip install -e .[dev]
```

**3. Run the Linters**

Run these commands from the root directory to format and check the Python code. They use `uv run` to ensure the correct tools from the virtual environment are always used, so you don't need to activate it manually.

* **To format code with Black:**
    ```bash
    uv run black .
    ```
* **To sort imports with isort:**
    ```bash
    uv run isort .
    ```
* **To check for errors with flake8:**
    ```bash
    flake8 --config=./.flake8 .
    ```
---
## Automatic Linting on Commit (Optional Setup)

You can configure your local repository to run all linters automatically before every commit. This is the recommended way to ensure no un-linted code is ever committed. This setup only affects your local machine and doesn't change anything in the remote repository.

### 1. Install the `pre-commit` Tool

You need the `pre-commit` package manager. You can install it using `uv` or `pip`.
```bash
uv pip install pre-commit
```

### 2. Activate the Git Hooks

Navigate to the root of the repository and run the following command. This will set up the pre-commit script in your local `.git/hooks` directory. You only need to do this once per project.
```bash
pre-commit install
```

That's it! From now on, whenever you run `git commit`, the linters will automatically run on the files you've changed. If any issues are found, the commit will be aborted, giving you a chance to fix them.
