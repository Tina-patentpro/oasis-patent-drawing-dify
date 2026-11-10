"""Build with the official Dify CLI from an explicit, symlink-free allowlist."""

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_FILES = (
    "manifest.yaml",
    "main.py",
    "requirements.txt",
    "pyproject.toml",
    "README.md",
    "_assets/icon.png",
    "provider/__init__.py",
    "provider/patent_drawing.py",
    "provider/patent_drawing.yaml",
    "tools/__init__.py",
    "tools/generate.py",
    "tools/generate.yaml",
    "tools/edit.py",
    "tools/edit.yaml",
    "patent_drawing/__init__.py",
    "patent_drawing/errors.py",
    "patent_drawing/prompts.py",
    "patent_drawing/images.py",
    "patent_drawing/file_input.py",
    "patent_drawing/client.py",
    "patent_drawing/invocation.py",
    "patent_drawing/safe_tool.py",
    "patent_drawing/text_client.py",
    "tools/plan_set.py",
    "tools/plan_set.yaml",
    "patent_drawing/sets.py",
    "patent_drawing/diagrams.py",
    "patent_drawing/set_delivery.py",
    "patent_drawing/set_tools.py",
    "tools/validate_set.py",
    "tools/validate_set.yaml",
    "tools/render_set_figure.py",
    "tools/render_set_figure.yaml",
    "tools/package_set.py",
    "tools/package_set.yaml",
    "_assets/NotoSansSC.ttf",
    "_assets/NotoSansSC-OFL.txt",
    "_assets/drawing_set_v2_prompt.txt",
    "_assets/cn_drawing_rules.json",
    "patent_drawing/operation_store.py",
    "patent_drawing/materials.py",
    "patent_drawing/pdf_worker.py",
    "patent_drawing/cn_policy.py",
    "patent_drawing/source_contract.py",
    "patent_drawing/source_geometry.py",
    "patent_drawing/source_profiles.py",
    "patent_drawing/expression_contract.py",
    "patent_drawing/expression_guide.py",
    "patent_drawing/projected_relations.py",
    "patent_drawing/expression_panels.py",
    "patent_drawing/cable_grounding_scene.py",
    "patent_drawing/scene_derivation.py",
    "patent_drawing/support_assembly.py",
    "patent_drawing/member_visibility.py",
    "patent_drawing/guide_readiness.py",
    "patent_drawing/structural_semantics.py",
    "patent_drawing/external_attempts.py",
    "patent_drawing/build_identity.py",
    "patent_drawing/ledger_continuity.py",
    "_assets/runtime-members.json",
    "patent_drawing/visual_request.py",
    "patent_drawing/spatial_guide.py",
    "patent_drawing/reference_bundle.py",
    "patent_drawing/moderation_recovery.py",
    "patent_drawing/release_gates.py",

    "patent_drawing/scene.py",
    "patent_drawing/annotations.py",
    "patent_drawing/source_labels.py",
    "patent_drawing/planner_contract.py",
    "patent_drawing/marking.py",
    "patent_drawing/visibility.py",
    "patent_drawing/reference_preparation.py",
    "patent_drawing/linked_edits.py",
    "patent_drawing/flow_fidelity.py",
    "patent_drawing/spatial_brief.py",
    "patent_drawing/coverage.py",
    "patent_drawing/budget_policy.py",
    "patent_drawing/operator_policy.py",
    "patent_drawing/views.py",
    "patent_drawing/delivery_v2.py",
    "patent_drawing/quality.py",
    "patent_drawing/workspace.py",
    "tools/workspace.py",
    "tools/workspace.yaml",
)
SECRET_PATTERN = re.compile(rb"sk-(?:proj-)?[A-Za-z0-9_-]{20,}")


def build_staging_tree(staging: Path, *, root: Path = ROOT):
    manifest=root/'_assets/runtime-members.json'
    if (not manifest.is_file() or manifest.stat().st_size>32768
            or any(p.is_symlink()for p in [manifest,*manifest.parents])):
        raise ValueError('runtime member manifest missing or unsafe')
    try:
        members=json.loads((root/'_assets/runtime-members.json').read_text())
    except (OSError,ValueError)as error:
        raise ValueError('runtime member manifest unreadable')from error
    if members!=list(RUNTIME_FILES):raise ValueError('runtime member manifest differs from explicit package allowlist')
    for name in RUNTIME_FILES:
        source = root / name
        if not source.is_file() or any(
            p.is_symlink() for p in [source, *source.parents] if p != root.parent
        ):
            raise ValueError(f"Missing or symlinked runtime member: {name}")
        target = staging / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)


def verify_package(package: Path) -> dict:
    with zipfile.ZipFile(package) as archive:
        names = archive.namelist()
        files = {n for n in names if not n.endswith("/")}
        allowed = set(RUNTIME_FILES) | {".checksum"}
        if (
            len(names) != len(set(names))
            or not set(RUNTIME_FILES).issubset(files)
            or files - allowed
        ):
            raise ValueError(
                "Package contains unexpected, duplicate, or missing members"
            )
        hashes = {}
        for name in files:
            content = archive.read(name)
            if SECRET_PATTERN.search(content):
                raise ValueError("Possible secret found in package; delivery blocked")
            hashes[name] = hashlib.sha256(content).hexdigest()
        if json.loads(archive.read('_assets/runtime-members.json'))!=list(RUNTIME_FILES):
            raise ValueError('runtime member manifest differs from package allowlist')
        return {
            "package": str(package),
            "sha256": hashlib.sha256(package.read_bytes()).hexdigest(),
            "byte_count": package.stat().st_size,
            "members": hashes,
            "secret_scan_passed": True,
        }


def package_plugin(cli: Path, output: Path) -> dict:
    if not cli.is_file():
        raise ValueError(
            "Official Dify CLI is required; ZIP fallback is intentionally unsupported"
        )
    if any(p.exists() for p in [output, output.with_suffix(output.suffix + '.sha256'), output.with_suffix(output.suffix + '.audit.json')]):
        raise ValueError('Package or receipt already exists; choose a new output path')
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="patent-drawing-package-") as directory:
        staging = Path(directory)
        build_staging_tree(staging)
        result = subprocess.run(
            [
                str(cli.resolve()),
                "plugin",
                "package",
                str(staging),
                "-o",
                str(output.resolve()),
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode:
            raise RuntimeError(
                "Official Dify CLI packaging failed: " + result.stderr[-2000:]
            )
    report = verify_package(output)
    report["builder"] = "official-dify-cli"
    report["cli_output"] = result.stdout.strip()
    output.with_suffix(output.suffix + ".sha256").write_text(
        report["sha256"] + "  " + output.name + "\n"
    )
    output.with_suffix(output.suffix + ".audit.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cli", type=Path, required=True)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "dist/oasisintwrite_patent_drawing-0.3.9.difypkg",
    )
    args = parser.parse_args()
    result = package_plugin(args.cli, args.output)
    print(
        json.dumps(
            {k: v for k, v in result.items() if k != "members"},
            ensure_ascii=False,
            indent=2,
        )
    )
