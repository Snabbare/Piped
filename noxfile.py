# -*- coding: utf-8 -*-
# BSD 3-Clause License
#
# Copyright (c) 2020-2022, Faster Speeding
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# * Redistributions of source code must retain the above copyright notice, this
#   list of conditions and the following disclaimer.
#
# * Redistributions in binary form must reproduce the above copyright notice,
#   this list of conditions and the following disclaimer in the documentation
#   and/or other materials provided with the distribution.
#
# * Neither the name of the copyright holder nor the names of its
#   contributors may be used to endorse or promote products derived from
#   this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
# FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
# SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
# OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
from __future__ import annotations

import itertools
import pathlib
import re
import shutil
import typing

import nox
import pydantic
import tomli

_CallbackT = typing.TypeVar("_CallbackT", bound=typing.Callable[..., typing.Any])


class Config(pydantic.BaseModel):
    """Configuration class for the project config."""

    default_sessions: typing.List[str]
    hide: typing.List[str] = pydantic.Field(default_factory=list)

    if typing.TYPE_CHECKING:
        path_ignore: typing.Optional[typing.Pattern[str]] = None

    else:
        path_ignore: typing.Optional[typing.Pattern] = None

    project_name: typing.Optional[str] = None
    top_level_targets: typing.List[str]
    vendor_dir: typing.Optional[str] = None

    def assert_project_name(self) -> str:
        if not self.project_name:
            raise RuntimeError("This CI cannot run without project_name")

        return self.project_name


with pathlib.Path("pyproject.toml").open("rb") as file:
    config = Config.parse_obj(tomli.load(file)["tool"]["piped"])


nox.options.sessions = config.default_sessions
_DEPS_DIR = pathlib.Path("./dev-requirements")
_SELF_INSTALL_REGEX = re.compile(r"^\.\[.+\]$")


def _dev_path(value: str) -> pathlib.Path:
    path = _DEPS_DIR / f"{value}.txt"
    if path.exists():
        return path

    return pathlib.Path(__file__).parent / "dev-requirements" / f"{value}.txt"


_CONSTRAINT_DIR = _dev_path("constraints")


def _deps(*dev_deps: str, constrain: bool = False) -> typing.Iterator[str]:
    if constrain and _CONSTRAINT_DIR.exists():
        return itertools.chain(["-c", str(_CONSTRAINT_DIR)], _deps(*dev_deps))

    return itertools.chain.from_iterable(("-r", str(_dev_path(value))) for value in dev_deps)


def _tracked_files(session: nox.Session, *, ignore_vendor: bool = False) -> typing.Iterable[str]:
    if ignore_vendor and config.path_ignore:
        return (path for path in _tracked_files(session) if not config.path_ignore.match(path))

    output = session.run("git", "ls-files", external=True, log=False, silent=True)
    assert isinstance(output, str)
    return output.splitlines()


def _install_deps(session: nox.Session, *requirements: str, first_call: bool = True) -> None:
    # --no-install --no-venv leads to it trying to install in the global venv
    # as --no-install only skips "reused" venvs and global is not considered reused.
    if not _try_find_option(session, "--skip-install", when_empty="True"):
        if first_call:
            session.install("--upgrade", "wheel")

        session.install("--upgrade", *map(str, requirements))

    elif any(map(_SELF_INSTALL_REGEX.fullmatch, requirements)):
        session.install("--upgrade", "--force-reinstall", "--no-dependencies", ".")


def _try_find_option(session: nox.Session, name: str, *other_names: str, when_empty: str | None = None) -> str | None:
    args_iter = iter(session.posargs)
    names = {name, *other_names}

    for arg in args_iter:
        if arg in names:
            return next(args_iter, when_empty)


def filtered_session(
    *,
    python: typing.Union[str, typing.Sequence[str], bool, None] = None,
    py: typing.Union[str, typing.Sequence[str], bool, None] = None,
    reuse_venv: bool | None = None,
    name: str | None = None,
    venv_backend: typing.Any = None,
    venv_params: typing.Any = None,
    tags: typing.List[str] | None = None,
) -> typing.Callable[[_CallbackT], typing.Union[_CallbackT, None]]:
    """Filtering version of `nox.session`."""

    def decorator(callback: _CallbackT, /) -> typing.Optional[_CallbackT]:
        name_ = name or callback.__name__
        if name_ in config.hide:
            return None

        return nox.session(
            python=python,
            py=py,
            reuse_venv=reuse_venv,
            name=name,
            venv_backend=venv_backend,
            venv_params=venv_params,
            tags=tags,
        )(callback)

    return decorator


@filtered_session(venv_backend="none")
def cleanup(session: nox.Session) -> None:
    """Cleanup any temporary files made in this project by its nox tasks."""
    import shutil

    # Remove directories
    for raw_path in ["./dist", "./site", "./.nox", "./.pytest_cache", "./coverage_html"]:
        path = pathlib.Path(raw_path)
        try:
            shutil.rmtree(str(path.absolute()))

        except Exception as exc:
            session.warn(f"[ FAIL ] Failed to remove '{raw_path}': {exc!s}")

        else:
            session.log(f"[  OK  ] Removed '{raw_path}'")

    # Remove individual files
    for raw_path in ["./.coverage", "./coverage_html.xml"]:
        path = pathlib.Path(raw_path)
        try:
            path.unlink()

        except Exception as exc:
            session.warn(f"[ FAIL ] Failed to remove '{raw_path}': {exc!s}")

        else:
            session.log(f"[  OK  ] Removed '{raw_path}'")


def _to_valid_urls(session: nox.Session) -> set[pathlib.Path] | None:
    if session.posargs:
        return set(map(pathlib.Path.resolve, map(pathlib.Path, session.posargs)))


_CONSTRAINTS_IN = pathlib.Path("./dev-requirements/constraints.in")


@filtered_session(name="freeze-dev-deps", reuse_venv=True)
def freeze_dev_deps(session: nox.Session) -> None:
    """Upgrade the dev dependencies."""
    _install_deps(session, *_deps("publish"))
    valid_urls = _to_valid_urls(session)

    if not valid_urls:
        with pathlib.Path("./pyproject.toml").open("rb") as file:
            project = tomli.load(file).get("project") or {}
            deps = project.get("dependencies") or []
            if optional := project.get("optional-dependencies"):
                deps.extend(itertools.chain(*optional.values()))

        if deps:
            with _CONSTRAINTS_IN.open("w+") as file:
                file.write("\n".join(deps) + "\n")

        else:
            _CONSTRAINTS_IN.unlink(missing_ok=True)
            pathlib.Path("./dev-requirements/constraints.txt").unlink(missing_ok=True)

    for path in pathlib.Path("./dev-requirements/").glob("*.in"):
        if not valid_urls or path.resolve() in valid_urls:
            target = path.with_name(path.name[:-3] + ".txt")
            target.unlink(missing_ok=True)
            session.run("pip-compile-cross-platform", "-o", str(target), "--min-python-version", "3.9,<3.12", str(path))


@filtered_session(name="verify-dev-deps", reuse_venv=True)
def verify_dev_deps(session: nox.Session) -> None:
    """Verify the dev deps by installing them."""
    valid_urls = _to_valid_urls(session)

    for path in pathlib.Path("./dev-requirements/").glob("*.txt"):
        if not valid_urls or path.resolve() in valid_urls:
            session.install("--dry-run", "-r", str(path))


@filtered_session(name="generate-docs", reuse_venv=True)
def generate_docs(session: nox.Session) -> None:
    """Generate docs for this project using Mkdoc."""
    _install_deps(session, *_deps("docs"))
    output_directory = _try_find_option(session, "-o", "--output") or "./site"
    session.run("mkdocs", "build", "-d", output_directory)
    for path in ("./CHANGELOG.md", "./README.md"):
        shutil.copy(path, pathlib.Path(output_directory) / path)


@filtered_session(reuse_venv=True)
def flake8(session: nox.Session) -> None:
    """Run this project's modules against the pre-defined flake8 linters."""
    _install_deps(session, *_deps("flake8"))
    session.log("Running flake8")
    session.run("pflake8", *config.top_level_targets, log=False)


@filtered_session(reuse_venv=True, name="slot-check")
def slot_check(session: nox.Session) -> None:
    """Check this project's slotted classes for common mistakes."""
    project_name = config.assert_project_name()
    _install_deps(session, ".", *_deps("lint", constrain=True))
    session.run("slotscheck", "-m", project_name)


@filtered_session(reuse_venv=True, name="spell-check")
def spell_check(session: nox.Session) -> None:
    """Check this project's text-like files for common spelling mistakes."""
    _install_deps(session, *_deps("lint"))
    session.log("Running codespell")
    session.run(
        "codespell", *_tracked_files(session, ignore_vendor=True), "--ignore-regex", "TimeSchedule|Nd", log=False
    )


@filtered_session(reuse_venv=True)
def build(session: nox.Session) -> None:
    """Build this project using flit."""
    _install_deps(session, *_deps("publish"))
    session.log("Starting build")
    session.run("flit", "build")


@filtered_session(name="verify-markup", reuse_venv=True)
def verify_markup(session: nox.Session):
    """Verify the syntax of the repo's markup files."""
    _install_deps(session, ".", *_deps("lint", constrain=True))
    tracked_files = list(_tracked_files(session))

    session.log("Running pre_commit_hooks.check_toml")
    session.run(
        "python",
        "-m",
        "pre_commit_hooks.check_toml",
        *(path for path in tracked_files if path.endswith(".toml")),
        success_codes=[0, 1],
        log=False,
    )

    session.log("Running pre_commit_hooks.check_yaml")
    session.run(
        "python",
        "-m",
        "pre_commit_hooks.check_yaml",
        *(path for path in tracked_files if path.endswith(".yml") or path.endswith(".yaml")),
        success_codes=[0, 1],
        log=False,
    )


def _publish(session: nox.Session, env: dict[str, str] | None = None) -> None:
    _install_deps(session, *_deps("publish"))
    _install_deps(session, ".", *_deps(constrain=True), first_call=False)

    env = env or session.env.copy()
    if target := session.env.get("PYPI_TARGET"):
        env["FLIT_INDEX_URL"] = target

    if token := session.env.get("PYPI_TOKEN"):
        env["FLIT_PASSWORD"] = token

    env.setdefault("FLIT_USERNAME", "__token__")
    session.run("flit", "publish", env=env)


@filtered_session(reuse_venv=True)
def publish(session: nox.Session):
    """Publish this project to pypi."""
    _publish(session)


@filtered_session(name="test-publish", reuse_venv=True)
def test_publish(session: nox.Session) -> None:
    """Publish this project to test pypi."""
    env = session.env.copy()
    env.setdefault("PYPI_TARGET", "https://test.pypi.org/legacy/")
    _publish(session, env=env)


@filtered_session(reuse_venv=True)
def reformat(session: nox.Session) -> None:
    """Reformat this project's modules to fit the standard style."""
    _install_deps(session, *_deps("reformat"))
    session.run("black", *config.top_level_targets)
    session.run("isort", *config.top_level_targets)
    session.run("pycln", *config.top_level_targets)

    tracked_files = list(_tracked_files(session, ignore_vendor=True))
    py_files = [path for path in tracked_files if re.fullmatch(r".+\.pyi?$", path)]

    session.log("Running sort-all")
    session.run("sort-all", *py_files, success_codes=[0, 1], log=False)

    session.log("Running pre_commit_hooks.end_of_file_fixer")
    session.run("python", "-m", "pre_commit_hooks.end_of_file_fixer", *tracked_files, success_codes=[0, 1], log=False)

    session.log("Running pre_commit_hooks.trailing_whitespace_fixer")
    session.run(
        "python", "-m", "pre_commit_hooks.trailing_whitespace_fixer", *tracked_files, success_codes=[0, 1], log=False
    )


@filtered_session(reuse_venv=True)
def test(session: nox.Session) -> None:
    """Run this project's tests using pytest."""
    _install_deps(session, ".", *_deps("tests", constrain=True))
    # TODO: can import-mode be specified in the config.
    session.run("pytest", "-n", "auto", "--import-mode", "importlib")


@filtered_session(name="test-coverage", reuse_venv=True)
def test_coverage(session: nox.Session) -> None:
    """Run this project's tests while recording test coverage."""
    project_name = config.assert_project_name()
    _install_deps(session, ".", *_deps("tests", constrain=True))
    # TODO: can import-mode be specified in the config.
    # https://github.com/nedbat/coveragepy/issues/1002
    session.run(
        "pytest",
        "-n",
        "auto",
        f"--cov={project_name}",
        "--cov-report",
        "html:coverage_html",
        "--cov-report",
        "xml:coverage.xml",
    )


def _run_pyright(session: nox.Session, *args: str) -> None:
    session.run("python", "-m", "pyright", "--version")
    session.run("python", "-m", "pyright", *args)


@filtered_session(name="type-check", reuse_venv=True)
def type_check(session: nox.Session) -> None:
    """Statically analyse and veirfy this project using Pyright."""
    _install_deps(session, ".", *_deps("nox", "tests", "type-checking", constrain=True))
    _run_pyright(session)
    # TODO: add allowed to fail MyPy call once it stops giving an insane amount of false-positives


@filtered_session(name="verify-types", reuse_venv=True)
def verify_types(session: nox.Session) -> None:
    """Verify the "type completeness" of types exported by the library using Pyright."""
    project_name = config.assert_project_name()
    _install_deps(session, ".", *_deps("type-checking", constrain=True))
    _run_pyright(session, "--verifytypes", project_name, "--ignoreexternal")
