"""Gọi Claude API.

Chiến lược tiết kiệm token:
1. JD -> bản tóm tắt yêu cầu gọn (JSON) bằng model rẻ (Haiku). Kết quả được cache trên đĩa theo hash.
2. Chỉ gửi phần thân CV theo section (không gửi preamble LaTeX), bỏ comment.
3. Claude chỉ trả về các edit nhỏ (old -> new) thay vì viết lại cả file.
4. System prompt cố định + khối CV được đánh dấu prompt caching: sửa cùng một CV cho
   nhiều JD liên tiếp sẽ đọc CV từ cache (rẻ hơn ~10-20 lần).

Hai cách kết nối:
- "api": Anthropic SDK, cần ANTHROPIC_API_KEY (trả tiền theo token).
- "claude-code": gọi CLI `claude -p` đã đăng nhập bằng tài khoản claude.ai (dùng hạn mức gói Pro/Max).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import anthropic

EXTRACT_MODEL = os.environ.get("CV_TAILOR_EXTRACT_MODEL", "claude-haiku-5-5")
DEFAULT_MODEL = os.environ.get("CV_TAILOR_MODEL", "claude-opus-5-5")
CACHE_DIR = Path(os.environ.get("CV_TAILOR_DATA", Path(__file__).resolve().parent.parent / "data")) / "jd_cache"

# USD / 1M token: (input, output, cache_read). Cache write tính 1.25 x input (TTL 5 phút).
PRICES = {
    "claude-opus-5-5": (4.0, 20.0, 0.20),
    "claude-sonnet-5-5": (2.0, 10.0, 0.20),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
}

BACKENDS = ("api", "claude-code")
CLAUDE_BIN = os.environ.get("CV_TAILOR_CLAUDE_BIN", "claude")
IMAGE_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif", "application/pdf": ".pdf"}

_client: anthropic.Anthropic | None = None


def available_backends() -> list[str]:
    out = []
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        out.append("api")
    if shutil.which(CLAUDE_BIN):
        out.append("claude-code")
    return out


def default_backend() -> str:
    env = os.environ.get("CV_TAILOR_BACKEND")
    if env in BACKENDS:
        return env
    avail = available_backends()
    return avail[0] if avail else "api"


def client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic()  # đọc ANTHROPIC_API_KEY từ biến môi trường
    return _client


class LLMError(RuntimeError):
    pass


def usage_dict(model: str, u) -> dict:
    d = {
        "model": model,
        "input": u.input_tokens or 0,
        "output": u.output_tokens or 0,
        "cache_write": getattr(u, "cache_creation_input_tokens", 0) or 0,
        "cache_read": getattr(u, "cache_read_input_tokens", 0) or 0,
    }
    pin, pout, pread = PRICES.get(model, (0, 0, 0))
    d["cost_usd"] = round((d["input"] * pin + d["cache_write"] * pin * 1.25 + d["cache_read"] * pread + d["output"] * pout) / 1e6, 5)
    return d


def _text_of(resp) -> str:
    if resp.stop_reason == "refusal":
        raise LLMError("Claude từ chối yêu cầu này.")
    if resp.stop_reason == "max_tokens":
        raise LLMError("Phản hồi bị cắt do vượt max_tokens.")
    t = next((b.text for b in resp.content if b.type == "text"), None)
    if t is None:
        raise LLMError("Claude không trả về nội dung văn bản.")
    return t


def _run(backend: str, *, model: str, system: str, schema: dict, effort: str, text: str,
         cached_text: str | None = None, attachment: tuple[bytes, str] | None = None,
         cache_system: bool = False, max_tokens: int = 16000) -> tuple[dict, dict]:
    """Một lượt gọi Claude trả về JSON theo schema. backend = "api" hoặc "claude-code"."""
    if backend == "claude-code":
        return _run_claude_code(model=model, system=system, schema=schema, effort=effort,
                                text=(cached_text + "\n\n" if cached_text else "") + text, attachment=attachment)

    content: list[dict] = []
    if attachment:
        data, media = attachment
        kind = "document" if media == "application/pdf" else "image"
        content.append({"type": kind, "source": {"type": "base64", "media_type": media, "data": base64.b64encode(data).decode()}})
    if cached_text:
        # Khối CV có cache_control: tái sử dụng khi chạy cùng CV với JD khác (trong 5 phút).
        content.append({"type": "text", "text": cached_text, "cache_control": {"type": "ephemeral"}})
    content.append({"type": "text", "text": text})
    sys_param = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}] if cache_system else system
    try:
        with client().messages.stream(
            model=model,
            max_tokens=max_tokens,
            system=sys_param,
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
            messages=[{"role": "user", "content": content}],
        ) as stream:
            resp = stream.get_final_message()
    except anthropic.APIError as e:
        raise LLMError(f"Lỗi Claude API: {e}") from e
    return json.loads(_text_of(resp)), usage_dict(model, resp.usage)


def _run_claude_code(*, model: str, system: str, schema: dict, effort: str, text: str,
                     attachment: tuple[bytes, str] | None) -> tuple[dict, dict]:
    """Gọi Claude Code CLI (`claude -p`) - dùng hạn mức gói Claude Pro/Max thay vì API trả tiền."""
    exe = shutil.which(CLAUDE_BIN)
    if not exe:
        raise LLMError("Không tìm thấy lệnh `claude`. Cài Claude Code: npm install -g @anthropic-ai/claude-code, "
                       "rồi chạy `claude` một lần để đăng nhập bằng tài khoản claude.ai.")
    # Bỏ API key khỏi môi trường để Claude Code dùng tài khoản claude.ai đã đăng nhập.
    env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "system.txt").write_text(system, encoding="utf-8")
        tools = ""
        if attachment:
            data, media = attachment
            fname = "jd" + IMAGE_EXT.get(media, ".bin")
            (Path(d) / fname).write_bytes(data)
            text = f"First read the file ./{fname} (the job description) with the Read tool.\n\n{text}"
            tools = "Read"
        cmd = [exe, "-p", "--output-format", "json", "--json-schema", json.dumps(schema),
               "--system-prompt-file", "system.txt", "--model", model, "--effort", effort,
               "--tools", tools, "--no-session-persistence"]
        try:
            r = subprocess.run(cmd, input=text, cwd=d, env=env, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=900)
        except subprocess.TimeoutExpired:
            raise LLMError("Claude Code chạy quá 15 phút.")
    try:
        out = json.loads(r.stdout)
    except json.JSONDecodeError:
        msg = (r.stdout + r.stderr).strip()[-800:]
        raise LLMError(f"Claude Code lỗi (bạn đã chạy `claude` để đăng nhập chưa?): {msg}")
    if out.get("is_error") or out.get("structured_output") is None:
        raise LLMError(f"Claude Code lỗi: {out.get('result') or out.get('subtype') or 'không có kết quả'}")
    u = out.get("usage") or {}
    usage = {
        "model": model,
        "input": u.get("input_tokens", 0),
        "output": u.get("output_tokens", 0),
        "cache_write": u.get("cache_creation_input_tokens", 0),
        "cache_read": u.get("cache_read_input_tokens", 0),
        "cost_usd": 0.0,  # tính vào hạn mức gói claude.ai, không trừ tiền
        "plan": True,
    }
    return out["structured_output"], usage


# ------------------------------------------------------------------ JD

JD_SCHEMA = {
    "type": "object",
    "properties": {
        "job_title": {"type": "string"},
        "company": {"type": "string"},
        "seniority": {"type": "string"},
        "domain": {"type": "string"},
        "must_have": {"type": "array", "items": {"type": "string"}},
        "nice_to_have": {"type": "array", "items": {"type": "string"}},
        "responsibilities": {"type": "array", "items": {"type": "string"}},
        "keywords": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["job_title", "company", "seniority", "domain", "must_have", "nice_to_have", "responsibilities", "keywords"],
    "additionalProperties": False,
}

JD_SYSTEM = (
    "You extract job requirements from a job description (JD) for CV tailoring. "
    "Be terse: each list item is a short phrase (max ~8 words), no duplicates, at most 12 items per list, "
    "ordered by importance. 'keywords' = exact technical terms/tools/methods an ATS would scan for, "
    "spelled as in the JD. Write in the JD's language for phrases but keep tool names verbatim. "
    "Use empty string when a field is unknown. Ignore benefits, salary, legal boilerplate and company marketing."
)


def _cache_get(key: str) -> dict | None:
    p = CACHE_DIR / f"{key}.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def _cache_put(key: str, data: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (CACHE_DIR / f"{key}.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def summarize_jd(*, text: str | None = None, image: tuple[bytes, str] | None = None,
                 pdf: bytes | None = None, backend: str = "api") -> tuple[dict, dict | None, bool]:
    """Trả về (summary, usage, from_cache). Chỉ một trong text / image / pdf."""
    raw = text.encode() if text else (image[0] if image else pdf)
    key = hashlib.sha256(EXTRACT_MODEL.encode() + raw).hexdigest()[:32]
    if (hit := _cache_get(key)) is not None:
        return hit, None, True

    if text:
        prompt, attachment = f"<jd>\n{text}\n</jd>", None
    elif image:
        prompt, attachment = "The image is a screenshot of a JD. Extract its requirements.", image
    else:
        prompt, attachment = "The PDF is a JD. Extract its requirements.", (pdf, "application/pdf")
    summary, usage = _run(backend, model=EXTRACT_MODEL, system=JD_SYSTEM, schema=JD_SCHEMA, effort="low",
                          text=prompt, attachment=attachment, max_tokens=4000)
    _cache_put(key, summary)
    return summary, usage, False


def jd_to_prompt(s: dict) -> str:
    """Biểu diễn gọn của JD (ít token hơn JSON đầy đủ)."""
    lines = [f"Role: {s.get('job_title','')} | Company: {s.get('company','')} | Level: {s.get('seniority','')} | Domain: {s.get('domain','')}"]
    for k, label in (("must_have", "Must-have"), ("nice_to_have", "Nice-to-have"),
                     ("responsibilities", "Responsibilities"), ("keywords", "ATS keywords")):
        if s.get(k):
            lines.append(f"{label}: " + "; ".join(s[k]))
    return "\n".join(lines)


# ------------------------------------------------------------------ tailor

EDIT_SCHEMA = {
    "type": "object",
    "properties": {
        "edits": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "section_id": {"type": "string"},
                    "op": {"type": "string", "enum": ["replace_text", "replace_section"]},
                    "old": {"type": "string"},
                    "new": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["section_id", "op", "old", "new", "reason"],
                "additionalProperties": False,
            },
        },
        "section_order": {"type": "array", "items": {"type": "string"}},
        "match_summary": {"type": "string"},
        "matched_keywords": {"type": "array", "items": {"type": "string"}},
        "missing_requirements": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["edits", "section_order", "match_summary", "matched_keywords", "missing_requirements"],
    "additionalProperties": False,
}

# Cố định từng byte để prompt caching hoạt động - không chèn thời gian/biến vào đây.
TAILOR_SYSTEM = """You are an expert technical recruiter and LaTeX CV editor. You tailor an existing CV to a specific job description (JD) so it passes ATS keyword screening and convinces a hiring manager, while staying 100% truthful.

INPUT
- <macros>: the CV's custom LaTeX macro definitions (for reference only; the preamble is not editable).
- <cv_section id=...>: the body of the CV split into sections. Section "header" holds name/contact/headline.
- <jd>: a compact summary of the job requirements.
- <coursework_bank> (optional): every university course the candidate actually took, grouped by school ("## School").
- <options>: user preferences.

HARD RULES (truthfulness)
1. Never invent experience, employers, dates, degrees, titles, metrics, publications, certifications or skills that are not already stated or directly implied by the CV text (courses listed in <coursework_bank> also count as true). Do not change numbers or dates.
2. You may: rephrase bullets to use the JD's terminology when it describes the same thing; reorder bullets, skills, projects and sections to put the most relevant first; emphasise relevant items with \\textbf; shorten or drop clearly irrelevant bullets/items; adjust the headline/title line to the target role only if the CV supports it; move an existing skill into a more prominent place.
3. If the JD requires something the CV does not show, do NOT add it. List it in "missing_requirements" (short phrases, in Vietnamese) so the candidate can decide.
4. Keep the CV in its current language. Do not touch contact details, links, names of people or publication metadata. In the header, never edit, split or re-wrap the contact line (phone | email | links): it must stay one line with no \\\\, \\par or blank lines. To change the headline, use replace_text on the headline only.

LATEX RULES
5. Output valid LaTeX that compiles with the existing preamble. Only use commands already used in the CV or standard LaTeX (\\textbf, \\textit, \\item, \\par, \\hfill, \\\\). Escape & % $ # _ { } in plain text. Keep \\begin/\\end pairs balanced inside each section. Preserve the existing macro calls and spacing commands.
6. Keep roughly the same length as the original (the CV must not gain pages). When you add words somewhere, trim elsewhere.

EDIT FORMAT (token efficiency)
7. Return ONLY the changes, as edits:
   - op "replace_text": "old" is an exact, verbatim substring copied from that section (long enough to be unique, typically one full \\item line or one skills line); "new" is its replacement. Use "new": "" to delete. Prefer this for small changes.
   - op "replace_section": "old" = "", "new" = the full new content of the section, starting with its section command. Use only when most of a section changes (e.g. reordering many items).
   - Never put two edits on overlapping text. Do not output unchanged sections.
8. "section_order": list of section ids (excluding "header") in the new order, or [] to keep the order.
9. "reason": max ~12 words, in Vietnamese, why this edit improves fit.
10. "match_summary": 2-3 sentences in Vietnamese on how well the CV fits and what you emphasised. "matched_keywords": JD keywords now present in the CV.
11. Be selective: make the edits with the highest impact on fit (usually 4-15 edits). Do not rewrite text that is already well aligned.

COURSEWORK (only when <coursework_bank> is given)
12. For each school in the Education section, rewrite its "Related courses"/"Relevant coursework" line with the 3-6 courses from THAT school's bank that best match the JD, most relevant first. You may swap out courses currently in the CV. Copy course names (and grades, if the bank shows them) exactly; never move a course to another school and never use a course that is not in the bank. If a school has no such line, add one in the same style as the other schools only if the page budget allows. If the current selection is already the best fit, leave it unchanged."""


def tailor(cv_prompt: str, jd_summary: dict, *, model: str, effort: str, options: str,
           backend: str = "api") -> tuple[dict, dict]:
    return _run(backend, model=model, system=TAILOR_SYSTEM, schema=EDIT_SCHEMA, effort=effort,
                cached_text=cv_prompt, cache_system=True, max_tokens=32000,
                text=f"<jd>\n{jd_to_prompt(jd_summary)}\n</jd>\n\n<options>\n{options}\n</options>")


FIX_SYSTEM = """You fix LaTeX compile errors or length problems in CV sections. Change as little as possible, never add new facts. Return edits in the given JSON format: op "replace_text" with an exact verbatim "old" substring from the section, or op "replace_section" with the full new section content. section_order = [], matched_keywords = [], missing_requirements = [], match_summary = short Vietnamese note of what you fixed."""


def fix(sections_prompt: str, problem: str, *, model: str, backend: str = "api") -> tuple[dict, dict]:
    return _run(backend, model=model, system=FIX_SYSTEM, schema=EDIT_SCHEMA, effort="low",
                text=f"{sections_prompt}\n\n<problem>\n{problem}\n</problem>")
