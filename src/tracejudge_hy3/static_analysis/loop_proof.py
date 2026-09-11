"""Narrow structural witnesses, not a general complexity analyser.

Abstain on callbacks, early exits, guards, aliases and input mutation. Only
unconditional full scans of an unchanged parameter are used by hard rules.
"""

from __future__ import annotations

import ast


def simple_full_scans(code: str, function_name: str | None) -> list[tuple[int, bool]]:
    """Return (line, indexed) scans for a deliberately small Python subset.

    ``indexed`` means exactly range(len(parameter)); two such scans restart the
    same index range, unlike two iterations of a possibly shared iterator.
    No claim is made about arbitrary Python objects or general runtime cost.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []
    functions = [n for n in tree.body if isinstance(n, ast.FunctionDef)]
    if len(tree.body) != 1 or len(functions) != 1:
        return []
    fn = functions[0]
    if fn.name != function_name or fn.name in {"range", "len"} or fn.decorator_list:
        return []
    params = [*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs]
    if len(params) != 1 or fn.args.vararg or fn.args.kwarg:
        return []
    parameter = params[0].arg
    if parameter in {"range", "len"}:
        return []
    # A final return is allowed; any earlier/conditional exit is not.
    body = fn.body[:-1] if fn.body and isinstance(fn.body[-1], ast.Return) else fn.body
    nodes = [child for statement in body for child in ast.walk(statement)]
    unsafe = (
        ast.Break,
        ast.Continue,
        ast.Return,
        ast.Raise,
        ast.Yield,
        ast.YieldFrom,
        ast.Await,
        ast.If,
        ast.IfExp,
        ast.While,
        ast.Try,
        ast.TryStar,
        ast.With,
        ast.Match,
        ast.FunctionDef,
        ast.AsyncFunctionDef,
        ast.Lambda,
        ast.ClassDef,
        ast.Import,
        ast.ImportFrom,
        ast.Global,
        ast.Nonlocal,
        ast.Delete,
        ast.NamedExpr,
        ast.ListComp,
        ast.SetComp,
        ast.DictComp,
        ast.GeneratorExp,
    )
    for node in nodes:
        if isinstance(node, unsafe):
            return []
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            if node.id in {parameter, "range", "len"}:
                return []
        if isinstance(node, ast.Attribute | ast.Subscript) and isinstance(node.ctx, ast.Store):
            return []
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in {"range", "len"}:
                return []
    scans = []
    for node in nodes:
        if not isinstance(node, ast.For):
            continue
        iterator = node.iter
        if isinstance(iterator, ast.Name) and iterator.id == parameter:
            scans.append((node.lineno, False))
        elif (
            isinstance(iterator, ast.Call)
            and isinstance(iterator.func, ast.Name)
            and iterator.func.id == "range"
            and len(iterator.args) == 1
            and not iterator.keywords
        ):
            length = iterator.args[0]
            if (
                isinstance(length, ast.Call)
                and isinstance(length.func, ast.Name)
                and length.func.id == "len"
                and len(length.args) == 1
                and not length.keywords
                and isinstance(length.args[0], ast.Name)
                and length.args[0].id == parameter
            ):
                scans.append((node.lineno, True))
    # Scans nested in a fixed loop can be unreachable (e.g. range(0)).
    # Require every enclosing loop to be a recognised full scan as well.
    known = {line for line, _ in scans}
    for node in nodes:
        if isinstance(node, ast.For) and node.lineno not in known:
            return []
    return scans
