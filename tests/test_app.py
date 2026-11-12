"""Kiểm thử không cần API key: thay client Claude bằng bản giả."""
import json
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app import latex, llm, main

SAMPLE = Path(__file__).parent / "sample_cv.tex"


def test_parse_roundtrip():
    tex = SAMPLE.read_text()
    cv = latex.parse_cv(tex)
    titles = [s.title for s in cv.sections]
    assert titles[0].startswith("Header") and "Technical Skills" in titles and "Education" in titles
    assert "documentclass" not in latex.sections_for_prompt(cv)  # preamble không gửi đi
    assert latex.parse_cv(cv.assemble()).assemble() == cv.assemble()


def test_apply_edits_loose_whitespace_and_failures():
    cv = latex.parse_cv(SAMPLE.read_text())
    sid = next(s.id for s in cv.sections if s.title == "Technical Skills")
    applied, failed = latex.apply_edits(cv, [
        {"section_id": sid, "op": "replace_text", "old": "\\textbf{Programming   Languages:}\nPython", "new": "\\textbf{Programming Languages:} Python", "reason": ""},
        {"section_id": sid, "op": "replace_text", "old": "khong ton tai", "new": "x", "reason": ""},
        {"section_id": "s99", "op": "replace_text", "old": "a", "new": "b", "reason": ""},
    ])
    assert len(applied) == 1 and len(failed) == 2


class FakeMessages:
    def __init__(self):
        self.calls = []

    def _resp(self, payload):
        u = SimpleNamespace(input_tokens=100, output_tokens=50, cache_creation_input_tokens=0, cache_read_input_tokens=0)
        return SimpleNamespace(stop_reason="end_turn", usage=u, content=[SimpleNamespace(type="text", text=json.dumps(payload))])

    def _jd(self):
        return self._resp({"job_title": "ML Engineer", "company": "Acme AI", "seniority": "Mid", "domain": "Fintech",
                           "must_have": ["Python", "LightGBM", "MLOps"], "nice_to_have": ["Kubernetes"],
                           "responsibilities": ["Build fraud models"], "keywords": ["fraud detection", "MLflow", "Kubernetes"]})

    def stream(self, **kw):
        self.calls.append(kw)
        if "job_title" in kw["output_config"]["format"]["schema"]["properties"]:
            return self._wrap(self._jd())
        cv_text = kw["messages"][0]["content"][0]["text"]
        sid = cv_text.split('title="Selected Projects"')[0].rsplit('id="', 1)[1].split('"')[0]
        payload = {"edits": [{"section_id": sid, "op": "replace_text",
                              "old": "Fraud risk scoring for card transactions on the IEEE-CIS payments dataset.",
                              "new": "Real-time \\textbf{fraud detection} and risk scoring for card transactions (IEEE-CIS dataset).",
                              "reason": "Khớp từ khoá fraud detection"}],
                   "section_order": [], "match_summary": "Phù hợp tốt.", "matched_keywords": ["fraud detection", "MLflow"],
                   "missing_requirements": ["Kubernetes"]}
        return self._wrap(self._resp(payload))

    @staticmethod
    def _wrap(resp):
        class S:
            def __enter__(s): return s
            def __exit__(s, *a): return False
            def get_final_message(s): return resp
        return S()


def test_end_to_end(tmp_path, monkeypatch):
    fake = SimpleNamespace(messages=FakeMessages())
    monkeypatch.setattr(llm, "_client", fake)
    monkeypatch.setattr(llm, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(main, "OUTPUT_DIR", tmp_path / "out")
    c = TestClient(main.app)

    jd = c.post("/api/jd", data={"backend": "api", "mode": "text", "text": "We need an ML engineer for fraud detection. " * 5}).json()
    assert jd["summary"]["company"] == "Acme AI" and not jd["cached"]
    assert c.post("/api/jd", data={"mode": "text", "text": "We need an ML engineer for fraud detection. " * 5}).json()["cached"]

    r = c.post("/api/tailor", data={"cv_text": SAMPLE.read_text(), "cv_name": "Tam CV", "jd_summary": json.dumps(jd["summary"]),
                                    "model": "claude-opus-5-5", "effort": "medium", "keep_pages": "true", "backend": "api"})
    assert r.status_code == 200, r.text
    r = r.json()
    assert len(r["applied"]) == 1 and not r["failed"] and r["missing_requirements"] == ["Kubernetes"]
    tex = c.get(r["files"]["tex"]).text
    assert "Real-time \\textbf{fraud detection}" in tex and tex.startswith("\\documentclass")
    if latex.find_engine(tex):
        assert r["compile"]["ok"], r["compile"]["error"]
        assert c.get(r["files"]["pdf"]).content[:4] == b"%PDF"
    # system prompt và khối CV đều có cache_control
    call = fake.messages.calls[-1]
    assert call["system"][0]["cache_control"] and call["messages"][0]["content"][0]["cache_control"]
    assert "documentclass" not in call["messages"][0]["content"][0]["text"]


def test_claude_code_backend(tmp_path, monkeypatch):
    """Giả lập CLI `claude -p`: kiểm tra lệnh được gọi đúng và API key bị gỡ khỏi môi trường."""
    seen = {}

    def fake_run(cmd, input, cwd, env, **kw):
        seen.update(cmd=cmd, input=input, env=env, files=sorted(p.name for p in Path(cwd).iterdir()))
        out = {"is_error": False, "structured_output": {"job_title": "X", "company": "Y", "seniority": "", "domain": "",
               "must_have": [], "nice_to_have": [], "responsibilities": [], "keywords": []},
               "usage": {"input_tokens": 5, "output_tokens": 7, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}
        return SimpleNamespace(stdout=json.dumps(out), stderr="", returncode=0)

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(llm.shutil, "which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr(llm.subprocess, "run", fake_run)
    monkeypatch.setattr(llm, "CACHE_DIR", tmp_path)
    summary, usage, cached = llm.summarize_jd(image=(b"\x89PNG fake", "image/png"), backend="claude-code")
    assert summary["company"] == "Y" and usage["cost_usd"] == 0 and not cached
    assert "ANTHROPIC_API_KEY" not in seen["env"]
    assert seen["cmd"][seen["cmd"].index("--tools") + 1] == "Read" and "jd.png" in seen["files"]
    assert "--json-schema" in seen["cmd"] and "./jd.png" in seen["input"]


def test_save_dir_name_and_recompile(tmp_path, monkeypatch):
    fake = SimpleNamespace(messages=FakeMessages())
    monkeypatch.setattr(llm, "_client", fake)
    monkeypatch.setattr(llm, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(main, "OUTPUT_DIR", tmp_path / "out")
    c = TestClient(main.app)
    target = tmp_path / "Ho so" / "CV"
    target.mkdir(parents=True)
    (target / "Tam_Acme.tex").write_text("ban cu")  # file sẵn có không được ghi đè
    jd = {"job_title": "ML", "company": "Acme", "seniority": "", "domain": "", "must_have": [], "nice_to_have": [],
          "responsibilities": [], "keywords": []}
    data = {"cv_text": SAMPLE.read_text(), "jd_summary": json.dumps(jd), "backend": "api",
            "save_dir": str(target), "file_name": "Tam_Acme.pdf"}

    # 1) máy chưa có LaTeX: vẫn lưu .tex, báo lỗi biên dịch
    real_find = latex.find_engine
    monkeypatch.setattr(latex, "find_engine", lambda tex: None)
    r = c.post("/api/tailor", data=data).json()
    assert not r["compile"]["ok"] and "MiKTeX" in r["compile"]["error"]
    assert r["saved"]["paths"] == [str(target / "Tam_Acme_2.tex")]
    assert (target / "Tam_Acme.tex").read_text() == "ban cu"

    # 2) cài LaTeX xong, bấm biên dịch lại: PDF nằm cạnh file .tex đã lưu
    monkeypatch.setattr(latex, "find_engine", real_find)
    if real_find(""):
        j = c.post("/api/compile", data={"run": r["run"]}).json()
        assert j["compile"]["ok"] and (target / "Tam_Acme_2.pdf").read_bytes()[:4] == b"%PDF"

    assert c.post("/api/tailor", data={**data, "save_dir": "relative/dir"}).status_code == 400


def test_password_for_remote_clients(monkeypatch):
    monkeypatch.setattr(main, "PASSWORD", "bimat")
    monkeypatch.setattr(main, "LOOPBACK", {"127.0.0.1"})  # coi TestClient là máy khác
    c = TestClient(main.app)
    assert c.get("/api/config").status_code == 401
    assert c.get("/api/config", auth=("ai-cung-duoc", "sai")).status_code == 401
    r = c.get("/api/config", auth=("ai-cung-duoc", "bimat"))
    assert r.status_code == 200 and r.json()["remote"] is True
    assert c.post("/api/pick-folder", auth=("x", "bimat")).status_code == 400


def test_coursework_bank_sent_to_claude(tmp_path, monkeypatch):
    fake = SimpleNamespace(messages=FakeMessages())
    monkeypatch.setattr(llm, "_client", fake)
    monkeypatch.setattr(main, "OUTPUT_DIR", tmp_path / "out")
    monkeypatch.setattr(main, "COURSES_FILE", tmp_path / "courses.md")
    c = TestClient(main.app)
    assert c.post("/api/courses", data={"text": "# chu thich\n## UIT\n- Databases (10/10)\n"}).json()["ok"]
    assert "Databases" in c.get("/api/courses").json()["text"]
    jd = json.dumps({"job_title": "ML", "company": "Acme", "seniority": "", "domain": "", "must_have": [],
                     "nice_to_have": [], "responsibilities": [], "keywords": []})
    base = {"cv_text": SAMPLE.read_text(), "jd_summary": jd, "backend": "api"}
    c.post("/api/tailor", data=base)
    sent = fake.messages.calls[-1]["messages"][0]["content"][0]["text"]
    assert "<coursework_bank>\n## UIT\n- Databases (10/10)\n</coursework_bank>" in sent and "chu thich" not in sent
    c.post("/api/tailor", data={**base, "use_courses": "false"})
    assert "coursework_bank" not in fake.messages.calls[-1]["messages"][0]["content"][0]["text"]


def test_header_contacts_stay_on_one_line():
    cv = latex.parse_cv(SAMPLE.read_text())
    head = cv.get("header").content
    broken = head.replace(" $\\vert$\n\\href{https://github", " $\\vert$\\\\\n\n\\href{https://github")
    applied, failed = latex.apply_edits(cv, [
        {"section_id": "header", "op": "replace_section", "old": "", "new": broken.replace("AI Engineer", "ML Engineer"), "reason": ""},
    ])
    new = cv.get("header").content
    assert not failed and "ML Engineer" in new  # sửa headline vẫn được giữ
    contact = [l for l in new.splitlines() if "mailto:" in l]
    assert len(contact) == 1 and "Google Scholar" in contact[0] and "\\\\" not in new
    assert sum("\\href" in l for l in new.splitlines()) == 1
    # PDF giữ nguyên: chỉ thay xuống dòng bằng dấu cách
    assert " ".join(new.split()) == " ".join(head.replace("AI Engineer", "ML Engineer").split())
