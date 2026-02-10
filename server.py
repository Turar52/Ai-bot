# server.py — ULTRA MATH GENIUS + ПОИСК "везде" (Wiki + DuckDuckGo HTML)
# ============================================================
# pip install fastapi uvicorn python-multipart httpx
# uvicorn server:app --reload --host 0.0.0.0 --port 8000
#
# ПОИСК:
# 1) Если математика не нашлась — сервер пробует "поиск везде":
#    - Wikipedia (RU) краткое описание + картинка
#    - DuckDuckGo (HTML) — топ 3 результата (title + url + snippet)
#
# API:
# POST /api/solve_start { text, imageDataUrl? } -> { job_id }
# GET  /api/solve_stream/{job_id}              -> SSE (step/delta/final/error)
#
# ВАЖНО:
# - Если у тебя "белый экран", значит не грузятся public/index.html или .css/.js.
#   Проверь: папка public рядом с server.py и файлы внутри.
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

import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

getcontext().prec = 60

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
# Text normalization: words -> math
# ============================================================

SIMPLIFY_PREFIXES = ("упрости:", "упрости", "simplify:", "simplify")
DERIV_PREFIXES = ("производная:", "производная", "derivative:", "derivative", "d/dx:", "d/dx")
INTEG_PREFIXES = ("интеграл:", "интеграл", "integral:", "integral", "∫:")

SEARCH_PREFIXES = (
    "/search", "поиск:", "найди:", "найти:", "search:", "гугл:", "гугл",
    "что такое", "кто такой", "кто такая", "кто такие", "что это",
)

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
    if any(fn in s for fn in ("sqrt(", "abs(", "sin(", "cos(", "tan(", "log(", "ln(")):
        if s.count("(") > s.count(")"):
            s += ")"
    return s

def _normalize_expr(expr: str) -> str:
    expr = (expr or "").strip()
    expr = expr.replace("×", "*").replace("÷", "/").replace(",", ".").replace("π", "pi")
    expr = re.sub(r"\s+", " ", expr)
    expr = re.sub(r"[^0-9a-zA-Zx y\.\+\-\*\/\(\),%!\^\=\s]", "", expr).strip()
    return expr

def _rewrite_power(expr: str) -> str:
    return expr.replace("^", "**")

def _tokenize_factorial(expr: str) -> str:
    expr = re.sub(r"(\d+(?:\.\d+)?)\s*!", r"fact(\1)", expr)
    while True:
        m = re.search(r"(\([^()]+\))\s*!", expr)
        if not m:
            break
        inner = m.group(1)
        expr = expr[:m.start()] + f"fact({inner})" + expr[m.end():]
    return expr

def _pretty_fraction(fr: Fraction) -> str:
    if fr.denominator == 1:
        return str(fr.numerator)
    return f"{fr.numerator}/{fr.denominator}"

def _frac_to_decimal_str(fr: Fraction) -> str:
    d = Decimal(fr.numerator) / Decimal(fr.denominator)
    s = format(d.normalize(), "f")
    if len(s) > 120:
        s = f"{d:.18f}".rstrip("0").rstrip(".")
    return s

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

# ============================================================
# Safe expression evaluator (Fractions)
# ============================================================

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

ALLOWED_CONSTS = {
    "pi": Fraction(Decimal(str(math.pi))),
    "e": Fraction(Decimal(str(math.e))),
}

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
        v = node.n
        if isinstance(v, int):
            return Fraction(v, 1)
        return Fraction(Decimal(str(v)))

    def visit_Name(self, node: ast.Name) -> Fraction:
        name = node.id.lower()
        if name in ALLOWED_CONSTS:
            return ALLOWED_CONSTS[name]
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
            if abs(e) > 6000:
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
        if len(expr) > 800:
            return None
        tree = ast.parse(expr, mode="eval")
        return SafeEval().visit(tree)
    except ZeroDivisionError:
        raise
    except Exception:
        return None

# ============================================================
# Polynomial engine (symbolic): simplify/derivative/integral
# ============================================================

MAX_POLY_DEG = 6
Poly = Dict[int, Fraction]

def poly_clean(p: Poly) -> Poly:
    return {k: v for k, v in p.items() if v != 0}

def poly_add(a: Poly, b: Poly) -> Poly:
    out = dict(a)
    for k, v in b.items():
        out[k] = out.get(k, Fraction(0, 1)) + v
    return poly_clean(out)

def poly_sub(a: Poly, b: Poly) -> Poly:
    out = dict(a)
    for k, v in b.items():
        out[k] = out.get(k, Fraction(0, 1)) - v
    return poly_clean(out)

def poly_mul(a: Poly, b: Poly) -> Poly:
    out: Poly = {}
    for pa, ca in a.items():
        for pb, cb in b.items():
            deg = pa + pb
            if deg > MAX_POLY_DEG:
                raise ValueError("polynomial degree too large")
            out[deg] = out.get(deg, Fraction(0, 1)) + (ca * cb)
    return poly_clean(out)

def poly_pow(a: Poly, n: int) -> Poly:
    if n < 0:
        raise ValueError("negative power not supported for polynomials")
    if n == 0:
        return {0: Fraction(1, 1)}
    res = {0: Fraction(1, 1)}
    base = a
    exp = n
    while exp > 0:
        if exp & 1:
            res = poly_mul(res, base)
        exp >>= 1
        if exp:
            base = poly_mul(base, base)
    return poly_clean(res)

def poly_scale(a: Poly, k: Fraction) -> Poly:
    return poly_clean({p: c * k for p, c in a.items()})

def poly_div_const(a: Poly, k: Fraction) -> Poly:
    if k == 0:
        raise ZeroDivisionError
    return poly_clean({p: c / k for p, c in a.items()})

def poly_derivative(a: Poly) -> Poly:
    out: Poly = {}
    for p, c in a.items():
        if p == 0:
            continue
        out[p - 1] = out.get(p - 1, Fraction(0, 1)) + c * p
    return poly_clean(out)

def poly_integral(a: Poly) -> Poly:
    out: Poly = {}
    for p, c in a.items():
        newp = p + 1
        if newp > MAX_POLY_DEG:
            raise ValueError("integral degree too large")
        out[newp] = out.get(newp, Fraction(0, 1)) + c / newp
    return poly_clean(out)

def poly_to_string(a: Poly) -> str:
    a = poly_clean(a)
    if not a:
        return "0"
    terms = []
    for p in sorted(a.keys(), reverse=True):
        c = a[p]
        if c == 0:
            continue
        sign = "-" if c < 0 else "+"
        c_abs = -c if c < 0 else c

        if p == 0:
            part = _pretty_fraction(c_abs)
        elif p == 1:
            part = "x" if c_abs == 1 else f"{_pretty_fraction(c_abs)}x"
        else:
            part = f"x^{p}" if c_abs == 1 else f"{_pretty_fraction(c_abs)}x^{p}"

        terms.append((sign, part))

    first_sign, first_part = terms[0]
    out = first_part if first_sign == "+" else f"-{first_part}"
    for sgn, part in terms[1:]:
        out += f" {sgn} {part}"
    return out

class PolyEval(ast.NodeVisitor):
    def visit_Expression(self, node: ast.Expression) -> Poly:
        return self.visit(node.body)

    def visit_Constant(self, node: ast.Constant) -> Poly:
        if isinstance(node.value, int):
            return {0: Fraction(node.value, 1)}
        if isinstance(node.value, float):
            return {0: Fraction(Decimal(str(node.value)))}
        raise ValueError("bad const")

    def visit_Num(self, node: ast.Num) -> Poly:
        v = node.n
        if isinstance(v, int):
            return {0: Fraction(v, 1)}
        return {0: Fraction(Decimal(str(v)))}

    def visit_Name(self, node: ast.Name) -> Poly:
        name = node.id.lower()
        if name == "x":
            return {1: Fraction(1, 1)}
        if name in ALLOWED_CONSTS:
            return {0: ALLOWED_CONSTS[name]}
        raise ValueError("unknown name")

    def visit_UnaryOp(self, node: ast.UnaryOp) -> Poly:
        p = self.visit(node.operand)
        if isinstance(node.op, ast.UAdd):
            return p
        if isinstance(node.op, ast.USub):
            return poly_scale(p, Fraction(-1, 1))
        raise ValueError("bad unary")

    def visit_BinOp(self, node: ast.BinOp) -> Poly:
        a = self.visit(node.left)
        b = self.visit(node.right)

        if isinstance(node.op, ast.Add):
            return poly_add(a, b)
        if isinstance(node.op, ast.Sub):
            return poly_sub(a, b)
        if isinstance(node.op, ast.Mult):
            return poly_mul(a, b)
        if isinstance(node.op, ast.Div):
            if any(k != 0 for k in b.keys()):
                raise ValueError("division by non-constant not supported")
            k = b.get(0, Fraction(0, 1))
            return poly_div_const(a, k)
        if isinstance(node.op, ast.Pow):
            if any(k != 0 for k in b.keys()):
                raise ValueError("power must be constant integer")
            exp_val = b.get(0, Fraction(0, 1))
            if exp_val.denominator != 1:
                raise ValueError("power must be integer")
            n = int(exp_val.numerator)
            if n < 0:
                raise ValueError("negative power not supported")
            if n > MAX_POLY_DEG:
                raise ValueError("power too large")
            return poly_pow(a, n)

        raise ValueError("bad op")

    def visit_Call(self, node: ast.Call) -> Poly:
        raise ValueError("functions not allowed in polynomial mode")

    def generic_visit(self, node):
        raise ValueError(f"unsupported: {type(node).__name__}")

def parse_poly(expr: str) -> Optional[Poly]:
    try:
        expr = _rewrite_power(expr)
        if "!" in expr or "fact(" in expr:
            return None
        if any(fn in expr for fn in ("sin", "cos", "tan", "sqrt", "abs", "log", "ln")):
            return None
        tree = ast.parse(expr, mode="eval")
        return PolyEval().visit(tree)
    except Exception:
        return None

# ============================================================
# Equations (linear/quadratic) + Systems 2x2
# ============================================================

def solve_linear_equation(expr: str, var: str = "x") -> Optional[Dict[str, str]]:
    if "=" not in expr:
        return None
    left, right = expr.split("=", 1)
    pL = parse_poly(left.strip())
    pR = parse_poly(right.strip())
    if pL is None or pR is None:
        return None

    p = poly_sub(pL, pR)
    a = p.get(1, Fraction(0, 1))
    b = p.get(0, Fraction(0, 1))
    if any(k > 1 for k in p.keys()):
        return None

    if a == 0:
        if b == 0:
            return {"var": var, "value_frac": "Бесконечно много решений", "value_dec": ""}
        return {"var": var, "value_frac": "Нет решений", "value_dec": ""}

    x = -b / a
    return {"var": var, "value_frac": _pretty_fraction(x), "value_dec": _frac_to_decimal_str(x)}

def solve_quadratic_equation(expr: str, var: str = "x") -> Optional[Dict[str, Any]]:
    if "=" not in expr:
        return None
    left, right = expr.split("=", 1)
    pL = parse_poly(left.strip())
    pR = parse_poly(right.strip())
    if pL is None or pR is None:
        return None

    p = poly_clean(poly_sub(pL, pR))
    if not p:
        return {"type": "quadratic", "a": "0", "b": "0", "c": "0", "roots": ["Бесконечно много решений"]}

    if any(k > 2 for k in p.keys()):
        return None

    a = p.get(2, Fraction(0, 1))
    b = p.get(1, Fraction(0, 1))
    c = p.get(0, Fraction(0, 1))

    if a == 0:
        lin = solve_linear_equation(expr, var)
        if lin:
            return {"type": "linear", "roots": [f"{var} = {lin['value_frac']}" + (f" (≈ {lin['value_dec']})" if lin["value_dec"] else "")]}
        return None

    D = b * b - 4 * a * c

    def sqrt_fraction(x: Fraction) -> Tuple[bool, Fraction]:
        if x < 0:
            return False, Fraction(0, 1)
        num, den = x.numerator, x.denominator
        rn, rd = int(math.isqrt(num)), int(math.isqrt(den))
        if rn * rn == num and rd * rd == den:
            return True, Fraction(rn, rd)
        val = Decimal(num) / Decimal(den)
        approx = Decimal(str(math.sqrt(float(val))))
        return False, Fraction(approx)

    if D == 0:
        x = (-b) / (2 * a)
        return {"type": "quadratic", "D": _pretty_fraction(D), "roots": [f"x = {_pretty_fraction(x)} (≈ {_frac_to_decimal_str(x)})"]}

    if D < 0:
        ok, sD = sqrt_fraction(-D)
        twoa = 2 * a
        real = (-b) / twoa
        imag = sD / abs(twoa)
        return {
            "type": "quadratic",
            "D": _pretty_fraction(D),
            "roots": [f"x = {_pretty_fraction(real)} ± i*{_pretty_fraction(imag)} (≈ {_frac_to_decimal_str(real)} ± i*{_frac_to_decimal_str(imag)})"],
        }

    ok, sD = sqrt_fraction(D)
    twoa = 2 * a
    x1 = (-b + sD) / twoa
    x2 = (-b - sD) / twoa
    return {
        "type": "quadratic",
        "D": _pretty_fraction(D),
        "roots": [f"x1 = {_pretty_fraction(x1)} (≈ {_frac_to_decimal_str(x1)})",
                  f"x2 = {_pretty_fraction(x2)} (≈ {_frac_to_decimal_str(x2)})"],
    }

MATH_CHUNK_RE = re.compile(r"[0-9a-zA-Zx y\.\+\-\*\/\(\),%!\^\=\s]+", re.IGNORECASE)

def _looks_like_math(text: str) -> bool:
    if not text:
        return False
    if not re.search(r"\d", text) and ("x" not in text and "y" not in text):
        return False
    if re.search(r"[+\-*/^()=]", text):
        return True
    low = text.lower()
    return any(h in low for h in ("sqrt", "abs", "sin", "cos", "tan", "gcd", "lcm", "log", "ln", "%", "!", "pi", "e"))

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
        if re.fullmatch(r"\d+(?:\.\d+)?", expr):
            continue
        out.append(expr)
    out.sort(key=len, reverse=True)
    return out

def solve_2x2_system(text: str) -> Optional[Dict[str, str]]:
    parts = [p.strip() for p in re.split(r"[;\n]+", text) if p.strip()]
    if len(parts) != 2:
        return None
    eq1, eq2 = parts[0], parts[1]
    if "=" not in eq1 or "=" not in eq2:
        return None

    def parse_eq(eq: str) -> Optional[Tuple[Fraction, Fraction, Fraction]]:
        L, R = eq.split("=", 1)
        L = L.strip().replace(" ", "")
        R = R.strip()
        s = L.replace("-", "+-")
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
                ax += tofrac(t[:-1])
            elif t.endswith("y"):
                by += tofrac(t[:-1])
            else:
                k += tofrac(t)

        rv = eval_expr(_normalize_expr(words_to_math(R)))
        if rv is None:
            try:
                rv = tofrac(R.replace(" ", ""))
            except Exception:
                return None

        c = rv - k
        return ax, by, c

    p1 = parse_eq(eq1)
    p2 = parse_eq(eq2)
    if not p1 or not p2:
        return None

    a1, b1, c1 = p1
    a2, b2, c2 = p2
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
# High-level math solver
# ============================================================

def solve_expression(expr: str) -> Optional[str]:
    expr = expr.strip()
    if not expr:
        return None

    if "%" in expr:
        maybe = _try_percent_patterns(expr)
        if maybe is not None:
            return f"🧮 {expr} = {_pretty_fraction(maybe)}  (≈ {_frac_to_decimal_str(maybe)})"

    v = eval_expr(expr)
    if v is None:
        return None

    frac = _pretty_fraction(v)
    dec = _frac_to_decimal_str(v)
    if frac == dec:
        return f"🧮 {expr} = {frac}"
    return f"🧮 {expr} = {frac}  (≈ {dec})"

def simplify_polynomial(expr: str) -> Optional[str]:
    p = parse_poly(expr)
    if p is None:
        return None
    simp = poly_to_string(p)
    return f"🧠 Упрощено: {simp}"

def derivative_polynomial(expr: str) -> Optional[str]:
    p = parse_poly(expr)
    if p is None:
        return None
    dp = poly_derivative(p)
    return f"🧠 Производная: {poly_to_string(dp)}"

def integral_polynomial(expr: str) -> Optional[str]:
    p = parse_poly(expr)
    if p is None:
        return None
    ip = poly_integral(p)
    return f"🧠 Интеграл: {poly_to_string(ip)} + C"

def solve_any_math(text: str) -> Optional[str]:
    raw = (text or "").strip()
    if not raw:
        return None

    low = raw.lower().strip()

    for pref in SIMPLIFY_PREFIXES:
        if low.startswith(pref):
            expr = raw[len(pref):].strip(" :")
            expr = _normalize_expr(words_to_math(expr))
            return simplify_polynomial(expr) or "🧠 Упрощение: пока только полиномы по x."

    for pref in DERIV_PREFIXES:
        if low.startswith(pref):
            expr = raw[len(pref):].strip(" :")
            expr = _normalize_expr(words_to_math(expr))
            return derivative_polynomial(expr) or "🧠 Производная: пока только полиномы по x."

    for pref in INTEG_PREFIXES:
        if low.startswith(pref):
            expr = raw[len(pref):].strip(" :")
            expr = _normalize_expr(words_to_math(expr))
            return integral_polynomial(expr) or "🧠 Интеграл: пока только полиномы по x."

    s = _normalize_expr(words_to_math(raw))

    sys_try = solve_2x2_system(s)
    if sys_try:
        return f"🧮 {sys_try['result']}\n\nx = {sys_try['x']}\ny = {sys_try['y']}"

    if "=" in s and "x" in s:
        quad = solve_quadratic_equation(s, "x")
        if quad and quad.get("type") == "quadratic":
            roots = "\n".join(quad["roots"])
            return f"🧮 Квадратное уравнение\nD = {quad['D']}\n\n{roots}"
        lin = solve_linear_equation(s, "x")
        if lin:
            if lin["value_dec"]:
                return f"🧮 Решение: x = {lin['value_frac']}  (≈ {lin['value_dec']})"
            return f"🧮 {lin['value_frac']}"

    if "x" in s and "=" not in s:
        simp = simplify_polynomial(s)
        if simp:
            return simp

    expr_ans = solve_expression(s)
    if expr_ans:
        return expr_ans

    candidates = extract_candidates(raw)
    for cand in candidates[:10]:
        if "=" in cand and "x" in cand:
            quad = solve_quadratic_equation(cand, "x")
            if quad and quad.get("type") == "quadratic":
                roots = "\n".join(quad["roots"])
                return f"🧮 Квадратное уравнение\nD = {quad['D']}\n\n{roots}"
            lin = solve_linear_equation(cand, "x")
            if lin:
                if lin["value_dec"]:
                    return f"🧮 Решение: x = {lin['value_frac']}  (≈ {lin['value_dec']})"
                return f"🧮 {lin['value_frac']}"

        if "x" in cand and "=" not in cand:
            simp = simplify_polynomial(cand)
            if simp:
                return simp

        ans = solve_expression(cand)
        if ans:
            return ans

    return None

# ============================================================
# SEARCH "everywhere"
# ============================================================

def should_search(text: str) -> bool:
    t = (text or "").strip().lower()
    if not t:
        return False

    # если математическое — не ищем
    if solve_any_math(text):
        return False

    # если явно попросили поиск
    if any(t.startswith(p) for p in SEARCH_PREFIXES):
        return True

    # если вопросительный/объяснительный текст и мало мат-символов
    if "?" in t:
        return True

    # если это просто обычная фраза без цифр и без x/y — чаще всего нужен поиск/ответ
    if not re.search(r"\d", t) and ("x" not in t and "y" not in t):
        return True

    return False

def clean_search_query(text: str) -> str:
    t = (text or "").strip()
    tl = t.lower().strip()
    # убираем префиксы
    for p in ("/search", "поиск:", "найди:", "найти:", "search:", "гугл:", "гугл"):
        if tl.startswith(p):
            t = t[len(p):].strip()
            break
    return t.strip()

async def wiki_search_ru(query: str) -> Optional[Dict[str, Any]]:
    q = (query or "").strip()
    if not q:
        return None

    url = "https://ru.wikipedia.org/w/api.php"
    params = {
        "action": "query",
        "generator": "search",
        "gsrsearch": q,
        "gsrlimit": 1,
        "prop": "pageimages|extracts",
        "pithumbsize": 600,
        "exintro": 1,
        "explaintext": 1,
        "redirects": 1,
        "format": "json",
        "origin": "*",
    }

    try:
        async with httpx.AsyncClient(timeout=8.0, headers={"User-Agent": "UltraMathGenius/1.0"}) as client:
            r = await client.get(url, params=params)
            r.raise_for_status()
            data = r.json()
    except Exception:
        return None

    pages = data.get("query", {}).get("pages", {})
    if not pages:
        return None
    page = list(pages.values())[0]
    title = page.get("title") or "Wikipedia"
    extract = (page.get("extract") or "").strip()
    thumb = page.get("thumbnail", {}).get("source")
    pageid = page.get("pageid")
    link = f"https://ru.wikipedia.org/?curid={pageid}" if pageid else None

    if not extract:
        extract = "Описание отсутствует."
    return {"source": "wikipedia", "title": title, "text": extract, "image": thumb, "url": link}

async def ddg_search(query: str) -> List[Dict[str, str]]:
    """
    DuckDuckGo HTML (без ключей). Иногда может блокироваться сетью/хостингом — тогда вернём пусто.
    """
    q = (query or "").strip()
    if not q:
        return []
    url = "https://duckduckgo.com/html/"
    try:
        async with httpx.AsyncClient(timeout=8.0, headers={"User-Agent": "Mozilla/5.0"}) as client:
            r = await client.post(url, data={"q": q})
            r.raise_for_status()
            html = r.text
    except Exception:
        return []

    results: List[Dict[str, str]] = []
    # очень простенький парсер (регекс): title + url + snippet
    # (DDG HTML разный, но обычно работает)
    for m in re.finditer(r'<a rel="nofollow" class="result__a" href="([^"]+)".*?>(.*?)</a>.*?<a class="result__snippet".*?>(.*?)</a>',
                         html, flags=re.DOTALL):
        href = re.sub(r"&amp;", "&", m.group(1))
        title = re.sub(r"<.*?>", "", m.group(2)).strip()
        snippet = re.sub(r"<.*?>", "", m.group(3)).strip()
        if title and href:
            results.append({"title": title[:120], "url": href[:500], "snippet": snippet[:240]})
        if len(results) >= 3:
            break

    return results

async def search_everywhere(query: str) -> Dict[str, Any]:
    q = clean_search_query(query)
    wiki = await wiki_search_ru(q)
    ddg = await ddg_search(q)
    return {"query": q, "wiki": wiki, "ddg": ddg}

def format_search_answer(payload: Dict[str, Any]) -> str:
    q = payload.get("query") or ""
    wiki = payload.get("wiki")
    ddg = payload.get("ddg") or []

    lines = [f"🔎 Поиск: {q}"]

    if wiki:
        lines.append("")
        lines.append(f"📚 Wikipedia: {wiki.get('title')}")
        lines.append(safe_trim(wiki.get("text") or "", 700))
        if wiki.get("url"):
            lines.append(f"Источник: {wiki['url']}")

    if ddg:
        lines.append("")
        lines.append("🌐 Результаты (DuckDuckGo):")
        for i, it in enumerate(ddg, 1):
            lines.append(f"{i}) {it.get('title')}")
            if it.get("snippet"):
                lines.append(f"   {it['snippet']}")
            if it.get("url"):
                lines.append(f"   {it['url']}")

    if not wiki and not ddg:
        lines.append("")
        lines.append("Ничего не нашёл (возможно, нет доступа к интернету на сервере).")

    return "\n".join(lines)

# ============================================================
# Bot answer (Math first -> Search -> Chat)
# ============================================================

async def smart_answer(text: str, has_image: bool) -> Tuple[str, Optional[Dict[str, Any]]]:
    t = (text or "").strip()
    tl = t.lower()

    math_ans = solve_any_math(t)
    if math_ans:
        return math_ans, None

    if should_search(t):
        payload = await search_everywhere(t)
        return format_search_answer(payload), payload

    if tl in ("привет", "привет!", "здравствуй", "здравствуйте"):
        return (
            "Привет! Я ультра-математический ассистент 😄\n\n"
            "Примеры:\n"
            "• 1/2 + 3/4\n"
            "• 150 + 20%\n"
            "• 2x+3=11\n"
            "• x^2 - 5x + 6 = 0\n"
            "• 2x+3y=7; x-y=1\n"
            "• упрости: (x+1)^2 - x^2\n"
            "• производная: 3x^2 - 2x + 1\n"
            "• интеграл: 6x - 4\n\n"
            "И если это не математика — просто спроси, я отвечу 🙂"
        ), None

    if has_image and not t:
        return "Фото получено 📷\n\nНапиши, что сделать с фото (задача/текст/объяснение).", None

    if has_image and t:
        return (
            f"Фото получено 📷\nЗапрос: «{t}»\n\n"
            "Распознавание фото сейчас в демо-режиме (без настоящего Vision AI),\n"
            "но математика в тексте работает очень мощно."
        ), None

    return (
        f"Понял ✅\n\n«{t}»\n\n"
        "Если это математика — пиши пример прямо в тексте, я сам найду и решу.\n"
        "Если это обычный вопрос — я отвечу или попробую поиск."
    ), None

# ============================================================
# Job runner (human-like typing)
# ============================================================

async def run_job(job: Job, text: str, image_data_url: Optional[str]) -> None:
    try:
        has_image = bool(image_data_url)
        img_ok = bool(parse_data_url(image_data_url)) if has_image else False

        await job.queue.put({"type": "step", "stage": "input", "title": "Input received",
                             "detail": f"text={safe_trim(text, 80) if text else '∅'}, image={'yes' if has_image else 'no'}",
                             "ts": now_ms()})

        if has_image:
            await asyncio.sleep(0.15)
            await job.queue.put({"type": "step", "stage": "image", "title": "Analyzing image",
                                 "detail": "Decoding image data URL" if img_ok else "Bad image data",
                                 "ts": now_ms()})
            await asyncio.sleep(0.16)
            await job.queue.put({"type": "step", "stage": "image", "title": "Understanding scene",
                                 "detail": "Demo mode (no real vision AI)",
                                 "ts": now_ms()})

        await asyncio.sleep(0.12)
        await job.queue.put({"type": "step", "stage": "reasoning", "title": "Engine",
                             "detail": "Math → Search → Answer",
                             "ts": now_ms()})

        final_text, search_payload = await smart_answer(text, has_image)

        await asyncio.sleep(0.08)
        for ch in final_text:
            await job.queue.put({"type": "delta", "delta": ch})
            await asyncio.sleep(0.010 + (0.028 * ((uuid.uuid4().int % 9) / 9)))

        # если есть поиск — отправим метаданные отдельно (для UI-карточек)
        if search_payload:
            await job.queue.put({"type": "step", "stage": "search_meta", "title": "Search meta",
                                 "detail": json.dumps(search_payload, ensure_ascii=False)[:900],
                                 "ts": now_ms()})

        await job.queue.put({"type": "final", "title": "Ответ", "text": final_text, "ts": now_ms()})

    except Exception as e:
        await job.queue.put({"type": "error", "title": "Сбой", "text": f"Ошибка сервера: {e}", "ts": now_ms()})
    finally:
        job.done = True
        asyncio.create_task(cleanup_jobs())

# ============================================================
# FastAPI app
# ============================================================

app = FastAPI(title="Ultra Math Genius Server + Search Everywhere")

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
