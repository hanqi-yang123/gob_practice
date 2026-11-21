"""validate.py 的端到端用例：每个校验项都覆盖通过与不通过。"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "validate.py"
CREATED = "2026-11-21T00:00:00+08:00"


def write_yaml(path: Path, doc: dict) -> None:
    """写出 YAML，保留中文并使用换行结尾。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(doc, allow_unicode=True, sort_keys=False)
    path.write_text(text, encoding="utf-8", newline="\n")


def read_yaml(path: Path) -> dict:
    """读回 YAML 以便改动。"""
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def ontology_block(oid: str, name: str, imports: list[dict], version: str = "1.0.0") -> dict:
    """构造一条 ontology 记录。"""
    return {
        "ontology": {
            "id": oid,
            "kind": "container",
            "canonical_name": name,
            "path_alias": name,
            "description": f"{name}的描述。",
            "sources": ["练习 数据 #1"],
            "created_at": CREATED,
            "version": version,
            "imports": imports,
        }
    }


def concept(oid: str, name: str, parents: list[str]) -> dict:
    """构造一个概念记录。"""
    return {
        "id": oid,
        "canonical_name": name,
        "description": f"{name}的描述。",
        "parents": parents,
        "sources": ["练习 数据 #1"],
        "created_at": CREATED,
    }


def build_dataset(root: Path) -> None:
    """造两块的合规数据：子块依赖根块并跨块引用。"""
    write_yaml(root / "base" / "ontology.yaml", ontology_block("ontology_000000000001", "根块", []))
    write_yaml(
        root / "base" / "concepts.yaml",
        {"concepts": [concept("concept_000000000001", "根概念", [])]},
    )
    write_yaml(
        root / "child" / "ontology.yaml",
        ontology_block(
            "ontology_000000000002",
            "子块",
            [{"id": "ontology_000000000001", "version": ">=0.1.0,<2.0.0"}],
        ),
    )
    write_yaml(
        root / "child" / "concepts.yaml",
        {"concepts": [concept("concept_000000000002", "子概念", ["concept_000000000001"])]},
    )
    write_yaml(
        root / "child" / "operators.yaml",
        {
            "operators": [
                {
                    "id": "operator_000000000001",
                    "canonical_name": "取父概念",
                    "input_concepts": ["concept_000000000002"],
                    "output_concept": "concept_000000000002",
                    "description": "取父概念。",
                    "sources": ["练习 数据 #2"],
                    "created_at": CREATED,
                }
            ]
        },
    )
    write_yaml(
        root / "child" / "individuals.yaml",
        {
            "individuals": [
                {
                    "id": "individual_000000000001",
                    "canonical_name": "某个实例",
                    "concept": "concept_000000000002",
                    "description": "某个实例。",
                    "sources": ["练习 数据 #3"],
                    "created_at": CREATED,
                }
            ]
        },
    )


def check(data_root: Path, *args: str) -> tuple[int, str]:
    """以子进程运行校验脚本，返回退出码与合并输出。"""
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), str(data_root), "--no-git", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=ROOT,
    )
    return proc.returncode, proc.stdout + proc.stderr


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    """提供一份合规的临时数据根。"""
    root = tmp_path / "data"
    build_dataset(root)
    return root


def test_compliant_dataset_passes(data_dir: Path) -> None:
    """合规数据应零错误退出。"""
    code, out = check(data_dir)
    assert code == 0, out
    assert "0 errors" in out


def test_missing_field(data_dir: Path) -> None:
    """缺少必填字段应报 missing_field。"""
    path = data_dir / "base" / "concepts.yaml"
    doc = read_yaml(path)
    del doc["concepts"][0]["description"]
    write_yaml(path, doc)
    code, out = check(data_dir)
    assert code == 1
    assert "missing_field" in out


def test_bad_id(data_dir: Path) -> None:
    """id 格式不合法应报 bad_id。"""
    path = data_dir / "base" / "concepts.yaml"
    doc = read_yaml(path)
    doc["concepts"][0]["id"] = "concept_XYZ"
    write_yaml(path, doc)
    code, out = check(data_dir)
    assert code == 1
    assert "bad_id" in out


def test_bad_kind(data_dir: Path) -> None:
    """kind 取值不在枚举内应报 bad_kind。"""
    path = data_dir / "base" / "ontology.yaml"
    doc = read_yaml(path)
    doc["ontology"]["kind"] = "shared"
    write_yaml(path, doc)
    code, out = check(data_dir)
    assert code == 1
    assert "bad_kind" in out


def test_import_not_found(data_dir: Path) -> None:
    """依赖指向不存在的块应报 import_not_found。"""
    path = data_dir / "child" / "ontology.yaml"
    doc = read_yaml(path)
    doc["ontology"]["imports"][0]["id"] = "ontology_000000000009"
    write_yaml(path, doc)
    code, out = check(data_dir)
    assert code == 1
    assert "import_not_found" in out


def test_import_cycle(data_dir: Path) -> None:
    """块之间相互依赖应报 import_cycle。"""
    path = data_dir / "base" / "ontology.yaml"
    doc = read_yaml(path)
    doc["ontology"]["imports"] = [
        {"id": "ontology_000000000002", "version": ">=0.1.0,<2.0.0"}
    ]
    write_yaml(path, doc)
    code, out = check(data_dir)
    assert code == 1
    assert "import_cycle" in out


def test_version_out_of_range(data_dir: Path) -> None:
    """被依赖块版本不满足范围应报 version_out_of_range。"""
    path = data_dir / "child" / "ontology.yaml"
    doc = read_yaml(path)
    doc["ontology"]["imports"][0]["version"] = ">=9.0.0"
    write_yaml(path, doc)
    code, out = check(data_dir)
    assert code == 1
    assert "version_out_of_range" in out


def test_bad_version_range(data_dir: Path) -> None:
    """版本范围无法解析应报 bad_version_range。"""
    path = data_dir / "child" / "ontology.yaml"
    doc = read_yaml(path)
    doc["ontology"]["imports"][0]["version"] = "^1.0"
    write_yaml(path, doc)
    code, out = check(data_dir)
    assert code == 1
    assert "bad_version_range" in out


def test_ref_out_of_scope(data_dir: Path) -> None:
    """未声明依赖就跨块引用应报 ref_out_of_scope。"""
    path = data_dir / "child" / "ontology.yaml"
    doc = read_yaml(path)
    doc["ontology"]["imports"] = []
    write_yaml(path, doc)
    code, out = check(data_dir)
    assert code == 1
    assert "ref_out_of_scope" in out


def test_ref_not_found(data_dir: Path) -> None:
    """引用不存在的对象应报 ref_not_found。"""
    path = data_dir / "child" / "concepts.yaml"
    doc = read_yaml(path)
    doc["concepts"][0]["parents"] = ["concept_0000000000ff"]
    write_yaml(path, doc)
    code, out = check(data_dir)
    assert code == 1
    assert "ref_not_found" in out


def test_dup_id(data_dir: Path) -> None:
    """同一 id 在两处内容不同应报 dup_id。"""
    path = data_dir / "base" / "concepts.yaml"
    doc = read_yaml(path)
    doc["concepts"].append(concept("concept_000000000001", "另一个名字", []))
    write_yaml(path, doc)
    code, out = check(data_dir)
    assert code == 1
    assert "dup_id" in out


def test_no_block(data_dir: Path) -> None:
    """数据根下直接放对象文件应报 no_block。"""
    write_yaml(
        data_dir / "concepts.yaml",
        {"concepts": [concept("concept_000000000003", "孤立概念", [])]},
    )
    code, out = check(data_dir)
    assert code == 1
    assert "no_block" in out


def run_git(repo: Path, *args: str) -> None:
    """在临时仓库里执行 git，失败即断言。"""
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    proc = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, encoding="utf-8", env=env
    )
    assert proc.returncode == 0, proc.stderr


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """建一个含基线提交与 origin/main 引用的小仓库。"""
    root = tmp_path / "repo"
    shutil.copytree(ROOT / "schema", root / "schema")
    (root / "scripts").mkdir()
    shutil.copy2(SCRIPT, root / "scripts" / "validate.py")
    build_dataset(root / "data")
    run_git(root, "init", "-b", "main")
    run_git(root, "add", "-A")
    run_git(root, "commit", "-m", "chore: base")
    run_git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
    return root


def check_repo(repo: Path) -> tuple[int, str]:
    """在临时仓库内运行校验脚本。"""
    proc = subprocess.run(
        [sys.executable, "scripts/validate.py", "data"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=repo,
    )
    return proc.returncode, proc.stdout + proc.stderr


def test_no_version_bump(repo: Path) -> None:
    """改了本块 CO 文件却未升 version 应报 no_version_bump。"""
    path = repo / "data" / "child" / "concepts.yaml"
    doc = read_yaml(path)
    doc["concepts"][0]["description"] = "改过的描述。"
    write_yaml(path, doc)
    code, out = check_repo(repo)
    assert code == 1, out
    assert "no_version_bump" in out


def test_breaking_change_delete(repo: Path) -> None:
    """删除被引用的对象应报 breaking_change。"""
    path = repo / "data" / "base" / "concepts.yaml"
    write_yaml(path, {"concepts": []})
    code, out = check_repo(repo)
    assert code == 1, out
    assert "breaking_change" in out


def test_breaking_change_modify(repo: Path) -> None:
    """修改被引用的对象应报 breaking_change，升版也不能豁免。"""
    path = repo / "data" / "child" / "concepts.yaml"
    doc = read_yaml(path)
    doc["concepts"][0]["description"] = "改过的描述。"
    write_yaml(path, doc)
    ontology_path = repo / "data" / "child" / "ontology.yaml"
    ontology_doc = read_yaml(ontology_path)
    ontology_doc["ontology"]["version"] = "1.0.1"
    write_yaml(ontology_path, ontology_doc)
    code, out = check_repo(repo)
    assert code == 1, out
    assert "breaking_change" in out


def test_change_with_bump_passes(repo: Path) -> None:
    """新增对象并同 PR 升版应通过。"""
    path = repo / "data" / "child" / "concepts.yaml"
    doc = read_yaml(path)
    doc["concepts"].append(concept("concept_000000000005", "新增概念", []))
    write_yaml(path, doc)
    ontology_path = repo / "data" / "child" / "ontology.yaml"
    ontology_doc = read_yaml(ontology_path)
    ontology_doc["ontology"]["version"] = "1.0.1"
    write_yaml(ontology_path, ontology_doc)
    code, out = check_repo(repo)
    assert code == 0, out
