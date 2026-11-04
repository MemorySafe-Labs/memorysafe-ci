# packaging/

Native installer assets. Only one product needs them.

`macos/` builds `MemorySafe Beta Installer.app` — the ChatGPT-on-macOS tunnel connector,
assembled by `scripts/build_macos_installer.sh` and shipped inside the beta download that
`scripts/build_beta_download.py` produces. `Installer-Info.plist` carries a copy of the
version, which `tests/test_claude_packaging.py` keeps in agreement with `pyproject.toml`.

**There is no `windows/` or `linux/`, and adding one would be wrong.** Windows and Linux
have no native installer. They install the same marketplace plugin as macOS, through
`plugin/scripts/start` and its `start.cmd` sibling, built by `scripts/build_plugin.py`
into one cross-platform tree. The legacy `scripts/install_windows.py` was deleted in
`f2fa2bf`; nothing replaced it because nothing needed to. An empty `packaging/windows`
would assert a symmetry that does not exist: this directory is split by *product*, not by
operating system.

The ChatGPT route is no longer documented as an install path: every assistant installs
the marketplace plugin now. The code here still builds and
existing tunnel testers still work; it is the documentation that retired.
