"""Tests for the stdlib-AST behavioral pass (durin/security/skill_ast.py)."""
from durin.security.skill_ast import scan_python_ast


def test_ast_flags_os_system():
    fs = scan_python_ast("import os\nos.system('rm -rf /')\n", "scripts/a.py")
    assert any(f.category == "dangerous_code" and "os.system" in f.detail for f in fs)


def test_ast_flags_subprocess_shell_true():
    fs = scan_python_ast("import subprocess\nsubprocess.run(x, shell=True)\n", "scripts/a.py")
    assert any("shell=True" in f.detail for f in fs)


def test_ast_flags_pickle_loads():
    fs = scan_python_ast("import pickle\npickle.loads(blob)\n", "scripts/a.py")
    assert any("pickle.loads" in f.detail for f in fs)


def test_ast_benign_subprocess_no_shell_clean():
    # plain subprocess.run (no shell=True) is common and benign — must NOT flag.
    fs = scan_python_ast("import subprocess\nsubprocess.run(['ls', '-l'])\n", "scripts/a.py")
    assert fs == []


def test_ast_benign_clean():
    fs = scan_python_ast("import json\nprint(json.dumps({'a': 1}))\n", "scripts/a.py")
    assert fs == []


def test_ast_syntax_error_is_caution_not_raise():
    fs = scan_python_ast("def (:\n", "scripts/a.py")
    assert len(fs) == 1 and fs[0].severity == "caution"
