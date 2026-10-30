"""Build in a minimal DLL search environment, avoiding unrelated PATH DLLs."""
import os
from pathlib import Path
import subprocess
import sys

if sys.version_info < (3, 12) or sys.maxsize <= 2**32:
    raise SystemExit('Python 3.12+ x64 required')

source = Path(__file__).resolve().parent
env = os.environ.copy()
windows = Path(env.get('SystemRoot', r'C:\Windows'))
env['PATH'] = os.pathsep.join([str(Path(sys.executable).parent), sys.base_prefix,
                             str(windows / 'System32'), str(windows)])
env.pop('PYTHONPATH', None)
env.pop('PYTHONHOME', None)
args = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--onefile', '--windowed',
        '--name', 'NAS_Comic_Compressor', '--exclude-module', 'PySide6.QtQml',
        '--exclude-module', 'PySide6.QtQuick', '--exclude-module', 'PySide6.QtWebEngineCore',
        '--exclude-module', 'PySide6.QtWebEngineWidgets', *sys.argv[1:], str(source / 'main.py')]
raise SystemExit(subprocess.call(args, cwd=source, env=env))
