# Third-party components

This application includes Python, PySide6/Qt, Shiboken6, Pillow and a PyInstaller bootloader. Bandizip is invoked from the user's installation and is not redistributed.

Original dependency notices supplied by the installed distributions are copied into `licenses/`. Qt open-source license texts are included alongside the commercial-license notice shipped in the PyPI wheels. The wheel's commercial-license notice is not a claim that this application includes a commercial Qt license.

| Component | Version | Project / source |
|---|---|---|
| Python | 3.12.14 | https://www.python.org/ / https://github.com/python/cpython |
| PySide6 / Shiboken6 | 6.10.1 | https://code.qt.io/cgit/pyside/pyside-setup.git/ |
| Qt | 6.10.1 | https://code.qt.io/cgit/qt/qtbase.git/ |
| Pillow | 12.3.0 | https://github.com/python-pillow/Pillow |
| PyInstaller | 6.17.0 | https://github.com/pyinstaller/pyinstaller |

The complete application Python source and build instructions are included. To rebuild with modified Qt/PySide6 components, prepare the corresponding Python environment, replace the pinned dependency as required, run the tests, and run the PyInstaller command in `source/build.bat` using that environment. The application dynamically uses Qt DLLs packaged by PyInstaller.

The application code supplied for this requested project may be used and modified by the recipient. Third-party components retain their respective licenses.
