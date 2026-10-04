"""Build a clean v3.9 source ZIP from an explicit allowlist.

Run with Python 3.12 from any working directory. --check validates the complete
archive in memory without writing it. Existing packages are never overwritten.
No user assets, installed dependencies, directory trees, or logs are copied.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import sys
import zipfile


ROOT = Path(__file__).resolve().parent
PACKAGE = "AnimationWorkbench_v3.9_source"
OUTPUT_DIR = ROOT / "分享套件"
SOURCE_FILES = (
    ".gitignore",
    "LICENSE",
    "README.md",
    "CHANGELOG.md",
    "THIRD_PARTY.md",
    "docs/getting-started.zh-TW.md",
    "docs/similar-tools.zh-TW.md",
    "docs/images/workbench.jpg",
    "docs/images/preview-100.jpg",
    "workspace.css",
    "workspace.js",
    "beginner.css",
    "beginner.js",
    "ui.html",
    "app.js",
    "loop.js",
    "queue.js",
    "queue_manager.py",
    "region.js",
    "region.css",
    "test_queue.py",
    "test_source_refresh.py",
    "test_source_refresh.cjs",
    "transitions.py",
    "transition_tone.py",
    "test_transition_tone.py",
    "transition_seams.py",
    "test_transition_seams.py",
    "transitions.js",
    "transitions.css",
    "transitions_panel.html",
    "engine.py",
    "export_progress.py",
    "loop_editor.py",
    "loop_closure.py",
    "test_loop_closure.py",
    "seam_alignment.py",
    "test_alignment.py",
    "experiments/alignment_synthetic_v37.py",
    "experiments/shared_seams_fixture_v38.py",
    "test_alignment_integration.py",
    "server.py",
    "launcher.py",
    "launcher_restart.py",
    "test_launcher.py",
    "setup_environment.py",
    "requirements.txt",
    "setup.cmd",
    "start.cmd",
    "restart.cmd",
    "test_loop_editor.py",
    "test_export_progress.py",
    "test_loop_ui_state.cjs",
    "test_transitions.py",
    "使用說明.txt",
    "依賴與限制.txt",
    "build_share.py",
)
GENERATED_FILES = (
    "qwen_original.json",
    "第一次安裝.cmd",
    "啟動工具.cmd",
    "重新啟動工作台.cmd",
    "tools/ffmpeg/bin/放置FFmpeg說明.txt",
)
MANIFEST = "SHA256.json"
FORBIDDEN_PARTS = {
    "jobs", "uploads", "logs", "log", "backups", "backup", ".venv",
    "__pycache__", ".git", "private", "transition_projects", "batch_queue.json",
}
PRIVATE_PATH_PATTERN = re.compile(
    r"[a-z]:(?:\\+|/)Users(?:\\+|/)|/(?:Users|home)/[^/\s]+/",
    re.IGNORECASE,
)
FFMPEG_README = (
    "此處放接收者自行取得的 FFmpeg 套件 bin 內容，包含 ffmpeg.exe、"
    "ffprobe.exe 及必要 DLL。也可使用 PATH 中的 FFmpeg。"
    "此分享包未附任何 FFmpeg 執行檔。\r\n"
)


def sanitized_workflow(data: bytes) -> bytes:
    """Keep the fixed API graph while replacing machine-specific UI remnants."""
    graph = json.loads(data.decode("utf-8-sig"))
    if not isinstance(graph, dict):
        raise ValueError("工作流必須是 API 節點物件。")
    graph.pop("472", None)
    for node in graph.values():
        if not isinstance(node, dict) or not isinstance(node.get("inputs"), dict):
            raise ValueError("工作流含有無效節點。")
        if node.get("class_type") == "LoadImage":
            node["inputs"]["image"] = "input.png"
        elif node.get("class_type") == "SaveImageAdvanced":
            node["inputs"]["filename_prefix"] = "animation_cutout"
    if graph.get("470", {}).get("class_type") != "LoadImage":
        raise ValueError("工作流缺少固定輸入節點 470。")
    if graph.get("461", {}).get("class_type") != "SaveImageAdvanced":
        raise ValueError("工作流缺少固定輸出節點 461。")
    return (json.dumps(graph, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def workflow_payload() -> tuple[bytes, str]:
    old_package = OUTPUT_DIR / "AnimationWorkbench_v2.1_source.zip"
    old_entry = "AnimationWorkbench_v2.1_source/qwen_original.json"
    if old_package.is_file():
        try:
            with zipfile.ZipFile(old_package, "r") as archive:
                return sanitized_workflow(archive.read(old_entry)), "v2.1 clean ZIP"
        except (OSError, KeyError, ValueError, UnicodeError, zipfile.BadZipFile):
            print("舊分享 ZIP 的工作流無法讀取，改為清理根目錄的工作流。")
    source = ROOT / "qwen_original.json"
    if source.is_symlink() or not source.is_file():
        raise ValueError("找不到一般檔案 qwen_original.json。")
    return sanitized_workflow(source.read_bytes()), "root workflow, sanitized"


def validate_payload(payload: dict[str, bytes]) -> None:
    expected = set(SOURCE_FILES) | set(GENERATED_FILES)
    if set(payload) != expected:
        raise ValueError("分享檔案與固定 allowlist 不符。")
    private_root = str(ROOT).replace("\\", "/").casefold()
    for name, data in payload.items():
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or "\\" in name:
            raise ValueError(f"不安全的分享檔名：{name}")
        if any(part.casefold() in FORBIDDEN_PARTS for part in path.parts):
            raise ValueError(f"分享檔名含私人／執行期目錄：{name}")
        if path.suffix.casefold() in {".log", ".pyc", ".exe", ".dll"}:
            raise ValueError(f"分享包不包含此類檔案：{name}")
        if name in {"docs/images/workbench.jpg", "docs/images/preview-100.jpg"}:
            if not data.startswith(b"\xff\xd8\xff"):
                raise ValueError(f"教學截圖不是有效的 JPEG 格式：{name}")
            continue
        text = data.decode("utf-8-sig")
        normalized = text.replace("\\\\", "\\").replace("\\", "/").casefold()
        if private_root in normalized or PRIVATE_PATH_PATTERN.search(text):
            raise ValueError(f"分享內容仍含本機私人路徑，請先清理：{name}")


def collect_payload() -> tuple[dict[str, bytes], str]:
    missing = [name for name in SOURCE_FILES if not (ROOT / name).is_file()]
    if missing:
        raise ValueError("缺少必要分享檔案：" + ", ".join(missing))
    payload = {}
    for name in SOURCE_FILES:
        source = ROOT / name
        if source.is_symlink():
            raise ValueError(f"不接受指向其他位置的符號連結：{name}")
        payload[name] = source.read_bytes()
    payload["qwen_original.json"], workflow_source = workflow_payload()
    # These literal Unicode names intentionally do not reuse old ZIP filenames.
    payload["第一次安裝.cmd"] = payload["setup.cmd"]
    payload["啟動工具.cmd"] = payload["start.cmd"]
    payload["重新啟動工作台.cmd"] = payload["restart.cmd"]
    payload["tools/ffmpeg/bin/放置FFmpeg說明.txt"] = (
        b"\xef\xbb\xbf" + FFMPEG_README.encode("utf-8")
    )
    validate_payload(payload)
    return payload, workflow_source


def archive_bytes(payload: dict[str, bytes]) -> bytes:
    checksums = {
        name: hashlib.sha256(data).hexdigest()
        for name, data in sorted(payload.items())
    }
    files = dict(payload)
    files[MANIFEST] = (
        json.dumps(checksums, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
    ).encode("ascii")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(files.items()):
            info = zipfile.ZipInfo(f"{PACKAGE}/{name}", (2026, 10, 5, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            # zipfile writes non-ASCII str names as UTF-8 and sets flag 0x800.
            archive.writestr(info, data)
    packed = buffer.getvalue()
    verify_archive(packed, set(payload))
    return packed


def verify_archive(packed: bytes, payload_names: set[str]) -> None:
    expected = {f"{PACKAGE}/{name}" for name in payload_names | {MANIFEST}}
    with zipfile.ZipFile(io.BytesIO(packed), "r") as archive:
        names = archive.namelist()
        if len(names) != len(expected) or set(names) != expected:
            raise ValueError("ZIP 的檔案清單、數量或名稱不正確。")
        failed = archive.testzip()
        if failed is not None:
            raise ValueError(f"ZIP CRC 驗證失敗：{failed}")
        for entry in archive.infolist():
            if not entry.filename.isascii() and not entry.flag_bits & 0x800:
                raise ValueError(f"ZIP 中文檔名未標記為 UTF-8：{entry.filename}")
        manifest = json.loads(archive.read(f"{PACKAGE}/{MANIFEST}").decode("ascii"))
        if set(manifest) != payload_names:
            raise ValueError("SHA256.json 未涵蓋全部分享檔案。")
        for name, digest in manifest.items():
            actual = hashlib.sha256(archive.read(f"{PACKAGE}/{name}")).hexdigest()
            if actual != digest:
                raise ValueError(f"SHA-256 驗證失敗：{name}")


def write_new_archive(packed: bytes) -> Path:
    OUTPUT_DIR.mkdir(exist_ok=True)
    number = 1
    while True:
        suffix = "" if number == 1 else f"_{number:03d}"
        destination = OUTPUT_DIR / f"{PACKAGE}{suffix}.zip"
        try:
            # Exclusive creation protects existing files, including a race.
            with destination.open("xb") as stream:
                stream.write(packed)
            return destination
        except FileExistsError:
            number += 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="只驗證，不寫入 ZIP")
    args = parser.parse_args()
    try:
        payload, workflow_source = collect_payload()
        packed = archive_bytes(payload)
        if args.check:
            print("驗證通過；未寫入 ZIP。")
        else:
            destination = write_new_archive(packed)
            verify_archive(destination.read_bytes(), set(payload))
            print(f"輸出：{destination}")
        print(f"大小：{len(packed):,} bytes")
        print(f"檔案數：{len(payload) + 1}（含 SHA256.json）")
        print(f"工作流來源：{workflow_source}")
        print("驗證：CRC / SHA-256 / UTF-8 中文檔名 / 固定 allowlist 通過。")
        return 0
    except (OSError, ValueError, UnicodeError, zipfile.BadZipFile) as exc:
        print(f"打包失敗：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
