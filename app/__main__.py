"""Khởi động CV Tailor: `python -m app`.

Tự đọc file .env (chịu được BOM, khoảng trắng, dấu ngoặc kép), kiểm tra cấu hình
và in lỗi dễ hiểu thay vì traceback dài.
"""
from __future__ import annotations

import os
import socket
import sys
import threading
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _read_text(path: Path) -> str:
    raw = path.read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):  # Notepad "Unicode" (UTF-16)
        return raw.decode("utf-16", errors="replace")
    return raw.decode("utf-8-sig", errors="replace")


def load_env(path: Path) -> list[str]:
    """Nạp biến từ file .env, trả về danh sách tên biến đọc được."""
    if not path.is_file():
        return []
    keys = []
    for line in _read_text(path).splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key, val = key.strip(), val.split(" #", 1)[0].strip().strip('"').strip("'")
        if key:
            keys.append(key)
            os.environ.setdefault(key, val)
    return keys


def lan_ips() -> list[str]:
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))  # không gửi gói tin nào, chỉ để hỏi card mạng đang dùng
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    return sorted(ips)


def fail(msg: str) -> None:
    print("\n[LỖI] " + msg + "\n")
    sys.exit(1)


def main() -> None:
    os.chdir(ROOT)
    args = [a.lower() for a in sys.argv[1:]]
    env_files = [p for p in (ROOT / ".env", ROOT / ".env.txt") if p.is_file()]
    for p in env_files:
        keys = load_env(p)
        print(f"Đã đọc {p.name}: {', '.join(k for k in keys if k != 'ANTHROPIC_API_KEY' and 'PASSWORD' not in k) or '(không có biến nào)'}"
              + (" (+ mật khẩu)" if "CV_TAILOR_PASSWORD" in keys else ""))
    if not env_files:
        print(f"Không thấy file .env trong {ROOT} (không sao nếu chỉ dùng trên máy này).")

    if "lan" in args or "--lan" in args:  # run.bat lan: cho máy khác truy cập, không cần sửa .env
        os.environ["CV_TAILOR_HOST"] = "0.0.0.0"
        if not os.environ.get("CV_TAILOR_PASSWORD", "").strip():
            pw = input("Đặt mật khẩu cho máy khác truy cập: ").strip()
            if not pw:
                fail("Cần có mật khẩu.")
            os.environ["CV_TAILOR_PASSWORD"] = pw
    host = os.environ.get("CV_TAILOR_HOST", "127.0.0.1").strip() or "127.0.0.1"
    os.environ["CV_TAILOR_HOST"] = host
    try:
        port = int(os.environ.get("CV_TAILOR_PORT", "8000").strip() or 8000)
    except ValueError:
        fail("CV_TAILOR_PORT trong .env phải là số, ví dụ 8000.")
    if host not in ("127.0.0.1", "localhost") and not os.environ.get("CV_TAILOR_PASSWORD", "").strip():
        fail(f"Bạn đặt CV_TAILOR_HOST={host} (cho máy khác truy cập) nhưng chưa đặt mật khẩu.\n"
             "Thêm dòng CV_TAILOR_PASSWORD=mat-khau-cua-ban vào file .env, hoặc xoá dòng CV_TAILOR_HOST.")

    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", port)) == 0:
            fail(f"Cổng {port} đang bị dùng: có thể app đã đang chạy ở một cửa sổ CMD khác.\n"
                 f"Hãy mở http://127.0.0.1:{port} hoặc đóng cửa sổ cũ (Ctrl+C) rồi chạy lại. "
                 "Hoặc đổi cổng: CV_TAILOR_PORT=8001 trong .env.")

    try:
        import uvicorn
        from app import main as _app  # noqa: F401  (báo lỗi import sớm, dễ đọc)
    except Exception as e:
        fail(f"Không nạp được app: {e}\nThử xoá thư mục .venv rồi chạy lại run.bat để cài lại thư viện.")

    print(f"\nCV Tailor đang chạy: http://127.0.0.1:{port}")
    if host not in ("127.0.0.1", "localhost"):
        for ip in lan_ips():
            print(f"Máy khác truy cập qua: http://{ip}:{port}")
        print(f"Qua Tailscale: http://<IP 100.x.x.x của máy này>:{port}")
    else:
        print("(Chỉ máy này truy cập được. Cho máy khác truy cập: chạy  run.bat lan)")
    print("Tắt app: nhấn Ctrl+C\n")
    if os.environ.get("CV_TAILOR_NO_BROWSER") != "1":
        threading.Timer(1.5, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    uvicorn.run("app.main:app", host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
