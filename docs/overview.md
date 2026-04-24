# Ouvrage — Architecture Overview

An MCP server that does two things, and they matter equally: it keeps a durable, retrievable record of planning context across sessions, and it dispatches autonomous Claude Code workers against git repositories. The knowledge side is not infrastructure underneath the orchestration — it's the reason the orchestration produces coherent work. Conversations, specs, decisions, task results, and review feedback persist in a searchable store. Workers pull from it; humans return to it.

The alternative is compaction — the summarisation pass a chat interface runs when its context window fills. Compaction crushes the reasoning behind decisions into a sentence, keeps the outcome, throws the nuance away. This system is the inverse. Structured, human-curated, retrievable context that keeps the reasoning intact. Continuity of idea development, not just of outputs.

Prompt engineering is what you do when you're building an agent — a chatbot, a workflow step, something with a specific role. **Context engineering** is what you do when you're putting agents to work: controlling what data is in their context, for how long, and with what boundaries, so they stay on task. Claude is a capable engineer; it will also happily build spaghetti if you let it wander. The recipe that works: tight scope, clear success criteria, clean specs, good rails. *Leave lots of room for how; leave very little room for what.*

Cognitive load management is context engineering pointed inward — what do I need to hold in working memory right now, and what should live in durable, retrievable storage. The same machinery solves both problems.

The dispatch layer rides on top of that. When a task dispatches, the worker runs in its own git branch, reports back through the same MCP protocol, and pulls context from the same store the planning session wrote into.

This document is the entry point for the architecture docs. Deep-dives live alongside it:

- [`context-engineering.md`](context-engineering.md) — storage, embeddings, retrieval, the MCP surface
- [`task-lifecycle.md`](task-lifecycle.md) — the finite state machine and the gate pipeline built on top of it
- [`prompt-engineering.md`](prompt-engineering.md) — the spec as artifact, collaborative drafting with Claude.ai, brief delivery to workers
- [`security-and-isolation.md`](security-and-isolation.md) — worker isolation, credential storage, auth model

---

## Why this exists

I'm a technical architect at a web agency in Montreal. My day is heavy context switching: client discovery, system design, implementation, reviews. I own a lot of moving pieces from start to finish and often swap between projects in the same hour.

My planning surface is a Claude.ai project. My execution surface is Claude Code. For months I was walking specs between the two by hand — write the design in Claude.ai, copy the spec as markdown, paste into a Claude Code session, watch the build, bring results back to Claude.ai, repeat. Manual plumbing that gets old fast, and in retrospect, context engineering done by hand.

Ouvrage started as a small MCP server — call it a BBS — that let my Claudes post in shared threads and read each other's messages. Around 350 lines. Claude.ai drafts the spec in a thread; Claude Code picks it up. That was the whole thing.

Over about six weeks it grew into a ~12,000-line orchestration system with solid test coverage. Somewhere in the middle of that growth the BBS became the first externalisation of my context discipline — instead of juggling tabs and compactions, I had a durable, retrievable record of what had been decided and why. Once I could run Claude Code *from* the VPS rather than through it, the message board became an orchestrator.

The system was built on a phone, from dog walks and my kitchen table, with the agents doing the build under direction.

## Goals / Non-goals

**Goals:**

- Give me one retrievable surface for planning context: conversations, specs, decisions, task results. Persistent, searchable, pinned-where-canonical.
- Dispatch autonomous coding work to Claude Code workers without sitting at a terminal.
- Run a review loop that catches problems before code ships, with feedback that feeds back into the next attempt.
- Work from any MCP client. Primarily Claude.ai (planning) and Claude Code (worker). Anything else that speaks MCP can connect.
- Stay a single-operator tool. One person, one VPS, one SQLite file.

**Non-goals:**

- Multi-user SaaS. A SaaS shape was briefly considered and rejected — the right shape would have been Docker-container-per-user anyway, which didn't improve on what was already there.
- A drop-in replacement for Jira or GitHub Projects. Ouvrage tracks work that agents do. It doesn't track sprints, story points, people.
- A general agent framework. It orchestrates Claude Code. LangGraph, AutoGen, and CrewAI solve different problems.
- Zero-config onboarding. This is a tool for someone comfortable with Docker, git, and an MCP-capable client. The README walks it; it isn't turnkey.

---

*Open any of the four deep-dives for detail on the subsystem you care about.*
