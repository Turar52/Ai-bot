# server.py — ULTRA MATH GENIUS (FastAPI + SSE)
# ============================================================
# Установка:
#   pip install fastapi uvicorn python-multipart
# Запуск:
#   uvicorn server:app --reload --host 0.0.0.0 --port 8000
# ============================================================

from __future__ import annotations

import asyncio
import ast
import base64
import json
import math
import re
import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal, getcontext
from fractions import Fraction
from typing import Any, Dict, Optional, List, Tuple

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

getcontext().prec = 50  # больше точности для Decimal

# ============================================================
# Utils
# ============================================================

def now_ms() -> int:
    return int(time.time() * 1000)

def sse_pack(obj: Dict[str, Any]) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"

def safe_trim(s: str, n: int) -> str:
    s = (s or "").strip()
    return s[:n] + ("…" if len(s) > n else "")

def parse_data_url(data_url: str) -> Optional[bytes]:
    if not data_url or not isinstance(data_url, str):
        return None
    m = re.match(r"^data:(image\/[a-zA-Z0-9.+-]+);base64,(.*)$", data_url, re.DOTALL)
    if not m:
        return None
    try:
        return base64.b64decode(m.group(2), validate=False)
    except Exception:
        return None

# ============================================================
# SSE Jobs
# ============================================================

@dataclass
class Job:
    job_id: str
    created_at_ms: int
    queue: "asyncio.Queue[Dict[str, Any]]" = field(default_factory=asyncio.Queue)
    done: bool = False

JOBS: Dict[str, Job] = {}
JOBS_LOCK = asyncio.Lock()

async def create_job() -> Job:
    job_id = uuid.uuid4().hex
    job = Job(job_id=job_id, created_at_ms=now_ms())
    async with JOBS_LOCK:
        JOBS[job_id] = job
    return job

async def get_job(job_id: str) -> Optional[Job]:
    async with JOBS_LOCK:
        return JOBS.get(job_id)

async def cleanup_jobs(max_age_sec: int = 60 * 15) -> None:
    cutoff = now_ms() - max_age_sec * 1000
    async with JOBS_LOCK:
        old = [jid for jid, j in JOBS.items() if j.created_at_ms < cutoff]
        for jid in old:
            del JOBS[jid]

# ============================================================
# ULTRA MATH ENGINE
# ============================================================

CALC_PREFIXES = ("/calc", "calc:", "кальк:", "калькулятор:", "дробь:", "дроби:", "реши:", "реши")
STEP_PREFIXES = ("/steps", "steps:", "шаги:", "покажи шаги", "решение:")

# Русские слова -> мат. токены
WORD_REPL = [
    (r"\bделить\s+на\b", "/"),
    (r"\bразделить\s+на\b", "/"),
    (r"\bумножить\s+на\b", "*"),
    (r"\bплюс\b", "+"),
    (r"\bминус\b", "-"),
    (r"\bумножить\b", "*"),
    (r"\bумножь\b", "*"),
    (r"\bделить\b", "/"),
    (r"\bразделить\b", "/"),
    (r"\bв\s+степени\b", "^"),
    (r"\bстепень\b", "^"),
    (r"\bквадрат\b", "^2"),
    (r"\bкуб\b", "^3"),
    (r"\bкорень\s+из\b", "sqrt("),
    (r"\bкорень\b", "sqrt("),
    (r"\bмодуль\b", "abs("),
    (r"\bпроцентов\b", "%"),
    (r"\bпроцента\b", "%"),
    (r"\bпроцент\b", "%"),
    (r"\bпи\b", "pi"),
    (r"\bлог\b", "log("),
    (r"\bln\b", "ln("),
    (r"\bсин\b", "sin("),
    (r"\bкос\b", "cos("),
    (r"\bтан\b", "tan("),
]

def words_to_math(text: str) -> str:
    s = (text or "").lower().strip()
    s = s.replace("×", "*").replace("÷", "/").replace(",", ".").replace("π", "pi")
    s = re.sub(r"\s+", " ", s)
    for pat, rep in WORD_REPL:
        s = re.sub(pat, rep, s, flags=re.IGNORECASE)

    # мягкое автозакрытие функций (если скобок не хватает)
    if any(fn in s for fn in ("sqrt(", "abs(", "sin(", "cos(", "tan(", "log(", "ln(")):
        if s.count("(") > s.count(")"):
            s += ")"
    return s

def _normalize_expr(expr: str) -> str:
    expr = (expr or "").strip()
    expr = expr.replace("×", "*").replace("÷", "/").replace(",", ".").replace("π", "pi")
    expr = re.sub(r"\s+", " ", expr)
    # разрешаем только safe chars + буквы для функций/переменных x,y
    expr = re.sub(r"[^0-9a-zA-Zx y\.\+\-\*\/\(\),%!\^\=\s]", "", expr).strip()
    return expr

def _rewrite_power(expr: str) -> str:
    return expr.replace("^", "**")

def _tokenize_factorial(expr: str) -> str:
    # 5! -> fact(5)
    expr = re.sub(r"(\d+(?:\.\d+)?)\s*!", r"fact(\1)", expr)
    # (..)! -> fact((..)) (упрощённо)
    while True:
        m = re.search(r"(\([^()]+\))\s*!", expr)
        if not m:
            break
        inner = m.group(1)
        expr = expr[:m.start()] + f"fact({inner})" + expr[m.end():]
    return expr

def _frac_to_decimal_str(fr: Fraction) -> str:
    d = Decimal(fr.numerator) / Decimal(fr.denominator)
    s = format(d.normalize(), "f")
    if len(s) > 80:
        s = f"{d:.16f}".rstrip("0").rstrip(".")
    return s

def _pretty_fraction(fr: Fraction) -> str:
    if fr.denominator == 1:
        return str(fr.numerator)
    return f"{fr.numerator}/{fr.denominator}"

def _is_int(fr: Fraction) -> bool:
    return fr.denominator == 1

def _as_int(fr: Fraction) -> int:
    if fr.denominator != 1:
        raise ValueError("not int")
    return int(fr.numerator)

def _to_float(fr: Fraction) -> float:
    return float(fr.numerator) / float(fr.denominator)

def _lcm(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return abs(a * b) // math.gcd(a, b)

def _fact(n: int) -> int:
    if n < 0:
        raise ValueError("factorial negative")
    if n > 5000:
        raise ValueError("factorial too big")
    return math.factorial(n)

# --- проценты: 20% от 150 / 150 + 20% / 150 - 20% / 20% ---
PERCENT_OF_RE = re.compile(r"^\s*(.+?)\s*%\s*(?:от|of)\s*(.+?)\s*$", re.IGNORECASE)
PERCENT_PLUS_RE = re.compile(r"^\s*(.+?)\s*([+\-])\s*(.+?)\s*%\s*$", re.IGNORECASE)
PERCENT_SIMPLE_RE = re.compile(r"^\s*(.+?)\s*%\s*$", re.IGNORECASE)

def _try_percent_patterns(raw: str) -> Optional[Fraction]:
    m = PERCENT_OF_RE.match(raw)
    if m:
        p = eval_expr(_normalize_expr(m.group(1)))
        base = eval_expr(_normalize_expr(m.group(2)))
        if p is None or base is None:
            return None
        return base * p / 100

    m = PERCENT_PLUS_RE.match(raw)
    if m:
        base = eval_expr(_normalize_expr(m.group(1)))
        op = m.group(2)
        p = eval_expr(_normalize_expr(m.group(3)))
        if base is None or p is None:
            return None
        delta = base * p / 100
        return base + delta if op == "+" else base - delta

    m = PERCENT_SIMPLE_RE.match(raw)
    if m:
        p = eval_expr(_normalize_expr(m.group(1)))
        if p is None:
            return None
        return p / 100

    return None

# --- Константы ---
ALLOWED_CONSTS = {
    "pi": Fraction(Decimal(str(math.pi))),
    "e": Fraction(Decimal(str(math.e))),
}

# --- Safe AST evaluator ---
class SafeEval(ast.NodeVisitor):
    def visit_Expression(self, node: ast.Expression) -> Fraction:
        return self.visit(node.body)

    def visit_Constant(self, node: ast.Constant) -> Fraction:
        if isinstance(node.value, int):
            return Fraction(node.value, 1)
        if isinstance(node.value, float):
            return Fraction(Decimal(str(node.value)))
        raise ValueError("bad const")

    def visit_Num(self, node: ast.Num) -> Fraction:
        return self.visit_Constant(ast.Constant(node.n))

    def visit_Name(self, node: ast.Name) -> Fraction:
        name = node.id.lower()
        if name in ALLOWED_CONSTS:
            return ALLOWED_CONSTS[name]
        # переменные x,y не разрешаем в обычном выражении (они только для уравнений)
        raise ValueError("unknown name")

    def visit_UnaryOp(self, node: ast.UnaryOp) -> Fraction:
        v = self.visit(node.operand)
        if isinstance(node.op, ast.UAdd):
            return v
        if isinstance(node.op, ast.USub):
            return -v
        raise ValueError("bad unary")

    def visit_BinOp(self, node: ast.BinOp) -> Fraction:
        a = self.visit(node.left)
        b = self.visit(node.right)
        if isinstance(node.op, ast.Add):
            return a + b
        if isinstance(node.op, ast.Sub):
            return a - b
        if isinstance(node.op, ast.Mult):
            return a * b
        if isinstance(node.op, ast.Div):
            if b == 0:
                raise ZeroDivisionError("div0")
            return a / b
        if isinstance(node.op, ast.Pow):
            if not _is_int(b):
                raise ValueError("pow int only")
            e = _as_int(b)
            if abs(e) > 4000:
                raise ValueError("pow too large")
            return a ** e
        raise ValueError("bad op")

    def visit_Call(self, node: ast.Call) -> Fraction:
        if not isinstance(node.func, ast.Name):
            raise ValueError("bad call")
        fname = node.func.id.lower()
        args = [self.visit(a) for a in node.args]

        if fname == "abs":
            if len(args) != 1:
                raise ValueError("abs(x)")
            return abs(args[0])

        if fname == "sqrt":
            if len(args) != 1:
                raise ValueError("sqrt(x)")
            x = args[0]
            if x < 0:
                raise ValueError("sqrt negative")
            num, den = x.numerator, x.denominator
            rn, rd = int(math.isqrt(num)), int(math.isqrt(den))
            if rn * rn == num and rd * rd == den:
                return Fraction(rn, rd)
            val = Decimal(num) / Decimal(den)
            approx = Decimal(str(math.sqrt(float(val))))
            return Fraction(approx)

        if fname in ("sin", "cos", "tan"):
            if len(args) != 1:
                raise ValueError(f"{fname}(x)")
            x = _to_float(args[0])
            if fname == "sin":
                return Fraction(Decimal(str(math.sin(x))))
            if fname == "cos":
                return Fraction(Decimal(str(math.cos(x))))
            return Fraction(Decimal(str(math.tan(x))))

        if fname in ("ln", "log"):
            # ln(x) / log(x, base?) (если 2 арг — лог по основанию)
            if len(args) == 1:
                x = _to_float(args[0])
                if x <= 0:
                    raise ValueError("log domain")
                if fname == "ln":
                    return Fraction(Decimal(str(math.log(x))))
                return Fraction(Decimal(str(math.log10(x))))
            if len(args) == 2 and fname == "log":
                x = _to_float(args[0])
                base = _to_float(args[1])
                if x <= 0 or base <= 0 or base == 1:
                    raise ValueError("log domain")
                return Fraction(Decimal(str(math.log(x, base))))
            raise ValueError("log usage")

        if fname == "fact":
            if len(args) != 1:
                raise ValueError("fact(n)")
            if not _is_int(args[0]):
                raise ValueError("fact int only")
            return Fraction(_fact(_as_int(args[0])), 1)

        if fname == "gcd":
            if len(args) != 2:
                raise ValueError("gcd(a,b)")
            if not _is_int(args[0]) or not _is_int(args[1]):
                raise ValueError("gcd int only")
            return Fraction(math.gcd(_as_int(args[0]), _as_int(args[1])), 1)

        if fname == "lcm":
            if len(args) != 2:
                raise ValueError("lcm(a,b)")
            if not _is_int(args[0]) or not _is_int(args[1]):
                raise ValueError("lcm int only")
            return Fraction(_lcm(_as_int(args[0]), _as_int(args[1])), 1)

        raise ValueError("func not allowed")

    def generic_visit(self, node):
        raise ValueError(f"unsupported: {type(node).__name__}")

def eval_expr(expr: str) -> Optional[Fraction]:
    try:
        expr = (expr or "").strip()
        if not expr:
            return None
        expr = _rewrite_power(expr)
        expr = _tokenize_factorial(expr)
        if len(expr) > 500:
            return None
        tree = ast.parse(expr, mode="eval")
        return SafeEval().visit(tree)
    except ZeroDivisionError:
        raise
    except Exception:
        return None

def _looks_like_math(text: str) -> bool:
    if not text:
        return False
    if not re.search(r"\d", text):
        return False
    if re.search(r"[+\-*/^()=]", text):
        return True
    low = text.lower()
    return any(h in low for h in ("sqrt", "abs", "sin", "cos", "tan", "gcd", "lcm", "log", "ln", "%", "!", "pi", "e"))

# --- extract candidates from any text ---
MATH_CHUNK_RE = re.compile(r"[0-9a-zA-Zx y\.\+\-\*\/\(\),%!\^\=\s]+", re.IGNORECASE)

def extract_candidates(raw_text: str) -> List[str]:
    s = words_to_math(raw_text)
    chunks = MATH_CHUNK_RE.findall(s)
    out: List[str] = []
    for ch in chunks:
        expr = _normalize_expr(ch).strip()
        if not expr:
            continue
        if not _looks_like_math(expr):
            continue
        # одиночное число НЕ считаем, чтобы "айфон 15" не считался
        if re.fullmatch(r"\d+(?:\.\d+)?", expr):
            continue
        out.append(expr)
    out.sort(key=len, reverse=True)
    return out

# ============================================================
# EQUATIONS (x, y)
# ============================================================

def _parse_linear(expr: str, var: str) -> Optional[Tuple[Fraction, Fraction]]:
    """
    Возвращает (a, b) для выражения вида a*var + b
    Поддержка: + - * / ** (степень только 1 для var)
    """
    expr = expr.strip()
    if not expr:
        return None

    expr = expr.replace("^", "**")
    expr = _tokenize_factorial(expr)

    try:
        tree = ast.parse(expr, mode="eval")
    except Exception:
        return None

    def walk(node) -> Tuple[Fraction, Fraction]:
        # returns (a,b)
        if isinstance(node, ast.Expression):
            return walk(node.body)

        if isinstance(node, ast.Constant):
            if isinstance(node.value, int):
                return Fraction(node.value, 1), Fraction(0, 1)  # <-- not used this way
            if isinstance(node.value, float):
                return Fraction(Decimal(str(node.value))), Fraction(0, 1)
            raise ValueError

        if isinstance(node, ast.Num):
            v = node.n
            if isinstance(v, int):
                return Fraction(v, 1), Fraction(0, 1)
            return Fraction(Decimal(str(v))), Fraction(0, 1)

        if isinstance(node, ast.Name):
            name = node.id.lower()
            if name == var:
                return Fraction(1, 1), Fraction(0, 1)
            if name in ALLOWED_CONSTS:
                return Fraction(0, 1), ALLOWED_CONSTS[name]
            raise ValueError

        if isinstance(node, ast.UnaryOp):
            a, b = walk(node.operand)
            if isinstance(node.op, ast.UAdd):
                return a, b
            if isinstance(node.op, ast.USub):
                return -a, -b
            raise ValueError

        if isinstance(node, ast.BinOp):
            if isinstance(node.op, ast.Add):
                a1, b1 = walk(node.left)
                a2, b2 = walk(node.right)
                return a1 + a2, b1 + b2

            if isinstance(node.op, ast.Sub):
                a1, b1 = walk(node.left)
                a2, b2 = walk(node.right)
                return a1 - a2, b1 - b2

            if isinstance(node.op, ast.Mult):
                a1, b1 = walk(node.left)
                a2, b2 = walk(node.right)
                # (a1 x + b1) * (a2 x + b2) должно быть линейным => один из a должен быть 0
                if a1 != 0 and a2 != 0:
                    raise ValueError
                return (a1 * b2 + a2 * b1), (b1 * b2)

            if isinstance(node.op, ast.Div):
                a1, b1 = walk(node.left)
                a2, b2 = walk(node.right)
                # делить можно только на константу (a2==0)
                if a2 != 0:
                    raise ValueError
                if b2 == 0:
                    raise ZeroDivisionError
                return a1 / b2, b1 / b2

            if isinstance(node.op, ast.Pow):
                # разрешаем x**1 или константа**константа
                a1, b1 = walk(node.left)
                a2, b2 = walk(node.right)
                # right must be constant integer
                if a2 != 0:
                    raise ValueError
                if b2.denominator != 1:
                    raise ValueError
                exp = int(b2.numerator)
                # (linear)**1 ok
                if exp == 1:
                    return a1, b1
                # (constant)**exp ok if a1==0
                if a1 == 0:
                    # constant power
                    if exp < 0:
                        # 1/(b1**abs(exp))
                        if b1 == 0:
                            raise ZeroDivisionError
                        return Fraction(0, 1), Fraction(1, 1) / (b1 ** abs(exp))
                    return Fraction(0, 1), b1 ** exp
                raise ValueError

        if isinstance(node, ast.Call):
            # функции допускаем только если они константные (не зависят от x)
            if not isinstance(node.func, ast.Name):
                raise ValueError
            fname = node.func.id.lower()
            args = [walk(a) for a in node.args]
            if any(a != 0 for a, _ in args):
                raise ValueError
            # все аргументы константы => можно посчитать через eval_expr строкой
            # но безопаснее: соберём выражение обратно нельзя, поэтому просто запретим
            raise ValueError

        raise ValueError

    try:
        # walk returns (a,b) but for constants we returned weird in Constant;
        # fix constant node behavior: return (0,const)
        def walk_fixed(node):
            if isinstance(node, ast.Expression):
                return walk_fixed(node.body)
            if isinstance(node, (ast.Constant, ast.Num)):
                v = node.value if isinstance(node, ast.Constant) else node.n
                if isinstance(v, int):
                    return Fraction(0, 1), Fraction(v, 1)
                if isinstance(v, float):
                    return Fraction(0, 1), Fraction(Decimal(str(v)))
                raise ValueError
            if isinstance(node, ast.Name):
                name = node.id.lower()
                if name == var:
                    return Fraction(1, 1), Fraction(0, 1)
                if name in ALLOWED_CONSTS:
                    return Fraction(0, 1), ALLOWED_CONSTS[name]
                raise ValueError
            if isinstance(node, ast.UnaryOp):
                a, b = walk_fixed(node.operand)
                if isinstance(node.op, ast.UAdd):
                    return a, b
                if isinstance(node.op, ast.USub):
                    return -a, -b
                raise ValueError
            if isinstance(node, ast.BinOp):
                if isinstance(node.op, ast.Add):
                    a1, b1 = walk_fixed(node.left)
                    a2, b2 = walk_fixed(node.right)
                    return a1 + a2, b1 + b2
                if isinstance(node.op, ast.Sub):
                    a1, b1 = walk_fixed(node.left)
                    a2, b2 = walk_fixed(node.right)
                    return a1 - a2, b1 - b2
                if isinstance(node.op, ast.Mult):
                    a1, b1 = walk_fixed(node.left)
                    a2, b2 = walk_fixed(node.right)
                    if a1 != 0 and a2 != 0:
                        raise ValueError
                    return (a1 * b2 + a2 * b1), (b1 * b2)
                if isinstance(node.op, ast.Div):
                    a1, b1 = walk_fixed(node.left)
                    a2, b2 = walk_fixed(node.right)
                    if a2 != 0:
                        raise ValueError
                    if b2 == 0:
                        raise ZeroDivisionError
                    return a1 / b2, b1 / b2
                if isinstance(node.op, ast.Pow):
                    a1, b1 = walk_fixed(node.left)
                    a2, b2 = walk_fixed(node.right)
                    if a2 != 0:
                        raise ValueError
                    if b2.denominator != 1:
                        raise ValueError
                    exp = int(b2.numerator)
                    if exp == 1:
                        return a1, b1
                    if a1 == 0:
                        if exp < 0:
                            if b1 == 0:
                                raise ZeroDivisionError
                            return Fraction(0, 1), Fraction(1, 1) / (b1 ** abs(exp))
                        return Fraction(0, 1), b1 ** exp
                    raise ValueError
                raise ValueError
            raise ValueError

        a, b = walk_fixed(tree)
        return a, b
    except Exception:
        return None

def solve_linear_equation(expr: str, var: str = "x") -> Optional[Dict[str, str]]:
    """
    Решает линейное уравнение вида: left = right
    Возвращает {"var":"x", "value_frac":"4", "value_dec":"4"}
    """
    if "=" not in expr:
        return None
    left, right = expr.split("=", 1)
    left = left.strip()
    right = right.strip()

    L = _parse_linear(left, var)
    R = _parse_linear(right, var)
    if not L or not R:
        return None

    a1, b1 = L
    a2, b2 = R
    # a1*x + b1 = a2*x + b2  -> (a1-a2)x = (b2-b1)
    A = a1 - a2
    B = b2 - b1

    if A == 0:
        if B == 0:
            return {"var": var, "value_frac": "Бесконечно много решений", "value_dec": ""}
        return {"var": var, "value_frac": "Нет решений", "value_dec": ""}

    x = B / A
    return {"var": var, "value_frac": _pretty_fraction(x), "value_dec": _frac_to_decimal_str(x)}

def solve_2x2_system(text: str) -> Optional[Dict[str, str]]:
    """
    Решает систему 2х2:
      "2x+3y=7; x-y=1"
    Разделители: ; или \n
    """
    parts = [p.strip() for p in re.split(r"[;\n]+", text) if p.strip()]
    if len(parts) != 2:
        return None

    eq1, eq2 = parts[0], parts[1]
    if "=" not in eq1 or "=" not in eq2:
        return None

    def parse_eq(eq: str) -> Optional[Tuple[Fraction, Fraction, Fraction]]:
        # a*x + b*y = c
        L, R = eq.split("=", 1)
        L = L.strip()
        R = R.strip()

        # выразим L как ax + by + k
        # вытащим x:
        lx = _parse_linear(L.replace("y", "0"), "x")  # грубо, но не годится
        # Вместо этого: сделаем отдельный парсер: ax + by + c
        # Упростим: заменим y на (y) переменную и разберём вручную:
        # Мы сделаем эвристический разбор через коэффициенты:
        # Ищем все члены вида number*x, number*y, x, y, и константы.
        # Работает для типичных школьных записей.
        s = L.replace(" ", "").replace("**", "^")
        s = s.replace("-", "+-")
        terms = [t for t in s.split("+") if t]
        ax = Fraction(0, 1)
        by = Fraction(0, 1)
        k = Fraction(0, 1)

        def tofrac(num: str) -> Fraction:
            if num in ("", "+"):
                return Fraction(1, 1)
            if num == "-":
                return Fraction(-1, 1)
            if "/" in num:
                a, b = num.split("/", 1)
                return Fraction(int(a), int(b))
            if "." in num:
                return Fraction(Decimal(num))
            return Fraction(int(num), 1)

        for t in terms:
            if t.endswith("x"):
                coef = t[:-1]
                ax += tofrac(coef)
            elif t.endswith("y"):
                coef = t[:-1]
                by += tofrac(coef)
            else:
                # константа
                k += tofrac(t)

        # правую часть считаем как число/дробь выражение
        r_expr = _normalize_expr(words_to_math(R))
        rv = eval_expr(r_expr)
        if rv is None:
            # попробуем просто число
            try:
                rv = tofrac(R.replace(" ", ""))
            except Exception:
                return None

        # ax*x + by*y + k = rv -> ax*x + by*y = rv - k
        c = rv - k
        return ax, by, c

    p1 = parse_eq(eq1)
    p2 = parse_eq(eq2)
    if not p1 or not p2:
        return None

    a1, b1, c1 = p1
    a2, b2, c2 = p2

    # решаем по Крамеру
    det = a1 * b2 - a2 * b1
    if det == 0:
        return {"result": "Система не имеет единственного решения (det=0).", "x": "", "y": ""}

    x = (c1 * b2 - c2 * b1) / det
    y = (a1 * c2 - a2 * c1) / det

    return {
        "result": "Решено (2×2).",
        "x": f"{_pretty_fraction(x)} (≈ {_frac_to_decimal_str(x)})",
        "y": f"{_pretty_fraction(y)} (≈ {_frac_to_decimal_str(y)})",
    }

# ============================================================
# MAIN: detect math in any text, solve expression/equation/system
# ============================================================

def solve_any_math(text: str) -> Optional[str]:
    raw = (text or "").strip()
    if not raw:
        return None

    s = words_to_math(raw)
    s = _normalize_expr(s)

    # 1) система 2х2
    sys_try = solve_2x2_system(s)
    if sys_try:
        return f"🧮 {sys_try['result']}\n\nx = {sys_try['x']}\ny = {sys_try['y']}"

    # 2) уравнение x
    if "=" in s and ("x" in s):
        sol = solve_linear_equation(s, "x")
        if sol:
            if sol["value_dec"]:
                return f"🧮 Решение: {sol['var']} = {sol['value_frac']}  (≈ {sol['value_dec']})"
            return f"🧮 {sol['value_frac']}"

    # 3) проценты
    if "%" in s:
        maybe = _try_percent_patterns(s)
        if maybe is not None:
            return f"🧮 {raw} = {_pretty_fraction(maybe)}  (≈ {_frac_to_decimal_str(maybe)})"

    # 4) обычное выражение
    v = eval_expr(s)
    if v is not None:
        frac = _pretty_fraction(v)
        dec = _frac_to_decimal_str(v)
        if frac == dec:
            return f"🧮 {s} = {frac}"
        return f"🧮 {s} = {frac}  (≈ {dec})"

    # 5) если выражение спрятано в тексте — попробуем кандидаты
    candidates = extract_candidates(raw)
    for cand in candidates[:7]:
        # система/уравнение внутри
        sys_try = solve_2x2_system(cand)
        if sys_try:
            return f"🧮 {sys_try['result']}\n\nx = {sys_try['x']}\ny = {sys_try['y']}"
        if "=" in cand and "x" in cand:
            sol = solve_linear_equation(cand, "x")
            if sol:
                if sol["value_dec"]:
                    return f"🧮 Решение: x = {sol['value_frac']}  (≈ {sol['value_dec']})"
                return f"🧮 {sol['value_frac']}"
        if "%" in cand:
            maybe = _try_percent_patterns(cand)
            if maybe is not None:
                return f"🧮 {cand} = {_pretty_fraction(maybe)}  (≈ {_frac_to_decimal_str(maybe)})"
        vv = eval_expr(cand)
        if vv is not None:
            frac = _pretty_fraction(vv)
            dec = _frac_to_decimal_str(vv)
            if frac == dec:
                return f"🧮 {cand} = {frac}"
            return f"🧮 {cand} = {frac}  (≈ {dec})"

    return None

def extract_candidates(raw_text: str) -> List[str]:
    s = words_to_math(raw_text)
    chunks = MATH_CHUNK_RE.findall(s)
    out: List[str] = []
    for ch in chunks:
        expr = _normalize_expr(ch).strip()
        if not expr:
            continue
        if not _looks_like_math(expr):
            continue
        # не считаем одиночное число
        if re.fullmatch(r"\d+(?:\.\d+)?", expr):
            continue
        out.append(expr)
    out.sort(key=len, reverse=True)
    return out

def smart_answer(text: str, has_image: bool) -> str:
    t = (text or "").strip()
    tl = t.lower()

    # математика — сразу
    math_ans = solve_any_math(t)
    if math_ans:
        return math_ans

    if tl in ("привет", "привет!", "здравствуй", "здравствуйте"):
        return "Привет! Я могу решать математику (дроби, проценты, уравнения). Напиши пример 🙂"

    if has_image and not t:
        return "Фото получено 📷\n\nНапиши, что нужно сделать с фото."

    if has_image and t:
        return f"Фото получено 📷\nЗапрос: «{t}»\n\n(Сейчас демо: математика мощная, но распознавание фото — без реального AI.)"

    return f"Понял ✅\n\n«{t}»\n\nЕсли это математика — напиши пример типа:\n- 150 + 20%\n- 2x+3=11\n- 2x+3y=7; x-y=1"

# ============================================================
# Job runner (typing like human)
# ============================================================

async def run_job(job: Job, text: str, image_data_url: Optional[str]) -> None:
    try:
        has_image = bool(image_data_url)
        img_ok = bool(parse_data_url(image_data_url)) if has_image else False

        await job.queue.put({"type": "step", "stage": "input", "title": "Input received",
                             "detail": f"text={safe_trim(text, 80) if text else '∅'}, image={'yes' if has_image else 'no'}",
                             "ts": now_ms()})

        if has_image:
            await asyncio.sleep(0.2)
            await job.queue.put({"type": "step", "stage": "image", "title": "Analyzing image",
                                 "detail": "Decoding image data URL" if img_ok else "Bad image data",
                                 "ts": now_ms()})
            await asyncio.sleep(0.2)
            await job.queue.put({"type": "step", "stage": "image", "title": "Understanding scene",
                                 "detail": "Demo mode (no real vision AI)",
                                 "ts": now_ms()})

        await asyncio.sleep(0.15)
        await job.queue.put({"type": "step", "stage": "reasoning", "title": "Math engine",
                             "detail": "Parsing / solving…",
                             "ts": now_ms()})

        final_text = smart_answer(text, has_image)

        await asyncio.sleep(0.12)
        for ch in final_text:
            await job.queue.put({"type": "delta", "delta": ch})
            await asyncio.sleep(0.012 + (0.03 * ((uuid.uuid4().int % 9) / 9)))

        await job.queue.put({"type": "final", "title": "Ответ", "text": final_text, "ts": now_ms()})

    except Exception as e:
        await job.queue.put({"type": "error", "title": "Сбой", "text": f"Ошибка сервера: {e}", "ts": now_ms()})
    finally:
        job.done = True
        asyncio.create_task(cleanup_jobs())

# ============================================================
# FastAPI app
# ============================================================

app = FastAPI(title="Ultra Math Genius Server")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/public", StaticFiles(directory="public"), name="public")

@app.get("/", response_class=HTMLResponse)
async def home():
    try:
        with open("public/index.html", "r", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    except FileNotFoundError:
        return HTMLResponse(
            "<h2>Нет public/index.html</h2><p>Создай папку <b>public</b> и положи туда index.html</p>",
            status_code=404,
        )

@app.get("/styles.css")
async def styles_css():
    try:
        with open("public/styles.css", "rb") as f:
            return Response(f.read(), media_type="text/css; charset=utf-8")
    except FileNotFoundError:
        return Response("/* public/styles.css not found */", media_type="text/css; charset=utf-8", status_code=404)

@app.get("/script.js")
async def script_js():
    try:
        with open("public/script.js", "rb") as f:
            return Response(f.read(), media_type="application/javascript; charset=utf-8")
    except FileNotFoundError:
        return Response("// public/script.js not found", media_type="application/javascript; charset=utf-8", status_code=404)

@app.post("/api/solve_start")
async def solve_start(req: Request):
    payload = await req.json()
    text = (payload.get("text") or "").strip()
    image_data_url = payload.get("imageDataUrl")
    job = await create_job()
    asyncio.create_task(run_job(job, text=text, image_data_url=image_data_url))
    return {"job_id": job.job_id}

@app.get("/api/solve_stream/{job_id}")
async def solve_stream(job_id: str):
    job = await get_job(job_id)
    if not job:
        async def not_found():
            yield sse_pack({"type": "error", "title": "Сбой", "text": "job_id не найден"})
        return StreamingResponse(not_found(), media_type="text/event-stream")

    async def event_gen():
        last_ping = time.time()
        while True:
            if time.time() - last_ping > 10:
                last_ping = time.time()
                yield ": ping\n\n"
            try:
                ev = await asyncio.wait_for(job.queue.get(), timeout=1.0)
                yield sse_pack(ev)
                if ev.get("type") in ("final", "error"):
                    break
            except asyncio.TimeoutError:
                if job.done and job.queue.empty():
                    break
                continue
        yield ": done\n\n"

    return StreamingResponse(event_gen(), media_type="text/event-stream")
