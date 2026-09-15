"""Build real offline distributions and verify a clean sdist can rebuild them."""

from email.parser import Parser
from pathlib import Path
import subprocess
import tarfile
import tomllib
from zipfile import ZipFile

from packaging.requirements import Requirement


ROOT = Path(__file__).resolve().parents[1]


def _build(source, output, *options):
    result = subprocess.run(["uv", "build", "--offline", "--out-dir", str(output), *options],
        cwd=source, text=True, capture_output=True, timeout=60)
    assert result.returncode == 0, result.stderr


def _check_wheel(path, production_files):
    with ZipFile(path) as archive:
        names = set(archive.namelist())
        assert {name for name in names if name.startswith("app/")} == production_files
        assert len([name for name in names if name.startswith("app/templates/")]) == 11
        assert len([name for name in names if name.startswith("app/static/")]) == 6
        assert all(name.startswith("app/") or ".dist-info/" in name for name in names)
        metadata = Parser().parsestr(archive.read(next(
            name for name in names if name.endswith(".dist-info/METADATA"))).decode())
        requirements = {str(Requirement(value)) for value in metadata.get_all("Requires-Dist")
                        if Requirement(value).marker is None}
        declared = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["dependencies"]
        assert requirements == {str(Requirement(value)) for value in declared}


def test_offline_sdist_contains_only_rebuild_sources_and_rebuilds_complete_wheel(tmp_path):
    production_files = {path.relative_to(ROOT).as_posix() for path in (ROOT / "app").rglob("*")
                        if path.is_file() and path.suffix in {".py", ".html", ".css", ".js"}}
    first = tmp_path / "first"
    _build(ROOT, first)
    _check_wheel(next(first.glob("*.whl")), production_files)
    with tarfile.open(next(first.glob("*.tar.gz"))) as archive:
        names = {member.name.split("/", 1)[1] for member in archive.getmembers() if member.isfile()}
        # Hatch carries the VCS exclusion manifest into the sdist so rebuilds
        # retain the source tree's exclusions without requiring a git checkout.
        expected = production_files | {"pyproject.toml", "README.md", "uv.lock", ".gitignore", "PKG-INFO"}
        assert names == expected, sorted(names - expected)[:20]
        archive.extractall(tmp_path / "rebuild", filter="data")
    source = next((tmp_path / "rebuild").iterdir())
    rebuilt = tmp_path / "rebuilt"
    _build(source, rebuilt, "--wheel")
    _check_wheel(next(rebuilt.glob("*.whl")), production_files)
