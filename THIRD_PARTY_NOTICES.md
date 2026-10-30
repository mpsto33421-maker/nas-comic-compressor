# Third-party components

NAS Comic Compressor (v1.1) is built with the components below. Bandizip is invoked from the user's own installation and is not redistributed.

License texts for these components are kept in `licenses/`. Qt/PySide6 are used under the LGPLv3; the application loads the Qt DLLs dynamically (packaged by PyInstaller), so they can be replaced by rebuilding with a modified PySide6 environment using `build.bat`.

| Component | Version | License | Source |
|---|---|---|---|
| Python | 3.12 | PSF License | https://github.com/python/cpython |
| PySide6 / Shiboken6 | 6.8.3 | LGPLv3 | https://code.qt.io/cgit/pyside/pyside-setup.git/ |
| Qt | 6.8.3 | LGPLv3 | https://code.qt.io/cgit/qt/qtbase.git/ |
| PyInstaller | 6.x | GPLv2 with bootloader exception | https://github.com/pyinstaller/pyinstaller |
| Pillow (build-time icon only) | latest | MIT-CMU (HPND) | https://github.com/python-pillow/Pillow |

The complete application source is in this repository. Third-party components retain their respective licenses.
