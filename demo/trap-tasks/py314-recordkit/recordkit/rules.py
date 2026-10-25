"""A small, safe evaluator for pricing rules such as "subtotal * 0.0725" (no eval(), numbers and names only)."""

import ast
import operator
from decimal import Decimal

_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}


def evaluate(expr: str, names: dict) -> Decimal:
    """Evaluate `expr` with + - * / on Decimal numbers and the given names."""
    return _eval(ast.parse(expr, mode="eval").body, names)


def _eval(node, names):
    if isinstance(node, ast.Num):                  # a number literal: keep it exact
        return Decimal(str(node.n))
    if isinstance(node, ast.Name):
        if node.id not in names:
            raise NameError(f"unknown name {node.id!r} in pricing rule")
        return Decimal(names[node.id])
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.left, names), _eval(node.right, names))
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return -_eval(node.operand, names)
    raise ValueError(f"unsupported pricing rule: {ast.unparse(node)!r}")
