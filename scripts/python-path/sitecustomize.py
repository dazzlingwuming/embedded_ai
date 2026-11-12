"""Preserve this project's ASCII SUBST alias in ESP-IDF Python subprocesses.

Windows realpath otherwise expands P: back into the Chinese workspace name,
which some native toolchain utilities cannot encode. Only this project and
this process's descendants are affected; no system Python files are changed.
"""
import os

if os.name == 'nt' and os.environ.get('NANO_PRESERVE_DRIVE_ALIAS') == '1':
    _realpath = os.path.realpath
    _root = _realpath('P:\\').rstrip('\\')

    def _project_realpath(path, *args, **kwargs):
        result = _realpath(path, *args, **kwargs)
        if isinstance(result, str):
            if result.casefold() == _root.casefold():
                return 'P:\\'
            if result.casefold().startswith((_root + '\\').casefold()):
                return 'P:' + result[len(_root):]
        return result

    os.path.realpath = _project_realpath
