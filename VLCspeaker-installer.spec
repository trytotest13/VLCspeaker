# -*- mode: python ; coding: utf-8 -*-
# Build the installable one-folder edition of VLCspeaker (the file tree the
# MSI wraps). Run from the repo root:
#     pyinstaller VLCspeaker-installer.spec --noconfirm --clean
# -> dist\vlcspeaker\vlcspeaker.exe (+ _internal\), ready for
#     wix\gen_installer.py + wix build (see build_installer.cmd).
#
# Unlike the older VLCspeaker.spec (onefile, TTS stripped out), this build
# bundles the full Piper neural-TTS stack so the installed app speaks the
# shipped voice models offline with no Python on the target machine.

import os
from PyInstaller.utils.hooks import collect_all

datas = []
binaries = []
hiddenimports = [
    'winrt.windows.foundation',
    'winrt.windows.globalization',
    'winrt.windows.graphics.imaging',
    'winrt.windows.media.ocr',
    'winrt.windows.storage.streams',
    'numpy',
    'pathvalidate',
]

# Drag & drop support ships tcl/tk extension files
tmp_ret = collect_all('tkinterdnd2')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]

# Piper carries a compiled bridge (espeakbridge.pyd) and its own
# espeak-ng-data folder, so collect the whole package.
tmp_ret = collect_all('piper')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]

# onnxruntime loads its inference DLLs from the package directory
tmp_ret = collect_all('onnxruntime')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]

tmp_ret = collect_all('pyaudio')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]

a = Analysis(
    ['vlcspeaker.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['pandas', 'torch', 'torchvision', 'torchaudio', 'scipy',
              'transformers', 'tensorflow', 'boto3', 'sqlalchemy',
              'pyarrow', 'numba', 'llvmlite'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='vlcspeaker',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['assets/icon.ico'],
    version=os.path.join(SPECPATH, 'installer', 'exe_version.txt'),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='vlcspeaker',
)
