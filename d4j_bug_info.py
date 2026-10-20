#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
d4j_bug_info.py - coleta, de forma automática, as informações de um bug do Defects4J.

Uso:
    python3 d4j_bug_info.py Math-1
    python3 d4j_bug_info.py Math 1 --output ./dados --verify --with-source
    python3 d4j_bug_info.py Lang-1 --fetch-report-dates --method-history
    python3 d4j_bug_info.py --project-path ./meu-projeto --source-file src/main/java/p/A.java --line 42
    python3 d4j_bug_info.py --project-path ./meu-projeto --stack-trace erro.log

Entrada : projeto + identificador do bug (modo Defects4J), ou uma pasta de projeto
          e evidência de falha (modo projeto real).
Saída   : JSON com metadados do Defects4J ou candidatos a método focal.

Requisitos:
    - Defects4J instalado (comando `defects4j` no PATH ou --d4j-home / $D4J_HOME), com init.sh já executado
    - git, Java 11 e Perl (os mesmos exigidos pelo Defects4J)
    - pip install tree-sitter tree-sitter-java   (AST para achar o método de cada linha)
    - modo projeto real: não exige Defects4J; requer --project-path e --source-file/--line
      ou --stack-trace para fornecer evidência da falha.

Estrutura do JSON (schema_version 1.0):
    bug_id, project, bug_number
    repository      : {program, path, vcs}
    report          : {id, url, created, resolved}               (datas só com --fetch-report-dates)
    versions        : {buggy: {...commit...}, fixed: {...commit...}}
    reproduction    : {commands, layout, trigger_tests, modified_classes, verification}
    changes         : {source_files, test_files, summary}         (arquivos, hunks e linhas)
    focal_method    : método focal escolhido, o caminho seguido para achá-lo e os candidatos
    affected_methods: [{file, class, method, signature, kind, change_type, buggy{...}, fixed{...}}]
    non_method_changes: linhas alteradas fora de qualquer método (campos, imports, comentários...)
    temporal        : {datas dos commits, intervalo, histórico dos arquivos (e dos métodos, opcional)}
    provenance      : {data de geração, versão do schema, home e commit do Defects4J}
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import filecmp
import functools
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

SCHEMA_VERSION = "1.0"


class D4JError(RuntimeError):
    pass


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- #
# Utilitários gerais
# --------------------------------------------------------------------------- #
def run(cmd, cwd=None, check=True) -> subprocess.CompletedProcess:
    p = subprocess.run(
        [str(c) for c in cmd], cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, errors="replace",
    )
    if check and p.returncode != 0:
        detail = (p.stderr.strip() or p.stdout.strip())[-1500:]
        raise D4JError(f"Comando falhou ({p.returncode}): {' '.join(str(c) for c in cmd)}\n{detail}")
    return p


def parse_dt(s: Optional[str]) -> Optional[dt.datetime]:
    """Interpreta datas ISO 8601 e variações comuns (JIRA '+0000', Defects4J '... -0500')."""
    if not s:
        return None
    s = s.strip()
    s = re.sub(r"\s+([+-]\d{2}:?\d{2})$", r"\1", s)
    s = re.sub(r"Z$", "+00:00", s)
    s = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", s)
    if re.match(r"\d{4}-\d{2}-\d{2} \d", s):
        s = s.replace(" ", "T", 1)
    try:
        return dt.datetime.fromisoformat(s)
    except ValueError:
        return None


def delta_days(a: Optional[dt.datetime], b: Optional[dt.datetime]) -> Optional[float]:
    """Dias de a até b (b - a)."""
    if a is None or b is None:
        return None
    if (a.tzinfo is None) != (b.tzinfo is None):
        a = a.replace(tzinfo=a.tzinfo or dt.timezone.utc)
        b = b.replace(tzinfo=b.tzinfo or dt.timezone.utc)
    return round((b - a).total_seconds() / 86400.0, 4)


def iso(d: Optional[dt.datetime]) -> Optional[str]:
    return d.isoformat() if d else None


# --------------------------------------------------------------------------- #
# Defects4J
# --------------------------------------------------------------------------- #
def find_d4j_home(arg: Optional[str]) -> Path:
    if arg:
        return Path(arg).expanduser().resolve()
    if os.environ.get("D4J_HOME"):
        return Path(os.environ["D4J_HOME"]).expanduser().resolve()
    exe = shutil.which("defects4j")
    if exe:
        return Path(exe).resolve().parents[2]  # <home>/framework/bin/defects4j
    raise D4JError("Defects4J não encontrado: use --d4j-home, $D4J_HOME ou coloque 'defects4j' no PATH.")


class Defects4J:
    def __init__(self, home: Path):
        self.home = home
        self.bin = home / "framework" / "bin" / "defects4j"
        if not self.bin.exists():
            raise D4JError(f"Executável não encontrado: {self.bin}")

    def run(self, *args, cwd=None, check=True):
        return run([self.bin, *args], cwd=cwd, check=check)

    def canon_project(self, name: str) -> str:
        base = self.home / "framework" / "projects"
        for d in base.iterdir():
            if d.is_dir() and d.name.lower() == name.lower():
                return d.name
        raise D4JError(f"Projeto desconhecido: {name}")

    def active_bug(self, project: str, n: int) -> dict:
        path = self.home / "framework" / "projects" / project / "active-bugs.csv"
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if str(row.get("bug.id")) == str(n):
                    return row
        raise D4JError(f"Bug {project}-{n} não está em {path} (id inexistente ou depreciado).")

    def project_info(self, project: str) -> dict:
        txt = self.run("info", "-p", project).stdout
        g = lambda pat: (re.search(pat, txt, re.M) or [None, None])[1]
        return {
            "program": (g(r"^\s*Program:\s*(.+)$") or "").strip() or None,
            "repository": (g(r"^\s*Repository:\s*(.+)$") or "").strip() or None,
        }

    def bug_info(self, project: str, n: int) -> dict:
        p = self.run("info", "-p", project, "-b", str(n), check=False)
        txt = p.stdout
        out = {}
        for key, pat in [
            ("buggy_revision", r"Revision ID \(buggy\):\s*(\S+)"),
            ("buggy_date", r"Revision date \(buggy\):\s*(.+)"),
            ("fixed_revision", r"Revision ID \(fixed\):\s*(\S+)"),
            ("fixed_date", r"Revision date \(fixed\):\s*(.+)"),
            ("report_id", r"Bug report id:\s*(\S+)"),
            ("report_url", r"Bug report url:\s*(\S+)"),
        ]:
            m = re.search(pat, txt)
            out[key] = m.group(1).strip() if m else None
        return out

    def trigger_tests_detail(self, project: str, n: int) -> list[dict]:
        """Lê framework/projects/<P>/trigger_tests/<N>: '--- Classe::teste' + stack trace."""
        path = self.home / "framework" / "projects" / project / "trigger_tests" / str(n)
        if not path.exists():
            return []
        tests, cur = [], None
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("--- "):
                cur = {"test": line[4:].strip(), "exception": None, "message": None, "stack_top": [], "_frames": []}
                tests.append(cur)
            elif cur is not None:
                if cur["exception"] is None and line.strip() and not line.lstrip().startswith("at "):
                    head, _, msg = line.partition(": ")
                    cur["exception"], cur["message"] = head.strip(), (msg.strip() or None)
                elif line.lstrip().startswith("at "):
                    if len(cur["stack_top"]) < 5:
                        cur["stack_top"].append(line.strip())
                    fm = FRAME_RE.search(line)
                    if fm:
                        cur["_frames"].append({"class": fm.group(1), "method": fm.group(2),
                                               "file": fm.group(3), "line": int(fm.group(4))})
        return tests

    def checkout(self, project: str, n: int, version: str, dest: Path) -> None:
        if (dest / ".defects4j.config").exists():
            log(f"  reutilizando checkout existente: {dest}")
            return
        if dest.exists() and any(dest.iterdir()):
            raise D4JError(f"{dest} já existe e não é um checkout do Defects4J; apague-o ou use outro --workdir.")
        dest.parent.mkdir(parents=True, exist_ok=True)
        log(f"  defects4j checkout -p {project} -v {n}{version} -w {dest}")
        self.run("checkout", "-p", project, "-v", f"{n}{version}", "-w", dest)

    def export(self, prop: str, workdir: Path) -> list[str]:
        out = self.run("export", "-p", prop, "-w", workdir).stdout
        return [l.strip() for l in out.splitlines() if l.strip()]

    def failing_tests(self, workdir: Path, tests: list[str]) -> list[str]:
        """Compila e executa os testes indicados; devolve os que falharam."""
        self.run("compile", "-w", workdir)
        failing: list[str] = []
        for t in tests:
            p = self.run("test", "-t", t, "-w", workdir, check=False)
            if re.search(r"Failing tests:\s*[1-9]", p.stdout):
                failing.append(t)
        return failing

    def commit(self) -> Optional[str]:
        p = run(["git", "-C", self.home, "rev-parse", "HEAD"], check=False)
        return p.stdout.strip() or None


# --------------------------------------------------------------------------- #
# Git (repositório do projeto, usado só para metadados e histórico)
# --------------------------------------------------------------------------- #
class Repo:
    def __init__(self, path: Optional[str]):
        self.path = Path(path) if path else None

    @property
    def available(self) -> bool:
        return bool(self.path and self.path.exists())

    def git(self, *args, check=True):
        return run(["git", "--git-dir", self.path, *args], check=check)

    def commit(self, rev: str) -> Optional[dict]:
        if not self.available:
            return None
        fmt = "%H%x1f%P%x1f%an%x1f%ae%x1f%aI%x1f%cn%x1f%ce%x1f%cI%x1f%s"
        p = self.git("show", "-s", f"--format={fmt}", rev, check=False)
        parts = p.stdout.strip().split("\x1f")
        if p.returncode != 0 or len(parts) < 9:
            return None
        return {
            "commit": parts[0], "parents": parts[1].split(), "author": parts[2], "author_email": parts[3],
            "author_date": parts[4], "committer": parts[5], "committer_email": parts[6],
            "commit_date": parts[7], "message_subject": parts[8],
        }

    def file_history(self, rev: str, path: str) -> Optional[dict]:
        if not self.available:
            return None
        p = self.git("log", "--follow", "--format=%H%x1f%cI", rev, "--", path, check=False)
        rows = [l.split("\x1f") for l in p.stdout.splitlines() if l.strip()]
        if p.returncode != 0 or not rows:
            return None
        return {
            "first_commit": {"commit": rows[-1][0], "date": rows[-1][1]},
            "last_change_before_buggy": {"commit": rows[0][0], "date": rows[0][1]},
            "commits_before_buggy": len(rows),
        }

    def range_history(self, rev: str, path: str, start: int, end: int) -> Optional[dict]:
        if not self.available:
            return None
        p = self.git("log", "-L", f"{start},{end}:{path}", "--format=%H%x1f%cI", "-s", rev, check=False)
        rows = [l.split("\x1f") for l in p.stdout.splitlines() if "\x1f" in l]
        if p.returncode != 0 or not rows:
            return None
        return {
            "introduced_or_first_seen": {"commit": rows[-1][0], "date": rows[-1][1]},
            "last_change_before_buggy": {"commit": rows[0][0], "date": rows[0][1]},
            "commits_touching_range": len(rows),
        }


# --------------------------------------------------------------------------- #
# Diff entre buggy e fixed
# --------------------------------------------------------------------------- #
HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def diff_hunks(old: Optional[Path], new: Optional[Path]) -> list[dict]:
    """Hunks (git diff -U0) entre dois arquivos; None representa arquivo inexistente."""
    a = str(old) if old else os.devnull
    b = str(new) if new else os.devnull
    p = subprocess.run(
        ["git", "diff", "--no-index", "--no-color", "--no-ext-diff", "--no-renames", "-U0", "--", a, b],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="replace",
    )
    if p.returncode not in (0, 1):
        raise D4JError(f"git diff falhou ({p.returncode}): {p.stderr.strip()}")
    hunks: list[dict] = []
    cur = None
    o_ln = n_ln = 0
    for line in p.stdout.splitlines():
        m = HUNK_RE.match(line)
        if m:
            cur = {
                "old_start": int(m.group(1)), "old_count": int(m.group(2) or 1),
                "new_start": int(m.group(3)), "new_count": int(m.group(4) or 1),
                "removed": [], "added": [],
            }
            hunks.append(cur)
            o_ln, n_ln = cur["old_start"], cur["new_start"]
        elif cur is not None:
            if line.startswith("-"):
                cur["removed"].append({"line": o_ln, "text": line[1:]})
                o_ln += 1
            elif line.startswith("+"):
                cur["added"].append({"line": n_ln, "text": line[1:]})
                n_ln += 1
    return hunks


# --------------------------------------------------------------------------- #
# AST: método que contém cada linha (tree-sitter-java)
# --------------------------------------------------------------------------- #
@dataclass
class MethodInfo:
    cls: str
    name: str
    kind: str
    signature: str
    start: int
    end: int
    ordinal: int = 0

    @property
    def key(self) -> tuple:
        return (self.cls, self.name, self.signature, self.ordinal)


class TreeSitterIndexer:
    TYPE_DECLS = {
        "class_declaration", "interface_declaration", "enum_declaration",
        "record_declaration", "annotation_type_declaration",
    }
    KINDS = {
        "method_declaration": "method",
        "constructor_declaration": "constructor",
        "compact_constructor_declaration": "constructor",
        "static_initializer": "static_initializer",
    }

    def __init__(self):
        try:
            import tree_sitter_java
            from tree_sitter import Language, Parser
        except ImportError as e:
            raise D4JError("Faltam dependências da AST. Instale: pip install tree-sitter tree-sitter-java") from e
        ptr = tree_sitter_java.language()
        try:
            lang = Language(ptr)
        except TypeError:
            lang = Language(ptr, "java")
        try:
            parser = Parser(lang)
        except TypeError:
            parser = Parser()
            try:
                parser.language = lang
            except AttributeError:
                parser.set_language(lang)
        self.parser = parser

    @staticmethod
    def _t(node) -> str:
        return node.text.decode("utf-8", errors="replace") if node is not None else ""

    def _param_types(self, params) -> list[str]:
        types = []
        for ch in params.named_children:
            if ch.type == "formal_parameter":
                txt = self._t(ch.child_by_field_name("type")) + self._t(ch.child_by_field_name("dimensions"))
                types.append(re.sub(r"\s+", " ", txt).strip())
            elif ch.type == "spread_parameter":
                ty = next((c for c in ch.named_children if c.type not in ("modifiers", "variable_declarator")), None)
                types.append(re.sub(r"\s+", " ", self._t(ty)).strip() + "...")
        return types

    def index(self, path: Path) -> list[MethodInfo]:
        tree = self.parser.parse(path.read_bytes())
        package = ""
        methods: list[MethodInfo] = []
        seen: dict[tuple, int] = {}
        stack = [(tree.root_node, ())]
        while stack:
            node, scope = stack.pop()
            t = node.type
            if t == "package_declaration":
                package = re.sub(r"^\s*package\s+|\s*;\s*$", "", self._t(node)).strip()
            new_scope = scope
            if t in self.TYPE_DECLS:
                new_scope = scope + (self._t(node.child_by_field_name("name")),)
            elif t == "class_body" and node.parent is not None and node.parent.type in (
                    "object_creation_expression", "enum_constant"):
                new_scope = scope + ("<anon>",)
            kind = self.KINDS.get(t)
            if kind is None and t == "block" and node.parent is not None and node.parent.type == "class_body":
                kind = "instance_initializer"
            if kind:
                if kind == "static_initializer":
                    name, sig = "<clinit>", "<clinit>()"
                elif kind == "instance_initializer":
                    name, sig = "<init-block>", "<init-block>()"
                else:
                    name = self._t(node.child_by_field_name("name"))
                    params = node.child_by_field_name("parameters")
                    sig = f"{name}({', '.join(self._param_types(params) if params is not None else [])})"
                inner = "$".join(scope)
                cls = f"{package}.{inner}" if package and inner else (inner or package)
                k = (cls, name, sig)
                seen[k] = seen.get(k, -1) + 1
                methods.append(MethodInfo(cls, name, kind, sig, node.start_point[0] + 1,
                                          node.end_point[0] + 1, seen[k]))
            for child in reversed(node.children):
                stack.append((child, new_scope))
        return methods


def make_indexer() -> TreeSitterIndexer:
    return TreeSitterIndexer()


def map_lines(methods: list[MethodInfo], lines: list[int]):
    """Associa cada linha ao método mais interno que a contém.
    Retorna ({chave_do_método: [linhas]}, [linhas fora de qualquer método])."""
    inside: dict[tuple, list[int]] = {}
    outside: list[int] = []
    for ln in sorted(set(lines)):
        best = None
        for m in methods:
            if m.start <= ln <= m.end and (best is None or (m.end - m.start) < (best.end - best.start)):
                best = m
        if best is None:
            outside.append(ln)
        else:
            inside.setdefault(best.key, []).append(ln)
    return inside, outside


def affected_methods(path: str, b: Optional[Path], f: Optional[Path], hunks: list[dict],
                     get_indexer: Callable, with_source: bool):
    indexer = get_indexer()
    old_methods = indexer.index(b) if b else []
    new_methods = indexer.index(f) if f else []
    removed = [x["line"] for h in hunks for x in h["removed"]]
    added = [x["line"] for h in hunks for x in h["added"]]
    old_touch, old_out = map_lines(old_methods, removed)
    new_touch, new_out = map_lines(new_methods, added)
    otab = {m.key: m for m in old_methods}
    ntab = {m.key: m for m in new_methods}

    texts: dict[Path, list[str]] = {}

    def snippet(p: Optional[Path], m: MethodInfo) -> Optional[str]:
        if p is None:
            return None
        if p not in texts:
            texts[p] = p.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(texts[p][m.start - 1:m.end])

    entries = []
    for k in dict.fromkeys(list(old_touch) + list(new_touch)):
        bm, fm = otab.get(k), ntab.get(k)
        entries.append(_entry(path, bm, fm, old_touch.get(k, []), new_touch.get(k, []),
                              snippet(b, bm) if (with_source and bm) else None,
                              snippet(f, fm) if (with_source and fm) else None))
    entries = _merge_signature_changes(entries)
    non_method = []
    if old_out:
        non_method.append({"file": path, "side": "buggy", "lines": old_out})
    if new_out:
        non_method.append({"file": path, "side": "fixed", "lines": new_out})
    return entries, non_method


def _entry(path, bm, fm, old_lines, new_lines, src_b, src_f) -> dict:
    ref = fm or bm
    change = "modified" if (bm and fm) else ("removed" if bm else "added")

    def side(m, lines, src):
        if m is None:
            return None
        d = {"start_line": m.start, "end_line": m.end, "changed_lines": lines}
        if src is not None:
            d["source"] = src
        return d

    return {
        "file": path, "class": ref.cls, "method": ref.name, "signature": ref.signature,
        "kind": ref.kind, "change_type": change,
        "buggy": side(bm, old_lines, src_b), "fixed": side(fm, new_lines, src_f),
    }


def _merge_signature_changes(entries: list[dict]) -> list[dict]:
    """removed + added com mesmo (classe, nome) e assinatura diferente => signature_changed."""
    groups: dict[tuple, dict[str, list[int]]] = {}
    for i, e in enumerate(entries):
        if e["change_type"] in ("removed", "added"):
            groups.setdefault((e["class"], e["method"]), {"removed": [], "added": []})[e["change_type"]].append(i)
    drop = set()
    for g in groups.values():
        if len(g["removed"]) == 1 and len(g["added"]) == 1:
            r, a = entries[g["removed"][0]], entries[g["added"][0]]
            a["change_type"] = "signature_changed"
            a["buggy"] = r["buggy"]
            a["previous_signature"] = r["signature"]
            drop.add(g["removed"][0])
    return [e for i, e in enumerate(entries) if i not in drop]


def collect_changes(rel_dir: str, bdir: Path, fdir: Path, get_indexer, with_source: bool):
    broot, froot = bdir / rel_dir, fdir / rel_dir
    bfiles = {p.relative_to(broot).as_posix(): p for p in broot.rglob("*.java")} if broot.is_dir() else {}
    ffiles = {p.relative_to(froot).as_posix(): p for p in froot.rglob("*.java")} if froot.is_dir() else {}
    files, methods, non_method = [], [], []
    for rel in sorted(set(bfiles) | set(ffiles)):
        b, f = bfiles.get(rel), ffiles.get(rel)
        if b and f:
            if filecmp.cmp(b, f, shallow=False):
                continue
            status = "modified"
        else:
            status = "added" if f else "removed"
        hunks = diff_hunks(b, f)
        path = (Path(rel_dir) / rel).as_posix()
        files.append({
            "path": path, "status": status,
            "lines_added": sum(len(h["added"]) for h in hunks),
            "lines_removed": sum(len(h["removed"]) for h in hunks),
            "hunks": hunks,
        })
        m, n = affected_methods(path, b, f, hunks, get_indexer, with_source)
        methods += m
        non_method += n
    return files, methods, non_method


# --------------------------------------------------------------------------- #
# Método focal
# --------------------------------------------------------------------------- #
FRAME_RE = re.compile(r"at\s+([\w.$]+)\.([\w$<>]+)\(([\w$.]+):(\d+)\)")


def _focal_result(m: dict, reason: str, path: str, cands: list[dict]) -> dict:
    return {
        "file": m["file"], "class": m["class"], "method": m["method"], "signature": m["signature"],
        "kind": m["kind"], "change_type": m["change_type"],
        "reason": reason,   # código curto do critério usado
        "path": path,       # explicação, em português, do caminho seguido para chegar a este método
        "n_affected": len(cands),
        "candidates": [f"{c['class']}.{c['signature']}" for c in cands],
    }


def _short(m: dict) -> str:
    return f"{m['class'].rsplit('.', 1)[-1]}.{m['signature']}"


def choose_focal(methods: list[dict], tests: list[dict]) -> Optional[dict]:
    """Escolhe o método focal entre os métodos afetados (só código de fonte) e explica o caminho seguido.

    Critérios, em ordem:
      1. unico_metodo      : só há um método afetado.
      2. stack_trace       : método (versão buggy) que contém o frame mais próximo da exceção na pilha de
                             erro dos testes de gatilho.
      3. teste_de_gatilho  : método da classe que o teste de gatilho exercita (BigFractionTest -> BigFraction).
      4. mais_linhas       : método com mais linhas alteradas.
      5. primeiro_na_ordem : primeiro método do diff (desempate final).
    """
    cand = [m for m in methods if not m.get("in_tests")]
    if not cand:
        return None
    if len(cand) == 1:
        return _focal_result(cand[0], "unico_metodo", "é o único método alterado pela correção", cand)

    # 2) pilha de erro dos testes de gatilho
    hits: dict[int, dict] = {}
    for ti, t in enumerate(tests):
        per_test: dict[int, tuple] = {}
        for fi, fr in enumerate(t.get("_frames", [])):
            for ci, m in enumerate(cand):
                b = m.get("buggy")
                if b and Path(m["file"]).name == fr["file"] and b["start_line"] <= fr["line"] <= b["end_line"]:
                    per_test.setdefault(ci, (fi, fr))
        for ci, (fi, fr) in per_test.items():
            h = hits.setdefault(ci, {"best": fi, "frame": fr, "test": t["test"], "tests": 0, "first_test": ti})
            if fi < h["best"]:
                h.update(best=fi, frame=fr, test=t["test"])
            h["tests"] += 1
    if hits:
        ranked = sorted(hits.items(), key=lambda kv: (kv[1]["best"], -kv[1]["tests"], kv[1]["first_test"], kv[0]))
        top_ci, top = ranked[0]
        where = f"{top['frame']['file']}:{top['frame']['line']}"
        if len(ranked) == 1:
            path = (f"é o único método alterado que aparece na pilha de erro dos testes de gatilho "
                    f"(teste {top['test']}, frame em {where})")
        else:
            others = ", ".join(_short(cand[ci]) for ci, _ in ranked[1:])
            second = ranked[1][1]
            if (second["best"], -second["tests"]) == (top["best"], -top["tests"]):
                path = (f"vários métodos alterados aparecem na pilha de erro à mesma distância da exceção "
                        f"({_short(cand[top_ci])}, {others}); foi escolhido o do primeiro teste de gatilho, "
                        f"{top['test']} (frame em {where})")
            else:
                path = (f"na pilha de erro do teste {top['test']}, o frame mais próximo da exceção que está "
                        f"dentro de um método alterado é {where}; os outros métodos alterados que aparecem "
                        f"na pilha ficam mais longe da exceção: {others}")
        return _focal_result(cand[top_ci], "stack_trace", path, cand)

    # 3) classe exercitada pelo teste de gatilho (BigFractionTest -> BigFraction)
    under_test: dict[str, str] = {}
    for t in tests:
        cls = t["test"].split("::")[0]
        simple = cls.rsplit(".", 1)[-1]
        stripped = re.sub(r"^Test", "", re.sub(r"(Tests?|TestCase|IT)$", "", simple))
        under_test[cls[: len(cls) - len(simple)] + stripped] = cls
    matched = [m for m in cand if m["class"].split("$")[0] in under_test]
    prefix = "a pilha de erro e o nome do teste de gatilho não apontam para um método alterado; "
    if matched:
        owner = matched[0]["class"].split("$")[0]
        tcls = under_test[owner]
        if len(matched) == 1:
            path = (f"a pilha de erro não aponta para um método alterado, mas o teste de gatilho {tcls} "
                    f"exercita a classe {owner}, onde está este método (os outros métodos alterados ficam "
                    f"em outras classes)")
            return _focal_result(matched[0], "teste_de_gatilho", path, cand)
        cand = matched
        prefix = (f"a pilha de erro não aponta para um método alterado; o teste de gatilho {tcls} exercita "
                  f"a classe {owner} e ela tem {len(matched)} métodos alterados; ")

    # 4) e 5) tamanho da alteração / ordem no diff
    def size(m):
        return sum(len(m[side]["changed_lines"]) for side in ("buggy", "fixed") if m.get(side))

    best = max(size(m) for m in cand)
    top = [m for m in cand if size(m) == best]
    if len(top) == 1:
        rest = max(size(m) for m in cand if m is not top[0])
        path = prefix + (f"foi escolhido o de maior alteração ({best} linhas alteradas, contra no máximo "
                         f"{rest} nos demais)")
        return _focal_result(top[0], "mais_linhas", path, cand)
    path = prefix + f"há empate no número de linhas alteradas ({best}); foi escolhido o primeiro método na ordem do diff"
    return _focal_result(top[0], "primeiro_na_ordem", path, cand)


# --------------------------------------------------------------------------- #
# Modo independente: análise AST em projeto real (sem buggy/fixed)
# --------------------------------------------------------------------------- #
def _method_to_dict(method: MethodInfo, file_path: Path, project_root: Path, line: int) -> dict:
    try:
        rel = file_path.resolve().relative_to(project_root.resolve()).as_posix()
    except ValueError:
        rel = str(file_path.resolve())
    return {
        "file": rel,
        "class": method.cls,
        "method": method.name,
        "signature": method.signature,
        "kind": method.kind,
        "start_line": method.start,
        "end_line": method.end,
        "evidence": {"type": "source_line", "line": line},
    }


def identify_method_at_line(source_file: Path, line_number: int, project_root: Path) -> Optional[dict]:
    """Localiza o método mais interno que contém uma linha, sem comparar versões."""
    if not source_file.is_file():
        raise D4JError(f"Arquivo Java não encontrado: {source_file}")
    if line_number < 1:
        raise D4JError("O número da linha deve ser maior ou igual a 1.")
    methods = make_indexer().index(source_file)
    candidates = [m for m in methods if m.start <= line_number <= m.end]
    if not candidates:
        return None
    method = min(candidates, key=lambda m: m.end - m.start)
    return _method_to_dict(method, source_file, project_root, line_number)


def analyze_real_project(project_path: str, source_file: Optional[str] = None,
                         line_number: Optional[int] = None,
                         stack_trace_path: Optional[str] = None) -> dict:
    """Analisa stack trace ou arquivo/linha em código Java atual; não requer Defects4J."""
    root = Path(project_path).expanduser().resolve()
    if not root.is_dir():
        raise D4JError(f"Pasta do projeto não encontrada: {root}")
    if bool(source_file) != (line_number is not None):
        raise D4JError("Use --source-file e --line juntos.")
    if not stack_trace_path and not source_file:
        raise D4JError("No modo projeto real, informe --source-file/--line ou --stack-trace.")
    if stack_trace_path and source_file:
        raise D4JError("Escolha uma evidência: --stack-trace ou --source-file/--line, não ambas.")

    java_files = list(root.rglob("*.java"))
    # Ignora diretórios normalmente gerados, para reduzir falsos candidatos e custo.
    ignored = {".git", ".gradle", ".idea", "build", "target", "out", "node_modules"}
    java_files = [p for p in java_files if not any(part in ignored for part in p.relative_to(root).parts)]
    if not java_files:
        raise D4JError(f"Nenhum arquivo .java encontrado em {root}")

    focal = None
    candidates = []
    evidence = None
    if source_file:
        given = Path(source_file).expanduser()
        file_path = (root / given).resolve() if not given.is_absolute() else given.resolve()
        try:
            file_path.relative_to(root)
        except ValueError:
            raise D4JError("--source-file deve apontar para um arquivo dentro de --project-path.")
        focal = identify_method_at_line(file_path, int(line_number), root)
        evidence = {"type": "source_line", "file": str(file_path.relative_to(root)), "line": int(line_number)}
        if focal:
            candidates = [focal]
    else:
        trace_path = Path(stack_trace_path).expanduser().resolve()
        if not trace_path.is_file():
            raise D4JError(f"Arquivo de stack trace não encontrado: {trace_path}")
        trace_text = trace_path.read_text(encoding="utf-8", errors="replace")
        frames = []
        for frame_index, line in enumerate(trace_text.splitlines()):
            match = FRAME_RE.search(line)
            if match:
                frames.append({"class": match.group(1), "method": match.group(2),
                               "file": match.group(3), "line": int(match.group(4)),
                               "frame_index": frame_index, "raw": line.strip()})
        evidence = {"type": "stack_trace", "file": str(trace_path), "frames_found": len(frames)}
        indexed = {}
        indexer = make_indexer()
        for java_file in java_files:
            try:
                indexed[java_file] = indexer.index(java_file)
            except (OSError, UnicodeError):
                continue
        for frame in frames:
            matching_files = [p for p in indexed if p.name == frame["file"]]
            if not matching_files:
                continue
            # Prefere a classe exata do frame quando houver arquivos homônimos.
            exact = [p for p in matching_files if any(
                m.cls == frame["class"] or m.cls.endswith("." + frame["class"])
                for m in indexed[p]
            )]
            if exact:
                matching_files = exact
            frame_candidates = []
            for path in matching_files:
                methods = [m for m in indexed[path] if m.start <= frame["line"] <= m.end]
                if not methods:
                    continue
                method = min(methods, key=lambda m: m.end - m.start)
                item = _method_to_dict(method, path, root, frame["line"])
                item["evidence"].update({"type": "stack_trace", "frame": frame["raw"],
                                         "frame_index": frame["frame_index"]})
                frame_candidates.append(item)
            if frame_candidates:
                candidates.extend(frame_candidates)
                # Heurística explícita: primeiro frame que pertence ao projeto e contém um método.
                focal = frame_candidates[0]
                break

    status = "candidate_found" if focal else "not_found"
    if focal and evidence and evidence["type"] == "stack_trace":
        reason = "primeiro frame do stack trace mapeado para um método do projeto"
    elif focal:
        reason = "método que contém a linha informada"
    else:
        reason = "nenhuma evidência fornecida pôde ser associada a um método Java do projeto"
    return {
        "schema_version": SCHEMA_VERSION,
        "analysis_mode": "real_project_ast",
        "project_path": str(root),
        "evidence": evidence,
        "status": status,
        "focal_method_candidate": focal,
        "candidates": candidates,
        "selection_reason": reason,
        "limitations": [
            "O método retornado é um candidato baseado na evidência, não uma confirmação causal do defeito.",
            "Sem stack trace, linha de falha, teste ou outra evidência, a AST sozinha não determina o método focal."
        ],
        "provenance": {"generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                       "tree_sitter": True},
    }


# --------------------------------------------------------------------------- #
# Datas do relatório de bug (opcional, usa a rede)
# --------------------------------------------------------------------------- #
def fetch_report_dates(url: Optional[str]) -> dict:
    if not url:
        return {}
    api = None
    m = re.match(r"https?://([^/]+)(/jira)?/browse/([A-Z][A-Z0-9_]*-\d+)", url)
    if m:
        api = f"https://{m.group(1)}{m.group(2) or ''}/rest/api/2/issue/{m.group(3)}?fields=created,resolutiondate"
        keys = ("created", "resolutiondate")
        pick = lambda d: (d["fields"].get(keys[0]), d["fields"].get(keys[1]))
    else:
        m = re.match(r"https?://github\.com/([^/]+)/([^/]+)/(?:issues|pull)/(\d+)", url)
        if m:
            api = f"https://api.github.com/repos/{m.group(1)}/{m.group(2)}/issues/{m.group(3)}"
            pick = lambda d: (d.get("created_at"), d.get("closed_at"))
    if not api:
        return {"note": "URL de relatório não suportada para consulta automática"}
    try:
        req = urllib.request.Request(api, headers={"User-Agent": "d4j-bug-info", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            created, resolved = pick(json.load(r))
        return {"created": iso(parse_dt(created)), "resolved": iso(parse_dt(resolved))}
    except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
        return {"note": f"falha ao consultar o relatório: {e}"}


# --------------------------------------------------------------------------- #
# Montagem do resultado
# --------------------------------------------------------------------------- #
def parse_target(target: str, bug: Optional[str]) -> tuple[str, int]:
    m = re.fullmatch(r"([A-Za-z]+)[-:_/](\d+)", target)
    if m and bug is None:
        return m.group(1), int(m.group(2))
    if bug is not None and target.isalpha() and str(bug).isdigit():
        return target, int(bug)
    raise D4JError("Informe o bug como 'Projeto-N' (ex.: Math-1) ou 'Projeto N'.")


def collect(args) -> dict:
    d4j = Defects4J(find_d4j_home(args.d4j_home))
    project, n = parse_target(args.target, args.bug)
    project = d4j.canon_project(project)
    bug_id = f"{project}-{n}"
    log(f"[{bug_id}] coletando dados...")
    get_indexer = functools.lru_cache(maxsize=1)(make_indexer)
    get_indexer()  # falha cedo, antes dos checkouts, se faltar tree-sitter

    row = d4j.active_bug(project, n)
    pinfo = d4j.project_info(project)
    binfo = d4j.bug_info(project, n)
    buggy_rev = row.get("revision.id.buggy") or binfo["buggy_revision"]
    fixed_rev = row.get("revision.id.fixed") or binfo["fixed_revision"]
    report_id = row.get("report.id") or binfo["report_id"]
    report_url = row.get("report.url") or binfo["report_url"]

    # versões buggy/fixed (commits no repositório do projeto)
    repo = Repo(pinfo["repository"])
    if not repo.available:
        log("  aviso: repositório do projeto não encontrado (rode init.sh); datas virão do 'defects4j info'.")
    versions = {}
    for label, rev, fallback_date in (("buggy", buggy_rev, binfo["buggy_date"]),
                                      ("fixed", fixed_rev, binfo["fixed_date"])):
        ci = repo.commit(rev) or {"commit": rev, "commit_date": iso(parse_dt(fallback_date))}
        versions[label] = ci

    # checkouts (buggy e fixed) e layout do projeto
    workdir = Path(args.workdir).expanduser().resolve() / bug_id
    bdir, fdir = workdir / "buggy", workdir / "fixed"
    d4j.checkout(project, n, "b", bdir)
    d4j.checkout(project, n, "f", fdir)

    def export1(prop):
        v = d4j.export(prop, bdir)
        return v[0] if v else None

    layout = {
        "src_classes": export1("dir.src.classes"), "src_tests": export1("dir.src.tests"),
        "bin_classes": export1("dir.bin.classes"), "bin_tests": export1("dir.bin.tests"),
    }
    trigger = d4j.export("tests.trigger", bdir)
    modified_classes = d4j.export("classes.modified", bdir)
    detail = {t["test"]: t for t in d4j.trigger_tests_detail(project, n)}
    trigger_full = [detail.get(t, {"test": t, "exception": None, "message": None, "stack_top": [], "_frames": []})
                    for t in trigger]
    trigger_tests = [{k: v for k, v in t.items() if not k.startswith("_")} for t in trigger_full]

    verification = None
    if args.verify:
        log("  verificando reprodução (compila e roda os testes que disparam o bug)...")
        fb = d4j.failing_tests(bdir, trigger)
        ff = d4j.failing_tests(fdir, trigger)
        verification = {
            "failing_on_buggy": fb, "failing_on_fixed": ff,
            "reproduced": bool(trigger) and all(t in fb and t not in ff for t in trigger),
        }

    # diff buggy x fixed e métodos afetados
    src_files, src_methods, src_non = ([], [], [])
    test_files, test_methods, test_non = ([], [], [])
    if layout["src_classes"]:
        src_files, src_methods, src_non = collect_changes(layout["src_classes"], bdir, fdir, get_indexer, args.with_source)
    if layout["src_tests"]:
        test_files, test_methods, test_non = collect_changes(layout["src_tests"], bdir, fdir, get_indexer, args.with_source)

    focal = choose_focal(src_methods, trigger_full)

    # informações temporais
    bd, fd = parse_dt(versions["buggy"].get("commit_date")), parse_dt(versions["fixed"].get("commit_date"))
    temporal = {
        "buggy_commit_date": versions["buggy"].get("commit_date"),
        "fixed_commit_date": versions["fixed"].get("commit_date"),
        "buggy_author_date": versions["buggy"].get("author_date"),
        "fixed_author_date": versions["fixed"].get("author_date"),
        "buggy_to_fixed_days": delta_days(bd, fd),
        "buggy_is_parent_of_fixed": buggy_rev in (versions["fixed"].get("parents") or []) if versions["fixed"].get("parents") else None,
        "report": None,
        "files_history": [],
    }
    report = {"id": report_id, "url": report_url, "created": None, "resolved": None}
    if args.fetch_report_dates:
        dates = fetch_report_dates(report_url)
        report.update({k: v for k, v in dates.items() if k in ("created", "resolved")})
        cr, rs = parse_dt(report["created"]), parse_dt(report["resolved"])
        temporal["report"] = {"created": report["created"], "resolved": report["resolved"],
                              "report_to_resolution_days": delta_days(cr, rs),
                              "report_to_fixed_commit_days": delta_days(cr, fd),
                              **({"note": dates["note"]} if "note" in dates else {})}
    for fe in src_files:
        h = repo.file_history(buggy_rev, fe["path"]) if fe["status"] != "added" else None
        if h:
            h["path"] = fe["path"]
            last = parse_dt(h["last_change_before_buggy"]["date"])
            h["days_since_last_change_before_buggy"] = delta_days(last, bd)
            h["file_age_days_at_buggy"] = delta_days(parse_dt(h["first_commit"]["date"]), bd)
            temporal["files_history"].append(h)
    if args.method_history:
        temporal["methods_history"] = []
        for m in src_methods:
            if m["buggy"]:
                h = repo.range_history(buggy_rev, m["file"], m["buggy"]["start_line"], m["buggy"]["end_line"])
                if h:
                    h.update({"file": m["file"], "signature": m.get("previous_signature") or m["signature"],
                              "approximate": True})
                    temporal["methods_history"].append(h)

    wd = lambda v: f"defects4j checkout -p {project} -v {n}{v} -w <dir>"
    return {
        "schema_version": SCHEMA_VERSION,
        "bug_id": bug_id, "project": project, "bug_number": n,
        "repository": {"program": pinfo["program"], "path": pinfo["repository"], "vcs": "git"},
        "report": report,
        "versions": versions,
        "reproduction": {
            "commands": {
                "checkout_buggy": wd("b"), "checkout_fixed": wd("f"),
                "compile": "defects4j compile -w <dir>",
                "run_trigger_test": "defects4j test -t <Classe::metodo> -w <dir>",
            },
            "layout": layout,
            "trigger_tests": trigger_tests,
            "modified_classes": modified_classes,
            "verification": verification,
        },
        "changes": {
            "source_files": src_files, "test_files": test_files,
            "summary": {
                "source_files_changed": len(src_files),
                "source_lines_added": sum(f["lines_added"] for f in src_files),
                "source_lines_removed": sum(f["lines_removed"] for f in src_files),
                "test_files_changed": len(test_files),
                "methods_affected": len(src_methods),
            },
        },
        "focal_method": focal,
        "affected_methods": src_methods + ([{**m, "in_tests": True} for m in test_methods] if test_methods else []),
        "non_method_changes": src_non + test_non,
        "temporal": temporal,
        "provenance": {
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "schema_version": SCHEMA_VERSION,
            "defects4j_home": str(d4j.home), "defects4j_commit": d4j.commit(),
            "workdir": str(workdir),
        },
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Coleta informações de um bug do Defects4J.")
    ap.add_argument("target", nargs="?", help="'Math-1' ou apenas o projeto (ex.: Math), seguido do número em 'bug'")
    ap.add_argument("bug", nargs="?", help="número do bug, se o primeiro argumento for só o projeto")
    ap.add_argument("--d4j-home", help="pasta do Defects4J (padrão: $D4J_HOME ou o 'defects4j' do PATH)")
    ap.add_argument("--workdir", default="~/d4j_work", help="onde ficam os checkouts buggy/fixed (padrão: ~/d4j_work)")
    ap.add_argument("--output", default=".", help="pasta de saída do JSON (padrão: .)")
    ap.add_argument("--verify", action="store_true", help="compila e roda os testes de gatilho em buggy e fixed")
    ap.add_argument("--with-source", action="store_true", help="inclui o código-fonte de cada método afetado")
    ap.add_argument("--fetch-report-dates", action="store_true", help="consulta datas do relatório (JIRA/GitHub)")
    ap.add_argument("--method-history", action="store_true", help="histórico git por método (aproximado)")
    ap.add_argument("--stdout", action="store_true", help="imprime o JSON no stdout em vez de gravar arquivo")
    ap.add_argument("--project-path", help="modo projeto real: raiz do projeto, sem exigir Defects4J")
    ap.add_argument("--source-file", help="arquivo Java relativo à raiz (usar com --line)")
    ap.add_argument("--line", type=int, help="número da linha que aparece na evidência de falha")
    ap.add_argument("--stack-trace", help="arquivo de texto com stack trace Java")
    args = ap.parse_args(argv)
    try:
        if args.project_path:
            if args.target or args.bug:
                raise D4JError("No modo --project-path, não informe um alvo Defects4J (ex.: Lang-1).")
            data = analyze_real_project(args.project_path, args.source_file, args.line, args.stack_trace)
        else:
            if not args.target:
                raise D4JError("Informe um alvo Defects4J (ex.: Lang-1) ou use --project-path para um projeto real.")
            if args.source_file or args.line is not None or args.stack_trace:
                raise D4JError("--source-file, --line e --stack-trace só podem ser usados com --project-path.")
            data = collect(args)
    except D4JError as e:
        log(f"erro: {e}")
        return 1
    text = json.dumps(data, indent=2, ensure_ascii=False)
    if args.stdout:
        print(text)
    else:
        out = Path(args.output).expanduser()
        out.mkdir(parents=True, exist_ok=True)
        output_name = f"{data['bug_id']}.json" if data.get("bug_id") else "analise_projeto.json"
        dest = out / output_name
        dest.write_text(text, encoding="utf-8")
        log(f"gravado: {dest}")
    if data.get("analysis_mode") == "real_project_ast":
        fm = data.get("focal_method_candidate")
        if fm:
            log(f"candidato a método focal: {fm['class']}.{fm['signature']}")
            log(f"  arquivo/linhas: {fm['file']}:{fm['start_line']}-{fm['end_line']}")
        else:
            log("nenhum método pôde ser associado à evidência fornecida")
        log(f"critério: {data['selection_reason']}")
    else:
        s = data["changes"]["summary"]
        log(f"resumo: {s['source_files_changed']} arquivo(s), +{s['source_lines_added']}/-{s['source_lines_removed']} linhas, "
            f"{s['methods_affected']} método(s) afetado(s)")
        fm = data.get("focal_method")
        if fm:
            log(f"método focal: {fm['class']}.{fm['signature']}")
            log(f"  caminho: {fm['path']}")
        for m in data["affected_methods"]:
            where = "teste" if m.get("in_tests") else "fonte"
            log(f"  - {m['class']}.{m['signature']} [{m['change_type']}] ({where}, {m['file']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
