"""Frozen project snapshots and fail-closed native execution permissions.

This module never edits user configuration or starts model inference. A passing
command/exec probe is evidence for the selected binary/profile, not a claim that
the legacy read-only sandbox limits filesystem reads.
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import stat
import tempfile

from core import BenchmarkError, check_sensitive, digest, home, now, read, require, sha, write


MODES = ("fixed-evidence", "project-readonly")
SKIP_DIRECTORIES = {".git", ".svn", ".hg", ".codex", "node_modules", "__pycache__", "Binaries", "Intermediate", "DerivedDataCache"}
PROFILE_VERSION = 1
POLICY = {
    "backend": "mxc", "filesystem": "platform-minimal-plus-explicit-roots",
    "network": "disabled", "native_tools": "enabled", "delegation": "native",
    "agent_defaults": "native-inheritance", "write_scope": "scratch-only",
}


def execution_mode(protocol):
    profile = protocol.get("execution_profile")
    return profile.get("mode", "fixed-evidence") if isinstance(profile, dict) else "fixed-evidence"


def toml_value(value):
    """Encode CLI -c values, including tables that JSON cannot represent as TOML."""
    if value is None:
        raise BenchmarkError("Null cannot be encoded as a TOML override")
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (int, float)):
        return json.dumps(value, allow_nan=False)
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(x) for x in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(json.dumps(str(k), ensure_ascii=False) + " = " + toml_value(v) for k, v in value.items()) + " }"
    raise BenchmarkError("Unsupported TOML override value")


def instruction_fingerprint(paths):
    """Freeze reported instruction files by canonical path and bytes, never body."""
    require(isinstance(paths, list), "Instruction sources must be a list of absolute file paths")
    result = {}
    for raw in paths:
        require(isinstance(raw, str) and bool(raw), "Instruction source path is invalid")
        path = Path(raw).expanduser()
        require(path.is_absolute(), "Instruction source must be an absolute file path")
        path = path.resolve()
        require(path.is_file(), "Instruction source file is unavailable")
        check_sensitive(str(path))
        result[str(path)] = sha(path)
    return [{"path": path, "sha256": result[path]} for path in sorted(result)]


def _overlaps(a, b):
    a, b = Path(a).resolve(), Path(b).resolve()
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


def _not_link(path):
    p = Path(path)
    # is_symlink alone does not catch Windows junctions and other reparse points.
    for part in [p, *p.parents]:
        info = part.lstat()
        require(not stat.S_ISLNK(info.st_mode) and not (getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)),
                "Snapshot paths must not contain links or reparse points")


def _profile_hash(profile):
    return digest({k: v for k, v in profile.items() if k != "profile_sha256"})


def _runtime_files(roots):
    result = {}
    for raw in roots:
        root = Path(raw)
        _not_link(root)
        files = [root] if root.is_file() else sorted(root.rglob("*"))
        entries = {}
        for p in files:
            _not_link(p)
            if p.is_file():
                require(p.stat().st_nlink == 1, "Runtime roots must not contain hardlink aliases")
                entries[p.relative_to(root).as_posix() if root.is_dir() else "."] = sha(p)
        require(entries, "Runtime read root contains no files")
        result[str(root)] = entries
    return result


def _runtime_container_roots():
    """Container folders are never narrow executable/library allowances."""
    user = Path.home().resolve()
    roots = {user, user.parent, user / "AppData", user / "AppData/Local", user / "AppData/Roaming"}
    for name in ("APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "SYSTEMROOT", "PUBLIC", "ALLUSERSPROFILE"):
        if os.environ.get(name):
            roots.add(Path(os.environ[name]).resolve())
    if os.name == "nt":
        roots.update(Path(user.anchor) / name for name in ("ProgramData", "Program Files", "Program Files (x86)", "Windows", "Users"))
    return roots


def _check_file_sensitive(path):
    data = Path(path).read_bytes()
    # ASCII credentials can occur inside GB2312 or UTF-16 source/output too.
    check_sensitive(data.decode("utf-8", errors="ignore"))
    if b"\x00" in data:
        check_sensitive(data.replace(b"\x00", b"").decode("utf-8", errors="ignore"))


def prepare_profile(spec, runroot):
    """Copy explicit source roots and return the profile frozen into protocol.

    spec: {source_roots:[{path,logical_path}], model_work_root?:path,
           runtime_read_roots?:[path], forbidden_read_paths?:[path]}.
    The work root must be new and outside the private run and all source roots.
    """
    require(isinstance(spec, dict), "Execution specification must be a JSON object")
    require(set(spec) <= {"mode", "backend", "source_roots", "model_work_root", "runtime_read_roots", "forbidden_read_paths"}, "Unknown execution specification field")
    require(spec.get("mode", "project-readonly") == "project-readonly", "Use explicit fixed-evidence mode without a project specification")
    require(spec.get("backend", "mxc") == "mxc", "Project-readonly currently requires strict native MXC; no fallback is permitted")
    roots = spec.get("source_roots")
    require(isinstance(roots, list) and roots, "Project-readonly needs explicit source_roots")
    runroot = Path(runroot).resolve()
    private = [runroot, home() / "benchmarks", home() / "sessions", home() / "memories"]
    work = Path(spec["model_work_root"]).expanduser().absolute() if spec.get("model_work_root") else Path(tempfile.gettempdir()) / ("codex-benchmark-" + secrets.token_hex(10))
    require(not work.exists(), "Model work root must be new; do not reuse another sample's files")
    _not_link(work.parent)
    work = work.resolve()
    require(not any(_overlaps(work, p) for p in private), "Model work root must be separate from benchmark, session and memory directories")
    parsed, logical_names = [], []
    for item in roots:
        require(isinstance(item, dict) and set(item) == {"path", "logical_path"}, "Each source root needs exactly path and logical_path")
        source = Path(item["path"]).expanduser().absolute()
        require(source.exists(), "Source root unavailable")
        _not_link(source)
        source = source.resolve()
        require(source.is_file() or source.is_dir(), "Source root must be a regular file or directory")
        require(not any(_overlaps(source, p) for p in [*private, work]), "Source roots may not expose private benchmark/session/memory files")
        logical = PurePosixPath(item["logical_path"])
        require(not logical.is_absolute() and logical.parts and not any(p in ("", ".", "..") for p in logical.parts) and not re.search(r"[\\:]", str(logical)), "logical_path must be a safe relative POSIX path")
        require(not any(logical == old or logical in old.parents or old in logical.parents for old in logical_names), "Source logical paths overlap")
        logical_names.append(logical)
        parsed.append((source, logical))
    runtime = []
    for raw in spec.get("runtime_read_roots", []):
        p = Path(raw).expanduser().absolute()
        require(p.exists(), "Runtime read root unavailable")
        _not_link(p)
        p = p.resolve()
        require(p.parent != p and p not in _runtime_container_roots() and not any(_overlaps(p, q) for q in [*private, work, *(source for source, _ in parsed)]), "Runtime roots must be narrow executable/library paths, outside private data and original source roots")
        runtime.append(str(p))
    work.mkdir(parents=False)
    source_root, scratch, probe_root = work / "source", work / "scratch", work / "permission-read-control"
    for p in [source_root, scratch, probe_root]:
        p.mkdir()
    read_control = probe_root / "read-control.txt"
    read_control.write_text("benchmark read control\n", encoding="utf-8")
    archived_source = runroot / "input/source-snapshot"
    archived_source.mkdir(parents=True, exist_ok=False)
    files, excluded = {}, []
    for source, logical in parsed:
        candidates = [source] if source.is_file() else sorted(source.rglob("*"))
        for p in candidates:
            relative = p.relative_to(source) if source.is_dir() else Path()
            if any(part in SKIP_DIRECTORIES for part in relative.parts):
                if p.is_dir():
                    excluded.append(str(logical / PurePosixPath(relative.as_posix())))
                continue
            _not_link(p)
            if p.is_dir():
                continue
            require(p.is_file() and p.stat().st_nlink == 1, "Snapshot files must be regular and must not have hardlink aliases")
            target_rel = logical / PurePosixPath(relative.as_posix()) if source.is_dir() else logical
            require(str(target_rel) not in files, "Duplicate snapshot file")
            _check_file_sensitive(p)
            before = sha(p)
            target = source_root.joinpath(*target_rel.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(p, target)
            require(sha(p) == before and sha(target) == before, "Source changed while copying its snapshot")
            archived = archived_source.joinpath(*target_rel.parts)
            archived.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(target, archived)
            require(sha(archived) == before, "Archival source copy differs")
            files[str(target_rel)] = {"source_path": str(p), "sha256": before, "bytes": target.stat().st_size}
    require(bool(files), "Source snapshot is empty")
    manifest = {"schema_version": 1, "files": files, "excluded_directories": sorted(set(excluded))}
    manifest_path = runroot / "input/execution-manifest.json"
    write(manifest_path, manifest)
    forbidden = [str(p.resolve()) for p in private if p.exists()]
    for raw in spec.get("forbidden_read_paths", []):
        p = Path(raw).expanduser().resolve()
        require(p.exists(), "Explicit forbidden read path unavailable")
        require(not any(_overlaps(p, q) for q in [source_root, scratch, probe_root, *runtime]), "Forbidden and allowed roots overlap")
        forbidden.append(str(p))
    profile = {"schema_version": PROFILE_VERSION, "mode": "project-readonly", "policy": dict(POLICY),
               "run_root": str(runroot), "model_work_root": str(work), "source_root": str(source_root), "scratch_root": str(scratch), "work_cwd": str(source_root),
               "probe_root": str(probe_root), "probe_control_sha256": sha(read_control),
               "manifest_path": str(manifest_path), "manifest_sha256": sha(manifest_path),
               "source_content_sha256": digest({k: v["sha256"] for k, v in files.items()}),
               "runtime_read_roots": sorted(set(runtime)), "runtime_files_sha256": _runtime_files(sorted(set(runtime))),
               "forbidden_read_paths": sorted(set(forbidden))}
    profile["permission_profile_id"] = "benchmark_" + digest({"work": str(work), "sources": profile["source_content_sha256"]})[:20]
    profile["comparison_fingerprint"] = digest({"mode": profile["mode"], "policy": profile["policy"], "sources": profile["source_content_sha256"], "runtime_files": profile["runtime_files_sha256"]})
    profile["profile_sha256"] = _profile_hash(profile)
    check_profile(profile)
    return profile


def check_profile(profile, runtime=True, runroot=None):
    """Validate private archived bytes; optionally also validate live model work.

    Offline exports can pass their new runroot without needing the original
    temporary work directory by setting runtime=False. Sampling uses the default.
    """
    require(isinstance(profile, dict) and profile.get("schema_version") == PROFILE_VERSION and profile.get("mode") == "project-readonly", "Unsupported execution profile")
    require(profile.get("policy") == POLICY, "Execution policy changed or unsupported")
    require(profile.get("profile_sha256") == _profile_hash(profile), "Frozen execution profile changed")
    root = Path(runroot or profile["run_root"])
    manifest_path = root / "input/execution-manifest.json"
    require(sha(manifest_path) == profile["manifest_sha256"], "Source manifest changed")
    files = read(manifest_path)["files"]
    roots = [root / "input/source-snapshot"]
    if runtime:
        roots.append(Path(profile["source_root"]))
    for source_root in roots:
        _not_link(source_root)
        actual = {}
        for p in source_root.rglob("*"):
            _not_link(p)
            if p.is_file():
                require(p.stat().st_nlink == 1, "Snapshot hardlink alias detected")
                actual[p.relative_to(source_root).as_posix()] = sha(p)
        require(actual == {k: v["sha256"] for k, v in files.items()}, "Frozen source file set or bytes changed")
        require(digest(actual) == profile["source_content_sha256"], "Source content fingerprint changed")
    if runtime:
        control = Path(profile["probe_root"]) / "read-control.txt"
        require(sha(control) == profile["probe_control_sha256"], "Readonly permission control changed")
        _not_link(Path(profile["scratch_root"]))
        require(_runtime_files(profile["runtime_read_roots"]) == profile["runtime_files_sha256"], "Frozen runtime executable/library files changed")
    return profile


def launch_overrides(profile):
    check_profile(profile)
    filesystem = {":minimal": "read", profile["source_root"]: "read", profile["scratch_root"]: "write", profile["probe_root"]: "read"}
    filesystem.update({p: "read" for p in profile["runtime_read_roots"]})
    return {"windows.sandbox": "mxc", "approval_policy": "never", "default_permissions": profile["permission_profile_id"],
            "permissions": {profile["permission_profile_id"]: {"filesystem": filesystem, "network": {"enabled": False}}}}


def thread_permissions(profile):
    check_profile(profile)
    return {"permissions": profile["permission_profile_id"]}


def _ps_string(value):
    return "'" + str(value).replace("'", "''") + "'"


def _probe_read_targets(profile, private_control, junction=None):
    blocked = list(profile["forbidden_read_paths"]) + [str(private_control), str(Path(profile["probe_root"]) / ".." / ".." / private_control.parent.name / private_control.name)]
    # The explicit relative escape above is normalized by the host OS, not by a
    # simulated tool matcher; it must fail for the same reason as absolute reads.
    blocked[-1] = str(Path(profile["source_root"]) / os.path.relpath(private_control, profile["source_root"]))
    if os.name == "nt":
        blocked += ["\\\\?\\" + str(private_control), str(private_control).replace("\\", "/")]
    targets = [{"path": path, "directory": Path(path).is_dir()} for path in blocked]
    if junction is not None:
        targets.append({"path": str(junction / private_control.name), "directory": False})
    return targets


def _probe_command(profile, private_control, targets):
    """Only booleans are emitted; forbidden bytes and command errors stay private."""
    control = Path(profile["probe_root"]) / "read-control.txt"
    scratch_control = Path(profile["scratch_root"]) / "permission-write-control.txt"
    source_file = next(iter(read(profile["manifest_path"])["files"]))
    source_file = Path(profile["source_root"]) / source_file
    paths = ",".join("@{path=" + _ps_string(x["path"]) + ";directory=" + ("$true" if x["directory"] else "$false") + "}" for x in targets)
    script = "$ErrorActionPreference='Stop'; $result=[ordered]@{}; "
    script += f"try {{ $null=[IO.File]::ReadAllBytes({_ps_string(control)}); $result.read_control=$true }} catch {{ $result.read_control=$false }}; "
    script += f"try {{ $null=[IO.File]::ReadAllBytes({_ps_string(source_file)}); $result.source_read=$true }} catch {{ $result.source_read=$false }}; "
    script += f"try {{ [IO.File]::WriteAllText({_ps_string(scratch_control)},'probe'); $result.scratch_write=([IO.File]::ReadAllText({_ps_string(scratch_control)}) -eq 'probe') }} catch {{ $result.scratch_write=$false }}; "
    # Attempt a harmless exclusive new file in the readonly source root. Never
    # attempt to overwrite a frozen business source if a broken sandbox permits it.
    source_write = Path(profile["source_root"]) / ".permission-unexpected-write"
    script += f"try {{ $s=[IO.File]::Open({_ps_string(source_write)},[IO.FileMode]::CreateNew); $s.Dispose(); $result.source_write_denied=$false }} catch {{ $result.source_write_denied=$true }}; "
    script += f"$checks=@(); foreach($probe in @({paths})) {{ try {{ if($probe.directory) {{ $null=[IO.Directory]::GetFileSystemEntries($probe.path) }} else {{ $null=[IO.File]::ReadAllBytes($probe.path) }}; $checks+= $false }} catch [UnauthorizedAccessException] {{ $checks+=$true }} catch [System.Security.SecurityException] {{ $checks+=$true }} catch {{ $checks+=$false }} }}; "
    script += "$result.forbidden_reads_denied=$checks; $result | ConvertTo-Json -Compress -Depth 4"
    return script


def permission_probe(server, profile, fingerprint):
    """Run no-inference native commands and save a binary/profile-bound gate."""
    check_profile(profile)
    runroot = Path(profile["run_root"])
    report = {"schema_version": 1, "at": now(), "inference_calls": 0, "backend": "mxc", "profile_sha256": profile["profile_sha256"],
              "source_content_sha256": profile["source_content_sha256"], "fingerprint": fingerprint, "passed": False, "shells": []}
    private_control = runroot / "permission-probe-private/control.txt"
    private_control.parent.mkdir(parents=True, exist_ok=True)
    private_control.write_text(secrets.token_hex(24), encoding="ascii")
    junction = None
    try:
        require(os.name == "nt", "Project-readonly MXC permission probe requires Windows")
        alias = Path(profile["scratch_root"]) / "permission-junction-control"
        require(not alias.exists() and not alias.is_symlink(), "Permission probe junction path already exists")
        try:
            import _winapi
            _winapi.CreateJunction(str(private_control.parent), str(alias))
            junction = alias
            report["junction_probe"] = "included"
        except (ImportError, AttributeError, OSError):
            report["junction_probe"] = "unavailable; not proven"
        targets = _probe_read_targets(profile, private_control, junction)
        report["forbidden_check_count"] = len(targets)
        script = _probe_command(profile, private_control, targets)
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        powershell = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        require(powershell.is_file(), "Windows PowerShell unavailable for native permission proof")
        shells = [str(powershell)]
        if shutil.which("pwsh"):
            shells.append(shutil.which("pwsh"))
        for shell in shells:
            response = server.rpc("command/exec", {"command": [shell, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                "cwd": profile["source_root"], "permissionProfile": profile["permission_profile_id"], "disableTimeout": True, "outputBytesCap": 16384})
            if response.get("exitCode") != 0:
                report["native_exit_code"] = response.get("exitCode")
                raise BenchmarkError("Native MXC permission command failed; no fallback is allowed")
            try:
                observed = json.loads(response.get("stdout", "").strip())
            except (ValueError, TypeError):
                raise BenchmarkError("Native permission proof returned malformed output") from None
            expected_keys = {"read_control", "source_read", "scratch_write", "source_write_denied", "forbidden_reads_denied"}
            require(isinstance(observed, dict) and set(observed) == expected_keys, "Native permission proof returned unexpected checks")
            report["shells"].append({"executable": shell, "checks": observed})
            require(all(observed[k] is True for k in expected_keys - {"forbidden_reads_denied"}), "Native source/scratch access restrictions failed")
            denied = observed["forbidden_reads_denied"]
            require(isinstance(denied, list) and len(denied) == len(targets) and all(x is True for x in denied), "Private benchmark/session/memory reads are not demonstrably blocked")
        check_profile(profile)
        report["passed"] = True
    except BaseException as error:
        report["failure_type"] = type(error).__name__
        write(runroot / "permission-probe.json", report)
        raise
    finally:
        if junction is not None:
            # Remove the junction itself, never recursively remove its target.
            require(junction.parent.resolve() == Path(profile["scratch_root"]).resolve(), "Permission junction escaped its owned scratch")
            os.rmdir(junction)
    write(runroot / "permission-probe.json", report)
    write(runroot / "permission-probe-seal.json", {"report_sha256": sha(runroot / "permission-probe.json"), "profile_sha256": profile["profile_sha256"], "fingerprint_sha256": digest(fingerprint)})
    return report


def require_permission_probe(profile, fingerprint):
    check_profile(profile)
    runroot = Path(profile["run_root"])
    path = runroot / "permission-probe.json"
    seal_path = runroot / "permission-probe-seal.json"
    require(path.is_file() and seal_path.is_file(), "Run the no-model MXC permission proof before sampling")
    report, seal = read(path), read(seal_path)
    require(seal.get("report_sha256") == sha(path) and seal.get("profile_sha256") == profile["profile_sha256"] and seal.get("fingerprint_sha256") == digest(fingerprint), "Permission proof does not match the frozen binary/profile/environment")
    require(report.get("passed") is True and report.get("inference_calls") == 0 and report.get("backend") == "mxc" and report.get("fingerprint") == fingerprint, "Native permission proof failed or environment changed")
    return report


def reset_scratch(profile, destination=None):
    """Archive settled trial files if requested, then clear only owned scratch.

    The caller must settle every related process before calling. Paths and the
    entire entry set are verified before the first deletion; no symlinks,
    junctions or hardlinks are followed. Archives stay outside model-readable roots.
    """
    check_profile(profile)
    scratch = Path(profile["scratch_root"]).resolve()
    work = Path(profile["model_work_root"]).resolve()
    require(scratch.parent == work and scratch.name == "scratch" and scratch != scratch.parent,
            "Scratch target is not the exact owned workspace scratch directory")
    entries = sorted(scratch.rglob("*"))
    files, directories = [], []
    for p in entries:
        _not_link(p)
        require(p.resolve().is_relative_to(scratch), "Scratch path escaped its owned directory")
        if p.is_file():
            require(p.stat().st_nlink == 1, "Scratch hardlink aliases cannot be archived or cleared")
            _check_file_sensitive(p)
            files.append((p, sha(p)))
        else:
            require(p.is_dir(), "Unsupported scratch entry")
            directories.append(p)
    manifest = {p.relative_to(scratch).as_posix(): h for p, h in files}
    if destination is not None:
        archive = Path(destination).absolute()
        require(not archive.exists(), "Scratch archive destination already exists")
        _not_link(archive.parent)
        allowed = [profile["source_root"], profile["scratch_root"], profile["probe_root"], *profile["runtime_read_roots"]]
        require(not any(_overlaps(archive, p) for p in allowed), "Scratch archive must stay outside model-readable roots")
        archive.mkdir()
        for p, h in files:
            target = archive / p.relative_to(scratch)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(p, target)
            require(sha(target) == h, "Scratch archival bytes differ")
        write(archive / "scratch-manifest.json", {"files_sha256": manifest})
    # Revalidate immediately before mutations; only paths below the checked
    # absolute scratch target are ever unlinked or removed.
    require(sorted(scratch.rglob("*")) == entries, "Scratch changed while being archived")
    for p, h in files:
        _not_link(p)
        require(sha(p) == h, "Scratch file changed while being archived")
    for p, _ in files:
        p.unlink()
    for p in sorted(directories, key=lambda p: len(p.parts), reverse=True):
        p.rmdir()
    require(not any(scratch.iterdir()), "Scratch was not fully cleared")
    return {"files_sha256": manifest, "archived": destination is not None}
