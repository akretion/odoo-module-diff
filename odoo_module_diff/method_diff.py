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


def extract_model_identifier(class_node: ast.ClassDef) -> str:
    """Extracts _name or _inherit string from an AST ClassDef node, falling back to class_node.name."""
    model_name = None
    inherit_name = None

    for stmt in class_node.body:
        if isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name):
                    if target.id == "_name":
                        if isinstance(stmt.value, ast.Constant) and isinstance(
                            stmt.value.value, str
                        ):
                            model_name = stmt.value.value
                        elif isinstance(stmt.value, ast.Str):
                            model_name = stmt.value.s
                    elif target.id == "_inherit":
                        if isinstance(stmt.value, ast.Constant) and isinstance(
                            stmt.value.value, str
                        ):
                            inherit_name = stmt.value.value
                        elif isinstance(stmt.value, ast.Str):
                            inherit_name = stmt.value.s
                        elif (
                            isinstance(stmt.value, (ast.List, ast.Tuple))
                            and stmt.value.elts
                        ):
                            first = stmt.value.elts[0]
                            if isinstance(first, ast.Constant) and isinstance(
                                first.value, str
                            ):
                                inherit_name = first.value
                            elif isinstance(first, ast.Str):
                                inherit_name = first.s

    if model_name:
        return model_name
    if inherit_name:
        return inherit_name
    return class_node.name


class MethodVisitor(ast.NodeVisitor):
    def __init__(self):
        self.models: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self.current_model: Optional[str] = None

    def visit_ClassDef(self, node: ast.ClassDef):
        prev_model = self.current_model
        model_id = extract_model_identifier(node)
        self.current_model = model_id
        if model_id not in self.models:
            self.models[model_id] = {}
        self.generic_visit(node)
        self.current_model = prev_model

    def visit_FunctionDef(self, node: ast.FunctionDef):
        self._handle_func(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
        self._handle_func(node)

    def _handle_func(self, node: ast.FunctionDef | ast.AsyncFunctionDef):
        if self.current_model:
            self.models[self.current_model][node.name] = {
                "sig": format_signature(node),
                "lineno": node.lineno,
            }


def extract_methods_from_commit(
    commit: git.Commit,
    addon: str,
    module_prefix: str = "addons/",
) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """Extract all method signatures across all python model files in an addon at a specific commit, grouped by Odoo model ID."""
    if addon == "base" and module_prefix == "addons/":
        path = "odoo/addons/base/models"
    else:
        path = f"{module_prefix}{addon}/models"
    try:
        tree = commit.tree
        for part in path.split("/"):
            tree = tree[part]
    except KeyError:
        return {}

    blobs = get_py_blobs(tree)
    models: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for filepath, code in blobs.items():
        try:
            parsed = ast.parse(code)
            visitor = MethodVisitor()
            visitor.visit(parsed)
            for model_id, methods in visitor.models.items():
                if model_id not in models:
                    models[model_id] = {}
                models[model_id].update(methods)
        except Exception:
            pass
    return models


def build_commit_map_from_repo(
    repo: git.Repo,
    addon: str,
    start_commit: git.Commit,
    end_commit: git.Commit,
    module_prefix: str = "addons/",
) -> Dict[str, List[str]]:
    """Build index of method_name -> list of comment lines for all commits that touched each method."""
    if addon == "base" and module_prefix == "addons/":
        path = "odoo/addons/base/models/"
    else:
        path = f"{module_prefix}{addon}/models/"
    commits = list(
        repo.iter_commits(f"{start_commit.hexsha}..{end_commit.hexsha}", paths=path)
    )
    commit_map: Dict[str, List[str]] = {}
    for c in reversed(commits):  # Chronological order
        pr = ""
        for line in c.message.splitlines():
            if " odoo/odoo#" in line:
                pr = f"https://github.com/odoo/odoo/pull/{line.split(' odoo/odoo#')[1].strip()}"
        c_sha = c.hexsha[:10]
        author = c.author.name
        summary = c.message.splitlines()[0]
        comment_line = f"# Commit: {c_sha} | PR: {pr} | {author} | {summary}"

        for parent in c.parents:
            try:
                diffs = parent.diff(c, paths=path, create_patch=True)
                for d in diffs:
                    patch_str = d.diff.decode("utf-8", errors="ignore")
                    for line in patch_str.splitlines():
                        if (
                            line.startswith("+    def ")
                            or line.startswith("-    def ")
                            or line.startswith("+   def ")
                            or line.startswith("-   def ")
                        ) and "(" in line:
                            mname = line.split("def ")[1].split("(")[0].strip()
                            if mname not in commit_map:
                                commit_map[mname] = []
                            if comment_line not in commit_map[mname]:
                                commit_map[mname].append(comment_line)
            except Exception:
                pass
    return commit_map


def generate_method_signatures_diff(
    repo: git.Repo,
    addon: str,
    start_commit: git.Commit,
    end_commit: git.Commit,
    output_module_dir: str,
    commit_items: Optional[List[Dict[str, Any]]] = None,
    module_prefix: str = "addons/",
):
    """
    Generates method_signatures.patch for the given addon between start_commit and end_commit.
    Methods are grouped by Odoo model ID (_name / _inherit).
    Move-invariant: methods moved without signature change are ignored.
    """
    print(f"Extracting method signatures for {addon} at start/end commits...")
    methods_start = extract_methods_from_commit(start_commit, addon, module_prefix)
    methods_end = extract_methods_from_commit(end_commit, addon, module_prefix)

    all_models = sorted(set(methods_start.keys()) | set(methods_end.keys()))
    diff_data = {}
    total_added = 0
    total_removed = 0
    total_changed = 0

    for model_id in all_models:
        m_start = methods_start.get(model_id, {})
        m_end = methods_end.get(model_id, {})

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
            diff_data[model_id] = {
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

    print(f"Mapping commit SHAs for changed methods in {addon}...")
    commit_map = build_commit_map_from_repo(
        repo, addon, start_commit, end_commit, module_prefix
    )

    os.makedirs(output_module_dir, exist_ok=True)
    out_filepath = os.path.join(output_module_dir, "method_signatures.patch")

    with open(out_filepath, "w") as f:
        f.write(f"# Method Signatures Delta: {addon}\n")
        f.write(
            f"# Summary: {total_changed} modified, {total_added} added, {total_removed} removed\n\n"
        )

        for model_id, data in diff_data.items():
            f.write(f"[{model_id}]\n")
            for mname, orig_sig, new_sig in data["changed"]:
                cmts = commit_map.get(mname, [])
                for cmt in cmts:
                    f.write(f"{cmt}\n")
                f.write(f"- {orig_sig}\n")
                f.write(f"+ {new_sig}\n\n")

            for mname, sig in data["removed"]:
                cmts = commit_map.get(mname, [])
                for cmt in cmts:
                    f.write(f"{cmt}\n")
                f.write(f"- {sig}\n\n")

            for mname, sig in data["added"]:
                cmts = commit_map.get(mname, [])
                for cmt in cmts:
                    f.write(f"{cmt}\n")
                f.write(f"+ {sig}\n\n")

    print(
        f"Generated {out_filepath} ({total_changed} changed, {total_added} added, {total_removed} removed)"
    )
