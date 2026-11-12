"""Tách CV .tex thành các section, áp dụng edit, ghép lại và biên dịch PDF.

Chỉ phần thân (giữa \\begin{document} và \\end{document}) được gửi cho Claude;
preamble được giữ nguyên ở máy để tiết kiệm token.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

BEGIN_DOC = re.compile(r"\\begin\{document\}")
END_DOC = re.compile(r"\\end\{document\}")
NEWCMD = re.compile(r"\\(?:re)?newcommand\*?\s*\{?\\([A-Za-z@]+)\}?([^\n]*)")
FULL_LINE_COMMENT = re.compile(r"(?m)^[ \t]*%[^\n]*\n")


@dataclass
class Section:
    id: str
    title: str
    content: str


@dataclass
class ParsedCV:
    preamble: str          # tất cả đến hết \begin{document}
    sections: list[Section]
    postamble: str         # từ \end{document} trở đi
    macros: list[str] = field(default_factory=list)  # dòng \newcommand (gửi cho Claude để hiểu macro)

    def get(self, sid: str) -> Section | None:
        return next((s for s in self.sections if s.id == sid), None)

    def assemble(self) -> str:
        body = "".join(s.content for s in self.sections)
        if not body.endswith("\n"):
            body += "\n"
        return self.preamble + body + self.postamble


def _section_commands(preamble: str) -> list[str]:
    names = {"section", "cvsection"}
    for m in NEWCMD.finditer(preamble):
        name, rest = m.group(1), m.group(2)
        if "sec" in name.lower() or "\\section" in rest:
            names.add(name)
    return sorted(names, key=len, reverse=True)


def _brace_arg(text: str, start: int) -> str:
    """Lấy nội dung {…} đầu tiên từ vị trí start (có xử lý ngoặc lồng nhau)."""
    i = text.find("{", start)
    if i < 0:
        return ""
    depth = 0
    for j in range(i, len(text)):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return text[i + 1:j]
    return text[i + 1:]


def parse_cv(tex: str) -> ParsedCV:
    b = BEGIN_DOC.search(tex)
    e = END_DOC.search(tex)
    if not b or not e:
        raise ValueError("File .tex thiếu \\begin{document} hoặc \\end{document}.")
    preamble, body, postamble = tex[:b.end()], tex[b.end():e.start()], tex[e.start():]
    body = FULL_LINE_COMMENT.sub("", body)  # comment không cần gửi cho Claude

    cmds = _section_commands(preamble)
    sec_re = re.compile(r"(?m)^[ \t]*\\(" + "|".join(map(re.escape, cmds)) + r")\*?\s*(?:\[[^\]]*\])?\s*\{")
    starts = [m.start() for m in sec_re.finditer(body)]

    sections: list[Section] = []
    bounds = [0] + starts + [len(body)]
    for k in range(len(bounds) - 1):
        chunk = body[bounds[k]:bounds[k + 1]]
        if k == 0:
            if chunk.strip():
                sections.append(Section("header", "Header (tên, liên hệ)", chunk))
            elif chunk:
                sections.append(Section("header", "Header", chunk))
            continue
        title = re.sub(r"\\[A-Za-z]+|[{}\\\"]", "", _brace_arg(chunk, 0)).strip() or f"Section {k}"
        sections.append(Section(f"s{k}", title, chunk))

    macros = [m.group(0).strip() for m in NEWCMD.finditer(preamble)]
    return ParsedCV(preamble, sections, postamble, macros)


def sections_for_prompt(cv: ParsedCV) -> str:
    parts = []
    if cv.macros:
        parts.append("<macros>\n" + "\n".join(cv.macros) + "\n</macros>")
    for s in cv.sections:
        parts.append(f'<cv_section id="{s.id}" title="{s.title}">\n{s.content.strip()}\n</cv_section>')
    return "\n\n".join(parts)


def _loose_pattern(old: str) -> re.Pattern:
    tokens = old.split()
    return re.compile(r"\s+".join(map(re.escape, tokens)))


CONTACT_MARK = re.compile(r"\\href|mailto:|\\vert|\\sep\b|\\faPhone|\\faEnvelope|\+\d[\d ]{6,}")


def _contact_span(lines: list[str]) -> tuple[int, int] | None:
    """Vị trí (dòng đầu, dòng cuối) của khối thông tin liên hệ trong header."""
    idx = [i for i, l in enumerate(lines) if CONTACT_MARK.search(l)]
    return (idx[0], idx[-1]) if idx else None


def one_line_contacts(header: str) -> str:
    """Gộp khối liên hệ (SĐT | email | GitHub | ...) thành MỘT dòng nguồn duy nhất.

    Với LaTeX, xuống dòng đơn chỉ là dấu cách nên PDF không đổi, nhưng mọi chỗ hiển thị
    theo dòng (khung diff của app, trình soạn thảo, Claude) sẽ thấy đúng một hàng.
    """
    lines = header.split("\n")
    span = _contact_span(lines)
    if not span or span[0] == span[1]:
        return header
    a, b = span
    joined = " ".join(l.strip().rstrip("%").strip() for l in lines[a:b + 1] if l.strip())
    return "\n".join(lines[:a] + [joined] + lines[b + 1:])


def protect_contacts(original: str, edited: str) -> str:
    """Trả lại khối liên hệ gốc nếu Claude đã sửa nó (thêm \\\\, dòng trống, \\par... làm xuống hàng)."""
    o_lines, e_lines = original.split("\n"), edited.split("\n")
    o_span, e_span = _contact_span(o_lines), _contact_span(e_lines)
    if not o_span:
        return edited
    block = o_lines[o_span[0]:o_span[1] + 1]
    if not e_span:  # Claude xoá mất khối liên hệ -> giữ header gốc
        return original
    if e_lines[e_span[0]:e_span[1] + 1] == block:
        return edited
    return "\n".join(e_lines[:e_span[0]] + block + e_lines[e_span[1] + 1:])


def apply_edits(cv: ParsedCV, edits: list[dict], order: list[str] | None = None) -> tuple[list[dict], list[dict]]:
    """Áp dụng edit lên cv (in-place). Trả về (applied, failed)."""
    applied, failed = [], []
    head = cv.get("header")
    if head is not None:
        head.content = one_line_contacts(head.content)
    head_before = head.content if head is not None else None
    for ed in edits:
        sec = cv.get(ed.get("section_id", ""))
        if sec is None:
            failed.append({**ed, "error": "không tìm thấy section"})
            continue
        before = sec.content
        if ed.get("op") == "replace_section":
            new = ed.get("new", "")
            sec.content = new.rstrip() + "\n\n"
            applied.append({**ed, "section_title": sec.title, "old": before.strip()})
            continue
        old, new = ed.get("old", ""), ed.get("new", "")
        if not old:
            failed.append({**ed, "error": "thiếu đoạn 'old'"})
            continue
        n = sec.content.count(old)
        if n == 1:
            sec.content = sec.content.replace(old, new, 1)
        else:
            matches = list(_loose_pattern(old).finditer(sec.content)) if old.split() else []
            if len(matches) != 1:
                failed.append({**ed, "error": f"đoạn 'old' khớp {len(matches) if n == 0 else n} lần"})
                continue
            m = matches[0]
            sec.content = sec.content[:m.start()] + new + sec.content[m.end():]
        applied.append({**ed, "section_title": sec.title})

    if head is not None and head.content != head_before:
        head.content = protect_contacts(head_before, head.content)

    if order:
        by_id = {s.id: s for s in cv.sections}
        head = [s for s in cv.sections if s.id == "header"]
        ordered = [by_id[i] for i in order if i in by_id and i != "header"]
        rest = [s for s in cv.sections if s not in ordered and s.id != "header"]
        for s in ordered + rest:  # đảm bảo mỗi section kết thúc bằng dòng trống khi đổi vị trí
            if not s.content.endswith("\n\n"):
                s.content = s.content.rstrip("\n") + "\n\n"
        cv.sections = head + ordered + rest
    return applied, failed


# ---------------------------------------------------------------- compile

def _latex_dirs() -> list[str]:
    """Thư mục cài LaTeX phổ biến, dùng khi chương trình chưa có trong PATH (hay gặp trên Windows)."""
    import glob
    import os
    dirs = [os.environ.get("CV_TAILOR_LATEX_DIR", "")]
    local, prog = os.environ.get("LOCALAPPDATA", ""), os.environ.get("ProgramFiles", r"C:\Program Files")
    if os.name == "nt":
        dirs += [
            os.path.join(local, "Programs", "MiKTeX", "miktex", "bin", "x64"),
            os.path.join(prog, "MiKTeX", "miktex", "bin", "x64"),
            os.path.join(prog, "MiKTeX 2.9", "miktex", "bin", "x64"),
            os.path.join(local, "Programs", "tectonic"),
            os.path.join(os.path.expanduser("~"), "tectonic"),
        ]
        for pat in (r"C:\texlive\*\bin\windows", r"C:\texlive\*\bin\win64", r"C:\texlive\*\bin\win32"):
            dirs += sorted(glob.glob(pat), reverse=True)
    else:
        dirs += ["/Library/TeX/texbin", "/opt/homebrew/bin", "/usr/local/bin", os.path.expanduser("~/.cargo/bin")]
        dirs += sorted(glob.glob("/usr/local/texlive/*/bin/*"), reverse=True)
    return [d for d in dirs if d and os.path.isdir(d)]


def _which(name: str) -> str | None:
    return shutil.which(name) or next((p for d in _latex_dirs() if (p := shutil.which(name, path=d))), None)


def find_engine(tex: str) -> list[str] | None:
    needs_xe = bool(re.search(r"\\usepackage(\[[^\]]*\])?\{(fontspec|polyglossia)\}", tex))
    if exe := _which("tectonic"):
        return [exe, "--keep-logs", "--chatter", "minimal"]
    # Gọi thẳng pdflatex/xelatex trước latexmk: latexmk của MiKTeX cần cài thêm Perl.
    for eng in (["xelatex", "pdflatex"] if needs_xe else ["pdflatex"]):
        if exe := _which(eng):
            return [exe, "-interaction=nonstopmode", "-halt-on-error"]
    if exe := _which("latexmk"):
        return [exe, "-pdfxe" if needs_xe else "-pdf", "-interaction=nonstopmode", "-halt-on-error"]
    return None


NO_ENGINE_MSG = ("Không tìm thấy trình biên dịch LaTeX (tectonic / pdflatex). Trên Windows: cài MiKTeX "
                 "(https://miktex.org/download hoặc lệnh: winget install ChristianSchenk.MiKTeX), mở MiKTeX Console > "
                 "Settings > chọn 'Install missing packages: Always', rồi tắt app (Ctrl+C) và chạy lại run.bat. "
                 "File .tex đã được lưu; bấm 'Biên dịch lại PDF' sau khi cài xong.")


@dataclass
class CompileResult:
    ok: bool
    pdf: bytes | None
    log_tail: str
    pages: int | None


def compile_tex(tex: str, name: str = "cv") -> CompileResult:
    engine = find_engine(tex)
    if engine is None:
        return CompileResult(False, None, NO_ENGINE_MSG, None)
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / f"{name}.tex"
        p.write_text(tex, encoding="utf-8")
        try:
            r = subprocess.run(engine + [p.name], cwd=d, capture_output=True, text=True, timeout=600, errors="replace")
            out = r.stdout + r.stderr
        except subprocess.TimeoutExpired:
            return CompileResult(False, None, "Biên dịch quá 10 phút (lần đầu MiKTeX có thể đang tải package, hãy thử lại).", None)
        log = Path(d) / f"{name}.log"
        if log.exists():
            out = log.read_text(errors="replace")
        pdf = Path(d) / f"{name}.pdf"
        if r.returncode != 0 or not pdf.exists():
            return CompileResult(False, None, _error_excerpt(out), None)
        data = pdf.read_bytes()
    return CompileResult(True, data, "", _count_pages(data))


def _error_excerpt(log: str, max_lines: int = 25) -> str:
    lines = log.splitlines()
    idx = next((i for i, l in enumerate(lines) if l.startswith("!") or "error:" in l.lower()), None)
    if idx is None:
        return "\n".join(lines[-max_lines:])
    return "\n".join(lines[idx:idx + max_lines])


def _count_pages(pdf: bytes) -> int | None:
    try:
        import io
        from pypdf import PdfReader
        return len(PdfReader(io.BytesIO(pdf)).pages)
    except Exception:
        return None
