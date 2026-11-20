#!/usr/bin/env python3
"""Extract Python module inventory from dodaios package."""
import os
import json
import ast
from pathlib import Path

REPO_ROOT = Path("/mnt/6C7CE26A7CE22E90/Dodaios-dev")
DODAIOS_ROOT = REPO_ROOT / "dodaios"
EXCLUDE_DIRS = {"__pycache__", ".pytest_cache", "node_modules", ".git", "dist", ".venv", ".freebuff", ".knowledge"}

def should_exclude(path: Path) -> bool:
    for part in path.parts:
        if part in EXCLUDE_DIRS:
            return True
    return False

def extract_module_info(filepath: Path) -> dict:
    """Extract classes, functions, and imports from a Python file."""
    try:
        content = filepath.read_text(encoding='utf-8')
        tree = ast.parse(content)
    except Exception as e:
        return {"error": str(e), "classes": [], "functions": [], "imports": []}

    classes = []
    functions = []
    imports = []

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            classes.append({
                "name": node.name,
                "line": node.lineno,
                "bases": [ast.unparse(b) if hasattr(ast, 'unparse') else str(b) for b in node.bases],
                "methods": [n.name for n in node.body if isinstance(n, ast.FunctionDef)]
            })
        elif isinstance(node, ast.FunctionDef):
            functions.append({
                "name": node.name,
                "line": node.lineno,
                "args": [arg.arg for arg in node.args.args],
                "is_async": isinstance(node, ast.AsyncFunctionDef) if hasattr(ast, 'AsyncFunctionDef') else False
            })
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.append({"module": alias.name, "type": "import"})
            else:
                module = node.module or ""
                for alias in node.names:
                    imports.append({"module": f"{module}.{alias.name}" if module else alias.name, "type": "from"})

    return {"classes": classes, "functions": functions, "imports": imports}

def main():
    modules = []
    py_files = []

    for filepath in DODAIOS_ROOT.rglob("*.py"):
        if should_exclude(filepath):
            continue
        rel_path = filepath.relative_to(REPO_ROOT)
        py_files.append(str(rel_path))

        info = extract_module_info(filepath)
        modules.append({
            "path": str(rel_path),
            "size": filepath.stat().st_size,
            "classes": info.get("classes", []),
            "functions": info.get("functions", []),
            "imports": info.get("imports", []),
            "error": info.get("error")
        })

    # Save inventory
    inventory = {
        "total_files": len(py_files),
        "files": py_files,
        "modules": modules
    }

    output_path = REPO_ROOT / ".knowledge" / "inventory" / "files.jsonl"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, 'w') as f:
        for module in modules:
            f.write(json.dumps(module) + "\n")

    # Also save summary
    summary_path = REPO_ROOT / ".knowledge" / "inventory" / "summary.json"
    with open(summary_path, 'w') as f:
        json.dump({
            "total_files": len(py_files),
            "total_classes": sum(len(m["classes"]) for m in modules),
            "total_functions": sum(len(m["functions"]) for m in modules),
            "total_imports": sum(len(m["imports"]) for m in modules)
        }, f, indent=2)

    print(f"Extracted {len(py_files)} Python files")
    print(f"Total classes: {sum(len(m['classes']) for m in modules)}")
    print(f"Total functions: {sum(len(m['functions']) for m in modules)}")
    print(f"Total imports: {sum(len(m['imports']) for m in modules)}")

if __name__ == "__main__":
    main()