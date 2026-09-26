# PyInstaller recipe for the engine the desktop app ships (run by packaging/build_engine.sh).
# One folder, not one file: it starts faster, and the matcher processes start the same executable.
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

hidden = collect_submodules("mcsync", filter=lambda name: not name.startswith("mcsync.testing"))

a = Analysis(
    ["entry.py"],
    datas=collect_data_files("mcsync"),
    hiddenimports=hidden,
    excludes=["tkinter", "matplotlib", "PIL", "IPython", "pytest", "opentimelineio", "mcsync.testing"],
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="mcsync-engine", console=True, upx=False)
coll = COLLECT(exe, a.binaries, a.datas, name="mcsync-engine", upx=False)
