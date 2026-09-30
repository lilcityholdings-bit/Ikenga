# AI Ladder — the plan (revised)

This replaces the earlier "lifelong companion" plan. The earlier version tried to be too many
things at once. This one picks one customer, one grade range, and one question to answer first.

## Year 1 in one sentence

**AI Ladder is the 10-minute practice-and-measurement layer for AI literacy in grades 3-8. It's
used right after a lesson from a free curriculum teachers already have (Day of AI, Code.org), and
it shows each teacher what their class actually learned.**

We don't compete with Day of AI or Code.org. We make their lessons stick and show that they worked.

## What changed, and why

| Problem with the old plan | Fix | Status |
|---|---|---|
| Two customers with opposite needs: schools (one year, one district) vs. families (lifelong) | **Year 1 customer is the teacher. Year 2 buyer is the district.** The lifelong, family-owned companion is the long-term vision, not the pitch. Families get a downloadable copy of their child's record now, which keeps the door open without promising more. | Done in the plan. "Download my learning record" is built. |
| "Designed to step back" works against usage-based renewals | Year 1 success is measured by **weekly use plus measured learning gains** (check-ins). Independence becomes a selling point only once we have data showing it. | Done in the plan. Check-ins are built. |
| AI literacy has no class time; free competitors exist | Fit into time that already exists: a **teacher-set practice focus** makes the next session match whatever was just taught. We pitch alongside free curricula, not against them. | Built ("Practice focus" on the dashboard). |
| Claimed "no one else does teach-back" (untrue: teachable-agent research such as Vanderbilt's Betty's Brain exists) | Stop claiming any single feature is new. **Our edge is the combination:** one scale across grades, before/after measurement, calibrated difficulty, privacy by design, free for teachers. Credit prior research openly. | Done in the plan. |
| Rankings built on guessed difficulties | Difficulties are now **measured from real students** (`calibrate.py`). Levels are labelled **"estimated"** everywhere. The class board ranks by **growth**, not score. Raw ratings are never shown to classmates. | Built. |
| Unproven "four measures" | Year 1 reports only what we can defend: **skill estimates and before/after check-in scores.** Teaching, independence and calibration measures wait until pilot data can validate them. | Done in the plan. Check-ins are built. |
| Scope far too big for 18 months, and no team or budget | Everything past the pilot is **parked** behind named evidence gates (below). The plan now includes a team and budget section. | Done in the plan. |
| Vague success tests | **Numeric pilot gates** (below), measured by the product itself. | Built ("Active this week" and the check-in table on the dashboard). |
| Young kids get a thin "companion" | Don't promise young kids a companion. For K-5 the promise is **short, fun, read-aloud games**, which is what's actually built. | Done in the plan. |
| "AI that grows up with your child" draws scrutiny | Dropped from all year-1 messaging. No chatbot anywhere in the pilot. | Done in the plan. |
| Unreviewed content | **Students flag bad questions** (3 fixed reasons, no free text so no personal info), teachers see the flags, and `calibrate.py` lists them as a review queue. Two paid teacher reviewers go through all 97 challenges before the pilot. | Flagging is built. Reviewers need you. |
| Wrong repository, undecided name, no legal review | See "Needs you" below. | Needs you. |

## The pilot (months 0-3)

**Who:** 3 classrooms, grades 3-8, ideally in 2 different schools, with teachers who already
teach some AI or computer science.

**How:**
1. Week 0: the teacher opens a check-in called "Before" (6 questions, about 5 minutes).
2. Weeks 1-6: 2-3 practice sessions a week of 10-15 minutes, with the practice focus matched to
   that week's lesson.
3. Week 6: the teacher opens an "After" check-in with the same questions. The dashboard shows each
   student's change.
4. Run `calibrate.py` and fix every flagged question.

**Pass if all of these are true** (starting targets I picked. Agree on them with your pilot teachers before week 0, not after):

| Measure | Target | Where you see it |
|---|---|---|
| Students active in a typical week | at least 60% of each class, through week 6 | Dashboard: "Active this week" |
| Before → after check-in, class average | up by at least 15 percentage points | Dashboard: check-in table |
| Teachers who want to keep using it | at least 2 of 3 | Ask them |
| Flagged questions left unresolved | 0 | Dashboard and `calibrate.py` |

**If it fails:** the failures tell you what to change. Low activity means a classroom-fit problem,
so talk to the teachers. Low gains mean a content problem, so look at which skills didn't move. Do
not add features to rescue a failed pilot. Change one thing and pilot again.

Two cautions on reading the results. Three classrooms can't prove learning gains the way a proper
study can. They show whether teachers and kids will use it and whether results point the right way.
And part of any before-to-after gain is simply practice. It's still the most honest signal a small
pilot can give.

## Parked until the evidence exists

| Idea | Unpark when |
|---|---|
| Persistent "teach your robot" companion | The pilot passes, and kids say Teach the Robot is their favourite game type |
| District features (Clever / ClassLink / Google sign-in, school admin reports) | A district asks to pay, or a grant requires it |
| Grades 9-12 with a real AI model in teach-back mode | The pilot passes, plus a signed district data agreement and a safety review |
| Team leagues and a "class brain" | 10+ active classrooms |
| AI Passport and family-owned lifelong memory | Families or colleges ask for it, and a privacy lawyer approves the design |
| College level expansion, and a second subject | Paying district customers exist |

## Team and money (rough estimates — check them before relying on them)

- **People for the pilot:** you (teacher relationships, pilot running, feedback), one part-time
  developer (or continued sessions like this one), and **two teacher reviewers** (paid stipends)
  for content.
- **Costs:** hosting is minimal (one small server). The real costs are a short
  **education-privacy legal review**, teacher-reviewer stipends, and your time.
- **Funding paths:** education-innovation competitions (for example the Tools Competition), state
  AI-literacy or computer-science grants, and small foundation grants. Apply after the pilot, with
  the results in hand.
- **District sales take a long time** (often 6-18 months, plus a data-privacy agreement per
  district). The first revenue realistically comes in year 2. Plan cash for that.

## Before any real child uses it (compliance checklist)

- [ ] Short consult with an education-privacy lawyer (COPPA, FERPA, your pilot states' student-data
      laws).
- [ ] A one-page letter to parents: what's collected (a nickname, answers), what's not (names,
      emails, birthdays), how to delete it.
- [ ] Each pilot school's own approval process. Many use a standard student data privacy
      agreement.
- [ ] Host the server in the US, with backups, and a named person responsible for it.
- [ ] An accessibility pass (keyboard, screen reader, contrast) and a written summary of it.

## Needs you (I can't do these)

1. **Recruit the 3 pilot teachers and 2 content reviewers.**
2. **Book the privacy-lawyer consult.**
3. **Decide the name.** "AI Ladder" is safe. "Ikenga" is meaningful but needs input from Igbo
   cultural voices first.
4. **Approve moving the app to its own repository.** It currently lives inside the unrelated
   Ikenga trading-platform repo. Creating the new repo is quick once you say yes.
5. **Decide on budget:** how much you can spend before year-2 revenue.

## Next 30 days

| Week | Do |
|---|---|
| 1 | Teacher and parent conversations (5-10). Test the one-sentence pitch above. Pick the name. |
| 2 | Lawyer consult. Content reviewers start. Move the repo. Host a pilot server. |
| 3 | Fix reviewer findings. Draft the parent letter. Confirm the 3 pilot classrooms. |
| 4 | Train the teachers (30 minutes each). Pilot week 0: the "Before" check-in. |
