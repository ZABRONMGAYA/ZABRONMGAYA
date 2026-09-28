# PyInstaller recipe for the engine the desktop app ships (run by packaging/build_engine.sh).
# One folder, not one file: it starts faster, and the matcher processes start the same executable.
from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

hidden = collect_submodules("mcsync", filter=lambda name: not name.startswith("mcsync.testing"))
# The speech engine (mcsync.ai): its Python extension and the ONNX Runtime libraries next to it.
sherpa_datas, sherpa_binaries, sherpa_hidden = collect_all("sherpa_onnx")
sherpa_datas = [d for d in sherpa_datas if "/include/" not in d[0].replace("\\", "/")]  # C headers: not needed

a = Analysis(
    ["entry.py"],
    datas=collect_data_files("mcsync") + sherpa_datas + collect_data_files("certifi"),
    binaries=sherpa_binaries,
    hiddenimports=hidden + [h for h in sherpa_hidden if not h.startswith("sherpa_onnx.cli")] + ["certifi"],
    excludes=["tkinter", "matplotlib", "PIL", "IPython", "pytest", "opentimelineio", "mcsync.testing", "click"],
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="mcsync-engine", console=True, upx=False)
coll = COLLECT(exe, a.binaries, a.datas, name="mcsync-engine", upx=False)
