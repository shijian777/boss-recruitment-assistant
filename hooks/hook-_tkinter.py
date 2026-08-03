from pathlib import Path
import sys


base = Path(sys.base_prefix)
tcl_root = base / "tcl"
dll_root = base / "DLLs"

datas = [
    (str(tcl_root / "tcl8.6"), "_tcl_data"),
    (str(tcl_root / "tk8.6"), "_tk_data"),
]

binaries = [
    (str(dll_root / "tcl86t.dll"), "."),
    (str(dll_root / "tk86t.dll"), "."),
]
