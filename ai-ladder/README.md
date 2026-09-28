# 🪜 AI Ladder

**Classroom software that teaches students to understand and use AI — from kindergarten through a
bachelor's degree — and ranks each student's learning on one continuous scale.**

Students play short, age-appropriate games and challenges. Every answer updates a skill rating, so
teachers see at a glance where each student is working ("3rd grader, but reasoning at a middle-school
level") and which skill the class needs next.

## Run it

```bash
cd ai-ladder
python3 server.py          # open http://localhost:8080
python3 test_ladder.py     # 15 tests: content, grading, ratings, full HTTP classroom flow
```

Only Python 3.9+ is required: no packages, no build step, no internet. With Docker:

```bash
docker build -t ai-ladder ai-ladder
docker run -p 8080:8080 -v ladder-data:/data ai-ladder
```

## The ladder: six levels

| Level | Name | Grades | What it feels like |
|---|---|---|---|
| 1 | Spark | K-2 | Big text, emoji, read-aloud 🔊, tap-to-answer. "Teach the robot which animals fly." |
| 2 | Explorer | 3-5 | Pixels, training data, safe sharing, first prompts. |
| 3 | Builder | 6-8 | Build a spam filter, spot AI hallucinations, bias in hiring AI, deepfakes. |
| 4 | Engineer | 9-12 | CNNs, embeddings, imbalanced data, PII, few-shot prompting. |
| 5 | Scholar | College yr 1-2 | Convolution math, A*, precision/recall, tune gradient descent, RAG, fairness metrics. |
| 6 | Researcher | College yr 3-4 | ViT, CLIP, Bayes, Adam, RLHF, fairness impossibility results, EU AI Act, agent security. |

## Six skills

Five come from the **AI4K12 "Five Big Ideas in AI"**, the framework most US state AI-education
guidance builds on. The sixth covers hands-on use of AI tools:

👁️ Perception · 🧩 Representation & Reasoning · 📈 Learning · 💬 Natural Interaction ·
⚖️ Societal Impact · 🛠️ Using AI Tools (prompting, checking AI's work)

## Seven kinds of challenges (97 in the bank)

| Type | What the student does |
|---|---|
| Multiple choice | Tap an answer (auto-submits, which suits little kids). |
| Sort | Put each card in the right bin ("Safe to share" / "Keep private"). |
| Order | Tap steps into the right order (partial credit for near-misses). |
| Spot the hallucination | Tap every false sentence in an AI-written answer. |
| Prompt builder | Assemble a prompt from parts; scored against a rubric, with a stronger option shown for each part. |
| **Teach the robot** | Label training examples; a **real nearest-neighbour model** trained on *your* labels is tested on new examples. Mislabel, and the robot learns your mistakes. |
| **Tune gradient descent** | Pick a learning rate and watch the loss curve converge or blow up. |

## How ranking works

- Every student has a rating per skill, and every challenge a difficulty, on **one scale**:
  K-2 starts at 500, a bachelor's degree at 2000.
- Ratings update **Elo-style** with partial credit: beating a hard challenge moves you up a lot,
  missing an easy one moves you down. New students move fast (placement), then settle.
- The next challenge is **adaptive**. It picks the skill you've practised least, at a difficulty
  slightly below your rating (about 64% expected success), from one level below your class band to
  two above. It won't repeat a challenge you saw in your last 20.
- The **overall level** is the average of all six skills, so breadth matters.
- **Leaderboards show growth next to rank**, so improvement counts. They're off by default for
  K-5, and teachers can toggle them for any class.
- Answers never reach the browser before submission, grading happens on the server, and each
  student can only answer the one challenge they were issued. A student can't farm their rating by
  resubmitting.

## Teacher features

Create a class in 30 seconds and share a 6-letter class code. The dashboard shows:

- every student's level and growth
- a per-skill heatmap
- the class's weakest skill
- a CSV export for gradebooks
- printable login cards
- one-click permanent deletion of a student's data

## Privacy and school adoption

- **No PII collected.** Students have a nickname and a login code. There's no email, birthday,
  real name, or third-party login. This keeps the design inside the spirit of **COPPA** (under-13)
  and **FERPA** (education records). A district should still run its own review and sign a data
  privacy agreement before deployment.
- **No outside AI calls, trackers, ads, or CDNs.** Everything is served from your own server, under
  a strict Content-Security-Policy. The app works on filtered school networks, locked-down
  Chromebooks, iPads, and phones.
- **Accessible.** Keyboard navigable, large touch targets, read-aloud via the browser's built-in
  speech, dark mode, and reduced reading load at the youngest level.
- **Cheap to host.** One process and one SQLite file. A $5/month box or a school server handles a
  whole building.

## Files

| File | Contents |
|---|---|
| `content.py` | Levels, skills, and the full challenge bank. Add challenges here. |
| `grading.py` | Serves shuffled challenges and grades them, including the kNN and gradient-descent simulators. |
| `rating.py` | The Elo rating scale, levels, and adaptive targeting. |
| `server.py` | HTTP API and SQLite storage, standard library only. |
| `static/` | The single-page web app (plain JavaScript). |
| `test_ladder.py` | Tests. |

## What's next (not built yet)

- **Clever / ClassLink / Google Classroom rostering and SSO.** Most US districts require one of
  these to adopt at scale.
- **Live AI sandbox.** A moderated chatbot for grades 6+ where students practise prompting against
  a real model, gated by teacher approval and a district-signed data agreement.
- **Content growth.** 97 challenges is a working core. Target 50+ per level, with educator review,
  plus Spanish translation.
- **Standards mapping report.** Map challenges to each state's CS/AI standards and to the
  CSTA K-12 standards, and give teachers a printable per-student progress report for parents.
- **Multi-teacher schools.** School and district admin accounts, aggregate reporting, and teacher
  key rotation.
