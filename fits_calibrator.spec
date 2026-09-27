# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_data_files

block_cipher = None

EXCLUDES = [
    # Dashboard / viz frameworks (pulled by ccdproc deps)
    'panel', 'bokeh', 'param', 'holoviews', 'hvplot', 'datashader',
    # Heavy numeric / ML packages not used by this app
    # scipy REMOVED — ccdproc imports it at module load; excluding it breaks the import
    'pandas', 'numba', 'llvmlite', 'sklearn', 'skimage',
    # Cloud / network SDKs
    'botocore', 'boto3', 's3fs', 'fsspec',
    # Image libs (app uses astropy for FITS, not PIL/matplotlib)
    'PIL', 'Pillow', 'matplotlib',
    # HDF5 / zarr / dask (pulled by astropy extras, not needed)
    'h5py', 'zarr', 'dask', 'numcodecs',
    # IPython / Jupyter
    'IPython', 'ipykernel', 'ipywidgets', 'jupyter', 'notebook',
    # Testing
    'pytest', 'hypothesis',
    # Misc heavy unused
    'wx', 'PyQt5', 'PyQt6', 'PySide2', 'PySide6',
    'sqlalchemy', 'psycopg2',
    'cryptography', 'OpenSSL',
    'pydantic', 'pydantic_core',
    'Pythonwin',
]

a = Analysis(
    ['fits_solver.py'],
    pathex=[],
    binaries=[],
    datas=collect_data_files('asdf'),
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='FITS Calibrator 2.2.2',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='FITS Calibrator 2.2.2',
)
