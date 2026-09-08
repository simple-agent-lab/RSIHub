---
name: task-execution
description: Solve repository tasks by inspecting first, making focused edits, and verifying the result.
---

# Task Execution

1. Read the task instruction and inspect the relevant repository files.
2. Form a concrete hypothesis before editing.
   If an execution deadline is supplied, check remaining time before a long
   operation and when changing approach. Reserve time for a usable result and
   its final verification; choose probes and tool waits that fit what remains.
   A shorter run is not success if required work is missing. Do not invent a
   deadline when the runtime reports that it is unavailable.
3. Make the smallest coherent change that solves the task.
4. Run focused checks, then broaden verification when the change has shared impact.
5. Leave generated artifacts and unrelated files untouched.
