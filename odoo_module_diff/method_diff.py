import ast
import os
from typing import Any, Dict, List, Optional
import git


def get_py_blobs(tree: git.Tree) -> Dict[str, str]:
    """Recursively fetch python file content from git tree object."""
    blobs = {}
    for item in tree:
        if item.type == "blob" and item.name.endswith(".py"):
            try:
                blobs[item.path] = item.data_stream.read().decode("utf-8", errors="ignore")
            except Exception:
                pass
        elif item.type == "tree":
            blobs.update(get_py_blobs(item))
    return blobs


def format_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """Format AST function node into normalized signature string: def name(args...)."""
    args = []
    pos_args = node.args.args
    defaults = node.args.defaults
    num_no_defaults = len(pos_args) - len(defaults)

    for idx, arg in enumerate(pos_args):
        arg_str = arg.arg
        if idx >= num_no_defaults:
            default_node = defaults[idx - num_no_defaults]
            try:
                arg_str += f"={ast.unparse(default_node)}"
            except Exception:
                arg_str += "=..."
        args.append(arg_str)

    if node.args.vararg:
        args.append(f"*{node.args.vararg.arg}")
    elif node.args.kwonlyargs:
        args.append("*")

    for idx, arg in enumerate(node.args.kwonlyargs):
        arg_str = arg.arg
        if idx < len(node.args.kw_defaults) and node.args.kw_defaults[idx] is not None:
            try:
                arg_str += f"={ast.unparse(node.args.kw_defaults[idx])}"
            except Exception:
                arg_str += "=..."
        args.append(arg_str)

    if node.args.kwarg:
        args.append(f"**{node.args.kwarg.arg}")

    arg_list_str = ", ".join(args)
    return f"def {node.name}({arg_list_str})"


class MethodVisitor(ast.NodeVisitor):
    def __init__(self):
        self.classes: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self.current_class: Optional[str] = None

    def visit_ClassDef(self, node: ast.ClassDef):
        prev_class = self.current_class
        self.current_class = node.name
        if node.name not in self.classes:
            self.classes[node.name] = {}
        self.generic_visit(node)
        self.current_class = prev_class

    def visit_FunctionDef(self, node: ast.FunctionDef):
        self._handle_func(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
        self._handle_func(node)

    def _handle_func(self, node: ast.FunctionDef | ast.AsyncFunctionDef):
        if self.current_class:
            self.classes[self.current_class][node.name] = {
                "sig": format_signature(node),
                "lineno": node.lineno,
            }


def extract_methods_from_commit(
    commit: git.Commit, addon: str
) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """Extract all method signatures across all python model files in an addon at a specific commit."""
    path = f"addons/{addon}/models" if addon != "base" else "odoo/addons/base/models"
    try:
        tree = commit.tree
        for part in path.split("/"):
            tree = tree[part]
    except KeyError:
        return {}

    blobs = get_py_blobs(tree)
    classes: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for filepath, code in blobs.items():
        try:
            parsed = ast.parse(code)
            visitor = MethodVisitor()
            visitor.visit(parsed)
            for cls, methods in visitor.classes.items():
                if cls not in classes:
                    classes[cls] = {}
                classes[cls].update(methods)
        except Exception:
            pass
    return classes


def build_commit_map_from_items(commit_items: List[Dict[str, Any]]) -> Dict[str, str]:
    """Build index of method_name -> comment_line from scanned structural commit items."""
    commit_map = {}
    for item in commit_items:
        c_sha = item.get("commit_sha", "")[:10]
        pr = item.get("pr", "")
        author = item.get("author", "")
        summary = item.get("summary", "")
        comment_line = f"# PR: {pr} | Commit: {c_sha} | {author} | {summary}"
        for diff_str in item.get("diffs", []):
            for line in diff_str.splitlines():
                if (
                    line.startswith("+    def ")
                    or line.startswith("-    def ")
                    or line.startswith("+   def ")
                    or line.startswith("-   def ")
                ) and "(" in line:
                    mname = line.split("def ")[1].split("(")[0].strip()
                    if mname not in commit_map:
                        commit_map[mname] = comment_line
    return commit_map


def generate_method_signatures_diff(
    repo: git.Repo,
    addon: str,
    start_commit: git.Commit,
    end_commit: git.Commit,
    output_module_dir: str,
    commit_items: Optional[List[Dict[str, Any]]] = None,
):
    """
    Generates method_signatures.diff for the given addon between start_commit and end_commit.
    Move-invariant: methods moved without signature change are ignored.
    """
    print(f"Extracting method signatures for {addon} at start/end commits...")
    methods_start = extract_methods_from_commit(start_commit, addon)
    methods_end = extract_methods_from_commit(end_commit, addon)

    all_classes = sorted(set(methods_start.keys()) | set(methods_end.keys()))
    diff_data = {}
    total_added = 0
    total_removed = 0
    total_changed = 0

    for cls in all_classes:
        m_start = methods_start.get(cls, {})
        m_end = methods_end.get(cls, {})

        added = []
        removed = []
        changed = []

        all_methods = sorted(set(m_start.keys()) | set(m_end.keys()))
        for m in all_methods:
            s = m_start.get(m)
            e = m_end.get(m)
            if s and not e:
                removed.append((m, s["sig"]))
            elif e and not s:
                added.append((m, e["sig"]))
            elif s and e and s["sig"] != e["sig"]:
                changed.append((m, s["sig"], e["sig"]))

        if added or removed or changed:
            diff_data[cls] = {
                "added": added,
                "removed": removed,
                "changed": changed,
            }
            total_added += len(added)
            total_removed += len(removed)
            total_changed += len(changed)

    if not diff_data:
        print(f"No method signature changes found for addon {addon}.")
        return

    commit_map = {}
    if commit_items:
        commit_map = build_commit_map_from_items(commit_items)

    os.makedirs(output_module_dir, exist_ok=True)
    out_filepath = os.path.join(output_module_dir, "method_signatures.diff")

    with open(out_filepath, "w") as f:
        f.write(f"# Method Signatures Delta: {addon}\n")
        f.write(
            f"# Summary: {total_changed} modified, {total_added} added, {total_removed} removed\n\n"
        )

        for cls, data in diff_data.items():
            f.write(f"[{cls}]\n")
            for mname, orig_sig, new_sig in data["changed"]:
                cmt = commit_map.get(mname, "")
                if cmt:
                    f.write(f"{cmt}\n")
                f.write(f"- {orig_sig}\n")
                f.write(f"+ {new_sig}\n\n")

            for mname, sig in data["removed"]:
                cmt = commit_map.get(mname, "")
                if cmt:
                    f.write(f"{cmt}\n")
                f.write(f"- {sig}\n\n")

            for mname, sig in data["added"]:
                cmt = commit_map.get(mname, "")
                if cmt:
                    f.write(f"{cmt}\n")
                f.write(f"+ {sig}\n\n")

    print(
        f"Generated {out_filepath} ({total_changed} changed, {total_added} added, {total_removed} removed)"
    )
