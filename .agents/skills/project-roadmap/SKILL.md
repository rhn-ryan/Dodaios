---
name: project-roadmap
description: >-
  Use when starting, pausing, or finishing work on Dodaios so the durable plan
  stays true. Reads docs/PROJECT_PLAN.md, picks the next unblocked action,
  updates milestone status and the decision log, and leaves a resume note after
  a crash. Trigger phrases: "what's next", "update the roadmap", "continue the
  project", "where did we leave off", "keep the plan current".
---

# Keep the Dodaios project plan current

The source of truth is `docs/PROJECT_PLAN.md`. It is written for a fresh agent:
Snapshot, Milestones, Next actions, Decisions. Never replace it with a chat
summary, and never delete a decision another agent made — append a newer one.

## Start of a work session

1. Read `docs/PROJECT_PLAN.md` §1 (resume protocol), §2 (snapshot) and §5 (next
   actions).
2. `git status --short` and `git log --oneline -5`. If the tree is dirty, the
   previous agent stopped mid-wave: read the diff before doing anything new.
3. `python3 tools/dodaios_launcher.py --status` for the real running state.
   Do not start a second server on the same port.
4. Pick the first **unblocked** next action. Do not re-plan the whole roadmap.

## While working

- Keep the milestone's acceptance test visible; that is the definition of done.
- If the direction changes (a provider is dropped, a design is rejected), note
  it immediately in §6 (open decisions) rather than at the end.
- Run focused tests, not the full suite. The plan names the useful files.

## End of a milestone-sized change

1. Update §2 Snapshot with what is now true (running service, default provider,
   voice path).
2. Update the milestone row in §3 and its detail in §4: what landed, what is
   still missing, the exact file paths.
3. Rewrite §5 Next actions so the first item is the single best next step.
4. If a direction changed, append a row to §8 Decision log with the date and
   why. Do not rewrite old rows.
5. Run `python scripts/agent_done_gate.py`; a turn is done only on `DONE`.
6. Commit the code and the plan together when the user asked for commits; keep
   `dist/` out of the commit (it is generated and untracked).

## Updating rules

- **Facts, not intentions.** "FLA is selectable" belongs in the plan only after
  a real turn succeeded; before that it is "picker fix landed, bundle rebuild
  pending".
- **One acceptance test per milestone.** If it cannot be stated as a check a
  person can run, the milestone is too vague.
- **Blockers name the decider.** "Needs the account owner's choice" is useful;
  "blocked" alone is not.
- **Keep it short.** This file is read at the start of every session. Prefer
  replacing stale text over appending a diary.

## When the plan and reality disagree

Reality wins. Fix the plan, then fix the code. A stale plan is worse than no
plan because the next agent trusts it.
