import ast
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("ffi_name", ["ffi.dll", "ffi-8.dll", "libffi-8.dll"])
def test_spec_collects_conda_runtime_with_original_dll_names(tmp_path, ffi_name):
    directory = tmp_path / "Library" / "bin"
    directory.mkdir(parents=True)
    for name in (ffi_name, "sqlite3.dll"):
        (directory / name).touch()
    spec = Path(__file__).resolve().parents[2] / "packaging" / "joyread.spec"
    tree = ast.parse(spec.read_text(encoding="utf-8"))
    block = next(node for node in tree.body if isinstance(node, ast.If)
                 and ast.unparse(node.test) == "platform_key() == 'windows'")
    namespace = {"Path": Path, "sys": SimpleNamespace(prefix=str(tmp_path)), "binaries": []}
    code = compile(ast.Module(body=block.body, type_ignores=[]), str(spec), "exec")
    exec(code, namespace)
    assert {Path(source).name for source, _ in namespace["binaries"]} == {ffi_name, "sqlite3.dll"}
    (directory / "sqlite3.dll").unlink()
    with pytest.raises(SystemExit, match="Missing Windows Conda runtime"):
        exec(code, namespace)
