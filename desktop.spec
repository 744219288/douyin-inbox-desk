from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

root = Path(SPECPATH)
vendor = root / 'vendor' / 'Douyin_Spider'
datas = [(str(root / 'static'), 'static')]
datas.append((str(root / 'assets' / 'heic-smoke.heic'), 'assets'))
for file in vendor.rglob('*'):
    if file.is_file() and not {'.git', '__pycache__', 'datas'} & set(file.parts) and file.suffix not in {'.py', '.pyc', '.log'} and file.name != '.env':
        datas.append((str(file), str(file.relative_to(vendor).parent)))
hidden = ['builder.auth', 'dy_apis.douyin_api', 'dy_apis.douyin_recv_msg', 'static.Live_pb2', 'static.Response_pb2', 'playwright.sync_api', 'webview.platforms.winforms']
datas += collect_data_files('webview')
a = Analysis([str(root / 'desktop.py')], pathex=[str(root), str(vendor)], binaries=[], datas=datas,
             hiddenimports=hidden, hookspath=[], runtime_hooks=[], excludes=['PyQt5', 'PyQt6', 'PySide2', 'PySide6', 'matplotlib', 'pandas'], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name='DouyinDesk', debug=False,
          bootloader_ignore_signals=False, strip=False, upx=False, console=False,
          icon=str(root / 'assets' / 'douyin-desk.ico'))
