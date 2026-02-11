// server.js — FULL POWER ✅ (FILE DB + CAMERA OCR + SOLVE ALL TASKS ON PHOTO)
// ==========================================================
// ✅ У каждого пользователя свой код (token) + свои чаты/история
// ✅ Всё хранится в файле ./data/users_db.json (не пропадает после перезапуска)
// ✅ Камера/фото: OCR (rus+eng) + решает ВСЕ задания, которые найдёт на фото (по строкам)
// ✅ Ultra Math: дроби frac{a}{b}, проценты, уравнения, системы, производная, интеграл, упрости
// ✅ Поиск "везде": Wikipedia RU + DuckDuckGo (если это не математика)
//
// --------------------------
// УСТАНОВКА
// --------------------------
// npm init -y
// npm i express cors nerdamer tesseract.js
//
// Структура:
// project/
//   server.js
//   public/
//     index.html
//     styles.css
//     script.js
//   data/                (создастся само)
//     users_db.json      (создастся само)
//
// Запуск:
// node server.js
// открыть: http://127.0.0.1:3000
//
// --------------------------
// API
// --------------------------
// POST /api/user_code        body: { userId } -> { userId, code }
// POST /api/solve_start      body: { userId, code, text, imageDataUrl? } -> { job_id }
// GET  /api/solve_stream/:id (SSE) -> step | delta | final | error
// GET  /api/history?userId&code
// POST /api/new_chat         body: { userId, code, title? } -> { chatId }
// POST /api/select_chat      body: { userId, code, chatId } -> { ok:true }
// POST /api/reset_user       body: { userId, code } -> { ok:true }
// ==========================================================

const fs = require("fs");
const fsp = require("fs/promises");
const path = require("path");
const express = require("express");
const cors = require("cors");
const nerdamer = require("nerdamer/all");
const Tesseract = require("tesseract.js");

const app = express();

// ---------- Config ----------
const PORT = process.env.PORT || 3000;
const PUBLIC_DIR = path.join(__dirname, "public");

// File storage
const DATA_DIR = path.join(__dirname, "data");
const USERS_DB_FILE = path.join(DATA_DIR, "users_db.json");

// OCR settings
const OCR_LANG = "rus+eng";
const OCR_LANG_PATH = "https://tessdata.projectnaptha.com/4.0.0";

// ---------- Middlewares ----------
app.use(cors());
app.use(express.json({ limit: "25mb" }));
app.use(express.urlencoded({ extended: true, limit: "25mb" }));
app.use(express.static(PUBLIC_DIR));

// ===========================
// Helpers
// ===========================
function nowMs() {
  return Date.now();
}
function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}
function safeLower(s) {
  return (s || "").toString().trim().toLowerCase();
}
function makeId(prefix = "id") {
  return (
    prefix +
    "_" +
    Math.random().toString(16).slice(2) +
    Math.random().toString(16).slice(2) +
    "_" +
    Date.now().toString(16)
  );
}

// ===========================
// FILE DB (users_db.json)
// ===========================
//
// DB schema:
// {
//   "users": {
//     "@name": {
//        "token": "...",
//        "createdAt": 123,
//        "activeChatId": "chat_...",
//        "chats": {
//           "chat_...": { "id": "...", "title": "...", "createdAt": 123, "messages": [ {role,text,ts, imageDataUrl?} ] }
//        },
//        "chatOrder": ["chat_...","chat_..."]
//     }
//   }
// }

let DB = { users: {} };
let DB_DIR_READY = false;
let DB_WRITE_QUEUE = Promise.resolve();

async function ensureDataDir() {
  if (DB_DIR_READY) return;
  await fsp.mkdir(DATA_DIR, { recursive: true });
  DB_DIR_READY = true;
}

async function loadDb() {
  await ensureDataDir();
  if (!fs.existsSync(USERS_DB_FILE)) {
    DB = { users: {} };
    await saveDb();
    return;
  }
  try {
    const raw = await fsp.readFile(USERS_DB_FILE, "utf-8");
    const parsed = JSON.parse(raw || "{}");
    DB = parsed && typeof parsed === "object" ? parsed : { users: {} };
    if (!DB.users) DB.users = {};
  } catch {
    DB = { users: {} };
    await saveDb();
  }
}

function saveDb() {
  DB_WRITE_QUEUE = DB_WRITE_QUEUE.then(async () => {
    await ensureDataDir();
    const tmp = USERS_DB_FILE + ".tmp";
    const json = JSON.stringify(DB, null, 2);
    await fsp.writeFile(tmp, json, "utf-8");
    await fsp.rename(tmp, USERS_DB_FILE);
  });
  return DB_WRITE_QUEUE;
}

function getOrCreateUser(userId) {
  const id = safeLower(userId);
  if (!id) return null;

  if (!DB.users[id]) {
    const chatId = makeId("chat");
    DB.users[id] = {
      token: "u_" + Math.random().toString(16).slice(2) + Date.now().toString(16),
      createdAt: nowMs(),
      activeChatId: chatId,
      chats: {
        [chatId]: {
          id: chatId,
          title: "Новый чат",
          createdAt: nowMs(),
          messages: [],
        },
      },
      chatOrder: [chatId],
    };
  } else {
    const u = DB.users[id];
    if (!u.token) u.token = "u_" + Math.random().toString(16).slice(2) + Date.now().toString(16);
    if (!u.chats) u.chats = {};
    if (!Array.isArray(u.chatOrder)) u.chatOrder = Object.keys(u.chats);
    if (!u.activeChatId || !u.chats[u.activeChatId]) {
      const chatId = makeId("chat");
      u.chats[chatId] = { id: chatId, title: "Новый чат", createdAt: nowMs(), messages: [] };
      u.chatOrder.unshift(chatId);
      u.activeChatId = chatId;
    }
  }
  return DB.users[id];
}

function authUser(userId, code) {
  const id = safeLower(userId);
  if (!id) return { ok: false, reason: "userId required" };
  const u = getOrCreateUser(id);
  if (!u) return { ok: false, reason: "bad userId" };

  const c = (code || "").toString().trim();
  if (!c) return { ok: false, reason: "code required" };
  if (c !== u.token) return { ok: false, reason: "bad code" };
  return { ok: true, user: u, userId: id };
}

function getActiveChat(user) {
  const id = user.activeChatId;
  return user.chats[id];
}

function pushMessage(user, role, payload) {
  const chat = getActiveChat(user);
  if (!chat) return;
  chat.messages.push(payload);

  // автозаголовок по первому сообщению
  if (role === "user" && chat.title === "Новый чат") {
    const t = (payload.text || "").trim();
    if (t) chat.title = t.slice(0, 25) + (t.length > 25 ? "…" : "");
  }
}

// ===========================
// Jobs / SSE (per request)
// ===========================
const JOBS = new Map();
const JOB_TTL_MS = 15 * 60 * 1000;

function ssePack(obj) {
  return `data: ${JSON.stringify(obj)}\n\n`;
}

function createJob() {
  const jobId = makeId("job");
  JOBS.set(jobId, { queue: [], waiters: [], done: false, createdAt: nowMs() });
  return jobId;
}
function jobPush(jobId, ev) {
  const job = JOBS.get(jobId);
  if (!job) return;
  if (job.waiters.length) job.waiters.shift()(ev);
  else job.queue.push(ev);
}
function jobWait(jobId) {
  const job = JOBS.get(jobId);
  if (!job) return Promise.resolve(null);
  if (job.queue.length) return Promise.resolve(job.queue.shift());
  return new Promise((resolve) => job.waiters.push(resolve));
}
setInterval(() => {
  const cutoff = nowMs() - JOB_TTL_MS;
  for (const [id, job] of JOBS.entries()) {
    if (job.createdAt < cutoff) JOBS.delete(id);
  }
}, 30 * 1000).unref();

// ===========================
// Image helpers
// ===========================
function parseDataUrl(dataUrl) {
  if (!dataUrl || typeof dataUrl !== "string") return null;
  const m = dataUrl.match(/^data:(image\/[a-zA-Z0-9.+-]+);base64,(.*)$/s);
  if (!m) return null;
  try {
    return { mime: m[1], bytes: Buffer.from(m[2], "base64") };
  } catch {
    return null;
  }
}

// ===========================
// OCR
// ===========================
function cleanupOcrText(s) {
  return (s || "")
    .replace(/[|]/g, "1")
    .replace(/[“”]/g, '"')
    .replace(/[‘’]/g, "'")
    .replace(/\s+\n/g, "\n")
    .replace(/\n\s+/g, "\n")
    .replace(/[ \t]+/g, " ")
    .trim();
}
async function ocrImageBuffer(buffer) {
  const { data } = await Tesseract.recognize(buffer, OCR_LANG, {
    langPath: OCR_LANG_PATH,
    logger: () => {},
  });
  return cleanupOcrText(data?.text || "");
}

// ===========================
// MATH (ULTRA)
// ===========================
function convertFracToDivision(input) {
  if (!input || typeof input !== "string") return input;
  let s = input;
  if (!s.includes("frac{")) return s;

  function readBraced(str, startIndex) {
    if (str[startIndex] !== "{") return null;
    let i = startIndex + 1;
    let depth = 1;
    let out = "";
    while (i < str.length) {
      const ch = str[i];
      if (ch === "{") {
        depth++;
        out += ch;
      } else if (ch === "}") {
        depth--;
        if (depth === 0) return { value: out, endIndex: i };
        out += ch;
      } else out += ch;
      i++;
    }
    return null;
  }

  let guard = 0;
  while (s.includes("frac{") && guard < 80) {
    guard++;
    const idx = s.indexOf("frac{");
    if (idx < 0) break;

    const numStart = idx + "frac".length;
    const num = readBraced(s, numStart);
    if (!num) break;

    const denStart = num.endIndex + 1;
    if (s[denStart] !== "{") break;
    const den = readBraced(s, denStart);
    if (!den) break;

    const numVal = convertFracToDivision(num.value);
    const denVal = convertFracToDivision(den.value);

    const replaced = `(( ${numVal} ))/(( ${denVal} ))`;
    s = s.slice(0, idx) + replaced + s.slice(den.endIndex + 1);
  }
  return s;
}

function wordsToMath(text) {
  let s = (text || "").toLowerCase().trim();
  s = s
    .replaceAll("×", "*")
    .replaceAll("÷", "/")
    .replaceAll(",", ".")
    .replaceAll("π", "pi");
  s = s.replace(/\s+/g, " ");

  const repl = [
    [/\bделить\s+на\b/gi, "/"],
    [/\bразделить\s+на\b/gi, "/"],
    [/\bумножить\s+на\b/gi, "*"],
    [/\bплюс\b/gi, "+"],
    [/\bминус\b/gi, "-"],
    [/\bв\s+степени\b/gi, "^"],
    [/\bстепень\b/gi, "^"],
    [/\bквадрат\b/gi, "^2"],
    [/\bкуб\b/gi, "^3"],
    [/\bкорень\s+из\b/gi, "sqrt("],
    [/\bкорень\b/gi, "sqrt("],
    [/\bмодуль\b/gi, "abs("],
    [/\bпроцентов\b/gi, "%"],
    [/\bпроцента\b/gi, "%"],
    [/\bпроцент\b/gi, "%"],
    [/\bпи\b/gi, "pi"],
    [/\bлог\b/gi, "log("],
    [/\bln\b/gi, "ln("],
    [/\bсин\b/gi, "sin("],
    [/\bкос\b/gi, "cos("],
    [/\bтан\b/gi, "tan("],
  ];
  for (const [a, b] of repl) s = s.replace(a, b);

  const open = (s.match(/\(/g) || []).length;
  const close = (s.match(/\)/g) || []).length;
  if (open > close) s += ")".repeat(open - close);

  return s;
}

function normalizeExpr(expr) {
  let s = (expr || "").trim();
  s = s.replaceAll("×", "*").replaceAll("÷", "/").replaceAll(",", ".").replaceAll("π", "pi");
  s = s.replace(/\s+/g, " ");
  s = s.replace(/[^0-9a-zA-Zxy\.\+\-\*\/\(\),%!\^\=\s{};]/g, "").trim();
  return s;
}

function safeNerdamerEval(expr) {
  let e = normalizeExpr(wordsToMath(expr));
  if (!e) return null;
  e = convertFracToDivision(e);
  e = e.replace(/(\d+(?:\.\d+)?)\s*!/g, "factorial($1)");
  try {
    return nerdamer(e).evaluate().text();
  } catch {
    return null;
  }
}

function tryPercentPatterns(raw) {
  const s = raw.trim();

  let m = s.match(/^\s*(.+?)\s*%\s*(?:от|of)\s*(.+?)\s*$/i);
  if (m) {
    const p = safeNerdamerEval(m[1]);
    const base = safeNerdamerEval(m[2]);
    if (p == null || base == null) return null;
    return nerdamer(`${base}*(${p})/100`).evaluate().text();
  }

  m = s.match(/^\s*(.+?)\s*([+\-])\s*(.+?)\s*%\s*$/i);
  if (m) {
    const base = safeNerdamerEval(m[1]);
    const p = safeNerdamerEval(m[3]);
    if (base == null || p == null) return null;
    const delta = nerdamer(`${base}*(${p})/100`).evaluate().text();
    return m[2] === "+"
      ? nerdamer(`${base}+(${delta})`).evaluate().text()
      : nerdamer(`${base}-(${delta})`).evaluate().text();
  }

  m = s.match(/^\s*(.+?)\s*%\s*$/i);
  if (m) {
    const p = safeNerdamerEval(m[1]);
    if (p == null) return null;
    return nerdamer(`(${p})/100`).evaluate().text();
  }
  return null;
}

function solveMath(text) {
  const raw = (text || "").trim();
  if (!raw) return null;
  const low = raw.toLowerCase();

  if (low.startsWith("упрости") || low.startsWith("simplify")) {
    const expr = raw.replace(/^упрости:?\s*/i, "").replace(/^simplify:?\s*/i, "");
    const e = normalizeExpr(wordsToMath(expr));
    try {
      return `🧠 Упрощено:\n${nerdamer(convertFracToDivision(e)).simplify().text()}`;
    } catch {
      return "🧠 Упрощение: не смог (попробуй проще выражение).";
    }
  }

  if (low.startsWith("производная") || low.startsWith("derivative") || low.startsWith("d/dx")) {
    const expr = raw
      .replace(/^производная:?\s*/i, "")
      .replace(/^derivative:?\s*/i, "")
      .replace(/^d\/dx:?\s*/i, "");
    const e = normalizeExpr(wordsToMath(expr));
    try {
      return `🧠 Производная по x:\n${nerdamer.diff(convertFracToDivision(e), "x").text()}`;
    } catch {
      return "🧠 Производная: не смог (проверь выражение и переменную x).";
    }
  }

  if (low.startsWith("интеграл") || low.startsWith("integral") || low.startsWith("∫")) {
    const expr = raw
      .replace(/^интеграл:?\s*/i, "")
      .replace(/^integral:?\s*/i, "")
      .replace(/^∫:?\s*/i, "");
    const e = normalizeExpr(wordsToMath(expr));
    try {
      return `🧠 Интеграл по x:\n${nerdamer.integrate(convertFracToDivision(e), "x").text()} + C`;
    } catch {
      return "🧠 Интеграл: не смог (проверь выражение и переменную x).";
    }
  }

  let expr = normalizeExpr(wordsToMath(raw));
  if (!expr) return null;

  if (expr.includes(";") && expr.includes("=") && (expr.includes("x") || expr.includes("y"))) {
    const parts = expr.split(";").map((s) => s.trim()).filter(Boolean);
    if (parts.length >= 2) {
      try {
        const sol = nerdamer.solveEquations(parts.slice(0, 2));
        const pretty = sol.map((pair) => `${pair[0]} = ${pair[1]}`).join("\n");
        return `🧮 Решение системы:\n${pretty}`;
      } catch {}
    }
  }

  if (expr.includes("=") && expr.includes("x")) {
    const [L, R] = expr.split("=", 2);
    const eq = `(${L})-(${R})`;
    try {
      const roots = nerdamer.solve(convertFracToDivision(eq), "x");
      return `🧮 Решение уравнения:\n${roots.text()}`;
    } catch {}
  }

  if (expr.includes("%")) {
    const v = tryPercentPatterns(expr);
    if (v != null) return `🧮 ${expr} = ${v}`;
  }

  const v = safeNerdamerEval(expr);
  if (v != null) return `🧮 ${expr} = ${v}`;

  // solve "everywhere": find math pieces in text
  const pieces = [];
  const re = /frac\{[^{}]*\}\{[^{}]*\}|[\dxy\.\(\)\+\-\*\/\^=%! ]{3,}/gi;
  let m;
  while ((m = re.exec(raw)) && pieces.length < 6) {
    const candidate = m[0].trim();
    if (!candidate) continue;
    if (!/\d/.test(candidate) && !candidate.includes("frac{")) continue;

    const letters = (candidate.match(/[a-zA-Zа-яА-Я]/g) || []).length;
    if (letters > 8) continue;

    const calc = safeNerdamerEval(candidate);
    if (calc != null) pieces.push(`• ${candidate} = ${calc}`);
  }
  if (pieces.length) return `🧮 Нашёл и решил:\n${pieces.join("\n")}`;

  return null;
}

// ===========================
// SOLVE ALL TASKS FROM PHOTO TEXT
// ===========================
function looksLikeMathLine(line) {
  const s = (line || "").trim();
  if (!s) return false;

  if (s.includes("frac{")) return true;
  if (/[=+\-*/^]/.test(s) && /\d/.test(s)) return true;
  if (/[xXyY]/.test(s) && s.includes("=")) return true;
  if (/^\s*\d+\s*[\)\.:-]\s*.+/.test(s) && /\d/.test(s)) return true;
  if (/[()]/.test(s) && /\d/.test(s)) return true;

  // типо "1/2" "3:4" "7·8"
  if (/\d+\s*[\/:]\s*\d+/.test(s)) return true;

  return false;
}

function cleanupOcrLines(ocrText) {
  return (ocrText || "")
    .replace(/\r/g, "\n")
    .split("\n")
    .map((l) => l.trim())
    .filter(Boolean)
    .map((l) =>
      l
        .replace(/[—–]/g, "-")
        .replace(/[×]/g, "*")
        .replace(/[÷]/g, "/")
        .replace(/[,:]/g, ".")
        .replace(/\bO\b/g, "0")
        .replace(/\s+/g, " ")
        .trim()
    );
}

function stripNumbering(line) {
  return (line || "").replace(/^\s*\d+\s*[\)\.:-]\s*/, "").trim();
}

function solveAllFromTextBlock(textBlock) {
  const lines = cleanupOcrLines(textBlock);

  const solved = [];
  const seen = new Set();

  for (const line of lines) {
    if (!looksLikeMathLine(line)) continue;
    const normalized = stripNumbering(line);
    if (!normalized) continue;

    const key = normalized.toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);

    const ans = solveMath(normalized);
    if (ans) solved.push({ task: line, normalized, answer: ans });
  }

  // если ничего не нашли — попытка вытащить выражения из всего текста
  if (solved.length === 0) {
    const raw = (textBlock || "").replace(/\s+/g, " ").trim();
    const re = /frac\{[^{}]+\}\{[^{}]+\}|[0-9xXyY\.\(\)\+\-\*\/\^=%! ]{4,}/g;
    const found = raw.match(re) || [];
    for (const f of found.slice(0, 12)) {
      const candidate = f.trim();
      if (!candidate) continue;
      const ans = solveMath(candidate);
      if (ans) solved.push({ task: candidate, normalized: candidate, answer: ans });
    }
  }

  return solved;
}

// ===========================
// SEARCH “везде” (Wiki RU + DDG)
// ===========================
const SEARCH_PREFIXES = [
  "/search",
  "поиск:",
  "найди:",
  "найти:",
  "search:",
  "гугл:",
  "гугл",
  "что такое",
  "кто такой",
  "кто такая",
  "что это",
];

function cleanSearchQuery(text) {
  let t = (text || "").trim();
  const low = t.toLowerCase();
  for (const p of ["/search", "поиск:", "найди:", "найти:", "search:", "гугл:", "гугл"]) {
    if (low.startsWith(p)) {
      t = t.slice(p.length).trim();
      break;
    }
  }
  return t.trim();
}

function shouldSearch(text) {
  const t = (text || "").trim().toLowerCase();
  if (!t) return false;

  // если математика уже решается — не ищем
  if (solveMath(text)) return false;

  if (SEARCH_PREFIXES.some((p) => t.startsWith(p))) return true;
  if (t.includes("?")) return true;

  const hasDigits = /\d/.test(t);
  if (!hasDigits) return true;

  return false;
}

async function wikiSearchRu(query) {
  const q = cleanSearchQuery(query);
  if (!q) return null;

  const url = new URL("https://ru.wikipedia.org/w/api.php");
  url.searchParams.set("action", "query");
  url.searchParams.set("generator", "search");
  url.searchParams.set("gsrsearch", q);
  url.searchParams.set("gsrlimit", "1");
  url.searchParams.set("prop", "pageimages|extracts");
  url.searchParams.set("pithumbsize", "600");
  url.searchParams.set("exintro", "1");
  url.searchParams.set("explaintext", "1");
  url.searchParams.set("redirects", "1");
  url.searchParams.set("format", "json");
  url.searchParams.set("origin", "*");

  try {
    const r = await fetch(url.toString(), { headers: { "User-Agent": "UltraMathGenius/1.0" } });
    if (!r.ok) return null;
    const data = await r.json();
    const pages = data?.query?.pages;
    if (!pages) return null;
    const page = Object.values(pages)[0];
    const title = page?.title || "Wikipedia";
    const text = (page?.extract || "").trim() || "Описание отсутствует.";
    const img = page?.thumbnail?.source || null;
    const pageid = page?.pageid;
    const link = pageid ? `https://ru.wikipedia.org/?curid=${pageid}` : null;
    return { source: "wikipedia", title, text, image: img, url: link };
  } catch {
    return null;
  }
}

async function ddgSearch(query) {
  const q = cleanSearchQuery(query);
  if (!q) return [];
  try {
    const r = await fetch("https://duckduckgo.com/html/", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded", "User-Agent": "Mozilla/5.0" },
      body: new URLSearchParams({ q }).toString(),
    });
    if (!r.ok) return [];
    const html = await r.text();

    const results = [];
    const re =
      /<a rel="nofollow" class="result__a" href="([^"]+)".*?>(.*?)<\/a>[\s\S]*?<a class="result__snippet".*?>(.*?)<\/a>/g;
    let m;
    while ((m = re.exec(html)) && results.length < 3) {
      const href = m[1].replace(/&amp;/g, "&");
      const title = m[2].replace(/<.*?>/g, "").trim();
      const snippet = m[3].replace(/<.*?>/g, "").trim();
      if (title && href) results.push({ title, url: href, snippet });
    }
    return results;
  } catch {
    return [];
  }
}

async function searchEverywhere(query) {
  const q = cleanSearchQuery(query);
  const wiki = await wikiSearchRu(q);
  const ddg = await ddgSearch(q);
  return { query: q, wiki, ddg };
}

function formatSearchAnswer(payload) {
  const q = payload?.query || "";
  const wiki = payload?.wiki;
  const ddg = payload?.ddg || [];

  const lines = [`🔎 Поиск: ${q}`];

  if (wiki) {
    lines.push("");
    lines.push(`📚 Wikipedia: ${wiki.title}`);
    const t = (wiki.text || "").trim();
    lines.push(t.slice(0, 900) + (t.length > 900 ? "…" : ""));
    if (wiki.url) lines.push(`Источник: ${wiki.url}`);
  }

  if (ddg.length) {
    lines.push("");
    lines.push("🌐 Результаты (DuckDuckGo):");
    ddg.forEach((it, i) => {
      lines.push(`${i + 1}) ${it.title}`);
      if (it.snippet) lines.push(`   ${it.snippet}`);
      if (it.url) lines.push(`   ${it.url}`);
    });
  }

  if (!wiki && !ddg.length) {
    lines.push("");
    lines.push("Ничего не нашёл (возможно блок/сеть).");
  }

  return lines.join("\n");
}

// ===========================
// Smart Answer
// ===========================
async function smartAnswer(text, hasImage) {
  const t = (text || "").trim();
  const low = t.toLowerCase();

  const math = solveMath(t);
  if (math) return { text: math, meta: null };

  if (shouldSearch(t)) {
    const payload = await searchEverywhere(t);
    return { text: formatSearchAnswer(payload), meta: payload };
  }

  if (["привет", "привет!", "здравствуй", "здравствуйте", "хай", "yo"].includes(low)) {
    return {
      text:
        "Привет! Я ультра-математический ассистент 🤖🧮\n\n" +
        "Примеры:\n" +
        "• frac{1}{2} + frac{3}{4}\n" +
        "• 20% от 150\n" +
        "• 150 + 20%\n" +
        "• x^2 - 5x + 6 = 0\n" +
        "• 2x+3y=7; x-y=1\n" +
        "• упрости: (x+1)^2 - x^2\n" +
        "• производная: 3x^2 - 2x + 1\n" +
        "• интеграл: 6x - 4\n\n" +
        "Можно отправлять фото с заданиями — я попробую решить всё, что распознаю.",
      meta: null,
    };
  }

  if (hasImage && !t) return { text: "Фото получено 📷\nСейчас попробую прочитать и решить задания.", meta: null };

  return {
    text:
      `Понял ✅\n\n«${t}»\n\n` +
      "Если это математика — пиши пример (можно frac{1}{2}).\n" +
      "Если вопрос — могу поискать и ответить.",
    meta: null,
  };
}

// ===========================
// Job runner
// ===========================
async function runJob(jobId, userId, text, imageDataUrl) {
  try {
    const u = getOrCreateUser(userId);
    if (!u) {
      jobPush(jobId, { type: "error", title: "Сбой", text: "bad userId", ts: nowMs() });
      const job = JOBS.get(jobId);
      if (job) job.done = true;
      return;
    }

    const hasImage = Boolean(imageDataUrl);
    const img = hasImage ? parseDataUrl(imageDataUrl) : null;

    jobPush(jobId, {
      type: "step",
      stage: "input",
      title: "Input received",
      detail: `user=${userId}, text=${(text || "").slice(0, 90) || "∅"}, image=${hasImage ? "yes" : "no"}`,
      ts: nowMs(),
    });

    // Save user message to DB (file)
    pushMessage(u, "user", {
      role: "user",
      text: (text || "").trim(),
      ts: nowMs(),
      imageDataUrl: hasImage ? imageDataUrl : null,
    });
    await saveDb();

    let finalUserText = (text || "").trim();
    let ocrText = "";

    // OCR if image
    if (hasImage) {
      await sleep(120);

      if (!img) {
        jobPush(jobId, { type: "step", stage: "image", title: "Analyzing image", detail: "Bad image data URL", ts: nowMs() });
      } else {
        jobPush(jobId, {
          type: "step",
          stage: "image",
          title: "Analyzing image",
          detail: `Decoded (${img.mime}, ${img.bytes.length} bytes)`,
          ts: nowMs(),
        });

        await sleep(150);
        jobPush(jobId, { type: "step", stage: "ocr", title: "Reading text (OCR)", detail: "Recognizing text…", ts: nowMs() });

        try {
          ocrText = await ocrImageBuffer(img.bytes);
        } catch {
          ocrText = "";
        }

        if (ocrText) {
          jobPush(jobId, {
            type: "step",
            stage: "ocr",
            title: "OCR result",
            detail: ocrText.slice(0, 700) + (ocrText.length > 700 ? "…" : ""),
            ts: nowMs(),
          });

          // объединяем: текст пользователя + OCR
          if (!finalUserText) finalUserText = ocrText;
          else finalUserText = `${finalUserText}\n\n[TEXT_FROM_IMAGE]\n${ocrText}`;
        } else {
          jobPush(jobId, { type: "step", stage: "ocr", title: "OCR result", detail: "No readable text found.", ts: nowMs() });
        }
      }
    }

    await sleep(120);
    jobPush(jobId, { type: "step", stage: "engine", title: "Engine", detail: "Trying to solve everything…", ts: nowMs() });

    // ==========================
    // 🔥 MAIN: If image exists -> solve ALL tasks from OCR
    // ==========================
    let finalText = "";
    let meta = null;

    if (hasImage && ocrText) {
      const solvedPack = solveAllFromTextBlock(ocrText);

      if (solvedPack.length) {
        finalText += "📷 Я прочитал задания с фото и решил всё, что смог:\n\n";

        solvedPack.slice(0, 40).forEach((it, i) => {
          finalText += `#${i + 1}\n🧾 ${it.task}\n✅ ${it.answer}\n\n`;
        });

        if (solvedPack.length > 40) {
          finalText += `…и ещё ${solvedPack.length - 40} заданий (слишком много для одного ответа).\n\n`;
        }

        finalText +=
          "Если что-то не решилось — обычно OCR плохо прочитал строку.\n" +
          "Сфоткай ближе, ровно сверху, без тени и чтобы текст был крупный.";
      } else {
        // если OCR был, но не нашёл задания — fallback
        const ansObj = await smartAnswer(finalUserText, hasImage);
        finalText = ansObj.text;
        meta = ansObj.meta;
      }
    } else {
      // без картинки — обычная логика
      const ansObj = await smartAnswer(finalUserText, hasImage);
      finalText = ansObj.text;
      meta = ansObj.meta;
    }

    // Typing stream (human-like)
    await sleep(60);
    for (const ch of finalText) {
      jobPush(jobId, { type: "delta", delta: ch });
      await sleep(10 + Math.floor(Math.random() * 25));
    }

    if (meta) {
      jobPush(jobId, { type: "step", stage: "search_meta", title: "Search meta", detail: JSON.stringify(meta).slice(0, 1200), ts: nowMs() });
    }

    jobPush(jobId, { type: "final", title: "Ответ", text: finalText, ts: nowMs() });

    // Save AI message
    pushMessage(u, "ai", { role: "ai", text: finalText, ts: nowMs() });
    await saveDb();
  } catch (e) {
    jobPush(jobId, { type: "error", title: "Сбой", text: `Ошибка сервера: ${String(e)}`, ts: nowMs() });
  } finally {
    const job = JOBS.get(jobId);
    if (job) job.done = true;
  }
}

// ===========================
// Routes
// ===========================
app.get("/", (req, res) => {
  const indexPath = path.join(PUBLIC_DIR, "index.html");
  res.sendFile(indexPath, (err) => {
    if (err) {
      res.status(404).send(
        `<h2>Нет public/index.html</h2>
         <p>Положи в папку <b>public</b>: index.html, styles.css, script.js</p>
         <p>Проверь, что <code>/styles.css</code> и <code>/script.js</code> открываются без 404.</p>`
      );
    }
  });
});

// получить “код пользователя” (token)
app.post("/api/user_code", async (req, res) => {
  const userId = safeLower(req.body?.userId);
  if (!userId) return res.status(400).json({ error: "userId required" });

  const u = getOrCreateUser(userId);
  await saveDb();
  res.json({ userId, code: u.token });
});

// история пользователя
app.get("/api/history", (req, res) => {
  const userId = safeLower(req.query?.userId);
  const code = (req.query?.code || "").toString().trim();
  const auth = authUser(userId, code);
  if (!auth.ok) return res.status(401).json({ error: auth.reason });

  const u = auth.user;
  const chats = auth.user.chatOrder.map((cid) => u.chats[cid]).filter(Boolean);
  res.json({ userId, activeChatId: u.activeChatId, chats });
});

// создать новый чат
app.post("/api/new_chat", async (req, res) => {
  const userId = safeLower(req.body?.userId);
  const code = (req.body?.code || "").toString().trim();
  const title = (req.body?.title || "Новый чат").toString().trim();

  const auth = authUser(userId, code);
  if (!auth.ok) return res.status(401).json({ error: auth.reason });

  const u = auth.user;
  const chatId = makeId("chat");
  u.chats[chatId] = { id: chatId, title: title || "Новый чат", createdAt: nowMs(), messages: [] };
  u.chatOrder.unshift(chatId);
  u.activeChatId = chatId;

  await saveDb();
  res.json({ chatId, ok: true });
});

// выбрать чат
app.post("/api/select_chat", async (req, res) => {
  const userId = safeLower(req.body?.userId);
  const code = (req.body?.code || "").toString().trim();
  const chatId = (req.body?.chatId || "").toString().trim();

  const auth = authUser(userId, code);
  if (!auth.ok) return res.status(401).json({ error: auth.reason });

  const u = auth.user;
  if (!u.chats[chatId]) return res.status(404).json({ error: "chat not found" });
  u.activeChatId = chatId;

  await saveDb();
  res.json({ ok: true });
});

// сброс пользователя (удалить все чаты, создать новый)
app.post("/api/reset_user", async (req, res) => {
  const userId = safeLower(req.body?.userId);
  const code = (req.body?.code || "").toString().trim();

  const auth = authUser(userId, code);
  if (!auth.ok) return res.status(401).json({ error: auth.reason });

  const old = auth.user;
  const token = old.token;
  const chatId = makeId("chat");

  DB.users[userId] = {
    token,
    createdAt: nowMs(),
    activeChatId: chatId,
    chats: { [chatId]: { id: chatId, title: "Новый чат", createdAt: nowMs(), messages: [] } },
    chatOrder: [chatId],
  };

  await saveDb();
  res.json({ ok: true });
});

// старт задачи
app.post("/api/solve_start", async (req, res) => {
  const userId = safeLower(req.body?.userId);
  const code = (req.body?.code || "").toString().trim();
  const text = (req.body?.text || "").toString();
  const imageDataUrl = req.body?.imageDataUrl || null;

  const auth = authUser(userId, code);
  if (!auth.ok) return res.status(401).json({ error: auth.reason });

  const jobId = createJob();
  runJob(jobId, userId, text, imageDataUrl); // async
  res.json({ job_id: jobId });
});

// SSE stream
app.get("/api/solve_stream/:jobId", async (req, res) => {
  const jobId = req.params.jobId;
  const job = JOBS.get(jobId);

  res.setHeader("Content-Type", "text/event-stream; charset=utf-8");
  res.setHeader("Cache-Control", "no-cache, no-transform");
  res.setHeader("Connection", "keep-alive");

  if (!job) {
    res.write(ssePack({ type: "error", title: "Сбой", text: "job_id не найден" }));
    res.write(": done\n\n");
    return res.end();
  }

  let closed = false;
  req.on("close", () => (closed = true));

  const pingTimer = setInterval(() => {
    if (!closed) res.write(": ping\n\n");
  }, 10000);
  pingTimer.unref();

  try {
    while (!closed) {
      const ev = await jobWait(jobId);
      if (!ev) break;
      res.write(ssePack(ev));
      if (ev.type === "final" || ev.type === "error") break;
    }
  } finally {
    clearInterval(pingTimer);
    res.write(": done\n\n");
    res.end();
  }
});

app.get("/api/health", (req, res) => {
  res.json({ ok: true, time: new Date().toISOString() });
});

// ===========================
// Start
// ===========================
(async () => {
  await loadDb();
  app.listen(PORT, "0.0.0.0", () => {
    console.log(`✅ Server running: http://127.0.0.1:${PORT}`);
    console.log(`📦 Static folder: ${PUBLIC_DIR}`);
    console.log(`💾 Users DB file: ${USERS_DB_FILE}`);
    console.log(`🔐 Get user code: POST /api/user_code {userId}`);
    console.log(`🔌 Solve: POST /api/solve_start {userId, code, text, imageDataUrl?}`);
    console.log(`📡 SSE:  GET  /api/solve_stream/:job_id`);
  });
})();
