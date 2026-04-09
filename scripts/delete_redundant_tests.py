#!/usr/bin/env python3
"""
Delete redundant tests based on coverage manifest.

Reads the manifest produced by analyze_test_coverage.py and:
- Deletes entire test files where all tests are redundant
- For partial files, removes individual test functions/methods using AST
- Preserves fixtures, helpers, imports, and class structure

Does NOT touch conftest.py or fixture files.
"""

import ast
import json
import os
import re
import sys
from collections import defaultdict


def load_manifest(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def _test_name_from_context(context: str) -> tuple:
    """Extract (class_name_or_none, function_name) from context string."""
    parts = context.split(".")
    if len(parts) == 4:
        return parts[2], parts[3]
    elif len(parts) == 3:
        return None, parts[2]
    else:
        return None, parts[-1]


def _file_path_from_manifest_file(manifest_file: str) -> str:
    return manifest_file


def get_all_test_ids_in_file(manifest: dict, file_key: str) -> set:
    all_ids = set()
    for item in manifest.get("kept", []):
        if item["file"] == file_key:
            all_ids.add(item["test_id"])
    for item in manifest.get("redundant", []):
        if item["file"] == file_key:
            all_ids.add(item["test_id"])
    return all_ids


def is_fully_redundant_file(manifest: dict, file_key: str, redundant_ids: set) -> bool:
    all_ids = get_all_test_ids_in_file(manifest, file_key)
    return all_ids and all_ids.issubset(redundant_ids)


def _decorator_start(node) -> int:
    if node.decorator_list:
        return node.decorator_list[0].lineno
    return node.lineno


def _is_fixture(node):
    """Check if a function node is decorated with @pytest.fixture."""
    for dec in node.decorator_list:
        if isinstance(dec, ast.Attribute) and dec.attr == "fixture":
            return True
        if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute) and dec.func.attr == "fixture":
            return True
        if isinstance(dec, ast.Name) and dec.id == "fixture":
            return True
        if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Name) and dec.func.id == "fixture":
            return True
    return False


def _has_autouse_fixture(class_node):
    """Check if a class has an autouse fixture."""
    for item in class_node.body:
        if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in item.decorator_list:
            if isinstance(dec, ast.Call):
                for kw in dec.keywords:
                    if kw.arg == "autouse":
                        if isinstance(kw.value, ast.Constant) and kw.value.value:
                            return True
    return False


def _class_has_remaining_tests(class_node, funcs_to_remove: set) -> bool:
    """Check if a class will still have test methods after removal."""
    for item in class_node.body:
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if item.name.startswith("test_") and item.name not in funcs_to_remove:
                return True
    return False


def remove_test_functions(file_path: str, functions_to_remove: list) -> str:
    """Remove specific test functions/methods from a Python file.

    Uses a line-marking approach: mark lines belonging to functions/classes
    to remove, then output only unmarked lines.
    """
    with open(file_path) as f:
        source = f.read()

    tree = ast.parse(source)
    lines = source.split("\n")
    total_lines = len(lines)

    # Build removal targets per class
    remove_by_class = defaultdict(set)  # class_name -> set of func_names
    remove_toplevel = set()  # set of func_names

    for ctx in functions_to_remove:
        cls_name, func_name = _test_name_from_context(ctx)
        if cls_name:
            remove_by_class[cls_name].add(func_name)
        else:
            remove_toplevel.add(func_name)

    # Mark lines to remove (1-indexed, stored as set)
    lines_to_remove = set()

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef) and node.name in remove_by_class:
            funcs_to_remove_in_class = remove_by_class[node.name]

            # Check if class should be removed entirely
            has_remaining_tests = _class_has_remaining_tests(node, funcs_to_remove_in_class)
            has_autouse = _has_autouse_fixture(node)

            # Count remaining meaningful body items
            remaining_items = []
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if item.name not in funcs_to_remove_in_class:
                        remaining_items.append(item)
                elif isinstance(item, ast.Expr) and isinstance(item.value, ast.Constant):
                    pass  # docstring
                elif isinstance(item, ast.Pass):
                    pass
                else:
                    remaining_items.append(item)

            if not remaining_items or (not has_remaining_tests and not has_autouse):
                # Remove entire class
                start = _decorator_start(node)
                end = node.end_lineno
                for i in range(start, end + 1):
                    lines_to_remove.add(i)
            else:
                # Remove individual functions from class
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        if item.name in funcs_to_remove_in_class:
                            start = _decorator_start(item)
                            end = item.end_lineno
                            for i in range(start, end + 1):
                                lines_to_remove.add(i)

        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in remove_toplevel and not _is_fixture(node):
                start = _decorator_start(node)
                end = node.end_lineno
                for i in range(start, end + 1):
                    lines_to_remove.add(i)

    if not lines_to_remove:
        return source

    # Build new source keeping only non-removed lines
    new_lines = []
    for i, line in enumerate(lines, 1):
        if i not in lines_to_remove:
            new_lines.append(line)

    result = "\n".join(new_lines)

    # Clean up excessive blank lines (more than 2 consecutive)
    result = re.sub(r'\n{4,}', '\n\n\n', result)

    # Ensure file ends with newline
    if result and not result.endswith('\n'):
        result += '\n'

    return result


def main():
    manifest_path = sys.argv[1] if len(sys.argv) > 1 else "coverage_manifest.json"
    dry_run = "--dry-run" in sys.argv

    manifest = load_manifest(manifest_path)
    redundant_ids = {r["test_id"] for r in manifest["redundant"]}

    print(f"Manifest: {manifest['stats']['redundant_count']} redundant tests to remove")
    if dry_run:
        print("DRY RUN — no files will be modified")

    redundant_by_file = manifest["redundant_by_file"]

    files_deleted = []
    files_modified = {}
    files_skipped = []
    tests_removed = 0

    for file_key, test_ids in sorted(redundant_by_file.items()):
        file_path = _file_path_from_manifest_file(file_key)

        if not os.path.exists(file_path):
            print(f"  SKIP {file_path} — file not found")
            continue

        if "conftest" in file_path:
            print(f"  SKIP {file_path} — conftest file")
            continue

        if is_fully_redundant_file(manifest, file_key, redundant_ids):
            print(f"  DELETE {file_path} ({len(test_ids)} tests)")
            if not dry_run:
                os.remove(file_path)
            files_deleted.append(file_path)
            tests_removed += len(test_ids)
        else:
            if not dry_run:
                modified = remove_test_functions(file_path, test_ids)
                try:
                    ast.parse(modified)
                except SyntaxError as e:
                    print(f"  SKIP {file_path} — syntax error at line {e.lineno}: {e.msg}")
                    files_skipped.append(file_path)
                    continue
                with open(file_path, "w") as f:
                    f.write(modified)
            print(f"  MODIFY {file_path} (remove {len(test_ids)} tests)")
            files_modified[file_path] = len(test_ids)
            tests_removed += len(test_ids)

    print(f"\nSummary:")
    print(f"  Tests removed: {tests_removed}")
    print(f"  Files deleted: {len(files_deleted)}")
    print(f"  Files modified: {len(files_modified)}")
    print(f"  Files skipped: {len(files_skipped)}")
    if files_deleted:
        print(f"  Deleted files: {', '.join(files_deleted)}")
    if files_skipped:
        print(f"  Skipped files: {', '.join(files_skipped)}")


if __name__ == "__main__":
    main()
