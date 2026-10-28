"""
Safe evaluation of user-entered calculation formulas.

Formulas are plain arithmetic expressions such as

    (V_sample - V_blank) * normality * 56.1 / mass_g

They are parsed with Python's ``ast`` module and only a small whitelist of
node types is evaluated, so a formula can never run arbitrary code.
"""

import ast
import math
import operator

FUNCTIONS = {
    "abs": abs,
    "min": min,
    "max": max,
    "round": round,
    "sqrt": math.sqrt,
    "log": math.log,
    "log10": math.log10,
    "exp": math.exp,
}

CONSTANTS = {
    "pi": math.pi,
    "e": math.e,
}

_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
    ast.Mod: operator.mod,
}

_UNARY_OPS = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}

MAX_EXPONENT = 100


class FormulaError(ValueError):
    pass


def _parse(expression):
    if not expression or not expression.strip():
        raise FormulaError("Formula is empty.")
    try:
        return ast.parse(expression.strip(), mode="eval")
    except SyntaxError as exc:
        raise FormulaError(f"Formula is not valid: {exc.msg}.") from exc


def _names_used(tree):
    """Variable names in the expression (function names excluded)."""
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    return {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)} - called


def _check_nodes(tree):
    for node in ast.walk(tree):
        if isinstance(node, (ast.Expression, ast.Load, ast.Name)):
            continue
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
                raise FormulaError("Only numbers are allowed as constants.")
            continue
        if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
            continue
        if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
            continue
        if type(node) in _BIN_OPS or type(node) in _UNARY_OPS:
            continue
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in FUNCTIONS:
                allowed = ", ".join(sorted(FUNCTIONS))
                raise FormulaError(f"Unknown function. Allowed functions: {allowed}.")
            if node.keywords:
                raise FormulaError("Functions take plain arguments only.")
            continue
        raise FormulaError(f"'{ast.unparse(node)}' is not allowed in a formula.")


def validate(expression, input_names):
    """Raise FormulaError if the expression is unsafe or uses undeclared inputs."""
    tree = _parse(expression)
    _check_nodes(tree)
    unknown = _names_used(tree) - set(input_names) - set(CONSTANTS)
    if unknown:
        raise FormulaError(
            "Formula uses names that are not declared as inputs: "
            + ", ".join(sorted(unknown))
            + "."
        )
    return tree


def _eval(node, values):
    if isinstance(node, ast.Expression):
        return _eval(node.body, values)
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id in values:
            return values[node.id]
        return CONSTANTS[node.id]
    if isinstance(node, ast.UnaryOp):
        return _UNARY_OPS[type(node.op)](_eval(node.operand, values))
    if isinstance(node, ast.BinOp):
        left = _eval(node.left, values)
        right = _eval(node.right, values)
        if isinstance(node.op, ast.Pow) and abs(right) > MAX_EXPONENT:
            raise FormulaError("Exponent is too large.")
        return _BIN_OPS[type(node.op)](left, right)
    if isinstance(node, ast.Call):
        args = [_eval(arg, values) for arg in node.args]
        return FUNCTIONS[node.func.id](*args)
    raise FormulaError("Unsupported expression.")


def evaluate(expression, values):
    """Evaluate the expression with the given {input_name: number} values."""
    numbers = {}
    for name, raw in values.items():
        try:
            numbers[name] = float(raw)
        except (TypeError, ValueError) as exc:
            raise FormulaError(f"'{name}' must be a number.") from exc
    tree = validate(expression, numbers.keys())
    missing = _names_used(tree) - set(numbers) - set(CONSTANTS)
    if missing:
        raise FormulaError("Missing values for: " + ", ".join(sorted(missing)) + ".")
    try:
        result = _eval(tree, numbers)
    except ZeroDivisionError as exc:
        raise FormulaError("Division by zero.") from exc
    except (ValueError, OverflowError, TypeError) as exc:
        raise FormulaError(f"Calculation failed: {exc}.") from exc
    if isinstance(result, complex) or not math.isfinite(result):
        raise FormulaError("Calculation did not give a real, finite number.")
    return float(result)
