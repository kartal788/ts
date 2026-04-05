from logging import FileHandler, StreamHandler, INFO, ERROR, Formatter, basicConfig, error as log_error, info as log_info
from os import path as ospath, environ
from pathlib import Path
from subprocess import run as srun, PIPE
from dotenv import load_dotenv
from datetime import datetime
import pytz
import shutil
IST = pytz.timezone("Europe/Istanbul")

class ISTFormatter(Formatter):
    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, IST)
        return dt.strftime(datefmt or "%d-%b-%y %I:%M:%S %p")

log_file = "log.txt"
if ospath.exists(log_file):
    with open(log_file, "w") as f:
        f.truncate(0)
if Path(".git").exists(): shutil.rmtree(".git")
file_handler = FileHandler(log_file)
stream_handler = StreamHandler()

formatter = ISTFormatter("[%(asctime)s] [%(levelname)s] - %(message)s", "%d-%b-%y %I:%M:%S %p")
file_handler.setFormatter(formatter)
stream_handler.setFormatter(formatter)

basicConfig(handlers=[file_handler, stream_handler], level=INFO)

load_dotenv("config.env")

UPSTREAM_REPO = environ.get("UPSTREAM_REPO", "").strip() or None
UPSTREAM_BRANCH = environ.get("UPSTREAM_BRANCH", "").strip() or "master"

if UPSTREAM_REPO:
    if Path(".git").exists():
        srun(["rm", "-rf", ".git"])

    # git reset --hard'dan önce korunması gereken dosyaları yedekle
    _PRESERVE_FILES = ["gdrive_token.pickle", "config.env"]
    _backups = {}
    for _pf in _PRESERVE_FILES:
        _pf_path = Path(_pf)
        if _pf_path.exists() and _pf_path.stat().st_size > 0:
            try:
                _backups[_pf] = _pf_path.read_bytes()
            except Exception:
                pass

    # git add . öncesinde büyük/gereksiz dizinleri exclude et
    # (uploads içindeki video dosyaları git'e eklenmeden güncelleme yapılır)
    _EXCLUDE_PATTERNS = [
        "Backend/uploads/",
        "Backend/uploads/**",
        "*.mkv",
        "*.mp4",
        "*.avi",
        "*.mov",
        "*.wmv",
        "*.flv",
        "*.ts",
        "*.m4v",
        "*.webm",
        "*.zip",
        "*.rar",
        "*.7z",
        "__pycache__/",
        "*.pyc",
        ".venv/",
        "*.log",
    ]
    Path(".git/info").mkdir(parents=True, exist_ok=True)
    with open(".git/info/exclude", "w") as _exc_f:
        _exc_f.write("\n".join(_EXCLUDE_PATTERNS) + "\n")

    update_cmd = (
        f"git init -q && "
        f"git config --global user.email 'doc.adhikari@gmail.com' && "
        f"git config --global user.name 'weebzone' && "
        f"git add . && git commit -sm 'update' -q && "
        f"git remote add origin {UPSTREAM_REPO} && "
        f"git fetch origin -q && "
        f"git reset --hard origin/{UPSTREAM_BRANCH} -q"
    )

    update = srun(update_cmd, shell=True)

    # git reset --hard sonrası yedeklenen dosyaları geri yükle
    for _pf, _data in _backups.items():
        try:
            Path(_pf).write_bytes(_data)
        except Exception as _restore_err:
            log_error(f"Dosya geri yüklenemedi: {_pf} — {_restore_err}")
    repo = UPSTREAM_REPO.strip("/").split("/")
    repo_url = f"https://github.com/{repo[-2]}/{repo[-1]}"
    log_info(f"UPSTREAM_REPO: {repo_url} | UPSTREAM_BRANCH: {UPSTREAM_BRANCH}")

    if update.returncode == 0:
        log_info("Successfully updated with latest commits!!")
    else:
        log_error("❌ Update failed! Retry or ask for support.")
