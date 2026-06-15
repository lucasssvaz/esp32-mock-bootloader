# Contributing

Thank you for considering a contribution to **esp32-mock-bootloader**.

## Before you open a pull request

1. Fork the repository and create a branch from `main`.
2. Install dev dependencies: `pip install -e ".[dev]"`.
3. Run tests: `pytest` (parallel by default via `pyproject.toml`; see [`reports/README.md`](reports/README.md) for coverage).
4. Open a pull request against `main` and fill in the PR template.

Bug reports and feature requests are welcome via [GitHub Issues](https://github.com/lucasssvaz/esp32-mock-bootloader/issues).

## Code expectations

- Match existing style and keep changes focused.
- Add or update tests for behavior you change.
- All CI checks must pass before merge.
- Do not add **espefuse** integration tests against the mock unless explicitly scoped (e.g. connect fails gracefully or partial `summary` smoke tests). Use `espefuse --virt` for efuse burn/read testing without hardware.

### Import conventions

- **Package root** (`from esp32_mock_bootloader import …`): only `MockBootloader` and `__version__`.
- **Submodules**: import the module, then use attributes — e.g. `chips.PROFILES`, `protocol.CMD_SYNC`, `mock.server.connect(port)`.
- Do not add long re-export lists to `__init__.py`. Put `__all__` on individual submodules (`testing/protocol.py`, etc.) when needed.
- **Advanced**: `import server` is allowed but considered unstable until 1.0.0.

## AI-assisted contributions

This project was built with help from AI coding assistants. **You may use AI tools too** — we ask for transparency and human accountability, not abstinence.

### Your responsibility

You are the author of anything you submit. That means you must:

- **Understand** every change and be able to explain it in review
- **Test** it locally (or justify why tests are not needed)
- **Verify** it fits the project and passes CI

AI output is a starting point. Maintainers may close pull requests that look unreviewed or that the author cannot explain.

### What to disclose

Disclose AI use in your pull request when it **meaningfully helped** with the change — for example code, tests, docs, or the PR description itself.

**Disclosure is not required** for trivial help (autocomplete, spelling, or small syntax fixes you verified yourself).

### How to disclose

Use the **AI assistance** section in the pull request template.

Optionally, add a commit trailer when AI materially helped:

```text
Assisted-by: Cursor
```

Use `Assisted-by:` when you reviewed and edited the output. Do not use `Co-Authored-By:` for the AI.

### What we look for

- Clear note of which tool was used and what it helped with
- Evidence you ran and understood the result (tests, manual checks)
- A focused diff with a plain-language summary **in your own words**

## License

By contributing, you agree that your contributions are licensed under the same [Apache-2.0](LICENSE) license as the project.
