# Ouvrage — Prompt Engineering

Prompt engineering in this system is the work of producing a good brief. The specification the worker executes against, the context surrounding it, and the delivery mechanism that puts both in front of the model at dispatch. When the brief is right, the worker's output is good. When the brief is thin or ambiguous, no amount of prompt cleverness downstream recovers it.

This document describes how specs get drafted (collaboratively, with Claude.ai doing the tuning), how the brief gets delivered to the worker at dispatch, how the review gate curates context from the opposite direction, and how retry feedback closes the loop. The smaller concerns — stopping conditions, turns exhaustion, tool economics — are footnotes.

---

## Why this exists

Claude is a capable engineer. Given a sharp spec it ships good code; given an ambiguous one it reaches for plausible interpretations and produces plausible work. The difference between the two outputs is not the model's intelligence. It's what the model was told.

The bottleneck is brief quality. A brief is a specification paired with the surrounding context a worker needs to execute without guessing. The prompt engineering discipline in this system is how that brief gets produced and delivered. Clear boundaries around a task exist to keep a capable agent focused and to prevent long-tail drift on a job that would otherwise expand under its own momentum — they are not there to compensate for a weak model.

## Goals / Non-goals

**Goals:**

- Produce specs that are precise, testable, and agent-optimized — tuned for a worker reader rather than a human reader.
- Use the system's accumulated context to draft specs collaboratively with Claude.ai rather than writing from scratch.
- Deliver the brief to the worker in one injection at dispatch, with pull tools available if it needs more.
- Close the loop. Review verdicts and test output feed back into the next attempt's brief.

**Non-goals:**

- Chain-of-thought scaffolding in worker prompts. Modern Claude models reason internally; external scratchpads are counterproductive.
- Automatic prompt optimization, A/B testing, or learned prompt selection. Every prompt is hand-edited based on observed failure modes.
- A general-purpose prompt framework. The prompts here are shaped for Claude Code dispatching against git repositories with test and review gates.
- Fine-tuning or custom weights. Every worker runs against stock Claude models.

## Stack

- **Claude Code via the Agent SDK** (Python). `ClaudeSDKClient` for persistent sessions with mid-task message injection; `query()` for one-shot calls.
- **Opus** for worker sessions and review gates by default. Configurable per task (`model`, `review_model`).
- **MCP tools** as both the context-pull surface (the worker, and Claude.ai when drafting) and the callback surface (worker progress, gate results).

## 1. The spec is an artifact

A spec in this system is not a template-filled form, not an ad-hoc prompt written into a dispatch call, and not a one-shot markdown block pasted into a thread. It's the `spec` argument passed to `dispatch_task`, produced beforehand through collaborative drafting in a conversation, and attached to the task at creation time as a `type='spec'` message on the task thread.

**Where the spec comes from.** The drafting happens in a conversation — typically one scoped to a feature, a surface area, or a larger body of work that might span multiple tasks. That conversation carries its own pinned artifacts: the curated source-of-truth messages that describe the feature at the project level. Pinning-as-curation is a conversation-level concept in this system, not a task-level one. The task spec is drafted *from* conversation context (the pinned artifacts plus the raw discussion around them); it's not a second instance of pinning. A task thread carries its spec as a message, but there is no user-facing `pin` action on tasks and no `get_pinned` retrieval scoped to a task.

**The artifact contains:**

- What the task is supposed to accomplish, stated as an outcome.
- Constraints — what must be true, what must not change, where the boundaries are.
- A checklist of verifiable items the worker can mark done as it goes.
- Context the worker needs that isn't retrievable from the conversation — APIs, file locations, domain terms, integration points.
- General direction when direction helps: areas to look at, patterns the codebase uses, hints about where to start, named components that already do similar work.

**The artifact does not contain:**

- A transcript of the conversation that produced it.
- Implementation-level specifics — line numbers, exact function bodies, step-by-step code instructions. *Leave lots of room for how; leave very little room for what.* Over-specifying wastes the worker's context budget on detail it will reconstruct anyway and reduces the agent's usefulness as a designer.
- Rationale that belongs on the conversation's pinned artifacts instead. The spec is narrow and temporal; the rationale is wide and long-lived.

**Relationship to conversation pins.** The conversation pin is the feature-wide source of truth — the place the design rationale lives, what decisions were made and why. The task spec is narrower and scoped to a single unit of work. When drafting a task's spec, the conversation pin (and the rest of the conversation's raw stream) is the seed material. When a worker is briefed at dispatch, the spec is what gets injected; if the worker wants the broader design context, it pulls the conversation pin on demand via `get_pinned`.

## 2. Building specs with Claude.ai

The drafting is a conversation. The human and Claude.ai discuss the task — what it is, what's in scope, where the constraints are. Claude generates the candidate artifacts — spec drafts, checklist proposals, task breakdowns — and the human reviews, edits, rejects, or accepts. What gets accepted is what gets passed as the `spec` argument when the task dispatches.

Claude.ai connects to the service over MCP. It uses the same pull surface the worker uses — `search`, `get_pinned`, `read`, `read_task_messages` — plus tools the worker never touches: `list_tasks`, `get_task_status`, `list_attempts`, `get_dispatch_log`, and the various artifact-retrieval tools. No distinct "spec drafting" toolkit exists. The same retrieval machinery wears a different hat depending on who's calling.

What Claude.ai brings to the drafting session:

- **Corpus traversal at speed.** The human and Claude.ai discuss; Claude pulls related prior work, relevant decisions from the conversation pin, review notes from similar tasks, specs that shipped successfully in adjacent areas. Minutes instead of scroll-back hours.
- **Agent-reader tuning.** A human can write a spec a human would understand perfectly that a worker will misread. Claude.ai drafts specs optimized for the agent that will consume them — explicit about what's in scope, explicit about what isn't, structured in a way a worker scans well. This is the value that matters most.
- **Proposing structure.** Breaking a large piece of work into a dependency chain, or a sequence of smaller tasks, or a single task with a detailed checklist. The shape of the work, not just its content.
- **Catching gaps before the worker does.** *"The spec doesn't say what happens when the upstream service is down."* *"The checklist has an item that requires a credential we haven't granted."* Review before the worker even starts.

The human directs, reviews, and decides. Claude generates candidates against the accumulated context and tunes them for an agent reader. The output of the session is the spec that becomes the task's brief.

## 3. Delivering the brief to the worker

A dispatched worker starts with a system prompt that renders at about 230 lines. The skeleton is stable across tasks; the per-task content is the spec and its surrounding context.

What gets injected at dispatch:

```
identity          You are an Ouvrage worker
context           dispatched_by, project_id, branch, worktree_path, task_id
goal              verbatim from the task record
spec              verbatim from the pinned task spec
checklist         enumerated with item_ids the worker marks as it goes
how to work       phase transitions, progress cadence, callback tools
escalation        stop conditions, when to ask vs decide
completion        push branch, clean worktree, post handoff + result
```

Everything above *how to work* is per-task content; everything below is the stable skeleton.

Opening lines of the rendered prompt:

> You are an Ouvrage worker
> Dispatched by {dispatched_by} for project {project_id}
> Branch: {branch} | Worktree: {worktree_path} | Task ID: {task_id}
>
> You are a headless remote worker. The user is not watching your terminal.
> They see your work through the dashboard: phase, checklist, and posted messages.

The worker knows it's running headlessly. That framing justifies the rest of the prompt — why it should post progress messages (the dashboard is how the user sees progress), why it should mark checklist items as it goes (the dashboard is how the user sees completion), why it should push the branch (the dashboard is not where code is, the remote is).

**How to work**, the stable guidance:

> Post a `progress` message every time you complete a major step — the user watches the dashboard, not your terminal. Update your phase as you transition: grounding → implementing → testing → finishing. Mark checklist items done as you go, not all at once at the end.
>
> Key tools: `post_task_message`, `update_task_phase`, `update_task_checklist`, `add_checklist_item`, `add_task_file`, `git_push`, `git_fetch`.

The instruction is specifically to broadcast. A worker that emits progress messages is legible. A worker that works silently for 20 minutes and posts a single "done" is illegible — if it went sideways at minute 5, nobody sees it until minute 20.

**Retrieval ordering**, when the worker reaches for more:

> The task spec and checklist are already in your prompt. On retry, test failures and reviewer notes are also in your prompt. You have tools to fetch more if the brief is incomplete — `get_pinned` for the conversation-wide spec, `search` for specifics, `read_task_messages` for the task's own thread. A well-constructed spec should avoid the need, but the tools are there.

Narrow-to-broad on demand. Task thread first, conversation pin second, search third, full history last. The injected brief is the center; the pull surface is the escape hatch.

**Completion**, the stable closing:

> Always push your branch before finishing — unpushed code has no value.
> Before posting your result, run `git status`. Your worktree MUST be clean.
>
> Sequence:
> 1. Ensure all checklist items are updated
> 2. Run `git status` — worktree must be clean
> 3. Push your branch: `mcp__ouvrage__git_push(task_id='{task_id}')`
> 4. Post a `handoff` message with key decisions, gotchas, notes
> 5. Post a `result` message (under 5 lines: what you did, files modified, caveats)

The worker's callback tools along the way:

| Tool | Purpose |
|---|---|
| `post_task_message` | Progress, questions, results, handoffs, plans. |
| `update_task_phase` | Move through `analysis → implementing → testing → finishing`. |
| `update_task_checklist` | Mark items done. |
| `add_checklist_item` | Add items the spec missed. |
| `escalate` | Explicit blocker signal for human review. |
| `git_push` / `git_fetch` | Authenticated git via the platform; the service holds the credential. |

Workers don't call `transition_task` — they don't drive their own lifecycle. A worker that posts a `result` message in the `finishing` phase is the signal for the system to transition the task; the dispatch engine issues the transition on the worker's behalf.

## 4. The review gate — curated context from the opposite direction

The review gate is the second half of the system's curation discipline. The worker was given a spec to execute; the reviewer is given the spec plus what the worker produced and told to render a judgment.

The review runs as a subtask in the worker's worktree — a new Claude Code session, same repository state, scoped to reviewing the diff. Own `session_id`, separate token budget, inactivity timeout of 300 seconds, model configurable via `review_model` (default `opus`). Cost tracked on the subtask and rolled up to the parent.

What the reviewer is briefed with:

- The full spec, verbatim.
- Course corrections the user posted to the task thread during the worker's run.
- Prior review history if this is a retry.
- The punchlist (items the worker claimed to complete).
- Component context if the task references registered components.
- Filesystem access to the worktree — the reviewer reads whatever code it needs.

What the reviewer is **not** briefed with:

- The worker's internal session log.
- The worker's `progress` and `handoff` messages. `result` messages are visible because those are user-facing outputs; the narration of how the work got done is not.
- The worker's reasoning.

The blinding is deliberate. A reviewer that reads the implementer's self-assessment of their own code rubber-stamps it. A reviewer that reads the spec, the diff, and nothing else is forced to form an independent judgment.

The stakes framing in the prompt:

> You are the final gate before code ships. If you approve → PR created or branch merged, dependent tasks dispatch. If you request changes → worker is retried with your feedback as revision instructions — this costs real time and money.

Output shape is strict:

- `title="APPROVED"` with a brief summary, or
- `title="CHANGES REQUESTED"` with `### Blockers` and `### Suggestions` sections.

The dispatch engine parses the title to decide the transition. `APPROVED` fires `gate_pass`; `CHANGES REQUESTED` fires `gate_fail`. *Approve with notes* is supported — title `APPROVED`, body includes suggestions; the suggestions surface in the task thread but don't trigger retry.

**Retry leniency.** On the second and third review attempts, the prompt adds a leniency instruction:

> This is a retry. Prior attempts already consumed resources. Only request changes for: bugs, unmet spec requirements, security issues, missing tests. Do NOT reject for style, naming, or cosmetic issues on retries.

Without the instruction, reviewers on retry find new things to complain about and the loop never converges. With it, the third pass either approves or escalates out.

Inactivity detection is two strikes. If the reviewer's session goes idle for 300 seconds, the first stall resumes the same session; the second stall halts with `gate_status = needs-review` and a human decides.

## 5. Feedback injection on retry — closing the loop

When a gate fails, the next dispatch is a retry — a **fresh Claude Code session** with the previous attempt's failure signal written verbatim into its opening prompt. The loop is the mechanism that incorporates the last attempt's specific failure into the next attempt's brief.

The new-session choice is deliberate. A resumed session would inherit the prior attempt's SDK context, which the SDK may compact and summarise as the task grows long — exactly the content the retry most needs to preserve. Fresh sessions keep the reviewer feedback and test output intact in user-role context, where it's load-bearing.

`collect_review_feedback()` queries the task thread for all messages with `type='review'` posted since the last worker completion. For a test-gate failure, the captured test output from `last_test_output` is surfaced the same way. The prompt builder pastes both into a block at the top of the next attempt's prompt:

```
# ⚠️ REVISION REQUESTED
This task was previously completed but needs revisions based on review feedback.
Your primary job is to address the feedback below.
This is attempt N.

### CHANGES REQUESTED
{reviewer's message body, verbatim}

### Test failure — attempt N-1
{stdout tail, exit code, command, verbatim}
```

Three properties:

**Verbatim.** No summarization, no interpretation. If the feedback is specific, the worker addresses specifics. If it's vague, the worker has to decide what to do — same as a human receiving vague PR feedback. Summarization discards the reviewer's precise phrasing; precise phrasing is often where the signal is.

**Positioned at the top.** Primacy. The prompt says *your primary job is to address the feedback below* before the original spec appears. The worker is biased toward the feedback, not the original scope.

**New session, no prior conversation.** The retry dispatches fresh — a different `session_id`, no SDK context carried from the previous attempt. The worker re-pays the grounding cost of re-reading the spec and any code it needs, but the trade-off is that the injected feedback and test output are guaranteed unmangled. User-triggered retries can optionally fork from the prior attempt's session, but gate-triggered retries (test failure, review rejection) default to a clean session by design.

**Mid-task message injection** uses the same verbatim-context discipline. When a human or Claude.ai posts a message to a task thread while the worker is running, a background poller (5-second cadence) detects the new message and injects it via `client.query()`:

```
--- LIVE MESSAGE FROM {author} ({type}) ---
{content}
--- END LIVE MESSAGE ---

The above message was just posted to your task thread. Read it carefully
and adjust your work accordingly.
```

Explicit framing. The worker knows the text came from outside, not from its own reasoning. Worker-generated and dispatcher-generated messages are filtered out — the worker doesn't inject its own posts back into its own context.

## 6. Behavioral rails — footnotes

Worker prompts include a section on escalation and stop conditions. These are the margin cases — the places where an otherwise-well-specified task runs into something the spec didn't anticipate. They exist to prevent a capable agent from confidently going sideways when the right move is to surface the ambiguity.

The stop conditions enumerated in the prompt:

```
- Stuck → post a question message. Pauses your session until a human responds.
- Ambiguous spec → post a question. Don't guess.
- Scope significantly larger than expected → update phase to needs-review and explain.
- Fundamental blocker → call escalate(task_id, reason) to flag for human review.
```

Each has a named mechanism. The prompt never says *try harder before you ask*. Escalation is framed as the worker doing its job, not as failure.

**Budget exhaustion** is the system's involuntary escalation. When a worker runs out of its allotted budget before completing, the SDK returns a terminal signal; the dispatch engine catches it, pushes the branch to preserve whatever was done, transitions the task to `stopped` with an appropriate reason, and posts a message to the thread noting what happened. Not *failed* and not auto-retried. A human decides whether to resume, retry, or cancel.

**Inactivity timeout** in review subtasks — 300 seconds, two strikes, then halt. Covered under the review gate.

Escalation is an open problem at the margin. Explicit stop conditions, framing escalation as success, targeted prompt language — all help. When specs are tight enough, escalation is rarely needed; when specs are loose, escalation is where quality is preserved. The better answer is to write tighter specs, which is what this discipline is actually about.

## 7. Tool-surface economics

Fewer tools in the window, fewer decision points, more budget for the actual work.

The lifecycle actions — `start_task`, `stop_task`, `pause_task`, `resume_task`, `cancel_task`, `close_task`, `approve_task`, `hold_task`, `skip_gate` — all collapsed into `transition_task(task_id, action, **context)`. The action is a string; the tool dispatches into `lifecycle.execute()`, which validates against the transition table.

Two effects:

**Fewer tool definitions in the prompt.** Each tool definition takes schema space that a worker reads on every turn. Consolidating lifecycle actions made room for tools that actually needed distinct schemas — the callback tools (`post_task_message`, `update_task_checklist`, `update_task_phase`) do different things and deserve separate signatures.

**Less decision-surface ambiguity.** A controller asked to "cancel the task" previously had to pick between `cancel_task`, `stop_task`, and `close_task`. With `transition_task`, it picks an action. The FSM tells it what's legal; an illegal action returns an error listing the actions that are valid for the current state.

The principle: collapse tools that differ only in the action they imply on the same entity. Keep tools that differ in schema or in the entity they touch. `post_task_message` and `post` (to a conversation) stay separate because they write to different tables; `cancel_task` and `close_task` merged because they were the same tool wearing two hats.

Workers don't call `transition_task`. Workers post messages, update phases, toggle checklist items. The FSM transitions are system-driven: a `result` message in the `finishing` phase triggers the worker's completion transition; a gate outcome triggers `gate_pass` or `gate_fail`. Workers don't decide their own status.

## Alternatives considered

- **Automated spec generation from a natural-language task description.** Bypass the collaborative drafting; have Claude.ai one-shot a spec from a two-sentence ticket. Rejected because the human's framing — what's in scope, what's out, what tradeoffs are acceptable — is where most of the spec's value comes from. Automation can draft; the human has to decide.
- **Inject the conversation pin automatically into the worker prompt.** Every worker gets the project-wide source of truth injected alongside the task spec. Rejected because it bloats the prompt and assumes the pin's content is always relevant to the task at hand. The task spec carries what the worker needs; the conversation pin is available on demand when it doesn't.
- **Non-blind review — pass the worker's notes to the reviewer.** Rejected for rubber-stamping reasons. A reviewer who reads the implementer's justification finds it persuasive; a reviewer who reads the spec and the diff forms an independent judgment.
- **Review via rule-based lint and type checks only.** Replace the Claude review with linters, type checkers, and test gates. Rejected because the review catches intent mismatch against the spec, incomplete implementations that pass tests, and subtle edge cases. The test gate already handles what tests handle; review is a judgment layer above it.
- **Finer-grained escalation tools — `escalate_blocker`, `escalate_scope`, `escalate_ambiguity`.** One tool per escalation shape. Rejected because the distinctions would have to be communicated in the tool descriptions anyway; one `escalate` tool with a `reason` argument covers it and leaves the shape to the worker's prose.
- **Fine-tuning a model on the system's conventions.** Custom weights optimized for worker behavior in this project. Rejected for operational cost and the speed of model evolution — a custom weight locks in to a base model version; stock Claude upgrades propagate automatically.

## Tradeoffs

- **Spec quality depends on human attention.** Claude.ai amplifies; the human still has to show up and frame. A spec drafted without enough human input produces a worker execution without enough direction.
- **Verbatim feedback injection is faithful but long.** A review with a 2,000-character critique makes the retry prompt 2,000 characters longer. Cheaper than summarizing and risking meaning loss.
- **Fresh-session retries re-pay grounding cost.** A retry doesn't carry the prior attempt's SDK context, so the worker re-reads the spec and re-explores the code it needs. The trade-off is that the injected feedback is guaranteed intact; a resumed session risks having the feedback compacted into a one-line summary by the time the model acts on it.
- **Blinding the reviewer costs explanation.** The reviewer doesn't see notes that might clarify a design choice. A suggestion that's actually wrong because the worker had a good reason the reviewer can't see comes back as CHANGES REQUESTED, and the worker re-explains. The alternative (rubber-stamping) is worse.
- **Prompts are edited by hand.** No automatic tuning. Every observed failure mode requires a human to notice and rewrite a section. Slow; also means every change is deliberate.
- **Agent-optimized specs may read strangely to humans.** A spec tuned for how a worker scans has structural choices — explicit scope boundaries, enumerated non-goals, redundant phrasing around fragile semantics — that a human reader might find over-spelled. The audience is the agent; human-reader polish is a non-goal.

## Cross-cutting concerns

- **Cost.** Opus is expensive. Every retry is a new session against the original context plus feedback. Retry caps (3 test, 2 review by default, each configurable per task) are deliberate. Token usage is tracked per attempt and rolled up to the task.
- **Observability.** Every worker session writes `.ouvrage/session.jsonl` in the worktree — every tool call, every result, every user message. The dashboard surfaces recent entries; `get_session_log` retrieves the full file. A worker that went sideways leaves a trail.
- **Auth.** Worker sessions reach back through `/mcp/worker` (localhost bypass). Prompts don't contain tokens or credentials — the MCP server authenticates by process context, not by something the worker has to remember.
- **Prompt evolution.** Prompts are in source control. Changes are committed with rationale. Every major change has been motivated by a specific failure mode observed in production sessions.

## Riff points

- Prompt engineering here is the work of producing a good brief. The spec is the artifact; the prompt is the delivery mechanism.
- Models are capable. Clear boundaries keep a capable agent focused, not compensate for weakness.
- Specs are produced through human + Claude.ai collaboration, using the same MCP pull surface the worker uses. Claude.ai amplifies; the human frames and decides.
- Claude.ai's specific value in drafting: tuning specs for the agent that will consume them. Humans write specs humans understand; agents often need different structure.
- The brief injected at dispatch: goal, spec, checklist, identity, escalation protocol, completion sequence. Pull tools for anything beyond.
- Review gate is curation from the opposite direction. Reviewer sees spec + diff + corrections, blind to implementer notes.
- Retry feedback is injected verbatim into the next prompt. Gate-triggered retries dispatch as fresh sessions so feedback doesn't get compacted away.
- `transition_task` collapsed eight lifecycle actions into one tool. Workers don't call it — they update phase, post messages, and let the system transition them.
- Stopping conditions are footnotes. Tighter specs are the real work.
