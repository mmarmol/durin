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


def test_ast_compile_is_caution_not_dangerous():
    # compile() produces a code object but does not execute — caution, not
    # dangerous. exec/eval of its result is what's dangerous (flagged separately).
    fs = scan_python_ast("compile(src, path, 'exec')\n", "scripts/a.py")
    compile_fs = [f for f in fs if f.detail == "dangerous call compile"]
    assert compile_fs and all(f.severity == "caution" for f in compile_fs)


def test_ast_exec_and_eval_stay_dangerous():
    for call in ("exec(x)", "eval(x)", "os.system(x)", "__import__(x)"):
        fs = scan_python_ast(call + "\n", "scripts/a.py")
        assert fs and fs[0].severity == "dangerous", call


def test_ast_exec_of_compile_still_dangerous():
    # compile alone is caution, but exec() of its result is dangerous.
    fs = scan_python_ast("exec(compile(src, p, 'exec'))\n", "scripts/a.py")
    assert any(f.severity == "dangerous" for f in fs)


def test_ast_subprocess_shell_true_is_dangerous_severity():
    fs = scan_python_ast("import subprocess\nsubprocess.run(cmd, shell=True)\n", "scripts/a.py")
    assert any(f.detail == "subprocess shell=True" and f.severity == "dangerous" for f in fs)
