"""CV Tailor: web app local chỉnh CV LaTeX theo JD bằng Claude."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import time
from pathlib import Path

import threading

import base64
import secrets

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse

from . import jd_input, latex, llm

ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = Path(os.environ.get("CV_TAILOR_OUTPUT", ROOT / "output"))
COURSES_FILE = Path(os.environ.get("CV_TAILOR_COURSES", ROOT / "courses.md"))
COURSES_EXAMPLE = ROOT / "courses.example.md"  # bản mẫu trong zip; courses.md của bạn không bị ghi đè khi cập nhật app


def _courses_text() -> str:
    src = COURSES_FILE if COURSES_FILE.is_file() else COURSES_EXAMPLE
    return src.read_text(encoding="utf-8-sig") if src.is_file() else ""
MODELS = ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"]
EFFORTS = ["low", "medium", "high"]

HOST = os.environ.get("CV_TAILOR_HOST", "127.0.0.1")
PASSWORD = os.environ.get("CV_TAILOR_PASSWORD", "")
LOOPBACK = {"127.0.0.1", "::1", "localhost", "testclient"}
if HOST not in LOOPBACK and not PASSWORD:
    raise RuntimeError("Mở app cho máy khác truy cập (CV_TAILOR_HOST=%s) thì phải đặt mật khẩu: "
                       "CV_TAILOR_PASSWORD=... trong file .env" % HOST)

app = FastAPI(title="CV Tailor")


def _is_local(request: Request) -> bool:
    return (request.client.host if request.client else "") in LOOPBACK


@app.middleware("http")
async def password_guard(request: Request, call_next):
    """Máy khác truy cập phải nhập mật khẩu (HTTP Basic, tên đăng nhập gõ gì cũng được)."""
    if PASSWORD and not _is_local(request):
        ok = False
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("basic "):
            try:
                pw = base64.b64decode(auth[6:]).decode("utf-8", "replace").split(":", 1)[1]
                ok = secrets.compare_digest(pw.encode(), PASSWORD.encode())
            except Exception:
                ok = False
        if not ok:
            return PlainTextResponse("Cần mật khẩu", status_code=401,
                                     headers={"WWW-Authenticate": 'Basic realm="CV Tailor", charset="UTF-8"'})
    return await call_next(request)
_page_cache: dict[str, int | None] = {}
_picker_lock = threading.Lock()


@app.get("/", response_class=HTMLResponse)
def index():
    return (Path(__file__).parent / "static" / "index.html").read_text(encoding="utf-8")


@app.get("/api/config")
def config(request: Request):
    return {"remote": not _is_local(request), "models": MODELS, "default_model": llm.DEFAULT_MODEL, "efforts": EFFORTS,
            "engine": (latex.find_engine("") or ["(chưa cài)"])[0],
            "backends": llm.available_backends(), "default_backend": llm.default_backend()}


# ------------------------------------------------------------------ bước 1: JD

@app.post("/api/jd")
async def analyze_jd(mode: str = Form(...), text: str = Form(""), url: str = Form(""),
                     file: UploadFile | None = File(None), backend: str = Form("")):
    note = None
    bk = backend if backend in llm.BACKENDS else llm.default_backend()
    try:
        if mode == "text":
            if len(text.strip()) < 50:
                raise ValueError("Text JD quá ngắn.")
            t, note = jd_input._limit(jd_input.clean_text(text))
            summary, usage, cached = llm.summarize_jd(text=t, backend=bk)
        elif mode == "url":
            t, note = jd_input.from_url(url.strip())
            summary, usage, cached = llm.summarize_jd(text=t, backend=bk)
        elif mode == "file":
            if file is None:
                raise ValueError("Chưa chọn file.")
            data = await file.read()
            ctype = file.content_type or ""
            name = (file.filename or "").lower()
            if ctype == "application/pdf" or name.endswith(".pdf"):
                t, note = jd_input.from_pdf_text(data)
                if t:
                    summary, usage, cached = llm.summarize_jd(text=t, backend=bk)
                else:
                    note = "PDF không có lớp text (bản scan) nên gửi nguyên PDF cho Claude đọc."
                    summary, usage, cached = llm.summarize_jd(pdf=data, backend=bk)
            elif ctype in jd_input.IMAGE_TYPES:
                summary, usage, cached = llm.summarize_jd(image=(data, ctype), backend=bk)
            elif name.endswith((".txt", ".md")):
                t, note = jd_input._limit(jd_input.clean_text(data.decode("utf-8", "replace")))
                summary, usage, cached = llm.summarize_jd(text=t, backend=bk)
            else:
                raise ValueError("Chỉ hỗ trợ PDF, ảnh (PNG/JPG/WEBP) hoặc .txt.")
        else:
            raise ValueError("mode không hợp lệ")
    except (ValueError, llm.LLMError) as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # lỗi mạng khi tải link, PDF hỏng…
        raise HTTPException(400, f"Không đọc được JD: {e}")
    return {"summary": summary, "usage": usage, "cached": cached, "note": note}


# ------------------------------------------------------------------ bước 2: sửa CV

def _slug(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", s or "").strip("-")
    return s[:40] or "job"


def _pages_of(tex: str) -> int | None:
    key = hashlib.sha256(tex.encode()).hexdigest()
    if key not in _page_cache:
        r = latex.compile_tex(tex, "original")
        _page_cache[key] = r.pages if r.ok else None
    return _page_cache[key]


def _changed_sections_prompt(cv: latex.ParsedCV, ids: set[str]) -> str:
    sub = copy.copy(cv)
    sub.sections = [s for s in cv.sections if s.id in ids] or cv.sections
    sub.macros = cv.macros
    return latex.sections_for_prompt(sub)


@app.post("/api/tailor")
async def tailor(cv_file: UploadFile | None = File(None), cv_text: str = Form(""),
                 jd_summary: str = Form(...), model: str = Form(llm.DEFAULT_MODEL),
                 effort: str = Form("medium"), keep_pages: bool = Form(True),
                 instructions: str = Form(""), cv_name: str = Form("CV"), backend: str = Form(""),
                 save_dir: str = Form(""), file_name: str = Form(""), use_courses: bool = Form(True)):
    bk = backend if backend in llm.BACKENDS else llm.default_backend()
    try:
        target_dir = _check_dir(save_dir)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if model not in MODELS or effort not in EFFORTS:
        raise HTTPException(400, "Model hoặc effort không hợp lệ.")
    tex = (await cv_file.read()).decode("utf-8", "replace") if cv_file else cv_text
    if not tex.strip():
        raise HTTPException(400, "Chưa có file CV .tex.")
    try:
        jd = json.loads(jd_summary)
        cv = latex.parse_cv(tex)
    except (ValueError, json.JSONDecodeError) as e:
        raise HTTPException(400, str(e))

    usages, log = [], []
    options = [f"Keep the CV within its current page count: {'yes' if keep_pages else 'no'}."]
    if instructions.strip():
        options.append("User notes (follow unless they conflict with the truthfulness rules): " + instructions.strip()[:4000])

    try:
        cv_prompt = latex.sections_for_prompt(cv)
        bank = _courses_bank() if use_courses else ""
        if bank:
            cv_prompt += f"\n\n<coursework_bank>\n{bank}\n</coursework_bank>"
        result, u = llm.tailor(cv_prompt, jd, model=model, effort=effort, options="\n".join(options), backend=bk)
        usages.append({**u, "step": "Sửa CV"})
        applied, failed = latex.apply_edits(cv, result["edits"], result.get("section_order") or None)
        changed = {e["section_id"] for e in applied}
        new_tex = cv.assemble()

        comp = latex.compile_tex(new_tex)
        if not comp.ok and latex.find_engine(new_tex):
            log.append("Lỗi biên dịch, nhờ Claude sửa lại phần đã đổi…")
            fixr, u = llm.fix(_changed_sections_prompt(cv, changed), "LaTeX compile error:\n" + comp.log_tail, model=model, backend=bk)
            usages.append({**u, "step": "Sửa lỗi LaTeX"})
            a2, f2 = latex.apply_edits(cv, fixr["edits"])
            applied += a2
            failed += f2
            new_tex = cv.assemble()
            comp = latex.compile_tex(new_tex)

        orig_pages = _pages_of(tex) if comp.ok and keep_pages else None
        if comp.ok and orig_pages and comp.pages and comp.pages > orig_pages:
            log.append(f"CV dài {comp.pages} trang (gốc {orig_pages}), nhờ Claude rút gọn…")
            fixr, u = llm.fix(latex.sections_for_prompt(cv),
                              f"The CV now renders {comp.pages} pages but must fit in {orig_pages}. "
                              "Shorten by trimming the least relevant bullets/words for this JD:\n" + llm.jd_to_prompt(jd),
                              model=model, backend=bk)
            usages.append({**u, "step": "Rút gọn số trang"})
            a2, f2 = latex.apply_edits(cv, fixr["edits"])
            applied += a2
            failed += f2
            trial_tex = cv.assemble()
            trial = latex.compile_tex(trial_tex)
            if trial.ok:
                new_tex, comp = trial_tex, trial
    except llm.LLMError as e:
        raise HTTPException(502, str(e))

    # lưu file
    run = time.strftime("%Y%m%d-%H%M%S") + "_" + _slug(jd.get("company") or jd.get("job_title"))
    out = OUTPUT_DIR / run
    out.mkdir(parents=True, exist_ok=True)
    base = _clean_name(file_name) or f"{_slug(cv_name)}_{_slug(jd.get('company') or jd.get('job_title'))}"
    files, saved = _save(out, run, base, new_tex, comp.pdf if comp.ok else None, target_dir)
    total = {k: sum(x[k] for x in usages) for k in ("input", "output", "cache_write", "cache_read", "cost_usd")}
    report = {
        "jd": jd, "model": model, "effort": effort, "backend": bk,
        "match_summary": result["match_summary"], "matched_keywords": result["matched_keywords"],
        "missing_requirements": result["missing_requirements"],
        "applied": applied, "failed": failed, "usage": usages, "usage_total": total, "log": log,
        "compile": {"ok": comp.ok, "error": comp.log_tail, "pages": comp.pages},
        "base": base, "saved": saved,
    }
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    return {**report, "files": files, "run": run}


# ------------------------------------------------------------------ lưu file

def _clean_name(name: str) -> str:
    """Tên file người dùng nhập: bỏ ký tự Windows không cho phép và đuôi .tex/.pdf."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name or "").strip().rstrip(".")
    name = re.sub(r"\.(tex|pdf)$", "", name, flags=re.I).strip()
    return name[:120]


def _check_dir(path: str) -> Path | None:
    if not path.strip():
        return None
    p = Path(os.path.expandvars(os.path.expanduser(path.strip().strip('"'))))
    if not p.is_absolute():
        raise ValueError("Thư mục lưu phải là đường dẫn đầy đủ, ví dụ C:\\Users\\ten\\Documents\\CV")
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise ValueError(f"Không tạo được thư mục lưu: {e}")
    return p


def _free_base(folder: Path, base: str) -> str:
    """Không ghi đè file sẵn có (ví dụ CV gốc): thêm _2, _3… nếu tên đã tồn tại."""
    cand, k = base, 2
    while (folder / f"{cand}.tex").exists() or (folder / f"{cand}.pdf").exists():
        cand, k = f"{base}_{k}", k + 1
    return cand


def _save(out: Path, run: str, base: str, tex: str, pdf: bytes | None, target: Path | None):
    """Luôn lưu bản lịch sử trong output/<run>/, và thêm một bản vào thư mục người dùng chọn."""
    (out / f"{base}.tex").write_text(tex, encoding="utf-8")
    files = {"tex": f"/files/{run}/{base}.tex"}
    if pdf:
        (out / f"{base}.pdf").write_bytes(pdf)
        files["pdf"] = f"/files/{run}/{base}.pdf"
    saved = []
    folder = target or out
    if target:
        name = _free_base(target, base)
        (target / f"{name}.tex").write_text(tex, encoding="utf-8")
        saved.append(str(target / f"{name}.tex"))
        if pdf:
            (target / f"{name}.pdf").write_bytes(pdf)
            saved.append(str(target / f"{name}.pdf"))
    else:
        saved = [str(out / f"{base}.tex")] + ([str(out / f"{base}.pdf")] if pdf else [])
    return files, {"folder": str(folder), "paths": saved}


def _courses_bank() -> str:
    """Danh sách môn học, bỏ dòng chú thích '#' để tiết kiệm token."""
    lines = _courses_text().splitlines()
    return "\n".join(l.rstrip() for l in lines if l.strip() and not l.lstrip().startswith("# ")).strip()


@app.get("/api/courses")
def get_courses():
    return {"text": _courses_text(), "path": str(COURSES_FILE)}


@app.post("/api/courses")
def save_courses(text: str = Form("")):
    COURSES_FILE.write_text(text.replace("\r\n", "\n"), encoding="utf-8")
    return {"ok": True, "path": str(COURSES_FILE)}


@app.post("/api/pick-folder")
def pick_folder(request: Request, initial: str = Form("")):
    """Mở hộp thoại chọn thư mục của hệ điều hành (app chạy trên chính máy bạn)."""
    if not _is_local(request):
        raise HTTPException(400, "Hộp thoại chỉ mở được trên máy chạy app. Hãy gõ đường dẫn thư mục (trên máy đó) vào ô.")
    if not _picker_lock.acquire(blocking=False):
        raise HTTPException(409, "Hộp thoại chọn thư mục đang mở.")
    try:
        import tkinter
        from tkinter import filedialog
        root = tkinter.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        path = filedialog.askdirectory(parent=root, initialdir=initial or str(Path.home()),
                                       title="Chọn thư mục lưu CV")
        root.destroy()
    except Exception as e:
        raise HTTPException(500, f"Không mở được hộp thoại ({e}). Hãy gõ đường dẫn thư mục vào ô.")
    finally:
        _picker_lock.release()
    return {"path": str(Path(path)) if path else ""}


@app.post("/api/compile")
def recompile(run: str = Form(...)):
    """Biên dịch lại PDF từ file .tex đã lưu (ví dụ sau khi vừa cài MiKTeX) - không tốn token."""
    out = (OUTPUT_DIR / run).resolve()
    if OUTPUT_DIR.resolve() not in out.parents or not (out / "report.json").is_file():
        raise HTTPException(404, "Không tìm thấy lần chạy này.")
    report = json.loads((out / "report.json").read_text(encoding="utf-8"))
    base = report.get("base") or next(out.glob("*.tex")).stem
    tex = (out / f"{base}.tex").read_text(encoding="utf-8")
    comp = latex.compile_tex(tex)
    report["compile"] = {"ok": comp.ok, "error": comp.log_tail, "pages": comp.pages}
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    if not comp.ok:
        return {"compile": report["compile"]}
    (out / f"{base}.pdf").write_bytes(comp.pdf)
    # PDF đặt cạnh file .tex đã lưu ở thư mục người dùng chọn (cùng tên)
    tex_path = Path(next((p for p in report.get("saved", {}).get("paths", []) if p.endswith(".tex")), out / f"{base}.tex"))
    pdf_path = tex_path.with_suffix(".pdf")
    if tex_path.parent.is_dir():
        pdf_path.write_bytes(comp.pdf)
    report["saved"] = {"folder": str(tex_path.parent), "paths": [str(tex_path), str(pdf_path)]}
    report["compile"] = {"ok": True, "error": "", "pages": comp.pages}
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"compile": report["compile"], "files": {"tex": f"/files/{run}/{base}.tex", "pdf": f"/files/{run}/{base}.pdf"},
            "saved": report["saved"]}


@app.get("/files/{run}/{name}")
def get_file(run: str, name: str):
    p = (OUTPUT_DIR / run / name).resolve()
    if OUTPUT_DIR.resolve() not in p.parents or not p.is_file():
        raise HTTPException(404)
    media = "application/pdf" if p.suffix == ".pdf" else "application/x-tex"
    disp = "inline" if p.suffix == ".pdf" else "attachment"
    return FileResponse(p, media_type=media, filename=p.name, content_disposition_type=disp)
