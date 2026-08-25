# Mastery Track

A daily examiner that lives in your Telegram. Every morning it asks you a few
questions about the things you are trying to learn, you answer in your own
words, and Claude grades the answer and explains what you missed.

It is not a flashcard app. Flashcards test whether you *recognise* an answer.
This tests whether you can *explain* one, in free text, about your own stack —
and it keeps a separate level for every topic, so the thing you keep fumbling
stays in front of you while the thing you know gets out of the way.

```
Question 3/5 · Claude Code hooks · foundation

Describe what a Claude Code hook is, which data a PreToolUse hook receives when
it fires and through which channel, and how the exit status of the hook script
decides whether the tool call proceeds or is blocked.
```

You reply. Twenty seconds later:

```
[~] Question 3 · hooks · incomplete

You have the event right, but not the channel: the hook receives its payload as
JSON on stdin, not as arguments. That matters because it is how you read the
tool input before deciding. Exit code 2 blocks the call and returns stderr to
the model; any other non-zero code is a warning that does not block.

Improve:
- Hook payload arrives as JSON on stdin
- Exit 2 is the only blocking code

hooks · foundation · streak 0/3
Comes back around 2026-09-02, in a different shape.
```

## Why per-topic levels

Every topic sits at one of four levels — `foundation`, `applied`, `advanced`,
`mastery` — and moves independently.

- **Three flawless independent answers** at the current level move that topic up.
  One wrong, incomplete, or "I don't know" resets the streak.
- **No skipping.** If a topic still has open items at a lower level, that is
  where the next question comes from. You do not drift towards mastery on a
  foundation you never secured.
- **"I don't know" costs nothing** but the streak. You get the concept explained
  properly — mechanism, when it applies, the trap, an example from your own
  context — instead of a red cross.
- **Open questions come back** on a random day 3–10 days later, rewritten from
  scratch: different scenario, different angle, different question type. The
  concept is identical; the phrasing is unrecognisable, so you cannot pass on
  memory of the wording. Only a flawless answer to the rewritten version marks
  the concept mastered.
- **A mastered topic goes quiet**, returning about once every 20 days as a spot
  check, and hands its slots to topics that still need them.
- **One set at a time.** Tomorrow's questions wait until today's are answered,
  so you never wake up to a backlog of forty unread questions and quit.

## Setup

Five minutes, and it costs nothing beyond Claude API usage.

**1. A Telegram bot.** Message [@BotFather](https://t.me/botfather), send
`/newbot`, keep the token. Then message your new bot once — it cannot see your
chat until you do.

**2. A Supabase project.** Free tier is plenty. Open the SQL editor, paste
[`schema.sql`](schema.sql), run it. Then Project Settings → API keys → copy the
**secret** key (`sb_secret_…`, or the legacy `service_role` JWT). Not the anon
key: RLS is on with no policies precisely so that the public key cannot read
your answers.

**3. Configure.**

```bash
cp .env.example .env          # fill in the four required values
cp context.example.md context.md   # describe what you are studying against
pip install -r requirements.txt
```

`context.md` is the setting that decides whether this is useful or generic. Name
your actual stack. The examiner builds scenarios out of whatever you put there.

**4. Prove it works.**

```bash
python mastery.py check
```

It checks every credential, resolves your chat id, and sends a test message. If
`MASTERY_TELEGRAM_CHAT_ID` is still empty, run `check` once, look at the error,
then find your id by messaging the bot and calling
`https://api.telegram.org/bot<TOKEN>/getUpdates`.

**5. Schedule it.** Two jobs: one that asks, one that collects answers.

Linux/macOS, `crontab -e`:

```cron
0 7 * * *      cd /path/to/mastery-track && python mastery.py ask  >> mastery.log 2>&1
0 6,9,12,15,18,21 * * *  cd /path/to/mastery-track && python mastery.py poll >> mastery.log 2>&1
```

Windows, PowerShell:

```powershell
$py = (Get-Command python).Source
$dir = "C:\path\to\mastery-track"
Register-ScheduledTask -TaskName "Mastery Ask" -Force `
  -Action (New-ScheduledTaskAction -Execute $py -Argument "mastery.py ask" -WorkingDirectory $dir) `
  -Trigger (New-ScheduledTaskTrigger -Daily -At '07:00')
```

Or skip cron entirely and run `python mastery.py poll --loop` as a long-lived
process; it long-polls Telegram and answers within seconds.

## Commands

```bash
python mastery.py check              # prove the configuration works
python mastery.py ask                # send today's questions
python mastery.py ask --dry-run      # print them, send and store nothing
python mastery.py ask --force        # send even while questions are open
python mastery.py poll               # collect answers, grade, reply
python mastery.py poll --loop        # keep listening
python mastery.py report --days 30   # progress: level, mastered, weakest topics
python mastery.py topics             # level per topic
```

In the chat itself: `/open`, `/levels`, `/report`, `/help`.

## Answering

Any of these work, in this order of precedence:

1. **Reply** to the question message in Telegram.
2. Start with the slot number: `3. a hook is a shell command that …`
3. Just answer — it attaches to the oldest unanswered question.

A question that has already been graded is never graded twice. Telegram
redelivers messages often enough (edits, retries) that this would otherwise
cost real money and quietly corrupt your streak.

## Cost

One Claude call for the daily set, one per answer graded. On `claude-opus-5`
that is roughly **$0.15–0.20 a day** for five questions and five answers. Set
`MASTERY_MODEL=claude-sonnet-5` to cut it by about two thirds; the questions get
slightly less sharp and the grading slightly more forgiving.

Telegram is free. Supabase free tier is far more than this needs — the whole
thing is three small tables.

## Data model

| Table | What it holds |
|---|---|
| `mastery_topics` | one row per topic: level, streak, when it was last asked |
| `mastery_questions` | question, your answer, verdict, feedback, status, attempts, and the chain of rewrites (`repeat_of`, `repeat_due_on`) |
| `mastery_state` | the Telegram update offset |
| `mastery_report` | a view: mastered vs open per topic, average attempts to mastery |

Everything is plain Postgres. Query it, chart it, export it — it is your data.

## Support

None. This is published because it might be useful, not because it is a product.
Copy it, fork it, rip the grading loop out and put it somewhere else. Issues and
pull requests may sit unread.

MIT licensed.
