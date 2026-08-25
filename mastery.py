"""Mastery Track — a daily examiner for whatever you are trying to learn.

Every morning it asks you a handful of questions about the topics you are
studying, delivered over Telegram. You answer in your own words. Claude grades
the answer, explains what was missing, and the verdict comes straight back into
the same chat.

What makes it different from a flashcard app: it grades free text against your
own stack, and it tracks a level PER TOPIC. Three flawless independent answers
at the current level move that one topic up. A topic you keep fumbling stays
where it is, and the question comes back days later in a completely different
shape, so the concept is tested rather than the phrasing.

    python mastery.py check              # prove the configuration works
    python mastery.py ask                # send today's questions
    python mastery.py ask --dry-run      # print them, send and store nothing
    python mastery.py poll               # collect answers, grade, reply
    python mastery.py poll --loop        # keep listening (long poll)
    python mastery.py report --days 10   # progress report
    python mastery.py report --telegram  # progress report into the chat
    python mastery.py topics             # level per topic

Configuration lives in a .env file next to this script — see .env.example.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import random
import sys
import time

import httpx

ROOT = pathlib.Path(__file__).resolve().parent

LEVELS = ["foundation", "applied", "advanced", "mastery"]
STREAK_TO_LEVEL_UP = 3
MAINTENANCE_DAYS = 20          # a mastered topic returns only as a spot check
REPEAT_MIN_DAYS, REPEAT_MAX_DAYS = 3, 10
MAX_REPEATS_PER_DAY = 3        # repeats must not eat the whole daily set

VERDICT_ICON = {"correct": "OK", "incomplete": "~", "wrong": "X", "unknown": "i"}


# ---------------------------------------------------------------- config

def env(key: str, default: str | None = None) -> str | None:
    """Real environment wins, then .env next to this script."""
    if os.environ.get(key):
        return os.environ[key]
    f = ROOT / ".env"
    if f.exists():
        for line in f.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if line.startswith(f"{key}=") and not line.startswith("#"):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return default


def need(key: str, hint: str) -> str:
    val = env(key)
    if not val:
        sys.exit(f"missing: {key} in .env — {hint}")
    return val


SLOTS = int(env("MASTERY_QUESTIONS_PER_DAY", "5") or 5)
LANGUAGE = env("MASTERY_LANGUAGE", "English") or "English"


def context() -> str:
    """What you are studying against — your stack, your codebase, your field.

    This is the single most important setting in the file. The examiner is only
    as sharp as the context you give it: 'a Django shop with Celery workers on
    Hetzner' produces better questions than 'web development'.
    """
    path = ROOT / (env("MASTERY_CONTEXT_FILE", "context.md") or "context.md")
    if path.exists():
        text = path.read_text(encoding="utf-8").strip()
        if text:
            return text
    sys.exit(f"missing context: write what you are studying into {path.name} "
             "(see context.example.md)")


def today() -> dt.date:
    return dt.datetime.now().date()


# ---------------------------------------------------------------- supabase

def sb(method: str, path: str, *, params: dict | None = None, json_body=None,
       prefer: str | None = None):
    url = need("MASTERY_SUPABASE_URL", "your project URL").rstrip("/")
    key = need("MASTERY_SUPABASE_SERVICE_KEY",
               "Supabase -> Project Settings -> API keys -> secret key")
    headers = {"apikey": key, "Authorization": f"Bearer {key}",
               "Content-Type": "application/json"}
    if prefer:
        headers["Prefer"] = prefer
    r = httpx.request(method, f"{url}/rest/v1/{path}", headers=headers,
                      params=params, json=json_body, timeout=30.0)
    if r.status_code >= 300:
        sys.exit(f"supabase {method} {path} -> {r.status_code}: {r.text[:400]}")
    return r.json() if r.text.strip() else []


def sb_insert(table: str, rows: list[dict]) -> list[dict]:
    return sb("POST", table, json_body=rows, prefer="return=representation")


def sb_update(table: str, params: dict, patch: dict) -> list[dict]:
    return sb("PATCH", table, params=params, json_body=patch,
              prefer="return=representation")


def state_get(key: str, default=None):
    rows = sb("GET", "mastery_state", params={"key": f"eq.{key}", "select": "value"})
    return rows[0]["value"] if rows else default


def state_set(key: str, value) -> None:
    sb("POST", "mastery_state",
       json_body=[{"key": key, "value": value,
                   "updated_at": dt.datetime.now(dt.timezone.utc).isoformat()}],
       prefer="resolution=merge-duplicates")


# ---------------------------------------------------------------- telegram

def tg(method: str, payload: dict, *, timeout: float = 30.0) -> dict:
    token = need("MASTERY_TELEGRAM_BOT_TOKEN", "create a bot with @BotFather")
    r = httpx.post(f"https://api.telegram.org/bot{token}/{method}",
                   json=payload, timeout=timeout)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"telegram {method}: {data}")
    return data["result"]


def chunks(text: str, size: int = 3900):
    while text:
        cut = text[:size]
        if len(text) > size:
            brk = cut.rfind("\n")
            if brk > size // 2:
                cut = cut[:brk]
        yield cut
        text = text[len(cut):].lstrip("\n")


def tg_send(text: str) -> int | None:
    """Plain text on purpose — Markdown breaks on underscores and braces."""
    chat_id = need("MASTERY_TELEGRAM_CHAT_ID", "message your bot, then run `check`")
    last = None
    for part in chunks(text):
        res = tg("sendMessage", {"chat_id": chat_id, "text": part,
                                 "disable_web_page_preview": True})
        last = res.get("message_id")
    return last


# ---------------------------------------------------------------- claude

def claude(system: str, prompt: str, schema: dict) -> dict:
    import anthropic

    key = env("ANTHROPIC_API_KEY") or need(
        "MASTERY_ANTHROPIC_API_KEY", "console.anthropic.com -> API keys")
    client = anthropic.Anthropic(api_key=key)
    resp = client.messages.create(
        model=env("MASTERY_MODEL", "claude-opus-5"),
        max_tokens=16000,
        thinking={"type": "adaptive"},
        system=system,
        messages=[{"role": "user", "content": prompt}],
        output_config={"format": {"type": "json_schema", "schema": schema}},
    )
    text = next(b.text for b in resp.content if b.type == "text")
    return json.loads(text)


# ---------------------------------------------------------------- prompts

LEVEL_DESC = {
    "foundation": "definitions, mechanism, when you reach for it — knowledge without application",
    "applied": "solve one concrete scenario from the learner's own context using this topic",
    "advanced": "trade-offs, failure modes, reading a log or config to find the fault, interactions with neighbouring parts",
    "mastery": "defend a design decision, edge cases, what breaks at scale and why",
}


def ask_system() -> str:
    return (
        "You are a demanding examiner teaching one learner. Your questions are precise, "
        "answerable, and never padded with preamble.\n"
        f"Write every question in {LANGUAGE}.\n\n"
        f"What the learner is studying against:\n{context()}\n\n"
        "Rules for the questions:\n"
        "- Alternate between concept questions, applied scenarios from the learner's own "
        "context, and fault-analysis exercises.\n"
        "- Each question tests exactly one underlying concept and is answerable in one "
        "paragraph.\n"
        "- Never multiple choice, never yes/no.\n"
        "- Stay exactly at the level given — do not reach for the level above.\n"
        "- 'expected' is your own answer key: the points a flawless answer contains.\n"
        "- For a repeat: different scenario, different angle, different question type, so it "
        "is not recognisable as a repeat. The underlying concept stays identical."
    )


ASK_SCHEMA = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "slot": {"type": "integer"},
                    "concept": {"type": "string"},
                    "question": {"type": "string"},
                    "expected": {"type": "string"},
                },
                "required": ["slot", "concept", "question", "expected"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["questions"],
    "additionalProperties": False,
}


def grade_system() -> str:
    return (
        "You grade one answer to one exam question. Short and direct, no compliments, no "
        "preamble.\n"
        f"Write your response in {LANGUAGE}.\n\n"
        f"What the learner is studying against:\n{context()}\n\n"
        "verdict:\n"
        "- correct    = every key point present and nothing factually wrong\n"
        "- incomplete = what is there is right, but key points are missing\n"
        "- wrong      = factually incorrect, or the wrong mechanism entirely\n"
        "- unknown    = the learner says they don't know, openly guesses, or asks for the answer\n\n"
        "independent = false as soon as the answer is 'I don't know', an open guess, or merely "
        "restates the question.\n"
        "explanation: at most three sentences when correct. When incomplete or wrong, name "
        "exactly what was missing or false, and why. When unknown, there is no penalty and no "
        "reproach — teach the concept calmly and completely (mechanism, when it applies, the "
        "trap, one concrete example from the learner's own context) so that it is understood "
        "afterwards.\n"
        "improvements: concrete actions or facts to remember, at most three, one line each."
    )


GRADE_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["correct", "incomplete", "wrong", "unknown"]},
        "independent": {"type": "boolean"},
        "explanation": {"type": "string"},
        "improvements": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["verdict", "independent", "explanation", "improvements"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------- planning

def load_topics() -> list[dict]:
    return sb("GET", "mastery_topics",
              params={"active": "is.true", "select": "*", "order": "slug"})


def open_items() -> list[dict]:
    return sb("GET", "mastery_questions",
              params={"status": "eq.open", "select": "*",
                      "order": "repeat_due_on.asc,id.asc"})


def unanswered() -> list[dict]:
    """Everything still waiting for an answer — this blocks the next set."""
    return sb("GET", "mastery_questions",
              params={"answer": "is.null", "select": "*",
                      "order": "asked_on.asc,slot.asc"})


def sent_today() -> int:
    return len(sb("GET", "mastery_questions",
                  params={"asked_on": f"eq.{today().isoformat()}", "select": "id"}))


def level_index(level: str) -> int:
    return LEVELS.index(level) if level in LEVELS else 0


def schedule_repeat() -> dt.date:
    return today() + dt.timedelta(days=random.randint(REPEAT_MIN_DAYS, REPEAT_MAX_DAYS))


def build_plan() -> list[dict]:
    """Pick today's slots: due repeats first, then new ground.

    Never skip a step: a topic with open items is questioned at the lowest level
    still open, not at the topic's current level. Otherwise you drift towards
    mastery on a foundation you never actually secured.
    """
    topics = {t["slug"]: t for t in load_topics()}
    opens = open_items()
    open_by_topic: dict[str, list[dict]] = {}
    for q in opens:
        open_by_topic.setdefault(q["topic"], []).append(q)

    plan: list[dict] = []
    due = [q for q in opens
           if q.get("repeat_due_on") and q["repeat_due_on"] <= today().isoformat()]
    for q in due[:MAX_REPEATS_PER_DAY]:
        if q["topic"] not in topics:
            continue
        plan.append({
            "topic": q["topic"], "label": topics[q["topic"]]["label"],
            "level": q["level"], "concept": q["concept"], "kind": "repeat",
            "repeat_of": q["id"], "attempts": q["attempts"] + 1,
            "prev_question": q["question"],
            "prev_answer": q.get("answer") or "(never answered)",
            "prev_feedback": q.get("feedback") or "",
        })

    # candidates for new ground: a mastered topic only as a spot check
    cands = []
    for t in topics.values():
        last = t.get("last_asked_on")
        if t["level"] == "mastery":
            if last and (today() - dt.date.fromisoformat(last)).days < MAINTENANCE_DAYS:
                continue
        cands.append(t)
    # longest untouched first; topics carrying open items get priority
    cands.sort(key=lambda t: (
        0 if open_by_topic.get(t["slug"]) else 1,
        t.get("last_asked_on") or "0000-00-00",
        t["slug"],
    ))

    used = [p["topic"] for p in plan]
    limit = 1 if len(cands) >= SLOTS else 2
    guard = 0
    while len(plan) < SLOTS and cands and guard < len(cands) * 4:
        t = cands[guard % len(cands)]
        guard += 1
        if used.count(t["slug"]) >= limit:
            continue
        level = t["level"]
        stuck = open_by_topic.get(t["slug"])
        if stuck:
            lowest = min(stuck, key=lambda q: level_index(q["level"]))["level"]
            if level_index(lowest) < level_index(level):
                level = lowest
        plan.append({"topic": t["slug"], "label": t["label"], "level": level,
                     "concept": None, "kind": "new", "repeat_of": None,
                     "attempts": 1})
        used.append(t["slug"])

    for n, p in enumerate(plan, 1):
        p["slot"] = n
    return plan


def generate_questions(plan: list[dict]) -> list[dict]:
    recent = sb("GET", "mastery_questions",
                params={"select": "topic,concept,question", "order": "id.desc",
                        "limit": "25"})
    lines = []
    for p in plan:
        head = (f"slot {p['slot']} | topic: {p['topic']} ({p['label']}) | "
                f"level: {p['level']} — {LEVEL_DESC[p['level']]}")
        if p["kind"] == "repeat":
            lines.append(
                head + "\n  TYPE: REPEAT of a concept that is not yet secure.\n"
                f"  concept (must stay identical): {p['concept']}\n"
                f"  earlier phrasing (do NOT reuse): {p['prev_question']}\n"
                f"  the learner's earlier answer: {str(p['prev_answer'])[:400]}\n"
                f"  earlier feedback: {str(p['prev_feedback'])[:400]}\n"
                "  Write a completely different question about the same concept: a different "
                "scenario, a different angle, a different question type."
            )
        else:
            lines.append(head + "\n  TYPE: NEW. Pick a sharp concept inside this topic at "
                                "this level.")
    avoid = "\n".join(f"- [{r['topic']}] {r['concept']}: {r['question'][:120]}"
                      for r in recent)
    prompt = (
        "Write today's questions. One question per slot, exactly these slots:\n\n"
        + "\n\n".join(lines)
        + "\n\nAvoid recognisable overlap with these recently asked questions:\n"
        + (avoid or "(none)")
    )
    out = claude(ask_system(), prompt, ASK_SCHEMA)
    by_slot = {int(q["slot"]): q for q in out["questions"]}
    result = []
    for p in plan:
        q = by_slot.get(p["slot"])
        if not q:
            sys.exit(f"the model returned no question for slot {p['slot']}")
        result.append({**p, "concept": p["concept"] or q["concept"],
                       "question": q["question"], "expected": q["expected"]})
    return result


# ---------------------------------------------------------------- commands

def cmd_ask(args) -> None:
    auto = getattr(args, "auto", False)
    if not args.dry_run and not args.force:
        pend = unanswered()
        if pend:
            # One set at a time: a new list waits until the previous one is done.
            print(f"blocked: {len(pend)} question(s) still unanswered")
            if not auto:
                tg_send(
                    f"{len(pend)} still unanswered. The next set waits until these are done.\n\n"
                    + "\n\n".join(f"{q['slot']}. [{q['topic']} · {q['level']}] {q['question']}"
                                  for q in pend[:SLOTS])
                )
            return
        if sent_today():
            print("a set already went out today — next round tomorrow morning")
            return

    plan = build_plan()
    if not plan:
        sys.exit("no active topics — seed the mastery_topics table")
    questions = generate_questions(plan)

    if args.dry_run:
        for q in questions:
            print(f"\n--- slot {q['slot']} · {q['topic']} · {q['level']} · {q['kind']}")
            print(f"concept  : {q['concept']}")
            print(f"question : {q['question']}")
            print(f"key      : {q['expected'][:300]}")
        return

    tg_send(f"Mastery — {today().strftime('%d-%m-%Y')}\n"
            f"{len(questions)} questions. Answer with \"1. ...\" or reply to a question. "
            "\"I don't know\" is allowed — you get the explanation, not a penalty.")

    for q in questions:
        row = sb_insert("mastery_questions", [{
            "asked_on": today().isoformat(), "slot": q["slot"], "topic": q["topic"],
            "level": q["level"], "concept": q["concept"], "question": q["question"],
            "expected": q["expected"], "attempts": q["attempts"],
            "repeat_of": q["repeat_of"], "status": "new",
        }])[0]
        mid = tg_send(f"Question {q['slot']}/{len(questions)} · {q['label']} · {q['level']}"
                      f"\n\n{q['question']}")
        if mid:
            sb_update("mastery_questions", {"id": f"eq.{row['id']}"},
                      {"telegram_message_id": mid})
        sb_update("mastery_topics", {"slug": f"eq.{q['topic']}"},
                  {"last_asked_on": today().isoformat()})
        print(f"slot {q['slot']} · {q['topic']} · {q['level']} · {q['kind']} -> sent")


def find_question(update_msg: dict) -> dict | None:
    """Reply-to wins, then a leading slot number, then the oldest open question."""
    reply = (update_msg.get("reply_to_message") or {}).get("message_id")
    if reply:
        rows = sb("GET", "mastery_questions",
                  params={"telegram_message_id": f"eq.{reply}", "select": "*"})
        if rows:
            return rows[0]

    text = (update_msg.get("text") or "").strip()
    pending = sb("GET", "mastery_questions",
                 params={"answer": "is.null", "select": "*",
                         "order": "asked_on.desc,slot.asc", "limit": "10"})
    if not pending:
        return None

    head = text[:2].rstrip(".):- ")
    if head.isdigit():
        n = int(head)
        newest = pending[0]["asked_on"]
        for row in pending:
            if row["asked_on"] == newest and row["slot"] == n:
                return row
    return sorted(pending, key=lambda r: (r["asked_on"], r["slot"] or 0))[0]


def grade_answer(q: dict, answer: str) -> dict:
    prompt = (
        f"Topic: {q['topic']} | level: {q['level']} — {LEVEL_DESC[q['level']]}\n"
        f"Concept: {q['concept']}\n\n"
        f"QUESTION:\n{q['question']}\n\n"
        f"ANSWER KEY (the points that matter):\n{q.get('expected') or '(none)'}\n\n"
        f"THE LEARNER'S ANSWER:\n{answer}"
    )
    return claude(grade_system(), prompt, GRADE_SCHEMA)


def apply_result(q: dict, answer: str, res: dict) -> str:
    verdict = res["verdict"]
    independent = bool(res["independent"]) and verdict != "unknown"
    flawless = verdict == "correct" and independent

    now = dt.datetime.now(dt.timezone.utc).isoformat()
    patch = {
        "answer": answer,
        "answered_at": now,
        "verdict": verdict,
        "feedback": res["explanation"],
        "status": "mastered" if flawless else "open",
        "graded_at": now,
        "repeat_due_on": None if flawless else schedule_repeat().isoformat(),
    }
    sb_update("mastery_questions", {"id": f"eq.{q['id']}"}, patch)

    # carry the whole chain of rephrasings with it
    parent = q.get("repeat_of")
    while parent:
        rows = sb_update("mastery_questions", {"id": f"eq.{parent}"},
                         {"status": "mastered" if flawless else "open",
                          "repeat_due_on": None if flawless else patch["repeat_due_on"]})
        parent = rows[0].get("repeat_of") if rows else None

    trow = sb("GET", "mastery_topics",
              params={"slug": f"eq.{q['topic']}", "select": "*"})[0]
    note = ""
    if flawless:
        streak = trow["streak"] + 1
        if streak >= STREAK_TO_LEVEL_UP and trow["level"] != "mastery":
            new_level = LEVELS[level_index(trow["level"]) + 1]
            upd = {"level": new_level, "streak": 0}
            if new_level == "mastery":
                upd["mastered_at"] = now
            sb_update("mastery_topics", {"slug": f"eq.{q['topic']}"}, upd)
            note = f"\n\nLevel {q['topic']}: {trow['level']} -> {new_level}"
        else:
            sb_update("mastery_topics", {"slug": f"eq.{q['topic']}"}, {"streak": streak})
            if trow["level"] != "mastery":
                note = (f"\n\n{q['topic']} · {trow['level']} · "
                        f"streak {streak}/{STREAK_TO_LEVEL_UP}")
    else:
        if trow["streak"]:
            sb_update("mastery_topics", {"slug": f"eq.{q['topic']}"}, {"streak": 0})
        note = f"\n\nComes back around {patch['repeat_due_on']}, in a different shape."
    return note


def reply_for(q: dict, res: dict, note: str) -> str:
    icon = VERDICT_ICON.get(res["verdict"], "-")
    head = f"[{icon}] Question {q['slot']} · {q['topic']} · {res['verdict']}"
    points = "\n".join(f"- {p}" for p in res.get("improvements") or [])
    if points:
        points = f"\n\nImprove:\n{points}"
    return f"{head}\n\n{res['explanation']}{points}{note}"


def handle_command(text: str) -> bool:
    cmd = text.split()[0].lower().lstrip("/").split("@")[0]
    if cmd in ("start", "help"):
        tg_send("Mastery Track.\n\nEvery morning a set of questions. Answer with "
                "\"1. ...\" or reply to a question. \"I don't know\" is allowed.\n\n"
                "/open   — questions still waiting\n"
                "/levels — level per topic\n"
                "/report — progress over the last 30 days")
        return True
    if cmd == "levels":
        rows = sb("GET", "mastery_report", params={"select": "*", "order": "topic"})
        tg_send("Level per topic\n\n" + "\n".join(
            f"{r['topic']}: {r['level']} (streak {r['streak']}/3, "
            f"{r['mastered'] or 0} mastered, {r['open_items'] or 0} open)" for r in rows))
        return True
    if cmd == "report":
        tg_send(report_text(30))
        return True
    if cmd == "open":
        rows = sb("GET", "mastery_questions",
                  params={"answer": "is.null", "select": "*",
                          "order": "asked_on.desc,slot.asc", "limit": str(SLOTS)})
        if not rows:
            tg_send("Nothing open.")
        else:
            tg_send("Still to answer:\n\n" + "\n\n".join(
                f"{r['slot']}. [{r['topic']} · {r['level']}] {r['question']}" for r in rows))
        return True
    return False


def process_updates(updates: list[dict]) -> int:
    chat_id = str(need("MASTERY_TELEGRAM_CHAT_ID", "run `check`"))
    handled = 0
    for u in updates:
        msg = u.get("message") or u.get("edited_message") or {}
        text = (msg.get("text") or "").strip()
        if not text or str((msg.get("chat") or {}).get("id")) != chat_id:
            continue
        if text.startswith("/") and handle_command(text):
            handled += 1
            continue
        q = find_question(msg)
        if not q:
            tg_send("No open question to attach this answer to.")
            continue
        if q.get("answer"):
            # Telegram redelivers a message often enough (edits, retries) that
            # grading twice would cost real money and muddle the streak.
            if (q["answer"] or "").strip() != text:
                tg_send(f"Question {q['slot']} was already graded — an edited answer no "
                        "longer counts. The concept will come back on its own.")
            print(f"skipped: question {q['id']} was already graded")
            continue
        res = grade_answer(q, text)
        note = apply_result(q, text, res)
        tg_send(reply_for(q, res, note))
        print(f"graded: question {q['id']} ({q['topic']}) -> {res['verdict']}")
        handled += 1
    return handled


def cmd_poll(args) -> None:
    while True:
        offset = state_get("telegram_offset", 0) or 0
        try:
            updates = tg("getUpdates",
                         {"offset": offset, "timeout": 25 if args.loop else 0,
                          "allowed_updates": ["message", "edited_message"]},
                         timeout=40.0)
        except (httpx.HTTPError, RuntimeError) as exc:
            print(f"getUpdates failed: {exc}", file=sys.stderr)
            if not args.loop:
                sys.exit(1)
            time.sleep(15)
            continue
        if updates:
            state_set("telegram_offset", updates[-1]["update_id"] + 1)
            if process_updates(updates) and not unanswered() and not sent_today() \
                    and 6 <= dt.datetime.now().hour < 22:
                # backlog cleared during the day: the new set may go out after all,
                # at most one set per calendar day.
                print("backlog cleared -> sending the next set")
                cmd_ask(argparse.Namespace(dry_run=False, force=False, auto=True))
        elif not args.loop:
            print("nothing new")
        if not args.loop:
            return


def report_text(days: int) -> str:
    since = (today() - dt.timedelta(days=days)).isoformat()
    rows = sb("GET", "mastery_report", params={"select": "*", "order": "topic"})
    window = sb("GET", "mastery_questions",
                params={"asked_on": f"gte.{since}",
                        "select": "topic,status,verdict,attempts"})
    per: dict[str, dict] = {}
    for q in window:
        d = per.setdefault(q["topic"],
                           {"asked": 0, "mastered": 0, "open": 0, "unknown": 0})
        d["asked"] += 1
        if q["status"] == "mastered":
            d["mastered"] += 1
        elif q["status"] == "open":
            d["open"] += 1
        if q["verdict"] == "unknown":
            d["unknown"] += 1

    def weakest(r):
        w = per.get(r["topic"], {})
        return (-w.get("open", 0), level_index(r["level"]),
                -(r["avg_attempts_to_mastery"] or 0))

    lines = [f"Mastery — last {days} days ({since} -> {today().isoformat()})", ""]
    for r in sorted(rows, key=weakest):
        w = per.get(r["topic"], {"asked": 0, "mastered": 0, "open": 0, "unknown": 0})
        lines.append(
            f"{r['topic']} · {r['level']} (streak {r['streak']}/3)\n"
            f"  window: {w['asked']} asked, {w['mastered']} mastered, "
            f"{w['open']} open, {w['unknown']}x don't-know\n"
            f"  total : {r['mastered'] or 0} mastered / {r['asked'] or 0} asked, "
            f"avg {r['avg_attempts_to_mastery'] or '-'} attempts to mastery"
        )
    weak = [r["topic"] for r in sorted(rows, key=weakest)[:3]]
    lines += ["", "Weakest topics first: " + ", ".join(weak)]
    return "\n".join(lines)


def cmd_report(args) -> None:
    text = report_text(args.days)
    print(text)
    if args.telegram:
        tg_send(text)


def cmd_topics(_args) -> None:
    for r in sb("GET", "mastery_report", params={"select": "*", "order": "topic"}):
        print(f"{r['topic']:16} {r['level']:11} streak {r['streak']}/3  "
              f"mastered {r['mastered'] or 0:3}  open {r['open_items'] or 0:3}  "
              f"last {r['last_asked_on'] or '-'}")


def cmd_check(_args) -> None:
    ok = True
    for key, hint in [("MASTERY_TELEGRAM_BOT_TOKEN", "@BotFather"),
                      ("MASTERY_TELEGRAM_CHAT_ID", "message your bot first"),
                      ("MASTERY_SUPABASE_URL", "your project URL"),
                      ("MASTERY_SUPABASE_SERVICE_KEY", "secret / service_role key"),
                      ("ANTHROPIC_API_KEY", "console.anthropic.com")]:
        val = env(key) or (env("MASTERY_ANTHROPIC_API_KEY")
                           if key == "ANTHROPIC_API_KEY" else None)
        print(f"{'OK ' if val else 'MIS'} {key}" + ("" if val else f"  <- {hint}"))
        ok = ok and bool(val)

    path = ROOT / (env("MASTERY_CONTEXT_FILE", "context.md") or "context.md")
    print(f"{'OK ' if path.exists() else 'MIS'} {path.name}"
          + ("" if path.exists() else "  <- copy context.example.md and fill it in"))
    ok = ok and path.exists()

    if env("MASTERY_TELEGRAM_BOT_TOKEN"):
        try:
            print(f"OK  telegram bot @{tg('getMe', {}).get('username')}")
        except Exception as exc:
            ok = False
            print(f"MIS telegram: {exc}")
    if env("MASTERY_SUPABASE_URL") and env("MASTERY_SUPABASE_SERVICE_KEY"):
        print(f"OK  supabase: {len(load_topics())} active topics")
    if ok:
        tg_send("Mastery Track is wired up. First set on the next scheduled run.")
        print("OK  test message sent")
    sys.exit(0 if ok else 1)


def main() -> None:
    p = argparse.ArgumentParser(description="Mastery Track — a daily examiner")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("ask", help="generate and send today's questions")
    a.add_argument("--dry-run", action="store_true")
    a.add_argument("--force", action="store_true",
                   help="send even while questions are still open")
    a.set_defaults(func=cmd_ask)

    b = sub.add_parser("poll", help="collect answers, grade them, reply")
    b.add_argument("--loop", action="store_true", help="keep listening")
    b.set_defaults(func=cmd_poll)

    c = sub.add_parser("report", help="progress report")
    c.add_argument("--days", type=int, default=10)
    c.add_argument("--telegram", action="store_true")
    c.set_defaults(func=cmd_report)

    sub.add_parser("topics", help="level per topic").set_defaults(func=cmd_topics)
    sub.add_parser("check", help="prove the configuration works").set_defaults(func=cmd_check)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
