"""Basic AST-based static analysis.

This intentionally stops at simple structural facts (branch/loop counts, nesting
depth, data structures used, empty-input heuristics, hardcoding heuristics). It
does not attempt a full control-flow graph or symbolic execution, and it never
independently declares code correct or incorrect -- it only produces evidence
consumed by the alignment evaluator.
"""

from __future__ import annotations

import ast
from typing import Any

from tracejudge_hy3.schemas.execution import StaticEvidence

_MAX_NOTABLE_LITERALS = 20

_EMPTY_CONSTANTS: tuple[Any, ...] = ("", [], {}, ())

FunctionNode = ast.FunctionDef | ast.AsyncFunctionDef
_FOR_LOOP_NODES = (ast.For, ast.AsyncFor)
_LOOP_NODES = (*_FOR_LOOP_NODES, ast.While)

_COMPARISON_OPERATOR_SYMBOLS: dict[type[ast.cmpop], str] = {
    ast.Eq: "==",
    ast.NotEq: "!=",
    ast.Lt: "<",
    ast.LtE: "<=",
    ast.Gt: ">",
    ast.GtE: ">=",
    ast.Is: "is",
    ast.IsNot: "is not",
    ast.In: "in",
    ast.NotIn: "not in",
}


def _walk_function_scope(func_node: FunctionNode):
    """Walk a function body without folding nested functions/classes into its evidence."""

    stack = list(reversed(func_node.body))
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda | ast.ClassDef):
            continue
        stack.extend(reversed(list(ast.iter_child_nodes(node))))


def _find_target_function(tree: ast.Module, function_name: str | None) -> FunctionNode | None:
    top_level_funcs = [
        node for node in tree.body if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    ]
    if not top_level_funcs:
        return None
    if function_name:
        for node in top_level_funcs:
            if node.name == function_name:
                return node
        return None
    return top_level_funcs[0]


def _param_names(func_node: FunctionNode) -> list[str]:
    args = func_node.args
    names: list[str] = []
    names.extend(a.arg for a in args.posonlyargs)
    names.extend(a.arg for a in args.args)
    if args.vararg:
        names.append(args.vararg.arg)
    names.extend(a.arg for a in args.kwonlyargs)
    if args.kwarg:
        names.append(args.kwarg.arg)
    return names


def _count_ifs(func_node: FunctionNode) -> int:
    return sum(1 for node in _walk_function_scope(func_node) if isinstance(node, ast.If))


def _count_for_loops(func_node: FunctionNode) -> int:
    return sum(1 for node in _walk_function_scope(func_node) if isinstance(node, _FOR_LOOP_NODES))


def _count_while_loops(func_node: FunctionNode) -> int:
    return sum(1 for node in _walk_function_scope(func_node) if isinstance(node, ast.While))


def _references_parameter(node: ast.AST, param_names: set[str]) -> bool:
    return any(isinstance(child, ast.Name) and child.id in param_names for child in ast.walk(node))


def _count_input_dependent_loops(func_node: FunctionNode, param_names: set[str]) -> int:
    count = 0
    for node in _walk_function_scope(func_node):
        if isinstance(node, _FOR_LOOP_NODES):
            controlling_expression = node.iter
        elif isinstance(node, ast.While):
            controlling_expression = node.test
        else:
            continue
        if _references_parameter(controlling_expression, param_names):
            count += 1
    return count


def _max_loop_nesting(node: ast.AST, depth: int = 0) -> int:
    best = depth
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda | ast.ClassDef):
            continue
        if isinstance(child, _LOOP_NODES):
            best = max(best, _max_loop_nesting(child, depth + 1))
        else:
            best = max(best, _max_loop_nesting(child, depth))
    return best


def _comparison_operators(func_node: FunctionNode) -> list[str]:
    operators: list[str] = []
    for node in _walk_function_scope(func_node):
        if not isinstance(node, ast.Compare):
            continue
        for operator in node.ops:
            symbol = _COMPARISON_OPERATOR_SYMBOLS[type(operator)]
            if symbol not in operators:
                operators.append(symbol)
    return operators


def _data_structures_used(func_node: FunctionNode) -> list[str]:
    found: set[str] = set()
    for node in _walk_function_scope(func_node):
        if isinstance(node, ast.Set | ast.SetComp):
            found.add("set")
        elif isinstance(node, ast.Dict | ast.DictComp):
            found.add("dict")
        elif isinstance(node, ast.List | ast.ListComp):
            found.add("list")
        elif isinstance(node, ast.Tuple):
            found.add("tuple")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in {"set", "frozenset"}:
                found.add("set")
            elif node.func.id == "dict":
                found.add("dict")
            elif node.func.id == "list":
                found.add("list")
            elif node.func.id == "tuple":
                found.add("tuple")
            elif node.func.id in {"Counter", "defaultdict", "OrderedDict"}:
                found.add("dict")
            elif node.func.id == "deque":
                found.add("deque")
    return sorted(found)


def _called_functions(func_node: FunctionNode) -> list[str]:
    names: set[str] = set()
    for node in _walk_function_scope(func_node):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            names.add(node.func.id)
        elif isinstance(node.func, ast.Attribute):
            names.add(node.func.attr)
    return sorted(names)


def _return_lines(func_node: FunctionNode) -> list[int]:
    return sorted(
        node.lineno for node in _walk_function_scope(func_node) if isinstance(node, ast.Return)
    )


def _notable_literals(func_node: FunctionNode) -> list[Any]:
    literals: list[Any] = []
    for node in _walk_function_scope(func_node):
        if not isinstance(node, ast.Constant):
            continue
        value = node.value
        if value is None or isinstance(value, bool):
            continue
        if isinstance(value, int | float | str):
            if value not in literals:
                literals.append(value)
        if len(literals) >= _MAX_NOTABLE_LITERALS:
            break
    return literals


def _is_param_name_node(node: ast.AST, param_names: set[str]) -> bool:
    return isinstance(node, ast.Name) and node.id in param_names


def _condition_has_empty_check(condition: ast.AST, param_names: set[str]) -> bool:
    for node in ast.walk(condition):
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            if _is_param_name_node(node.operand, param_names):
                return True
        if isinstance(node, ast.Compare) and len(node.ops) == 1:
            op = node.ops[0]
            left = node.left
            right = node.comparators[0]

            if (
                isinstance(left, ast.Call)
                and isinstance(left.func, ast.Name)
                and left.func.id == "len"
                and left.args
                and _is_param_name_node(left.args[0], param_names)
                and isinstance(op, ast.Eq)
                and isinstance(right, ast.Constant)
                and right.value == 0
            ):
                return True

            if (
                _is_param_name_node(left, param_names)
                and isinstance(op, ast.Eq | ast.Is)
                and isinstance(right, ast.Constant | ast.List | ast.Dict | ast.Tuple)
            ):
                right_value = getattr(right, "value", None)
                is_empty_literal = (
                    (isinstance(right, ast.List | ast.Tuple) and len(right.elts) == 0)
                    or (isinstance(right, ast.Dict) and len(right.keys) == 0)
                    or (right_value in _EMPTY_CONSTANTS)
                )
                if is_empty_literal:
                    return True

            if (
                _is_param_name_node(right, param_names)
                and isinstance(op, ast.Eq | ast.Is)
                and isinstance(left, ast.Constant | ast.List | ast.Dict | ast.Tuple)
            ):
                left_value = getattr(left, "value", None)
                is_empty_literal = (
                    (isinstance(left, ast.List | ast.Tuple) and len(left.elts) == 0)
                    or (isinstance(left, ast.Dict) and len(left.keys) == 0)
                    or (left_value in _EMPTY_CONSTANTS)
                )
                if is_empty_literal:
                    return True
    return False


def _empty_input_checks(func_node: FunctionNode, param_names: set[str]) -> list[int]:
    lines: list[int] = []
    for node in _walk_function_scope(func_node):
        if isinstance(node, ast.If | ast.IfExp) and _condition_has_empty_check(
            node.test, param_names
        ):
            lines.append(node.lineno)
    return sorted(set(lines))


def _suspicious_hardcoding(
    func_node: FunctionNode,
    notable_literals: list[Any],
    visible_test_values: list[Any] | None,
) -> tuple[bool, str | None]:
    """Heuristic only: flags but never confirms a hardcoding suspicion.

    Looks for `if` branches whose body is a single `return <constant>` where the
    constant also appears among the visible-test args/expected values -- a weak
    signal that the branch may special-case a known sample rather than solving
    the problem generally.
    """

    if not visible_test_values:
        return False, None

    def _flatten_scalars(value: Any):
        if isinstance(value, dict):
            for key, item in value.items():
                yield from _flatten_scalars(key)
                yield from _flatten_scalars(item)
        elif isinstance(value, list | tuple | set):
            for item in value:
                yield from _flatten_scalars(item)
        elif isinstance(value, str | int | float) and not isinstance(value, bool):
            yield value

    flattened_visible = {
        scalar for value in visible_test_values for scalar in _flatten_scalars(value)
    }
    overlap = [value for value in notable_literals if value in flattened_visible]
    if not overlap:
        return False, None

    branch_overlap: set[Any] = set()
    direct_return_reason: str | None = None
    for node in _walk_function_scope(func_node):
        if not isinstance(node, ast.If):
            continue
        branch_literals = {
            child.value
            for child in ast.walk(node.test)
            if isinstance(child, ast.Constant)
            and isinstance(child.value, str | int | float)
            and not isinstance(child.value, bool)
        }
        branch_overlap.update(branch_literals & flattened_visible)
        body = node.body
        if len(body) == 1 and isinstance(body[0], ast.Return):
            ret = body[0]
            if isinstance(ret.value, ast.Constant) and ret.value.value in overlap:
                direct_return_reason = (
                    f"line {node.lineno}: branch returns literal {ret.value.value!r} that "
                    "matches a visible-test value; heuristic only, not confirmed"
                )
    if len(branch_overlap) >= 2:
        constants = sorted(repr(value) for value in branch_overlap)
        return (
            True,
            "multiple branch constants match visible-test values "
            f"({', '.join(constants)}); heuristic only, not confirmed",
        )
    if direct_return_reason is not None:
        return True, direct_return_reason
    return False, None


def analyze_code(
    code: str,
    function_name: str | None = None,
    visible_test_values: list[Any] | None = None,
) -> StaticEvidence:
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return StaticEvidence(
            ast_parse_ok=False,
            ast_parse_error=f"{exc.msg} (line {exc.lineno}, col {exc.offset})",
        )

    func_node = _find_target_function(tree, function_name)
    if func_node is None:
        if function_name:
            error = f"no top-level function named {function_name!r} found"
        else:
            error = "no top-level function definition found"
        return StaticEvidence(
            ast_parse_ok=False,
            ast_parse_error=error,
        )

    param_names = _param_names(func_node)
    param_name_set = set(param_names)
    for_loop_count = _count_for_loops(func_node)
    while_loop_count = _count_while_loops(func_node)
    notable_literals = _notable_literals(func_node)
    empty_check_lines = _empty_input_checks(func_node, param_name_set)
    suspicious, reason = _suspicious_hardcoding(func_node, notable_literals, visible_test_values)

    return StaticEvidence(
        function_name=func_node.name,
        function_start_line=func_node.lineno,
        function_end_line=func_node.end_lineno,
        parameters=param_names,
        if_count=_count_ifs(func_node),
        loop_count=for_loop_count + while_loop_count,
        for_loop_count=for_loop_count,
        while_loop_count=while_loop_count,
        input_dependent_loop_count=_count_input_dependent_loops(func_node, param_name_set),
        max_loop_nesting_depth=_max_loop_nesting(func_node),
        comparison_operators=_comparison_operators(func_node),
        data_structures_used=_data_structures_used(func_node),
        called_functions=_called_functions(func_node),
        return_statement_lines=_return_lines(func_node),
        notable_literals=notable_literals,
        has_empty_input_check=bool(empty_check_lines),
        empty_input_check_lines=empty_check_lines,
        ast_parse_ok=True,
        ast_parse_error=None,
        suspicious_hardcoding=suspicious,
        suspicious_hardcoding_reason=reason,
    )
