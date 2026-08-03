"""Work around PyInstaller's Tcl-only probe on Python builds using tcl86t.dll.

The actual Tk root-window probe succeeds, but tkinter.Tcl() used by the stock
hook reports a false negative. Leaving search_dirs unchanged allows normal
stdlib tkinter analysis; hook-_tkinter.py supplies the verified data files.
"""


def pre_find_module_path(_hook_api) -> None:
    return
