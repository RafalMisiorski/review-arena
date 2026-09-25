# Published copy of the locked scoring script (NH lock 0bf38bdbb976 hashes the original); one vendor's identifiers are masked as vendor_d because its terms forbid publishing benchmark results.
"""M42 arena -- a sealed bug-injection benchmark of AI pull-request reviewers.

    python data/measurements/m42/arena.py round0                # local dry run: noise neutrality, caps, 2 dry branches, CLI + judge paths
    python data/measurements/m42/arena.py build                 # vendor corpus, select sealed mutations, seal the key, build main + 34 branches (local)
    python data/measurements/m42/arena.py push                  # create the public arena repo and push main + branches (key.sealed on main FIRST)
    python data/measurements/m42/arena.py open                  # open the PRs in seeded order; request Copilot review (best effort)
    python data/measurements/m42/arena.py collect               # vendor bot comments -> findings (idempotent snapshot)
    python data/measurements/m42/arena.py cli codex|claude      # run a CLI arm over every PR branch (resumable)
    python data/measurements/m42/arena.py judge                 # blind cross-vendor panel over every finding (resumable, batched)
    python data/measurements/m42/arena.py score                 # reveal the key, R12, seal the result

State lives in runs/m42/. Every stage after `build` verifies the lock. ASCII-only console output.
"""
from __future__ import annotations

import ast
import asyncio
import io
import json
import random
import re
import shutil
import subprocess
import sys
import time
import tokenize
from datetime import datetime, timezone
from pathlib import Path

M42 = Path(__file__).resolve().parent
NH = M42.parents[2]
if str(NH) not in sys.path:
    sys.path.insert(0, str(NH))
SEALEVAL_SRC = Path("D:/Projects/sealeval/src")
if str(SEALEVAL_SRC) not in sys.path:
    sys.path.insert(0, str(SEALEVAL_SRC))

from app.benchmark.code_review.sealing import prereg  # noqa: E402
from sealeval.measure.confirm import match_findings  # noqa: E402
from sealeval.measure.stats import wilson_ci  # noqa: E402
from sealeval.mutation import catalog  # noqa: E402
from sealeval.sealing.keyseal import make_seal, verify_seal  # noqa: E402

LOCK = M42 / "prereg.lock.json"
RUNS = M42 / "runs" / "m42"
WORK = Path(__import__("os").environ.get("TEMP", "/tmp")) / "m42_arena"
CLEAN = Path(__import__("os").environ.get("TEMP", "/tmp")) / "m42_requests"
NO_TOOLS = ["Bash", "Read", "Write", "Edit", "MultiEdit", "NotebookEdit", "Glob", "Grep", "WebSearch", "WebFetch", "Agent", "Task"]

CONFIG = {
    "corpus": {"repo": "https://github.com/psf/requests", "commit": "4c800e9aea2059660b8306b0fc8f9e9a4232cb3e", "subdir": "src/requests",
               "license": "Apache-2.0"},
    "plan": {"n_bug": 24, "caps": {"inverted_condition": 8, "swallowed_exception": 6, "wrong_operator": 5, "null_deref": 4, "off_by_one": 1},
             "n_clean": 8, "n_control": 2, "noise_main": [4, 10], "noise_carrier": [2, 4], "max_mutations_per_file": 3},
    "seed": 20260924,
    "arena": {"repo": "RafalMisiorski/review-arena", "default_branch": "main", "branch_prefix": "pr-"},
    "arms": {"vendor_d": "vendor-d[bot]", "greptile": "greptile-apps[bot]", "qodo": "qodo-merge-pro[bot]", "sourcery": "sourcery-ai[bot]",
             "copilot": "copilot-pull-request-reviewer[bot]", "codex": "codex review --base main (default model)",
             "claude": "claude-fable-5 via call_claude, tools disallowed"},
    "bot_aliases": {"codiumai-pr-agent-pro[bot]": "qodo", "qodo-merge-for-open-source[bot]": "qodo", "pr-agent[bot]": "qodo", "qodo-code-review[bot]": "qodo",   # D29
                    "sourcery-ai-bot[bot]": "sourcery"},
    "disclosure": {"vendor_d": "results withheld under the vendor's terms of service"},
    "judge": {"line_tolerance": 2, "mode": "both_genuine", "panel": ["claude-fable-5", "codex default"], "batch": 8},
    "archetype_version": "v2",
}
REVIEW_PROMPT = (
    "You are reviewing a pull request for REAL, present defects introduced by the change (logic errors: inverted "
    "conditions, off-by-one, wrong operator/comparison, None/null dereference, swallowed exceptions). Report ONLY defects "
    "you are confident are real bugs in the code as shown after the change. Do not report style issues, wording of "
    "comments or docstrings, TODOs, missing features, or hypothetical improvements. Output ONLY a JSON array: "
    "[{\"file\": \"<path as in the diff>\", \"line\": <int, line number in the NEW file>, \"claim\": \"<one-sentence defect description>\"}]. "
    "If there are none, output []."
)
JUDGE_SYSTEM = (
    "You are an independent, skeptical senior engineer verifying code-review claims. For each claim you are shown a "
    "CODE EXCERPT (numbered lines, post-change) from the file it refers to. Judge each claim ON ITS MERITS against the "
    "code shown. Try HARD to REFUTE it: default to REFUTED unless the described defect is concretely present and "
    "triggerable. GENUINE_BUG = a real correctness / security / data-loss defect, concretely present; REAL_NOT_BUG = the "
    "described thing is real but a style / design / latent-only issue; REFUTED = the mechanism or consequence is not "
    "present. Respond with ONLY a JSON array, one object per claim id: "
    "[{\"id\": \"...\", \"verdict\": \"GENUINE_BUG\"|\"REAL_NOT_BUG\"|\"REFUTED\", \"mechanism\": \"<= 1 sentence\"}]"
)
NEUTRAL_TITLES = ["housekeeping: {m} tidy-ups", "chore: small cleanups in {m}", "minor: {m} maintenance", "tidy {m}", "cleanup: {m}"]


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def L() -> dict:
    if not prereg.verify(LOCK):
        raise SystemExit("M42: lock does not verify -- refusing to run")
    return json.loads(LOCK.read_text(encoding="utf-8"))["content"]


def run(cmd: list, cwd: Path | None = None, check: bool = True, timeout: int = 600) -> subprocess.CompletedProcess:
    p = subprocess.run(cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    if check and p.returncode != 0:
        raise RuntimeError("%s failed (%d): %s" % (" ".join(cmd[:3]), p.returncode, (p.stderr or p.stdout)[-400:]))
    return p


def git(args: list, cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    return run(["git", "-c", "user.email=rafal.misiorski@gmail.com", "-c", "user.name=Rafal Misiorski", "-c", "core.autocrlf=false"] + args, cwd, check)


# ----------------------------------------------------------------------------- corpus and noise


def corpus_files(clean_root: Path) -> list:
    sub = clean_root / CONFIG["corpus"]["subdir"]
    return sorted(p.relative_to(clean_root).as_posix() for p in sub.glob("*.py"))


_SUBS = [(" which ", " that "), (" e.g. ", " for example "), ("  ", " "), (" the ", " this ")]


def _is_triple(s: str) -> bool:
    return s.lstrip("rRbBuUfF")[:3] in ('"""', "'''")


def _noise_candidates(src: str) -> list:
    """(line, col) pairs: a comment starting at ``col`` on ``line``, or an inner docstring line (col 0)."""
    out = []
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except (tokenize.TokenError, SyntaxError):
        return out
    for t in toks:
        if t.type == tokenize.COMMENT and t.string.strip("# ").strip():
            out.append((t.start[0], t.start[1]))
        elif t.type == tokenize.STRING and _is_triple(t.string) and "\n" in t.string and "{" not in t.string and "%" not in t.string:
            for ln in range(t.start[0] + 1, t.end[0]):        # inner lines only; delimiter lines untouched
                out.append((ln, 0))
    return sorted(set(out))


def _reword(line: str, col: int = 0) -> str | None:
    """Reword ONLY the text from ``col`` on (the comment) or the whole line (docstring interior)."""
    eol = "\r\n" if line.endswith("\r\n") else ("\n" if line.endswith("\n") else "")
    body = line[: len(line) - len(eol)]
    head, text = body[:col], body[col:]
    if not text.strip() or '"""' in text or "'''" in text or text.lstrip().startswith("#!"):
        return None
    for a, b in _SUBS:
        if a in text:
            if a == "  " and (text.lstrip().startswith("#") or col == 0 and text.startswith(" ")):
                continue
            return head + text.replace(a, b, 1) + eol
    words = text.strip().lstrip("# ").split()
    if len(words) >= 3 and not text.rstrip().endswith((".", ":", ",", ")", "`", "'", '"')):
        return head + text.rstrip() + "." + eol
    return None


def noise_edits(src: str, k: int, rng: random.Random, exclude_lines=()) -> tuple:
    """Apply up to ``k`` behaviour-neutral rewordings on comment / docstring lines. Returns (new_src, edited_lines)."""
    lines = src.splitlines(keepends=True)
    cands = [(ln, col) for ln, col in _noise_candidates(src) if ln not in set(exclude_lines) and ln <= len(lines)]
    rng.shuffle(cands)
    edited = []
    for ln, col in cands:
        if len(edited) >= k or ln in edited:
            continue
        new = _reword(lines[ln - 1], col)
        if new is None or new == lines[ln - 1]:
            continue
        lines[ln - 1] = new
        edited.append(ln)
    return "".join(lines), sorted(edited)


def neutral_check(a: str, b: str) -> bool:
    """True iff every token differs only inside COMMENT or triple-quoted STRING tokens (line structure identical)."""
    try:
        ta = [t for t in tokenize.generate_tokens(io.StringIO(a).readline) if t.type not in (tokenize.NL, tokenize.NEWLINE)]
        tb = [t for t in tokenize.generate_tokens(io.StringIO(b).readline) if t.type not in (tokenize.NL, tokenize.NEWLINE)]
    except (tokenize.TokenError, SyntaxError):
        return False
    if len(ta) != len(tb) or a.count("\n") != b.count("\n"):
        return False
    for x, y in zip(ta, tb):
        if x.string == y.string and x.type == y.type:
            continue
        if x.type != y.type:
            return False
        if x.type == tokenize.COMMENT:
            continue
        if x.type == tokenize.STRING and _is_triple(x.string) and _is_triple(y.string) and x.string[:4] == y.string[:4]:
            continue
        return False
    return True


# ----------------------------------------------------------------------------- mutations, controls, plan


def select_mutations(clean_root: Path, plan: dict, seed: int, version: str = "v2") -> list:
    """Seeded selection of exactly one candidate per BUG PR under the archetype caps; <= max per file."""
    rng = random.Random(seed)
    pool = []
    for rel in corpus_files(clean_root):
        src = (clean_root / rel).read_text(encoding="utf-8")
        for c in catalog.find_candidates(src, catalog.MVP_ARCHETYPES, version=version):
            pool.append((rel, c))
    pool.sort(key=lambda rc: (rc[0], rc[1].line, rc[1].archetype))
    rng.shuffle(pool)
    # scarcity-first: the rarest archetypes pick their files before the abundant ones fill the per-file slots
    size = {}
    for _, c in pool:
        size[c.archetype] = size.get(c.archetype, 0) + 1
    pool.sort(key=lambda rc: size.get(rc[1].archetype, 0))
    caps = dict(plan["caps"])
    per_file = {}
    chosen, used = [], set()
    for rel, c in pool:
        if caps.get(c.archetype, 0) <= 0 or per_file.get(rel, 0) >= int(plan["max_mutations_per_file"]) or (rel, c.line) in used:
            continue
        src = (clean_root / rel).read_text(encoding="utf-8")
        mutated = catalog.replace_span(src, c.node, c.new_src)
        try:
            compile(mutated, rel, "exec")
        except SyntaxError:
            continue
        original = catalog.source_segment(src, c.node) or ""
        chosen.append({"file": rel, "line": c.line, "col": int(getattr(c.node, "col_offset", 0)), "archetype": c.archetype,
                       "description": c.description, "original_segment": original, "mutated_segment": c.new_src, "mutated_src": mutated})
        caps[c.archetype] -= 1
        per_file[rel] = per_file.get(rel, 0) + 1
        used.add((rel, c.line))
        if len(chosen) >= int(plan["n_bug"]):
            break
    return chosen


CONTROLS = [
    {"file": "src/requests/_bounds.py", "line": 8, "archetype": "control", "description": "clamp returns the wrong bound",
     "content": '"""Small numeric helpers used by adapters and utilities."""\n\n\ndef clamp(value, low, high):\n    """Return ``value`` limited to the closed interval [low, high].\n\n    ``clamp(15, 0, 10)`` is ``10``; ``clamp(-3, 0, 10)`` is ``0``.\n    """\n    return min(low, max(value, high))\n'},
    {"file": "src/requests/_parity.py", "line": 7, "archetype": "control", "description": "is_even tests for odd",
     "content": '"""Parity helpers used when splitting request bodies into chunks."""\n\n\ndef is_even(n):\n    """Return True when ``n`` is an even integer (2, 4, 6, ...)."""\n    n = int(n)\n    return n % 2 == 1\n'},
]


def make_plan(clean_root: Path, plan: dict, seed: int, version: str = "v2") -> tuple:
    """(prs, key). prs = ordered PR specs with kinds; key = the sealed injection records (BUG PRs only)."""
    rng = random.Random(seed + 1)
    muts = select_mutations(clean_root, plan, seed, version)
    if len(muts) < int(plan["n_bug"]):
        raise RuntimeError("only %d mutations selected, need %d" % (len(muts), plan["n_bug"]))
    # only files that can carry the noise budget are eligible as clean / carrier files (a PR must differ from main)
    files = [f for f in corpus_files(clean_root)
             if len(_noise_candidates((clean_root / f).read_text(encoding="utf-8"))) >= int(plan["noise_main"][1]) + 2]
    specs = []
    for m in muts:
        carrier = rng.choice([f for f in files if f != m["file"]])
        specs.append({"kind": "bug", "file": m["file"], "carrier": carrier, "mutation": m,
                      "noise_main": rng.randint(*plan["noise_main"]), "noise_carrier": rng.randint(*plan["noise_carrier"])})
    for _ in range(int(plan["n_clean"])):
        f1, f2 = rng.sample(files, 2)
        specs.append({"kind": "clean", "file": f1, "carrier": f2, "mutation": None,
                      "noise_main": rng.randint(*plan["noise_main"]), "noise_carrier": rng.randint(*plan["noise_carrier"])})
    for c in CONTROLS[: int(plan["n_control"])]:
        specs.append({"kind": "control", "file": c["file"], "carrier": rng.choice(files), "mutation": None, "control": c,
                      "noise_main": 0, "noise_carrier": rng.randint(*plan["noise_carrier"])})
    rng.shuffle(specs)
    prs = []
    for i, s in enumerate(specs):
        mod = Path(s["file"]).stem.strip("_") or "utils"
        s = dict(s)
        s["branch"] = "%s%03d" % (CONFIG["arena"]["branch_prefix"], i + 1)
        s["title"] = rng.choice(NEUTRAL_TITLES).format(m=mod)
        s["body"] = "Small cleanups; no functional change intended."
        prs.append(s)
    key = [{"file": p["mutation"]["file"], "line": p["mutation"]["line"], "col": p["mutation"]["col"], "archetype": p["mutation"]["archetype"],
            "description": p["mutation"]["description"], "original_segment": p["mutation"]["original_segment"],
            "mutated_segment": p["mutation"]["mutated_segment"], "branch": p["branch"]} for p in prs if p["kind"] == "bug"]
    return prs, key


def apply_spec(clean_root: Path, spec: dict, seed: int) -> dict:
    """Return {path: new_content} for a PR spec; asserts noise neutrality on every noised file."""
    rng = random.Random(seed + sum(ord(ch) for ch in spec["branch"]))
    out = {}
    if spec["kind"] == "control":
        out[spec["file"]] = spec["control"]["content"]
    else:
        src = (clean_root / spec["file"]).read_text(encoding="utf-8")
        base = spec["mutation"]["mutated_src"] if spec["kind"] == "bug" else src
        excl = (spec["mutation"]["line"],) if spec["kind"] == "bug" else ()
        noised, edited = noise_edits(base, spec["noise_main"], rng, exclude_lines=excl)
        if not neutral_check(base, noised):
            raise RuntimeError("noise not neutral on %s" % spec["file"])
        out[spec["file"]] = noised
        spec["noise_lines_main"] = edited
    csrc = (clean_root / spec["carrier"]).read_text(encoding="utf-8")
    cnoised, cedited = noise_edits(csrc, spec["noise_carrier"], rng)
    if not neutral_check(csrc, cnoised):
        raise RuntimeError("carrier noise not neutral on %s" % spec["carrier"])
    out[spec["carrier"]] = cnoised
    spec["noise_lines_carrier"] = cedited
    if spec["kind"] == "clean" and not spec.get("noise_lines_main") and not cedited:
        raise RuntimeError("clean PR %s would be identical to main" % spec["branch"])
    return out


# ----------------------------------------------------------------------------- arena repo


README = """# review-arena

This repository hosts a **pre-registered benchmark of AI pull-request reviewers**. `src/requests/` is a verbatim copy
of [psf/requests](https://github.com/psf/requests) at commit `{commit}` (Apache-2.0; see LICENSE and NOTICE). Pull
requests on this repository are benchmark items: some contain exactly one real single-line defect, some contain none.
The ground truth was sealed (`key.sealed`, sha-256 over salt + key) and committed to `main` **before** the first pull
request was opened; the key is revealed only after every reviewer under test has reviewed. Protocol, results and the
scoring code are published from the pre-registration in the operator's measurement ledger.

Please do not merge these pull requests; they exist to be reviewed, not shipped.
"""
NOTICE = "This repository vendors psf/requests (Apache License 2.0) at commit {commit} for a code-review benchmark.\nCopyright 2019 Kenneth Reitz. Licensed under the Apache License, Version 2.0.\n"


def _rmtree(path: Path) -> None:
    import os
    import stat

    def _onexc(func, p, exc):   # Windows: git objects are read-only
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except OSError:
            pass
    shutil.rmtree(path, onexc=_onexc)


def build_arena(work: Path, clean_root: Path, prs: list, key: list, seed: int, salt: str | None = None) -> dict:
    if work.exists():
        _rmtree(work)
    work.mkdir(parents=True)
    git(["init", "-q", "-b", CONFIG["arena"]["default_branch"]], work)
    (work / "src" / "requests").mkdir(parents=True)
    for rel in corpus_files(clean_root):   # normalise to LF so a branch diff is only what the branch changed
        (work / rel).write_text((clean_root / rel).read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
    (work / "LICENSE").write_text((clean_root / "LICENSE").read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
    (work / ".gitattributes").write_text("* text=auto eol=lf\n", encoding="utf-8", newline="\n")
    (work / "NOTICE").write_text(NOTICE.format(commit=CONFIG["corpus"]["commit"]), encoding="utf-8")
    (work / "README.md").write_text(README.format(commit=CONFIG["corpus"]["commit"]), encoding="utf-8")
    (work / ".gitignore").write_text("__pycache__/\n*.pyc\n", encoding="utf-8")
    seal = make_seal(key, salt=salt, count=len(key))
    (work / "key.sealed").write_text(json.dumps(seal, indent=2), encoding="utf-8")
    git(["add", "-A"], work)
    git(["commit", "-q", "-m", "arena: vendored corpus + sealed key (before any pull request)"], work)
    main_sha = git(["rev-parse", "HEAD"], work).stdout.strip()
    manifest = {"ts": now(), "main_sha": main_sha, "seal": seal, "branches": []}
    for spec in prs:
        git(["checkout", "-q", "-b", spec["branch"], CONFIG["arena"]["default_branch"]], work)
        changes = apply_spec(clean_root, spec, seed)
        for rel, content in changes.items():
            (work / rel).parent.mkdir(parents=True, exist_ok=True)
            (work / rel).write_text(content, encoding="utf-8", newline="\n")
        git(["add", "-A"], work)
        git(["commit", "-q", "-m", spec["title"]], work)
        manifest["branches"].append({"branch": spec["branch"], "kind": spec["kind"], "title": spec["title"], "files": sorted(changes),
                                     "sha": git(["rev-parse", "HEAD"], work).stdout.strip(),
                                     "noise_lines_main": spec.get("noise_lines_main", []), "noise_lines_carrier": spec.get("noise_lines_carrier", [])})
        git(["checkout", "-q", CONFIG["arena"]["default_branch"]], work)
    return manifest


def diff_for(work: Path, branch: str) -> str:
    return git(["diff", "%s...%s" % (CONFIG["arena"]["default_branch"], branch), "--", "."], work).stdout


def file_at(work: Path, branch: str, rel: str) -> str:
    return git(["show", "%s:%s" % (branch, rel)], work, check=False).stdout


# ----------------------------------------------------------------------------- arms


def _json_array(raw: str) -> list:
    t = (raw or "").strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    i, j = t.find("["), t.rfind("]")
    if i < 0 or j <= i:
        return []
    try:
        arr = json.loads(t[i:j + 1])
    except ValueError:
        return []
    return arr if isinstance(arr, list) else []


def parse_findings(raw: str, arm: str, branch: str, allowed_files: set) -> list:
    out = []
    for o in _json_array(raw):
        if not isinstance(o, dict):
            continue
        f = str(o.get("file", "")).replace("\\", "/").lstrip("./")
        f = next((a for a in allowed_files if a == f or a.endswith("/" + f) or f.endswith(a)), f)
        try:
            ln = int(o.get("line"))
        except (TypeError, ValueError):
            continue
        claim = str(o.get("claim", "")).strip()
        if claim:
            out.append({"arm": arm, "branch": branch, "file": f, "line": ln, "claim": claim[:400]})
    return out


CODEX_FINDING_RE = re.compile(r"^- \[P\d\]\s*(?P<title>.+?)\s+[—–-]+\s+(?P<path>\S+?):(?P<start>\d+)(?:-(?P<end>\d+))?\s*$", re.M)


def parse_codex_native(raw: str, arm: str, branch: str, allowed_files: set, work: Path) -> list:
    """Codex's native review output: '- [P2] <title> -- <abs path>:<start>-<end>' followed by an indented paragraph.
    The stream repeats the final block; identical findings are deduplicated."""
    out, seen = [], set()
    text = (raw or "").replace("\r\n", "\n")
    root = work.resolve().as_posix().lower()
    for m in CODEX_FINDING_RE.finditer(text):
        p = m.group("path").replace("\\", "/")
        pl = p.lower()
        if pl.startswith(root + "/"):
            p = p[len(root) + 1:]
        p = p.lstrip("./")
        p = next((a for a in allowed_files if a == p or a.endswith("/" + p) or p.endswith(a)), p)
        body = text[m.end():].split("\n\n", 1)[0].strip()
        claim = (m.group("title").strip() + ". " + " ".join(ln.strip() for ln in body.splitlines()))[:400]
        key = (p, int(m.group("start")), m.group("title").strip())
        if key in seen:
            continue
        seen.add(key)
        out.append({"arm": arm, "branch": branch, "file": p, "line": int(m.group("start")), "claim": claim})
    return out


def claude_review_prompt(work: Path, branch: str, files: list) -> str:
    parts = [REVIEW_PROMPT, "", "=== DIFF (main...%s) ===" % branch, diff_for(work, branch)]
    for rel in files:
        text = file_at(work, branch, rel)
        parts += ["", "=== FILE %s (post-change, numbered) ===" % rel] + ["%d: %s" % (i + 1, ln) for i, ln in enumerate(text.splitlines())]
    return "\n".join(parts)


async def _claude_review(prompt: str, timeout: int = 900) -> str:
    from scripts.llm_call import call_claude
    return await call_claude(prompt, model="claude-fable-5", timeout=timeout, disallowed_tools=NO_TOOLS)


async def _codex_review(work: Path, timeout: int = 900) -> str:
    from scripts.llm_call import call_codex_review
    return await call_codex_review("", base=CONFIG["arena"]["default_branch"], cwd=str(work), timeout=timeout)   # native review


def _cost(model: str, purpose: str, duration_s: float) -> None:
    try:
        from app.measure import post_seal_review as PSR
        PSR.cost_row(model=model, purpose=purpose, duration_s=duration_s, ledger=PSR._default_cost_ledger())
    except Exception:  # noqa: BLE001
        pass


def run_cli_arm(arm: str, work: Path, manifest: dict, out_path: Path, caller=None) -> dict:
    done = {}
    if out_path.exists():
        for line in out_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                done[r["branch"]] = r
    for b in manifest["branches"]:
        if b["branch"] in done:
            continue
        files = b["files"]
        t0 = time.time()
        if caller is not None:
            raw = caller(arm, work, b)
        elif arm == "claude":
            raw = asyncio.run(_claude_review(claude_review_prompt(work, b["branch"], files)))
        else:
            git(["checkout", "-q", b["branch"]], work)
            try:
                raw = asyncio.run(_codex_review(work))
            finally:
                git(["checkout", "-q", CONFIG["arena"]["default_branch"]], work)
        dur = round(time.time() - t0, 1)
        _cost(arm, "m42_arm_%s" % arm, dur)
        findings = parse_codex_native(raw, arm, b["branch"], set(files), work) if arm == "codex" else parse_findings(raw, arm, b["branch"], set(files))
        rec = {"branch": b["branch"], "arm": arm, "duration_s": dur, "n_findings": len(findings), "findings": findings, "raw": (raw or "")[-3000:]}
        with out_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=True) + "\n")
        done[b["branch"]] = rec
        print("[m42] %s %s: %d findings in %.0fs" % (arm, b["branch"], len(findings), dur))
    return done


def gh_json(path: str) -> list:
    p = run(["gh", "api", "--paginate", path], check=False)
    try:
        return json.loads(p.stdout) if p.stdout.strip() else []
    except ValueError:
        return []


def collect_vendor(prs_map: dict, arms: dict) -> list:
    """prs_map: branch -> PR number. Inline review comments by bot login -> findings; summaries counted as noise."""
    by_login = {v: k for k, v in arms.items() if v.endswith("[bot]")}
    by_login.update(CONFIG.get("bot_aliases", {}))
    repo = CONFIG["arena"]["repo"]
    out = []
    unknown = set()
    for branch, num in prs_map.items():
        for c in gh_json("repos/%s/pulls/%d/comments" % (repo, num)):
            login = (c.get("user") or {}).get("login", "")
            if login not in by_login:
                if login.endswith("[bot]"):
                    unknown.add(login)
                continue
            ln = c.get("line") or c.get("original_line")
            out.append({"arm": by_login[login], "branch": branch, "file": c.get("path"), "line": int(ln) if ln else None,
                        "claim": (c.get("body") or "")[:400], "kind": "inline", "id": c.get("id")})
        for r in gh_json("repos/%s/pulls/%d/reviews" % (repo, num)):
            login = (r.get("user") or {}).get("login", "")
            if login in by_login and (r.get("body") or "").strip():
                out.append({"arm": by_login[login], "branch": branch, "file": None, "line": None, "claim": (r.get("body") or "")[:400], "kind": "summary", "id": r.get("id")})
        for c in gh_json("repos/%s/issues/%d/comments" % (repo, num)):
            login = (c.get("user") or {}).get("login", "")
            if login in by_login:
                out.append({"arm": by_login[login], "branch": branch, "file": None, "line": None, "claim": (c.get("body") or "")[:400], "kind": "summary", "id": c.get("id")})
    if unknown:
        print("[m42] unmapped bot logins seen (add to bot_aliases if they are arms): %s" % sorted(unknown))
    return out


# ----------------------------------------------------------------------------- judging


def excerpt(text: str, line: int | None, window: int = 60, max_full: int = 220) -> str:
    lines = text.splitlines()
    if not lines:
        return "(empty file)"
    if len(lines) <= max_full or not line:
        return "\n".join("%d: %s" % (i + 1, ln) for i, ln in enumerate(lines[:max_full]))
    lo, hi = max(0, int(line) - window), min(len(lines), int(line) + window)
    return "\n".join("%d: %s" % (i + 1, lines[i]) for i in range(lo, hi))


def judge_prompt(batch: list, work: Path) -> str:
    parts = [JUDGE_SYSTEM, ""]
    for f in batch:
        text = file_at(work, f["branch"], f["file"]) if f.get("file") else ""
        parts += ["=== CLAIM %s ===" % f["id"], "file: %s  line: %s" % (f.get("file"), f.get("line")), "claim: %s" % f["claim"], "",
                  "--- excerpt ---", excerpt(text, f.get("line")), ""]
    parts.append("Answer with the JSON array only.")
    return "\n".join(parts)


def parse_verdicts(raw: str, ids: set) -> dict:
    out = {}
    for o in _json_array(raw):
        if isinstance(o, dict) and o.get("id") in ids:
            v = str(o.get("verdict", "")).strip().upper()
            if v in ("GENUINE_BUG", "REAL_NOT_BUG", "REFUTED"):
                out[o["id"]] = {"verdict": v, "mechanism": str(o.get("mechanism", ""))[:200]}
    return out


async def _judge_call(vendor: str, prompt: str, timeout: int = 900) -> str:
    from scripts.llm_call import call_claude, call_codex
    if vendor == "codex":
        import tempfile
        with tempfile.TemporaryDirectory(prefix="m42_judge_") as d:
            return await call_codex(prompt, sandbox="read-only", cwd=d, timeout=timeout)
    return await call_claude(prompt, model="claude-fable-5", timeout=timeout, disallowed_tools=NO_TOOLS)


def judge_all(findings: list, work: Path, out_path: Path, batch_size: int = 8, caller=None) -> dict:
    """Blind: the panel never sees the arm. Resumable per (vendor, claim id)."""
    verdicts = json.loads(out_path.read_text(encoding="utf-8")) if out_path.exists() else {"claude": {}, "codex": {}}
    items = [f for f in findings if f.get("file") and f.get("line")]
    for i, f in enumerate(items):
        f["id"] = f.get("id") or "c%04d" % (i + 1)
    for vendor in ("claude", "codex"):
        todo = [f for f in items if str(f["id"]) not in verdicts[vendor]]
        for k in range(0, len(todo), batch_size):
            batch = todo[k:k + batch_size]
            prompt = judge_prompt([{**b, "id": str(b["id"])} for b in batch], work)
            t0 = time.time()
            raw = caller(vendor, prompt) if caller else asyncio.run(_judge_call(vendor, prompt))
            _cost(vendor, "m42_judge", round(time.time() - t0, 1))
            got = parse_verdicts(raw, {str(b["id"]) for b in batch})
            for b in batch:
                verdicts[vendor][str(b["id"])] = got.get(str(b["id"]), {"verdict": "UNPARSED", "mechanism": ""})
            out_path.write_text(json.dumps(verdicts, indent=1), encoding="utf-8")
            print("[m42] judge %s: %d/%d parsed" % (vendor, len(got), len(batch)))
    return verdicts


# ----------------------------------------------------------------------------- scoring


def score(findings: list, verdicts: dict, key: list, manifest: dict, Lc: dict) -> dict:
    tol = int(Lc["judge"]["line_tolerance"])
    kinds = {b["branch"]: b["kind"] for b in manifest["branches"]}
    n_bug = len(key)
    arms = sorted({f["arm"] for f in findings})
    res = {"ts": now(), "n_bug": n_bug, "n_prs": len(manifest["branches"]), "arms": {}, "rule": Lc["rules"]["R12"]}
    ctrl_files = {c["file"]: c["line"] for c in CONTROLS}
    per_arm_matched = {}
    for arm in arms:
        fs = [f for f in findings if f["arm"] == arm]
        inline = [f for f in fs if f.get("file") and f.get("line")]
        genuine = [f for f in inline if verdicts["claude"].get(str(f.get("id")), {}).get("verdict") == "GENUINE_BUG"
                   and verdicts["codex"].get(str(f.get("id")), {}).get("verdict") == "GENUINE_BUG"]
        # controls: any inline finding on the control file within tolerance counts (obviousness, not the panel)
        ctrl_hit = {}
        for cf, cl in ctrl_files.items():
            ctrl_hit[cf] = any(f["file"] == cf and abs(int(f["line"]) - cl) <= tol for f in inline)
        void = sum(ctrl_hit.values()) == 0
        # recall on sealed bugs: matched by a GENUINE finding on the same branch, same file, within tolerance
        matched = set()
        for k_i, rec in enumerate(key):
            for f in genuine:
                if f["branch"] == rec["branch"] and f["file"] == rec["file"] and abs(int(f["line"]) - int(rec["line"])) <= tol:
                    matched.add(k_i)
                    break
        per_arm_matched[arm] = matched
        tp = len(matched)
        clean_fp = [f for f in inline if kinds.get(f["branch"]) == "clean"]
        unseeded = [f for f in genuine if not any(f["branch"] == r["branch"] and f["file"] == r["file"] and abs(int(f["line"]) - int(r["line"])) <= tol for r in key)]
        by_arch = {}
        for a in Lc["plan"]["caps"]:
            idx = [i for i, r in enumerate(key) if r["archetype"] == a]
            by_arch[a] = {"n": len(idx), "found": sum(1 for i in idx if i in matched)}
        prs_seen = {f["branch"] for f in fs}
        res["arms"][arm] = {"findings_inline": len(inline), "summaries": sum(1 for f in fs if f.get("kind") == "summary"),
                            "genuine": len(genuine), "recall": round(tp / n_bug, 4) if n_bug else None, "recall_ci95": wilson_ci(tp, n_bug),
                            "precision": round(len(genuine) / len(inline), 4) if inline else None, "precision_ci95": wilson_ci(len(genuine), len(inline)) if inline else None,
                            "clean_pr_findings": len(clean_fp), "noise_per_pr": round(len(inline) / max(1, len(manifest["branches"])), 3),
                            "controls": ctrl_hit, "void": void, "unseeded_genuine": len(unseeded), "by_archetype": by_arch, "prs_with_output": len(prs_seen)}
    live = [a for a in arms if not res["arms"][a]["void"]]
    union = set().union(*[per_arm_matched[a] for a in live]) if live else set()
    best = max((res["arms"][a]["recall"] or 0.0 for a in live), default=0.0)
    overlap = {}
    for i, a in enumerate(live):
        for b in live[i + 1:]:
            overlap["%s_x_%s" % (a, b)] = len(per_arm_matched[a] & per_arm_matched[b])
    res.update({"live": live, "void": [a for a in arms if a not in live], "union_recall": round(len(union) / n_bug, 4) if n_bug else None,
                "best_recall": best, "overlap": overlap})
    recs = {a: res["arms"][a]["recall"] or 0.0 for a in live}
    precs = {a: res["arms"][a]["precision"] for a in live if res["arms"][a]["precision"] is not None}
    noise = {a: res["arms"][a]["noise_per_pr"] for a in live}
    arch_found = {a: sum(res["arms"][x]["by_archetype"][a]["found"] for x in live) for a in Lc["plan"]["caps"]} if live else {}
    arch_rate = {a: (arch_found[a] / (Lc["plan"]["caps"][a] * max(1, len(live)))) for a in arch_found}
    vendors = [a for a in ("vendor_d", "greptile", "gemini_code_assist", "copilot") if a in live]
    clis = [a for a in ("codex", "claude") if a in live]
    preds = {"P59": bool(live) and best < 0.60,
             "P60": bool(precs) and max(precs.values()) < 0.70,
             "P61": bool(precs) and bool(noise) and (max(noise, key=noise.get) == min(precs, key=precs.get)),
             "P62": bool(clis) and bool(vendors) and max(recs[c] for c in clis) >= max(recs[v] for v in vendors),
             "P63": bool(live) and (res["union_recall"] or 0.0) <= best + 0.15,
             "P64": bool(res["void"]),
             "P65": bool(arch_rate) and min(arch_rate, key=arch_rate.get) == "swallowed_exception",
             "P66": len([a for a in ("vendor_d", "greptile", "gemini_code_assist", "copilot") if a not in arms]) >= 2}
    res["predictions_resolved"] = preds
    res["verdict"] = "SEALED_TABLE" if live else "VOID"
    return res


def render(res: dict) -> str:
    md = ["# M42 result -- sealed bug-injection benchmark of AI PR reviewers", "",
          "**%s.** %d sealed bugs across %d pull requests. Live arms: %s; void: %s; union recall %s; best single %s." % (
              res["verdict"], res["n_bug"], res["n_prs"], ", ".join(res["live"]) or "-", ", ".join(res["void"]) or "-", res["union_recall"], res["best_recall"]), "",
          "| arm | inline findings | genuine (both judges) | recall | Wilson | precision | Wilson | clean-PR findings | noise/PR | controls | unseeded genuine |",
          "|---|---|---|---|---|---|---|---|---|---|---|"]
    for a, r in res["arms"].items():
        md.append("| %s | %d | %d | %s | %s | %s | %s | %d | %s | %s%s | %d |" % (
            a, r["findings_inline"], r["genuine"], r["recall"], r["recall_ci95"], r["precision"], r["precision_ci95"], r["clean_pr_findings"], r["noise_per_pr"],
            "/".join("ok" if v else "MISS" for v in r["controls"].values()), " VOID" if r["void"] else "", r["unseeded_genuine"]))
    md += ["", "Per archetype (found / n):", ""]
    for a, r in res["arms"].items():
        md.append("- %s: " % a + ", ".join("%s %d/%d" % (k, v["found"], v["n"]) for k, v in r["by_archetype"].items()))
    md += ["", "Overlap of sealed bugs found (pairs): %s" % json.dumps(res["overlap"]),
           "", "Predictions: " + ", ".join("%s %s" % (k, "TRUE" if v else "FALSE") for k, v in res["predictions_resolved"].items()),
           "", "Rule: %s" % res["rule"], ""]
    return "\n".join(md)


# ----------------------------------------------------------------------------- stages


def ensure_clean(clean_root: Path = CLEAN) -> Path:
    if not (clean_root / "src" / "requests").exists():
        run(["git", "clone", "-q", CONFIG["corpus"]["repo"], str(clean_root)])
    run(["git", "checkout", "-q", CONFIG["corpus"]["commit"]], clean_root)
    return clean_root


def stage_round0(caller_cli=None, caller_judge=None, online: bool = True, reuse: bool = False) -> dict:
    RUNS.mkdir(parents=True, exist_ok=True)
    clean = ensure_clean()
    files = corpus_files(clean)
    rng = random.Random(7)
    neutral = []
    for rel in files:
        src = (clean / rel).read_text(encoding="utf-8")
        noised, edited = noise_edits(src, 6, rng)
        neutral.append({"file": rel, "edits": len(edited), "neutral": neutral_check(src, noised)})
    pool = {}
    for rel in files:
        for c in catalog.find_candidates((clean / rel).read_text(encoding="utf-8"), catalog.MVP_ARCHETYPES, version=CONFIG["archetype_version"]):
            pool[c.archetype] = pool.get(c.archetype, 0) + 1
    caps_ok = all(pool.get(a, 0) >= n for a, n in CONFIG["plan"]["caps"].items())
    prs, key = make_plan(clean, CONFIG["plan"], CONFIG["seed"] + 999, CONFIG["archetype_version"])   # dry seed, never the real one
    dry = WORK.parent / "m42_dry"
    manifest = build_arena(dry, clean, [p for p in prs if p["kind"] == "bug"][:1] + [p for p in prs if p["kind"] == "clean"][:1], key[:1], CONFIG["seed"] + 999, salt="dry")
    dry_diff_lines = {b["branch"]: diff_for(dry, b["branch"]).count("\n") for b in manifest["branches"]}
    cli = {}
    if online:
        for arm in ("codex", "claude"):
            out = RUNS / ("round0_%s.jsonl" % arm)
            if out.exists() and not reuse:
                out.unlink()
            got = run_cli_arm(arm, dry, manifest, out, caller=caller_cli)
            # "usable output": the tool answered. Claude: a JSON list (possibly empty). Codex (native review): non-empty text that is
            # not a transport error; findings, if any, parse from its structured block. Phrasing of "nothing to flag" is not policed.
            cli[arm] = {b: {"n_findings": r["n_findings"],
                            "usable": (len(r["raw"].strip()) > 20 and "TRANSPORT_ERROR" not in r["raw"]) if arm == "codex"
                            else (re.search(r"\[\s*\]", r["raw"]) is not None or bool(_json_array(r["raw"])))} for b, r in got.items()}
        fixture = [{"id": "c0001", "arm": "x", "branch": manifest["branches"][0]["branch"], "file": key[0]["file"], "line": key[0]["line"], "claim": key[0]["description"]}]
        jp = RUNS / "round0_verdicts.json"
        if jp.exists():
            jp.unlink()
        v = judge_all(fixture, dry, jp, caller=caller_judge)
        judge_ok = all(v[x].get("c0001", {}).get("verdict") in ("GENUINE_BUG", "REAL_NOT_BUG", "REFUTED") for x in ("claude", "codex"))
    else:
        judge_ok = None
    m = match_findings([{"file": key[0]["file"], "line": key[0]["line"] + 1, "verdict": "GENUINE_BUG"}], [{"file": key[0]["file"], "line": key[0]["line"]}], line_tolerance=2)
    ok = all(n["neutral"] for n in neutral) and caps_ok and len(key) == CONFIG["plan"]["n_bug"] and m.get("tp") == 1 and (judge_ok in (True, None)) \
        and (not online or all(all(x["usable"] for x in arm.values()) for arm in cli.values()))
    r0 = {"ts": now(), "noise_neutral": neutral, "candidate_pool": pool, "caps_ok": caps_ok, "plan": {"prs": len(prs), "bugs": len(key),
          "kinds": {k: sum(1 for p in prs if p["kind"] == k) for k in ("bug", "clean", "control")}},
          "dry_branches": dry_diff_lines, "cli_paths": cli, "judge_paths_ok": judge_ok, "match_fixture_tp": m.get("tp"),
          "verdict": "APPARATUS_OK" if ok else "REPAIR_THEN_RERUN", "admissibility": "dry seed; no sealed number read"}
    (RUNS / "round0.json").write_text(json.dumps(r0, indent=2), encoding="utf-8")
    return r0


def stage_build() -> dict:
    Lc = L()
    RUNS.mkdir(parents=True, exist_ok=True)
    if (RUNS / "manifest.json").exists():
        return json.loads((RUNS / "manifest.json").read_text(encoding="utf-8"))
    clean = ensure_clean()
    prs, key = make_plan(clean, Lc["plan"], int(Lc["seed"]), Lc["archetype_version"])
    manifest = build_arena(WORK, clean, prs, key, int(Lc["seed"]))
    (RUNS / "key.json").write_text(json.dumps(key, indent=2), encoding="utf-8")            # PRIVATE until reveal
    (RUNS / "key.sealed").write_text(json.dumps(manifest["seal"], indent=2), encoding="utf-8")
    (RUNS / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")   # PRIVATE (kinds)
    assert verify_seal(key, manifest["seal"])
    print("[m42] built %d branches, %d sealed bugs; seal %s" % (len(manifest["branches"]), len(key), manifest["seal"]["seal"][:12]))
    return manifest


def stage_push() -> dict:
    L()
    repo = CONFIG["arena"]["repo"]
    p = run(["gh", "repo", "view", repo, "--json", "url"], check=False)
    if p.returncode != 0:
        run(["gh", "repo", "create", repo, "--public", "--description", "Pre-registered benchmark of AI pull-request reviewers (sealed key on main)"])
    run(["git", "remote", "remove", "origin"], WORK, check=False)
    run(["git", "remote", "add", "origin", "https://github.com/%s.git" % repo], WORK)
    git(["push", "-q", "-u", "origin", CONFIG["arena"]["default_branch"]], WORK)
    manifest = json.loads((RUNS / "manifest.json").read_text(encoding="utf-8"))
    for b in manifest["branches"]:
        git(["push", "-q", "-u", "origin", b["branch"]], WORK)
    rec = {"ts": now(), "repo": repo, "main_sha": manifest["main_sha"], "pushed_branches": len(manifest["branches"])}
    (RUNS / "push.json").write_text(json.dumps(rec, indent=2), encoding="utf-8")
    return rec


def stage_open(request_copilot: bool = True) -> dict:
    L()
    repo = CONFIG["arena"]["repo"]
    manifest = json.loads((RUNS / "manifest.json").read_text(encoding="utf-8"))
    prs = json.loads((RUNS / "prs.json").read_text(encoding="utf-8")) if (RUNS / "prs.json").exists() else {}
    for b in manifest["branches"]:
        if b["branch"] in prs:
            continue
        p = run(["gh", "pr", "create", "--repo", repo, "--head", b["branch"], "--base", CONFIG["arena"]["default_branch"], "--title", b["title"],
                 "--body", "Small cleanups; no functional change intended."], check=False)
        m = re.search(r"/pull/(\d+)", p.stdout + p.stderr)
        if not m:
            print("[m42] open %s failed: %s" % (b["branch"], (p.stderr or p.stdout)[-200:]))
            continue
        prs[b["branch"]] = int(m.group(1))
        if request_copilot:
            run(["gh", "api", "-X", "POST", "repos/%s/pulls/%d/requested_reviewers" % (repo, prs[b["branch"]]), "-f", "reviewers[]=copilot-pull-request-reviewer[bot]"], check=False)
        (RUNS / "prs.json").write_text(json.dumps(prs, indent=2), encoding="utf-8")
        print("[m42] opened %s -> #%d" % (b["branch"], prs[b["branch"]]))
        time.sleep(2)
    return prs


def stage_collect() -> list:
    Lc = L()
    prs = json.loads((RUNS / "prs.json").read_text(encoding="utf-8"))
    found = collect_vendor(prs, Lc["arms"])
    (RUNS / "findings_vendor.json").write_text(json.dumps(found, indent=1), encoding="utf-8")
    by = {}
    for f in found:
        by.setdefault(f["arm"], {"inline": 0, "summary": 0})[f["kind"]] += 1
    print("[m42] vendor findings:", json.dumps(by))
    return found


def all_findings() -> list:
    out = []
    p = RUNS / "findings_vendor.json"
    if p.exists():
        out += json.loads(p.read_text(encoding="utf-8"))
    for arm in ("codex", "claude"):
        q = RUNS / ("arm_%s.jsonl" % arm)
        if q.exists():
            for line in q.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    r = json.loads(line)
                    for f in r["findings"]:
                        out.append({**f, "kind": "inline"})
    for i, f in enumerate(out):
        f["id"] = "c%04d" % (i + 1)
    return out


def stage_cli(arm: str) -> dict:
    L()
    manifest = json.loads((RUNS / "manifest.json").read_text(encoding="utf-8"))
    return run_cli_arm(arm, WORK, manifest, RUNS / ("arm_%s.jsonl" % arm))


def stage_judge() -> dict:
    Lc = L()
    findings = all_findings()
    (RUNS / "findings_all.json").write_text(json.dumps(findings, indent=1), encoding="utf-8")
    return judge_all(findings, WORK, RUNS / "verdicts.json", batch_size=int(Lc["judge"]["batch"]))


def stage_score(emit_event: bool = True) -> dict:
    Lc = L()
    if (RUNS / "result.json").exists():
        return json.loads((RUNS / "result.json").read_text(encoding="utf-8"))
    findings = json.loads((RUNS / "findings_all.json").read_text(encoding="utf-8"))
    verdicts = json.loads((RUNS / "verdicts.json").read_text(encoding="utf-8"))
    key = json.loads((RUNS / "key.json").read_text(encoding="utf-8"))
    manifest = json.loads((RUNS / "manifest.json").read_text(encoding="utf-8"))
    assert verify_seal(key, manifest["seal"]), "key does not match the seal"
    res = score(findings, verdicts, key, manifest, Lc)
    (RUNS / "result.json").write_text(json.dumps(res, indent=2), encoding="utf-8")
    (RUNS / "RESULT.md").write_text(render(res), encoding="utf-8")
    print("[m42] SEALED %s live=%s" % (res["verdict"], res["live"]))
    if emit_event:
        try:
            from app.measure.events import SEALED, emit
            rel = lambda q: str(Path(q).resolve().relative_to(NH)).replace("\\", "/")  # noqa: E731
            emit(SEALED, "m42", {"verdict": res["verdict"], "artifacts": [rel(RUNS / "RESULT.md"), rel(M42 / "PREREG.md"), rel(M42 / "DEVIATIONS.md")]})
        except Exception as exc:  # noqa: BLE001
            print("[m42] sealed-event emit failed (result unaffected): %s" % exc)
    return res


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(__doc__)
        return 2
    cmd = argv[0]
    if cmd == "round0":
        res = stage_round0(online="--offline" not in argv, reuse="--reuse" in argv)
    elif cmd == "build":
        res = stage_build()
    elif cmd == "push":
        res = stage_push()
    elif cmd == "open":
        res = stage_open(request_copilot="--no-copilot" not in argv)
    elif cmd == "collect":
        res = {"n": len(stage_collect())}
    elif cmd == "cli":
        res = {"branches_done": len(stage_cli(argv[1]))}
    elif cmd == "judge":
        v = stage_judge()
        res = {k: len(val) for k, val in v.items()}
    elif cmd == "score":
        res = stage_score()
    else:
        print(__doc__)
        return 2
    print(json.dumps({k: val for k, val in res.items() if k in ("verdict", "caps_ok", "plan", "dry_branches", "cli_paths", "judge_paths_ok", "match_fixture_tp",
                                                                 "noise_neutral", "live", "void", "union_recall", "best_recall", "predictions_resolved", "n", "branches_done",
                                                                 "pushed_branches", "claude", "codex")}, indent=1)[:2500])
    return 0


if __name__ == "__main__":
    sys.exit(main())
