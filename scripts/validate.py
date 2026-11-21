"""按 schema、依赖图与基线比较校验 CO 数据。

单文件结构归 schema；唯一性、引用越界、依赖成环、同 PR 升版与破坏性更新由本脚本负责。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import yaml
from jsonschema import Draft7Validator, FormatChecker
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version
from referencing import Registry, Resource

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_DIR = ROOT / "schema"
DEFAULT_DATA = ROOT / "data"
DEFAULT_BASE = "origin/main"

SECTION_SCHEMA = {
    "ontology": "ontology.schema.json",
    "concepts": "concept.schema.json",
    "operators": "operator.schema.json",
    "individuals": "individual.schema.json",
}

# 记录类型 -> 引用字段：字段名、目标类型、是否为数组
REF_FIELDS = {
    "concept": [("parents", "concept", True)],
    "operator": [
        ("input_concepts", "concept", True),
        ("output_concept", "concept", False),
    ],
    "individual": [("concept", "concept", False)],
}

KEYWORD_CODE = {
    "required": "missing_field",
    "additionalProperties": "unknown_field",
    "type": "wrong_type",
    "format": "bad_format",
    "enum": "bad_kind",
    "minLength": "empty_value",
    "minItems": "missing_field",
    "uniqueItems": "dup_ref",
}

TEXT_FIELDS = {"canonical_name", "description", "sources"}
ID_FIELDS = {"id", "parents", "input_concepts", "output_concept", "concept"}


class DuplicateKeyError(yaml.YAMLError):
    """YAML 中出现重复键。"""


class StrictLoader(yaml.SafeLoader):
    """禁止重复键的 YAML 加载器。"""


def _construct_mapping(loader: StrictLoader, node, deep: bool = False):
    """构造映射时检测重复键。"""
    seen = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            raise DuplicateKeyError(f"duplicate key: {key}")
        seen.add(key)
    return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)


StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


class Block:
    """一个带 ontology.yaml 的作用域块。"""

    def __init__(self, directory: Path, rel: str, record: dict) -> None:
        self.dir = directory
        self.rel = rel
        self.record = record
        self.id = record.get("id")
        self.version = record.get("version")
        self.imports = record.get("imports") or []


class Report:
    """收集并输出校验结果。"""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.errors = 0

    def add(self, rel, index, rid, path, code, message) -> None:
        """追加一条错误，行格式与 GOB 一致。"""
        where = f"第 {index} 条 ({rid})" if index is not None else f"({rid})"
        self.lines.append(f"{rel}  {where}  {path}  [{code}] {message}")
        self.errors += 1

    def note(self, message: str) -> None:
        """输出一条提示，不计入错误。"""
        self.lines.append(f"note: {message}")

    def flush(self) -> None:
        """输出全部结果。"""
        for line in self.lines:
            print(line)


def load_schemas() -> dict[str, dict]:
    """读取全部 schema 文件。"""
    return {
        p.name: json.loads(p.read_text(encoding="utf-8")) for p in SCHEMA_DIR.glob("*.schema.json")
    }


def build_validators() -> dict[str, Draft7Validator]:
    """为每类文件构造带跨文件引用的校验器。"""
    schemas = load_schemas()
    registry = Registry().with_resources(
        (schema["$id"], Resource.from_contents(schema)) for schema in schemas.values()
    )
    return {
        section: Draft7Validator(schemas[name], registry=registry, format_checker=FormatChecker())
        for section, name in SECTION_SCHEMA.items()
    }


def pointer(path) -> str:
    """把 jsonschema 的错误路径渲染成字段路径。"""
    out = ""
    for part in path:
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            out += f".{part}" if out else str(part)
    return out or "<root>"


def field_name(path) -> str:
    """取路径最后一段的字段名。"""
    parts = [p for p in path if isinstance(p, str)]
    return parts[-1] if parts else ""


def error_code(err) -> str:
    """把 schema 校验关键词映射为错误码。"""
    keyword = err.validator
    if keyword == "pattern":
        return "bad_id" if field_name(err.absolute_path) in ID_FIELDS else "bad_format"
    if keyword == "uniqueItems":
        name = field_name(err.absolute_path)
        return "dup_source" if name == "sources" else "dup_ref"
    return KEYWORD_CODE.get(keyword, "invalid")


def type_of(oid) -> str:
    """按 id 前缀判断对象类型。"""
    return oid.split("_", 1)[0] if isinstance(oid, str) and "_" in oid else "unknown"


def detect_cycle(graph: dict[str, list[str]]) -> list[list[str]]:
    """在依赖图里找出环，返回每个环的节点序列。"""
    color: dict[str, int] = {}
    cycles: list[list[str]] = []
    stack: list[str] = []

    def visit(node: str) -> None:
        color[node] = 1
        stack.append(node)
        for nxt in graph.get(node, []):
            if nxt not in graph:
                continue
            if color.get(nxt, 0) == 1:
                cycles.append(stack[stack.index(nxt) :] + [nxt])
            elif color.get(nxt, 0) == 0:
                visit(nxt)
        stack.pop()
        color[node] = 2

    for node in graph:
        if color.get(node, 0) == 0:
            visit(node)
    return cycles


def parse_yaml(path: Path, rel: str, report: Report):
    """读入 YAML，语法或重复键错误记一条后返回 None。"""
    try:
        return yaml.load(path.read_text(encoding="utf-8"), Loader=StrictLoader)
    except DuplicateKeyError as exc:
        report.add(rel, None, "-", "<root>", "dup_key", str(exc))
    except yaml.YAMLError as exc:
        report.add(rel, None, "-", "<root>", "yaml_error", str(exc).splitlines()[0])
    return None


def add_schema_errors(rel, section, items, report, validator) -> None:
    """按记录序号归组输出 schema 错误。"""
    doc = {section: items[0]} if section == "ontology" else {section: items}
    errors = sorted(validator.iter_errors(doc), key=lambda e: [str(p) for p in e.absolute_path])
    for err in errors:
        parts = list(err.absolute_path)
        if section == "ontology":
            index, rest, record = None, parts[1:], items[0]
        elif len(parts) >= 2 and isinstance(parts[1], int):
            local = parts[1]
            index, rest = local + 1, parts[2:]
            record = items[local] if 0 <= local < len(items) else None
        else:
            index, rest, record = None, parts, None
        rid = record.get("id", "-") if isinstance(record, dict) else "-"
        report.add(rel, index, rid, pointer(rest), error_code(err), err.message)


def validate_document(rel, path, doc, validators, report) -> list[tuple[int | None, dict]]:
    """校验单个文件的 schema 结构，返回记录清单。"""
    if not isinstance(doc, dict) or not doc:
        report.add(rel, None, "-", "<root>", "bad_document", "文件必须是非空对象")
        return []

    stem = path.stem
    if stem not in SECTION_SCHEMA:
        report.add(rel, None, "-", "<root>", "unknown_section", f"未知文件 {stem}.yaml")
        return []

    if stem == "ontology":
        extra = [key for key in doc if key != "ontology"]
        if extra or not isinstance(doc.get("ontology"), dict):
            report.add(rel, None, "-", "<root>", "misplacement", "该文件只能含 ontology 记录")
            return []
        add_schema_errors(rel, "ontology", [doc["ontology"]], report, validators["ontology"])
        return [(None, doc["ontology"])]

    extra = [key for key in doc if key != stem]
    if extra:
        report.add(
            rel,
            None,
            "-",
            "<root>",
            "misplacement",
            f"文件名 {stem}.yaml 与顶层字段 {extra[0]} 不一致",
        )
        return []
    items = doc.get(stem)
    if not isinstance(items, list):
        report.add(rel, None, "-", stem, "wrong_type", "顶层字段必须是数组")
        return []

    add_schema_errors(rel, stem, items, report, validators[stem])
    return [(pos + 1, item) for pos, item in enumerate(items) if isinstance(item, dict)]


def nearest_block(directory: Path, blocks: dict[Path, Block]) -> Block | None:
    """取最近的祖先块，目录上下级不构成依赖。"""
    for parent in (directory, *directory.parents):
        if parent in blocks:
            return blocks[parent]
    return None


def load_blocks(data_root, validators, report) -> dict[Path, Block]:
    """扫描 ontology.yaml，建立块表。"""
    blocks: dict[Path, Block] = {}
    for path in sorted(data_root.rglob("ontology.yaml")):
        rel = path.relative_to(data_root).as_posix()
        doc = parse_yaml(path, rel, report)
        if doc is None:
            continue
        for _, record in validate_document(rel, path, doc, validators, report):
            blocks[path.parent] = Block(path.parent, rel, record)
    return blocks


def load_objects(data_root, blocks, validators, report) -> list[dict]:
    """扫描对象文件并归入所属块。"""
    records: list[dict] = []
    for path in sorted(data_root.rglob("*.yaml")):
        if path.name == "ontology.yaml":
            continue
        rel = path.relative_to(data_root).as_posix()
        doc = parse_yaml(path, rel, report)
        if doc is None:
            continue
        block = nearest_block(path.parent, blocks)
        for index, record in validate_document(rel, path, doc, validators, report):
            if block is None:
                report.add(
                    rel,
                    index,
                    str(record.get("id", "-")),
                    "id",
                    "no_block",
                    "对象不在任何带 ontology.yaml 的块内",
                )
                continue
            records.append({"rel": rel, "index": index, "record": record, "block": block})
    return records


def build_index(records: list[dict]) -> dict[str, list[dict]]:
    """按 id 建立对象索引。"""
    index: dict[str, list[dict]] = {}
    for item in records:
        oid = item["record"].get("id")
        if isinstance(oid, str):
            index.setdefault(oid, []).append(item)
    return index


def check_duplicates(index, report) -> None:
    """同一 id 多处出现且内容不同时报错，内容一致视为副本。"""
    for oid, hits in index.items():
        first = hits[0]["record"]
        for item in hits[1:]:
            if item["record"] != first:
                report.add(
                    item["rel"],
                    item["index"],
                    oid,
                    "id",
                    "dup_id",
                    f"与 {hits[0]['rel']} 中的同 id 对象内容不一致",
                )


def dependency_closure(block: Block, by_id: dict[str, Block]) -> set[str]:
    """按 imports 求依赖闭包，只认显式声明的依赖。"""
    seen: set[str] = set()
    stack = [block.id]
    while stack:
        bid = stack.pop()
        if not isinstance(bid, str) or bid in seen:
            continue
        seen.add(bid)
        dependency = by_id.get(bid)
        if dependency is None:
            continue
        for item in dependency.imports:
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                stack.append(item["id"])
    return seen


def check_refs(index, by_id, report) -> None:
    """检查引用的存在性、类型、自引用与作用域。"""
    closures: dict[str, set[str]] = {}
    for oid, hits in index.items():
        for item in hits:
            block = item["block"]
            scope = closures.setdefault(block.id, dependency_closure(block, by_id))
            for fld, target, is_list in REF_FIELDS.get(type_of(oid), []):
                raw = item["record"].get(fld, [])
                values = raw if isinstance(raw, list) else [raw]
                for pos, ref in enumerate(values):
                    if not isinstance(ref, str):
                        continue
                    path = f"{fld}[{pos}]" if is_list else fld
                    if ref == oid:
                        report.add(
                            item["rel"], item["index"], oid, path, "self_ref", "不能引用自身"
                        )
                        continue
                    found = index.get(ref)
                    if not found:
                        report.add(
                            item["rel"],
                            item["index"],
                            oid,
                            path,
                            "ref_not_found",
                            f"指向的 {target} 不存在",
                        )
                        continue
                    if type_of(ref) != target:
                        report.add(
                            item["rel"],
                            item["index"],
                            oid,
                            path,
                            "wrong_ref_type",
                            f"应为 {target}_ 前缀",
                        )
                        continue
                    if found[0]["block"].id not in scope:
                        report.add(
                            item["rel"],
                            item["index"],
                            oid,
                            path,
                            "ref_out_of_scope",
                            f"引用了 {found[0]['block'].rel} 的对象，该块不在依赖闭包内",
                        )


def check_concept_cycles(index, report) -> None:
    """检查概念继承关系是否成环。"""
    parents = {
        oid: values
        for oid, hits in index.items()
        if type_of(oid) == "concept"
        for values in [list(hits[0]["record"].get("parents", []))]
    }
    for cycle in detect_cycle(parents):
        if len(set(cycle)) < 2:
            continue
        hit = index[cycle[0]][0]
        report.add(
            hit["rel"],
            hit["index"],
            cycle[0],
            "parents",
            "ref_cycle",
            "继承关系成环：" + " -> ".join(cycle),
        )


def check_imports(blocks, by_id, report) -> None:
    """检查依赖存在、去重、成环与版本范围。"""
    for block in blocks.values():
        seen: set[str] = set()
        for pos, item in enumerate(block.imports):
            if not isinstance(item, dict):
                continue
            dep_id = item.get("id")
            path = f"imports[{pos}]"
            if dep_id in seen:
                report.add(block.rel, None, str(block.id), path, "dup_import", "依赖重复声明")
                continue
            seen.add(dep_id)
            if dep_id == block.id:
                report.add(block.rel, None, str(block.id), path, "import_self", "不能依赖自身")
                continue
            dependency = by_id.get(dep_id)
            if dependency is None:
                report.add(
                    block.rel,
                    None,
                    str(block.id),
                    f"{path}.id",
                    "import_not_found",
                    f"依赖块 {dep_id} 不存在",
                )
                continue
            try:
                spec = SpecifierSet(str(item.get("version")))
            except InvalidSpecifier:
                report.add(
                    block.rel,
                    None,
                    str(block.id),
                    f"{path}.version",
                    "bad_version_range",
                    f"版本范围 {item.get('version')} 无法解析",
                )
                continue
            try:
                current = Version(str(dependency.version))
            except InvalidVersion:
                report.add(
                    block.rel,
                    None,
                    str(block.id),
                    f"{path}.version",
                    "bad_version",
                    f"被依赖块版本 {dependency.version} 不是合法语义化版本",
                )
                continue
            if not spec.contains(current, prereleases=True):
                report.add(
                    block.rel,
                    None,
                    str(block.id),
                    f"{path}.version",
                    "version_out_of_range",
                    f"{dependency.rel} 当前版本 {current} 不满足 {spec}",
                )


def check_import_cycles(blocks, report) -> None:
    """按 imports 建图并检查依赖环。"""
    by_id = {block.id: block for block in blocks.values() if isinstance(block.id, str)}
    graph = {
        bid: [
            item["id"]
            for item in block.imports
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        ]
        for bid, block in by_id.items()
    }
    for cycle in detect_cycle(graph):
        if len(set(cycle)) < 2:
            continue
        block = by_id[cycle[0]]
        report.add(
            block.rel,
            None,
            str(block.id),
            "imports",
            "import_cycle",
            "依赖成环：" + " -> ".join(cycle),
        )


def run_git(args: list[str]) -> str:
    """在仓库根执行 git，非零退出抛异常。"""
    proc = subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8"
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "git 命令失败")
    return proc.stdout


def git_checks(data_root, blocks, index, base, report) -> None:
    """与基线比较，检查同 PR 升版与破坏性更新。"""
    try:
        data_rel = data_root.resolve().relative_to(ROOT).as_posix()
        run_git(["rev-parse", "--verify", "--quiet", base])
    except (ValueError, RuntimeError):
        report.note(f"无法与 {base} 比较，跳过升版与破坏性检查")
        return

    changed = set(
        run_git(
            ["-c", "core.quotepath=false", "diff", "--name-only", base, "--", data_rel]
        ).splitlines()
    )
    base_files = [
        line
        for line in run_git(
            ["-c", "core.quotepath=false", "ls-tree", "-r", "--name-only", base, "--", data_rel]
        ).splitlines()
        if line.endswith(".yaml")
    ]

    base_versions: dict[str, str] = {}
    base_objects: dict[str, tuple[str, dict]] = {}
    for path in base_files:
        try:
            doc = yaml.safe_load(run_git(["show", f"{base}:{path}"]))
        except (RuntimeError, yaml.YAMLError):
            continue
        if not isinstance(doc, dict):
            continue
        if "ontology" in doc and isinstance(doc["ontology"], dict):
            base_versions[path] = doc["ontology"].get("version")
        for section, items in doc.items():
            if section == "ontology" or not isinstance(items, list):
                continue
            for record in items:
                if isinstance(record, dict) and isinstance(record.get("id"), str):
                    base_objects.setdefault(record["id"], (path, record))

    for block in blocks.values():
        try:
            block_rel = block.dir.resolve().relative_to(ROOT).as_posix()
        except ValueError:
            continue
        touched = [f for f in changed if f.startswith(block_rel + "/")]
        co_touched = [f for f in touched if Path(f).name != "ontology.yaml"]
        if not co_touched:
            continue
        if base_versions.get(f"{block_rel}/ontology.yaml") != block.version:
            continue
        report.add(
            block.rel,
            None,
            str(block.id),
            "version",
            "no_version_bump",
            "改了本块的 CO 文件，但 version 未变",
        )

    referenced: set[str] = set()
    for oid, (_, record) in base_objects.items():
        for fld, _, _ in REF_FIELDS.get(type_of(oid), []):
            raw = record.get(fld, [])
            for ref in raw if isinstance(raw, list) else [raw]:
                if isinstance(ref, str):
                    referenced.add(ref)

    for oid in sorted(referenced):
        origin = base_objects.get(oid)
        if origin is None:
            continue
        hits = index.get(oid)
        if not hits:
            report.add(
                origin[0],
                None,
                oid,
                "id",
                "breaking_change",
                "删除了已被其他对象引用的对象",
            )
        elif hits[0]["record"] != origin[1]:
            report.add(
                hits[0]["rel"],
                hits[0]["index"],
                oid,
                "id",
                "breaking_change",
                "修改了已被其他对象引用的对象",
            )


def validate_root(data_root: Path, base: str, use_git: bool) -> int:
    """校验一个数据根，输出报告并返回错误数。"""
    validators = build_validators()
    report = Report()

    blocks = load_blocks(data_root, validators, report)
    records = load_objects(data_root, blocks, validators, report)
    index = build_index(records)
    by_id = {block.id: block for block in blocks.values() if isinstance(block.id, str)}

    check_duplicates(index, report)
    check_refs(index, by_id, report)
    check_concept_cycles(index, report)
    check_imports(blocks, by_id, report)
    check_import_cycles(blocks, report)
    if use_git:
        git_checks(data_root, blocks, index, base, report)

    report.flush()
    files = len(list(data_root.rglob("*.yaml")))
    print(
        f"summary: {files} files, {len(blocks)} blocks, "
        f"{len(index)} objects, {report.errors} errors"
    )
    return report.errors


def main() -> int:
    """CLI 入口：校验 CO 数据并返回退出码。"""
    parser = argparse.ArgumentParser(description="校验 GOB 练习仓库的 CO 数据")
    parser.add_argument("data_root", nargs="?", default=str(DEFAULT_DATA))
    parser.add_argument("--base", default=DEFAULT_BASE, help="比较基线，默认 origin/main")
    parser.add_argument("--no-git", action="store_true", help="跳过升版与破坏性检查")
    args = parser.parse_args()

    data_root = Path(args.data_root)
    if not data_root.is_dir():
        print(f"数据根不存在：{data_root}")
        return 2
    return 1 if validate_root(data_root, args.base, not args.no_git) else 0


if __name__ == "__main__":
    sys.exit(main())
